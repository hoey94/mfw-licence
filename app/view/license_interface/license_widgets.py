# -*- coding: utf-8 -*-
"""
软件授权 UI 组件（PySide6 + qfluentwidgets）。
============================================
* ``LicenseActivationCard``：本机试用码 / 机器码（只读 + 复制）、激活码输入、
  「填入试用码」「验证激活」按钮与行内反馈。
* ``LicenseStatusCard``：底部状态卡片，按 ``LicenseSnapshot`` 在
  未激活 / 已激活倒计时 / 即将到期 / 已到期 / 当前会话已锁定之间动态切换。
* ``LicensePage``：设置中的「软件授权」详情页，同时被首次启动门禁弹窗复用。
* ``LicenseGateDialog``：首次启动强制激活，未激活成功不允许进入主程序。
* ``LicenseExpiredDialog``：到期模态提示框，可就地填入新激活码续期，或确定后退出。

设计参考了 MFAvalonia 的三张截图，但布局与状态机按本项目的 Fluent 组件体系重做：
没有引入左侧二级菜单，而是把详情页作为设置页顶部的独立分区。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    FluentIcon as FIF,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    MessageBoxBase,
    PrimaryPushButton,
    PushButton,
    SimpleCardWidget,
    StrongBodyLabel,
    SubtitleLabel,
    TextEdit,
    isDarkTheme,
)

from app.common.license_contract import (
    LicenseError,
    LicenseKind,
    LicenseSnapshot,
    LicenseStatus,
    format_expire_at,
)

_ACCENT = "#2b6cf6"
_SUCCESS = "#2e9e5b"
_WARNING = "#d98a0b"
_DANGER = "#e04b4b"
_MUTED_LIGHT = "#606570"
_MUTED_DARK = "#a7adb8"


def _muted_color() -> str:
    return _MUTED_DARK if isDarkTheme() else _MUTED_LIGHT


def create_license_dialog_parent() -> QWidget:
    """启动阶段没有主窗口时，为 MessageBoxBase 创建全屏透明父窗口。

    qfluentwidgets 的 ``MaskDialogBase.__init__`` 会直接访问 ``parent.width()``，
    parent=None 会抛 AttributeError；与项目 StartupDialogManager 的处理方式一致。
    """
    parent = QWidget()
    parent.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
    parent.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    app = QApplication.instance()
    if app is not None:
        screen = app.primaryScreen()
        if screen is not None:
            parent.setGeometry(screen.availableGeometry())
    parent.show()
    return parent


def cleanup_license_dialog_parent(parent: QWidget | None) -> None:
    if parent is not None:
        parent.close()
        parent.deleteLater()


class CopyableField(QWidget):
    """一行只读字段：标题 + 值 + 复制按钮。"""

    copied = Signal(str)

    def __init__(self, title: str, value: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._value = value
        row = QVBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        label = BodyLabel(title, self)
        label.setStyleSheet(f"color: {_muted_color()}; font-size: 12px;")
        row.addWidget(label)

        line = QHBoxLayout()
        line.setSpacing(8)
        self.edit = LineEdit(self)
        self.edit.setText(value)
        self.edit.setReadOnly(True)
        self.edit.setClearButtonEnabled(False)
        self.edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.copy_button = PushButton("复制", self)
        self.copy_button.setFixedWidth(72)
        self.copy_button.setToolTip(f"复制{title}")
        self.copy_button.clicked.connect(self._on_copy)
        line.addWidget(self.edit, 1)
        line.addWidget(self.copy_button, 0)
        row.addLayout(line)

    def _on_copy(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._value)
        self.copied.emit(self._value)


class LicenseActivationCard(SimpleCardWidget):
    """「设备与激活」卡片，只负责收集输入与展示行内反馈。"""

    activationRequested = Signal(str)
    copied = Signal(str)

    def __init__(
        self,
        machine_code: str,
        trial_code: str,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setBorderRadius(10)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(14)

        header = StrongBodyLabel("设备与激活", self)
        header.setStyleSheet("font-size: 15px;")
        root.addWidget(header)

        self.trial_field = CopyableField("本机试用码", trial_code, self)
        self.machine_field = CopyableField("机器码 · 发送给作者获取正式授权", machine_code, self)
        self.trial_field.copied.connect(self._on_copied)
        self.machine_field.copied.connect(self._on_copied)
        root.addWidget(self.trial_field)
        root.addWidget(self.machine_field)

        self.key_edit = TextEdit(self)
        self.key_edit.setPlaceholderText("粘贴作者签发的激活码，或填入本机试用码")
        self.key_edit.setFixedHeight(96)
        root.addWidget(self.key_edit)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.fill_trial_button = PushButton("填入试用码", self)
        self.fill_trial_button.clicked.connect(self._on_fill_trial)
        self.verify_button = PrimaryPushButton("验证激活", self, FIF.EDIT)
        self.verify_button.clicked.connect(self._on_verify)
        actions.addWidget(self.fill_trial_button)
        actions.addWidget(self.verify_button)
        actions.addStretch(1)
        root.addLayout(actions)

        self.feedback = CaptionLabel("", self)
        self.feedback.setWordWrap(True)
        root.addWidget(self.feedback)

    def _on_copied(self, _value: str) -> None:
        self.show_feedback("已复制到剪贴板，可直接发给作者。", "muted")
        self.copied.emit(_value)

    def _on_fill_trial(self) -> None:
        self.key_edit.setPlainText(self.trial_field.edit.text())

    def _on_verify(self) -> None:
        self.activationRequested.emit(self.key_edit.toPlainText())

    def show_feedback(self, text: str, level: str = "muted") -> None:
        colors = {
            "success": _SUCCESS,
            "error": _DANGER,
            "warning": _WARNING,
            "muted": _muted_color(),
        }
        self.feedback.setStyleSheet(f"color: {colors.get(level, _muted_color())};")
        self.feedback.setText(text)

    def apply_snapshot(self, snapshot: LicenseSnapshot) -> None:
        locked = snapshot.status is LicenseStatus.LOCKED
        self.key_edit.setEnabled(not locked)
        self.fill_trial_button.setEnabled(not locked)
        self.verify_button.setEnabled(not locked)
        # 警告统一由状态卡片展示，避免每秒刷新覆盖「验证成功」等行内反馈
        if locked:
            self.show_feedback("当前会话已锁定，请重新启动后激活有效授权。", "error")


class LicenseStatusCard(SimpleCardWidget):
    """底部授权状态卡片：标题 / 说明 / 倒计时 / 有效期，随状态整体换色。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setBorderRadius(10)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(8)

        self.title_label = SubtitleLabel("尚未激活", self)
        self.subtitle = BodyLabel("", self)
        self.subtitle.setWordWrap(True)
        self.subtitle.setStyleSheet(f"color: {_muted_color()}; font-size: 12px;")

        self.countdown = BodyLabel("-- : -- : --", self)
        font = QFont()
        font.setPointSize(22)
        font.setBold(True)
        self.countdown.setFont(font)

        self.expire_caption = CaptionLabel("有效期至（本地时间）", self)
        self.expire_caption.setStyleSheet(f"color: {_muted_color()};")
        self.expire_label = BodyLabel("--", self)
        self.warning_label = CaptionLabel("", self)
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet(f"color: {_WARNING};")

        root.addWidget(self.title_label)
        root.addWidget(self.subtitle)
        root.addSpacing(6)
        root.addWidget(self.countdown)
        root.addSpacing(4)
        root.addWidget(self.expire_caption)
        root.addWidget(self.expire_label)
        root.addWidget(self.warning_label)

    def apply_snapshot(self, snapshot: LicenseSnapshot) -> None:
        status = snapshot.status
        kind_text = {
            LicenseKind.FORMAL: "正式授权",
            LicenseKind.TRIAL: "试用授权",
            LicenseKind.NONE: "",
        }[snapshot.kind]

        titles = {
            LicenseStatus.ACTIVE: f"已激活 · {kind_text}",
            LicenseStatus.EXPIRING_SOON: f"即将到期 · {kind_text}",
            LicenseStatus.EXPIRED: "授权已到期",
            LicenseStatus.LOCKED: "当前会话已锁定",
            LicenseStatus.UNACTIVATED: "尚未激活",
        }
        colors = {
            LicenseStatus.ACTIVE: _ACCENT,
            LicenseStatus.EXPIRING_SOON: _WARNING,
            LicenseStatus.EXPIRED: _DANGER,
            LicenseStatus.LOCKED: _DANGER,
            LicenseStatus.UNACTIVATED: _muted_color(),
        }
        color = colors[status]
        self.title_label.setText(titles[status])
        self.title_label.setStyleSheet(f"color: {color}; font-size: 15px; font-weight: 600;")
        self.subtitle.setText(snapshot.message or "")
        self.countdown.setText(snapshot.countdown_text)
        self.countdown.setStyleSheet(f"color: {color};")
        self.expire_label.setText(format_expire_at(snapshot.expire_at))
        self.warning_label.setText(snapshot.warning or "")
        self.warning_label.setVisible(bool(snapshot.warning))


class LicensePage(QWidget):
    """设置中的「软件授权」详情页，也可嵌入首次激活弹窗。

    通过 ``LicenseGuard.statusChanged`` 接收统一状态推送，自身不持有计时器，
    保证设置页、弹窗、运行期监护看到的是同一份 ``LicenseSnapshot``。
    """

    activationSucceeded = Signal()

    def __init__(
        self,
        guard=None,
        parent: QWidget | None = None,
        *,
        gate_mode: bool = False,
    ):
        super().__init__(parent)
        if guard is None:
            from app.utils.license_guard import get_license_guard

            guard = get_license_guard()
        self._guard = guard
        self._gate_mode = gate_mode

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(16)

        self.header = SubtitleLabel("软件授权", self)
        self.header.setStyleSheet("font-size: 20px; font-weight: 600;")
        self.description = BodyLabel(
            "首次使用需激活后才能进入主程序；激活此设备，开启你的自动化工作空间。"
            if gate_mode
            else "激活此设备，开启你的自动化工作空间。",
            self,
        )
        self.description.setWordWrap(True)
        self.description.setStyleSheet(f"color: {_muted_color()}; font-size: 12px;")
        root.addWidget(self.header)
        root.addWidget(self.description)

        service = guard.service
        self.activation_card = LicenseActivationCard(
            service.machine_code, service.trial_code, self
        )
        self.status_card = LicenseStatusCard(self)
        self.activation_card.activationRequested.connect(self._on_activate)
        root.addWidget(self.activation_card)
        root.addWidget(self.status_card)

        self.footer = CaptionLabel(
            "试用从首次验证成功后开始计时（2 小时），重启或重新填写试用码都不会重置；"
            "正式授权与本机绑定。可在设置中随时查看状态或更新激活码。",
            self,
        )
        self.footer.setWordWrap(True)
        self.footer.setStyleSheet(f"color: {_muted_color()};")
        root.addWidget(self.footer)

        self._guard.statusChanged.connect(self.apply_snapshot)
        self.apply_snapshot(self._guard.snapshot())

    def apply_snapshot(self, snapshot: LicenseSnapshot) -> None:
        self.activation_card.apply_snapshot(snapshot)
        self.status_card.apply_snapshot(snapshot)

    def _on_activate(self, key: str) -> None:
        try:
            snapshot = self._guard.activate(key)
        except LicenseError as exc:
            self.activation_card.show_feedback(str(exc), "error")
            if not self._gate_mode:
                InfoBar.error(
                    title="激活失败",
                    content=str(exc),
                    orient=Qt.Orientation.Horizontal,
                    isClosable=True,
                    position=InfoBarPosition.TOP_RIGHT,
                    duration=5000,
                    parent=self.window(),
                )
            return
        except Exception as exc:  # 兜底：任何未预期异常都不能让界面失去响应
            message = f"激活过程发生未知错误：{exc}"
            self.activation_card.show_feedback(message, "error")
            return

        self.activation_card.show_feedback("验证成功，授权已保存。", "success")
        self.apply_snapshot(snapshot)
        self.activationSucceeded.emit()


class LicenseDialogBase(MessageBoxBase):
    """统一处理启动阶段（无主窗口）弹窗的父窗口，避免 parent=None 崩溃。"""

    def __init__(self, parent: QWidget | None = None):
        self._owned_parent: QWidget | None = None
        if parent is None:
            parent = create_license_dialog_parent()
            self._owned_parent = parent
        super().__init__(parent)

    def release_owned_parent(self) -> None:
        cleanup_license_dialog_parent(self._owned_parent)
        self._owned_parent = None


class LicenseGateDialog(LicenseDialogBase):
    """首次启动强制激活弹窗：没有"跳过"，只能激活成功或主动退出程序。"""

    def __init__(self, guard, parent: QWidget | None = None):
        super().__init__(parent)
        self._activated = False
        self._allow_close = False
        self.buttonGroup.hide()
        self.widget.setMinimumWidth(600)

        self.page = LicensePage(guard, self.widget, gate_mode=True)
        self.viewLayout.addWidget(self.page)
        self.page.activationSucceeded.connect(self._on_activated)

        footer = QHBoxLayout()
        footer.addStretch(1)
        self.exit_button = PushButton("退出程序", self.widget)
        self.exit_button.clicked.connect(self._on_exit)
        footer.addWidget(self.exit_button)
        self.viewLayout.addLayout(footer)

    @property
    def activated(self) -> bool:
        return self._activated

    def _on_activated(self) -> None:
        self._activated = True
        self.accept()

    def _on_exit(self) -> None:
        self._allow_close = True
        self.reject()

    def keyPressEvent(self, event) -> None:
        # 禁止 Esc 绕过激活门禁；退出必须显式点击「退出程序」
        if event.key() == Qt.Key.Key_Escape:
            self.page.activation_card.show_feedback("请先完成激活，或点击「退出程序」。", "warning")
            event.ignore()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        if not self._activated and not self._allow_close:
            event.ignore()
            return
        super().closeEvent(event)


class LicenseExpiredDialog(LicenseDialogBase):
    """授权到期提示框：可就地填入新激活码续期，或点「确定」退出程序。

    * 启动期到期：续期成功即可放行进入主程序；
    * 运行期到期：会话已锁定/任务已停止，续期成功后仍需重启，故保存后退出。
    """

    def __init__(
        self,
        snapshot: LicenseSnapshot,
        parent: QWidget | None = None,
        *,
        guard=None,
        runtime: bool = False,
    ):
        super().__init__(parent)
        if guard is None:
            from app.utils.license_guard import get_license_guard

            guard = get_license_guard()
        self._guard = guard
        self._runtime = runtime
        self._renewed = False
        self.buttonGroup.hide()
        self.widget.setMinimumWidth(520)

        title = SubtitleLabel("授权已到期", self.widget)
        title.setStyleSheet(f"color: {_DANGER}; font-size: 18px; font-weight: 600;")
        content = BodyLabel("授权已到期，请向作者获取新的激活码", self.widget)
        content.setWordWrap(True)
        detail = CaptionLabel(
            "当前会话已锁定，软件正在停止任务；填入新激活码保存后请重新启动程序。"
            if runtime
            else "填入作者签发的新激活码即可继续使用，或点击「确定」退出程序。",
            self.widget,
        )
        detail.setWordWrap(True)
        detail.setStyleSheet(f"color: {_muted_color()};")

        self.viewLayout.addWidget(title)
        self.viewLayout.addSpacing(8)
        self.viewLayout.addWidget(content)
        self.viewLayout.addWidget(detail)
        self.viewLayout.addSpacing(10)
        self.viewLayout.addWidget(
            CopyableField("机器码 · 获取新激活码时请一并提供", snapshot.machine_code, self.widget)
        )
        if snapshot.expire_at:
            expire = CaptionLabel(
                f"有效期至（本地时间）：{format_expire_at(snapshot.expire_at)}", self.widget
            )
            expire.setStyleSheet(f"color: {_muted_color()};")
            self.viewLayout.addWidget(expire)

        self.viewLayout.addSpacing(12)
        renew_tip = CaptionLabel("新的激活码", self.widget)
        renew_tip.setStyleSheet(f"color: {_muted_color()};")
        self.key_edit = TextEdit(self.widget)
        self.key_edit.setPlaceholderText("粘贴作者签发的新激活码")
        self.key_edit.setFixedHeight(76)
        self.feedback = CaptionLabel("", self.widget)
        self.feedback.setWordWrap(True)
        self.viewLayout.addWidget(renew_tip)
        self.viewLayout.addWidget(self.key_edit)
        self.viewLayout.addWidget(self.feedback)

        actions = QHBoxLayout()
        self.renew_button = PrimaryPushButton("验证激活", self.widget)
        self.renew_button.clicked.connect(self._on_renew)
        actions.addWidget(self.renew_button)
        actions.addStretch(1)
        self.confirm_button = PushButton("确定", self.widget)
        self.confirm_button.setFixedWidth(110)
        self.confirm_button.clicked.connect(self.accept)
        actions.addWidget(self.confirm_button)
        self.viewLayout.addLayout(actions)

    @property
    def renewed(self) -> bool:
        return self._renewed

    def _set_feedback(self, text: str, color: str) -> None:
        self.feedback.setStyleSheet(f"color: {color};")
        self.feedback.setText(text)

    def _on_renew(self) -> None:
        """就地验证并保存新激活码；失败时保留弹窗让用户重试。"""
        try:
            self._guard.activate(self.key_edit.toPlainText())
        except LicenseError as exc:
            self._set_feedback(str(exc), _DANGER)
            return
        except Exception as exc:
            self._set_feedback(f"激活过程发生未知错误：{exc}", _DANGER)
            return

        self._renewed = True
        self.renew_button.setEnabled(False)
        self.key_edit.setEnabled(False)
        if self._runtime:
            self._set_feedback("验证成功，授权已保存。程序将退出，请重新启动后使用新授权。", _SUCCESS)
            QTimer.singleShot(1500, self.accept)
        else:
            self._set_feedback("验证成功，授权已保存。", _SUCCESS)
            QTimer.singleShot(300, self.accept)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            event.ignore()  # 到期拦截不可被 Esc 取消
            return
        super().keyPressEvent(event)


def show_license_expired_dialog(
    snapshot: LicenseSnapshot,
    parent: QWidget | None = None,
    *,
    guard=None,
    runtime: bool = False,
) -> bool:
    """阻塞展示到期提示，返回用户是否已在弹窗内成功续期。"""
    dialog = LicenseExpiredDialog(snapshot, parent, guard=guard, runtime=runtime)
    try:
        dialog.exec()
    finally:
        dialog.release_owned_parent()
    return dialog.renewed
