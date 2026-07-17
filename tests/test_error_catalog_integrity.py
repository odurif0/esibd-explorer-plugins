import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
ERROR_CATALOG = Path("vendor/runtime/error_codes.json")
CANONICAL_FOLDER = "amx"
EXPECTED_CATALOG_COUNT = 13
EXPECTED_DEBUG_OUTPUT_MESSAGE = "Error opening the file for debugging output"

PLUGIN_FOLDERS = (
    "ampr_a",
    "ampr_b",
    "amx",
    "amx_a",
    "amx_b",
    "amx_hd",
    "dmmr",
    "esi",
    "psu_a",
    "psu_b",
    "psu_c",
    "psu_d",
    "psu_e",
)


def catalog_path(root: Path, folder: str) -> Path:
    return root / folder / ERROR_CATALOG


def catalog_paths(root: Path = REPO_ROOT) -> tuple[Path, ...]:
    return tuple(catalog_path(root, folder) for folder in PLUGIN_FOLDERS)


def load_catalog(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8") as stream:
        catalog = json.load(stream)
    assert isinstance(catalog, dict), f"{path} did not parse as a JSON object"
    assert all(
        isinstance(code, str) and isinstance(message, str)
        for code, message in catalog.items()
    ), f"{path} did not parse as a string-to-string map"
    return catalog


def catalog_drift(
    actual: dict[str, str],
    expected: dict[str, str],
) -> dict[str, dict[str, str | None]]:
    differences = {}
    for code in sorted(set(actual) | set(expected)):
        if actual.get(code) != expected.get(code):
            differences[code] = {
                "actual": actual.get(code),
                "expected": expected.get(code),
            }
    return differences


def assert_catalog_matches_canonical(
    catalog_file: Path,
    canonical_file: Path = catalog_path(REPO_ROOT, CANONICAL_FOLDER),
) -> None:
    catalog = load_catalog(catalog_file)
    canonical = load_catalog(canonical_file)
    assert catalog == canonical, (
        f"{catalog_file}: catalog drift from {canonical_file}: "
        f"{catalog_drift(catalog, canonical)}"
    )


def test_authoritative_error_catalog_list_is_complete() -> None:
    paths = catalog_paths()
    assert len(paths) == EXPECTED_CATALOG_COUNT
    assert [path.relative_to(REPO_ROOT).as_posix() for path in paths] == [
        f"{folder}/vendor/runtime/error_codes.json" for folder in PLUGIN_FOLDERS
    ]
    for path in paths:
        assert path.is_file()


def test_canonical_debug_output_message_is_correct() -> None:
    catalog = load_catalog(catalog_path(REPO_ROOT, CANONICAL_FOLDER))
    assert catalog["-400"] == EXPECTED_DEBUG_OUTPUT_MESSAGE


@pytest.mark.parametrize("folder", PLUGIN_FOLDERS)
def test_error_catalog_matches_amx_reference(folder: str) -> None:
    assert_catalog_matches_canonical(catalog_path(REPO_ROOT, folder))


def test_catalog_comparison_rejects_value_drift(tmp_path: Path) -> None:
    canonical = load_catalog(catalog_path(REPO_ROOT, CANONICAL_FOLDER))
    canonical_file = tmp_path / "canonical_error_codes.json"
    drifting_file = tmp_path / "drifting_error_codes.json"

    canonical_file.write_text(json.dumps(canonical), encoding="utf-8")
    drifting_catalog = dict(canonical)
    drifting_catalog["-400"] = "Changed debug output message"
    drifting_file.write_text(json.dumps(drifting_catalog), encoding="utf-8")

    with pytest.raises(AssertionError) as excinfo:
        assert_catalog_matches_canonical(drifting_file, canonical_file)

    message = str(excinfo.value)
    assert str(drifting_file) in message
    assert "-400" in message
    assert "Changed debug output message" in message
    assert EXPECTED_DEBUG_OUTPUT_MESSAGE in message
