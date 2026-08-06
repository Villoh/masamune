from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ProviderRequest:
    package: str
    version_name: str | None
    version_code: str | None
    arch: str
    output: Path
    expected_signer: str | None = None


@dataclass(frozen=True)
class ProviderResult:
    provider: str
    directory: Path
    provenance: Path


class Provider(Protocol):
    @property
    def name(self) -> str: ...

    def download(self, request: ProviderRequest) -> ProviderResult: ...
