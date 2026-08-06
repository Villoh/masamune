"""Background task bodies used by the Textual application."""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import nullcontext
from threading import Event
from typing import Any

from ..cli import TEMPLATE_KEYSTORE_PASSWORD
from ..providers.google_play import (  # pyright: ignore[reportMissingImports]
    set_build_cancel_event,
    set_terminal_owner,
)


def run_build_task(
    args: Any,
    *,
    runner: Callable[..., dict[str, object]],
    reporter: Any,
    cancel_event: Event,
    uses_template_keystore: bool,
    output_sink: Callable[[str], None],
) -> dict[str, object]:
    del output_sink
    if uses_template_keystore and not os.environ.get("MORPHE_KEYSTORE_PASSWORD"):
        os.environ["MORPHE_KEYSTORE_PASSWORD"] = TEMPLATE_KEYSTORE_PASSWORD
    return runner(args, reporter=reporter, cancel_event=cancel_event)


def run_download_task(
    request: Any,
    *,
    runner: Callable[..., Any],
    reporter: Any,
    cancel_event: Event,
    output_sink: Callable[[str], None],
    **kwargs: Any,
) -> Any:
    set_terminal_owner(_subprocess_owner, output_sink=output_sink)
    set_build_cancel_event(cancel_event)
    try:
        return runner(
            request,
            reporter=reporter,
            cancel_event=cancel_event,
            **kwargs,
        )
    finally:
        set_build_cancel_event(None)
        set_terminal_owner(None)


def _subprocess_owner():
    return nullcontext()
