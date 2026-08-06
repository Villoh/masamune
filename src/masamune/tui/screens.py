"""Small modal screens used by the Textual TUI."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import ClassVar, Literal

from textual.app import ComposeResult  # pyright: ignore[reportMissingImports]
from textual.binding import Binding  # pyright: ignore[reportMissingImports]
from textual.containers import (  # pyright: ignore[reportMissingImports]
    Horizontal,
    Vertical,
    VerticalScroll,
)
from textual.events import MouseDown  # pyright: ignore[reportMissingImports]
from textual.screen import ModalScreen  # pyright: ignore[reportMissingImports]
from textual.suggester import SuggestFromList  # pyright: ignore[reportMissingImports]
from textual.widgets import (  # pyright: ignore[reportMissingImports]
    Button,
    Checkbox,
    DataTable,
    Input,
    Label,
    OptionList,
    Select,
    Static,
)
from textual.widgets.button import (  # pyright: ignore[reportMissingImports]
    ButtonVariant,
)
from textual.widgets.option_list import Option  # pyright: ignore[reportMissingImports]

from .models import PatchOptionValue
from .native_picker import choose_source  # pyright: ignore[reportMissingImports]
from .widgets import (  # pyright: ignore[reportMissingImports]
    FullWidthDataTable,
)

_BUILD_MODES = ("apk", "module", "both")
_ARCHITECTURES = ("arm64-v8a", "armeabi-v7a", "both", "all")


class BundleAppsScreen(ModalScreen[tuple[str, str] | None]):
    """Show applications exposed by selected bundle and available actions."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=False)
    ]

    def __init__(
        self,
        apps: Sequence[Mapping[str, object]],
        configured: Mapping[str, object],
        source: str,
        can_edit: bool,
    ) -> None:
        super().__init__()
        self.apps = apps
        self.configured = configured
        self.source = source
        self.can_edit = can_edit

    def compose(self) -> ComposeResult:
        with Vertical(id="bundle-app-dialog"):
            yield Static(
                "Applications in selected bundle", id="bundle-app-dialog-title"
            )
            yield FullWidthDataTable(id="bundle-app-dialog-table", cursor_type="row")
            with Horizontal(id="bundle-app-dialog-actions"):
                yield Button(
                    "＋ Add to dashboard",
                    id="dialog-add-bundle-app",
                    disabled=not self.can_edit,
                )
                yield Button(
                    "⇄ Assign to app",
                    id="dialog-assign-bundle",
                    disabled=not self.can_edit,
                )
                yield Button("Cancel", id="dialog-cancel-bundle")

    def on_mount(self) -> None:
        table = self.query_one("#bundle-app-dialog-table", FullWidthDataTable)
        table.add_columns("App", "Package", "Versions", "Patches", "State")
        for app in self.apps:
            package = str(app.get("package", ""))
            if not package:
                continue
            versions = app.get("versions", ())
            version_text = (
                ", ".join(str(value) for value in versions[:3])
                if isinstance(versions, (list, tuple))
                else ""
            )
            current = self.configured.get(package)
            state = "configured" if current is not None else "new"
            if (
                current is not None
                and getattr(current, "patches_source", None) != self.source
            ):
                state = "other bundle"
            table.add_row(
                str(app.get("name", package)),
                package,
                version_text,
                str(app.get("patch_count", 0)),
                state,
                key=package,
            )
        self.call_after_refresh(table.fit_columns)
        table.focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dialog-cancel-bundle":
            self.dismiss(None)
            return
        table = self.query_one("#bundle-app-dialog-table", DataTable)
        if not table.row_count:
            return
        package = str(
            table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        )
        action = (
            "add"
            if event.button.id == "dialog-add-bundle-app"
            else "assign"
            if event.button.id == "dialog-assign-bundle"
            else None
        )
        if action:
            self.dismiss((action, package))

    def action_cancel(self) -> None:
        self.dismiss(None)


class _ContextMenuScreen(ModalScreen[str | None]):
    """Base for menus that close when clicking outside their panel."""

    menu_id: ClassVar[str]

    def on_mouse_down(self, event: MouseDown) -> None:
        menu = self.query_one(f"#{self.menu_id}")
        if not menu.region.contains(event.screen_x, event.screen_y):
            event.stop()
            self.dismiss(None)


class BundleContextMenuScreen(_ContextMenuScreen):
    """Actions for one bundle catalog row."""

    menu_id = "bundle-context-menu"
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=False)
    ]

    def __init__(self, bundle: str, position: tuple[int, int] | None = None) -> None:
        super().__init__()
        self.bundle = bundle
        self.position = position

    def compose(self) -> ComposeResult:
        with Vertical(id="bundle-context-menu"):
            yield Static(self.bundle, id="bundle-context-title", markup=False)
            yield OptionList(
                Option("▣  Show apps", id="show"),
                id="bundle-context-options",
            )

    def on_mount(self) -> None:
        if self.position is None:
            return
        self.add_class("at-pointer")
        x, y = self.position
        self.query_one("#bundle-context-menu").styles.offset = (
            max(0, min(x, self.size.width - 38)),
            max(0, min(y, self.size.height - 6)),
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class DownloadContextMenuScreen(_ContextMenuScreen):
    """Actions for one downloaded APK set."""

    menu_id = "download-context-menu"
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=False)
    ]

    def __init__(self, label: str, position: tuple[int, int] | None = None) -> None:
        super().__init__()
        self.label = label
        self.position = position

    def compose(self) -> ComposeResult:
        with Vertical(id="download-context-menu"):
            yield Static(self.label, id="download-context-title", markup=False)
            yield OptionList(
                Option("▣  Open folder", id="open"),
                Option("✓  Verify", id="verify"),
                Option("✕  Delete", id="delete"),
                id="download-context-options",
            )

    def on_mount(self) -> None:
        if self.position is None:
            return
        self.add_class("at-pointer")
        x, y = self.position
        self.query_one("#download-context-menu").styles.offset = (
            max(0, min(x, self.size.width - 38)),
            max(0, min(y, self.size.height - 8)),
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ContextMenuScreen(_ContextMenuScreen):
    """Compact row action menu opened by right click or Enter."""

    menu_id = "context-menu"
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=False)
    ]

    def __init__(self, package: str, position: tuple[int, int] | None = None) -> None:
        super().__init__()
        self.package = package
        self.position = position

    def compose(self) -> ComposeResult:
        with Vertical(id="context-menu"):
            yield Static(self.package, id="context-title", markup=False)
            yield OptionList(
                Option("✎  Edit app", id="edit"),
                Option("≡  Choose patches", id="patches"),
                Option("✕  Remove app", id="remove"),
                id="context-options",
            )

    def on_mount(self) -> None:
        if self.position is None:
            return
        self.add_class("at-pointer")
        x, y = self.position
        self.query_one("#context-menu").styles.offset = (
            max(0, min(x, self.size.width - 38)),
            max(0, min(y, self.size.height - 8)),
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class BuildContextMenuScreen(_ContextMenuScreen):
    """Build history row actions opened by right click or Enter."""

    menu_id = "build-context-menu"
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=False)
    ]

    def __init__(self, timestamp: str, position: tuple[int, int] | None = None) -> None:
        super().__init__()
        self.timestamp = timestamp
        self.position = position

    def compose(self) -> ComposeResult:
        with Vertical(id="build-context-menu"):
            yield Static(self.timestamp, id="build-context-title", markup=False)
            yield OptionList(
                Option("↗  Open folder", id="open"),
                Option("✕  Delete build", id="delete"),
                id="build-context-options",
            )

    def on_mount(self) -> None:
        if self.position is None:
            return
        self.add_class("at-pointer")
        x, y = self.position
        self.query_one("#build-context-menu").styles.offset = (
            max(0, min(x, self.size.width - 38)),
            max(0, min(y, self.size.height - 6)),
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class CacheContextMenuScreen(_ContextMenuScreen):
    """Cache area action menu opened by right click or Enter."""

    menu_id = "cache-context-menu"
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=False)
    ]

    def __init__(
        self,
        area: str,
        can_delete: bool,
        position: tuple[int, int] | None = None,
    ) -> None:
        super().__init__()
        self.area = area
        self.can_delete = can_delete
        self.position = position

    def compose(self) -> ComposeResult:
        with Vertical(id="cache-context-menu"):
            yield Static(self.area, id="cache-context-title", markup=False)
            yield OptionList(
                Option("↗  Open folder", id="open"),
                Option("✕  Delete", id="delete", disabled=not self.can_delete),
                id="cache-context-options",
            )

    def on_mount(self) -> None:
        if self.position is None:
            return
        self.add_class("at-pointer")
        x, y = self.position
        self.query_one("#cache-context-menu").styles.offset = (
            max(0, min(x, self.size.width - 38)),
            max(0, min(y, self.size.height - 7)),
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class PatchContextMenuScreen(_ContextMenuScreen):
    """Patch actions opened by right click or Enter."""

    menu_id = "patch-context-menu"
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=False)
    ]

    def __init__(
        self,
        patch: str,
        selected: bool,
        option_count: int,
        position: tuple[int, int] | None = None,
    ) -> None:
        super().__init__()
        self.patch = patch
        self.selected = selected
        self.option_count = option_count
        self.position = position

    def compose(self) -> ComposeResult:
        with Vertical(id="patch-context-menu"):
            yield Static(self.patch, id="patch-context-title", markup=False)
            yield OptionList(
                Option(
                    "✕  Disable patch" if self.selected else "＋  Enable patch",
                    id="toggle",
                ),
                Option(
                    f"⚙  Edit options ({self.option_count})",
                    id="options",
                    disabled=self.option_count == 0,
                ),
                id="patch-context-options",
            )

    def on_mount(self) -> None:
        if self.position is None:
            return
        self.add_class("at-pointer")
        x, y = self.position
        self.query_one("#patch-context-menu").styles.offset = (
            max(0, min(x, self.size.width - 42)),
            max(0, min(y, self.size.height - 7)),
        )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class CacheCleanScreen(ModalScreen[tuple[str, ...] | None]):
    """Select cache areas before starting destructive cleanup."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=False)
    ]

    def __init__(
        self,
        choices: Sequence[tuple[str, str, bool]],
        summary: str,
    ) -> None:
        super().__init__()
        self.choices = tuple(choices)
        self.summary = summary

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="cache-clean-dialog"):
            yield Static("Clean cache", classes="modal-title")
            yield Label(self.summary, id="cache-clean-summary")
            with Vertical(id="cache-clean-options"):
                for index, (_name, label, checked) in enumerate(self.choices):
                    yield Checkbox(label, value=checked, id=f"cache-clean-{index}")
            with Horizontal(classes="modal-actions"):
                yield Button("Delete selected", id="cache-clean-apply", variant="error")
                yield Button("Cancel", id="cache-clean-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cache-clean-apply":
            selected = tuple(
                name
                for index, (name, _label, _checked) in enumerate(self.choices)
                if self.query_one(f"#cache-clean-{index}", Checkbox).value
            )
            self.dismiss(selected)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class LocalSourceScreen(ModalScreen[Path | None]):
    """Ask for one app's local APK source using native file dialogs."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=False)
    ]

    def __init__(self, app_name: str, package: str) -> None:
        super().__init__()
        self.app_name = app_name
        self.package = package

    def compose(self) -> ComposeResult:
        with Vertical(id="local-source-dialog"):
            yield Static("Select local APK source", classes="modal-title")
            yield Label(f"{self.app_name} · {self.package}", id="local-source-app")
            yield Label(
                "Select one APK (its folder is used) or the folder containing all splits.",
                id="local-source-help",
            )
            yield Static("No source selected", id="local-source-error", markup=False)
            with Horizontal(classes="modal-actions"):
                yield Button("Select APK", id="choose-source-apk", variant="primary")
                yield Button("Select split folder", id="choose-source-folder")
                yield Button("Cancel", id="cancel-local-source")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-local-source":
            self.dismiss(None)
            return
        button_id = event.button.id or ""
        kind: Literal["apk", "folder"]
        if button_id == "choose-source-apk":
            kind = "apk"
        elif button_id == "choose-source-folder":
            kind = "folder"
        else:
            return
        try:
            path = choose_source(kind)
        except Exception as error:
            self.query_one("#local-source-error", Static).update(
                f"Native picker unavailable: {error}"
            )
            return
        if path is not None:
            self.dismiss(path)

    def action_cancel(self) -> None:
        self.dismiss(None)


class DownloadVersionsScreen(ModalScreen[str | None]):
    """Choose one patch-compatible version for an explicit download."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=False)
    ]

    def __init__(
        self,
        app_name: str,
        package: str,
        versions: Sequence[str],
        selected: str | None,
    ) -> None:
        super().__init__()
        self.app_name = app_name
        self.package = package
        self.versions = tuple(versions)
        self.selected = selected if selected in self.versions else self.versions[0]

    def compose(self) -> ComposeResult:
        with Vertical(id="download-version-dialog"):
            yield Static("Patch-compatible versions", classes="modal-title")
            yield Static(
                f"{self.app_name} · {self.package}",
                id="download-version-dialog-app",
                markup=False,
            )
            yield Label("Select version")
            yield Select(
                ((version, version) for version in self.versions),
                value=self.selected,
                id="download-version-choice",
            )
            yield Static(
                "Only versions supported by selected Morphe patches are shown.",
                id="download-version-dialog-hint",
                markup=False,
            )
            with Horizontal(classes="modal-actions"):
                yield Button(
                    "Use version", id="use-download-version", variant="primary"
                )
                yield Button("Cancel", id="cancel-download-version")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "use-download-version":
            value = self.query_one("#download-version-choice", Select).value
            self.dismiss(None if value is Select.BLANK else str(value))
        elif event.button.id == "cancel-download-version":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """Confirmation for destructive or reviewed actions."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=False)
    ]

    def __init__(
        self,
        message: str,
        *,
        confirm_label: str = "Remove",
        confirm_variant: ButtonVariant = "error",
    ) -> None:
        super().__init__()
        self.message = message
        self.confirm_label = confirm_label
        self.confirm_variant: ButtonVariant = confirm_variant

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="confirm-dialog"):
            yield Label(self.message, id="confirm-question")
            with Horizontal(classes="modal-actions"):
                yield Button(self.confirm_label, id="yes", variant=self.confirm_variant)
                yield Button("Cancel", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_cancel(self) -> None:
        self.dismiss(False)


class PatchOptionsScreen(ModalScreen[dict[str, PatchOptionValue] | None]):
    """Type-aware scalar option editor for one selected patch."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=False)
    ]

    def __init__(
        self,
        patch: str,
        options: Sequence[Mapping[str, object]],
        values: Mapping[str, PatchOptionValue],
    ) -> None:
        super().__init__()
        self.patch = patch
        self.options = tuple(options)
        self.values = dict(values)

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="patch-options-editor"):
            yield Static(f"Patch options · {self.patch}", classes="modal-title")
            for index, option in enumerate(self.options):
                key = str(option.get("key", ""))
                title = str(option.get("title", key))
                description = str(option.get("description", "")).strip()
                default = option.get("default")
                type_name = str(option.get("type", "kotlin.String"))
                yield Label(title)
                details = [description] if description else []
                details.append(f"Key: {key} · Type: {type_name.rsplit('.', 1)[-1]}")
                yield Static(
                    "\n".join(details), classes="patch-option-description", markup=False
                )
                if type_name.endswith(".Boolean"):
                    yield Select(
                        (("True", True), ("False", False)),
                        value=self.values.get(key, Select.BLANK),
                        prompt=(
                            f"Use default ({str(default).lower()})"
                            if default is not None
                            else "Use patch default"
                        ),
                        allow_blank=True,
                        id=f"patch-option-{index}",
                    )
                else:
                    suggestions = option.get("values", ())
                    suggestion_values = (
                        [str(value) for value in suggestions]
                        if isinstance(suggestions, (list, tuple))
                        else []
                    )
                    yield Input(
                        str(self.values[key]) if key in self.values else "",
                        placeholder=(
                            f"Default: {default}"
                            if default is not None
                            else "Required"
                            if option.get("required")
                            else "Use patch default"
                        ),
                        type=(
                            "integer"
                            if type_name.rsplit(".", 1)[-1]
                            in {"Byte", "Short", "Int", "Long"}
                            else "number"
                            if type_name.rsplit(".", 1)[-1] in {"Float", "Double"}
                            else "text"
                        ),
                        suggester=(
                            SuggestFromList(suggestion_values)
                            if suggestion_values
                            else None
                        ),
                        valid_empty=True,
                        id=f"patch-option-{index}",
                    )
            yield Static("", id="patch-option-error", markup=False)
            with Horizontal(classes="modal-actions"):
                yield Button("Save options", id="save-patch-options", variant="primary")
                yield Button("Reset", id="reset-patch-options")
                yield Button("Cancel", id="cancel-patch-options")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-patch-options":
            self.dismiss(None)
            return
        if event.button.id == "reset-patch-options":
            self.dismiss({})
            return
        values = dict(self.values)
        for index, option in enumerate(self.options):
            key = str(option.get("key", ""))
            default = option.get("default")
            required = bool(option.get("required"))
            type_name = str(option.get("type", "kotlin.String"))
            field = self.query_one(f"#patch-option-{index}")
            if isinstance(field, Select):
                value = field.value
                if value is Select.BLANK:
                    values.pop(key, None)
                    if required and default is None:
                        self._error(f"{key} is required")
                        return
                else:
                    values[key] = bool(value)
                continue
            raw = field.value.strip() if isinstance(field, Input) else ""
            if not raw:
                values.pop(key, None)
                if required and default is None:
                    self._error(f"{key} is required")
                    return
                continue
            try:
                values[key] = self._scalar(raw, type_name)
            except ValueError:
                self._error(f"Invalid value for {key}")
                return
        self.dismiss(values)

    def _error(self, message: str) -> None:
        self.query_one("#patch-option-error", Static).update(message)

    @staticmethod
    def _scalar(value: str, type_name: str) -> PatchOptionValue:
        kind = type_name.rsplit(".", 1)[-1]
        try:
            if kind in {"Byte", "Short", "Int", "Long"}:
                return int(value)
            if kind in {"Float", "Double"}:
                return float(value)
        except ValueError:
            raise ValueError("invalid patch option value") from None
        return value

    def action_cancel(self) -> None:
        self.dismiss(None)


class AppEditorScreen(ModalScreen[dict[str, str] | None]):
    """Add/edit fields the TUI safely owns in one app table."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Cancel", show=False)
    ]

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        super().__init__()
        self.values = dict(values or {})
        self.source_dir = self.values.get("source-dir", "").strip()

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="app-editor"):
            yield Static(
                "Edit application" if self.values else "Add application",
                classes="modal-title",
            )
            yield Label("Package")
            yield Input(self.values.get("package", ""), id="edit-package")
            yield Label("Name")
            yield Input(self.values.get("name", ""), id="edit-name")
            yield Label("Build settings", classes="field-section")
            yield Checkbox(
                "Enabled for builds",
                value=self.values.get("enabled", "true").lower() == "true",
                id="edit-enabled",
            )
            yield Checkbox(
                "Include universal patches",
                value=self.values.get("include-universal-patches", "").lower()
                == "true",
                id="edit-include-universal-patches",
            )
            yield Label("Slug (optional)")
            yield Input(self.values.get("slug", ""), id="edit-slug")
            yield Label("Patched package (optional)")
            yield Input(
                self.values.get("patched-package", ""), id="edit-patched-package"
            )
            yield Label("Expected signer SHA-256 (optional)")
            yield Input(
                self.values.get("expected-signer", ""), id="edit-expected-signer"
            )
            yield Label("Downloader settings (optional)", classes="field-section")
            yield Label("Google Play profile")
            yield Input(
                self.values.get("google-play-profile", ""), id="edit-google-profile"
            )
            yield Label("Google Play country")
            yield Input(
                self.values.get("google-play-country", ""), id="edit-google-country"
            )
            yield Label("Google Play proxy")
            yield Input(
                self.values.get("google-play-proxy", ""), id="edit-google-proxy"
            )
            yield Label("Google Play dispenser")
            yield Input(
                self.values.get("google-play-dispenser", ""), id="edit-google-dispenser"
            )
            yield Label("APKMirror URLs (comma-separated)")
            yield Input(
                self.values.get("fallback-apkmirror", ""), id="edit-fallback-apkmirror"
            )
            yield Label("Direct APK URLs (comma-separated)")
            yield Input(
                self.values.get("fallback-direct", ""), id="edit-fallback-direct"
            )
            yield Label("Local APK source (optional)")
            with Vertical(id="source-picker-row"):
                yield Static(
                    self.source_dir or "Not selected; choose it when starting a build",
                    id="edit-source-dir",
                    markup=False,
                )
                with Horizontal(id="source-picker-actions"):
                    yield Button("Select APK", id="choose-app-source-apk")
                    yield Button("Select split folder", id="choose-app-source-folder")
                    yield Button("Clear", id="clear-app-source")
            yield Label("Version")
            yield Input(self.values.get("version", "auto"), id="edit-version")
            yield Label("Build mode")
            yield Select(
                ((value, value) for value in _BUILD_MODES),
                value=self.values.get("build-mode", "both"),
                allow_blank=False,
                id="edit-build-mode",
            )
            yield Label("Architecture")
            yield Select(
                ((value, value) for value in _ARCHITECTURES),
                value=self.values.get("arch", "both"),
                allow_blank=False,
                id="edit-arch",
            )
            yield Label("Patches source owner/repo (optional)")
            yield Input(self.values.get("patches-source", ""), id="edit-patches-source")
            yield Label("Patches version (optional; fixed tag or latest)")
            yield Input(
                self.values.get("patches-version", ""),
                placeholder="latest or release tag, e.g. v1.38.0",
                id="edit-patches-version",
            )
            yield Label("Patches SHA-256 (optional)")
            yield Input(self.values.get("patches-sha256", ""), id="edit-patches-sha256")
            yield Static("", id="editor-error", markup=False)
            with Horizontal(classes="modal-actions"):
                yield Button("Save", id="save-app", variant="primary")
                yield Button("Cancel", id="cancel-app")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-app":
            self.dismiss(None)
            return
        if event.button.id == "clear-app-source":
            self.source_dir = ""
            self.query_one("#edit-source-dir", Static).update(
                "Not selected; choose it when starting a build"
            )
            return
        if event.button.id in {"choose-app-source-apk", "choose-app-source-folder"}:
            try:
                path = choose_source(
                    "apk" if event.button.id == "choose-app-source-apk" else "folder"
                )
            except Exception as error:
                self.query_one("#editor-error", Static).update(
                    f"Native picker unavailable: {error}"
                )
                return
            if path is not None:
                self.source_dir = str(path)
                self.query_one("#edit-source-dir", Static).update(self.source_dir)
            return
        values = {
            "package": self.query_one("#edit-package", Input).value,
            "name": self.query_one("#edit-name", Input).value,
            "enabled": str(self.query_one("#edit-enabled", Checkbox).value).lower(),
            "include-universal-patches": str(
                self.query_one("#edit-include-universal-patches", Checkbox).value
            ).lower(),
            "slug": self.query_one("#edit-slug", Input).value,
            "patched-package": self.query_one("#edit-patched-package", Input).value,
            "expected-signer": self.query_one("#edit-expected-signer", Input).value,
            "google-play-profile": self.query_one("#edit-google-profile", Input).value,
            "google-play-country": self.query_one("#edit-google-country", Input).value,
            "google-play-proxy": self.query_one("#edit-google-proxy", Input).value,
            "google-play-dispenser": self.query_one(
                "#edit-google-dispenser", Input
            ).value,
            "fallback-apkmirror": self.query_one(
                "#edit-fallback-apkmirror", Input
            ).value,
            "fallback-direct": self.query_one("#edit-fallback-direct", Input).value,
            "source-dir": self.source_dir,
            "version": self.query_one("#edit-version", Input).value,
            "build-mode": str(self.query_one("#edit-build-mode", Select).value),
            "arch": str(self.query_one("#edit-arch", Select).value),
            "patches-source": self.query_one("#edit-patches-source", Input).value,
            "patches-version": self.query_one("#edit-patches-version", Input).value,
            "patches-sha256": self.query_one("#edit-patches-sha256", Input).value,
        }
        if not values["package"].strip() or not values["name"].strip():
            self.query_one("#editor-error", Static).update(
                "Package and name are required."
            )
            return
        self.dismiss(values)

    def action_cancel(self) -> None:
        self.dismiss(None)
