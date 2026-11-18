"""The source a plugin entrypoint runs, for tests that read its functions instead of importing it."""
from __future__ import annotations

import ast
from pathlib import Path
import re

# Code an entrypoint runs from its own folder as if written in it (the AMX plugins share theirs this way):
# the file, and the entrypoint statement that runs it.
SHARED_FILES = {
    "_runtime/_amx_common.py": re.compile(r"^exec\(compile\(_AMX_COMMON\.read_text\(.*$", re.M),
}


def shared_files(entrypoint) -> list[Path]:
    """The shared files an entrypoint runs (none for most plugins)."""
    path = Path(entrypoint)
    return [path.parent / name for name, statement in SHARED_FILES.items()
            if (path.parent / name).is_file() and statement.search(path.read_text(encoding="utf-8"))]


def entrypoint_code(entrypoint) -> str:
    """The entrypoint as it runs: each shared file it runs written in place of the statement that runs it."""
    path = Path(entrypoint)
    text = path.read_text(encoding="utf-8")
    for name, statement in SHARED_FILES.items():
        shared = path.parent / name
        if shared.is_file():
            code = shared.read_text(encoding="utf-8")
            text, count = statement.subn(lambda _match, code=code: code, text)
            assert count <= 1, f"{path} runs {name} {count} times"
    return text


def entrypoint_tree(entrypoint) -> ast.Module:
    """The module an entrypoint runs (its shared code included), parsed."""
    return ast.parse(entrypoint_code(entrypoint), filename=str(entrypoint))
