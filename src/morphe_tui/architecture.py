from __future__ import annotations

from enum import StrEnum


class Architecture(StrEnum):
    """Canonical configured Android architecture with explicit tool mappings."""

    ARM64 = "arm64-v8a"
    ARMV7 = "arm-v7a"

    @classmethod
    def from_config(cls, value: str | Architecture) -> Architecture:
        try:
            return cls(value)
        except ValueError:
            raise ValueError(f"unsupported architecture: {value}") from None

    @classmethod
    def from_goopdl(cls, value: str) -> Architecture:
        for architecture in cls:
            if architecture.goopdl == value:
                return architecture
        raise ValueError(f"unsupported Google Play architecture: {value}")

    @property
    def goopdl(self) -> str:
        return "arm64" if self is self.ARM64 else "armv7"

    @property
    def android_abi(self) -> str:
        return "arm64-v8a" if self is self.ARM64 else "armeabi-v7a"

    @property
    def module(self) -> str:
        return "arm64" if self is self.ARM64 else "arm"
