# Google Play integrity

Google Play downloads run through `python -m goopdl` with split delivery and
`--no-extras`. Masamune first inspects delivery for requested ABI, then downloads
into an untrusted temporary directory.

Before publication Masamune verifies:

- goopdl integrity manifest coverage, paths, sizes, and Google digests;
- APK ZIP safety and Android manifest identity;
- package, version name, version code, ABI, required splits, and signer lineage;
- SHA-256 provenance for every published APK.

Only verified output moves atomically into user-selected destination. Existing
output is never overwritten. Provenance excludes credential-bearing URL parts.

Google version-code resolution uses explicit config, confirmed cache, APKMirror
metadata hint, then Google delivery. Hints become cache entries only after a
verified artifact confirms version code. ARM64 and ARMv7 mappings stay separate.

Build does not invoke goopdl, Google Play, or any fallback provider. Downloads
are user-triggered from Downloads view and must be manually selected as Build
source afterward.
