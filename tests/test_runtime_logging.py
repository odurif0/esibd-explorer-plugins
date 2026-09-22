"""Driver imports and bounded logs must work in Explorer's frozen Python runtime."""

from pathlib import Path
import subprocess
import sys

import pytest

from conftest import PLUGIN_SPECS

ROOT = Path(__file__).resolve().parents[1]

# Use a fresh interpreter: pytest or another plugin can already have imported
# logging.handlers, hiding the missing-module failure seen in packaged Explorer.
PROBE = r'''
import importlib.util
import io
import logging
from pathlib import Path
import sys

runtime = Path(sys.argv[1])
log_dir = Path(sys.argv[2])
missing_handlers = sys.argv[3] == "missing"
if missing_handlers:
    sys.modules["logging.handlers"] = None
else:
    from logging.handlers import RotatingFileHandler as StandardHandler

namespace = "_logging_probe_runtime"
spec = importlib.util.spec_from_file_location(
    namespace, runtime / "__init__.py", submodule_search_locations=[str(runtime)]
)
module = importlib.util.module_from_spec(spec)
sys.modules[namespace] = module
original_path = list(sys.path)
spec.loader.exec_module(module)
assert sys.path == original_path
assert module.__all__ and all(callable(getattr(module, name)) for name in module.__all__)
common = sys.modules[f"{namespace}._driver_common"]
if not missing_handlers:
    assert common.RotatingFileHandler is StandardHandler

kwargs = dict(instrument_name="AMPR", device_id="probe_com13", logger=None,
              log_dir=log_dir, source_file=str(runtime / "ampr" / "ampr.py"))
logger = common.build_device_logger(**kwargs)
assert common.build_device_logger(**kwargs) is logger
assert len(logger.handlers) == 1
handler = logger.handlers[0]
assert handler.maxBytes == 1_000_000
assert handler.backupCount == 3
assert not logger.propagate

# Exercise several rotations at a small limit, including pre-existing backups.
handler.maxBytes = 512
messages = [f"sample {index:03}: V=1.000 I=0.002" for index in range(80)]
original_replace = Path.replace
if missing_handlers:
    def checked_replace(source, target):
        # Model Windows' restriction, which a Linux-only rename test misses.
        assert previous_stream.closed
        assert handler.stream is None
        return original_replace(source, target)
    Path.replace = checked_replace
try:
    for message in messages:
        previous_stream = handler.stream
        logger.info(message)
    handler.flush()
    filename = Path(handler.baseFilename)
    paths = [filename.with_name(filename.name + f".{index}") for index in (3, 2, 1)] + [filename]
    assert set(log_dir.iterdir()) == set(paths)
    assert all(0 < path.stat().st_size <= handler.maxBytes for path in paths)
    retained = [line.split(" - INFO - ", 1)[1]
                for path in paths for line in path.read_text(encoding="utf-8").splitlines()]
    assert 0 < len(retained) < len(messages)
    assert retained == messages[-len(retained):]

    # Unicode and multi-line diagnostics must survive logging and rotation.
    previous_stream = handler.stream
    logger.warning("Tension mesurée : 12 µV\nÉtat : arrêt confirmé")
    handler.flush()
    assert "Tension mesurée : 12 µV\nÉtat : arrêt confirmé" in filename.read_text(encoding="utf-8")

    if missing_handlers:
        # Rotation errors must not escape into hardware control. A subsequent
        # record must reopen the file and rotate normally once access recovers.
        def denied_replace(source, target):
            raise PermissionError("rotation denied")
        Path.replace = denied_replace
        original_stderr = sys.stderr
        errors = io.StringIO()
        sys.stderr = errors
        handler.maxBytes = 1
        try:
            logger.error("rotation failure must not abort control")
        finally:
            sys.stderr = original_stderr
            Path.replace = original_replace
        assert "rotation denied" in errors.getvalue()
        logger.info("logging recovered")
        handler.flush()
        assert "logging recovered" in filename.read_text(encoding="utf-8")
finally:
    Path.replace = original_replace
    logger.removeHandler(handler)
    handler.close()

# An injected host logger still works without creating any driver log files.
stream = io.StringIO()
external = logging.Logger("host_logger", level=logging.INFO)
external.addHandler(logging.StreamHandler(stream))
kwargs.update(logger=external, log_dir=log_dir / "unused")
adapter = common.build_device_logger(**kwargs)
adapter.info("device connected")
assert stream.getvalue() == "probe_com13 - device connected\n"
assert not (log_dir / "unused").exists()
'''


@pytest.mark.parametrize("spec", [s for s in PLUGIN_SPECS if s.runtime_family], ids=lambda spec: spec.folder)
@pytest.mark.parametrize("handlers", ["available", "missing"])
def test_runtime_import_and_log_rotation_without_optional_stdlib(spec, handlers, tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", PROBE,
         str(ROOT / spec.folder / "vendor" / "runtime"), str(tmp_path), handlers],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not result.stderr, result.stderr


def test_logging_fallback_does_not_hide_an_unrelated_import_failure():
    code = r'''
import builtins
import importlib.util
from pathlib import Path
import sys

original_import = builtins.__import__
def broken_import(name, *args, **kwargs):
    if name == "logging.handlers":
        raise ModuleNotFoundError("unrelated missing dependency", name="missing_dependency")
    return original_import(name, *args, **kwargs)
builtins.__import__ = broken_import
runtime = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location(
    "_logging_dependency_probe", runtime / "__init__.py",
    submodule_search_locations=[str(runtime)],
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
try:
    spec.loader.exec_module(module)
except ModuleNotFoundError as exc:
    assert exc.name == "missing_dependency", exc
else:
    raise AssertionError("Unrelated import failure was silently swallowed")
'''
    result = subprocess.run(
        [sys.executable, "-c", code, str(ROOT / "ampr_a" / "vendor" / "runtime")],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
