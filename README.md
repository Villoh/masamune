<!-- markdownlint-disable MD013 -->

<div align="center">

<img src="https://raw.githubusercontent.com/Villoh/masamune/main/assets/logo.png" alt="Masamune logo" width="220">

# Masamune

**Local, verified Morphe APK builds configured and monitored from a terminal UI.**

[GitHub](https://github.com/Villoh/masamune) | [Releases](https://github.com/Villoh/masamune/releases) | [Issues](https://github.com/Villoh/masamune/issues)

[![PyPI](https://img.shields.io/pypi/v/masamune-tui?style=for-the-badge&label=PyPI&labelColor=000000&color=FFFFFF&logo=pypi&logoColor=white)](https://pypi.org/project/masamune-tui/)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-FFFFFF?style=for-the-badge&labelColor=000000&logo=python&logoColor=white)](https://www.python.org/)
[![Java](https://img.shields.io/badge/java-21%2B-FFFFFF?style=for-the-badge&labelColor=000000&logo=openjdk&logoColor=white)](https://openjdk.org/)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json&style=for-the-badge&label=managed%20with&labelColor=000000&color=261230)](https://docs.astral.sh/uv/)

</div>

---

Masamune is a Textual terminal interface for building Morphe APKs from
local APK/split inputs or verified stock downloads.

It verifies inputs, merges splits, resolves Morphe tooling, applies patches,
signs outputs, and optionally packages modules. Downloads are explicit; Start
build never performs network activity.

## Preview

<p align="center">
  <img src="https://raw.githubusercontent.com/Villoh/masamune/main/assets/tui-preview.gif" alt="Masamune preview" width="900">
</p>

## Requirements

| Requirement | Version | Notes |
| --- | --- | --- |
| [`Python`](https://www.python.org/) | 3.11–3.13 | Runtime and development support. |
| [`Java`](https://openjdk.org/) | 21+ | Required by Morphe CLI, APKEditor, and uber-apk-signer. |
| [`uv`](https://docs.astral.sh/uv/) | latest | Recommended for installation and development. |

Windows and Linux are supported. Morphe CLI, patch bundles, APKEditor, and
uber-apk-signer are prepared in the user cache when a build starts.

## Features

- **Local APK input**: native APK and folder pickers; build input stays local-only.
- **Explicit verified downloads**: Google Play → APKMirror → Direct, available from
  Downloads and tracked in the Downloaded library view.
- **Input verification**: package identity, version, version code, architecture,
  split coverage, hashes, and signing certificates are checked before use.
- **Config-driven builds**: apps, architectures, versions, patch sources, and
  build modes live in `morphe.toml`.
- **Morphe patch workflow**: discover patches, select exact patch sets, and edit
  configurable patch options.
- **APK and module output**: build patched APKs plus optional deterministic
  Magisk/KernelSU modules.
- **Background execution**: current stage, per-job status, redacted events, and
  build logs remain visible while tools run.
- **Signing controls**: auto-generated per-user keystore, or an explicit
  private keystore for real signing.
- **Build history**: completed, failed, and cancelled builds remain available
  with output paths and summaries.
- **Safe cache cleanup**: inspect cache areas and remove only disposable data.
- **Atomic publication**: existing outputs are never overwritten; failed builds
  publish nothing.
- **Terminal-native UI**: keyboard navigation, command palette, themes, compact
  sidebar, and reduced-motion support.

## Install

### From PyPI

```bash
uv tool install masamune-tui
masamune
```

### From source

```bash
git clone https://github.com/Villoh/masamune.git
cd masamune
uv sync
uv run masamune
```

Running `masamune` without arguments opens the TUI. The explicit
`masamune tui` form accepts launch options.

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

`source-dir` is optional. Build source precedence is:

1. Configured `source-dir`.
2. Matching verified APK/split set from Downloads.
3. Native picker if no verified download exists.

Select an APK to use its containing directory, or select a folder directly.
Use `{arch}`, `{abi}`, or `{module}` placeholders for architecture-specific
local directories.

The TUI writes validated configuration changes atomically. Unsupported TOML
fields remain safe to edit manually.

## Launch options

```bash
masamune tui \
  --config morphe.toml \
  --cache /path/to/cache \
  --output build
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--config PATH` | User config path | Configuration displayed and edited by TUI. |
| `--cache PATH` | Platform cache root | Toolchain, patch, and build cache. |
| `--output PATH` | `build` | Atomic build publication directory. |
| `--keystore PATH` | Generated per-user key under cache | Optional external signing keystore. |
| `--keystore-alias ALIAS` | `masamune` | Signing alias. |

Private keystore passwords are read only from `MORPHE_KEYSTORE_PASSWORD`. They
are never displayed or stored by TUI.

## Interface

| View | Key | Purpose |
| --- | --- | --- |
| Dashboard | `1` | Inspect and edit configured applications. |
| Bundles | `2` | Discover supported applications from patch sources. |
| Downloads | `3` | Resolve versions and download verified stock APKs. |
| Downloaded | `4` | Browse, verify, open, and delete downloaded APK sets. |
| Build | `5` | Review parameters, start builds, and monitor results. |
| Builds | `6` | Review persisted build history and outputs. |
| Patches | `7` | Discover patches and save exact selections. |
| Cache | `8` | Inspect cache paths and remove disposable work. |

| Key | Action |
| --- | --- |
| `Ctrl+B` | Toggle compact sidebar. |
| `Ctrl+P` | Open command palette. |
| `?` | Show available keys. |
| `T` | Open theme selector. |
| `Q` | Quit. |

## Download flow

Downloads require an explicit action in Downloads (`3`). User can choose
Automatic (Google Play → APKMirror → Direct), Google Play, APKMirror, or Direct.
Destination is fixed at `%LOCALAPPDATA%\\masamune\\downloads` on Windows
(and the platform data equivalent). Downloads view resolves versions from Morphe
patch compatibility and lets the user choose any supported version manually.
The Downloaded view lists verified APK/split sets and supports opening, verifying,
and deleting them. Credentials come from goopdl
environment variables and never appear
in TUI, logs, or provenance. Press `Resolve versions` to query Morphe patch
compatibility, then choose a supported version manually. Every result is independently
verified before atomic publication. Builds can use matching downloaded sources
automatically when `source-dir` is not configured; configuration is never changed
implicitly.

## Build flow

1. Add or select an application.
2. Configure `source-dir`, or download a verified stock APK/split set.
3. Review source, version, architecture, patches, output, and signing key.
4. Confirm the build.
5. TUI verifies inputs, merges splits, applies Morphe patches, signs outputs,
   and publishes verified artifacts atomically.

Build view reports:

- pending, running, success, failed, and cancelled states;
- current stage and per-job state;
- redacted, scrollable subprocess events;
- output paths and artifact names;
- `build.log` and provenance files in each published build directory.

Only one build runs at a time. Stop build requests cancellation after the
current build operation and cleans temporary staging; it does not interrupt
arbitrary external tools.

## Patch bundles and individual patches

**Bundles** provides discovery data for supported applications from public patch
sources. Add an application to the dashboard or assign a patch source to an
existing app without replacing unrelated configuration.

**Patches** resolves the configured source, lists compatible patches, and saves
an exact selection. Configurable patches expose boolean, numeric, and free-text
options with upstream defaults and suggestions.

Patch resolution may download Morphe metadata and toolchains into the external
cache. It never downloads stock APKs.

## Safety model

- Start build performs no network activity or provider fallback.
- Downloads run only after user confirmation in Downloads.
- Google Play, APKMirror, and explicit Direct URLs are independently verified.
- Original local split APKs remain unchanged.
- APK metadata, hashes, architecture, and signer identity are verified.
- Existing output directories are never overwritten.
- Signing keys are protected from cache cleanup.
- Sensitive passwords and subprocess output are redacted.
- Failed builds publish no partial output.

Generated keys are for local builds only. Use an explicit private keystore for
distributable builds.

## Current limits

The TUI intentionally does not provide:

- credential storage or display;
- raw/free-form TOML editing;
- GitLab patch-source assignment;
- parallel builds;
- web mode;
- runtime PNG rendering;
- hard cancellation of every external subprocess.

Use the CLI or a text editor for automation and unsupported configuration fields.

## Development

```bash
uv sync
uv run python -m unittest discover -s tests
uv lock --check
uv build
```

Detailed interface notes live in [`docs/tui.md`](docs/tui.md).

## License

MIT. See [`LICENSE`](LICENSE).
