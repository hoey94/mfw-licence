# -*- coding: utf-8 -*-
"""
授权门禁与运行期监护（连接纯逻辑层 ``LicenseService`` 与 UI 层）。
================================================================
对外只有四个入口：

* ``get_license_guard()``       进程内单例；
* ``check_startup_license()``   在 MainWindow 构造之前调用，返回是否放行；
* ``install_runtime_license_guard()``  主窗口显示后启动 1s 监护；
* ``force_quit_application()``  到期确定后主动关闭整个程序。

到期处理顺序：``lock_session`` -> 状态卡片切到「当前会话已锁定」 ->
``signalBus.license_session_locked``（MainWindow 停任务） -> 模态 MessageBox ->
用户确定 -> 优雅退出（走 MainWindow 既有清理），8 秒未退出则强制结束进程。
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

from app.common.signal_bus import signalBus
from app.core.service.license_service import (
    LicenseGateAction,
    LicenseNetworkError,
    LicenseSnapshot,
    LicenseStatus,
    decide_license_gate,
    get_license_service,
)

logger = logging.getLogger(__name__)


def _build_online_verifier():
    """可选在线校验：未配置 MFW_LICENSE_ENDPOINT 时完全离线。

    网络不通抛 ``LicenseNetworkError``，由 ``LicenseService`` 按
    ``require_online`` 决定拒绝还是降级为离线激活。
    """
    endpoint = os.environ.get("MFW_LICENSE_ENDPOINT", "").strip()
    if not endpoint:
        return None

    def verifier(key: str, machine_code: str) -> bool:
        import requests

        try:
            response = requests.post(
                endpoint,
                json={"key": key, "machine_code": machine_code},
                timeout=3,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise LicenseNetworkError(f"授权服务器不可达：{exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise LicenseNetworkError("授权服务器响应格式异常。") from exc
        if not isinstance(payload, dict) or "valid" not in payload:
            raise LicenseNetworkError("授权服务器响应格式异常。")
        return bool(payload["valid"])

    return verifier


class LicenseGuard(QObject):
    """向所有 UI 消费者广播统一的授权快照，并检测运行期到期。"""

    statusChanged = Signal(object)   # LicenseSnapshot
    expired = Signal(object)         # LicenseSnapshot（已锁定）

    def __init__(self, service=None, parent: QObject | None = None):
        super().__init__(parent)
        self.service = service or get_license_service()
        self.service.online_verifier = _build_online_verifier()
        self.service.require_online = os.environ.get("MFW_LICENSE_REQUIRE_ONLINE") == "1"

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._expiry_handled = False

    # -- 对外 API -----------------------------------------------------------
    def snapshot(self) -> LicenseSnapshot:
        return self.service.snapshot()

    def activate(self, key: str) -> LicenseSnapshot:
        snapshot = self.service.activate(key)
        self._expiry_handled = False
        self.statusChanged.emit(snapshot)
        signalBus.license_status_changed.emit(snapshot)
        return snapshot

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()
        self._tick()

    def stop(self) -> None:
        self._timer.stop()

    # -- 内部 ---------------------------------------------------------------
    def _tick(self) -> None:
        try:
            snapshot = self.snapshot()
        except Exception as exc:  # 监护定时器绝不能因异常中断
            logger.exception("刷新授权状态失败: %s", exc)
            return

        if snapshot.status is LicenseStatus.EXPIRED and not self._expiry_handled:
            self._expiry_handled = True
            locked = self.service.lock_session()
            self.statusChanged.emit(locked)
            signalBus.license_status_changed.emit(locked)
            signalBus.license_session_locked.emit()
            signalBus.license_expired.emit(locked)
            self.expired.emit(locked)
            return

        self.statusChanged.emit(snapshot)
        signalBus.license_status_changed.emit(snapshot)


_guard: LicenseGuard | None = None


def get_license_guard() -> LicenseGuard:
    global _guard
    if _guard is None:
        _guard = LicenseGuard(parent=QApplication.instance())
    return _guard


# ---------------------------------------------------------------------------
# 启动门禁
# ---------------------------------------------------------------------------
def check_startup_license(parent=None) -> str:
    """在构造 MainWindow 之前调用。

    Returns:
        ``"ok"`` 放行；``"first_run_denied"`` 用户放弃激活；
        ``"expired"`` 授权已到期（已弹窗提示，调用方应直接退出）。
    """
    from app.view.license_interface.license_widgets import (
        LicenseGateDialog,
        show_license_expired_dialog,
    )

    guard = get_license_guard()
    try:
        snapshot = guard.snapshot()
    except Exception as exc:
        logger.exception("读取本地授权状态失败，按未激活处理: %s", exc)
        snapshot = LicenseSnapshot(
            LicenseStatus.UNACTIVATED,
            machine_code=guard.service.machine_code,
            trial_code=guard.service.trial_code,
            message="本地授权数据读取失败，请重新激活。",
            warning=str(exc),
        )

    action = decide_license_gate(snapshot)
    if action is LicenseGateAction.EXPIRED:
        logger.warning("启动时检测到授权已到期，弹出续期窗口")
        renewed = show_license_expired_dialog(snapshot, parent, guard=guard)
        if renewed and decide_license_gate(guard.snapshot()) is LicenseGateAction.ALLOW:
            logger.info("到期窗口内续期成功，放行进入主程序")
            return "ok"
        return "expired"

    if action is LicenseGateAction.REQUIRE_ACTIVATION:
        logger.info(
            "检测到%s，弹出强制激活窗口",
            "首次启动（本地配置/注册表为空）"
            if guard.service.is_first_run()
            else "本地授权无效或缺失",
        )
        dialog = LicenseGateDialog(guard, parent)
        try:
            dialog.exec()
        finally:
            dialog.release_owned_parent()
        if not dialog.activated:
            return "first_run_denied"
        # 激活后立刻复核一次，避免刚激活的密钥本身已到期
        after = guard.snapshot()
        if decide_license_gate(after) is LicenseGateAction.EXPIRED:
            renewed = show_license_expired_dialog(after, parent, guard=guard)
            if renewed and decide_license_gate(guard.snapshot()) is LicenseGateAction.ALLOW:
                return "ok"
            return "expired"
    return "ok"


# ---------------------------------------------------------------------------
# 运行期监护与主动退出
# ---------------------------------------------------------------------------
_runtime_installed = False


def install_runtime_license_guard(app: QApplication, dispatcher: Any) -> LicenseGuard:
    """主窗口显示后调用：启动倒计时监护，并在到期后弹窗 + 退出程序。"""
    global _runtime_installed
    guard = get_license_guard()
    if not _runtime_installed:
        guard.expired.connect(
            lambda snapshot: _on_runtime_expired(snapshot, app, dispatcher)
        )
        _runtime_installed = True
    guard.start()
    return guard


def _on_runtime_expired(snapshot: LicenseSnapshot, app: QApplication, dispatcher: Any) -> None:
    from app.view.license_interface.license_widgets import (
        show_license_expired_dialog,
    )

    guard = get_license_guard()
    guard.stop()  # 弹窗嵌套事件循环期间不再重复触发
    logger.warning("运行期检测到授权到期，锁定会话并准备退出")
    try:
        renewed = show_license_expired_dialog(snapshot, guard=guard, runtime=True)
        if renewed:
            logger.info("到期窗口内已保存新授权；会话已锁定，按流程退出后需重新启动")
    except Exception as exc:  # 弹窗异常也不能阻断退出
        logger.exception("显示授权到期提示失败: %s", exc)
    force_quit_application(app, dispatcher)


def force_quit_application(app: QApplication, dispatcher: Any = None) -> None:
    """主动关闭整个程序：优先走 MainWindow 既有清理流程，超时则强制结束。"""
    window = getattr(dispatcher, "window", None) if dispatcher is not None else None
    if window is not None:
        # closeEvent 会依次停止运行时、通知/更新线程，再真正关闭窗口
        window._allow_window_close = True
        QTimer.singleShot(0, window.close)
    else:
        QTimer.singleShot(0, app.quit)

    def _hard_exit() -> None:
        logger.error("授权到期后的优雅退出超时，强制结束进程")
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)

    QTimer.singleShot(8000, _hard_exit)
