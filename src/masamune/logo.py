"""Hand-drawn Unicode Android logo for terminal-safe rendering."""

_BOX_WIDTH = 10  # "╭" + 8 dashes + "╮" — even, matching "MORPHE BUILDER"'s
# length parity so the splash centers exactly (see `_splash_content`).


def _antenna_row() -> str:
    inner = [" "] * _BOX_WIDTH
    inner[2] = "╲"
    inner[-3] = "╱"
    return "".join(inner)


def _eyes_row(left: str, right: str) -> str:
    return f"│  {left}  {right}  │"


_TOP = "╭" + "─" * (_BOX_WIDTH - 2) + "╮"
_BOTTOM = "╰" + "─" * (_BOX_WIDTH - 2) + "╯"
_ANTENNAE = _antenna_row()


def _face(left: str, right: str) -> str:
    """Every row is exactly `_BOX_WIDTH` wide and symmetric on its own, so
    each one centers identically regardless of length mismatches with any
    text placed alongside it (see `MasamuneApp._splash_content`)."""
    return "\n".join((_ANTENNAE, _TOP, _eyes_row(left, right), _BOTTOM))


LOGO_SMALL_FRAMES = (
    _face("●", "●"),  # open, idle
    _face("─", "─"),  # blink
)

LOGO_SMALL = LOGO_SMALL_FRAMES[0]

# Splash reuses the exact same small face for a quick boot-in blink sequence.
LOGO_SPLASH_FRAMES = (
    _face("·", "·"),
    _face("○", "○"),
    _face("◉", "○"),
    _face("○", "◉"),
    _face("●", "●"),
    _face("─", "─"),
    _face("●", "●"),
)

LOGO_SPLASH = LOGO_SPLASH_FRAMES[-1]
