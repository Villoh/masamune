import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

try:
    from masamune.tui import (
        _EYE_SCHEDULE_LENGTH,
        MasamuneApp,
        Preferences,
        append_build_history,
        load_build_history,
        load_preferences,
        save_preferences,
        validate_keybindings,
    )
except ModuleNotFoundError as error:
    if error.name != "textual":
        raise
    MasamuneApp: Any = None
    Preferences: Any = None
    append_build_history: Any = None
    load_build_history: Any = None
    load_preferences: Any = None
    save_preferences: Any = None
    validate_keybindings: Any = None
    _EYE_SCHEDULE_LENGTH: Any = None

from masamune.cli import parser
from masamune.config import ConfigError, load_config
from masamune.errors import IntegrityMetadataError
from masamune.logo import LOGO_SMALL_FRAMES, LOGO_SPLASH_FRAMES
from masamune.orchestrator import BuildResult, _summary
from masamune.toolchain import ToolchainError

_CONFIG = """
[[apps]]
package = "com.example.app"
name = "Example"
build-mode = "both"
arch = "arm64-v8a"
source-dir = "inputs/{arch}"
"""


def tui_app(config: Path | None = None):
    if MasamuneApp is None:
        raise AssertionError("Textual extra not installed")
    args = parser().parse_args(["tui"])
    args.config = config
    args.cache = Path("cache")
    args.output = Path("output")
    args.keystore = Path("keystore.p12")
    return MasamuneApp(args)


def content(app, identifier: str) -> str:
    return str(app.query_one(identifier).content)


async def confirm_build(pilot, app, *, accept: bool = True) -> None:
    await pilot.pause(0.1)
    app.action_start_build()
    await pilot.pause(0.1)
    await pilot.click("#yes" if accept else "#no")
    await pilot.pause()


def rows(app, identifier: str) -> list[tuple[str, ...]]:
    table = app.query_one(identifier)
    return [
        tuple(str(cell) for cell in table.get_row_at(index))
        for index in range(table.row_count)
    ]


@unittest.skipUnless(MasamuneApp, "Textual extra not installed")
class TuiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.preferences_directory = tempfile.TemporaryDirectory()
        self.preferences_patch = patch(
            "masamune.tui.app.preference_path",
            return_value=Path(self.preferences_directory.name) / "tui.json",
        )
        self.preferences_patch.start()

    def tearDown(self) -> None:
        self.preferences_patch.stop()
        self.preferences_directory.cleanup()

    def test_build_job_key_normalizes_arch_aliases(self) -> None:
        self.assertEqual(
            MasamuneApp._build_job_key("com.example.app", "arm64"),
            "com.example.app arm64-v8a",
        )
        self.assertEqual(
            MasamuneApp._build_job_key("com.example.app", "armv7"),
            "com.example.app arm-v7a",
        )

    async def test_splash_skips_and_sidebar_collapses(self) -> None:
        app = tui_app(Path("morphe.toml"))
        async with app.run_test() as pilot:
            self.assertTrue(app.query("#splash"))
            self.assertEqual(app.theme, "morphe-dark")
            self.assertTrue(
                {"morphe-dark", "morphe-light", "morphe-high-contrast"}
                <= set(app.available_themes)
            )
            await pilot.press("x")
            self.assertFalse(app.query("#splash"))
            await pilot.press("ctrl+b")
            self.assertTrue(app.sidebar_collapsed)
            self.assertTrue(app.has_class("sidebar-collapsed"))
            self.assertTrue(app.query_one("#sidebar-rail").display)

    async def test_splash_blocks_actions_and_palette_works_afterwards(self) -> None:
        app = tui_app(Path("morphe.toml"))
        async with app.run_test() as pilot:
            app._splash_active = True
            app.action_toggle_sidebar()
            self.assertFalse(app.sidebar_collapsed)
            app.dismiss_splash()
            commands = {
                command.title for command in app.get_system_commands(app.screen)
            }
            self.assertIn("Theme", commands)
            self.assertIn("Keys", commands)
            self.assertIn("Start build", commands)
            for unnecessary in (
                "Build",
                "Patches",
                "Cache",
                "Confirm build",
                "Cancel build",
                "Clean cache",
                "Morphe dark theme",
                "Morphe light theme",
                "High contrast theme",
            ):
                self.assertNotIn(unnecessary, commands)
            await pilot.press("ctrl+p")
            self.assertEqual(type(app.screen).__name__, "CommandPalette")
            await pilot.press("escape")
            await pilot.press("t")
            self.assertEqual(type(app.screen).__name__, "CommandPalette")

    async def test_right_click_opens_context_menu_and_edits_toml(self) -> None:
        from textual.events import MouseDown  # pyright: ignore[reportMissingImports]

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "morphe.toml"
            config.write_text(_CONFIG, encoding="utf-8")
            app = tui_app(config)
            async with app.run_test(size=(120, 35)) as pilot:
                await pilot.press("x")
                table = app.query_one("#apps")
                await pilot.hover("#apps", offset=(3, 1))
                table.on_mouse_down(
                    MouseDown(table, 3, 1, 0, 0, 3, False, False, False)
                )
                await pilot.pause()
                self.assertEqual(type(app.screen).__name__, "ContextMenuScreen")
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(type(app.screen).__name__, "AppEditorScreen")
                app.screen.query_one("#edit-name").value = "Edited"
                app.screen.query_one("#edit-enabled").value = False
                app.screen.query_one(
                    "#edit-fallback-direct"
                ).value = "https://downloads.example.invalid/app.apk"
                app.screen.query_one("#edit-patches-version").value = "v1.0.0"
                app.screen.query_one("#edit-patches-sha256").value = "a" * 64
                app.screen.query_one("#save-app").focus()
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(type(app.screen).__name__, "Screen")
                saved = load_config(config).apps[0]
                self.assertEqual(saved.name, "Edited")
                self.assertFalse(saved.enabled)
                self.assertEqual(saved.patches_version, "v1.0.0")
                self.assertEqual(saved.patches_sha256, "a" * 64)
                self.assertEqual(
                    saved.fallbacks.direct,
                    ("https://downloads.example.invalid/app.apk",),
                )
                self.assertEqual(rows(app, "#apps")[0][0], "Edited")

    async def test_context_menu_title_text_is_actually_visible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "morphe.toml"
            config.write_text(_CONFIG, encoding="utf-8")
            app = tui_app(config)
            async with app.run_test(size=(120, 35)) as pilot:
                await pilot.press("x")
                await pilot.click("#apps", offset=(3, 1))
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(type(app.screen).__name__, "ContextMenuScreen")
                svg = app.export_screenshot()
            self.assertIn("com.example.app", svg)

    async def test_context_menu_closes_when_clicking_outside(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "morphe.toml"
            config.write_text(_CONFIG, encoding="utf-8")
            app = tui_app(config)
            async with app.run_test(size=(120, 35)) as pilot:
                await pilot.press("x")
                await pilot.click("#apps", offset=(3, 1))
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(type(app.screen).__name__, "ContextMenuScreen")
                await pilot.click(offset=(0, 0))
                await pilot.pause()
                self.assertEqual(type(app.screen).__name__, "Screen")

    async def test_app_actions_are_left_aligned_not_centered(self) -> None:
        app = tui_app(Path("morphe.toml"))
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.press("x")
            container = app.query_one("#app-actions")
            self.assertEqual(container.styles.align_horizontal, "left")
            self.assertEqual(app.query_one("#add-app").region.x, container.region.x)

    async def test_left_click_only_selects_and_enter_opens_context_menu(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "morphe.toml"
            config.write_text(_CONFIG, encoding="utf-8")
            app = tui_app(config)
            async with app.run_test(size=(120, 35)) as pilot:
                await pilot.press("x")
                await pilot.click("#apps", offset=(3, 1))
                self.assertEqual(type(app.screen).__name__, "Screen")
                self.assertEqual(app.query_one("#apps").cursor_row, 0)
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(type(app.screen).__name__, "ContextMenuScreen")

    async def test_app_editor_stretches_actions_and_shrinks_to_content(self) -> None:
        app = tui_app(Path("morphe.toml"))
        async with app.run_test(size=(120, 80)) as pilot:
            await pilot.press("x")
            await pilot.click("#add-app")
            await pilot.pause()
            editor = app.screen.query_one("#app-editor")
            actions = app.screen.query_one(".modal-actions")
            save = app.screen.query_one("#save-app")
            cancel = app.screen.query_one("#cancel-app")
            self.assertIsNotNone(app.screen.query_one("#edit-patches-sha256"))
            self.assertLess(editor.outer_size.height, app.screen.size.height * 0.9)
            self.assertLessEqual(editor.region.bottom - actions.region.bottom, 2)
            self.assertEqual(save.outer_size.width, cancel.outer_size.width)

    async def test_app_table_is_selectable_and_fills_viewport(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "morphe.toml"
            config.write_text(_CONFIG, encoding="utf-8")
            app = tui_app(config)
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.press("x")
                await pilot.pause()
                table = app.query_one("#apps")
                self.assertTrue(table.can_focus)
                self.assertGreater(table.size.width, 0)
                self.assertEqual(
                    sum(
                        column.get_render_width(table)
                        for column in table.columns.values()
                    ),
                    table.size.width,
                )
                table.focus()
                await pilot.press("down")
                self.assertEqual(table.cursor_row, 0)

    async def test_sidebar_click_and_keys_switch_views(self) -> None:
        app = tui_app(Path("morphe.toml"))
        async with app.run_test() as pilot:
            await pilot.press("x")
            switcher = app.query_one("#content")
            self.assertEqual(switcher.current, "dashboard")
            await pilot.press("5")
            self.assertEqual(switcher.current, "build")
            self.assertEqual(app.query_one("#sidebar-items").index, 4)
            await pilot.press("6")
            self.assertEqual(switcher.current, "builds")
            await pilot.click("#nav-cache")
            self.assertEqual(switcher.current, "cache")
            self.assertIn("Cache: cache", content(app, "#cache-status"))
            self.assertFalse(app.query_one("#dashboard").display)

    async def test_empty_panels_stay_hidden_and_help_panel_toggles(self) -> None:
        app = tui_app(Path("morphe.toml"))
        async with app.run_test() as pilot:
            await pilot.press("x")
            for identifier in ("#build-error", "#results"):
                self.assertFalse(app.query_one(identifier).display)
            self.assertFalse(app.screen.query("HelpPanel"))
            await pilot.press("?")
            await pilot.pause()
            self.assertTrue(app.screen.query("HelpPanel"))
            await pilot.press("?")
            await pilot.pause()
            self.assertFalse(app.screen.query("HelpPanel"))

    async def test_narrow_terminal_uses_compact_sidebar_rail(self) -> None:
        app = tui_app(Path("morphe.toml"))
        async with app.run_test(size=(30, 20)):
            self.assertTrue(app.has_class("compact-rail"))
            self.assertTrue(app.query_one("#sidebar-rail").display)
            self.assertEqual(app.query_one("#sidebar").outer_size.width, 7)

    async def test_splash_animates_times_out_and_reduced_motion_is_short(self) -> None:
        app = tui_app(Path("morphe.toml"))
        async with app.run_test() as pilot:
            self.assertEqual(
                app._splash_content(LOGO_SPLASH_FRAMES[app._splash_frame]),
                content(app, "#splash"),
            )
            self.assertFalse(app.screen.show_horizontal_scrollbar)
            self.assertFalse(app.screen.show_vertical_scrollbar)
            await pilot.pause(0.2)
            self.assertGreater(app._splash_frame, 0)
            await pilot.pause(1.1)
            self.assertFalse(app.query("#splash"))

        reduced = tui_app(Path("morphe.toml"))
        reduced.animation_level = "none"
        async with reduced.run_test() as pilot:
            await pilot.pause(0.1)
            self.assertFalse(reduced.query("#splash"))

    async def test_header_logo_blinks_on_schedule_then_reopens(self) -> None:
        app = tui_app(Path("morphe.toml"))
        async with app.run_test():
            self.assertEqual(content(app, "#header-logo"), LOGO_SMALL_FRAMES[0])
            app._eye_tick = _EYE_SCHEDULE_LENGTH - 1
            app._advance_eyes()
            self.assertEqual(content(app, "#header-logo"), LOGO_SMALL_FRAMES[1])
            app._advance_eyes()
            self.assertEqual(content(app, "#header-logo"), LOGO_SMALL_FRAMES[0])

    def test_splash_content_centers_logo_and_title_on_same_column(self) -> None:
        rendered = MasamuneApp._splash_content(LOGO_SPLASH_FRAMES[-1])
        centers = {
            (len(line) - len(line.lstrip(" "))) + len(line.strip()) / 2
            for line in rendered.split("\n")
            if line.strip()
        }
        self.assertEqual(len(centers), 1)

    async def test_loaded_config_renders_apps_and_display_only_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "morphe.toml"
            config.write_text(_CONFIG, encoding="utf-8")
            app = tui_app(config)
            async with app.run_test():
                self.assertTrue(app.dashboard_state.loaded)
                self.assertEqual(content(app, "#header-logo"), LOGO_SMALL_FRAMES[0])
                self.assertEqual(content(app, "#header-config"), str(config))
                self.assertIn("▦", content(app, "#nav-dashboard Label"))
                self.assertIn("≡", content(app, "#nav-bundles Label"))
                self.assertIn("≡", content(app, "#nav-patches Label"))
                self.assertNotIn("☷", content(app, "#nav-bundles Label"))
                self.assertIn("State: loaded", content(app, "#config-status"))
                self.assertEqual(
                    rows(app, "#apps"),
                    [("Example", "com.example.app", "arm64-v8a", "both", "enabled")],
                )
                paths = content(app, "#paths")
                self.assertIn("Cache: cache", paths)
                self.assertIn("Output: output", paths)
                self.assertIn("Keystore: keystore.p12", paths)

    async def test_app_name_with_rich_markup_renders_literal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "morphe.toml"
            config.write_text(
                _CONFIG.replace('name = "Example"', 'name = "[/]"'), encoding="utf-8"
            )
            app = tui_app(config)
            async with app.run_test():
                self.assertEqual(rows(app, "#apps")[0][0], "[/]")

    async def test_invalid_config_error_is_redacted(self) -> None:
        app = tui_app(Path("invalid.toml"))
        with patch(
            "masamune.tui.app.load_config",
            side_effect=ConfigError("token=secret"),
        ):
            async with app.run_test():
                status = content(app, "#config-status")
                self.assertIn("State: invalid", status)
                self.assertIn("token=<redacted>", status)
                self.assertNotIn("secret", status)
                self.assertFalse(app.dashboard_state.loaded)

    async def test_missing_config_stays_not_loaded(self) -> None:
        app = tui_app()
        async with app.run_test():
            self.assertEqual(
                content(app, "#config-status"), "Configuration: not loaded"
            )
            self.assertEqual(rows(app, "#apps"), [])
            self.assertFalse(app.query_one("#apps").display)
            self.assertFalse(app.dashboard_state.loaded)

    async def test_list_patches_runs_in_thread_and_reports_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "morphe.toml"
            config.write_text(_CONFIG, encoding="utf-8")
            app = tui_app(config)
            with patch(
                "masamune.tui.app.run_list_patches", return_value={"apps": [{}]}
            ) as list_patches:
                async with app.run_test() as pilot:
                    await pilot.press("x")
                    app.action_list_patches()
                    worker = app._patch_worker
                    self.assertIsNotNone(worker)
                    assert worker is not None
                    self.assertTrue(worker._thread_worker)
                    await worker.wait()
                    await pilot.pause()
                    list_patches.assert_called_once_with(config, cache=Path("cache"))
                    self.assertEqual(
                        content(app, "#patch-status"), "Patch list: loaded for 1 app(s)"
                    )

    async def test_patch_catalog_lists_individual_patches_and_saves_exclusive(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "morphe.toml"
            config.write_text(_CONFIG, encoding="utf-8")
            app = tui_app(config)
            catalog = {
                "package": "com.example.app",
                "selected": ["Hide ads"],
                "configured_options": {"Theme": {"color": "#000000"}},
                "patches": [
                    {
                        "name": "Hide ads",
                        "enabled": True,
                        "versions": ["1"],
                        "options": [],
                    },
                    {
                        "name": "Theme",
                        "enabled": False,
                        "versions": ["1"],
                        "options": [
                            {
                                "title": "Color",
                                "description": "Theme color.",
                                "required": False,
                                "key": "color",
                                "default": "#FFFFFF",
                                "values": ["#000000", "#FFFFFF"],
                                "type": "kotlin.String",
                            },
                            {
                                "title": "Enabled",
                                "description": "Toggle theme.",
                                "required": False,
                                "key": "enabled",
                                "default": False,
                                "values": [],
                                "type": "kotlin.Boolean",
                            },
                        ],
                    },
                ],
            }
            with patch(
                "masamune.tui.app.run_patch_catalog", return_value=catalog
            ) as get_catalog:
                async with app.run_test(size=(120, 35)) as pilot:
                    await pilot.press("x")
                    app._show_app_patches("com.example.app")
                    worker = app._catalog_worker
                    self.assertIsNotNone(worker)
                    assert worker is not None
                    await worker.wait()
                    await pilot.pause()
                    get_catalog.assert_called_once_with(
                        config, cache=Path("cache"), package="com.example.app"
                    )
                    patches = app.query_one("#patch-list")
                    self.assertEqual(patches.option_count, 2)
                    self.assertEqual(patches.selected, ["Hide ads"])
                    option_details = patches.query(".patch-option-summary")
                    self.assertEqual(len(option_details), 2)
                    self.assertIn("Color", str(option_details.first().content))
                    self.assertNotIn(
                        "[OPTIONS", str(patches.query(".patch-summary").last().content)
                    )
                    patches.select("Theme")
                    patches.highlighted = 1
                    await pilot.pause()
                    self.assertFalse(app.query_one("#patch-options").disabled)
                    await pilot.click("#patch-options")
                    await pilot.pause()
                    self.assertEqual(type(app.screen).__name__, "PatchOptionsScreen")
                    app.screen.query_one("#patch-option-0").value = "#123456"
                    app.screen.query_one("#patch-option-1").value = True
                    await pilot.click("#save-patch-options")
                    await pilot.pause()
                    await pilot.click("#save-patches")
                    await pilot.pause()
                    saved = load_config(config).apps[0]
                    self.assertEqual(
                        saved.exclusive_patches,
                        ("Hide ads", "Theme"),
                    )
                    self.assertEqual(saved.patch_options["Theme"]["color"], "#123456")
                    self.assertIs(saved.patch_options["Theme"]["enabled"], True)
                    self.assertIn("Saved 2", content(app, "#patch-status"))

    async def test_community_bundles_load_by_default_and_show_apps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "morphe.toml"
            config.write_text(_CONFIG, encoding="utf-8")
            community = {
                "bundles": [
                    {
                        "provider": "github",
                        "repo": "owner/community",
                        "name": "Community",
                        "author": "Author",
                        "description": "Example bundle",
                        "patch_count": 4,
                        "apps": [
                            {
                                "package": "com.example.app",
                                "name": "Example",
                                "versions": ["1.0"],
                                "patch_count": 2,
                            }
                        ],
                    }
                ]
            }
            source = {
                "source": "owner/community",
                "version": "latest",
                "apps": community["bundles"][0]["apps"],
            }
            with (
                patch(
                    "masamune.tui.app.run_community_bundles",
                    return_value=community,
                ) as load_bundles,
                patch(
                    "masamune.tui.app.run_bundle_catalog", return_value=source
                ) as load_source,
            ):
                app = tui_app(config)
                async with app.run_test(size=(140, 40)) as pilot:
                    await pilot.press("x")
                    app.action_show_bundles()
                    worker = app._community_worker
                    self.assertIsNotNone(worker)
                    assert worker is not None
                    await worker.wait()
                    await pilot.pause()
                    load_bundles.assert_called_once_with()
                    table = app.query_one("#bundles-table")
                    self.assertEqual(table.row_count, 1)
                    table.move_cursor(row=0)
                    await pilot.click("#show-bundle-apps")
                    bundle_worker = app._bundle_worker
                    self.assertIsNotNone(bundle_worker)
                    assert bundle_worker is not None
                    await bundle_worker.wait()
                    await pilot.pause()
                    load_source.assert_called_once_with(
                        "owner/community", version="latest"
                    )
                    self.assertEqual(
                        app.screen.query_one("#bundle-app-dialog-table").row_count,
                        1,
                    )
                    await pilot.press("escape")
                    await pilot.pause()
                    app._open_bundle_context_menu("owner/community", (4, 4))
                    await pilot.pause()
                    await pilot.press("enter")
                    await pilot.pause()
                    bundle_worker = app._bundle_worker
                    self.assertIsNotNone(bundle_worker)
                    assert bundle_worker is not None
                    await bundle_worker.wait()
                    await pilot.pause()
                    self.assertEqual(
                        app.screen.query_one("#bundle-app-dialog-table").row_count,
                        1,
                    )

    async def test_bundle_catalog_can_add_and_assign_apps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "morphe.toml"
            config.write_text(_CONFIG, encoding="utf-8")
            catalog = {
                "source": "owner/community-patches",
                "version": "v2.0.0",
                "apps": [
                    {
                        "package": "com.example.new",
                        "name": "New App",
                        "versions": ["2.0"],
                        "patch_count": 3,
                    },
                    {
                        "package": "com.example.app",
                        "name": "Example",
                        "versions": ["1.0"],
                        "patch_count": 2,
                    },
                ],
            }
            with patch(
                "masamune.tui.app.run_bundle_catalog", return_value=catalog
            ) as load_bundle:
                app = tui_app(config)
                async with app.run_test(size=(140, 40)) as pilot:
                    await pilot.press("x")
                    app.show_view("bundles")
                    app.action_load_bundle()
                    worker = app._bundle_worker
                    self.assertIsNotNone(worker)
                    assert worker is not None
                    await worker.wait()
                    await pilot.pause()
                    load_bundle.assert_called_once_with(
                        "MorpheApp/morphe-patches", version="latest"
                    )
                    self.assertEqual(app.query_one("#bundles-table").row_count, 1)
                    app.action_show_bundle_apps()
                    await pilot.pause()
                    self.assertEqual(
                        app.screen.query_one("#bundle-app-dialog-table").row_count,
                        2,
                    )
                    app.action_add_bundle_app("com.example.new")
                    await pilot.pause()
                    added = load_config(config).apps[1]
                    self.assertEqual(added.package, "com.example.new")
                    self.assertEqual(added.arch, "both")
                    self.assertEqual(added.build_mode, "both")
                    self.assertEqual(added.patches_source, "owner/community-patches")
                    self.assertEqual(added.patches_version, "v2.0.0")
                    app.action_assign_bundle("com.example.app")
                    await pilot.pause()
                    assigned = load_config(config).apps[0]
                    self.assertEqual(assigned.patches_source, "owner/community-patches")
                    self.assertEqual(assigned.patches_version, "v2.0.0")

    async def test_patch_label_highlights_and_checkbox_toggles(self) -> None:
        app = tui_app(Path("morphe.toml"))
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.press("x")
            app.show_view("patches")
            app._render_patch_catalog(
                {
                    "selected": ["Theme"],
                    "configured_options": {},
                    "patches": [
                        {
                            "name": "Theme",
                            "enabled": True,
                            "versions": ["1"],
                            "options": [{"key": "color"}],
                        }
                    ],
                }
            )
            patches = app.query_one("#patch-list")
            await pilot.pause()
            await pilot.click("#patch-list", offset=(10, 1))
            await pilot.pause()
            self.assertEqual(patches.selected, ["Theme"])
            self.assertEqual(patches.highlighted, 0)
            self.assertIn("Options (1)", str(app.query_one("#patch-options").label))
            self.assertFalse(app.query_one("#patch-options").disabled)
            await pilot.click("#patch-list", offset=(2, 1))
            await pilot.pause()
            self.assertEqual(patches.selected, [])

    async def test_patch_context_menu_opens_options(self) -> None:
        from textual.events import MouseDown  # pyright: ignore[reportMissingImports]

        app = tui_app(Path("morphe.toml"))
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.press("x")
            app.show_view("patches")
            app._render_patch_catalog(
                {
                    "selected": ["Theme"],
                    "configured_options": {},
                    "patches": [
                        {
                            "name": "Theme",
                            "enabled": True,
                            "versions": ["1"],
                            "options": [
                                {
                                    "title": "Color",
                                    "description": "Theme color.",
                                    "required": False,
                                    "key": "color",
                                    "default": "#000000",
                                    "values": [],
                                    "type": "kotlin.String",
                                }
                            ],
                        }
                    ],
                }
            )
            patches = app.query_one("#patch-list")
            await pilot.pause()
            item = patches.children[0]
            await pilot.hover(item, offset=(10, 0))
            item.on_mouse_down(MouseDown(item, 10, 0, 0, 0, 3, False, False, False))
            await pilot.pause()
            self.assertEqual(type(app.screen).__name__, "PatchContextMenuScreen")
            await pilot.press("down", "enter")
            await pilot.pause()
            self.assertEqual(type(app.screen).__name__, "PatchOptionsScreen")
            await pilot.press("escape")
            patches.focus()
            patches.highlighted = 0
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(type(app.screen).__name__, "PatchContextMenuScreen")

    async def test_entering_patches_loads_first_app_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "morphe.toml"
            config.write_text(_CONFIG, encoding="utf-8")
            app = tui_app(config)
            with (
                patch(
                    "masamune.tui.app.run_list_patches", return_value={"apps": []}
                ),
                patch(
                    "masamune.tui.app.run_patch_catalog",
                    return_value={"selected": [], "patches": []},
                ) as get_catalog,
            ):
                async with app.run_test() as pilot:
                    await pilot.press("x")
                    app.action_show_patches()
                    worker = app._catalog_worker
                    self.assertIsNotNone(worker)
                    assert worker is not None
                    await worker.wait()
                    await pilot.pause()
                    get_catalog.assert_called_once_with(
                        config, cache=Path("cache"), package="com.example.app"
                    )

    async def test_changing_patch_app_refreshes_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "morphe.toml"
            config.write_text(
                _CONFIG + '\n[[apps]]\npackage = "com.example.two"\nname = "Two"\n',
                encoding="utf-8",
            )
            app = tui_app(config)
            catalog = {
                "package": "com.example.two",
                "selected": [],
                "configured_options": {},
                "patches": [],
            }
            with patch(
                "masamune.tui.app.run_patch_catalog", return_value=catalog
            ) as get_catalog:
                async with app.run_test() as pilot:
                    await pilot.press("x")
                    app.show_view("patches")
                    app.query_one("#patch-app").value = "com.example.two"
                    await pilot.pause()
                    worker = app._catalog_worker
                    self.assertIsNotNone(worker)
                    assert worker is not None
                    await worker.wait()
                    get_catalog.assert_called_once_with(
                        config, cache=Path("cache"), package="com.example.two"
                    )

    async def test_patch_checksum_error_explains_config_override(self) -> None:
        from textual.worker import WorkerFailed  # pyright: ignore[reportMissingImports]

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "morphe.toml"
            config.write_text(_CONFIG, encoding="utf-8")
            app = tui_app(config)
            with patch(
                "masamune.tui.app.run_patch_catalog",
                side_effect=ToolchainError(
                    "downloaded SHA-256 mismatch for morphe-patches"
                ),
            ):
                async with app.run_test() as pilot:
                    await pilot.press("x")
                    app.show_view("patches")
                    app.action_fetch_patch_catalog()
                    worker = app._catalog_worker
                    self.assertIsNotNone(worker)
                    assert worker is not None
                    with self.assertRaises(WorkerFailed):
                        await worker.wait()
                    await pilot.pause()
                    self.assertIn("patches-sha256", content(app, "#patch-status"))

    async def test_list_patches_reports_redacted_worker_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "morphe.toml"
            config.write_text(_CONFIG, encoding="utf-8")
            app = tui_app(config)
            with patch(
                "masamune.tui.app.run_list_patches",
                side_effect=ConfigError("token=secret"),
            ):
                async with app.run_test() as pilot:
                    await pilot.press("x")
                    app.action_list_patches()
                    worker = app._patch_worker
                    self.assertIsNotNone(worker)
                    assert worker is not None
                    with self.assertRaises(Exception) as raised:
                        await worker.wait()
                    self.assertEqual(type(raised.exception).__name__, "WorkerFailed")
                    await pilot.pause()
                    status = content(app, "#patch-status")
                    self.assertIn("Patch list failed", status)
                    self.assertIn("token=<redacted>", status)
                    self.assertNotIn("secret", status)

    async def test_build_worker_reports_redacted_events_and_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "morphe.toml"
            keystore = root / "builder.p12"
            config.write_text(_CONFIG, encoding="utf-8")
            keystore.write_bytes(b"key")
            app = tui_app(config)
            app.args.keystore = keystore

            def build(args, *, reporter, cancel_event=None):
                self.assertIs(args, app.args)
                reporter.event("tools", "preparing tools")
                reporter.event(
                    "resolve",
                    "token=secret",
                    package="com.example.app",
                    arch="arm64-v8a",
                    token="secret",
                )
                return _summary(
                    [
                        BuildResult(
                            name="Example",
                            package="com.example.app",
                            version_name="1",
                            version_code="1",
                            architecture="arm64-v8a",
                            artifacts=("example.apk",),
                        )
                    ],
                    [
                        {
                            "name": "Other",
                            "package": "com.example.other",
                            "architecture": "x86_64",
                            "reason": "unavailable",
                        }
                    ],
                )

            with (
                patch.dict("os.environ", {"MORPHE_KEYSTORE_PASSWORD": "test-only"}),
                patch(
                    "masamune.tui.app.run_build", side_effect=build
                ) as run_build,
            ):
                async with app.run_test() as pilot:
                    await pilot.press("x")
                    await confirm_build(pilot, app)
                    worker = app._build_worker
                    self.assertIsNotNone(worker)
                    assert worker is not None
                    self.assertTrue(worker._thread_worker)
                    await worker.wait()
                    await pilot.pause()
                    run_build.assert_called_once()
                    self.assertEqual(content(app, "#build-status"), "Build: SUCCESS")
                    self.assertEqual(
                        content(app, "#build-stage"), "Build stage: resolve"
                    )
                    events = content(app, "#build-events")
                    self.assertIn("token=<redacted>", events)
                    self.assertNotIn("secret", events)
                    for index in range(60):
                        app._record_build_event(
                            {"event": "download", "message": f"event-{index}"}
                        )
                    events = content(app, "#build-events")
                    self.assertIn("event-0", events)
                    self.assertIn("event-59", events)
                    results = content(app, "#results")
                    self.assertIn("Completed: Example", results)
                    self.assertIn("example.apk", results)
                    self.assertIn("Skipped: Other", results)
                    self.assertEqual(
                        rows(app, "#build-jobs"),
                        [
                            ("com.example.app arm64-v8a", "SUCCESS"),
                            ("com.example.other x86_64", "SKIPPED"),
                        ],
                    )

    async def test_late_build_event_after_teardown_is_ignored(self) -> None:
        app = tui_app(Path("morphe.toml"))
        async with app.run_test() as pilot:
            await pilot.press("x")
            with patch.object(
                app, "call_from_thread", side_effect=RuntimeError("app closed")
            ):
                app._relay_build_event({"event": "late", "message": "token=secret"})
            self.assertEqual(content(app, "#build-events"), "No build events.")

    async def test_build_worker_reports_failure_without_error_detail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "morphe.toml"
            config.write_text(_CONFIG, encoding="utf-8")
            app = tui_app(config)
            app.args.keystore = root / "builder.p12"
            with (
                patch.dict("os.environ", {"MORPHE_KEYSTORE_PASSWORD": "test-only"}),
                patch(
                    "masamune.tui.app.run_build",
                    side_effect=RuntimeError("password=secret"),
                ),
            ):
                async with app.run_test() as pilot:
                    await pilot.press("x")
                    await confirm_build(pilot, app)
                    worker = app._build_worker
                    self.assertIsNotNone(worker)
                    assert worker is not None
                    with self.assertRaises(Exception) as raised:
                        await worker.wait()
                    self.assertEqual(type(raised.exception).__name__, "WorkerFailed")
                    await pilot.pause()
                    self.assertEqual(content(app, "#build-status"), "Build: FAILED")
                    error = content(app, "#build-error")
                    self.assertEqual(error, "Build failed: password=<redacted>")
                    self.assertNotIn("secret", error)

    async def test_build_worker_reports_known_error_message_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "morphe.toml"
            config.write_text(_CONFIG, encoding="utf-8")
            app = tui_app(config)
            app.args.keystore = root / "builder.p12"
            with (
                patch.dict("os.environ", {"MORPHE_KEYSTORE_PASSWORD": "test-only"}),
                patch(
                    "masamune.tui.app.run_build",
                    side_effect=IntegrityMetadataError(
                        "missing required split: config.arm64_v8a token=secret"
                    ),
                ),
            ):
                async with app.run_test() as pilot:
                    await pilot.press("x")
                    await confirm_build(pilot, app)
                    worker = app._build_worker
                    self.assertIsNotNone(worker)
                    assert worker is not None
                    with self.assertRaises(Exception) as raised:
                        await worker.wait()
                    self.assertEqual(type(raised.exception).__name__, "WorkerFailed")
                    await pilot.pause()
                    error = content(app, "#build-error")
                    self.assertEqual(
                        error,
                        "Build failed: missing required split: config.arm64_v8a "
                        "token=<redacted>",
                    )

    async def test_second_build_request_is_rejected_while_worker_runs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "morphe.toml"
            config.write_text(_CONFIG, encoding="utf-8")
            app = tui_app(config)
            app.args.keystore = root / "builder.p12"
            started = Event()
            release = Event()

            def build(args, *, reporter, cancel_event=None):
                started.set()
                release.wait(timeout=10)
                return {
                    "status": "complete",
                    "jobs": [],
                    "skipped": [],
                    "summary": "ok",
                }

            with (
                patch.dict("os.environ", {"MORPHE_KEYSTORE_PASSWORD": "test-only"}),
                patch(
                    "masamune.tui.app.run_build", side_effect=build
                ) as run_build,
            ):
                async with app.run_test() as pilot:
                    await pilot.press("x")
                    await confirm_build(pilot, app)
                    await pilot.pause(0.1)
                    self.assertTrue(started.is_set())
                    app.action_start_build()
                    await pilot.pause()
                    self.assertEqual(run_build.call_count, 1)
                    self.assertEqual(content(app, "#status"), "Build already running")
                    notifications = list(app._notifications)
                    self.assertTrue(notifications)
                    self.assertEqual(notifications[-1].severity, "warning")
                    release.set()
                    worker = app._build_worker
                    self.assertIsNotNone(worker)
                    assert worker is not None
                    await worker.wait()
                    await pilot.pause()

    async def test_stop_build_requests_cancellation(self) -> None:
        app = tui_app(Path("morphe.toml"))
        async with app.run_test() as pilot:
            await pilot.press("x")
            app._build_worker = SimpleNamespace(is_finished=False)
            app.action_stop_build()
            await pilot.pause()
            self.assertFalse(app._build_cancel_event.is_set())
            self.assertIn(
                "Stop active build after current operation?",
                str(app.screen.query_one("#confirm-question").content),
            )
            await pilot.click("#yes")
            await pilot.pause()
            self.assertTrue(app._build_cancel_event.is_set())
            self.assertTrue(app.query_one("#stop-build").disabled)
            self.assertEqual(
                content(app, "#status"), "Stopping build after current operation"
            )

    async def test_confirmation_cancel_and_missing_password_do_not_start_build(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "morphe.toml"
            config.write_text(_CONFIG, encoding="utf-8")
            app = tui_app(config)
            app.args.keystore = root / "builder.p12"
            with patch("masamune.tui.app.run_build") as run_build:
                async with app.run_test() as pilot:
                    await pilot.press("x")
                    app.action_start_build()
                    await pilot.pause()
                    confirmation = str(
                        app.screen.query_one("#confirm-question").content
                    )
                    self.assertIn(f"Configuration: {config}", confirmation)
                    self.assertIn("Alias: masamune", confirmation)
                    self.assertNotIn("MORPHE_KEYSTORE_PASSWORD=", confirmation)
                    await pilot.click("#no")
                    await pilot.pause()
                    self.assertIsNone(app._build_worker)
                    run_build.assert_not_called()
                    with patch.dict("os.environ", {}, clear=True):
                        await confirm_build(pilot, app)
                    self.assertIsNone(app._build_worker)
                    self.assertEqual(content(app, "#build-status"), "Build: FAILED")
                    self.assertIn(
                        "MORPHE_KEYSTORE_PASSWORD is required",
                        content(app, "#build-error"),
                    )

    async def test_build_gets_fresh_timestamped_output_each_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "morphe.toml"
            config.write_text(_CONFIG, encoding="utf-8")
            app = tui_app(config)
            app.args.keystore = root / "builder.p12"
            app._output_base = root / "output"
            app.args.output = app._output_base
            app._output_base.mkdir()
            with (
                patch.dict("os.environ", {"MORPHE_KEYSTORE_PASSWORD": "test-only"}),
                patch(
                    "masamune.tui.app.run_build",
                    return_value={"jobs": [], "skipped": []},
                ) as run_build,
            ):
                async with app.run_test() as pilot:
                    await pilot.press("x")
                    await confirm_build(pilot, app)
                    self.assertIsNotNone(app._build_worker)
                    assert app._build_worker is not None
                    await app._build_worker.wait()
                    await pilot.pause()
                    run_build.assert_called_once()
                    self.assertEqual(app.args.output.parent, app._output_base)
                    self.assertNotEqual(app.args.output, app._output_base)
                    self.assertEqual(content(app, "#build-status"), "Build: SUCCESS")

    async def test_completed_build_is_recorded_and_survives_new_app_instance(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "morphe.toml"
            config.write_text(_CONFIG, encoding="utf-8")
            app = tui_app(config)
            app.args.keystore = root / "builder.p12"
            with (
                patch.dict("os.environ", {"MORPHE_KEYSTORE_PASSWORD": "test-only"}),
                patch(
                    "masamune.tui.app.run_build",
                    return_value={
                        "jobs": [
                            {
                                "package": "com.example.app",
                                "architecture": "arm64-v8a",
                                "artifacts": ["out.apk"],
                            }
                        ],
                        "skipped": [],
                    },
                ),
            ):
                async with app.run_test() as pilot:
                    await pilot.press("x")
                    await confirm_build(pilot, app)
                    assert app._build_worker is not None
                    await app._build_worker.wait()
                    await pilot.pause()
                    self.assertEqual(content(app, "#build-status"), "Build: SUCCESS")
                    app.action_show_builds()
                    self.assertEqual(
                        rows(app, "#builds-table")[0][3], "com.example.app arm64-v8a"
                    )
            history = load_build_history()
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["status"], "SUCCESS")
            self.assertEqual(history[0]["jobs"], ["com.example.app arm64-v8a"])
            reopened = tui_app(config)
            self.assertEqual(reopened._builds_history, history)

    async def test_duplicate_build_timestamps_render_with_unique_row_keys(self) -> None:
        app = tui_app(Path("morphe.toml"))
        app._builds_history = [
            {"timestamp": "2024-01-01T00:00:00", "status": "SUCCESS"},
            {"timestamp": "2024-01-01T00:00:00", "status": "FAILED"},
        ]
        async with app.run_test() as pilot:
            await pilot.press("x")
            table = app.query_one("#builds-table")
            self.assertEqual(table.row_count, 2)

    async def test_build_context_menu_opens_output_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "20240101-000000"
            output.mkdir()
            append_build_history(
                {
                    "timestamp": "2024-01-01T00:00:00",
                    "status": "SUCCESS",
                    "output": str(output),
                    "jobs": ["com.example.app arm64-v8a"],
                }
            )
            app = tui_app(Path("morphe.toml"))
            app._builds_history = load_build_history()
            with (
                patch("masamune.tui.app.sys.platform", "win32"),
                patch("masamune.tui.app.os.startfile", create=True) as opener,
            ):
                async with app.run_test(size=(120, 35)) as pilot:
                    await pilot.press("x")
                    app.action_show_builds()
                    await pilot.pause()
                    await pilot.click("#builds-table", offset=(3, 1))
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertEqual(
                        type(app.screen).__name__, "BuildContextMenuScreen"
                    )
                    await pilot.press("enter")
                    await pilot.pause()
            opener.assert_called_once_with(output)

    async def test_build_context_menu_opens_output_folder_on_linux(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "20240101-000000"
            output.mkdir()
            append_build_history(
                {
                    "timestamp": "2024-01-01T00:00:00",
                    "status": "SUCCESS",
                    "output": str(output),
                    "jobs": ["com.example.app arm64-v8a"],
                }
            )
            app = tui_app(Path("morphe.toml"))
            app._builds_history = load_build_history()
            with (
                patch("masamune.tui.app.sys.platform", "linux"),
                patch("masamune.tui.app.subprocess.run") as run,
            ):
                async with app.run_test(size=(120, 35)) as pilot:
                    await pilot.press("x")
                    app.action_show_builds()
                    await pilot.pause()
                    await pilot.click("#builds-table", offset=(3, 1))
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertEqual(
                        type(app.screen).__name__, "BuildContextMenuScreen"
                    )
                    await pilot.press("enter")
                    await pilot.pause()
            run.assert_called_once_with(["xdg-open", str(output)], check=False)

    async def test_build_context_menu_deletes_build_and_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "20240101-000000"
            output.mkdir()
            (output / "app.apk").write_bytes(b"data")
            append_build_history(
                {
                    "timestamp": "2024-01-01T00:00:00",
                    "status": "SUCCESS",
                    "output": str(output),
                    "jobs": ["com.example.app arm64-v8a"],
                }
            )
            app = tui_app(Path("morphe.toml"))
            app._builds_history = load_build_history()
            async with app.run_test(size=(120, 35)) as pilot:
                await pilot.press("x")
                app.action_show_builds()
                await pilot.pause()
                await pilot.click("#builds-table", offset=(3, 1))
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(type(app.screen).__name__, "BuildContextMenuScreen")
                await pilot.press("down", "enter")
                await pilot.pause()
                self.assertEqual(type(app.screen).__name__, "ConfirmScreen")
                await pilot.click("#yes")
                await pilot.pause()
                self.assertEqual(rows(app, "#builds-table"), [])
            self.assertFalse(output.exists())
            self.assertEqual(load_build_history(), [])

    async def test_cache_view_reports_actual_cache_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cache"
            (cache / "toolchains").mkdir(parents=True)
            (cache / "toolchains" / "tool.jar").write_bytes(b"tool")
            (cache / "tools").mkdir()
            (cache / "tools" / "tool.jar").write_bytes(b"tool")
            (cache / "work").mkdir()
            (cache / "work" / "scratch").write_bytes(b"tmp")
            (cache / "google-version-mappings.json").write_text("{}")
            keystore = Path(directory) / "signing.p12"
            keystore.write_bytes(b"key")
            app = tui_app(Path("morphe.toml"))
            app.args.cache = cache
            app.args.keystore = keystore
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.press("x")
                await pilot.pause(0.1)
                app.action_show_cache()
                await pilot.pause()
                inventory = {row[0]: row for row in rows(app, "#cache-table")}
                self.assertIn(f"Cache: {cache}", content(app, "#cache-status"))
                self.assertEqual(inventory["toolchains"][2], "1")
                self.assertEqual(inventory["tools"][2], "1")
                self.assertEqual(inventory["work"][2], "1")
                self.assertEqual(inventory["google-version-mappings.json"][2], "1")
                self.assertEqual(inventory["keystore (external)"][2], "1")
                self.assertIn(
                    "Configured signing key", inventory["keystore (external)"][1]
                )
                self.assertIn(
                    "Disposable now: 2 file(s)", content(app, "#cache-status")
                )

    async def test_cache_context_menu_opens_area_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cache"
            trusted = cache / "toolchains"
            trusted.mkdir(parents=True)
            app = tui_app(Path("morphe.toml"))
            app.args.cache = cache
            with (
                patch("masamune.tui.app.sys.platform", "win32"),
                patch(
                    "masamune.tui.app.os.startfile", create=True
                ) as open_folder,
            ):
                async with app.run_test(size=(140, 40)) as pilot:
                    await pilot.press("x")
                    app.action_show_cache()
                    app._open_cache_context_menu("toolchains")
                    await pilot.pause()
                    self.assertEqual(
                        type(app.screen).__name__, "CacheContextMenuScreen"
                    )
                    await pilot.press("enter")
                    await pilot.pause()
            open_folder.assert_called_once_with(trusted)

    async def test_cache_context_menu_deletes_area(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cache"
            trusted = cache / "toolchains"
            trusted.mkdir(parents=True)
            (trusted / "tool.jar").write_bytes(b"tool")
            app = tui_app(Path("morphe.toml"))
            app.args.cache = cache
            async with app.run_test(size=(120, 35)) as pilot:
                await pilot.press("x")
                app.action_show_cache()
                app._open_cache_context_menu("toolchains")
                await pilot.pause()
                await pilot.press("down", "enter")
                await pilot.pause()
                self.assertEqual(type(app.screen).__name__, "ConfirmScreen")
                await pilot.click("#yes")
                await pilot.pause()
                worker = app._clean_worker
                self.assertIsNotNone(worker)
                assert worker is not None
                await worker.wait()
            self.assertFalse(trusted.exists())

    async def test_clean_runs_in_worker_and_reports_success_or_error(self) -> None:
        app = tui_app(Path("morphe.toml"))
        with patch(
            "masamune.tui.app.run_clean", return_value={"removed": ["work"]}
        ):
            async with app.run_test(size=(120, 50)) as pilot:
                await pilot.press("x")
                app.action_clean()
                await pilot.pause()
                app.screen.query_one("#cache-clean-apply").press()
                await pilot.pause()
                worker = app._clean_worker
                self.assertIsNotNone(worker)
                assert worker is not None
                self.assertTrue(worker._thread_worker)
                await worker.wait()
                await pilot.pause()
                self.assertIn("1 path(s) removed", content(app, "#status"))
        failed = tui_app(Path("morphe.toml"))
        with patch("masamune.tui.app.run_clean", side_effect=RuntimeError("bad")):
            async with failed.run_test(size=(120, 50)) as pilot:
                await pilot.press("x")
                failed.action_clean()
                await pilot.pause()
                failed.screen.query_one("#cache-clean-apply").press()
                await pilot.pause()
                worker = failed._clean_worker
                self.assertIsNotNone(worker)
                assert worker is not None
                await pilot.pause(0.1)
                self.assertEqual(
                    content(failed, "#status"), "Cache clean failed: RuntimeError"
                )

    def test_preferences_reject_terminal_aliases_of_reserved_bindings(self) -> None:
        with self.assertRaisesRegex(ValueError, "reserved"):
            validate_keybindings({"cache": "ctrl+i"})
        self.assertEqual(
            validate_keybindings({"cache": "ctrl+left_square_brace"}),
            {"cache": "escape"},
        )

    def test_preferences_validate_collisions_reserved_aliases_and_atomic_writes(
        self,
    ) -> None:
        assert validate_keybindings is not None
        with self.assertRaisesRegex(ValueError, "collide"):
            validate_keybindings({"cache": "q"})
        for key in ("ctrl+c", "?", "question_mark"):
            with self.assertRaisesRegex(ValueError, "reserved"):
                validate_keybindings({"cache": key})
        with self.assertRaisesRegex(ValueError, "invalid"):
            validate_keybindings({"unknown": "x"})
        self.assertEqual(validate_keybindings({"cache": "X"}), {"cache": "x"})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences" / "tui.json"
            assert (
                Preferences is not None
                and save_preferences is not None
                and load_preferences is not None
            )
            preferences = Preferences("morphe-light", {"cache": "x"})
            with ThreadPoolExecutor(max_workers=4) as executor:
                list(
                    executor.map(
                        lambda _: save_preferences(preferences, path), range(8)
                    )
                )
            self.assertEqual(load_preferences(path), preferences)
            self.assertFalse(list(path.parent.glob(".tui-*.tmp")))
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            path.write_text('{"theme":"bad"}', encoding="utf-8")
            self.assertEqual(load_preferences(path), Preferences())

    async def test_theme_persists_and_reset_restores_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tui.json"
            with patch("masamune.tui.app.preference_path", return_value=path):
                app = tui_app(Path("morphe.toml"))
                async with app.run_test() as pilot:
                    await pilot.press("x")
                    app.theme = "morphe-light"
                    await pilot.pause()
                    preferences = load_preferences(path)
                    self.assertEqual(preferences.theme, "morphe-light")
                    app.set_binding_overrides({"cache": "x"})
                    self.assertEqual(load_preferences(path).keymap(), {"cache": "x"})
                    app.action_reset_preferences()
                    self.assertEqual(load_preferences(path), Preferences())


if __name__ == "__main__":
    unittest.main()
