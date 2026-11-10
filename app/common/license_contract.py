# -*- coding: utf-8 -*-
"""
软件授权领域契约（跨层共享，零依赖）。
======================================
这里存放的是「数据契约」而非服务实现：状态枚举、授权快照、异常类型、
展示格式化函数与启动门禁决策。

``app/view`` 与 ``app/core/service`` 都只依赖本模块，以满足 Stage5 的
Facade 边界约束（view 层不得直接 import ``app.core.service.*``）；
``app/core/service/license_service.py`` 会原样 re-export 本模块的名字，
因此既有调用方无需改动。

本模块只依赖标准库，不依赖 Qt 与任何服务实现，可独立复用与单元测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class LicenseError(Exception):
    """授权模块异常基类，message 可直接展示给用户。"""


class InvalidActivationCode(LicenseError):
    """激活码为空、格式错误、签名不匹配或不属于本机。"""

    def __init__(self, message: str = "激活码无效，请检查是否与本机匹配，或向作者重新获取。"):
        super().__init__(message)


class LicenseExpiredError(LicenseError):
    """激活码本身有效但已过有效期。"""

    def __init__(self, message: str = "授权已到期，请向作者获取新的激活码"):
        super().__init__(message)


class LicenseStorageError(LicenseError):
    """本地授权文件/注册表读写失败或内容损坏。"""

    def __init__(self, message: str = "本地授权数据损坏，请重新激活。"):
        super().__init__(message)


class LicenseClockError(LicenseError):
    """检测到系统时间被大幅回拨。"""

    def __init__(self, message: str = "检测到系统时间异常，请校准系统时间后重试。"):
        super().__init__(message)


class LicenseNetworkError(LicenseError):
    """在线校验服务不可达。"""

    def __init__(self, message: str = "无法连接授权服务器，请检查网络后重试。"):
        super().__init__(message)


# ---------------------------------------------------------------------------
# 状态枚举
# ---------------------------------------------------------------------------
class LicenseKind(str, Enum):
    NONE = "none"
    TRIAL = "trial"
    FORMAL = "formal"


class LicenseStatus(str, Enum):
    UNACTIVATED = "unactivated"      # 首次启动 / 未激活
    ACTIVE = "active"                # 正常
    EXPIRING_SOON = "expiring_soon"  # 即将到期
    EXPIRED = "expired"              # 已到期
    LOCKED = "locked"                # 当前会话已锁定


class LicenseGateAction(str, Enum):
    """启动门禁决策，供 UI 层与单元测试共用。"""

    ALLOW = "allow"                            # 放行，进入主程序
    REQUIRE_ACTIVATION = "require_activation"  # 首次启动/本地授权无效，强制激活
    EXPIRED = "expired"                        # 已到期，提示后退出


# ---------------------------------------------------------------------------
# 快照与展示格式化
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LicenseSnapshot:
    """某一时刻的授权状态快照，UI 只依赖这个对象渲染。"""

    status: LicenseStatus
    kind: LicenseKind = LicenseKind.NONE
    machine_code: str = ""
    trial_code: str = ""
    expire_at: datetime | None = None
    remaining_seconds: float = 0.0
    message: str = ""
    warning: str = ""

    @property
    def is_usable(self) -> bool:
        return self.status in (LicenseStatus.ACTIVE, LicenseStatus.EXPIRING_SOON)

    @property
    def expire_text(self) -> str:
        return format_expire_at(self.expire_at) if self.expire_at else "--"

    @property
    def countdown_text(self) -> str:
        if not self.is_usable:
            return "-- : -- : --"
        return format_countdown(self.remaining_seconds)


def format_countdown(seconds: float) -> str:
    """倒计时：不足一天显示 HH : MM : SS，超过一天补上天数。"""
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    clock = f"{hours:02d} : {minutes:02d} : {secs:02d}"
    return f"{days} 天 {clock}" if days else clock


def format_expire_at(value: datetime | None) -> str:
    """``2026-11-10 18:53:20 +08:00`` 形式的本地时间展示。"""
    if value is None:
        return "--"
    offset = value.utcoffset() or timedelta(0)
    sign = "+" if offset >= timedelta(0) else "-"
    total_minutes = int(abs(offset).total_seconds()) // 60
    hours, minutes = divmod(total_minutes, 60)
    return f"{value.strftime('%Y-%m-%d %H:%M:%S')} {sign}{hours:02d}:{minutes:02d}"


# ---------------------------------------------------------------------------
# 启动门禁决策（与 Qt 无关的纯函数，便于测试）
# ---------------------------------------------------------------------------
def decide_license_gate(snapshot: LicenseSnapshot) -> LicenseGateAction:
    """纯函数：根据授权快照决定启动行为。"""
    if snapshot.status is LicenseStatus.EXPIRED:
        return LicenseGateAction.EXPIRED
    if snapshot.is_usable:
        return LicenseGateAction.ALLOW
    return LicenseGateAction.REQUIRE_ACTIVATION
