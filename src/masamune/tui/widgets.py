"""Reusable Textual widgets used by the TUI."""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar

from rich.text import Text  # pyright: ignore[reportMissingImports]
from textual import events  # pyright: ignore[reportMissingImports]
from textual.app import ComposeResult  # pyright: ignore[reportMissingImports]
from textual.binding import (  # pyright: ignore[reportMissingImports]
    Binding,
    BindingType,
)
from textual.containers import (  # pyright: ignore[reportMissingImports]
    Horizontal,
    Vertical,
)
from textual.message import Message  # pyright: ignore[reportMissingImports]
from textual.widgets import (  # pyright: ignore[reportMissingImports]
    Checkbox,
    DataTable,
    ListItem,
    ListView,
    Static,
)

from .models import PatchListEntry


class FullWidthDataTable(DataTable[object]):
    """Selectable DataTable whose columns consume available viewport width."""

    class ContextRequested(Message):
        def __init__(
            self,
            table: FullWidthDataTable,
            row_key: str,
            position: tuple[int, int] | None = None,
        ) -> None:
            super().__init__()
            self.table = table
            self.row_key = row_key
            self.position = position

    def fit_columns(self) -> None:
        if not self.columns or self.size.width <= 0:
            return
        self._update_dimensions(self.rows)
        columns = list(self.columns.values())
        natural = [
            max(column.content_width, column.label.cell_len) for column in columns
        ]
        padding = 2 * self.cell_padding * len(columns)
        extra = max(0, self.size.width - padding - sum(natural))
        total = sum(natural) or len(columns)
        distributed = 0
        for index, (column, width) in enumerate(zip(columns, natural, strict=True)):
            share = (
                extra - distributed
                if index == len(columns) - 1
                else extra * width // total
            )
            distributed += share
            column.auto_width = False
            column.width = width + share
        self.refresh(layout=True)

    def on_resize(self) -> None:
        self.call_after_refresh(self.fit_columns)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 3 or not self.is_valid_coordinate(self.hover_coordinate):
            return
        self.move_cursor(row=self.hover_coordinate.row)
        cell = self.coordinate_to_cell_key(self.hover_coordinate)
        event.stop()
        self.post_message(
            self.ContextRequested(
                self,
                str(cell.row_key.value),
                (event.screen_x, event.screen_y)
                if event.screen_x is not None and event.screen_y is not None
                else None,
            )
        )

    def action_select_cursor(self) -> None:
        super().action_select_cursor()
        if self.is_valid_coordinate(self.cursor_coordinate):
            cell = self.coordinate_to_cell_key(self.cursor_coordinate)
            self.post_message(self.ContextRequested(self, str(cell.row_key.value)))


class PatchListItem(ListItem):
    """Variable-height patch row with option titles beneath it."""

    def __init__(self, entry: PatchListEntry) -> None:
        super().__init__()
        self.entry = entry

    @property
    def patch(self) -> str:
        return self.entry.name

    @property
    def selected(self) -> bool:
        checkboxes = self.query(Checkbox)
        return checkboxes.first().value if checkboxes else self.entry.selected

    def set_selected(self, selected: bool) -> None:
        self.query_one(Checkbox).value = selected

    def compose(self) -> ComposeResult:
        with Vertical(classes="patch-item-body"):
            with Horizontal(classes="patch-item-row"):
                yield Checkbox(
                    "",
                    value=self.entry.selected,
                    compact=True,
                    classes="patch-checkbox",
                )
                summary = Text(self.entry.name)
                if self.entry.enabled:
                    summary.append(" · default")
                if self.entry.version_count:
                    summary.append(f" · {self.entry.version_count} version(s)")
                yield Static(summary, classes="patch-summary", markup=False)
            for option in self.entry.options:
                title = str(option.get("title", option.get("key", "Option")))
                default = option.get("default")
                detail = Text("↳ ", style="bold")
                detail.append(title)
                if default is not None:
                    detail.append(
                        f" · default: {str(default).lower() if isinstance(default, bool) else default}"
                    )
                yield Static(detail, classes="patch-option-summary", markup=False)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 3 or not isinstance(self.parent, PatchSelectionList):
            return
        event.stop()
        self.parent.request_context(
            self,
            (event.screen_x, event.screen_y)
            if event.screen_x is not None and event.screen_y is not None
            else None,
        )


class PatchSelectionList(ListView):
    """Patch rows with explicit checkbox selection and context actions."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "cursor_up", "Cursor up", show=False),
        Binding("down", "cursor_down", "Cursor down", show=False),
        Binding("space", "toggle_patch", "Toggle patch", show=False),
        Binding("enter", "context_menu", "Patch actions", show=False),
    ]

    class ContextRequested(Message):
        def __init__(
            self,
            selection_list: PatchSelectionList,
            patch: str,
            position: tuple[int, int] | None = None,
        ) -> None:
            super().__init__()
            self.selection_list = selection_list
            self.patch = patch
            self.position = position

    def __init__(self, *, id: str) -> None:
        super().__init__(id=id)
        self.entries: tuple[PatchListEntry, ...] = ()

    def compose(self) -> ComposeResult:
        yield from (PatchListItem(entry) for entry in self.entries)

    @property
    def selected(self) -> list[str]:
        return [item.patch for item in self.query(PatchListItem) if item.selected]

    @property
    def highlighted(self) -> int | None:
        return self.index

    @highlighted.setter
    def highlighted(self, index: int | None) -> None:
        self.index = index

    @property
    def option_count(self) -> int:
        return len(self.entries)

    def set_patches(self, entries: Iterable[PatchListEntry]) -> None:
        self.entries = tuple(entries)
        self.index = None
        self.refresh(recompose=True)

    def select(self, patch: str) -> None:
        item = self._patch_item(patch)
        if item is not None:
            item.set_selected(True)

    def toggle(self, patch: str) -> None:
        item = self._patch_item(patch)
        if item is not None:
            item.set_selected(not item.selected)

    def _patch_item(self, patch: str) -> PatchListItem | None:
        return next(
            (item for item in self.query(PatchListItem) if item.patch == patch), None
        )

    def _on_list_item__child_clicked(self, event: ListItem._ChildClicked) -> None:
        event.stop()
        self.focus()
        self.index = self._nodes.index(event.item)

    def request_context(
        self, item: PatchListItem, position: tuple[int, int] | None = None
    ) -> None:
        self.index = self._nodes.index(item)
        self.focus()
        self.post_message(self.ContextRequested(self, item.patch, position))

    def action_toggle_patch(self) -> None:
        item = self.highlighted_child
        if isinstance(item, PatchListItem):
            item.set_selected(not item.selected)

    def action_context_menu(self) -> None:
        item = self.highlighted_child
        if isinstance(item, PatchListItem):
            self.request_context(item)
