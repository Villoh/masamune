"""Discover applications exposed by Morphe patch bundles."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


class BundleError(ValueError):
    """Raised when a patch bundle cannot be loaded or parsed."""


@dataclass(frozen=True)
class BundleApp:
    package: str
    name: str
    versions: tuple[str, ...]
    patch_count: int


@dataclass(frozen=True)
class BundleCatalog:
    source: str
    version: str
    apps: tuple[BundleApp, ...]


@dataclass(frozen=True)
class CommunityBundle:
    provider: str
    repo: str
    name: str
    author: str
    description: str
    patch_count: int
    apps: tuple[BundleApp, ...]


@dataclass
class _BundleAppAccumulator:
    name: str
    versions: set[str]
    patches: set[str]


_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_MAX_JSON_BYTES = 20 * 1024 * 1024
_USER_AGENT = "masamune"
_COMMUNITY_BUNDLES_URL = "https://morphe-patches.software/data/bundles.json"


def load_bundle_catalog(
    source: str,
    *,
    version: str = "latest",
    opener: Callable[..., Any] = urlopen,
) -> BundleCatalog:
    """Load release metadata and supported apps from a GitHub patch source."""
    if not _REPOSITORY.fullmatch(source):
        raise BundleError("bundle source must be owner/repository")
    if not version or "/" in version:
        raise BundleError("bundle version is invalid")

    suffix = "latest" if version == "latest" else f"tags/{quote(version, safe='')}"
    release_url = f"https://api.github.com/repos/{source}/releases/{suffix}"
    try:
        release = _get_json(release_url, opener=opener)
    except BundleError as error:
        if version != "latest":
            raise
        for branch in ("main", "master"):
            try:
                return _load_raw_catalog(source, branch, version, opener)
            except BundleError:
                continue
        raise error
    if not isinstance(release, dict):
        raise BundleError("bundle release metadata is invalid")
    tag = release.get("tag_name")
    assets = release.get("assets")
    if not isinstance(tag, str) or not tag or not isinstance(assets, list):
        raise BundleError("bundle release metadata is incomplete")
    if version != "latest" and tag != version:
        raise BundleError("bundle release tag mismatch")

    asset_url = next(
        (
            asset.get("browser_download_url")
            for asset in assets
            if isinstance(asset, dict)
            and asset.get("name") == "patches-list.json"
            and isinstance(asset.get("browser_download_url"), str)
        ),
        None,
    )
    if asset_url is not None and _github_url(asset_url):
        payload = _get_json(asset_url, opener=opener)
    else:
        raw_url = f"https://raw.githubusercontent.com/{source}/{quote(tag, safe='')}/patches-list.json"
        payload = _get_json(raw_url, opener=opener)
    return BundleCatalog(source, tag, tuple(_apps_from_patch_list(payload)))


def _load_raw_catalog(
    source: str,
    ref: str,
    version: str,
    opener: Callable[..., Any],
) -> BundleCatalog:
    raw_url = (
        f"https://raw.githubusercontent.com/{source}/"
        f"{quote(ref, safe='')}/patches-list.json"
    )
    payload = _get_json(raw_url, opener=opener)
    return BundleCatalog(source, version, tuple(_apps_from_patch_list(payload)))


def load_community_bundles(
    *, opener: Callable[..., Any] = urlopen
) -> tuple[CommunityBundle, ...]:
    """Load public bundle summaries from Morphe's community catalog."""
    payload = _get_json(
        _COMMUNITY_BUNDLES_URL,
        opener=opener,
        referer="https://morphe-patches.software/",
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("bundles"), list):
        raise BundleError("community bundle catalog is invalid")
    raw_store = payload.get("store")
    store = cast(dict[str, object], raw_store) if isinstance(raw_store, dict) else {}
    compatibilities = payload.get("compatibilities")
    if not isinstance(compatibilities, list):
        compatibilities = []
    bundles: list[CommunityBundle] = []
    for item in payload["bundles"]:
        if not isinstance(item, dict):
            continue
        provider = item.get("source")
        repo = item.get("repo")
        if not isinstance(provider, str) or not isinstance(repo, str):
            continue
        if provider == "github" and not _REPOSITORY.fullmatch(repo):
            continue
        apps = _community_apps(item, compatibilities, store)
        bundles.append(
            CommunityBundle(
                provider=provider,
                repo=repo,
                name=str(item.get("name") or repo),
                author=str(item.get("author") or "Unknown"),
                description=str(item.get("repoDescription") or ""),
                patch_count=_int_field(item.get("patchCount")),
                apps=tuple(apps),
            )
        )
    return tuple(bundles)


def _get_json(
    url: str,
    *,
    opener: Callable[..., Any],
    referer: str | None = None,
) -> object:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": _USER_AGENT}
    if referer:
        headers["Referer"] = referer
    token = os.environ.get("GITHUB_TOKEN")
    if token and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with opener(Request(url, headers=headers), timeout=30) as response:
            raw = response.read(_MAX_JSON_BYTES + 1)
    except Exception as error:
        raise BundleError("bundle download failed") from error
    if len(raw) > _MAX_JSON_BYTES:
        raise BundleError("bundle metadata is too large")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise BundleError("bundle metadata is invalid JSON") from error


def _github_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and parsed.netloc in {
        "github.com",
        "objects.githubusercontent.com",
        "raw.githubusercontent.com",
    }


def _community_apps(
    bundle: dict[str, object],
    compatibilities: list[object],
    store: dict[str, object],
) -> list[BundleApp]:
    found: dict[str, _BundleAppAccumulator] = {}
    target_apps = bundle.get("targetApps")
    if isinstance(target_apps, list):
        for package in target_apps:
            if not isinstance(package, str):
                continue
            app_store = store.get(package)
            candidate_name = (
                app_store.get("name") if isinstance(app_store, dict) else None
            )
            name = candidate_name if isinstance(candidate_name, str) else package
            found[package] = _BundleAppAccumulator(name, set(), set())

    patches = bundle.get("patches")
    if not isinstance(patches, list):
        return _sorted_apps(found)
    for patch in patches:
        if not isinstance(patch, dict):
            continue
        compatible = patch.get("compatiblePackages")
        if compatible is None and isinstance(patch.get("compatiblePackagesKey"), int):
            key = patch["compatiblePackagesKey"]
            compatible = compatibilities[key] if key < len(compatibilities) else None
        if isinstance(compatible, dict):
            compatible = list(compatible.values())
        if not isinstance(compatible, list):
            continue
        for app in compatible:
            if not isinstance(app, dict):
                continue
            package = app.get("packageName")
            if not isinstance(package, str) or not package or "." not in package:
                continue
            entry = found.setdefault(
                package,
                _BundleAppAccumulator(str(app.get("name") or package), set(), set()),
            )
            if isinstance(app.get("name"), str) and app["name"]:
                entry.name = app["name"]
            for target in app.get("targets", ()):
                if isinstance(target, dict) and isinstance(target.get("version"), str):
                    entry.versions.add(target["version"])
            entry.patches.add(str(patch.get("name", "")))
    return _sorted_apps(found)


def _apps_from_patch_list(payload: object) -> list[BundleApp]:
    if not isinstance(payload, dict) or not isinstance(payload.get("patches"), list):
        raise BundleError("bundle patches list is invalid")
    found: dict[str, _BundleAppAccumulator] = {}
    for patch in payload["patches"]:
        if not isinstance(patch, dict):
            continue
        compatible = patch.get("compatiblePackages")
        if isinstance(compatible, dict):
            compatible = list(compatible.values())
        if not isinstance(compatible, list):
            continue
        for app in compatible:
            if not isinstance(app, dict):
                continue
            package = app.get("packageName") or app.get("name")
            if not isinstance(package, str) or not package or "." not in package:
                continue
            entry = found.setdefault(
                package,
                _BundleAppAccumulator(str(app.get("name") or package), set(), set()),
            )
            if isinstance(app.get("name"), str) and app["name"]:
                entry.name = app["name"]
            for target in app.get("targets", ()):
                if isinstance(target, dict) and isinstance(target.get("version"), str):
                    entry.versions.add(target["version"])
            entry.patches.add(str(patch.get("name", "")))
    return _sorted_apps(found)


def _int_field(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _sorted_apps(found: dict[str, _BundleAppAccumulator]) -> list[BundleApp]:
    return [
        BundleApp(
            package,
            entry.name,
            tuple(sorted(entry.versions, reverse=True)),
            len(entry.patches),
        )
        for package, entry in sorted(
            found.items(), key=lambda item: item[1].name.lower()
        )
    ]
