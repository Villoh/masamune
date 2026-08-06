from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen

from ..errors import IntegrityMetadataError
from .contract import ProviderRequest, ProviderResult
from .errors import (
    ProviderAmbiguous,
    ProviderArtifactMismatch,
    ProviderUnavailable,
    VersionUnavailable,
)
from .urls import _download_assets

BASE = "https://www.apkmirror.com"
USER_AGENT = "masamune"
MAX_PAGE_BYTES = 4 * 1024 * 1024
ARCH_LABELS = {
    "arm64": ("arm64-v8a",),
    "arm64-v8a": ("arm64-v8a",),
    "armv7": ("armeabi-v7a", "arm-v7a"),
    "arm-v7a": ("armeabi-v7a", "arm-v7a"),
}
NEUTRAL_ARCH_LABELS = ("universal", "noarch", "all architectures")
NEUTRAL_DPI_LABELS = ("nodpi", "anydpi")
_VERSION_CODE_RE = re.compile(r"\b[1-9][0-9]{5,}\b")


@dataclass(frozen=True)
class ApkMirrorAsset:
    url: str
    bundle: bool
    version_code: str


@dataclass(frozen=True)
class ApkMirrorProvider:
    urls: tuple[str, ...]
    opener: Callable[..., Any] = urlopen
    name: str = "apkmirror"

    def download(self, request: ProviderRequest) -> ProviderResult:
        if not self.urls:
            raise ProviderUnavailable("apkmirror source is not configured")
        version_name = request.version_name
        if version_name is None:
            raise VersionUnavailable("apkmirror requires a version name")
        assets = [
            resolve_apkmirror_asset(
                url,
                version_name=version_name,
                arch=request.arch,
                opener=self.opener,
            )
            if not url.lower().endswith(".apk")
            else ApkMirrorAsset(url, False, request.version_code or "")
            for url in self.urls
        ]
        try:
            return _download_assets(
                request,
                self.name,
                tuple(asset.url for asset in assets),
                bundles=tuple(asset.bundle for asset in assets),
                opener=self.opener,
            )
        except ProviderArtifactMismatch as error:
            if request.arch != "armv7":
                raise
            try:
                universal_assets = [
                    resolve_apkmirror_asset(
                        url,
                        version_name=version_name,
                        arch="universal",
                        opener=self.opener,
                    )
                    if not url.lower().endswith(".apk")
                    else ApkMirrorAsset(url, False, request.version_code or "")
                    for url in self.urls
                ]
            except (IntegrityMetadataError, ProviderAmbiguous):
                raise error from None
            if tuple(asset.url for asset in universal_assets) == tuple(
                asset.url for asset in assets
            ):
                raise error
            return _download_assets(
                request,
                self.name,
                tuple(asset.url for asset in universal_assets),
                bundles=tuple(asset.bundle for asset in universal_assets),
                opener=self.opener,
            )


class _LinkCollector(HTMLParser):
    """Collect hrefs whose tag carries every required class."""

    def __init__(self, tag: str, classes: tuple[str, ...]) -> None:
        super().__init__(convert_charrefs=True)
        self._tag = tag
        self._classes = classes
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != self._tag:
            return
        values = dict(attrs)
        href = values.get("href")
        if not href:
            return
        present = (values.get("class") or "").split()
        if all(name in present for name in self._classes):
            self.hrefs.append(href)


class _CatalogLinkCollector(HTMLParser):
    """Collect APKMirror app catalog links from search results."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if not href:
            return
        resolved = urljoin(BASE, href)
        parsed = urlparse(resolved)
        path = parsed.path.strip("/").split("/")
        if (
            len(path) >= 4
            and path[0] == "apk"
            and path[-1].endswith("-release")
            and not parsed.query
            and not parsed.fragment
        ):
            resolved = urljoin(BASE, "/".join(path[:3]) + "/")
            path = path[:3]
        if (
            len(path) == 3
            and path[0] == "apk"
            and not parsed.query
            and not parsed.fragment
        ):
            self.hrefs.append(resolved)


class _IdLinkCollector(HTMLParser):
    """Collect hrefs from one element ID."""

    def __init__(self, element_id: str) -> None:
        super().__init__(convert_charrefs=True)
        self._element_id = element_id
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        href = values.get("href")
        if values.get("id") == self._element_id and href:
            self.hrefs.append(href)


class _VariantRowParser(HTMLParser):
    """Extract download links and their text from APKMirror variant rows."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._depth = 0
        self._row_depth: int | None = None
        self._href: str | None = None
        self._text: list[str] = []
        self.rows: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "div" and tag != "a":
            if self._row_depth is not None and tag == "br":
                self._text.append(" ")
            return
        values = dict(attrs)
        if tag == "a":
            href = values.get("href")
            if self._row_depth is not None and href and self._href is None:
                self._href = href
            return
        self._depth += 1
        if self._row_depth is not None:
            return
        present = (values.get("class") or "").split()
        if "table-row" in present:
            self._row_depth = self._depth
            self._href = None
            self._text = []

    def handle_endtag(self, tag: str) -> None:
        if tag != "div":
            return
        if self._row_depth is not None and self._depth == self._row_depth:
            if self._href:
                self.rows.append((self._href, " ".join("".join(self._text).split())))
            self._row_depth = None
            self._href = None
            self._text = []
        self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._row_depth is not None:
            self._text.append(data)


def _apkmirror_catalog_candidates(
    package: str,
    *,
    opener: Callable[..., Any],
):
    if not package or "." not in package:
        raise IntegrityMetadataError("invalid Android package")
    search = f"{BASE}/?s={quote(package, safe='')}"
    parser = _CatalogLinkCollector()
    parser.feed(_fetch(search, opener=opener))
    play_store_id = re.compile(
        rf"play\.google\.com/store/apps/details\?id={re.escape(package)}(?:[\"'&])"
    )
    for candidate in dict.fromkeys(parser.hrefs):
        catalog = _checked(candidate, "APKMirror catalog URL")
        if play_store_id.search(_fetch(catalog, opener=opener)):
            yield catalog


def resolve_apkmirror_catalog(
    package: str,
    *,
    opener: Callable[..., Any] = urlopen,
) -> str:
    """Find and validate APKMirror catalog URL for an Android package."""
    try:
        return next(_apkmirror_catalog_candidates(package, opener=opener))
    except StopIteration:
        raise VersionUnavailable("APKMirror package is unavailable") from None


def resolve_apkmirror_version_code(
    catalog_url: str,
    *,
    version_name: str,
    arch: str,
    opener: Callable[..., Any] = urlopen,
) -> str:
    """Resolve version name and architecture to APKMirror version code."""
    release = _release_url(catalog_url, version_name)
    _variant, _bundle, version_code = _variant_url(_fetch(release, opener=opener), arch)
    return version_code


def resolve_apkmirror_version_code_for_package(
    package: str,
    *,
    version_name: str,
    arch: str,
    opener: Callable[..., Any] = urlopen,
) -> str:
    last_error: BaseException | None = None
    for catalog in _apkmirror_catalog_candidates(package, opener=opener):
        try:
            return resolve_apkmirror_version_code(
                catalog, version_name=version_name, arch=arch, opener=opener
            )
        except (
            IntegrityMetadataError,
            ProviderUnavailable,
            VersionUnavailable,
        ) as error:
            last_error = error
    if last_error is not None:
        raise last_error
    raise VersionUnavailable("APKMirror package is unavailable")


def resolve_apkmirror_asset(
    catalog_url: str,
    *,
    version_name: str,
    arch: str,
    opener: Callable[..., Any] = urlopen,
) -> ApkMirrorAsset:
    """Resolve an APKMirror catalog page to its APK or APKM bundle asset."""
    release = _release_url(catalog_url, version_name)
    variant, bundle, version_code = _variant_url(_fetch(release, opener=opener), arch)
    download = _single_link(
        _fetch(variant, opener=opener), "a", ("btn",), "variant download button"
    )
    asset = _single_id_link(
        _fetch(download, opener=opener), "download-link", "final asset link"
    )
    if urlparse(asset).path != "/wp-content/themes/APKMirror/download.php":
        raise IntegrityMetadataError("APKMirror final asset link is invalid")
    return ApkMirrorAsset(asset, bundle, version_code)


def _release_url(catalog_url: str, version_name: str) -> str:
    base = _checked(catalog_url, "APKMirror catalog URL")
    slug = base.rstrip("/").rsplit("/", 1)[-1]
    if not slug:
        raise IntegrityMetadataError("APKMirror catalog URL has no app slug")
    dashed = version_name.replace(".", "-").replace(" ", "-")
    return f"{base.rstrip('/')}/{slug}-{dashed}-release/"


def _variant_url(page: str, arch: str) -> tuple[str, bool, str]:
    parser = _VariantRowParser()
    parser.feed(page)
    rows = [
        (href, text.lower())
        for href, text in parser.rows
        if href.endswith("-apk-download/")
    ]
    if not rows:
        raise IntegrityMetadataError("APKMirror release page has no variant rows")
    wanted = NEUTRAL_ARCH_LABELS if arch == "universal" else ARCH_LABELS.get(arch)
    if wanted is None:
        raise IntegrityMetadataError(f"unsupported APKMirror architecture: {arch}")
    plain = [(href, text) for href, text in rows if "bundle" not in text]
    bundles = [(href, text) for href, text in rows if "bundle" in text]
    for candidates, bundle, labels in (
        (plain, False, wanted),
        (bundles, True, wanted),
        (plain, False, NEUTRAL_ARCH_LABELS),
        (bundles, True, NEUTRAL_ARCH_LABELS),
    ):
        matches = [
            (href, text)
            for href, text in candidates
            if any(name in text for name in labels)
        ]
        if not matches:
            continue
        if labels == wanted:
            exact_arch = [
                (href, text)
                for href, text in matches
                if not any(
                    f"{name} +" in text or f"+ {name}" in text for name in labels
                )
            ]
            matches = exact_arch or matches
        resolved = [
            (_checked(urljoin(BASE, href), "variant URL"), text)
            for href, text in matches
        ]
        urls = {url for url, _ in resolved}
        codes = {_version_code(text) for _, text in resolved}
        if len(urls) == 1 and len(codes) == 1:
            return urls.pop(), bundle, codes.pop()
        if labels == NEUTRAL_ARCH_LABELS and len(urls) == len(codes) == len(resolved):
            # Same version name, several universal uploads (staged rollout
            # increments each get their own page). The highest version code
            # is the most recent legitimate build; verify_apk_set still
            # checks package/version/signer on whatever gets downloaded.
            try:
                url, text = max(resolved, key=lambda item: int(_version_code(item[1])))
            except ValueError:
                raise ProviderAmbiguous(
                    "APKMirror release is ambiguous for this request"
                ) from None
            return url, bundle, _version_code(text)
        raise ProviderAmbiguous("APKMirror release is ambiguous for this request")
    if len(rows) == 1:
        href, text = rows[0]
        return (
            _checked(urljoin(BASE, href), "variant URL"),
            "bundle" in text,
            _version_code(text),
        )
    raise IntegrityMetadataError(f"APKMirror release has no variant for {arch}")


def _version_code(text: str) -> str:
    codes = set(_VERSION_CODE_RE.findall(text))
    if len(codes) != 1:
        raise IntegrityMetadataError("APKMirror variant has no unique version code")
    return codes.pop()


def _single_id_link(page: str, element_id: str, label: str) -> str:
    parser = _IdLinkCollector(element_id)
    parser.feed(page)
    return _unique_checked_link(parser.hrefs, label)


def _single_link(page: str, tag: str, classes: tuple[str, ...], label: str) -> str:
    parser = _LinkCollector(tag, classes)
    parser.feed(page)
    return _unique_checked_link(parser.hrefs, label)


def _unique_checked_link(hrefs: list[str], label: str) -> str:
    resolved = {_checked(urljoin(BASE, href), label) for href in hrefs}
    if len(resolved) != 1:
        raise IntegrityMetadataError(f"APKMirror page has no unique {label}")
    return resolved.pop()


def _checked(value: str, label: str) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        raise IntegrityMetadataError(f"invalid {label}")
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or not (host == "apkmirror.com" or host.endswith(".apkmirror.com"))
    ):
        raise IntegrityMetadataError(f"invalid {label}")
    return value


def _fetch(url: str, *, opener: Callable[..., Any]) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with opener(request, timeout=60) as response:
            payload = response.read(MAX_PAGE_BYTES + 1)
    except HTTPError as error:
        if error.code in {404, 410}:
            raise VersionUnavailable("APKMirror version is unavailable") from None
        raise ProviderUnavailable("APKMirror request failed") from None
    except (OSError, URLError):
        raise ProviderUnavailable("APKMirror request failed") from None
    if len(payload) > MAX_PAGE_BYTES:
        raise IntegrityMetadataError("APKMirror page exceeds size limit")
    return payload.decode("utf-8", errors="replace")
