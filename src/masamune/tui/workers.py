"""Background task bodies used by the Textual application."""

from __future__ import annotations

import os
from collections.abc import Callable
from threading import Event
from typing import Any

from ..cli import TEMPLATE_KEYSTORE_PASSWORD


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
