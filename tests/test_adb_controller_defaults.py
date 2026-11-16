import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.common.constants import (
    DEFAULT_ADB_ADDRESS,
    _CONTROLLER_,
    _PRETASK_,
    _RESOURCE_,
    POST_ACTION,
)
from app.core.item import ConfigItem, CoreSignalBus, TaskItem
from app.core.runner.task_flow import TaskFlowRunner
from app.core.service.config_service import ConfigService, JsonConfigRepository
from app.core.service.option_service import OptionService
from app.core.utils.controller_defaults import normalize_adb_controller_options

INTERFACE = {
    "name": "Demo",
    "controller": [{"name": "安卓端", "type": "Adb"}],
    "resource": [{"name": "官服"}],
}


def _controller_task(task_option=None):
    return TaskItem(
        name="Controller",
        item_id=_CONTROLLER_,
        is_checked=True,
        task_option=dict(task_option or {"controller_type": "安卓端"}),
    )


class ControllerDefaultsHelperTests(unittest.TestCase):
    def test_fresh_adb_config_gets_default_address_and_is_idempotent(self):
        options = {"controller_type": "安卓端"}

        self.assertTrue(normalize_adb_controller_options(options, INTERFACE))
        self.assertEqual(
            DEFAULT_ADB_ADDRESS, options["安卓端"]["address"]
        )
        self.assertFalse(normalize_adb_controller_options(options, INTERFACE))

    def test_interface_default_wins_over_builtin(self):
        interface = deepcopy(INTERFACE)
        interface["controller"][0]["option"] = {
            "address": "127.0.0.1:7555",
            "adb_path": "/opt/adb",
            "controller": ["安卓端"],
        }
        options = {"controller_type": "安卓端"}

        normalize_adb_controller_options(options, interface)

        self.assertEqual("127.0.0.1:7555", options["安卓端"]["address"])
        self.assertEqual("/opt/adb", options["安卓端"]["adb_path"])

    def test_user_values_and_empty_address_are_normalized(self):
        customized = {
            "controller_type": "安卓端",
            "安卓端": {"address": "127.0.0.1:5555", "adb_path": "adb"},
        }
        normalize_adb_controller_options(customized, INTERFACE)
        self.assertEqual("127.0.0.1:5555", customized["安卓端"]["address"])

        emptied = {
            "controller_type": "安卓端",
            "安卓端": {"address": "", "adb_path": "adb"},
        }
        self.assertTrue(normalize_adb_controller_options(emptied, INTERFACE))
        self.assertEqual(DEFAULT_ADB_ADDRESS, emptied["安卓端"]["address"])

    def test_non_adb_controller_is_untouched(self):
        interface = {
            "controller": [{"name": "桌面端", "type": "Win32"}],
        }
        options = {"controller_type": "桌面端"}

        self.assertFalse(normalize_adb_controller_options(options, interface))
        self.assertNotIn("桌面端", options)

    def test_legacy_type_key_is_migrated(self):
        options = {"controller_type": "安卓端", "adb": {"adb_path": "adb"}}

        self.assertTrue(normalize_adb_controller_options(options, INTERFACE))
        self.assertNotIn("adb", options)
        self.assertEqual(DEFAULT_ADB_ADDRESS, options["安卓端"]["address"])
        self.assertEqual("adb", options["安卓端"]["adb_path"])


class ConfigServiceAdbDefaultsTests(unittest.TestCase):
    def _build_service(self, temp_dir: Path) -> ConfigService:
        main_path = temp_dir / "multi_config.json"
        main_path.write_text(
            json.dumps(
                {
                    "curr_config_id": "",
                    "config_list": [],
                    "bundle": {"Demo": {"name": "Demo", "path": "./"}},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        repo = JsonConfigRepository(
            main_config_path=main_path,
            configs_dir=temp_dir / "configs",
            interface=deepcopy(INTERFACE),
        )
        return ConfigService(repo, CoreSignalBus())

    @staticmethod
    def _persisted_controller_options(service: ConfigService, config_id: str) -> dict:
        data = json.loads(
            (service.repo.configs_dir / f"{config_id}.json").read_text(
                encoding="utf-8"
            )
        )
        controller = next(
            task for task in data["tasks"] if task["item_id"] == _CONTROLLER_
        )
        return controller["task_option"]

    def test_first_launch_default_config_persists_adb_address(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._build_service(Path(temp_dir))
            config_id = service.current_config_id

            self.assertTrue(config_id)
            options = self._persisted_controller_options(service, config_id)
            self.assertEqual(DEFAULT_ADB_ADDRESS, options["安卓端"]["address"])
            loaded = service.get_config(config_id)
            loaded_controller = next(
                task for task in loaded.tasks if task.item_id == _CONTROLLER_
            )
            self.assertEqual(
                DEFAULT_ADB_ADDRESS,
                loaded_controller.task_option["安卓端"]["address"],
            )

    def test_new_config_dialog_payload_gets_default_address(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._build_service(Path(temp_dir))
            new_item = ConfigItem(
                name="新建配置 1",
                item_id=ConfigItem.generate_id(),
                tasks=[
                    _controller_task(),
                    TaskItem(
                        name="Resource",
                        item_id=_RESOURCE_,
                        is_checked=True,
                        task_option={"resource": "官服", "setting_options": {}},
                    ),
                    TaskItem(
                        name="Post-Action",
                        item_id=POST_ACTION,
                        is_checked=True,
                        task_option={},
                    ),
                ],
                know_task=[],
                bundle="Demo",
            )

            new_id = service.create_config(new_item)

            self.assertTrue(new_id)
            options = self._persisted_controller_options(service, new_id)
            self.assertEqual(DEFAULT_ADB_ADDRESS, options["安卓端"]["address"])

    def test_multiple_configs_each_load_their_own_controller_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._build_service(Path(temp_dir))
            first = service.current_config_id
            second = service.create_config(
                ConfigItem(
                    name="新建配置 1",
                    item_id=ConfigItem.generate_id(),
                    tasks=[_controller_task()],
                    know_task=[],
                    bundle="Demo",
                )
            )

            for config_id in (first, second):
                config = service.get_config(config_id)
                controller = next(
                    task for task in config.tasks if task.item_id == _CONTROLLER_
                )
                self.assertEqual(
                    DEFAULT_ADB_ADDRESS,
                    controller.task_option["安卓端"]["address"],
                )
                self.assertEqual(
                    DEFAULT_ADB_ADDRESS,
                    self._persisted_controller_options(service, config_id)["安卓端"][
                        "address"
                    ],
                )

    def test_existing_config_without_address_is_migrated_on_load(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            service = self._build_service(Path(temp_dir))
            config_id = service.current_config_id
            legacy = ConfigItem(
                name="Default Config",
                item_id=config_id,
                tasks=[
                    TaskItem(
                        name="PreTask",
                        item_id=_PRETASK_,
                        is_checked=True,
                        task_option={},
                    ),
                    _controller_task({"controller_type": "安卓端"}),
                    TaskItem(
                        name="Resource",
                        item_id=_RESOURCE_,
                        is_checked=True,
                        task_option={"resource": "官服"},
                    ),
                    TaskItem(
                        name="Post-Action",
                        item_id=POST_ACTION,
                        is_checked=True,
                        task_option={},
                    ),
                ],
                know_task=[],
                bundle="Demo",
            )
            (service.repo.configs_dir / f"{config_id}.json").write_text(
                json.dumps(legacy.to_dict(), ensure_ascii=False), encoding="utf-8"
            )

            options = self._persisted_controller_options(service, config_id)
            self.assertNotIn("安卓端", options)

            config = service.get_config(config_id)
            controller = next(
                task for task in config.tasks if task.item_id == _CONTROLLER_
            )
            self.assertEqual(
                DEFAULT_ADB_ADDRESS, controller.task_option["安卓端"]["address"]
            )
            self.assertEqual(
                DEFAULT_ADB_ADDRESS,
                self._persisted_controller_options(service, config_id)["安卓端"][
                    "address"
                ],
            )


class OptionServiceControllerWhitelistTests(unittest.TestCase):
    class _TaskService:
        def __init__(self, interface, task):
            self.interface = interface
            self._task = task
            self.saved = []

        def get_task(self, task_id):
            return self._task if task_id == _CONTROLLER_ else None

        def update_task(self, task):
            self.saved.append(deepcopy(task.task_option))
            return True

        def refresh_hidden_flags(self):
            return None

    def test_address_is_saved_even_when_interface_has_no_controller_names(self):
        task = _controller_task()
        task_service = self._TaskService({"controller": []}, task)
        service = OptionService(task_service, CoreSignalBus())  # type: ignore[arg-type]

        self.assertIn("安卓端", service._controller_names())
        self.assertTrue(
            service.update_controller_field(
                "安卓端", "adb", "address", "127.0.0.1:7777"
            )
        )
        self.assertEqual("127.0.0.1:7777", task.task_option["安卓端"]["address"])
        self.assertEqual(1, len(task_service.saved))


class ControllerSettingUiDefaultsTests(unittest.TestCase):
    def test_ui_copies_of_options_are_persisted(self):
        from app.view.task_interface.components.option_widget_mixin.controller_setting_mixin import (
            ControllerSettingWidget,
        )

        saved = []
        fake_options = SimpleNamespace(
            update_controller_field=lambda name, ctype, field, value: (
                saved.append((name, ctype, field, value)),
                True,
            )[1],
        )
        widget = SimpleNamespace(
            current_config={"controller_type": "安卓端"},
            service_coordinator=SimpleNamespace(
                interface_api=SimpleNamespace(data=INTERFACE),
                options=fake_options,
            ),
        )

        ControllerSettingWidget._persist_adb_defaults(widget, "安卓端")

        self.assertEqual(
            [("安卓端", "adb", "address", DEFAULT_ADB_ADDRESS)], saved
        )
        self.assertEqual(
            DEFAULT_ADB_ADDRESS, widget.current_config["安卓端"]["address"]
        )
        ControllerSettingWidget._persist_adb_defaults(widget, "安卓端")
        self.assertEqual(1, len(saved))


class RuntimeAdbDefaultsTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _runner(controller_raw, interface=INTERFACE, with_connect=True):
        task = _controller_task(deepcopy(controller_raw))
        runner = SimpleNamespace()
        runner._runtime_interface = interface
        runner._get_interface_controller_entry = (
            lambda name: TaskFlowRunner._get_interface_controller_entry(runner, name)
        )
        runner._controller_task = task
        runner._get_task = lambda task_id: task if task_id == _CONTROLLER_ else None
        runner._persist_runtime_task_update = Mock(return_value=True)
        runner.need_stop = False
        runner.adb_controller_config = {}
        runner._uses_ld_extras = Mock(return_value=False)
        runner._auto_find_adb_device = AsyncMock(return_value=None)
        runner._save_device_to_config = Mock()
        runner._emit_controller_setup_hint = Mock()
        runner.log_output = SimpleNamespace(emit=Mock())
        runner.tr = lambda text: text
        if with_connect:
            runner.maafw = SimpleNamespace(connect_adb=AsyncMock(return_value=True))
        return runner

    def test_runtime_fallback_fills_address_and_persists(self):
        raw = {"controller_type": "安卓端", "安卓端": {"adb_path": "adb"}}
        runner = self._runner(raw)

        TaskFlowRunner._ensure_runtime_adb_defaults(runner, raw, "安卓端")

        self.assertEqual(DEFAULT_ADB_ADDRESS, raw["安卓端"]["address"])
        self.assertEqual(
            DEFAULT_ADB_ADDRESS,
            runner._controller_task.task_option["安卓端"]["address"],
        )
        runner._persist_runtime_task_update.assert_called_once()

    def test_runtime_fallback_skips_non_adb_controller(self):
        interface = {"controller": [{"name": "桌面端", "type": "Win32"}]}
        raw = {"controller_type": "桌面端", "桌面端": {"hwnd": "1"}}
        runner = self._runner(raw, interface=interface)

        TaskFlowRunner._ensure_runtime_adb_defaults(runner, raw, "桌面端")

        self.assertNotIn("address", raw["桌面端"])
        runner._persist_runtime_task_update.assert_not_called()

    async def test_start_uses_default_address_instead_of_reporting_empty(self):
        raw = {"controller_type": "安卓端", "安卓端": {"adb_path": "adb"}}
        runner = self._runner(raw)
        TaskFlowRunner._ensure_runtime_adb_defaults(runner, raw, "安卓端")

        connected = await TaskFlowRunner._try_prepare_and_connect_adb(
            runner,
            controller_raw=raw,
            controller_type="adb",
            controller_name="安卓端",
            has_raw_input_method=False,
            raw_input_method=0,
            has_raw_screen_method=False,
            raw_screen_method=0,
        )

        self.assertTrue(connected)
        self.assertEqual(
            DEFAULT_ADB_ADDRESS, runner.maafw.connect_adb.await_args.args[1]
        )
        runner.log_output.emit.assert_not_called()
        runner._emit_controller_setup_hint.assert_not_called()


if __name__ == "__main__":
    unittest.main()
