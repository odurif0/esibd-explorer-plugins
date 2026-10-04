"""All Jupyter notebooks live in notebooks/, outside the plugin folders and tests."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_notebooks_live_in_the_notebooks_folder():
    notebooks = [path.relative_to(ROOT) for path in ROOT.rglob("*.ipynb")
                 if not any(part.startswith(".") for part in path.relative_to(ROOT).parts)]
    assert notebooks, "expected the repository notebooks"
    misplaced = [str(path) for path in notebooks if path.parts[0] != "notebooks"]
    assert misplaced == [], f"move these notebooks to notebooks/: {misplaced}"
