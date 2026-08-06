"""Recoverable provider failures only."""

from ..errors import ApkMismatch

# Compatibility alias for core APK verification callers.
ArtifactMismatch = ApkMismatch


class ProviderFallbackError(RuntimeError):
    """Provider cannot satisfy request; next provider may run."""


class ProviderUnavailable(ProviderFallbackError):
    """Provider cannot serve request now."""


class VersionUnavailable(ProviderFallbackError):
    """Requested version does not exist at provider."""


class ProviderAmbiguous(ProviderFallbackError):
    """Provider cannot select one untrusted asset safely."""


class ProviderArtifactMismatch(ProviderFallbackError):
    """Provider returned a valid artifact for different requested identity."""

