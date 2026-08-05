"""Exceptions shared across package layers."""


class MorpheBuilderError(RuntimeError):
    """Base error for public Morphe TUI failures."""


class IntegrityMetadataError(MorpheBuilderError):
    """Raised when supplied artifacts cannot be trusted."""


class ApkMismatch(IntegrityMetadataError):
    """Raised when APK metadata conflicts with expected identity."""


class GooglePlayAuthUnavailable(MorpheBuilderError):
    """Raised when Google Play authentication cannot be obtained."""


class GooglePlayVersionUnavailable(MorpheBuilderError):
    """Raised when Google Play cannot serve requested version."""


class GooglePlayArchitectureUnavailable(MorpheBuilderError):
    """Raised when Google Play delivery lacks requested architecture."""


class BuildCancelled(MorpheBuilderError):
    """Raised when user stops an active build."""


class TuiUnavailableError(MorpheBuilderError):
    """Raised when optional Textual support is unavailable."""


__all__ = [
    "ApkMismatch",
    "BuildCancelled",
    "GooglePlayArchitectureUnavailable",
    "GooglePlayAuthUnavailable",
    "GooglePlayVersionUnavailable",
    "IntegrityMetadataError",
    "MorpheBuilderError",
    "TuiUnavailableError",
]
