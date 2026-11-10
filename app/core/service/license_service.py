# -*- coding: utf-8 -*-
"""
软件授权核心服务（与 UI 框架无关，可单独做单元测试）。
==================================================
校验算法移植自 ``pyqt6-checker/license_app.py``，与作者端 ``--gen-key``
生成的激活码完全兼容（同样的 SECRET_KEY、机器码盐值、HMAC 签名与 base64 结构）。

本模块**只包含校验与状态管理，不包含任何签发能力**：激活码由作者端的
``pyqt6-checker/license_app.py --gen-key`` 离线签发，签发工具与私钥不随本仓库分发。

并针对工程化落地做了以下加固：

* 试用时长修正为 2 小时（原型的 ``TRIAL_DURATION = 3`` 是调试值）；
* 签名比较改为 ``hmac.compare_digest``，base64 使用 ``validate=True``；
* 到期时间支持本地时区 / ISO8601 偏移，避免跨时区误判；
* 本地状态 JSON 带 HMAC 校验和 + 原子写，损坏时隔离备份而不是静默重置；
* 试用起始时间同时写入状态文件、独立标记文件与 Windows 注册表，
  删除配置文件也无法重新获得试用期，且不会因标记损坏而"赠送"新试用；
* 区分异常类型：激活码错误 / 已到期 / 存储损坏 / 时钟回拨 / 网络不通；
* 可选在线校验钩子，网络不通时按策略降级为离线校验（默认离线优先）。

注意：客户端内置 SECRET_KEY 只能防误改、不能对抗逆向，正式产品应把私钥与
在线校验放到服务端。可通过环境变量 ``MFW_LICENSE_SECRET`` 覆盖。

作者端签发（本仓库不包含该程序）::

    python pyqt6-checker/license_app.py --gen-key <机器码> "2027-12-31 23:59:59"
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import platform
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# 领域契约：本模块原样 re-export，保持既有 import 路径兼容；
# view 层应直接依赖 app.common.license_contract，而非本服务实现模块。
from app.common.license_contract import (
    InvalidActivationCode,
    LicenseClockError,
    LicenseError,
    LicenseExpiredError,
    LicenseGateAction,
    LicenseKind,
    LicenseNetworkError,
    LicenseSnapshot,
    LicenseStatus,
    LicenseStorageError,
    decide_license_gate,
    format_countdown,
    format_expire_at,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量配置
# ---------------------------------------------------------------------------
# 与 pyqt6-checker/license_app.py 的作者端密钥保持一致；轮换后旧激活码即失效。
# 注意：对称密钥必须随客户端分发，仍可通过环境变量 MFW_LICENSE_SECRET 覆盖，
# 生产环境应改用非对称签名（客户端只保留公钥）。
SECRET_KEY = os.environ.get(
    "MFW_LICENSE_SECRET",
    "129a16160d875b21b51ae1606e192aae41b0a20a1a0765b3e302eee9dd63d64f",
)
TRIAL_DURATION_SECONDS = 2 * 3600
EXPIRING_SOON_SECONDS = 24 * 3600
CLOCK_SKEW_TOLERANCE_SECONDS = 300

STATE_VERSION = 1
STATE_FILENAME = "license_state.json"
TRIAL_MARKER_FILENAME = "trial_start.marker"
QUARANTINE_SUFFIX = ".corrupted"
REGISTRY_PATH = r"Software\MFW-ChainFlow Assistant\License"
REGISTRY_TRIAL_VALUE = "TrialStartedAt"


# ---------------------------------------------------------------------------
# 机器码 / 试用码 / 正式激活码（算法与 pyqt6-checker 保持一致）
# ---------------------------------------------------------------------------
def _stable_hardware_id() -> str:
    """取稳定的硬件标识；无物理网卡时退化为环境指纹，避免每次启动都变化。"""
    node = uuid.getnode()
    # uuid.getnode() 在取不到 MAC 时会返回带"多播位"的随机值
    if node and not ((node >> 40) & 1):
        mac_hex = "%012X" % node
        return ":".join(mac_hex[i : i + 2] for i in range(0, 12, 2))
    return "|".join(
        (
            "fallback",
            platform.node(),
            os.environ.get("COMPUTERNAME", ""),
            str(os.cpu_count() or 0),
        )
    )


def get_mac_address() -> str:
    """读取本机 MAC 地址（仅后台使用，不展示在 UI）。"""
    return _stable_hardware_id()


def generate_machine_code(mac: str) -> str:
    """根据 MAC 生成 16 位机器码（与作者端签发工具一致）。"""
    return hashlib.sha256((mac + "#my-license").encode("utf-8")).hexdigest().upper()[:16]


def get_machine_code() -> str:
    return generate_machine_code(_stable_hardware_id())


def generate_trial_uuid(machine_code: str, secret: str = SECRET_KEY) -> str:
    """同一机器码永远生成同一个试用码（UUID 形式）。"""
    digest = hashlib.sha256((machine_code + secret).encode("utf-8")).digest()
    return str(uuid.UUID(bytes=digest[:16]))


def parse_expire_datetime(value: str) -> datetime:
    """解析到期时间，统一转换为带时区的 datetime；无时区时视为本地时间。"""
    text = (value or "").strip()
    parsed: datetime | None = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def verify_formal_key(
    key_str: str, machine_code: str, secret: str = SECRET_KEY
) -> datetime | None:
    """校验正式激活码，成功返回到期时间，失败返回 None。"""
    # 用户从邮件/聊天工具粘贴的长密钥常带换行，先去掉所有空白
    text = "".join((key_str or "").split())
    if not text:
        return None
    try:
        decoded = base64.b64decode(text.encode("utf-8"), validate=True).decode("utf-8")
        parts = decoded.split("|")
        if len(parts) != 3:
            return None
        mc, expire_str, signature = parts
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None

    expected = hmac.new(
        secret.encode("utf-8"), f"{mc}|{expire_str}".encode("utf-8"), hashlib.sha256
    ).hexdigest()[:32]
    if not hmac.compare_digest(signature, expected):
        return None
    if not hmac.compare_digest(mc, machine_code):
        return None
    try:
        return parse_expire_datetime(expire_str)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# 本地状态仓储
# ---------------------------------------------------------------------------
def _default_license_dir() -> Path:
    if sys.platform == "win32":
        base = Path(
            os.environ.get("LOCALAPPDATA")
            or os.environ.get("APPDATA")
            or (Path.home() / "AppData" / "Local")
        )
        target = base / "MFW-ChainFlow Assistant" / "license"
    elif sys.platform == "darwin":
        target = Path.home() / "Library" / "Application Support" / "MFW-ChainFlow Assistant" / "license"
    else:
        target = Path(
            os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
        ) / "mfw-chainflow-assistant" / "license"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _registry_read(name: str) -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value)
    except OSError:
        return None


def _registry_write(name: str, value: str) -> None:
    if sys.platform != "win32":
        return
    try:
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REGISTRY_PATH, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    except OSError as exc:  # 无权限/注册表被禁用时不影响主流程
        logger.debug("写入授权注册表标记失败（已忽略）: %s", exc)


class LicenseRepository:
    """授权状态与试用计时标记的存储。

    - ``license_state.json``：带 HMAC 校验和的 JSON，原子写入；
    - ``trial_start.marker``：独立的试用起始时间戳；
    - Windows 注册表：第三份试用起始时间戳（对应需求的"注册表是否为空"检查）。

    三处取最早的时间戳作为试用起点，任何一处存在都不会重新发放试用期。
    """

    def __init__(
        self,
        directory: Path | None = None,
        secret: str = SECRET_KEY,
        *,
        use_registry: bool = True,
    ):
        self.directory = Path(directory) if directory is not None else _default_license_dir()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.state_path = self.directory / STATE_FILENAME
        self.marker_path = self.directory / TRIAL_MARKER_FILENAME
        self._secret = secret
        self._use_registry = use_registry and sys.platform == "win32"

    # -- 校验和 -------------------------------------------------------------
    def _sign(self, payload: dict) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hmac.new(
            self._secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    # -- 文件存在性 / 首次启动 -----------------------------------------------
    def has_any_state(self) -> bool:
        """配置文件、独立标记或注册表任一有内容，即认为不是全新机器。"""
        try:
            if self.state_path.is_file() and self.state_path.stat().st_size > 0:
                return True
            if self.marker_path.is_file() and self.marker_path.stat().st_size > 0:
                return True
        except OSError:
            pass
        return self._use_registry and _registry_read(REGISTRY_TRIAL_VALUE) is not None

    def _quarantine(self, reason: str) -> None:
        """损坏文件改名为 .corrupted 备份，避免下次启动反复报错，也不静默删除证据。"""
        try:
            backup = self.state_path.with_suffix(self.state_path.suffix + QUARANTINE_SUFFIX)
            backup.write_text(
                self.state_path.read_text(encoding="utf-8", errors="replace"),
                encoding="utf-8",
            )
            self.state_path.unlink(missing_ok=True)
            logger.warning("本地授权文件损坏（%s），已隔离到 %s", reason, backup)
        except OSError as exc:
            logger.warning("隔离损坏的授权文件失败: %s", exc)

    def load(self) -> dict | None:
        """读取状态；文件不存在返回 None，损坏/被篡改抛 LicenseStorageError。"""
        try:
            if not self.state_path.is_file():
                return None
            raw = self.state_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LicenseStorageError(f"无法读取本地授权文件：{exc}") from exc

        if not raw.strip():
            self._quarantine("空文件")
            raise LicenseStorageError("本地授权文件为空，请重新激活。")
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._quarantine("JSON 解析失败")
            raise LicenseStorageError("本地授权文件损坏，请重新激活。") from exc
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            self._quarantine("版本不匹配")
            raise LicenseStorageError("本地授权文件版本不兼容，请重新激活。")

        signature = data.get("checksum")
        payload = {k: v for k, v in data.items() if k != "checksum"}
        if not isinstance(signature, str) or not hmac.compare_digest(signature, self._sign(payload)):
            self._quarantine("校验和不匹配")
            raise LicenseStorageError("本地授权文件已被修改，请重新激活。")
        return payload

    def save(self, payload: dict) -> None:
        """原子写入：临时文件 + os.replace，避免写一半断电导致文件损坏。"""
        data = dict(payload)
        data["version"] = STATE_VERSION
        data["checksum"] = self._sign(data)
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.directory,
                prefix=".license-",
                suffix=".tmp",
                delete=False,
            ) as tmp:
                tmp_name = tmp.name
                json.dump(data, tmp, ensure_ascii=False, indent=2)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, self.state_path)
        except OSError as exc:
            if tmp_name:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
            raise LicenseStorageError(f"无法写入本地授权文件：{exc}") from exc

    # -- 试用计时标记 --------------------------------------------------------
    def _marker_time(self, path: Path) -> float | None:
        try:
            if path.is_file():
                return float(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            logger.warning("试用计时标记 %s 损坏，按'已计时'处理", path)
            return 0.0  # 损坏时不重新发放试用期
        return None

    def read_trial_start(self) -> float | None:
        candidates: list[float] = []
        try:
            state = self.load()
        except LicenseStorageError:
            # 状态文件损坏不应阻断重新激活；独立标记/注册表仍能阻止重置试用期
            state = None
        if state and state.get("trial_started_at") is not None:
            try:
                candidates.append(float(state["trial_started_at"]))
            except (TypeError, ValueError):
                candidates.append(0.0)
        marker = self._marker_time(self.marker_path)
        if marker is not None:
            candidates.append(marker)
        registry_value = _registry_read(REGISTRY_TRIAL_VALUE) if self._use_registry else None
        if registry_value is not None:
            try:
                candidates.append(float(registry_value))
            except (TypeError, ValueError):
                candidates.append(0.0)
        return min(candidates) if candidates else None

    def write_trial_start(self, timestamp: float) -> None:
        value = str(timestamp)
        try:
            self.marker_path.write_text(value, encoding="utf-8")
        except OSError as exc:
            logger.warning("写入试用计时标记失败: %s", exc)
        if self._use_registry:
            _registry_write(REGISTRY_TRIAL_VALUE, value)


# ---------------------------------------------------------------------------
# 授权服务
# ---------------------------------------------------------------------------
class LicenseService:
    """面向 UI / 启动门禁的授权门面。

    :param repository: 存储对象（测试可注入临时目录）
    :param now_fn: 返回 epoch 秒的函数（测试可注入假时钟）
    :param online_verifier: ``(key, machine_code) -> bool``，抛 LicenseNetworkError 表示网络不通
    :param require_online: True 时网络不通直接失败；False 时降级为离线校验并给出提示
    """

    def __init__(
        self,
        repository: LicenseRepository | None = None,
        *,
        secret: str = SECRET_KEY,
        now_fn: Callable[[], float] = time.time,
        machine_code: str | None = None,
        trial_duration: int = TRIAL_DURATION_SECONDS,
        expiring_soon_seconds: int = EXPIRING_SOON_SECONDS,
        online_verifier: Callable[[str, str], bool] | None = None,
        require_online: bool = False,
    ):
        self.repo = repository or LicenseRepository(secret=secret)
        self.secret = secret
        self._now = now_fn
        self.machine_code = machine_code or get_machine_code()
        self.trial_code = generate_trial_uuid(self.machine_code, secret)
        self.trial_duration = trial_duration
        self.expiring_soon_seconds = expiring_soon_seconds
        self.online_verifier = online_verifier
        self.require_online = require_online
        self._session_locked = False
        self._warning = ""

    # -- 首次启动 -----------------------------------------------------------
    def is_first_run(self) -> bool:
        return not self.repo.has_any_state()

    # -- 状态 ---------------------------------------------------------------
    def lock_session(self) -> LicenseSnapshot:
        """运行中检测到到期：锁定当前会话，UI 显示"当前会话已锁定"。"""
        self._session_locked = True
        return self.snapshot()

    def unlock_session(self) -> None:
        self._session_locked = False

    def snapshot(self) -> LicenseSnapshot:
        warning = self._warning
        try:
            state = self.repo.load()
        except LicenseStorageError as exc:
            state = None
            warning = str(exc)

        base = dict(machine_code=self.machine_code, trial_code=self.trial_code, warning=warning)
        if self._session_locked:
            return LicenseSnapshot(
                LicenseStatus.LOCKED,
                message="软件正在停止任务，请重新启动后激活有效授权。",
                **base,
            )
        if not state or not state.get("key"):
            return LicenseSnapshot(
                LicenseStatus.UNACTIVATED,
                message="本机尚未激活，请填入本机试用码或作者签发的激活码。",
                **base,
            )

        key = "".join(str(state["key"]).split())
        expire_at: datetime | None = None
        kind = LicenseKind.NONE
        formal_expire = verify_formal_key(key, self.machine_code, self.secret)
        if formal_expire is not None:
            kind = LicenseKind.FORMAL
            expire_at = formal_expire
        elif hmac.compare_digest(key, self.trial_code):
            kind = LicenseKind.TRIAL
            started_at = self.repo.read_trial_start()
            if started_at is None:
                return LicenseSnapshot(
                    LicenseStatus.EXPIRED,
                    kind=kind,
                    message="试用计时信息缺失，请向作者获取新的正式激活码。",
                    **base,
                )
            expire_at = datetime.fromtimestamp(
                started_at + self.trial_duration, tz=timezone.utc
            ).astimezone()
        else:
            return LicenseSnapshot(
                LicenseStatus.UNACTIVATED,
                message="本地保存的激活码无效，请重新激活。",
                **base,
            )

        remaining = expire_at.timestamp() - self._now()
        # 试用只有 2 小时，"即将到期"阈值必须随总时长收缩，否则全程都是警告态
        warning_threshold = self.expiring_soon_seconds
        if kind is LicenseKind.TRIAL:
            warning_threshold = min(warning_threshold, self.trial_duration / 6)
        if remaining <= 0:
            status = LicenseStatus.EXPIRED
            message = "试用或正式授权已到期，请向作者获取新的正式激活码。"
        elif remaining <= warning_threshold:
            status = LicenseStatus.EXPIRING_SOON
            message = "授权即将到期，请尽快向作者获取新的激活码。"
        else:
            status = LicenseStatus.ACTIVE
            message = (
                "授权与本机绑定，到期后将停止任务并锁定软件。"
                if kind is LicenseKind.FORMAL
                else "试用从首次验证成功后开始计时，重启不会重置试用。"
            )
        return LicenseSnapshot(
            status,
            kind=kind,
            expire_at=expire_at,
            remaining_seconds=max(0.0, remaining),
            message=message,
            **base,
        )

    # -- 在线校验（可选） ----------------------------------------------------
    def _check_online(self, key: str) -> str:
        if self.online_verifier is None:
            return ""
        try:
            accepted = self.online_verifier(key, self.machine_code)
        except LicenseNetworkError as exc:
            if self.require_online:
                raise
            logger.warning("在线授权校验不可用，降级为离线校验: %s", exc)
            return "网络不通，已使用离线校验完成激活。"
        except Exception as exc:  # 服务端异常不应导致用户无法使用离线密钥
            logger.exception("在线授权校验异常，降级为离线校验: %s", exc)
            return "授权服务器响应异常，已使用离线校验完成激活。"
        if not accepted:
            raise InvalidActivationCode("该激活码已被停用或在其他设备使用，请联系作者。")
        return ""

    # -- 激活 ---------------------------------------------------------------
    def activate(self, key: str) -> LicenseSnapshot:
        """验证并保存激活码；失败时抛出带用户可读文案的 LicenseError 子类。"""
        text = "".join((key or "").split())
        if not text:
            raise InvalidActivationCode("请先填入激活码。")

        online_warning = self._check_online(text)
        expire_at: datetime
        formal_expire = verify_formal_key(text, self.machine_code, self.secret)
        if formal_expire is not None:
            if formal_expire.timestamp() <= self._now():
                raise LicenseExpiredError()
            kind = LicenseKind.FORMAL
            expire_at = formal_expire
            trial_started_at = None
        elif hmac.compare_digest(text, self.trial_code):
            started_at = self.repo.read_trial_start()
            if started_at is None:
                started_at = self._now()
                self.repo.write_trial_start(started_at)
            if self._now() < started_at - CLOCK_SKEW_TOLERANCE_SECONDS:
                raise LicenseClockError()
            if self._now() >= started_at + self.trial_duration:
                raise LicenseExpiredError("本机试用期已结束，请向作者获取新的正式激活码。")
            kind = LicenseKind.TRIAL
            expire_at = datetime.fromtimestamp(
                started_at + self.trial_duration, tz=timezone.utc
            ).astimezone()
            trial_started_at = started_at
        else:
            raise InvalidActivationCode()

        payload = {
            "key": text,
            "kind": kind.value,
            "machine_code": self.machine_code,
            "activated_at": self._now(),
            "expire_at": expire_at.isoformat(),
            "trial_started_at": trial_started_at,
        }
        self.repo.save(payload)
        self._session_locked = False
        self._warning = online_warning
        return self.snapshot()


_service: LicenseService | None = None


def get_license_service() -> LicenseService:
    """进程内单例，保证启动门禁、设置页、运行期监护读取同一份状态。"""
    global _service
    if _service is None:
        _service = LicenseService()
    return _service
