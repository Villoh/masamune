# Configuration

Masamune keeps build input local. `source-dir` is optional; if missing, Start
build opens local APK/folder picker and persists only user-selected local path.

Downloader settings are separate from build execution:

```toml
[[apps]]
package = "com.google.android.youtube"
name = "YouTube"
version = "21.04.223"
version-code = "210422300"
source-dir = "downloads/youtube/21.04.223/arm64-v8a"

[apps.google-play]
profile = "default"
country = "US"
proxy = "https://proxy.example"

[apps.fallbacks]
apkmirror = ["https://www.apkmirror.com/apk/example/youtube/"]
direct = ["https://downloads.example/youtube-base.apk"]
```

Supported fallback keys are `apkmirror` and `direct`. Archive/Uptodown fields
are unsupported. URLs must be HTTPS, without credentials or fragments.

Downloads view requires explicit app, version, architecture, destination, and
confirmation. It never changes `source-dir`; user selects downloaded folder
manually before Build. Google Play secrets stay in goopdl environment variables,
not TOML.
