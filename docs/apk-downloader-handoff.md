# Masamune APK downloader — implementation handoff

## Objective

Reintroduce verified APK/split downloading inside Masamune TUI.

Morphe team gave permission for this feature. Masamune must keep local APK input working and must never download silently when user selected a local source.

Target flow:

```text
TUI build/download action
  ├─ local source selected → existing local verification path
  └─ download selected → provider chain → atomic trusted cache
                                      → same verification
                                      → existing merge/patch/sign flow
```

No merge, patch, or signing happens before downloaded artifacts pass verification.

## Current Masamune baseline

Project: `C:/Users/mikel/development/personal/projects/masamune`

Important existing seams:

- `src/masamune/orchestrator.py`
  - `_obtain_verified_source()` is currently local-only.
  - `_build_job()` already consumes `(ProviderResult, metadata, version_code)`.
  - `Reporter.event()` already supports redacted structured download events.
  - `trusted = cache / "trusted" / slug / architecture / version_name` is already passed into source acquisition.
- `src/masamune/apk.py`
  - Existing package/version/ABI/split/signer verification.
  - Existing Google delivery provenance writer can be reused.
- `src/masamune/compatibility.py`
  - Already contains `resolve_version_code_candidate()`.
  - Already contains `confirm_version_code()`.
  - Already contains `resolve_google_version()` and `resolve_google_versions()` hooks for fallback metadata/download.
- `src/masamune/config.py`
  - Already has `GooglePlayConfig`.
  - Already has `FallbackConfig` with `direct`, `archive`, `apkmirror`, `uptodown`.
  - Already validates `[apps.google-play]` and `[apps.fallbacks]` URLs.
  - `source-dir` remains optional.
- `src/masamune/config_editor.py`
  - Already supports fallback fields internally:
    - `fallback-direct`
    - `fallback-archive`
    - `fallback-apkmirror`
    - `fallback-uptodown`
  - `AppEditorScreen` does not expose these fields yet.
- `src/masamune/tui/app.py`
  - Build start already prompts for missing local sources.
  - Build worker already reports events and supports cancellation.
- `src/masamune/tui/workers.py`
  - Must gain provider terminal ownership/cancellation hooks from Morphe Builder once `goopdl` is added.
- `docs/tui.md`
  - Currently says stock APK downloads are unsupported. Update after implementation.

## Source implementation to port

Most downloader work already exists in Morphe Builder. Source project:

`C:/Users/mikel/development/personal/projects/morphe-builder`

Port these files with minimal changes:

```text
src/morphe_builder/providers/contract.py
src/morphe_builder/providers/errors.py
src/morphe_builder/providers/__init__.py
src/morphe_builder/providers/fallback.py
src/morphe_builder/providers/google_play.py
src/morphe_builder/providers/urls.py
src/morphe_builder/providers/apkmirror.py
src/morphe_builder/providers/uptodown.py
```

Adapt package imports from `morphe_builder` to `masamune`.

Also port the relevant old orchestrator code:

- imports for `confirm_version_code`, `resolve_version_code_candidate`
- imports for provider contracts and provider implementations
- `_obtain_verified_source()` remote branch
- `_apkmirror_version_code()`
- `_apkmirror_version_code_hint()`
- `_read_json()` if not already present

Do not copy the old local-source behavior blindly. Masamune must preserve explicit local/download choice.

Port provider tests from:

```text
tests/test_providers.py
```

Adapt imports and add tests for Masamune's local-vs-remote source selection.

## Dependency

Add exact reviewed dependency to `pyproject.toml`:

```toml
goopdl==1.2.0
```

Then run:

```bash
uv lock
uv lock --check
```

`goopdl` must be invoked through subprocess using `python -m goopdl`. Do not import private `goopdl` internals.

## Provider model

Use fixed provider order. Do not make order user-configurable in MVP:

```text
google-play → direct → archive → apkmirror → uptodown
```

Only configured fallback entries are attempted.

| Provider | Input | Purpose |
|---|---|---|
| `google-play` | `goopdl`, account/dispenser config | Preferred official delivery; split-aware and manifest-verified. |
| `direct` | Explicit HTTPS APK URLs | Stable self-hosted or known asset URLs. |
| `archive` | Explicit `archive.org/download/...` URLs | Historical APKs no longer served by Google Play. |
| `apkmirror` | APK URL or catalog URL | Resolves catalog → release → ABI variant → asset. Supports `.apkm`. |
| `uptodown` | APK URL or catalog URL | Resolves catalog → version → ABI variant → asset. Supports `.xapk`. |

Provider contract:

```python
@dataclass(frozen=True)
class ProviderRequest:
    package: str
    version_name: str | None
    version_code: str | None
    arch: str
    output: Path
    expected_signer: str | None = None

@dataclass(frozen=True)
class ProviderResult:
    provider: str
    directory: Path
    provenance: Path
```

## Fallback semantics

Only availability failures advance the chain:

- provider unavailable/network/rate-limit/auth failure → next provider
- requested version unavailable → next provider
- ambiguous catalog/version → next provider, if represented as recoverable provider error

Integrity failures are terminal:

- invalid ZIP/APKM/XAPK
- package mismatch
- version name/code mismatch
- wrong ABI or split set
- signer mismatch
- unsafe archive path
- unsafe redirect/host
- size/file-count violations
- invalid provenance

Never continue to another mirror after an artifact failed verification. Otherwise a malicious mirror could win by causing repeated fallback attempts.

## Orchestrator integration

### 1. Keep local path first

Refactor `_obtain_verified_source()` to:

```text
if app.source_dir exists:
    verify existing local APK set
    return ProviderResult("local", ...)

if user explicitly chose local during missing-source prompt:
    persist source-dir and use current local path

if user explicitly chose download:
    resolve version code
    execute provider chain
    verify trusted result
    return ProviderResult(provider, trusted_dir, provenance)
```

Do not automatically replace a configured `source-dir` with a download.

### 2. Download request

For each expanded architecture job:

```python
ProviderRequest(
    package=job.app.package,
    version_name=version_name,
    version_code=candidate.version_code if candidate else None,
    arch=internal_arch,
    output=trusted,
    expected_signer=job.app.expected_signer,
)
```

Use `job.app.google_play` and `job.app.fallbacks` to construct providers.

### 3. Version-code resolution

Google Play needs version code. Resolve in this order:

1. explicit `version-code`
2. confirmed mapping cache
3. APKMirror metadata hint
4. Google Play delivery inspection/download
5. verified fallback provider download

A mirror metadata candidate is only a hint. Cache it only after a real verified APK set confirms it.

ARM64 and ARMv7 mappings must remain independent. Never derive version code arithmetically or scan arbitrary code ranges.

### 4. Trusted cache

Use the existing path:

```text
<cache>/trusted/<app-slug>/<architecture>/<version-name>/
```

Provider behavior:

- refuse to overwrite existing output
- download into unique sibling temporary directory
- verify all files
- write sanitized `provenance.json`
- atomically rename temporary directory into trusted path
- reuse only when provenance identity and file hashes match request

The existing local source provenance format may remain separate from provider provenance. `ProviderResult` lets build output record `provider` consistently.

### 5. Build result

Keep `BuildResult.provider` populated with:

```text
local | google-play | direct | archive | apkmirror | uptodown
```

Build summary and history should show provider. Do not expose credentials, cookies, dispenser URLs with tokens, or authenticated URLs.

## TUI integration recommendation

### MVP: explicit choice at missing-source prompt

Reuse `LocalSourceScreen`, expanding it to three actions:

```text
Select local APK
Select local split folder
Download verified stock APKs
Cancel
```

When download is chosen:

- use current app and selected build version/architecture
- show provider chain and destination cache
- start background worker
- stream sanitized events to Build view
- do not write `source-dir`
- build immediately from trusted cache after success

This is smallest safe integration and avoids a second downloader workflow.

### Follow-up: dedicated Downloads view

After MVP works, add a `Downloads` view or command-palette action:

- select configured app
- show compatible version list
- select architecture
- show provider order
- show Google auth status without displaying secrets
- start/cancel download
- show provenance, provider, version, signer, file count, size, and cache path
- open trusted cache folder
- optionally delete one trusted source after confirmation

Suggested new key: `7`. Existing views use keys `1`–`6`.

Do not add credential text fields to the TUI. Read credentials from environment variables used by `goopdl` and show only whether required variables are present.

### App editor

Expose advanced provider fields in `AppEditorScreen` only after core flow works:

- Google Play profile
- country
- proxy
- dispenser URL
- direct fallback URLs
- archive URLs
- APKMirror catalog/asset URLs
- Uptodown catalog/asset URLs
- expected signer

Use comma-separated URLs in UI, preserving current `config_editor.py` round-trip behavior. Keep secrets out of TOML and logs.

## UX/UI specification

The downloader must feel like an extension of the current source picker, not a separate app. Reuse existing Textual modal screens, `FullWidthDataTable`, status bar, Build events, native picker patterns, and keyboard navigation.

### MVP entry point

When Start build finds an enabled app without `source-dir`, replace the current local-only modal with a source-choice modal:

```text
┌─ Stock source ───────────────────────────────────────────────┐
│ YouTube · com.google.android.youtube                         │
│ Version: 21.04.223    Architecture: arm64-v8a                │
│                                                              │
│ ○ Select APK                                                 │
│ ○ Select split folder                                        │
│ ○ Download verified stock APKs                               │
│                                                              │
│                                           Cancel             │
└──────────────────────────────────────────────────────────────┘
```

Rules:

- Keep local choices first.
- Make download an explicit action; never trigger it because `source-dir` is absent.
- Focus first actionable choice.
- `Escape` cancels current app source selection and build, matching current behavior.
- For multiple missing apps, return to this modal for next app only after current source choice succeeds.

### Download confirmation modal

Selecting download opens a second modal before network access:

```text
┌─ Download verified stock ────────────────────────────────────┐
│ App       YouTube                                             │
│ Package   com.google.android.youtube                         │
│ Version   21.04.223                                          │
│ ABI       arm64-v8a                                           │
│ Source    Google Play → APKMirror → Uptodown                 │
│ Cache     <cache>/trusted/youtube/arm64-v8a/21.04.223        │
│                                                              │
│ Google Play credentials: available / dispenser / unavailable  │
│ Fallbacks: configured / none                                 │
│                                                              │
│                         Download       Back                  │
└──────────────────────────────────────────────────────────────┘
```

Implementation notes:

- Version and ABI are read-only here; they come from build compatibility and job expansion.
- Provider order is displayed, but not editable in this modal.
- Show sanitized cache path only; redact user names if existing `redact()` policy requires it.
- Show credential state, never values, tokens, email secrets, proxy credentials, or dispenser query strings.
- If no fallback is configured, say `Google Play only` rather than implying automatic mirrors.
- `Back` returns to source choice. `Download` is the only button that starts network activity.

### Download progress

Use current Build view instead of creating a second progress implementation.

On confirmation:

1. Close modal.
2. Switch to Build view.
3. Set status to `Downloading verified stock`.
4. Add one job row per app/architecture.
5. Stream sanitized reporter events into the existing Events panel.
6. Keep `Stop build` enabled and map it to provider cancellation.

Expected event labels:

```text
[download] resolving Google Play delivery
[download] downloading verified stock
[download] Google Play unavailable; trying APKMirror
[verify] package/version/ABI/signer verified
[download] trusted stock acquired provider=apkmirror
```

Do not show raw `goopdl` output in the alternate-screen TUI. The provider worker owns subprocess output and forwards only redacted, useful events.

### Download result modal

After successful download, show a compact result before continuing the build:

```text
┌─ Stock APK verified ─────────────────────────────────────────┐
│ Provider       Google Play                                  │
│ Version        21.04.223 (210422300)                       │
│ Architecture   arm64-v8a                                   │
│ Files          8                                            │
│ Signer         <short SHA-256 fingerprint>                 │
│ Cache          trusted/youtube/arm64-v8a/21.04.223          │
│                                                              │
│ Verified source will be used for this build.                │
│                                      Continue                │
└──────────────────────────────────────────────────────────────┘
```

- Never require user to manually copy a cache path into `source-dir`.
- Do not persist `source-dir` for a downloaded source in MVP.
- Build continues using the trusted `ProviderResult` path in memory/cache.
- Full fingerprint remains available in provenance and build result; UI may shorten it for readability.

### Failure states

Use distinct user-facing messages:

| State | UI behavior |
|---|---|
| Provider unavailable | Show provider and reason; chain may continue internally. |
| Version unavailable | Show all attempted providers and requested version; no retry loop. |
| Integrity failure | Red/error state; stop chain; tell user trusted output was not published. |
| Cancelled | Return to Build view with `Download cancelled`; leave no partial trusted output. |
| Cached verified source | Show `Reusing verified stock cache` and continue without network. |

Do not expose traceback, cookies, signed URLs, HTTP authorization headers, or raw provider HTML in modal text. Keep detailed sanitized diagnostics in build log.

### Future Downloads view

Only add after MVP proves stable. Add view key `7` and command-palette entry `downloads`.

Layout:

```text
┌─ Downloads ──────────────────────────────────────────────────┐
│ App        Version       ABI          Provider       State    │
│ YouTube    21.04.223     arm64-v8a    Google Play     cached  │
│ YouTube    21.04.223     armeabi-v7a  APKMirror       ready   │
│ Reddit     2025.08.0     arm64-v8a    —               missing │
│                                                              │
│ Download selected   Open cache   Delete cache   Refresh       │
└──────────────────────────────────────────────────────────────┘
```

Context actions:

- Download selected.
- Open trusted cache folder.
- Delete selected verified source after confirmation.
- Refresh cache/provenance state.

Keep provider configuration in App Editor/advanced config, not in this table. Keep downloads view focused on artifacts and state.

### Visual consistency and accessibility

- Reuse current modal IDs/classes and theme tokens; do not introduce a second visual language.
- Keep labels literal with `markup=False` or `Text`; provider metadata is untrusted.
- Every action must be keyboard reachable and have a visible button label.
- Use `aria`/accessible labels where Textual supports them.
- Maintain compact layout at narrow terminal widths; use `VerticalScroll` for long provider/error text.
- Color is supplemental: include words such as `verified`, `cached`, `failed`, `cancelled`.
- Preserve current footer/status and `Escape`/`Stop build` semantics.

## Worker integration

Port the old provider hooks from Morphe Builder:

```python
from contextlib import nullcontext
from ..providers.google_play import set_build_cancel_event, set_terminal_owner
```

Worker must:

1. call `set_terminal_owner(...)` so `goopdl` cannot corrupt Textual alternate-screen output
2. call `set_build_cancel_event(cancel_event)`
3. clear both hooks in `finally`
4. keep current Masamune template-keystore behavior

Current Masamune worker passes `cancel_event` into the runner. Preserve that behavior while adding provider hooks.

## Security requirements

Must remain true:

- HTTPS only.
- No credential-bearing URLs.
- No URL fragments.
- Provider host allowlists enforced on catalog pages and redirects.
- APK/APKM/XAPK archive paths reject absolute paths, `..`, backslashes, duplicate names, excessive files, and excessive total size.
- Downloaded files go to temporary directories, never directly into trusted cache.
- Verify package, version name, version code, ABI, split requirements, signer, hashes, and file coverage from bytes.
- Preserve original downloaded split APKs unchanged.
- Atomic publication only after verification.
- Existing trusted output is never overwritten.
- Provenance strips query strings/fragments and never stores auth tokens.
- Logs and TUI events pass through `redact()`.
- Integrity failure stops provider chain.
- Local mode remains available and does not require network access.

## Documentation changes

Update:

```text
docs/tui.md
README.md
docs/configuration.md       # add/adapt downloader fields
docs/providers.md           # port from Morphe Builder
docs/google-play-integrity.md # port/adapt downloader verification docs
```

Replace current statement that Masamune does not download stock APKs with explicit behavior:

- local APKs remain supported
- downloads are user-triggered
- Google Play is preferred
- fallbacks are optional and fixed-order
- all remote artifacts receive identical independent verification

README must continue using raw GitHub asset URLs.

## Test plan

First port provider unit tests:

```bash
uv run python -m unittest tests/test_providers.py
```

Then full suite:

```bash
uv run python -m unittest discover -s tests
uv lock --check
uv build
uv run masamune --help
```

Add smallest Masamune-specific tests for:

- local source remains preferred when configured
- remote provider is used when user selects download
- missing source choice is explicit
- trusted cache is reused only with matching provenance
- provider result version is independently checked
- provider/integrity failures map to correct fallback behavior
- TUI worker clears subprocess hooks after success/failure/cancel
- secrets do not appear in reporter events or provenance

Live Google Play tests remain opt-in and must use external auth. Never commit APKs, auth caches, tokens, or live fixtures containing secrets.

## Suggested implementation order

1. Copy/adapt provider package and provider tests.
2. Add `goopdl==1.2.0`; regenerate `uv.lock`.
3. Port orchestrator provider imports and verified remote source branch.
4. Add APKMirror version-code hint helper.
5. Merge provider cancellation/terminal hooks into `tui/workers.py`.
6. Expand `LocalSourceScreen` with explicit download action.
7. Thread selected source mode through build start and `_obtain_verified_source()`.
8. Add config editor fields for provider settings.
9. Update cache inventory/cleanup labels for trusted downloaded stock.
10. Update docs and README.
11. Run tests, build, lock check, and a local TUI smoke test.
12. Only then consider dedicated Downloads view.

## Definition of done

- User can build from local APKs exactly as before.
- User can explicitly choose verified download from TUI.
- Google Play and configured fallback providers work through one fixed chain.
- Downloaded APK sets are independently verified before merge/patch/sign.
- Trusted cache and provenance survive TUI restart.
- Cancel, network failure, version unavailable, and integrity failure behave distinctly.
- No secrets leak to UI, logs, provenance, Git, or package artifacts.
- Unit tests, lock check, package build, and CLI help pass.
