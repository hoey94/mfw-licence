"""控制器默认配置的统一入口。

历史上「ADB 连接地址」的默认值只在 UI 的内存字典里用 ``setdefault`` 补齐，
首次启动或新建配置时写入磁盘的 ``Controller.task_option`` 只有
``controller_type``。任务流启动时会重新从磁盘反序列化配置，因此界面上明明
填了地址，日志却报「ADB 连接地址为空」。

本模块把默认值收敛到一处，供配置创建、配置读取、UI 与运行时共同使用，保证
多配置 / 多资源包场景下每一条配置的 ADB 控制器都带有默认连接地址。
"""

from __future__ import annotations

from typing import Any

from app.common.constants import DEFAULT_ADB_ADDRESS

# interface.controller[].option 若使用字典形式，可覆盖 ADB 默认连接参数。
# resource/controller 白名单标记不属于实际连接配置，这里只取连接字段。
_ADB_CONNECTION_FIELDS = ("adb_path", "address")


def resolve_controller_name(task_option: Any) -> str:
    """从 Controller.task_option 解析控制器名称（兼容旧版 dict 形式）。"""
    if not isinstance(task_option, dict):
        return ""
    raw = task_option.get("controller_type", "")
    if isinstance(raw, dict):
        raw = raw.get("value", "")
    return raw.strip() if isinstance(raw, str) else ""


def find_controller_entry(interface: Any, controller_name: str) -> dict[str, Any]:
    """按名称（忽略大小写）查找 interface.controller 中的定义。"""
    if not isinstance(interface, dict) or not controller_name:
        return {}
    controllers = interface.get("controller", [])
    if not isinstance(controllers, list):
        return {}
    normalized = controller_name.strip().lower()
    for entry in controllers:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("name", "") or "").strip().lower() == normalized:
            return entry
    return {}


def resolve_controller_type(interface: Any, controller_name: str) -> str:
    """返回小写控制器类型；interface 未加载或不匹配时返回空串。"""
    entry = find_controller_entry(interface, controller_name)
    return str(entry.get("type", "") or "").strip().lower()


def _interface_adb_defaults(interface: Any, controller_name: str) -> dict[str, str]:
    """读取 interface 中声明的 ADB 连接默认值（资源/控制器白名单字段除外）。"""
    entry = find_controller_entry(interface, controller_name)
    option = entry.get("option")
    if not isinstance(option, dict):
        return {}
    result: dict[str, str] = {}
    for field in _ADB_CONNECTION_FIELDS:
        value = option.get(field)
        if isinstance(value, str) and value.strip():
            result[field] = value.strip()
    return result


def adb_default_connection_values(
    interface: Any = None, controller_name: str = ""
) -> dict[str, str]:
    """返回 ADB 连接参数的默认值（interface 声明优先于内置默认值）。"""
    values = _interface_adb_defaults(interface, controller_name)
    if not values.get("address"):
        values["address"] = DEFAULT_ADB_ADDRESS
    return values


def normalize_adb_controller_options(
    task_option: dict[str, Any],
    interface: Any = None,
    *,
    controller_type: str | None = None,
) -> bool:
    """就地补齐 ADB 控制器的默认连接配置，返回是否发生修改。

    - 仅处理 ADB 控制器；类型可由 ``interface`` 推断，也可由调用方显式传入。
    - 已配置（非空）的值始终优先；interface 声明的默认值优先于内置默认值。
    - 兼容旧配置：按控制器类型（``adb``）存储的子配置会迁移到控制器名称键下。
    - 幂等，可在每次读取配置时安全调用。
    """
    if not isinstance(task_option, dict):
        return False
    controller_name = resolve_controller_name(task_option)
    if not controller_name:
        return False

    resolved_type = (
        controller_type or resolve_controller_type(interface, controller_name) or ""
    ).strip().lower()
    if resolved_type != "adb":
        return False

    controller_config = task_option.get(controller_name)
    changed = False
    if not isinstance(controller_config, dict):
        legacy = task_option.get("adb")
        if isinstance(legacy, dict):
            controller_config = legacy
            task_option.pop("adb", None)
        else:
            controller_config = {}
        task_option[controller_name] = controller_config
        changed = True

    defaults = adb_default_connection_values(interface, controller_name)
    for field, value in defaults.items():
        if str(controller_config.get(field) or "").strip():
            continue
        controller_config[field] = value
        changed = True
    return changed
