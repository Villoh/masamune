"""Textual dashboard over existing build orchestration."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import ClassVar
from urllib.parse import urlparse

from rich.text import Text
from textual import work  # pyright: ignore[reportMissingImports]
from textual.app import (  # pyright: ignore[reportMissingImports]
    App,
    ComposeResult,
    SystemCommand,
)
from textual.binding import (  # pyright: ignore[reportMissingImports]
    BindingType,
)
from textual.containers import (  # pyright: ignore[reportMissingImports]
    Horizontal,
    Vertical,
    VerticalScroll,
)
from textual.css.query import NoMatches  # pyright: ignore[reportMissingImports]
from textual.timer import Timer  # pyright: ignore[reportMissingImports]
from textual.widgets import (  # pyright: ignore[reportMissingImports]
    Button,
    Checkbox,
    ContentSwitcher,
    DataTable,
    Footer,
    Input,
    Label,
    ListItem,
    ListView,
    Select,
    Static,
)
from textual.worker import Worker, WorkerState  # pyright: ignore[reportMissingImports]

from ..apk import verify_apk_set
from ..architecture import Architecture
from ..cli import (  # pyright: ignore[reportMissingImports]
    TEMPLATE_KEYSTORE,
    __version__,
    error_code,
    redact,
)
from ..config import (  # pyright: ignore[reportMissingImports]
    AppConfig,
    ConfigError,
    load_config,
)
from ..config_editor import (  # pyright: ignore[reportMissingImports]
    add_app,
    remove_app,
    set_app_patch_source,
    set_exclusive_patches,
    update_app,
)
from ..errors import ApkMismatch, BuildCancelled, IntegrityMetadataError
from ..logo import LOGO_SMALL_FRAMES, LOGO_SPLASH_FRAMES
from ..orchestrator import (  # pyright: ignore[reportMissingImports]
    Reporter,
    run_build,
    run_bundle_catalog,
    run_clean,
    run_community_bundles,
    run_download,
    run_download_versions,
    run_list_patches,
    run_patch_catalog,
)
from ..paths import default_download_path, migrate_legacy_downloads
from ..providers import ProviderRequest
from .helpers import (
    _BINDINGS,
    _CACHE_CLEANABLE_NAMES,
    _COMMANDS,
    _EYE_SCHEDULE_LENGTH,
    _THEME_NAMES,
    _THEMES,
    _VIEWS,
    _cache_areas,
    _cache_inventory,
    _cell,
    _format_bytes,
    preference_path,
    validate_keybindings,
)
from .helpers import (
    append_build_history as _append_build_history,
)
from .helpers import (
    load_build_history as _load_build_history,
)
from .helpers import (
    load_preferences as _load_preferences,
)
from .helpers import (
    remove_build_history_entry as _remove_build_history_entry,
)
from .helpers import (
    save_preferences as _save_preferences,
)
from .models import (
    DashboardState,
    PatchListEntry,
    PatchOptionValue,
    Preferences,
)
from .screens import (  # pyright: ignore[reportMissingImports]
    AppEditorScreen,
    BuildContextMenuScreen,
    BundleAppsScreen,
    BundleContextMenuScreen,
    CacheCleanScreen,
    CacheContextMenuScreen,
    ConfirmScreen,
    ContextMenuScreen,
    DownloadContextMenuScreen,
    DownloadVersionsScreen,
    LocalSourceScreen,
    PatchContextMenuScreen,
    PatchOptionsScreen,
)
from .widgets import FullWidthDataTable, PatchListItem, PatchSelectionList
from .workers import run_build_task, run_download_task


def load_preferences(path: Path | None = None) -> Preferences:
    return _load_preferences(path or preference_path())


def save_preferences(preferences: Preferences, path: Path | None = None) -> None:
    _save_preferences(preferences, path or preference_path())


def builds_history_path() -> Path:
    return preference_path().parent / "builds.json"


def load_build_history(path: Path | None = None) -> list[dict[str, object]]:
    return _load_build_history(path or builds_history_path())


def append_build_history(
    record: dict[str, object], path: Path | None = None
) -> list[dict[str, object]]:
    return _append_build_history(record, path or builds_history_path())


def remove_build_history_entry(
    timestamp: str, path: Path | None = None
) -> list[dict[str, object]]:
    return _remove_build_history_entry(timestamp, path or builds_history_path())


class MasamuneApp(App[None]):
    """Dashboard over existing validated build operations."""

    CSS_PATH = Path(__file__).with_name("tui.tcss")
    TITLE = "Masamune"
    BINDINGS: ClassVar[list[BindingType]] = list(_BINDINGS)

    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__()
        self.args = args
        self._output_base = args.output
        self.preferences_path = preference_path()
        self.preferences = load_preferences(self.preferences_path)
        self.sidebar_collapsed = False
        self._programmatic_view: str | None = None
        self._patch_selector_value: str | None = None
        self.dashboard_state = DashboardState(args.config)
        self._patch_worker: Worker[dict[str, object]] | None = None
        self._catalog_worker: Worker[dict[str, object]] | None = None
        self._catalog_package: str | None = None
        self._catalog_patch_options: dict[str, tuple[Mapping[str, object], ...]] = {}
        self._catalog_option_values: dict[str, dict[str, PatchOptionValue]] = {}
        self._community_worker: Worker[dict[str, object]] | None = None
        self._community_bundles: list[Mapping[str, object]] | None = None
        self._bundle_worker: Worker[dict[str, object]] | None = None
        self._bundle_catalog: dict[str, object] | None = None
        self._bundle_open_after_load: tuple[str, str] | None = None
        self._build_worker: Worker[dict[str, object]] | None = None
        self._build_cancel_event = Event()
        self._download_worker: Worker[object] | None = None
        self._download_version_worker: Worker[dict[str, object]] | None = None
        self._download_cancel_event = Event()
        self._download_destination: Path = default_download_path()
        self._download_provider_name = "automatic"
        self._download_versions: tuple[str, ...] = ()
        self._download_resolution_package: str | None = None
        self._download_open_version_dialog = False
        self._download_library_records: dict[str, dict[str, object]] = {}
        self._download_library_selected: str | None = None
        self._download_request: ProviderRequest | None = None
        self._download_app: AppConfig | None = None
        self._download_events: list[str] = []
        self._download_result: object | None = None
        self._source_prompt_queue: list[str] = []
        self._clean_worker: Worker[dict[str, object]] | None = None
        self._cache_worker: (
            Worker[list[tuple[str, str, str, bool, bool, int, int]]] | None
        ) = None
        self._cache_inventory: (
            list[tuple[str, str, str, bool, bool, int, int]] | None
        ) = None
        self._cache_inventory_status: str | None = None
        self._builds_history: list[dict[str, object]] = load_build_history(
            self.preferences_path.parent / "builds.json"
        )
        self.build_state = "PENDING"
        self.build_stage = "not started"
        self.build_events: list[str] = []
        self.build_progress: dict[str, str] = {}
        self.build_result: dict[str, object] | None = None
        self._splash_active = True
        self._splash_frame = 0
        self._splash_timer: Timer | None = None
        self._eye_tick = 0
        self._eye_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static(LOGO_SMALL_FRAMES[0], id="header-logo", markup=False)
            with Vertical(id="header-meta"):
                yield Static(f"MASAMUNE v{__version__}", id="header-title")
                yield Static(
                    "No configuration loaded", id="header-config", markup=False
                )
        with Horizontal(id="workspace"):
            with Vertical(id="sidebar"):
                yield ListView(
                    *(
                        ListItem(
                            Label(mark, classes="nav-icon"),
                            Label(title, classes="nav-label"),
                            id=f"nav-{view}",
                        )
                        for view, title, mark in _VIEWS
                    ),
                    id="sidebar-items",
                )
                yield Static(
                    "\n".join(mark for _, _, mark in _VIEWS), id="sidebar-rail"
                )
            with ContentSwitcher(initial="dashboard", id="content"):
                with Vertical(id="dashboard"):
                    yield Static(
                        "Configuration: not loaded", id="config-status", markup=False
                    )
                    yield Static("", id="paths", markup=False)
                    yield Static("Applications", classes="section")
                    with Horizontal(id="app-actions"):
                        yield Button("＋ Add app", id="add-app", variant="primary")
                        yield Button("✎ Edit", id="edit-app")
                        yield Button("≡ Patches", id="app-patches")
                        yield Button("✕ Remove", id="remove-app", variant="error")
                    yield FullWidthDataTable(
                        id="apps", cursor_type="row", zebra_stripes=True
                    )
                with VerticalScroll(id="build"):
                    with Horizontal(id="build-header"):
                        with Vertical(id="build-status-block"):
                            yield Static(
                                "Build: PENDING", id="build-status", markup=False
                            )
                            yield Static(
                                "Build stage: not started",
                                id="build-stage",
                                markup=False,
                            )
                        yield Button(
                            "▶ Start build", id="start-build", variant="primary"
                        )
                        yield Button(
                            "■ Stop build",
                            id="stop-build",
                            variant="error",
                            disabled=True,
                        )
                    yield Static("", id="build-error", markup=False)
                    yield Static("Jobs", classes="section")
                    yield FullWidthDataTable(
                        id="build-jobs", cursor_type="row", zebra_stripes=True
                    )
                    yield Static("", id="results", markup=False)
                    yield Static("Events", classes="section")
                    with VerticalScroll(id="build-events-scroll"):
                        yield Static(
                            "No build events.", id="build-events", markup=False
                        )
                with VerticalScroll(id="builds"):
                    yield Static(
                        "Past builds, newest first. Survives closing the TUI.",
                        classes="view-hint",
                        markup=False,
                    )
                    yield FullWidthDataTable(
                        id="builds-table", cursor_type="row", zebra_stripes=True
                    )
                with VerticalScroll(id="patches"):
                    yield Static(
                        "Choose an app, adjust patches and options, then save exact selection.",
                        classes="view-hint",
                        markup=False,
                    )
                    with Horizontal(id="patch-actions"):
                        yield Select((), prompt="Select application", id="patch-app")
                        yield Button("↻ Refresh", id="fetch-patches", variant="primary")
                        yield Button("⚙ Options", id="patch-options", disabled=True)
                        yield Button("✓ Save selection", id="save-patches")
                    yield Static(
                        "Patch catalog: not loaded", id="patch-status", markup=False
                    )
                    yield PatchSelectionList(id="patch-list")
                    yield Static("Resolved build summary", classes="section")
                    yield FullWidthDataTable(
                        id="patch-table", cursor_type="row", zebra_stripes=True
                    )
                with VerticalScroll(id="bundles"):
                    yield Static(
                        "Community bundles. Select one, then show supported applications.",
                        classes="view-hint",
                        markup=False,
                    )
                    yield Static("Custom patch source", classes="section")
                    with Horizontal(id="bundle-source-actions"):
                        yield Input(
                            "MorpheApp/morphe-patches",
                            placeholder="owner/repository",
                            id="bundle-source",
                        )
                        yield Input(
                            "latest",
                            placeholder="latest or tag",
                            id="bundle-version",
                        )
                        yield Button("Load source", id="load-bundle")
                        yield Button(
                            "▣ Show apps", id="show-bundle-apps", disabled=True
                        )
                        yield Button(
                            "↻ Refresh", id="refresh-bundles", variant="primary"
                        )
                    yield Static(
                        "Community bundles: not loaded",
                        id="bundle-status",
                        markup=False,
                    )
                    yield Static("Community bundles", classes="section")
                    with Horizontal(id="bundle-search-actions"):
                        yield Input(placeholder="Search bundles", id="bundle-search")
                    yield FullWidthDataTable(
                        id="bundles-table", cursor_type="row", zebra_stripes=True
                    )
                with VerticalScroll(id="cache"):
                    yield Static("Cache inventory", classes="section")
                    yield Static("", id="cache-status", markup=False)
                    yield FullWidthDataTable(
                        id="cache-table", cursor_type="row", zebra_stripes=True
                    )
                    yield Static(
                        "Clean cache opens area selector. Signing keys stay protected; "
                        "Toolchain deletion forces preparation again.",
                        classes="view-hint",
                        markup=False,
                    )
                    with Horizontal(id="cache-actions"):
                        yield Button("↻ Refresh", id="refresh-cache")
                        yield Button("Clean cache", id="clean", variant="error")
                with VerticalScroll(id="downloads"):
                    yield Static(
                        "Downloads are explicit and never start from Build.",
                        classes="view-hint",
                        markup=False,
                    )
                    with Horizontal(id="download-actions"):
                        yield Select((), prompt="Select application", id="download-app")
                        yield Select(
                            (("arm64-v8a", "arm64-v8a"), ("arm-v7a", "arm-v7a")),
                            value="arm64-v8a",
                            id="download-arch",
                        )
                        yield Select(
                            (
                                ("Automatic", "automatic"),
                                ("Google Play", "google-play"),
                                ("APKMirror", "apkmirror"),
                                ("Direct", "direct"),
                            ),
                            value="automatic",
                            id="download-provider",
                        )
                    with Horizontal(id="download-version-actions"):
                        yield Select(
                            (),
                            prompt="Resolve patch-compatible versions",
                            id="download-version",
                        )
                        yield Button("Resolve versions", id="resolve-download-versions")
                    yield Static(
                        "Destination: default",
                        id="download-destination",
                        markup=False,
                    )
                    with Horizontal(id="download-buttons"):
                        yield Button(
                            "Download selected", id="start-download", variant="primary"
                        )
                        yield Button(
                            "Stop download",
                            id="stop-download",
                            variant="error",
                            disabled=True,
                        )
                        yield Button(
                            "Open folder", id="open-download-folder", disabled=True
                        )
                        yield Button("View downloads", id="view-download-history")
                        yield Button(
                            "Delete selected", id="delete-download", variant="error"
                        )
                    yield Static(
                        "Automatic: Google Play → APKMirror → Direct",
                        id="download-provider-order",
                        markup=False,
                    )
                    yield Static("", id="download-status", markup=False)
                    with VerticalScroll(id="download-events-scroll"):
                        yield Static(
                            "No download events.", id="download-events", markup=False
                        )
                with VerticalScroll(id="download-library"):
                    yield Static(
                        "Verified APK sets acquired by Masamune.",
                        classes="view-hint",
                        markup=False,
                    )
                    yield Static(
                        "Destination: default", id="download-library-destination"
                    )
                    with Horizontal(id="download-library-actions"):
                        yield Button("Refresh", id="refresh-download-library")
                        yield Button(
                            "Open folder", id="open-library-download", disabled=True
                        )
                        yield Button(
                            "Verify", id="verify-library-download", disabled=True
                        )
                        yield Button(
                            "Delete",
                            id="delete-library-download",
                            variant="error",
                            disabled=True,
                        )
                    yield FullWidthDataTable(
                        id="download-library-table",
                        cursor_type="row",
                        zebra_stripes=True,
                    )
        with Vertical(id="footer-bar"):
            yield Static("Ready.", id="status", markup=False)
            yield Footer()
        yield Static(
            self._splash_content(LOGO_SPLASH_FRAMES[0]),
            id="splash",
            markup=False,
        )

    def on_mount(self) -> None:
        for theme in _THEMES:
            self.register_theme(theme)
        self.theme = self.preferences.theme
        self.watch(self, "theme", self._persist_theme, init=False)
        self.set_keymap(self.preferences.keymap())
        self._set_compact_rail(self.size.width <= 40)
        self.set_class(self.size.width <= 80, "bundle-narrow")
        self.query_one("#apps", FullWidthDataTable).add_columns(
            "App", "Package", "Architecture", "Mode", "Build"
        )
        self.query_one("#build-jobs", FullWidthDataTable).add_columns("Job", "Stage")
        self.query_one("#patch-table", FullWidthDataTable).add_columns(
            "Package", "Version", "Patches"
        )
        self.query_one("#builds-table", FullWidthDataTable).add_columns(
            "Time", "Status", "Output", "Jobs"
        )
        self.query_one("#cache-table", FullWidthDataTable).add_columns(
            "Area", "Purpose", "Files", "Size", "Lifecycle"
        )
        self.query_one("#bundles-table", FullWidthDataTable).add_columns(
            "Bundle", "Author", "Repository", "Patches"
        )
        self._render_community_bundles()
        self._render_build_progress()
        self._render_patches([])
        self._load_dashboard()
        migrated = migrate_legacy_downloads(self._download_destination)
        if migrated:
            self._set_status(f"Migrated {migrated} legacy download folder(s)")
        self._render_download_apps()
        self._render_download_destination()
        self._start_cache_inventory()
        self._render_build_history()
        if self.animation_level != "none":
            self._splash_timer = self.set_interval(0.14, self._advance_splash)
            self._eye_timer = self.set_interval(0.2, self._advance_eyes)
        self.set_timer(
            0.05 if self.animation_level == "none" else 1.2, self.dismiss_splash
        )

    def on_resize(self, event) -> None:  # type: ignore[no-untyped-def]
        self._set_compact_rail(event.size.width <= 40)
        self.set_class(event.size.width <= 80, "bundle-narrow")

    def _set_compact_rail(self, compact: bool) -> None:
        self.set_class(compact, "compact-rail")

    def on_key(self, event) -> None:  # type: ignore[no-untyped-def]
        if self._splash_active:
            event.stop()
            self.dismiss_splash()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        return not self._splash_active

    @staticmethod
    def _splash_content(logo: str) -> str:
        """Center every row to one shared width before Rich sees it.

        Rich's per-line `justify` rounds each line's padding independently,
        so lines of a different length parity than the title can land up to
        a column off from it. Pre-centering to a common width up front
        makes every line the same length, which keeps them aligned no
        matter how Rich rounds.
        """
        lines = (*logo.splitlines(), "", "MASAMUNE")
        width = max(len(line) for line in lines)
        return "\n".join(line.center(width) for line in lines)

    def _advance_splash(self) -> None:
        splash = self.query("#splash")
        if (
            not self._splash_active
            or not splash
            or self._splash_frame >= len(LOGO_SPLASH_FRAMES) - 1
        ):
            if self._splash_timer is not None:
                self._splash_timer.pause()
            return
        self._splash_frame += 1
        splash.first(Static).update(
            self._splash_content(LOGO_SPLASH_FRAMES[self._splash_frame])
        )

    def _advance_eyes(self) -> None:
        """Idle mascot blink: mostly open, brief blink every ~6 seconds."""
        self._eye_tick = (self._eye_tick + 1) % _EYE_SCHEDULE_LENGTH
        frame = LOGO_SMALL_FRAMES[1] if self._eye_tick == 0 else LOGO_SMALL_FRAMES[0]
        header_logo = self.query("#header-logo")
        if header_logo:
            header_logo.first(Static).update(frame)

    def dismiss_splash(self) -> None:
        if self._splash_timer is not None:
            self._splash_timer.stop()
            self._splash_timer = None
        splash = self.query("#splash")
        if splash:
            splash.remove()
        self._splash_active = False

    def action_command_palette(self) -> None:
        if not self._splash_active:
            super().action_command_palette()

    def action_toggle_sidebar(self) -> None:
        if self._splash_active:
            return
        self.sidebar_collapsed = not self.sidebar_collapsed
        self.set_class(self.sidebar_collapsed, "sidebar-collapsed")

    def action_show_help(self) -> None:
        if self._splash_active:
            return
        if self.screen.query("HelpPanel"):
            self.action_hide_help_panel()
        else:
            self.action_show_help_panel()

    def show_view(self, view: str) -> None:
        """Select one workspace view from sidebar, keys, or palette."""
        self.query_one("#content", ContentSwitcher).current = view
        items = self.query_one("#sidebar-items", ListView)
        index = next(
            position for position, entry in enumerate(_VIEWS) if entry[0] == view
        )
        if items.index != index:
            self._programmatic_view = view
            items.index = index

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id == "patch-list":
            self._update_patch_options_button()
            return
        if self._splash_active or event.item is None or not event.item.id:
            return
        view = event.item.id.removeprefix("nav-")
        if self._programmatic_view == view:
            self._programmatic_view = None
            return
        {
            "dashboard": self.action_show_dashboard,
            "build": self.action_show_build_matrix,
            "builds": self.action_show_builds,
            "patches": self.action_show_patches,
            "bundles": self.action_show_bundles,
            "cache": self.action_show_cache,
            "downloads": self.action_show_downloads,
            "download-library": self.action_show_download_library,
        }.get(view, lambda: self.show_view(view))()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        actions = {
            "clean": self.action_clean,
            "refresh-cache": self.action_refresh_cache,
            "start-build": self.action_start_build,
            "stop-build": self.action_stop_build,
            "add-app": self.action_add_app,
            "edit-app": self.action_edit_app,
            "app-patches": self.action_app_patches,
            "remove-app": self.action_remove_app,
            "fetch-patches": self.action_fetch_patch_catalog,
            "patch-options": self.action_edit_patch_options,
            "save-patches": self.action_save_patch_selection,
            "refresh-bundles": self.action_load_community_bundles,
            "show-bundle-apps": self.action_show_bundle_apps,
            "load-bundle": self.action_load_bundle,
            "resolve-download-versions": self.action_resolve_download_versions,
            "start-download": self.action_start_download,
            "stop-download": self.action_stop_download,
            "open-download-folder": self.action_open_download_folder,
            "view-download-history": self.action_show_download_library,
            "refresh-download-library": self.action_show_download_library,
            "open-library-download": self.action_open_library_download,
            "verify-library-download": self.action_verify_library_download,
            "delete-library-download": self.action_delete_library_download,
            "delete-download": self.action_delete_download,
        }
        action = actions.get(event.button.id or "")
        if action is not None:
            action()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "bundle-search":
            self._render_community_bundles()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "download-app":
            selector = self.query_one("#download-version", Select)
            selector.set_options(())
            selector.value = Select.BLANK
            self._download_versions = ()
            self._download_resolution_package = None
            self._download_open_version_dialog = False
            return
        if event.select.id != "patch-app" or event.value is Select.BLANK:
            return
        value = str(event.value)
        if value == self._patch_selector_value:
            return
        self._patch_selector_value = value
        if not self._splash_active and self.query_one("#patches").display:
            self.action_fetch_patch_catalog()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.has_class("patch-checkbox"):
            self._update_patch_options_button()

    def on_patch_selection_list_context_requested(
        self, event: PatchSelectionList.ContextRequested
    ) -> None:
        selection_list = event.selection_list
        options = self._catalog_patch_options.get(event.patch, ())

        def selected(action: str | None) -> None:
            if action == "toggle":
                selection_list.toggle(event.patch)
                self._update_patch_options_button()
            elif action == "options":
                if event.patch not in selection_list.selected:
                    selection_list.select(event.patch)
                self.action_edit_patch_options()

        self.push_screen(
            PatchContextMenuScreen(
                event.patch,
                event.patch in selection_list.selected,
                len(options),
                event.position,
            ),
            selected,
        )

    def on_full_width_data_table_context_requested(
        self, event: FullWidthDataTable.ContextRequested
    ) -> None:
        if event.table.id == "apps":
            self._open_context_menu(event.row_key, event.position)
        elif event.table.id == "builds-table":
            self._open_build_context_menu(event.row_key, event.position)
        elif event.table.id == "cache-table":
            self._open_cache_context_menu(event.row_key, event.position)
        elif event.table.id == "bundles-table":
            self._open_bundle_context_menu(event.row_key, event.position)
        elif event.table.id == "download-library-table":
            self._open_download_context_menu(event.row_key, event.position)

    def _open_bundle_context_menu(
        self, repo: str, position: tuple[int, int] | None = None
    ) -> None:
        def selected(action: str | None) -> None:
            if action == "show":
                self.action_show_bundle_apps(repo)

        self.push_screen(BundleContextMenuScreen(repo, position), selected)

    def _open_context_menu(
        self, package: str, position: tuple[int, int] | None = None
    ) -> None:
        def selected(action: str | None) -> None:
            if action == "edit":
                self._open_app_editor(package)
            elif action == "patches":
                self._show_app_patches(package)
            elif action == "remove":
                self._confirm_remove_app(package)

        self.push_screen(ContextMenuScreen(package, position), selected)

    def _build_record(self, timestamp: str) -> dict[str, object] | None:
        record = next(
            (
                record
                for record in self._builds_history
                if record.get("timestamp") == timestamp
            ),
            None,
        )
        if record is not None:
            return record
        base, separator, ordinal = timestamp.rpartition("#")
        if not separator or not ordinal.isdigit():
            return None
        matches = [
            record for record in self._builds_history if record.get("timestamp") == base
        ]
        try:
            index = int(ordinal)
        except ValueError:
            return None
        return matches[index] if index < len(matches) else None

    def _open_cache_context_menu(
        self, area: str, position: tuple[int, int] | None = None
    ) -> None:
        path = self._cache_area_path(area)
        can_delete = (
            area in _CACHE_CLEANABLE_NAMES
            and path is not None
            and path.exists()
            and not path.is_symlink()
        )

        def selected(action: str | None) -> None:
            if action == "open":
                self._open_cache_folder(area)
            elif action == "delete" and can_delete:
                self._confirm_delete_cache_area(area)

        self.push_screen(CacheContextMenuScreen(area, can_delete, position), selected)

    def _cache_area_path(self, area: str) -> Path | None:
        for name, path, _purpose, _policy, _count_in_total in _cache_areas(
            self.args.cache, self.args.keystore
        ):
            if name == area:
                return path
        return None

    def _open_cache_folder(self, area: str) -> None:
        path = self._cache_area_path(area)
        if path is None:
            self._set_status("Cache area not found")
            return
        folder = path if path.is_dir() else path.parent
        if not folder.is_dir():
            self._set_status("Cache folder not found")
            return
        try:
            if sys.platform == "win32":
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.run(["open", str(folder)], check=False)
            else:
                subprocess.run(["xdg-open", str(folder)], check=False)
        except OSError:
            self._set_status("Could not open cache folder")

    def _confirm_delete_cache_area(self, area: str) -> None:
        path = self._cache_area_path(area)
        if path is None or area not in _CACHE_CLEANABLE_NAMES:
            return
        self.push_screen(
            ConfirmScreen(
                "\n".join(
                    (
                        f"Delete cache area: {area}?",
                        f"Path: {redact(str(path))}",
                        "This cannot be undone.",
                    )
                ),
                confirm_label="Delete",
                confirm_variant="error",
            ),
            lambda confirmed: self._start_cache_clean((area,)) if confirmed else None,
        )

    def _open_build_context_menu(
        self, timestamp: str, position: tuple[int, int] | None = None
    ) -> None:
        def selected(action: str | None) -> None:
            if action == "open":
                self._open_build_folder(timestamp)
            elif action == "delete":
                self._confirm_delete_build(timestamp)

        self.push_screen(BuildContextMenuScreen(timestamp, position), selected)

    def _open_build_folder(self, timestamp: str) -> None:
        record = self._build_record(timestamp)
        output = record.get("output") if record else None
        path = Path(str(output)) if isinstance(output, str) and output else None
        if path is None or not path.is_dir():
            self._set_status("Build output folder not found")
            return
        try:
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.run(["open", str(path)], check=False)
            else:
                subprocess.run(["xdg-open", str(path)], check=False)
        except OSError:
            self._set_status("Could not open build output folder")

    def _confirm_delete_build(self, timestamp: str) -> None:
        record = self._build_record(timestamp)
        if record is None:
            return
        self.push_screen(
            ConfirmScreen(
                "\n".join(
                    (
                        f"Delete build {timestamp}?",
                        f"Output: {redact(str(record.get('output', '')))}",
                        "Removes the output folder and this history entry.",
                    )
                ),
                confirm_label="Delete build",
                confirm_variant="error",
            ),
            lambda confirmed: self._delete_build(timestamp) if confirmed else None,
        )

    def _delete_build(self, timestamp: str) -> None:
        record = self._build_record(timestamp)
        output = record.get("output") if record else None
        if isinstance(output, str) and output:
            path = Path(output)
            try:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
            except OSError:
                self._set_status(f"Could not delete build output: {redact(str(path))}")
                return
        self._builds_history = remove_build_history_entry(
            timestamp, self.preferences_path.parent / "builds.json"
        )
        self._render_build_history()
        self._set_status(f"Deleted build {timestamp}")

    def _selected_app_package(self) -> str | None:
        table = self.query_one("#apps", FullWidthDataTable)
        if not table.row_count:
            self._set_status("No application selected")
            return None
        return str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)

    def action_add_app(self) -> None:
        self._open_app_editor(None)

    def action_edit_app(self) -> None:
        package = self._selected_app_package()
        if package is not None:
            self._open_app_editor(package)

    def _open_app_editor(self, package: str | None) -> None:
        path = self.dashboard_state.config_path
        if path is None:
            self._set_status("Load a configuration before editing applications")
            return
        app = next(
            (item for item in self.dashboard_state.apps if item.package == package),
            None,
        )
        values = None if app is None else self._editable_app_fields(app)

        def saved(fields: dict[str, str] | None) -> None:
            if fields is None:
                return
            try:
                if package is None:
                    add_app(path, fields)
                else:
                    update_app(path, package, fields)
            except (ConfigError, OSError, TypeError, ValueError) as error:
                self._set_status(f"Cannot save application: {redact(str(error))}")
                return
            self._reload_configuration()
            self._set_status("Application saved to morphe.toml")

        self.push_screen(AppEditorScreen(values), saved)

    @staticmethod
    def _editable_app_fields(app: AppConfig) -> dict[str, str]:
        return {
            "package": app.package,
            "name": app.name,
            "enabled": str(app.enabled).lower(),
            "include-universal-patches": (
                ""
                if app.include_universal_patches is None
                else str(app.include_universal_patches).lower()
            ),
            "slug": app.slug,
            "patched-package": app.patched_package or "",
            "expected-signer": app.expected_signer or "",
            "source-dir": app.source_dir or "",
            "google-play-profile": app.google_play.profile or "",
            "google-play-country": app.google_play.country or "",
            "google-play-proxy": app.google_play.proxy or "",
            "google-play-dispenser": app.google_play.dispenser or "",
            "fallback-direct": ", ".join(app.fallbacks.direct),
            "fallback-apkmirror": ", ".join(app.fallbacks.apkmirror),
            "version": app.version,
            "build-mode": app.build_mode,
            "arch": app.arch,
            "patches-source": app.patches_source or "",
            "patches-version": app.patches_version or "",
            "patches-sha256": app.patches_sha256 or "",
        }

    def action_remove_app(self) -> None:
        package = self._selected_app_package()
        if package is not None:
            self._confirm_remove_app(package)

    def _confirm_remove_app(self, package: str) -> None:
        path = self.dashboard_state.config_path
        if path is None:
            return

        def confirmed(remove: bool | None) -> None:
            if not remove:
                return
            try:
                remove_app(path, package)
            except (ConfigError, OSError, TypeError, ValueError) as error:
                self._set_status(f"Cannot remove application: {redact(str(error))}")
                return
            self._reload_configuration()
            self._set_status("Application removed from morphe.toml")

        self.push_screen(
            ConfirmScreen(f"Remove {package} from morphe.toml?"), confirmed
        )

    def action_app_patches(self) -> None:
        package = self._selected_app_package()
        if package is not None:
            self._show_app_patches(package)

    def _show_app_patches(self, package: str) -> None:
        self.show_view("patches")
        selector = self.query_one("#patch-app", Select)
        if selector.value == package:
            self.action_fetch_patch_catalog()
        else:
            selector.value = package

    def action_show_dashboard(self) -> None:
        if not self._splash_active:
            self.show_view("dashboard")

    def action_show_build_matrix(self) -> None:
        if not self._splash_active:
            self.show_view("build")

    def action_show_builds(self) -> None:
        if not self._splash_active:
            self.show_view("builds")
            self._render_build_history()

    def action_show_patches(self) -> None:
        if not self._splash_active:
            self.show_view("patches")
            self.action_list_patches()
            if self._catalog_package is None:
                self.action_fetch_patch_catalog()

    def action_show_bundles(self) -> None:
        if self._splash_active:
            return
        self.show_view("bundles")
        self._render_community_bundles()
        if self._community_bundles is None:
            self.action_load_community_bundles()

    def action_show_cache(self) -> None:
        if not self._splash_active:
            self.show_view("cache")
            self._start_cache_inventory()

    def action_show_downloads(self) -> None:
        if self._splash_active:
            return
        self.show_view("downloads")
        self._render_download_apps()

    def _render_download_apps(self) -> None:
        selector = self.query_one("#download-app", Select)
        current = selector.value
        selector.set_options(
            (Text(app.name), app.package)
            for app in self.dashboard_state.apps
            if app.enabled
        )
        packages = {app.package for app in self.dashboard_state.apps if app.enabled}
        if current in packages:
            selector.value = current
        elif packages:
            selector.value = next(iter(packages))
        else:
            selector.value = Select.BLANK

    def action_resolve_download_versions(self, *, open_dialog: bool = False) -> None:
        app = self._selected_download_app()
        if app is None:
            return
        self._download_open_version_dialog = open_dialog
        if (
            self._download_version_worker is not None
            and not self._download_version_worker.is_finished
        ):
            self._set_download_status("Resolving patch-compatible versions")
            return
        selector = self.query_one("#download-version", Select)
        selector.set_options(())
        selector.value = Select.BLANK
        self._download_resolution_package = app.package
        self._set_download_status("Resolving patch-compatible versions")
        self._download_version_worker = self.resolve_download_versions_worker(
            app.package
        )

    def _render_download_versions(self, result: Mapping[str, object]) -> None:
        result_package = result.get("package")
        current_app = self._selected_download_app()
        if (
            isinstance(result_package, str)
            and current_app is not None
            and result_package != current_app.package
        ):
            return
        raw_versions = result.get("versions", ())
        versions = (
            tuple(str(value) for value in raw_versions if isinstance(value, str))
            if isinstance(raw_versions, (list, tuple))
            else ()
        )
        self._download_versions = versions
        selector = self.query_one("#download-version", Select)
        selector.set_options((version, version) for version in versions)
        selected = result.get("selected")
        if isinstance(selected, str) and selected in versions:
            selector.value = selected
        elif versions:
            selector.value = versions[0]
        self._set_download_status(
            f"{len(versions)} patch-compatible version(s) available"
            if versions
            else "No patch-compatible versions found"
        )
        open_dialog = self._download_open_version_dialog
        self._download_open_version_dialog = False
        if not versions or not open_dialog or self._download_resolution_package is None:
            return
        app = next(
            (
                item
                for item in self.dashboard_state.apps
                if item.package == self._download_resolution_package
            ),
            None,
        )
        if app is None:
            return
        self.push_screen(
            DownloadVersionsScreen(
                app.name,
                app.package,
                versions,
                str(selected) if isinstance(selected, str) else None,
            ),
            self._handle_download_version_selection,
        )

    def _handle_download_version_selection(self, version: str | None) -> None:
        if version is None:
            self._set_download_status("Version selection cancelled")
            return
        self.query_one("#download-version", Select).value = version
        self._set_download_status(f"Download version selected: {version}")
        self.action_start_download()

    def _render_download_destination(self) -> None:
        self.query_one("#download-destination", Static).update(
            f"Destination: {redact(str(self._download_destination))}"
        )

    def _selected_download_app(self) -> AppConfig | None:
        value = self.query_one("#download-app", Select).value
        if value is Select.BLANK:
            self._set_download_status("No application selected")
            return None
        app = next(
            (item for item in self.dashboard_state.apps if item.package == str(value)),
            None,
        )
        if app is None:
            self._set_download_status("Selected application is unavailable")
        return app

    def action_start_download(self) -> None:
        if self._download_worker is not None and not self._download_worker.is_finished:
            self._set_download_status("Download already running")
            return
        app = self._selected_download_app()
        if app is None:
            return
        provider_name = str(self.query_one("#download-provider", Select).value)
        selected_version = self.query_one("#download-version", Select).value
        if selected_version is Select.BLANK:
            self.action_resolve_download_versions(open_dialog=True)
            return
        requested_version = str(selected_version)
        version_path = Path(requested_version)
        if version_path.name != requested_version or requested_version in {".", ".."}:
            self._set_download_status("Version is not a safe download path")
            return
        architecture = Architecture.from_config(
            str(self.query_one("#download-arch", Select).value)
        )
        output = (
            self._download_destination / app.slug / version_path / architecture.value
        )
        direct_hosts = sorted(
            {
                (urlparse(url).hostname or "<invalid>").lower()
                for url in app.fallbacks.direct
            }
        )
        direct_note = (
            "Direct hosts: " + ", ".join(direct_hosts)
            if direct_hosts
            else "Direct: not configured"
        )
        provider_label = {
            "automatic": "Automatic",
            "google-play": "Google Play",
            "apkmirror": "APKMirror",
            "direct": "Direct",
        }[provider_name]
        self._download_provider_name = provider_name
        self._download_app = app
        self._download_request = ProviderRequest(
            app.package,
            requested_version,
            app.version_code,
            architecture.goopdl,
            output,
            app.expected_signer,
        )
        self.push_screen(
            ConfirmScreen(
                "\n".join(
                    (
                        "Download verified stock",
                        f"App: {app.name} · {app.package}",
                        f"Version: {requested_version or 'latest available'}",
                        f"ABI: {architecture.value}",
                        f"Provider: {provider_label}",
                        direct_note if provider_name in {"automatic", "direct"} else "",
                        f"Destination: {redact(str(output))}",
                        "Download starts only after confirmation.",
                    )
                ),
                confirm_label="Download",
                confirm_variant="primary",
            ),
            self._handle_download_confirmation,
        )

    def _handle_download_confirmation(self, confirmed: bool | None) -> None:
        if (
            not confirmed
            or self._download_request is None
            or self._download_app is None
        ):
            self._set_download_status("Download cancelled before start")
            return
        self._download_cancel_event.clear()
        self._download_events.clear()
        self._download_result = None
        self.query_one("#stop-download", Button).disabled = False
        self.query_one("#open-download-folder", Button).disabled = True
        self._set_download_status("Downloading verified stock")
        self._render_download_events()
        self._download_worker = self.run_download_worker()

    def action_stop_download(self) -> None:
        if self._download_worker is None or self._download_worker.is_finished:
            self._set_download_status("No download running")
            return
        self._download_cancel_event.set()
        self.query_one("#stop-download", Button).disabled = True
        self._set_download_status("Stopping download")

    def action_show_download_library(self) -> None:
        if self._splash_active:
            return
        self.show_view("download-library")
        self._render_download_library()

    def _scan_download_library(self) -> dict[str, dict[str, object]]:
        records: dict[str, dict[str, object]] = {}
        app_names = {app.package: app.name for app in self.dashboard_state.apps}
        root = self._download_destination
        if not root.is_dir() or root.is_symlink():
            return records
        for provenance in root.rglob("provenance.json"):
            if provenance.is_symlink() or not provenance.is_file():
                continue
            try:
                data = json.loads(provenance.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            version = data.get("version")
            package = data.get("package")
            provider = data.get("provider")
            architecture = data.get("architecture")
            version_name = version.get("name") if isinstance(version, dict) else None
            version_code = version.get("code") if isinstance(version, dict) else None
            if (
                not isinstance(package, str)
                or not isinstance(provider, str)
                or not isinstance(architecture, str)
                or not isinstance(version_name, str)
                or not isinstance(version_code, str)
            ):
                continue
            if provider == "local" and data.get("schema_version") == 2:
                provider = "google-play"
            artifacts = data.get("files", data.get("artifacts", ()))
            sizes = (
                [
                    item.get("size", 0)
                    for item in artifacts
                    if isinstance(item, dict) and isinstance(item.get("size"), int)
                ]
                if isinstance(artifacts, list)
                else []
            )
            path = str(provenance.parent)
            records[path] = {
                "app": app_names.get(package, package),
                "package": package,
                "version": version_name,
                "version_code": version_code,
                "architecture": architecture,
                "provider": provider,
                "files": len(artifacts) if isinstance(artifacts, list) else 0,
                "size": _format_bytes(sum(sizes)),
                "path": path,
            }
        return dict(sorted(records.items(), key=lambda item: item[0].lower()))

    def _render_download_library(self) -> None:
        self._download_library_records = self._scan_download_library()
        self._download_library_selected = None
        self.query_one("#download-library-destination", Static).update(
            f"Destination: {redact(str(self._download_destination))}"
        )
        table = self.query_one("#download-library-table", FullWidthDataTable)
        table.clear(columns=True)
        table.add_columns("App", "Version", "ABI", "Provider", "Files", "Size")
        for path, record in self._download_library_records.items():
            table.add_row(
                str(record["app"]),
                str(record["version"]),
                str(record["architecture"]),
                str(record["provider"]),
                str(record["files"]),
                str(record["size"]),
                key=path,
            )
        self._update_download_library_actions()
        self.call_after_refresh(table.fit_columns)
        if self._download_library_records:
            table.focus()

    def _selected_download_library_record(self) -> dict[str, object] | None:
        path = self._download_library_selected
        return self._download_library_records.get(path) if path else None

    def _update_download_library_actions(self) -> None:
        enabled = self._selected_download_library_record() is not None
        for button_id in (
            "open-library-download",
            "verify-library-download",
            "delete-library-download",
        ):
            self.query_one(f"#{button_id}", Button).disabled = not enabled

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "download-library-table":
            return
        self._download_library_selected = str(event.row_key.value)
        self._update_download_library_actions()

    def _open_download_context_menu(
        self, path: str, position: tuple[int, int] | None = None
    ) -> None:
        record = self._download_library_records.get(path)
        if record is None:
            return
        self._download_library_selected = path
        self._update_download_library_actions()

        def selected(action: str | None) -> None:
            if action == "open":
                self.action_open_library_download()
            elif action == "verify":
                self.action_verify_library_download()
            elif action == "delete":
                self.action_delete_library_download()

        label = f"{record['app']} · {record['version']} · {record['architecture']}"
        self.push_screen(DownloadContextMenuScreen(label, position), selected)

    def action_open_library_download(self) -> None:
        record = self._selected_download_library_record()
        if record is None:
            return
        path = Path(str(record["path"]))
        if not path.is_dir() or path.is_symlink():
            self._set_status("Downloaded folder not found")
            return
        try:
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.run(["open", str(path)], check=False)
            else:
                subprocess.run(["xdg-open", str(path)], check=False)
        except OSError:
            self._set_status("Could not open downloaded folder")

    def action_verify_library_download(self) -> None:
        record = self._selected_download_library_record()
        if record is None:
            return
        self._set_status("Verifying downloaded APK set")
        self.notify("Verifying downloaded APK set", severity="information")
        path = Path(str(record["path"]))
        try:
            architecture = Architecture.from_goopdl(str(record["architecture"]))
            app = next(
                item
                for item in self.dashboard_state.apps
                if item.package == record["package"]
            )
            verify_apk_set(
                path,
                str(record["package"]),
                version_name=str(record["version"]),
                version_code=str(record["version_code"]),
                arch=architecture.goopdl,
                expected_signer=app.expected_signer,
            )
        except (
            ApkMismatch,
            IntegrityMetadataError,
            OSError,
            ValueError,
            StopIteration,
        ) as error:
            message = f"Download verification failed: {redact(str(error))}"
            self._set_status(message)
            self.notify(message, severity="error")
            return
        message = f"Verified {record['app']} {record['version']}"
        self._set_status(message)
        self.notify(message, severity="information")

    def action_delete_library_download(self) -> None:
        record = self._selected_download_library_record()
        if record is None:
            return
        self.push_screen(
            ConfirmScreen(
                f"Delete downloaded APK set?\n{redact(str(record['path']))}\nThis cannot be undone.",
                confirm_label="Delete",
                confirm_variant="error",
            ),
            lambda confirmed: (
                self._delete_library_download(record) if confirmed else None
            ),
        )

    def _delete_library_download(self, record: Mapping[str, object]) -> None:
        path = Path(str(record["path"]))
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
        except OSError:
            self._set_status("Could not delete downloaded APK set")
            return
        self._set_status("Downloaded APK set deleted")
        self._render_download_library()

    def action_delete_download(self) -> None:
        path = self._download_request.output if self._download_request else None
        if path is None or not path.is_dir():
            self._set_download_status("No verified download selected")
            return
        self.push_screen(
            ConfirmScreen(
                f"Delete verified download?\n{redact(str(path))}\nThis cannot be undone.",
                confirm_label="Delete download",
                confirm_variant="error",
            ),
            self._handle_delete_download,
        )

    def _handle_delete_download(self, confirmed: bool | None) -> None:
        if not confirmed or self._download_request is None:
            return
        path = self._download_request.output
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
        except OSError:
            self._set_download_status("Could not delete verified download")
            return
        self._download_result = None
        self.query_one("#open-download-folder", Button).disabled = True
        self._set_download_status("Verified download deleted")

    def action_open_download_folder(self) -> None:
        path = self._download_request.output if self._download_request else None
        if path is None or not path.is_dir():
            self._set_download_status("Verified download folder not found")
            return
        try:
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.run(["open", str(path)], check=False)
            else:
                subprocess.run(["xdg-open", str(path)], check=False)
        except OSError:
            self._set_download_status("Could not open download folder")

    def _set_download_status(self, message: str) -> None:
        self.query_one("#download-status", Static).update(redact(message))

    def _relay_download_event(self, event: dict[str, object]) -> None:
        with suppress(Exception):
            self.call_from_thread(self._record_download_event, event)

    def _record_download_event(self, event: dict[str, object]) -> None:
        stage = redact(str(event.get("event", "download")))
        message = redact(str(event.get("message", "")))
        fields = " ".join(
            f"{redact(str(key))}={redact(str(value))}"
            for key, value in event.items()
            if key not in {"event", "message"}
        )
        self._download_events.append(
            f"[{stage}] {message}{(' ' + fields) if fields else ''}"
        )
        self._render_download_events()

    def _render_download_events(self) -> None:
        self.query_one("#download-events", Static).update(
            "\n".join(self._download_events) or "No download events."
        )

    def action_start_build(self) -> None:
        if self._splash_active:
            return
        if self._build_worker is not None and not self._build_worker.is_finished:
            self._set_status("Build already running")
            self.notify("Build already running", severity="warning")
            return
        self.show_view("build")
        missing = [
            app.package
            for app in self.dashboard_state.apps
            if app.enabled
            and not app.source_dir
            and not self._has_verified_download(app.package)
        ]
        if missing:
            self._source_prompt_queue = missing
            self._prompt_local_source()
            return
        self._show_build_confirmation()

    def _has_verified_download(self, package: str) -> bool:
        return any(
            record.get("package") == package
            for record in self._scan_download_library().values()
        )

    def _prompt_local_source(self) -> None:
        if not self._source_prompt_queue:
            self._show_build_confirmation()
            return
        package = self._source_prompt_queue[0]
        app = next(
            (item for item in self.dashboard_state.apps if item.package == package),
            None,
        )
        if app is None:
            self._source_prompt_queue.pop(0)
            self._prompt_local_source()
            return

        def selected(path: Path | None) -> None:
            if path is None:
                self._source_prompt_queue.clear()
                self._set_status("Build cancelled: local APK source not selected")
                return
            config_path = self.dashboard_state.config_path
            if config_path is None:
                self._source_prompt_queue.clear()
                self._set_status("Build cancelled: configuration is not loaded")
                return
            try:
                update_app(config_path, package, {"source-dir": str(path)})
            except (ConfigError, OSError, TypeError, ValueError) as error:
                self._source_prompt_queue.clear()
                self._set_status(f"Cannot save local APK source: {redact(str(error))}")
                return
            self._source_prompt_queue.pop(0)
            self._reload_configuration()
            self._prompt_local_source()

        self.push_screen(LocalSourceScreen(app.name, app.package), selected)

    def _show_build_confirmation(self) -> None:
        self._set_status("Review build and confirm or cancel")
        self.args.output = self._next_build_output()
        self.push_screen(
            ConfirmScreen(
                self._confirmation_text(),
                confirm_label="Confirm build",
                confirm_variant="primary",
            ),
            self._handle_build_confirmation,
        )

    def action_stop_build(self) -> None:
        if self._build_worker is None or self._build_worker.is_finished:
            self._set_status("No build running")
            return
        self.push_screen(
            ConfirmScreen(
                "Stop active build after current operation? Temporary work will be discarded.",
                confirm_label="Stop build",
                confirm_variant="error",
            ),
            self._handle_stop_confirmation,
        )

    def _handle_stop_confirmation(self, confirmed: bool | None) -> None:
        if not confirmed:
            return
        if self._build_worker is None or self._build_worker.is_finished:
            self._set_status("No build running")
            return
        self._build_cancel_event.set()
        self.query_one("#stop-build", Button).disabled = True
        self._set_status("Stopping build after current operation")
        self.notify("Stopping after current operation", severity="warning")

    def _next_build_output(self) -> Path:
        """Give each build its own timestamped output directory.

        Refusing to overwrite an existing output is deliberate (never clobber
        a prior build). A fresh subfolder every run means that safety check
        never blocks a repeat build.
        """
        return self._output_base / datetime.now().strftime("%Y%m%d-%H%M%S")

    def _handle_build_confirmation(self, confirmed: bool | None) -> None:
        if not confirmed:
            self._set_status("Build cancelled before start")
            return
        if not self.dashboard_state.loaded:
            self._set_build_status("FAILED")
            self._set_build_error("Build requires loaded configuration")
            return
        if (
            self.args.keystore is not None
            and not os.environ.get("MORPHE_KEYSTORE_PASSWORD")
            and not self._uses_template_keystore()
        ):
            self._set_build_status("FAILED")
            self._set_build_error("MORPHE_KEYSTORE_PASSWORD is required")
            return
        if self.args.output.exists() or self.args.output.is_symlink():
            self._set_build_status("FAILED")
            self._set_build_error(
                f"Output already exists: {redact(str(self.args.output))}"
            )
            return
        self._build_cancel_event.clear()
        self.query_one("#stop-build", Button).disabled = False
        self.build_events.clear()
        self.build_progress.clear()
        self.build_result = None
        self._set_build_status("PENDING")
        self._set_build_stage("pending")
        self._set_build_error("")
        self._render_build_events()
        self._render_build_progress()
        self._set_panel("#results", "")
        self._build_worker = self.run_build_worker()

    def _confirmation_text(self) -> str:
        config = (
            "not loaded" if self.args.config is None else redact(str(self.args.config))
        )
        keystore = (
            "auto-generated per-user key (first build)"
            if self.args.keystore is None
            else redact(os.path.relpath(self.args.keystore))
        )
        password_note = (
            "Public template keystore uses its bundled test password."
            if self._uses_template_keystore()
            else "Auto-generated key manages its own password."
            if self.args.keystore is None
            else "Password is read only from environment; never displayed."
        )
        return "\n".join(
            (
                f"Configuration: {config}",
                f"Output: {redact(str(self.args.output))}",
                f"Cache: {redact(str(self.args.cache))}",
                f"Keystore: {keystore}",
                f"Alias: {redact(str(self.args.keystore_alias))}",
                password_note,
            )
        )

    def _uses_template_keystore(self) -> bool:
        keystore = self.args.keystore
        return bool(
            keystore is not None
            and TEMPLATE_KEYSTORE.is_file()
            and not TEMPLATE_KEYSTORE.is_symlink()
            and keystore.resolve() == TEMPLATE_KEYSTORE.resolve()
        )

    @work(thread=True, exit_on_error=False, name="build")
    def run_build_worker(self) -> dict[str, object]:
        return run_build_task(
            self.args,
            runner=run_build,
            reporter=Reporter(sink=self._relay_build_event),
            cancel_event=self._build_cancel_event,
            uses_template_keystore=self._uses_template_keystore(),
            output_sink=self._record_subprocess_output,
        )

    @work(thread=True, exit_on_error=False, name="download-versions")
    def resolve_download_versions_worker(self, package: str) -> dict[str, object]:
        config_path = self.dashboard_state.config_path
        if config_path is None:
            raise RuntimeError("configuration is not loaded")
        return run_download_versions(
            config_path, cache=self.args.cache, package=package
        )

    @work(thread=True, exit_on_error=False, name="download")
    def run_download_worker(self) -> object:
        request = self._download_request
        app = self._download_app
        if request is None or app is None:
            raise RuntimeError("download request is not prepared")
        return run_download_task(
            request,
            runner=run_download,
            reporter=Reporter(sink=self._relay_download_event),
            cancel_event=self._download_cancel_event,
            output_sink=self._record_download_output,
            cache=self.args.cache,
            google_play=app.google_play,
            fallbacks=app.fallbacks,
            provider_name=self._download_provider_name,
        )

    def _record_download_output(self, line: str) -> None:
        for message in line.replace("\r", "\n").splitlines():
            message = redact(message.strip())
            if message:
                self._relay_download_event({"event": "provider", "message": message})

    def _record_subprocess_output(self, line: str) -> None:
        for message in line.replace("\r", "\n").splitlines():
            message = redact(message.strip())
            if message:
                self._relay_build_event({"event": "tool", "message": message})

    def _relay_build_event(self, event: dict[str, object]) -> None:
        with suppress(Exception):
            self.call_from_thread(self._record_build_event, event)

    @staticmethod
    def _build_job_key(package: str, architecture: str) -> str:
        try:
            normalized = Architecture.from_config(architecture).value
        except ValueError:
            try:
                normalized = Architecture.from_goopdl(architecture).value
            except ValueError:
                normalized = architecture
        return f"{redact(package)} {redact(normalized)}"

    def _record_build_event(self, event: dict[str, object]) -> None:
        stage = redact(str(event.get("event", "unknown")))
        message = redact(str(event.get("message", "")))
        fields = " ".join(
            f"{redact(str(key))}={redact(str(value))}"
            for key, value in event.items()
            if key not in {"event", "message"}
        )
        package = event.get("package")
        architecture = event.get("arch")
        self.build_stage = stage
        if (
            isinstance(package, str)
            and package
            and isinstance(architecture, str)
            and architecture
        ):
            job = self._build_job_key(package, architecture)
            self.build_progress[job] = stage
        self.build_events.append(
            f"[{stage}] {message}{(' ' + fields) if fields else ''}"
        )
        self._set_build_stage(stage)
        self._render_build_progress()
        self._render_build_events()

    def _start_cache_inventory(
        self, *, status: str | None = "Cache inventory pending"
    ) -> None:
        if self._cache_worker is not None and not self._cache_worker.is_finished:
            self._cache_inventory_status = (
                None if status is None else self._cache_inventory_status
            )
            if status is not None:
                self._set_status("Cache inventory loading")
            return
        self._cache_inventory_status = (
            "Cache inventory refreshed" if status is not None else None
        )
        if status is not None:
            self._set_status(status)
        self._cache_worker = self.load_cache_inventory()

    @work(thread=True, exit_on_error=False, name="cache-inventory")
    def load_cache_inventory(
        self,
    ) -> list[tuple[str, str, str, bool, bool, int, int]]:
        return _cache_inventory(self.args.cache, self.args.keystore)

    def action_refresh_cache(self) -> None:
        if self._splash_active:
            return
        self._start_cache_inventory()

    def action_clean(self) -> None:
        if self._splash_active:
            return
        if self._build_worker is not None and not self._build_worker.is_finished:
            self._set_status("Cannot clean cache while build is running")
            return
        if self._clean_worker is not None and not self._clean_worker.is_finished:
            self._set_status("Cache clean already running")
            return
        if self._cache_inventory is None:
            self._start_cache_inventory()
            self._set_status("Cache inventory loading; try Clean cache again shortly")
            return
        choices = [
            (
                name,
                f"{name} · {purpose} · {files} file(s), {_format_bytes(size)}",
                policy == "disposable",
            )
            for name, purpose, policy, _count_in_total, _exists, files, size in self._cache_inventory
            if name in _CACHE_CLEANABLE_NAMES
        ]
        self.push_screen(
            CacheCleanScreen(
                choices,
                "Select cache areas to delete. Signing keys are protected. "
                "Cleaning trusted APKs and tools forces redownloads.",
            ),
            self._handle_clean_selection,
        )

    def _handle_clean_selection(self, selected: tuple[str, ...] | None) -> None:
        if not selected:
            self._set_status("No cache areas selected")
            return
        self._start_cache_clean(selected)

    def _start_cache_clean(self, selected: tuple[str, ...]) -> None:
        if self._build_worker is not None and not self._build_worker.is_finished:
            self._set_status("Cannot clean cache while build is running")
            return
        if self._clean_worker is not None and not self._clean_worker.is_finished:
            self._set_status("Cache clean already running")
            return
        self._clean_worker = self.run_clean_worker(selected)
        self._set_status("Cache clean pending")

    @work(thread=True, exit_on_error=False, name="clean")
    def run_clean_worker(self, selected: tuple[str, ...]) -> dict[str, object]:
        return run_clean(self.args.cache, selected=selected)

    def action_list_patches(self) -> None:
        if self._splash_active:
            return
        if not self.dashboard_state.loaded:
            self._set_patch_status(
                "Patch list unavailable: configuration is not loaded"
            )
            return
        if self._patch_worker is not None and not self._patch_worker.is_finished:
            self._set_patch_status("Patch list already running")
            return
        self._patch_worker = self.list_patches()
        self._set_patch_status("Patch list: pending")

    @work(thread=True, exit_on_error=False, name="list-patches")
    def list_patches(self) -> dict[str, object]:
        config_path = self.dashboard_state.config_path
        if config_path is None:
            raise RuntimeError("configuration is not loaded")
        return run_list_patches(config_path, cache=self.args.cache)

    def action_load_community_bundles(self) -> None:
        if (
            self._community_worker is not None
            and not self._community_worker.is_finished
        ):
            self._set_bundle_status("Community bundles: loading")
            return
        self._set_bundle_status("Community bundles: loading")
        self._community_worker = self.load_community_bundles_worker()

    @work(thread=True, exit_on_error=False, name="community-bundles")
    def load_community_bundles_worker(self) -> dict[str, object]:
        return run_community_bundles()

    def _selected_community_bundle(
        self, repo: str | None = None
    ) -> Mapping[str, object] | None:
        table = self.query_one("#bundles-table", FullWidthDataTable)
        if not table.row_count:
            self._set_bundle_status("Select a community bundle first")
            return None
        row = repo or str(
            table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        )
        return next(
            (
                bundle
                for bundle in self._community_bundles or ()
                if isinstance(bundle, Mapping) and bundle.get("repo") == row
            ),
            None,
        )

    def action_show_bundle_apps(self, repo: str | None = None) -> None:
        if self._bundle_worker is not None and not self._bundle_worker.is_finished:
            self._set_bundle_status("Bundle source is loading")
            return
        bundle = self._selected_community_bundle(repo)
        if bundle is None:
            return
        provider = str(bundle.get("provider", "github"))
        source = str(bundle.get("repo", ""))
        if provider != "github":
            self._set_bundle_status("GitLab bundle sources are not supported yet")
            return
        version = str(bundle.get("version", "latest"))
        self._bundle_open_after_load = (source, version)
        self._set_bundle_status(f"Loading {source} {version}")
        self._bundle_worker = self.load_bundle_worker(source, version)

    def _show_loaded_bundle_apps(self, catalog: Mapping[str, object]) -> None:
        apps = catalog.get("apps", [])
        if not isinstance(apps, list):
            self._set_bundle_status("Bundle source returned no applications")
            return
        self._bundle_catalog = dict(catalog)
        self.push_screen(
            BundleAppsScreen(
                [app for app in apps if isinstance(app, Mapping)],
                {app.package: app for app in self.dashboard_state.apps},
                str(catalog.get("source", "")),
                self.dashboard_state.config_path is not None,
            ),
            self._handle_bundle_apps_result,
        )

    def _handle_bundle_apps_result(self, result: tuple[str, str] | None) -> None:
        if result is None:
            return
        action, package = result
        if action == "add":
            self.action_add_bundle_app(package)
        else:
            self.action_assign_bundle(package)

    def action_load_bundle(self) -> None:
        if self._bundle_worker is not None and not self._bundle_worker.is_finished:
            self._set_bundle_status("Bundle: loading")
            return
        source = self.query_one("#bundle-source", Input).value.strip()
        version = self.query_one("#bundle-version", Input).value.strip() or "latest"
        if not source:
            self._set_bundle_status("Bundle failed: source is required")
            return
        if any(
            isinstance(bundle, Mapping) and bundle.get("repo") == source
            for bundle in self._community_bundles or ()
        ):
            self._set_bundle_status(
                "Bundle already in community table; select it and use Show apps"
            )
            return
        self._bundle_open_after_load = None
        self._bundle_catalog = None
        self._render_bundle_catalog()
        self._set_bundle_status("Bundle: loading")
        self._bundle_worker = self.load_bundle_worker(source, version)

    @work(thread=True, exit_on_error=False, name="bundle-catalog")
    def load_bundle_worker(self, source: str, version: str) -> dict[str, object]:
        return run_bundle_catalog(source, version=version)

    def _bundle_app(self, package: str) -> Mapping[str, object] | None:
        apps = (self._bundle_catalog or {}).get("apps", [])
        if not isinstance(apps, list):
            return None
        return next(
            (
                app
                for app in apps
                if isinstance(app, Mapping) and app.get("package") == package
            ),
            None,
        )

    def action_add_bundle_app(self, package: str) -> None:
        app = self._bundle_app(package)
        path = self.dashboard_state.config_path
        catalog = self._bundle_catalog
        if app is None or path is None or not catalog:
            return
        if str(catalog.get("provider", "github")) != "github":
            self._set_bundle_status("GitLab bundle sources are not supported yet")
            return
        package = str(app["package"])
        if any(item.package == package for item in self.dashboard_state.apps):
            self._set_bundle_status(f"{package} already exists on dashboard")
            return
        try:
            add_app(
                path,
                {
                    "package": package,
                    "name": str(app.get("name", package)),
                    "version": "auto",
                    "build-mode": "both",
                    "arch": "both",
                    "patches-source": str(catalog["source"]),
                    "patches-version": str(catalog["version"]),
                },
            )
        except (ConfigError, OSError, TypeError, ValueError) as error:
            self._set_bundle_status(f"Cannot add application: {redact(str(error))}")
            return
        self._reload_configuration()
        self._render_bundle_catalog()
        self._set_bundle_status(
            f"Added {package}; choose local APK source when starting a build"
        )

    def action_assign_bundle(self, package: str) -> None:
        app = self._bundle_app(package)
        path = self.dashboard_state.config_path
        catalog = self._bundle_catalog
        if app is None or path is None or not catalog:
            return
        if str(catalog.get("provider", "github")) != "github":
            self._set_bundle_status("GitLab bundle sources are not supported yet")
            return
        package = str(app["package"])
        if not any(item.package == package for item in self.dashboard_state.apps):
            self._set_bundle_status(f"{package} is not on dashboard")
            return
        try:
            set_app_patch_source(
                path,
                package,
                str(catalog["source"]),
                str(catalog["version"]),
            )
        except (ConfigError, OSError, TypeError, ValueError) as error:
            self._set_bundle_status(f"Cannot assign bundle: {redact(str(error))}")
            return
        self._reload_configuration()
        self._render_bundle_catalog()
        self._set_bundle_status(f"Assigned bundle to {package}")

    def action_fetch_patch_catalog(self) -> None:
        path = self.dashboard_state.config_path
        value = self.query_one("#patch-app", Select).value
        if path is None or value is Select.BLANK:
            self._set_patch_status("Select a configured application first")
            return
        package = str(value)
        if self._catalog_worker is not None and not self._catalog_worker.is_finished:
            if self._catalog_package == package:
                self._set_patch_status("Patch catalog already loading")
                return
            self._catalog_worker.cancel()
        self._catalog_package = package
        self._catalog_patch_options = {}
        self._catalog_option_values = {}
        self.query_one("#patch-list", PatchSelectionList).set_patches(())
        self.query_one("#patch-options", Button).disabled = True
        self._catalog_worker = self.load_patch_catalog(package)
        self._set_patch_status("Patch catalog: pending")

    @work(thread=True, exit_on_error=False, name="patch-catalog")
    def load_patch_catalog(self, package: str) -> dict[str, object]:
        config_path = self.dashboard_state.config_path
        if config_path is None:
            raise RuntimeError("configuration is not loaded")
        return run_patch_catalog(config_path, cache=self.args.cache, package=package)

    def _highlighted_patch(self) -> str | None:
        item = self.query_one("#patch-list", PatchSelectionList).highlighted_child
        return item.patch if isinstance(item, PatchListItem) else None

    def _update_patch_options_button(self) -> None:
        name = self._highlighted_patch()
        selected = self.query_one("#patch-list", PatchSelectionList).selected
        options = self._catalog_patch_options.get(name or "", ())
        button = self.query_one("#patch-options", Button)
        button.label = f"⚙ Options ({len(options)})" if options else "⚙ No options"
        button.disabled = not (name is not None and name in selected and options)

    def action_edit_patch_options(self) -> None:
        name = self._highlighted_patch()
        if name is None:
            self._set_patch_status("Highlight a patch first")
            return
        if name not in self.query_one("#patch-list", PatchSelectionList).selected:
            self._set_patch_status("Select the patch before editing options")
            return
        options = self._catalog_patch_options.get(name, ())
        if not options:
            self._set_patch_status(f"{name} has no configurable options")
            return

        def saved(values: dict[str, PatchOptionValue] | None) -> None:
            if values is None:
                return
            self._catalog_option_values[name] = values
            self._set_patch_status(
                f"{name}: {len(values)} option override(s) pending save"
            )

        self.push_screen(
            PatchOptionsScreen(
                name,
                options,
                self._catalog_option_values.get(name, {}),
            ),
            saved,
        )

    def action_save_patch_selection(self) -> None:
        path = self.dashboard_state.config_path
        value = self.query_one("#patch-app", Select).value
        if path is None or value is Select.BLANK or str(value) != self._catalog_package:
            self._set_patch_status("Fetch selected application's catalog before saving")
            return
        selected = self.query_one("#patch-list", PatchSelectionList).selected
        try:
            set_exclusive_patches(
                path,
                str(value),
                [str(item) for item in selected],
                self._catalog_option_values,
            )
        except (ConfigError, OSError, TypeError, ValueError) as error:
            self._set_patch_status(f"Cannot save patches: {redact(str(error))}")
            return
        self._reload_configuration()
        self._set_patch_status(f"Saved {len(selected)} exclusive patch(es)")

    def _render_patch_catalog(self, result: Mapping[str, object]) -> None:
        patches = result.get("patches", [])
        selected = result.get("selected", [])
        configured_options = result.get("configured_options", {})
        selected_names = set(selected) if isinstance(selected, list) else set()
        selection_list = self.query_one("#patch-list", PatchSelectionList)
        self._catalog_patch_options = {}
        self._catalog_option_values = {}
        if isinstance(configured_options, Mapping):
            for patch, values in configured_options.items():
                if not isinstance(values, Mapping):
                    continue
                self._catalog_option_values[str(patch)] = {
                    str(key): value
                    for key, value in values.items()
                    if isinstance(value, (str, int, float, bool))
                }
        if not isinstance(patches, list):
            self._set_patch_status("Patch catalog failed: invalid result")
            return
        entries: list[PatchListEntry] = []
        for patch in patches:
            if not isinstance(patch, Mapping):
                continue
            name = str(patch.get("name", "unknown"))
            versions = patch.get("versions", ())
            raw_options = patch.get("options", ())
            options = (
                tuple(option for option in raw_options if isinstance(option, Mapping))
                if isinstance(raw_options, (list, tuple))
                else ()
            )
            if options:
                self._catalog_patch_options[name] = options
            entries.append(
                PatchListEntry(
                    name,
                    bool(patch.get("enabled")),
                    len(versions) if isinstance(versions, (list, tuple)) else 0,
                    options,
                    name in selected_names,
                )
            )
        selection_list.set_patches(entries)
        self._update_patch_options_button()
        self._set_patch_status(
            f"Patch catalog: {selection_list.option_count} patch(es) available"
        )

    @staticmethod
    def _describe(error: BaseException | None) -> str:
        """Redacted message for known project errors; type name otherwise.

        Project error messages are hand-written and safe to show. Anything
        else may wrap arbitrary subprocess/tool output, so only its type is
        shown even after redaction.
        """
        if error is not None and error_code(error) is not None:
            return redact(str(error))
        return type(error).__name__

    @staticmethod
    def _patch_error(error: BaseException | None) -> str:
        message = MasamuneApp._describe(error)
        if message == "downloaded SHA-256 mismatch for morphe-patches":
            return (
                f"{message}. Verify asset independently, then set "
                "patches-sha256 in morphe.toml."
            )
        return message

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker is self._build_worker:
            if event.state is WorkerState.RUNNING:
                self._set_build_status("RUNNING")
                self._set_status("Build running")
            elif event.state is WorkerState.SUCCESS:
                self.query_one("#stop-build", Button).disabled = True
                result = event.worker.result
                self.build_result = result if isinstance(result, dict) else None
                self._set_build_status("SUCCESS")
                self._finalize_build_progress()
                self._render_results()
                self._record_build_history("SUCCESS")
                if self.query_one("#content", ContentSwitcher).current == "builds":
                    self._render_build_history()
                self._set_status("Build completed")
            elif event.state is WorkerState.ERROR:
                self.query_one("#stop-build", Button).disabled = True
                if isinstance(event.worker.error, BuildCancelled):
                    self._set_build_status("CANCELLED")
                    self._set_build_error("Build cancelled by user")
                    self._record_build_history("CANCELLED")
                    self._set_status("Build cancelled")
                else:
                    self._set_build_status("FAILED")
                    self._set_build_error(
                        f"Build failed: {self._describe(event.worker.error)}"
                    )
                    self._record_build_history("FAILED")
                if self.query_one("#content", ContentSwitcher).current == "builds":
                    self._render_build_history()
            return
        if event.worker is self._download_version_worker:
            if event.state is WorkerState.RUNNING:
                self._set_download_status("Resolving patch-compatible versions")
            elif event.state is WorkerState.SUCCESS:
                result = event.worker.result
                if isinstance(result, Mapping):
                    self._render_download_versions(result)
                else:
                    self._set_download_status(
                        "Version resolution returned invalid data"
                    )
            elif event.state is WorkerState.ERROR:
                self._set_download_status(
                    f"Version resolution failed: {self._describe(event.worker.error)}"
                )
            return
        if event.worker is self._download_worker:
            if event.state is WorkerState.RUNNING:
                self._set_download_status("Downloading verified stock")
            elif event.state is WorkerState.SUCCESS:
                self.query_one("#stop-download", Button).disabled = True
                self.query_one("#open-download-folder", Button).disabled = False
                self._download_result = event.worker.result
                provider = getattr(event.worker.result, "provider", "unknown")
                self._set_download_status(
                    "Download verified. Select its folder when configuring the build. "
                    f"provider={redact(str(provider))}"
                )
            elif event.state is WorkerState.ERROR:
                self.query_one("#stop-download", Button).disabled = True
                if isinstance(event.worker.error, BuildCancelled):
                    self._set_download_status("Download cancelled")
                else:
                    self._set_download_status(
                        f"Download failed: {self._describe(event.worker.error)}"
                    )
            return
        if event.worker is self._community_worker:
            if event.state is WorkerState.RUNNING:
                self._set_bundle_status("Community bundles: loading")
            elif event.state is WorkerState.SUCCESS:
                result = event.worker.result
                bundles = result.get("bundles") if isinstance(result, dict) else None
                if isinstance(bundles, list):
                    self._community_bundles = [
                        bundle for bundle in bundles if isinstance(bundle, Mapping)
                    ]
                    self._render_community_bundles()
                    self._set_bundle_status(
                        f"Community bundles: {len(self._community_bundles)} loaded"
                    )
                else:
                    self._set_bundle_status("Community bundles failed: invalid result")
            elif event.state is WorkerState.ERROR:
                self._set_bundle_status(
                    f"Community bundles failed: {redact(str(event.worker.error))}"
                )
            return
        if event.worker is self._bundle_worker:
            if event.state is WorkerState.RUNNING:
                self._set_bundle_status("Bundle: loading")
            elif event.state is WorkerState.SUCCESS:
                result = event.worker.result
                if isinstance(result, dict):
                    source = str(result.get("source", ""))
                    version = str(result.get("version", "latest"))
                    apps = result.get("apps", [])
                    apps = apps if isinstance(apps, list) else []
                    if self._bundle_open_after_load is not None:
                        self._bundle_open_after_load = None
                        self._show_loaded_bundle_apps(result)
                        self._set_bundle_status(
                            f"Bundle {redact(source)} {redact(version)} · "
                            f"{len(apps)} app(s) loaded"
                        )
                        return
                    if not apps:
                        self._set_bundle_status(
                            f"Bundle {redact(source)} has no supported apps; not added"
                        )
                        return
                    entry: dict[str, object] = {
                        "provider": "github",
                        "repo": source,
                        "name": source,
                        "author": "Custom source",
                        "description": "Loaded from custom patch source",
                        "patch_count": sum(
                            value
                            for app in apps
                            if isinstance(app, Mapping)
                            for value in [app.get("patch_count", 0)]
                            if isinstance(value, int)
                        ),
                        "apps": apps,
                        "version": version,
                    }
                    existing = [
                        item
                        for item in self._community_bundles or ()
                        if not (
                            isinstance(item, Mapping)
                            and item.get("repo") == source
                            and item.get("version", "latest") == version
                        )
                    ]
                    self._community_bundles = [*existing, entry]
                    self._bundle_catalog = None
                    self._render_community_bundles()
                    self._set_bundle_status(
                        f"Bundle {redact(source)} {redact(version)} · "
                        f"{len(apps)} app(s) added to table"
                    )
                else:
                    self._set_bundle_status("Bundle failed: invalid result")
            elif event.state is WorkerState.ERROR:
                self._bundle_open_after_load = None
                self._set_bundle_status(
                    f"Bundle failed: {redact(str(event.worker.error))}"
                )
            return
        if event.worker is self._catalog_worker:
            if event.state is WorkerState.RUNNING:
                self._set_patch_status("Patch catalog: loading")
            elif event.state is WorkerState.SUCCESS:
                result = event.worker.result
                if isinstance(result, Mapping):
                    self._render_patch_catalog(result)
                else:
                    self._set_patch_status("Patch catalog failed: invalid result")
            elif event.state is WorkerState.ERROR:
                self._set_patch_status(
                    f"Patch catalog failed: {self._patch_error(event.worker.error)}"
                )
            return
        if event.worker is self._cache_worker:
            if event.state is WorkerState.RUNNING:
                if self._cache_inventory_status is not None:
                    self._set_status("Cache inventory loading")
            elif event.state is WorkerState.SUCCESS:
                inventory = event.worker.result
                self._render_cache(inventory)
                status = self._cache_inventory_status
                self._cache_inventory_status = None
                if status is not None:
                    self._set_status(status)
            elif event.state is WorkerState.ERROR:
                self._set_status(
                    f"Cache inventory failed: {type(event.worker.error).__name__}"
                )
            return
        if event.worker is self._clean_worker:
            if event.state is WorkerState.RUNNING:
                self._set_status("Cache clean running")
            elif event.state is WorkerState.SUCCESS:
                result = event.worker.result
                removed = result.get("removed", []) if isinstance(result, dict) else []
                self._start_cache_inventory(status=None)
                self._set_status(
                    f"Cache clean complete: {len(removed)} path(s) removed"
                    if removed
                    else "Cache clean complete: no disposable files found"
                )
            elif event.state is WorkerState.ERROR:
                self._set_status(
                    f"Cache clean failed: {type(event.worker.error).__name__}"
                )
            return
        if event.worker is not self._patch_worker:
            return
        if event.state is WorkerState.RUNNING:
            self._set_patch_status("Patch list: running")
        elif event.state is WorkerState.SUCCESS:
            result = event.worker.result
            apps = result.get("apps") if isinstance(result, dict) else None
            if isinstance(apps, list):
                self._render_patches(apps)
                self._set_patch_status(f"Patch list: loaded for {len(apps)} app(s)")
            else:
                self._set_patch_status("Patch list failed: invalid result")
        elif event.state is WorkerState.ERROR:
            self._set_patch_status(
                f"Patch list failed: {self._patch_error(event.worker.error)}"
            )

    def action_dark_theme(self) -> None:
        if not self._splash_active:
            self._set_theme("morphe-dark")

    def action_light_theme(self) -> None:
        if not self._splash_active:
            self._set_theme("morphe-light")

    def action_high_contrast_theme(self) -> None:
        if not self._splash_active:
            self._set_theme("morphe-high-contrast")

    def _set_theme(self, theme: str) -> None:
        self.theme = theme

    def _persist_theme(self, theme: str) -> None:
        if theme in _THEME_NAMES:
            self.preferences = Preferences(theme, self.preferences.keymap())
            self._save_preferences()

    def set_binding_overrides(self, bindings: Mapping[str, str]) -> None:
        overrides = validate_keybindings(bindings)
        self.preferences = Preferences(self.preferences.theme, overrides)
        self.set_keymap(overrides)
        self._save_preferences()

    def action_reset_preferences(self) -> None:
        if not self._splash_active:
            self.preferences = Preferences()
            self.theme = self.preferences.theme
            self.set_keymap({})
            self._save_preferences()
            self._set_status("TUI preferences reset")

    def _save_preferences(self) -> None:
        with suppress(OSError, TypeError, ValueError):
            save_preferences(self.preferences, self.preferences_path)

    async def action_quit(self) -> None:
        if not self._splash_active:
            self.exit()

    def get_system_commands(self, screen) -> Iterable[SystemCommand]:  # type: ignore[no-untyped-def]
        yield from super().get_system_commands(screen)
        for command in _COMMANDS:
            if command.action in {"toggle_sidebar", "show_dashboard", "show_downloads"}:
                yield SystemCommand(
                    command.label,
                    command.description,
                    getattr(self, f"action_{command.action}"),
                )
        yield SystemCommand(
            "Start build", "Review build before starting", self.action_start_build
        )
        yield SystemCommand(
            "Reset preferences",
            "Restore default theme and bindings",
            self.action_reset_preferences,
        )

    def _reload_configuration(self) -> None:
        self.dashboard_state = DashboardState(self.dashboard_state.config_path)
        self._load_dashboard()
        self._render_download_apps()

    def _load_dashboard(self) -> None:
        path = self.dashboard_state.config_path
        if path is None:
            self._render_dashboard()
            return
        try:
            config = load_config(path)
        except ConfigError as error:
            self.dashboard_state = DashboardState(path, error=redact(str(error)))
        else:
            self.dashboard_state = DashboardState(path, config.apps)
        self._render_dashboard()

    def _render_dashboard(self) -> None:
        state = self.dashboard_state
        if state.config_path is None:
            config_text = "Configuration: not loaded"
        elif state.error is not None:
            config_text = f"Configuration: {redact(str(state.config_path))}\nState: invalid — {state.error}"
        else:
            config_text = (
                f"Configuration: {redact(str(state.config_path))}\nState: loaded"
            )
        keystore = (
            "auto-generated per-user key (first build)"
            if self.args.keystore is None
            else redact(os.path.relpath(self.args.keystore))
        )
        paths_text = "\n".join(
            (
                f"Cache: {redact(str(self.args.cache))}",
                f"Output: {redact(str(self.args.output))}",
                f"Keystore: {keystore}",
            )
        )
        self.query_one("#config-status", Static).update(config_text)
        self.query_one("#paths", Static).update(paths_text)
        table = self.query_one("#apps", FullWidthDataTable)
        table.clear()
        for app in state.apps:
            table.add_row(
                _cell(app.name),
                _cell(app.package),
                _cell(app.arch),
                _cell(app.build_mode),
                _cell("enabled" if app.enabled else "disabled"),
                key=app.package,
            )
        table.display = bool(state.apps)
        self.call_after_refresh(table.fit_columns)
        selector = self.query_one("#patch-app", Select)
        current = selector.value
        selector.set_options(
            (Text(f"{app.name} · {app.package}"), app.package) for app in state.apps
        )
        packages = {app.package for app in state.apps}
        if current in packages:
            selector.value = current
        elif state.apps:
            selector.value = state.apps[0].package
        else:
            selector.value = Select.BLANK
        self._patch_selector_value = (
            None if selector.value is Select.BLANK else str(selector.value)
        )
        if self._catalog_package is not None and self._catalog_package not in packages:
            self._catalog_package = None
            self.query_one("#patch-list", PatchSelectionList).set_patches(())
        self.query_one("#header-config", Static).update(
            "No configuration loaded"
            if state.config_path is None
            else redact(str(state.config_path))
        )

    def _render_cache(
        self,
        inventory: list[tuple[str, str, str, bool, bool, int, int]] | None = None,
    ) -> None:
        cache = self.args.cache
        rows = (
            inventory
            if inventory is not None
            else _cache_inventory(cache, self.args.keystore)
        )
        self._cache_inventory = rows
        table = self.query_one("#cache-table", FullWidthDataTable)
        table.clear()
        total_files = 0
        total_size = 0
        disposable_files = 0
        disposable_size = 0
        for name, purpose, policy, count_in_total, exists, files, size in rows:
            if count_in_total:
                total_files += files
                total_size += size
            if policy == "disposable":
                disposable_files += files
                disposable_size += size
            display_name = name + (" (missing)" if not exists else "")
            table.add_row(
                _cell(display_name),
                _cell(purpose),
                _cell(files),
                _cell(_format_bytes(size)),
                _cell(policy),
                key=name,
            )
        table.display = True
        self.call_after_refresh(table.fit_columns)
        self.query_one("#cache-status", Static).update(
            "\n".join(
                (
                    f"Cache: {redact(str(cache))}",
                    f"Inventory: {total_files} file(s), {_format_bytes(total_size)}",
                    f"Disposable now: {disposable_files} file(s), "
                    f"{_format_bytes(disposable_size)}",
                )
            )
        )

    def _render_build_history(self) -> None:
        table = self.query_one("#builds-table", FullWidthDataTable)
        table.clear()
        occurrences: dict[str, int] = {}
        for record in self._builds_history:
            jobs = record.get("jobs")
            timestamp = str(record.get("timestamp", ""))
            occurrence = occurrences.get(timestamp, 0)
            occurrences[timestamp] = occurrence + 1
            row_key = timestamp if occurrence == 0 else f"{timestamp}#{occurrence}"
            table.add_row(
                _cell(timestamp),
                _cell(str(record.get("status", ""))),
                _cell(redact(str(record.get("output", "")))),
                _cell(
                    ", ".join(str(job) for job in jobs)
                    if isinstance(jobs, list)
                    else ""
                ),
                key=row_key,
            )
        table.display = bool(self._builds_history)
        self.call_after_refresh(table.fit_columns)

    def _record_build_history(self, status: str) -> None:
        result = self.build_result or {}
        raw_jobs = result.get("jobs", [])
        jobs = [
            f"{job.get('package', job.get('name', 'job'))} {job.get('architecture', '')}".strip()
            for job in (raw_jobs if isinstance(raw_jobs, list) else [])
            if isinstance(job, Mapping)
        ]
        self._builds_history = append_build_history(
            {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "status": status,
                "output": str(self.args.output),
                "jobs": jobs,
            },
            self.preferences_path.parent / "builds.json",
        )

    def _set_panel(self, identifier: str, text: str) -> None:
        panel = self.query_one(identifier, Static)
        panel.update(redact(text))
        panel.display = bool(text)

    def _render_results(self) -> None:
        result = self.build_result or {}
        jobs = result.get("jobs", [])
        skipped = result.get("skipped", [])
        lines = [f"Output: {redact(str(self.args.output))}"]
        if isinstance(jobs, list):
            for job in jobs:
                if isinstance(job, Mapping):
                    artifacts = job.get("artifacts", [])
                    lines.append(
                        "Completed: "
                        f"{redact(str(job.get('name') or job.get('package', 'job')))} "
                        f"{redact(str(job.get('architecture', '')))} "
                        f"Artifacts: {', '.join(redact(str(item)) for item in artifacts) if isinstance(artifacts, (list, tuple)) else ''}"
                    )
        if isinstance(skipped, list):
            for job in skipped:
                if isinstance(job, Mapping):
                    lines.append(
                        f"Skipped: {redact(str(job.get('name') or job.get('package', 'job')))} — "
                        f"{redact(str(job.get('reason', 'unavailable')))}"
                    )
        self._set_panel("#results", "\n".join(lines))

    def _finalize_build_progress(self) -> None:
        result = self.build_result or {}
        for jobs, state in (
            (result.get("jobs", []), "SUCCESS"),
            (result.get("skipped", []), "SKIPPED"),
        ):
            if not isinstance(jobs, list):
                continue
            for job in jobs:
                if not isinstance(job, Mapping):
                    continue
                package = job.get("package")
                architecture = job.get("architecture")
                if (
                    isinstance(package, str)
                    and package
                    and isinstance(architecture, str)
                    and architecture
                ):
                    self.build_progress[self._build_job_key(package, architecture)] = (
                        state
                    )
        self._render_build_progress()

    def _set_status(self, message: str) -> None:
        self.query_one("#status", Static).update(redact(message))

    def _set_build_status(self, state: str) -> None:
        self.build_state = state
        self.query_one("#build-status", Static).update(f"Build: {state}")

    def _set_build_stage(self, stage: str) -> None:
        self.build_stage = redact(stage)
        self.query_one("#build-stage", Static).update(
            f"Build stage: {self.build_stage}"
        )

    def _set_build_error(self, message: str) -> None:
        self._set_panel("#build-error", message)

    def _render_build_events(self) -> None:
        scroll = self.query_one("#build-events-scroll", VerticalScroll)
        follow_tail = scroll.scroll_y >= scroll.max_scroll_y
        self.query_one("#build-events", Static).update(
            "\n".join(self.build_events) or "No build events."
        )
        if follow_tail:
            scroll.scroll_end(animate=False)

    def _render_build_progress(self) -> None:
        table = self.query_one("#build-jobs", FullWidthDataTable)
        table.clear()
        for job, stage in self.build_progress.items():
            table.add_row(_cell(job), _cell(stage), key=job)
        self.call_after_refresh(table.fit_columns)

    def _render_community_bundles(self) -> None:
        table = self.query_one("#bundles-table", FullWidthDataTable)
        table.clear()
        search = self.query_one("#bundle-search", Input).value.strip().lower()
        bundles = self._community_bundles or []
        for bundle in bundles:
            if not isinstance(bundle, Mapping):
                continue
            haystack = " ".join(
                str(bundle.get(key, ""))
                for key in ("name", "author", "repo", "description")
            ).lower()
            if search and search not in haystack:
                continue
            table.add_row(
                _cell(bundle.get("name", bundle.get("repo", ""))),
                _cell(bundle.get("author", "")),
                _cell(f"{bundle.get('provider', 'github')}/{bundle.get('repo', '')}"),
                _cell(bundle.get("patch_count", 0)),
                key=str(bundle.get("repo", "")),
            )
        table.display = bool(table.row_count)
        self.query_one("#show-bundle-apps", Button).disabled = not bool(table.row_count)
        self.call_after_refresh(table.fit_columns)

    def _set_bundle_status(self, message: str) -> None:
        try:
            self.query_one("#bundle-status", Static).update(redact(message))
        except NoMatches:
            return

    def _render_bundle_catalog(self) -> None:
        apps = (self._bundle_catalog or {}).get("apps", [])
        self.query_one("#show-bundle-apps", Button).disabled = not bool(
            isinstance(apps, list) and apps
        )

    def _set_patch_status(self, message: str) -> None:
        try:
            self.query_one("#patch-status", Static).update(redact(message))
        except NoMatches:
            # Worker can finish after a modal replaces patch view.
            return

    def _render_patches(self, apps: list[object]) -> None:
        entries = [app for app in apps if isinstance(app, Mapping)]
        table = self.query_one("#patch-table", FullWidthDataTable)
        table.clear()
        for app in entries:
            patches = app.get("selected_patches", ())
            package = str(app.get("package", "unknown"))
            table.add_row(
                _cell(package),
                _cell(app.get("selected_version", "unknown")),
                _cell(len(patches) if isinstance(patches, (list, tuple)) else 0),
                key=package,
            )
        table.display = bool(entries)
        self.call_after_refresh(table.fit_columns)


def run_tui(args: argparse.Namespace) -> None:
    MasamuneApp(args).run()
