"""README identity checks for standalone plugin folders."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class ReadmeContract:
    folder: str
    plugin_manager_name: str
    readme_title_name: str


README_CONTRACTS: tuple[ReadmeContract, ...] = (
    ReadmeContract("ampr_a", "AMPR_A", "AMPR_A"),
    ReadmeContract("ampr_b", "AMPR_B", "AMPR_B"),
    ReadmeContract("amx", "AMX", "AMX"),
    ReadmeContract("amx_a", "AMX_A", "AMX_A"),
    ReadmeContract("amx_b", "AMX_B", "AMX_B"),
    ReadmeContract("amx_hd", "AMX_HD", "AMX HD"),
    ReadmeContract("dmmr", "DMMR", "DMMR"),
    ReadmeContract("psu_a", "PSU_A", "PSU_A"),
    ReadmeContract("psu_b", "PSU_B", "PSU_B"),
    ReadmeContract("psu_c", "PSU_C", "PSU_C"),
    ReadmeContract("psu_d", "PSU_D", "PSU_D"),
    ReadmeContract("psu_e", "PSU_E", "PSU_E"),
)


def validate_readme_identity(
    readme_text: str,
    contract: ReadmeContract,
) -> tuple[str, ...]:
    lines = readme_text.splitlines()
    actual_title = lines[0] if lines else ""
    expected_title = f"# {contract.readme_title_name} Plugin"
    expected_activation = f"Enable the `{contract.plugin_manager_name}` plugin"
    expected_folder = f"keep the whole `{contract.folder}/` directory"

    problems: list[str] = []
    if actual_title != expected_title:
        problems.append(
            f"{contract.folder}: title name expected {expected_title!r}, "
            f"got {actual_title!r}"
        )
    if expected_activation not in readme_text:
        problems.append(
            f"{contract.folder}: Plugin Manager name missing "
            f"{expected_activation!r}"
        )
    if expected_folder not in readme_text:
        problems.append(
            f"{contract.folder}: exact folder missing {expected_folder!r}"
        )
    return tuple(problems)


@pytest.mark.parametrize(
    "contract",
    README_CONTRACTS,
    ids=lambda contract: contract.folder,
)
def test_plugin_readme_matches_documented_identity(
    contract: ReadmeContract,
) -> None:
    readme_path = REPO_ROOT / contract.folder / "README.md"
    problems = validate_readme_identity(
        readme_path.read_text(encoding="utf-8"),
        contract,
    )

    assert problems == (), "\n".join(problems)


def test_readme_validator_rejects_wrong_identity(tmp_path: Path) -> None:
    readme_path = tmp_path / "README.md"
    readme_path.write_text(
        "\n".join(
            (
                "# PSU_A Plugin",
                "",
                "5. Enable the `PSU_A` plugin in the Plugin Manager.",
                "",
                "To copy this plugin to another machine, keep the whole "
                "`psu_a/` directory together.",
            )
        ),
        encoding="utf-8",
    )

    problems = validate_readme_identity(
        readme_path.read_text(encoding="utf-8"),
        ReadmeContract("psu_b", "PSU_B", "PSU_B"),
    )

    assert problems == (
        "psu_b: title name expected '# PSU_B Plugin', got '# PSU_A Plugin'",
        "psu_b: Plugin Manager name missing 'Enable the `PSU_B` plugin'",
        "psu_b: exact folder missing 'keep the whole `psu_b/` directory'",
    )
