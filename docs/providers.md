# APK providers

Masamune downloader runs only from Downloads view. Build never calls providers.

User can select one provider or Automatic. Automatic uses:

```text
Google Play → APKMirror → Direct
```

Google Play uses `goopdl==1.2.1` through `python -m goopdl`. Direct accepts
explicit HTTPS APK URLs only; Masamune does not discover hosts. APKMirror accepts
optional catalog or asset URLs; when none are configured, it discovers the
catalog from the Android package, then resolves release, architecture, and APKM
assets.

Recoverable failures advance provider chain: unavailable provider, auth/rate
limit, missing version, or ambiguous catalog. Integrity failures stop download:
malformed archive, identity/version/ABI/split/signer mismatch, unsafe redirect or
path, size limit, or invalid provenance.

Every provider writes to a temporary directory. Masamune verifies bytes and
publishes atomically. Provenance strips URL queries/fragments and records file
hashes, package, version, architecture, provider, and signer metadata.

Example:

```toml
[apps.fallbacks]
apkmirror = ["https://www.apkmirror.com/apk/example/app/"]
direct = ["https://downloads.example/app-base.apk"]
```

Keep Google credentials in environment variables consumed by goopdl. Never put
credentials, dispenser query strings, tokens, or cookies in TOML.
