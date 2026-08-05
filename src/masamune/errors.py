"""Exceptions shared across package layers."""


class MasamuneError(RuntimeError):
    """Base error for public Masamune failures."""


class IntegrityMetadataError(MasamuneError):
    """Raised when supplied artifacts cannot be trusted."""


class ApkMismatch(IntegrityMetadataError):
    """Raised when APK metadata conflicts with expected identity."""


class GooglePlayAuthUnavailable(MasamuneError):
    """Raised when Google Play authentication cannot be obtained."""


class GooglePlayVersionUnavailable(MasamuneError):
    """Raised when Google Play cannot serve requested version."""


class GooglePlayArchitectureUnavailable(MasamuneError):
    """Raised when Google Play delivery lacks requested architecture."""


class BuildCancelled(MasamuneError):
    """Raised when user stops an active build."""


class MasamuneUnavailableError(MasamuneError):
    """Raised when optional Textual support is unavailable."""


__all__ = [
    "ApkMismatch",
    "BuildCancelled",
    "GooglePlayArchitectureUnavailable",
    "GooglePlayAuthUnavailable",
    "GooglePlayVersionUnavailable",
    "IntegrityMetadataError",
    "MasamuneError",
    "MasamuneUnavailableError",
]
