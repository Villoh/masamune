# Masamune

Masamune is a Textual interface for configuring and running Morphe builds from
APK files supplied locally by the user.

## Preview

<p align="center">
  <img src="https://raw.githubusercontent.com/Villoh/masamune/main/assets/tui-preview.gif" alt="Masamune preview" width="900">
</p>

## Install and launch

```bash
uv sync
uv run masamune
```

The explicit launcher accepts configuration options:

```bash
uv run masamune tui \
  --config morphe.toml \
  --cache .cache/masamune \
  --output build \
  --keystore .github/masamune.p12 \
  --keystore-alias masamune
```

## Local APK sources

`source-dir` may be set in `morphe.toml`. If absent, Start build requests a
source for each enabled app through the native file picker. Selecting an APK
stores its containing directory; selecting a folder stores that folder directly.

Supported placeholders: `{arch}`, `{abi}`, and `{module}`.

Masamune verifies package metadata, version, architecture, hashes, and
signing certificates before merging local splits. It does not download stock
APKs from Google Play or fallback providers.

## Navigation

| View | Key | Purpose |
| --- | --- | --- |
| Dashboard | `1` | Inspect and edit applications. |
| Build | `2` | Review and run builds. |
| Builds | `3` | Review build history and outputs. |
| Patches | `4` | Discover and select exact patches. |
| Cache | `5` | Inspect and clean disposable cache data. |
| Bundles | `6` | Discover supported applications. |

`Ctrl+B` toggles the compact sidebar. `Ctrl+P` opens the command palette. `T`
opens the theme selector. `?` shows available keys. `Q` quits.

## Build flow

1. Configure an app or add one from Bundles.
2. Select a local APK or split directory.
3. Review version, architecture, patches, output, and signing key.
4. Confirm the build.
5. TUI verifies, merges, patches, signs, and publishes artifacts atomically.

Build view shows stage, per-job state, redacted events, output paths, and
failures. Outputs include build logs and provenance metadata.

## Configuration editing

TUI edits supported TOML fields through validated round-trip writes. Changes are
written to a temporary file, parsed again, and atomically replaced only after
validation succeeds. Unsupported TOML fields remain safe to edit manually.

## Keystores

A checkout uses the bundled public template key for local test builds. Outside a
checkout, omitting `--keystore` creates a private per-user key in the cache.
Private keystore passwords come only from `MORPHE_KEYSTORE_PASSWORD` and are not
shown or persisted by TUI.

## Current limits

TUI does not provide credential storage, raw TOML editing, hard cancellation of
running subprocesses, parallel builds, web mode, or runtime image rendering.
