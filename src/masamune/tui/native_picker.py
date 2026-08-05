"""Native local APK and folder selection dialogs."""

from __future__ import annotations

from pathlib import Path
from typing import Literal


def choose_source(kind: Literal["apk", "folder"]) -> Path | None:
    """Return APK parent directory or selected split directory."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        if kind == "apk":
            selected = filedialog.askopenfilename(
                parent=root,
                title="Select base or split APK",
                filetypes=[("Android packages", "*.apk"), ("All files", "*.*")],
            )
            return Path(selected).parent if selected else None
        selected = filedialog.askdirectory(
            parent=root,
            title="Select directory containing APK splits",
        )
        return Path(selected) if selected else None
    finally:
        root.destroy()
