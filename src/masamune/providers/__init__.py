"""Provider contracts and verified download adapters."""

from .contract import Provider, ProviderRequest, ProviderResult
from .errors import (
    ArtifactMismatch,
    ProviderAmbiguous,
    ProviderArtifactMismatch,
    ProviderFallbackError,
    ProviderUnavailable,
    VersionUnavailable,
)
from .fallback import fallback_download, providers_for
from .urls import UrlProvider, download_urls

__all__ = [
    "ArtifactMismatch",
    "Provider",
    "ProviderAmbiguous",
    "ProviderArtifactMismatch",
    "ProviderFallbackError",
    "ProviderRequest",
    "ProviderResult",
    "ProviderUnavailable",
    "UrlProvider",
    "VersionUnavailable",
    "download_urls",
    "fallback_download",
    "providers_for",
]
