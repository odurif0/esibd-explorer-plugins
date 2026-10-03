# ESIBD Explorer Plugins

One plugin per device, plus standalone scan plugins.

## Current bundle

14 standalone plugin folders: `ampr_a`, `ampr_b`, `amx_a`, `amx_b`, `amx_hd`, `dmmr`, `esi`, `mscan`, `psu_a`, `psu_b`, `psu_c`, `psu_d`, `psu_e`, `tpg366`.

The 12 DLL-backed device folders own their entrypoint, icons, bundled runtime, device-specific vendor headers/DLLs, and `vendor/runtime/error_codes.json`. `mscan` is a scan, with its own entrypoint, icon and LICENSE; no runtime, DLL or catalog. `tpg366` is a USB pressure device with its own `_runtime/_tpg366.py` ASCII protocol, icons and LICENSE; no vendor DLL or error catalog.

## Architecture

- Entry points expose `providePlugins()`.
- Bundled runtimes load privately through `importlib.util.spec_from_file_location` into `_esibd_bundled_*` modules.
- Loader code registers those modules in `sys.modules` and does not mutate `sys.path`.
- Missing bundled runtime raises `ModuleNotFoundError`.

## Parity canonicals

- `ampr`: canonical `ampr_a`, sibling `ampr_b`
- `amx`: canonical `amx_a`, sibling `amx_b`
- `psu`: canonical `psu_a`, siblings `psu_b`, `psu_c`, `psu_d`, `psu_e`
- `amx_hd` and `dmmr` are standalone

## Autonomy constraints

- No symlinks anywhere inside a plugin tree.
- Required files must be regular files.
- Entrypoints must not statically import sibling plugin slugs.
- Keep each plugin tree self-contained.

## Error catalogs

- `tests/test_error_catalog_integrity.py` treats `amx_a/vendor/runtime/error_codes.json` as the canonical catalog.
- All 12 catalog files must match that canonical file exactly.

## Add / remove plugin sync points

Update these together:

1. `tests/conftest.py` (`PLUGIN_SPECS`)
2. `tests/test_documentation_integrity.py` (`README_CONTRACTS`)
3. `tests/test_error_catalog_integrity.py` (`PLUGIN_FOLDERS`, `EXPECTED_CATALOG_COUNT`)
4. `README.md` (plugin table + Quick Start tree)

If family membership changes, also update `tests/test_plugin_family_parity.py` (`PARITY_FAMILIES`, `EXPECTED_GROUP_SIZES`).

## Focused test guidance

Run the smallest check that covers the change, then widen only if needed.

```bash
python3 -m pytest -q
python3 -m pytest -q tests/test_plugin_family_parity.py
python3 -m pytest -q 'tests/test_plugin_family_parity.py::test_sibling_family_matches_canonical[amx]'
ESIBD_RELEASE_ZIP=/path/to/file.zip python3 -m pytest -q tests/test_release_archive_integrity.py
```

## Release archive behavior

- Releases are validated from a single archive path.
- If `ESIBD_RELEASE_ZIP` is set, that archive is used; a missing path fails fast.
- Otherwise the newest root `esibd-explorer-plugins-v*.zip` is inspected.
- If no root release ZIP exists, the archive test skips.
- Top-level ZIP entries must be exactly the 14 plugin folders.
- Do not ship `README.md`, `tests/`, `__pycache__/`, `logs/`, `.pyc`, or `.gitignore`.

## Stale claims to avoid

- Ignore legacy AMX folder references; current identities are `amx_a` / `amx_b`.
- `amx_hd` is standalone, not part of AMX parity.
- Count the bundle as 14 folders: 13 devices and MScan. The error catalog count remains 12.
- The canonical error catalog is `amx_a`.
