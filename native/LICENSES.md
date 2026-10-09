# Native Worker Component Notices

This file is a component index, not a blanket license grant for the repository,
the native worker, or vendor SDKs. Cargo's `license-file` points here because a
single `MIT` label would omit the licenses of ported code and dependencies.
Original copyright notices and license texts remain applicable to their
respective components. Including source or notices does not relicense a vendor
DLL, header, API, or SDK and does not grant additional SDK redistribution rights.

## Ported Plugin Components

| Component | Reference and preserved terms | Full text |
| --- | --- | --- |
| `worker/src/mscan.rs` | `mscan/LICENSE`: Copyright (C) 2021-2026 Tim Esser; GNU GPL version 2 or later | `licenses/Explorer-GPL-2.0-or-later.txt` |
| `worker/src/tpg366.rs` | `tpg366/LICENSE`: Copyright (C) 2021-2026 Tim Esser; GNU GPL version 2 or later | `licenses/Explorer-GPL-2.0-or-later.txt` |
| `worker/src/transmission.rs` | `transmission/LICENSE`: Copyright (c) 2026 Olivier Durif; MIT | `licenses/Transmission-MIT.txt` |

The GPL text is copied verbatim from the identical existing MScan and TPG366
license files, including their copyright and version-or-later notice. The
Transmission text is copied verbatim from its existing plugin license. Those
notices are retained even when these modules are present but their features are
disabled in a different worker's source archive.

The native port and worker integration were developed in October 2026. The Rust
implementations replace hardware/process or scan-engine code; Python GUI and
Explorer adapters remain separate. This records the port, not an additional
license grant. Device-family implementations and common native code must retain
applicable project and upstream terms; their presence here must not be interpreted
as a new repository-wide MIT license. Proprietary vendor SDK components are not
included in the source archive.

## Rust Dependencies

Each family has its own `THIRD_PARTY_NOTICES.txt` and `SOURCE_MANIFEST.json` inside
`source.zip`. They record the exact Cargo-locked packages reachable through normal
and build dependency edges for that family's enabled feature, using the union of
Windows GNU x86-64 and Linux GNU x86-64 resolution. Dependency SPDX expressions,
registry/repository URLs, Cargo checksums, and full available license/notice files
are preserved, rather than collapsed into a single license label.
GPL bundles may additionally include inactive-platform packages marked
`resolution-only`, because Cargo must resolve those manifest dependencies even
when the two supported platforms do not compile them. They are not described as
linked dependencies. Archive-only family manifests and lockfiles specialize the
build; the original project manifests and lockfile remain in `provenance/`.

Examples in the current dependency graph include:

- `libloading`: ISC.
- `serialport`, enabled for TPG366: MPL-2.0. Its full upstream source and license
  accompany the vendored TPG366 bundle.
- `nalgebra`, enabled for Transmission: Apache-2.0.
- Many other crates: MIT OR Apache-2.0, with their upstream copyright notices.
- `unicode-ident`: `(MIT OR Apache-2.0) AND Unicode-3.0`; its separate Unicode
  license is retained alongside MIT and Apache texts.

The published `argmin 0.11.0` and `argmin-math 0.5.1` crates refer to their
workspace licenses but omit those full files. Verbatim copies from each crate's
exact `.cargo_vcs_info.json` revision are preserved in `licenses/upstream/`.
`licenses/upstream.json` pins their repository, revisions, source URLs, and file
SHA-256 values. The bundler verifies these records without network access; it
does not substitute another project's generic MIT or Apache notice.

The package-specific records are authoritative for a particular build. Alternate
license expressions are preserved as published; this index does not choose an
alternative on a recipient's behalf. The official MPL text is available at
<https://www.mozilla.org/en-US/MPL/2.0/> and the GNU GPL v2 text at
<https://www.gnu.org/licenses/old-licenses/gpl-2.0.html>.

## Source Bundles And Builds

MScan and TPG366 bundles contain the reachable registry crate sources and Cargo
directory-source configuration for offline family builds. Other bundles contain
the project's native source, Cargo.lock, dependency provenance, and full notices;
their exact dependencies must be fetched or already cached before an offline
build. Rust's compiler/standard library, platform linkers and system libraries
are toolchain prerequisites, not bundled dependencies.

`BUILD.md` inside each archive names the feature and explicit no-default-feature
commands. Native source is included for all families, but the distributed worker
is built with only its selected family feature. The `test-backend` feature is a
development-only fault injector and must never be enabled in a deployed worker.
An included source bundle does not assert hardware validation or permission to
distribute a proprietary SDK.
