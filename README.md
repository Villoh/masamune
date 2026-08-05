# Morphe TUI

<p align="center">
  <img src="assets/logo.png" alt="Morphe TUI" width="180">
</p>

<p align="center">
  Terminal-first local frontend for building Morphe APKs from user-supplied files.
</p>

<p align="center">
  <a href="https://github.com/Villoh/morphe-tui/actions/workflows/ci.yml"><img src="https://github.com/Villoh/morphe-tui/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://pypi.org/project/morphe-tui/"><img src="https://img.shields.io/pypi/v/morphe-tui" alt="PyPI version"></a>
  <a href="https://github.com/Villoh/morphe-tui/blob/main/LICENSE"><img src="https://img.shields.io/github/license/Villoh/morphe-tui" alt="License"></a>
</p>

Morphe TUI lets users configure, verify, merge, patch, sign, and package Morphe
builds from local APK and split-APK files.

It does **not** download stock APKs from Google Play or fallback providers.
Original APK splits remain local and are verified before they enter the build.

## Preview

<p align="center">
  <img src="assets/tui-preview.gif" alt="Morphe TUI preview" width="900">
</p>

## Features

- Native APK and folder picker for local split sets.
- Local APK verification before merge or patching.
- Morphe patch discovery, exact patch selection, and patch options.
- APK and optional Magisk/KernelSU module builds.
- Background builds with stage, job, and event progress.
- Verified signing with user-provided or generated keystores.
- Persistent build history and atomic output publication.
- Cache inventory and safe cleanup for disposable work.
- Keyboard-accessible Textual interface with themes and preferences.

## Requirements

- Python 3.11–3.13
- Java 21+
- Local APK or split-APK files
- Internet access for Morphe toolchain and patch metadata preparation

Morphe CLI, patch bundles, APKEditor, and uber-apk-signer are resolved into the
user cache when a build starts. Stock APKs are never fetched by this project.

## Install

From a checkout:

```bash
uv sync
uv run morphe-tui
```

After PyPI publication:

```bash
uv tool install morphe-tui
morphe-tui
```

`morphe-tui tui` is also available when launch options are needed.

## Configure local APKs

Create `morphe.toml`:

```toml
[toolchain]
morphe-source = "MorpheApp/morphe-desktop"
morphe-version = "latest"
patches-source = "MorpheApp/morphe-patches"
patches-version = "latest"

[[apps]]
package = "com.google.android.youtube"
name = "YouTube"
source-dir = "C:/Users/me/Downloads/youtube-splits"
version = "auto"
arch = "arm64-v8a"
build-mode = "apk"
include-patches = ["GmsCore support", "SponsorBlock"]
```

`source-dir` is optional. If omitted, **Start build** opens a native picker for
each enabled app. Select either a base/split APK or a folder. Selecting an APK
stores its containing directory.

`source-dir` supports `{arch}`, `{abi}`, and `{module}` placeholders for
architecture-specific input directories.

## Launch options

```bash
morphe-tui tui \
  --config morphe.toml \
  --cache /path/to/cache \
  --output build \
  --keystore /path/outside/repository/morphe-tui.p12 \
  --keystore-alias morphe-tui
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--config PATH` | User config path | Configuration edited by TUI. |
| `--cache PATH` | Platform cache root | Toolchain, patch, and build cache. |
| `--output PATH` | `build` | Published build directories. |
| `--keystore PATH` | Bundled template in checkout, otherwise generated key | Signing keystore. |
| `--keystore-alias ALIAS` | `morphe-tui` | Signing alias. |

Private keystore passwords are read only from `MORPHE_KEYSTORE_PASSWORD`. They
are never displayed or stored by TUI.

## Interface

| View | Key | Purpose |
| --- | --- | --- |
| Dashboard | `1` | Inspect and edit configured applications. |
| Build | `2` | Review parameters, start builds, and monitor progress. |
| Builds | `3` | Review persisted build history and outputs. |
| Patches | `4` | Discover patches and save exact selections. |
| Cache | `5` | Inspect cache areas and remove disposable work. |
| Bundles | `6` | Discover applications exposed by a patch source. |

Other shortcuts:

| Key | Action |
| --- | --- |
| `Ctrl+B` | Toggle compact sidebar. |
| `Ctrl+P` | Open command palette. |
| `?` | Show available keys. |
| `T` | Open theme selector. |
| `Q` | Quit. |

## Build flow

1. Add or select an app.
2. Choose a local APK or split directory when prompted.
3. Review version, architecture, source, keystore, and patches.
4. Confirm build.
5. TUI verifies inputs, merges splits, applies Morphe patches, signs outputs,
   and publishes verified artifacts atomically.

Build view reports current stage, per-job status, redacted events, output paths,
and failures. Each completed build contains a `build.log` and provenance data.
A second build cannot start while one is active.

## Safety model

- No Google Play or fallback APK downloads.
- Local APK sets are verified before use.
- Input splits remain unchanged.
- Existing output directories are never overwritten.
- Signing keys are protected from cache cleanup.
- Private passwords and sensitive subprocess output are redacted.
- Failed builds publish nothing.

## Documentation

- [`docs/tui.md`](docs/tui.md): interface and workflow reference.
- [`docs/`](docs/): project documentation.

## Development

```bash
uv sync
uv run python -m unittest discover -s tests
uv lock --check
uv build
```

## License

MIT. See [`LICENSE`](LICENSE).
