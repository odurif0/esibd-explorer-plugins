# ESIBD Explorer Plugins

One plugin per device, plus standalone scan plugins.

## Current bundle

15 standalone plugin folders: `ampr_a`, `ampr_b`, `amx_a`, `amx_b`, `amx_hd`, `dmmr`, `esi`, `mscan`, `psu_a`, `psu_b`, `psu_c`, `psu_d`, `psu_e`, `tpg366`, `transmission`.

All 15 folders own `native/` binaries, a hash manifest, source archive and third-party notices, plus a private Python native-worker supervisor. The 12 DLL-backed device folders also own their entrypoint, icons, Python facade/reference runtime, device-specific vendor headers/DLLs, and `vendor/runtime/error_codes.json`. `mscan` has its own `_runtime/_native_scan.py` action adapter, icon and GPL LICENSE; no DLL or catalog. `tpg366` has `_runtime/_native_link.py`, a reference `_tpg366.py` ASCII protocol, icons and GPL LICENSE; no vendor DLL or catalog. `transmission` (MIT) has `_runtime/_native_engine.py`, reference/configuration `_engine.py`, `_simulator.py`, `_beamline.py` and `_log.py`; it drives other plugins' channels only through Explorer and imports none of them.

## Architecture

- Entry points expose `providePlugins()`.
- Bundled runtimes load privately through `importlib.util.spec_from_file_location` into `_esibd_bundled_*` modules.
- Loader code registers those modules in `sys.modules` and does not mutate `sys.path`.
- Missing bundled runtime raises `ModuleNotFoundError`.
- Production communications/scan engines use one Rust process per active plugin. Never fall back to inline DLLs, Python workers, a shared daemon or an external interpreter. Python remains for Explorer GUI/channel adapters and explicit reference/notebook tests.
- A dead or hung native worker must be stoppable and reconnectable independently. Reaping a process never confirms hardware OFF or discharge; retain the warning and invalid readings.
- `native/` is build-time shared Rust source only. Deploy one family feature per executable. No tests run on GitHub.
- Generate/check ABI and error-catalog copies with `python3 tools/generate_native_abi.py`; sync the private Python supervisor with `python3 tools/sync_native_sources.py`. Family sync also copies plugin-local `native/` assets.

## Parity canonicals

- `ampr`: canonical `ampr_a`, sibling `ampr_b`
- `amx`: canonical `amx_a`, sibling `amx_b`
- `psu`: canonical `psu_a`, siblings `psu_b`, `psu_c`, `psu_d`, `psu_e`
- `amx_hd` and `dmmr` are standalone
- Edit only the canonical plugin, then run `python3 tools/sync_family_siblings.py`
  (`--check` reports drift). Siblings differ only by the Device `name` literal.

## Notebooks and runtimes

All notebooks live in `notebooks/` (enforced by `tests/test_notebook_layout.py`),
never inside plugin folders or `tests/`, and are not shipped in the release ZIP.
Plugins and notebooks evolve freely: notebooks do not pin runtime or DLL hashes.
They only record the SHA-256 of the files used in each run's metadata.

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

Tests run locally only: no GitHub Actions or other CI (the owner does not want tests run on GitHub).
Run the smallest check that covers the change, then widen only if needed.

```bash
python3 -m pytest -q
python3 -m pytest -q -m "not slow"
python3 -m pytest -q --all-siblings   # release validation
python3 -m pytest -q tests/test_plugin_family_parity.py
python3 -m pytest -q 'tests/test_plugin_family_parity.py::test_sibling_family_matches_canonical[amx]'
ESIBD_RELEASE_ZIP=/path/to/file.zip python3 -m pytest -q tests/test_release_archive_integrity.py
cargo test --locked --offline --manifest-path native/Cargo.toml --features test-backend
python3 tools/generate_native_abi.py --check
python3 tools/sync_native_sources.py --check
python3 tools/build_native_workers.py --check
```

Real-Explorer tests validate the installed host only; the plugins target Explorer
1.0.2 (`tests/explorer_host.py`). `ESIBD_REQUIRE_TARGET_HOST=1` fails on another host.

## Release archive behavior

- Build with `python3 tools/build_release.py X.Y.Z` (tracked files only, reproducible SHA-256).
- Releases are validated from a single archive path.
- If `ESIBD_RELEASE_ZIP` is set, that archive is used; a missing path fails fast.
- Otherwise the newest root `esibd-explorer-plugins-v*.zip` is inspected.
- If no root release ZIP exists, the archive test skips.
- Top-level ZIP entries must be exactly the 15 plugin folders.
- Do not ship `README.md`, `tests/`, `__pycache__/`, `logs/`, `.pyc`, or `.gitignore`.
- Native binaries, manifests, corresponding sources and license notices must be tracked and validated before building. Do not stage the user's work just to test packaging; use an isolated temporary checkout.
- MScan/TPG366 source archives include vendored corresponding build dependencies. Preserve per-component licenses, including MPL-2.0 serialport, rather than relabeling the worker tree as a blanket MIT work.

## Stale claims to avoid

- Ignore legacy AMX folder references; current identities are `amx_a` / `amx_b`.
- `amx_hd` is standalone, not part of AMX parity.
- Count the bundle as 15 folders: 13 devices, MScan and Transmission. The error catalog count remains 12.
- The canonical error catalog is `amx_a`.
