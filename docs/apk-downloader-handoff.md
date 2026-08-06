# Masamune APK downloader — implementation handoff

## Objective

Reintroduce verified APK/split downloading inside Masamune TUI.

Morphe team gave permission for this feature. Masamune must keep local APK input working and must never download silently when user selected a local source.

Target flow:

```text
TUI Downloads action
  → user chooses destination folder
  → provider chain → atomic verified download
  → user manually selects downloaded folder as source
  → existing build flow

TUI Build action
  → user manually provides/selects local APK or split folder
  → existing local verification
  → merge → patch → sign
```

Build never starts network activity. No merge, patch, or signing happens before artifacts pass verification.

## Moderator constraint

Morphe moderators explicitly allowed a local end-user tool that downloads and patches locally, provided it is not automated like a GitHub build workflow:

> If you want to make a tool to download and patch locally, and it's not automated like for GitHub, and it's intended only for end users, then you could do that.

They also warned that improving Morphe Desktop may eventually make this tool obsolete.

Implementation interpretation:

- Masamune is an interactive end-user TUI.
- Download starts only after a user action in `Downloads` view.
- Build never downloads, retries providers, schedules work, or runs unattended.
- No GitHub/CI/server automation should call the downloader.
- User manually supplies downloaded APK/splits to the build flow.
- Keep downloader isolated so Morphe Desktop supersession can be handled or removed later.

## Current Masamune baseline

Project: `C:/Users/mikel/development/personal/projects/masamune`

Important existing seams:

- `src/masamune/orchestrator.py`
  - `_obtain_verified_source()` is currently local-only and must stay local-only.
  - `_build_job()` already consumes `(ProviderResult, metadata, version_code)`.
  - `Reporter.event()` already supports redacted structured events.
  - A standalone `run_download()` should write verified results to a user-selected destination; do not make `_build_job()` call providers.
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
  - Must gain a separate download-worker path with provider terminal ownership/cancellation hooks once `goopdl` is added.
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

Also port the relevant old orchestrator/provider code into a standalone download operation:

- imports for `confirm_version_code`, `resolve_version_code_candidate`
- imports for provider contracts and provider implementations
- new `run_download()` operation
- `_apkmirror_version_code()`
- `_apkmirror_version_code_hint()`
- `_read_json()` if not already present

Do **not** port the old remote branch into `_obtain_verified_source()` or `_build_job()`. Masamune's build path remains local-only. The downloader prepares artifacts; user manually supplies them to build through the existing native picker.

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

### 1. Keep build local-only

Leave `_obtain_verified_source()` as the build trust boundary:

```text
if app.source_dir exists:
    verify existing local APK set
    return ProviderResult("local", ...)

otherwise:
    raise BuildError("local APK directory required")
```

The existing missing-source prompt may persist a selected folder to `source-dir`, but it must never call a provider. A build must be reproducible from user-provided local files and must not unexpectedly require network/authentication.

### 2. Add standalone `run_download()`

Create a separate orchestrator operation called only by the Downloads TUI action. It should receive an explicit request:

```python
ProviderRequest(
    package=package,
    version_name=version_name,
    version_code=version_code,
    arch=internal_arch,
    output=destination,
    expected_signer=expected_signer,
)
```

The operation:

1. resolves version code when Google Play needs it
2. constructs the fixed provider chain
3. downloads into a temporary directory
4. verifies package/version/ABI/splits/signer
5. writes provenance
6. atomically publishes into the user-selected destination
7. returns `ProviderResult` for display only

It must not mutate `source-dir`, build state, or patch state.

### 3. Version-code resolution

Google Play needs version code. Resolve in this order:

1. explicit `version-code`
2. confirmed mapping cache
3. APKMirror metadata hint
4. Google Play delivery inspection/download
5. verified fallback provider download

A mirror metadata candidate is only a hint. Cache it only after a real verified APK set confirms it.

ARM64 and ARMv7 mappings must remain independent. Never derive version code arithmetically or scan arbitrary code ranges.

### 4. Download destination

Do not hide downloaded APKs in a build-only cache. Require a destination folder from the user via native folder picker, with an optional suggested default such as `downloads/<slug>/<version>/<arch>`.

The final layout may be:

```text
<user destination>/<slug>/<version-name>/<architecture>/
  *.apk
  provenance.json
```

The user later selects this folder through `Select split folder` or selects one APK through `Select APK`. If the implementation uses the existing trusted cache internally, it must still provide `Open folder` and require manual source selection before build.

### 5. Build result

Build results remain local-source results:

```text
local
```

Downloader results display provider separately in the Downloads view/history. Do not claim a build used `google-play` unless the build actually consumed a manually selected downloaded folder.

## TUI integration recommendation

### MVP: dedicated Downloads view

Do not add download to the missing-source build modal. Keep `LocalSourceScreen` local-only:

```text
Select APK
Select split folder
Cancel
```

Add a dedicated `Downloads` view or command-palette action as downloader MVP:

- select configured app
- select compatible version
- select architecture
- select destination folder through native folder picker
- show provider order
- show Google auth status without displaying secrets
- start/cancel download
- show provenance, provider, version, signer, file count, size, and output folder
- open output folder
- never assign output folder to app automatically

After successful download, status must say: `Download verified. Select its folder when configuring the build.`

Suggested new key: `7`. Existing views use keys `1`–`6`.

The build's missing-source modal remains the manual handoff point.

### Download view behavior

- Download selected app/version/architecture.
- Open destination folder.
- Delete downloaded source after confirmation.
- Refresh output/provenance state.
- Display/copy path for manual source selection.

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

When Start build finds an enabled app without `source-dir`, keep the current local-only source modal:

```text
┌─ Local stock source ──────────────────────────────────────────┐
│ YouTube · com.google.android.youtube                         │
│                                                              │
│ Select one APK or the folder containing all splits.           │
│                                                              │
│ Select APK        Select split folder        Cancel           │
└──────────────────────────────────────────────────────────────┘
```

Rules:

- No download button in build source selection.
- No network activity from Start build.
- Focus first local action.
- `Escape` cancels current app source selection and build.
- For multiple missing apps, prompt each local source in sequence.

### Download confirmation modal

Selecting download opens a second modal before network access:

```text
┌─ Download verified stock ────────────────────────────────────┐
│ App       YouTube                                             │
│ Package   com.google.android.youtube                         │
│ Version   21.04.223                                          │
│ ABI       arm64-v8a                                           │
│ Source    Google Play → APKMirror → Uptodown                 │
│ Destination <user-selected folder>                          │
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

Use Downloads view instead of Build view. Build remains local-only.

On confirmation:

1. Close modal.
2. Stay in Downloads view.
3. Set status to `Downloading verified stock`.
4. Add one download row per app/architecture.
5. Stream sanitized reporter events into the Downloads events panel.
6. Keep `Cancel download` enabled and map it to provider cancellation.
7. Do not start or mutate a build.

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

After successful download, show a compact result and return to Downloads view:

```text
┌─ Stock APK verified ─────────────────────────────────────────┐
│ Provider       Google Play                                  │
│ Version        21.04.223 (210422300)                       │
│ Architecture   arm64-v8a                                   │
│ Files          8                                            │
│ Signer         <short SHA-256 fingerprint>                 │
│ Folder         <user-selected destination>                  │
│                                                              │
│ Download verified. Select its folder when configuring build. │
│                                      Close                  │
└──────────────────────────────────────────────────────────────┘
```

- Do not write or mutate `source-dir` after download.
- Do not start a build automatically.
- `Select APK` / `Select split folder` remains the only build handoff.
- Offer `Open folder` and display the folder path so user can provide it manually.
- Full fingerprint remains available in provenance; UI may shorten it for readability.

### Failure states

Use distinct user-facing messages:

| State | UI behavior |
|---|---|
| Provider unavailable | Show provider and reason; chain may continue internally. |
| Version unavailable | Show all attempted providers and requested version; no retry loop. |
| Integrity failure | Red/error state; stop chain; tell user trusted output was not published. |
| Cancelled | Return to Downloads view with `Download cancelled`; leave no partial published output. |
| Already verified destination | Show `Verified download already exists`; let user open it or select it manually for a build. |

Do not expose traceback, cookies, signed URLs, HTTP authorization headers, or raw provider HTML in modal text. Keep detailed sanitized diagnostics in build log.

### Downloads view layout

Add view key `7` and command-palette entry `downloads`.

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
- Open destination folder.
- Delete selected verified source after confirmation.
- Refresh download/provenance state.
- Display/copy path for manual source selection.

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

Download worker must:

1. call `set_terminal_owner(...)` so `goopdl` cannot corrupt Textual alternate-screen output
2. call `set_build_cancel_event(cancel_event)`
3. clear both hooks in `finally`
4. keep current Masamune cancellation behavior

Keep provider hooks scoped to the standalone download worker. Build worker must remain network-free.

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
- downloads are user-triggered from Downloads view only
- Start build never downloads or uses providers
- user manually selects downloaded folder before build
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

- configured/local source remains the only build input
- Downloads action is explicit and separate from Build
- Start build never invokes provider or network code
- downloaded folder is never assigned automatically
- missing source choice is explicit and local-only
- downloaded output is reused only with matching provenance
- provider result version is independently checked
- provider/integrity failures map to correct fallback behavior
- TUI worker clears subprocess hooks after success/failure/cancel
- secrets do not appear in reporter events or provenance

Live Google Play tests remain opt-in and must use external auth. Never commit APKs, auth caches, tokens, or live fixtures containing secrets.

## Suggested implementation order

1. Copy/adapt provider package and provider tests.
2. Add `goopdl==1.2.0`; regenerate `uv.lock`.
3. Add standalone `run_download()`; do not alter build source acquisition to download.
4. Add APKMirror version-code hint helper.
5. Add download worker with provider cancellation/terminal hooks.
6. Add dedicated Downloads view and native destination-folder picker.
7. Keep `LocalSourceScreen` local-only; require manual source selection after download.
8. Add config editor fields for provider settings.
9. Update cache/download inventory labels.
10. Update docs and README.
11. Run tests, build, lock check, and local TUI smoke test.

## Definition of done

- User can build from local APKs exactly as before.
- User can explicitly download verified APKs from separate Downloads view.
- Start build never invokes download/network/provider code.
- User manually selects downloaded APK/split folder for build.
- Google Play and configured fallback providers work through one fixed chain.
- Downloaded APK sets are independently verified before publication.
- Download provenance survives TUI restart.
- Cancel, network failure, version unavailable, and integrity failure behave distinctly.
- No secrets leak to UI, logs, provenance, Git, or package artifacts.
- Unit tests, lock check, package build, and CLI help pass.
