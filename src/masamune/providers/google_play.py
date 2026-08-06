from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from subprocess import TimeoutExpired
from threading import Event, Thread

from ..apk import (
    MANIFEST_NAME,
    verify_apk_set,
    verify_google_delivery,
    write_provenance,
)
from ..errors import (
    ApkMismatch,
    BuildCancelled,
    GooglePlayArchitectureUnavailable,
    GooglePlayAuthUnavailable,
    GooglePlayVersionUnavailable,
    IntegrityMetadataError,
)
from .contract import ProviderRequest, ProviderResult
from .errors import ProviderArtifactMismatch, ProviderUnavailable, VersionUnavailable

AUTH_UNAVAILABLE_EXIT_CODE = 3
VERSION_UNAVAILABLE_EXIT_CODE = 4

_terminal_owner: Callable[[], AbstractContextManager[None]] | None = None
_terminal_output: Callable[[str], None] | None = None
_cancel_event: Event | None = None


def set_terminal_owner(
    owner: Callable[[], AbstractContextManager[None]] | None,
    *,
    output_sink: Callable[[str], None] | None = None,
) -> None:
    """Mark goopdl execution as owned by a caller such as the TUI.

    The owner context still brackets the subprocess, but owned executions
    discard goopdl's inherited output so it cannot corrupt an alternate-screen
    UI. The plain CLI never sets this and keeps its normal stdio.
    """
    global _terminal_owner, _terminal_output
    _terminal_owner = owner
    _terminal_output = output_sink if owner is not None else None


def set_build_cancel_event(event: Event | None) -> None:
    global _cancel_event
    _cancel_event = event


def run_goopdl(command: list[str]) -> None:
    """Run pinned goopdl without treating untrusted failures as recoverable."""
    owner_factory = _terminal_owner
    cancel_event = _cancel_event
    try:
        if cancel_event is None:
            if owner_factory is None:
                subprocess.run(command, check=True, timeout=1800)
            else:
                with owner_factory():
                    subprocess.run(
                        command,
                        check=True,
                        timeout=1800,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
            return
        if cancel_event.is_set():
            raise BuildCancelled("Build cancelled by user")
        with owner_factory() if owner_factory is not None else nullcontext():
            output_sink = _terminal_output
            capture_output = output_sink is not None
            output_queue: Queue[str] = Queue()
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE
                if capture_output
                else (subprocess.DEVNULL if owner_factory is not None else None),
                stderr=subprocess.STDOUT
                if capture_output
                else (subprocess.DEVNULL if owner_factory is not None else None),
                text=capture_output,
                encoding="utf-8" if capture_output else None,
                errors="replace" if capture_output else None,
            )
            reader: Thread | None = None
            if capture_output:
                stream = process.stdout
                assert stream is not None

                def drain_output() -> None:
                    for line in stream:
                        output_queue.put(line)

                reader = Thread(target=drain_output, daemon=True)
                reader.start()
            while True:
                if cancel_event.is_set():
                    process.kill()
                    process.wait()
                    if reader is not None:
                        reader.join(timeout=1)
                    raise BuildCancelled("Build cancelled by user")
                if output_sink is not None:
                    while True:
                        try:
                            output_sink(output_queue.get_nowait())
                        except Empty:
                            break
                try:
                    returncode = process.wait(timeout=0.2)
                except TimeoutExpired:
                    continue
                if reader is not None:
                    reader.join(timeout=1)
                    while not output_queue.empty():
                        if output_sink is not None:
                            output_sink(output_queue.get_nowait())
                if returncode:
                    raise subprocess.CalledProcessError(returncode, command)
                return
    except subprocess.CalledProcessError as error:
        if error.returncode == AUTH_UNAVAILABLE_EXIT_CODE:
            raise GooglePlayAuthUnavailable(
                "Google Play authentication is unavailable"
            ) from None
        if error.returncode == VERSION_UNAVAILABLE_EXIT_CODE:
            raise GooglePlayVersionUnavailable(
                "Google Play version is unavailable"
            ) from None
        raise IntegrityMetadataError(
            f"goopdl command failed (exit code {error.returncode})"
        ) from None
    except (OSError, TimeoutExpired):
        raise ProviderUnavailable("Google Play request failed") from None


def goopdl_command(
    action: str,
    *,
    arch: str,
    package: str | None = None,
    output: Path | None = None,
    manifest: Path | None = None,
    version: str | None = None,
    profile: str | None = None,
    dispenser: str | None = None,
    region: str | None = None,
) -> list[str]:
    command = [sys.executable, "-m", "goopdl", action]
    if package:
        command.append(package)
    command.extend(("--arch", arch))
    if output:
        command.extend(("--output", str(output)))
    if manifest:
        command.extend(("--integrity-manifest", str(manifest)))
    if version:
        command.extend(("--version", version))
    if profile:
        command.extend(("--profile", profile))
    if dispenser:
        command.extend(("--dispenser", dispenser))
    if region:
        command.extend(("--country", region))
    if action == "download":
        command.extend(("--splits", "--no-extras"))
    elif action == "inspect-delivery":
        command.append("--json")
    return command


def _delivery_supports_architecture(path: Path, arch: str) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        splits = data["splits"]
    except (OSError, TypeError, ValueError, KeyError):
        raise IntegrityMetadataError(
            "invalid Google Play delivery inspection"
        ) from None
    if not isinstance(splits, list) or not all(
        isinstance(item, str) for item in splits
    ):
        raise IntegrityMetadataError("invalid Google Play delivery split list")
    abi_tokens = {
        token
        for item in splits
        for token in ("arm64_v8a", "arm64-v8a", "armeabi_v7a", "armeabi-v7a")
        if token in item
    }
    if not abi_tokens:
        return True
    wanted = "arm64" if arch == "arm64" else "armeabi_v7a"
    return any(wanted in token for token in abi_tokens)


def _inspect_google_play_delivery(
    request: ProviderRequest,
    destination: Path,
    *,
    profile: str | None,
    region: str | None,
    dispenser: str | None,
) -> None:
    run_goopdl(
        goopdl_command(
            "inspect-delivery",
            arch=request.arch,
            package=request.package,
            output=destination,
            version=request.version_code or request.version_name,
            profile=profile,
            dispenser=dispenser,
            region=region,
        )
    )
    if not _delivery_supports_architecture(destination, request.arch):
        raise GooglePlayArchitectureUnavailable(
            f"Google Play delivery has no compatible ABI for {request.arch}"
        )


def download_google_play(
    request: ProviderRequest,
    *,
    profile: str | None,
    region: str | None,
    dispenser: str | None,
) -> ProviderResult:
    if request.version_code is None:
        raise GooglePlayVersionUnavailable(
            "Google Play requires a resolved version code"
        )
    if request.output.exists() or request.output.is_symlink():
        raise FileExistsError(
            f"refusing to overwrite output directory: {request.output}"
        )
    request.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{request.output.name}-untrusted-", dir=request.output.parent
    ) as temporary:
        staging = Path(temporary) / "verified"
        staging.mkdir()
        manifest = staging / MANIFEST_NAME
        _inspect_google_play_delivery(
            request,
            Path(temporary) / "delivery-inspection.json",
            profile=profile,
            region=region,
            dispenser=dispenser,
        )
        run_goopdl(
            goopdl_command(
                "download",
                arch=request.arch,
                package=request.package,
                output=staging,
                manifest=manifest,
                version=request.version_code or request.version_name,
                profile=profile,
                dispenser=dispenser,
                region=region,
            )
        )
        verify_google_delivery(staging, manifest)
        metadata = verify_apk_set(
            staging,
            request.package,
            version_name=request.version_name,
            version_code=request.version_code,
            arch=request.arch,
            expected_signer=request.expected_signer,
        )
        write_provenance(
            staging,
            manifest,
            metadata,
            package=request.package,
            arch=request.arch,
            profile=profile,
            region=region,
        )
        os.replace(staging, request.output)
    return ProviderResult(
        "google-play", request.output, request.output / "provenance.json"
    )


@dataclass(frozen=True)
class GooglePlayProvider:
    profile: str | None
    region: str | None
    dispenser: str | None
    proxy: str | None
    name: str = "google-play"
    runner: Callable[[ProviderRequest], object] | None = None

    def download(self, request: ProviderRequest) -> ProviderResult:
        if request.version_code is None:
            raise VersionUnavailable("Google Play requires a resolved version code")
        if request.output.exists():
            verify_apk_set(
                request.output,
                request.package,
                version_name=request.version_name,
                version_code=request.version_code,
                arch=request.arch,
                expected_signer=request.expected_signer,
            )
            return ProviderResult(
                self.name, request.output, request.output / "provenance.json"
            )
        previous = {key: os.environ.get(key) for key in ("HTTP_PROXY", "HTTPS_PROXY")}
        try:
            if self.proxy:
                os.environ["HTTP_PROXY"] = self.proxy
                os.environ["HTTPS_PROXY"] = self.proxy
            if self.runner is not None:
                self.runner(request)
            else:
                download_google_play(
                    request,
                    profile=self.profile,
                    region=self.region,
                    dispenser=self.dispenser,
                )
        except ApkMismatch as error:
            raise ProviderArtifactMismatch(str(error)) from None
        except GooglePlayAuthUnavailable as error:
            raise ProviderUnavailable(str(error)) from None
        except GooglePlayArchitectureUnavailable as error:
            raise VersionUnavailable(str(error)) from None
        except GooglePlayVersionUnavailable as error:
            raise VersionUnavailable(str(error)) from None
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        return ProviderResult(
            self.name, request.output, request.output / "provenance.json"
        )
