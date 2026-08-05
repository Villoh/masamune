from __future__ import annotations

import argparse
import os
import re
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path

from .config import default_config_path, ensure_config_file
from .errors import MasamuneUnavailableError
from .toolchain import default_cache_root

try:
    __version__ = package_version("masamune")
except PackageNotFoundError:
    __version__ = "0.1.0"

TEMPLATE_KEYSTORE = Path(__file__).resolve().parents[2] / ".github" / "masamune.p12"
TEMPLATE_KEYSTORE_PASSWORD = "masamune"
_SECRET_RE = re.compile(
    r"(?i)\b(authorization|cookie|token|password|dispenser)\b\s*[:=]\s*[^\r\n]*"
)
_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)


def redact(value: str) -> str:
    value = _SECRET_RE.sub(lambda match: f"{match.group(1)}=<redacted>", value)
    return _URL_RE.sub("<redacted-url>", value)


def error_code(error: BaseException) -> int | None:
    if isinstance(error, MasamuneUnavailableError):
        return 2
    return 1


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="masamune",
        description="Build Morphe artifacts from APKs supplied locally.",
    )
    root.add_argument("--version", action="version", version="%(prog)s " + __version__)
    tui = root.add_subparsers(dest="action")
    command = tui.add_parser("tui", help="open the Textual interface")
    command.add_argument("--config", type=Path, default=default_config_path())
    command.add_argument("--cache", type=Path, default=default_cache_root())
    command.add_argument("--output", type=Path, default=Path("build"))
    command.add_argument(
        "--keystore",
        type=Path,
        default=(
            TEMPLATE_KEYSTORE
            if TEMPLATE_KEYSTORE.is_file() and not TEMPLATE_KEYSTORE.is_symlink()
            else None
        ),
    )
    command.add_argument("--keystore-alias", default="masamune")
    command.set_defaults(handler=_handle_tui)
    return root


def _handle_tui(args: argparse.Namespace) -> None:
    try:
        from .tui import run_tui
    except ModuleNotFoundError as error:
        if error.name in {"textual", "rich", "tomlkit"}:
            raise MasamuneUnavailableError(
                "TUI dependencies are not installed; run `uv sync` first"
            ) from None
        raise
    ensure_config_file(args.config)
    run_tui(args)


def main() -> None:
    arguments = sys.argv[1:] or ["tui"]
    parsed = parser().parse_args(arguments)
    if not hasattr(parsed, "handler"):
        parser().print_help()
        return
    try:
        parsed.handler(parsed)
    except Exception as error:
        code = error_code(error)
        if code is None:
            raise
        print(f"error: {redact(str(error))}", file=sys.stderr)
        raise SystemExit(code) from error


if __name__ == "__main__":
    main()
