# -*- coding: utf-8 -*-
"""软件授权核心逻辑单元测试（不依赖 Qt，可在无 GUI 环境运行）。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.service.license_service import (
    LicenseGateAction,
    LicenseSnapshot,
    InvalidActivationCode,
    LicenseClockError,
    LicenseExpiredError,
    LicenseKind,
    LicenseNetworkError,
    LicenseRepository,
    LicenseService,
    LicenseStatus,
    format_countdown,
    format_expire_at,
    generate_machine_code,
    generate_trial_uuid,
    parse_expire_datetime,
    verify_formal_key,
)

SECRET = "unit-test-secret"
MACHINE_CODE = "2F060DCA0CB42F19"

# 由作者端 pyqt6-checker --gen-key 离线预生成的固定测试密钥（仅作为测试数据，
# 本仓库不包含任何签发程序）。密钥使用 SECRET 签名，机器码为 MACHINE_CODE。
FORMAL_KEY_FUTURE = "MkYwNjBEQ0EwQ0I0MkYxOXwyMDk5LTEyLTMxIDIzOjU5OjU5fGIzOWQ0NDc2OTJkZjc0MGRhZGViODYwYjFmMjVhYTA4"
FORMAL_KEY_PAST = "MkYwNjBEQ0EwQ0I0MkYxOXwyMDAwLTAxLTAxIDAwOjAwOjAwfGYyOGIwMmI2MzgwNjJiMjUxNzQ5M2Q2MjNhMzUwNWZk"
FORMAL_KEY_OTHER_MACHINE = "T1RIRVJNQUNISU5FMDAwMXwyMDk5LTEyLTMxIDIzOjU5OjU5fGE0OWM0MTUyMmY4YTgwZTMwZTM2ODIzOWM0ZDlhMDk0"
FORMAL_KEY_NEAR_EXPIRY = "MkYwNjBEQ0EwQ0I0MkYxOXwyMDMwLTAxLTAxIDAwOjAwOjAwfDExODE2NmY1YWNjODkxYTQ1YTY5YTE1MjUxYzNlNjIy"


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_service(root: str, clock: FakeClock, **kwargs) -> LicenseService:
    repo = LicenseRepository(Path(root), secret=SECRET, use_registry=False)
    return LicenseService(
        repo,
        secret=SECRET,
        now_fn=clock,
        machine_code=MACHINE_CODE,
        **kwargs,
    )


class AlgorithmTests(unittest.TestCase):
    def test_machine_code_and_trial_code_are_stable(self):
        code = generate_machine_code("AA:BB:CC:DD:EE:FF")
        self.assertEqual(16, len(code))
        self.assertEqual(code, generate_machine_code("AA:BB:CC:DD:EE:FF"))
        self.assertNotEqual(code, generate_machine_code("11:22:33:44:55:66"))
        self.assertEqual(
            generate_trial_uuid(code, SECRET), generate_trial_uuid(code, SECRET)
        )

    def test_formal_key_roundtrip(self):
        key = FORMAL_KEY_FUTURE
        expire = verify_formal_key(key, MACHINE_CODE, SECRET)
        self.assertIsNotNone(expire)
        assert expire is not None
        self.assertEqual(2099, expire.year)
        # 用户粘贴的长密钥带换行/空格也应通过
        self.assertIsNotNone(verify_formal_key(f"  {key[:20]}\n{key[20:]}  ", MACHINE_CODE, SECRET))

    def test_formal_key_rejects_tampering_and_wrong_machine(self):
        key = FORMAL_KEY_FUTURE
        self.assertIsNone(verify_formal_key(key, "OTHERMACHINE0001", SECRET))
        self.assertIsNone(verify_formal_key(key + "A", MACHINE_CODE, SECRET))
        self.assertIsNone(verify_formal_key("not-a-key", MACHINE_CODE, SECRET))
        self.assertIsNone(verify_formal_key("", MACHINE_CODE, SECRET))
        self.assertIsNone(verify_formal_key(key, MACHINE_CODE, "wrong-secret"))
        # 其它机器签发的密钥不能被本机接受
        self.assertIsNone(verify_formal_key(FORMAL_KEY_OTHER_MACHINE, MACHINE_CODE, SECRET))

    def test_expire_parsing_and_formatting(self):
        aware = parse_expire_datetime("2026-11-10T18:53:20+08:00")
        self.assertEqual("2026-11-10 18:53:20 +08:00", format_expire_at(aware))
        self.assertEqual(8 * 3600, aware.utcoffset().total_seconds())
        self.assertEqual("1 天 01 : 01 : 01", format_countdown(90061))
        self.assertEqual("00 : 00 : 36", format_countdown(36))
        self.assertEqual(
            "2026-11-10 18:53:20 +08:00",
            format_expire_at(datetime(2026, 11, 10, 18, 53, 20, tzinfo=timezone(timedelta(hours=8)))),
        )


class ServiceStateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.clock = FakeClock()
        self.service = make_service(self.root, self.clock)

    def test_first_run_is_unactivated(self):
        snapshot = self.service.snapshot()
        self.assertTrue(self.service.is_first_run())
        self.assertIs(LicenseStatus.UNACTIVATED, snapshot.status)
        self.assertFalse(snapshot.is_usable)
        self.assertEqual(MACHINE_CODE, snapshot.machine_code)
        self.assertEqual("-- : -- : --", snapshot.countdown_text)

    def test_invalid_and_empty_code_do_not_start_trial(self):
        with self.assertRaises(InvalidActivationCode):
            self.service.activate("")
        with self.assertRaises(InvalidActivationCode):
            self.service.activate("some-random-code")
        self.assertTrue(self.service.is_first_run())
        self.assertIs(LicenseStatus.UNACTIVATED, self.service.snapshot().status)

    def test_trial_activation_countdown_and_no_reset(self):
        snapshot = self.service.activate(self.service.trial_code)
        self.assertIs(LicenseStatus.ACTIVE, snapshot.status)
        self.assertIs(LicenseKind.TRIAL, snapshot.kind)
        self.assertAlmostEqual(7200, snapshot.remaining_seconds, delta=1)

        self.clock.advance(3600)
        self.assertAlmostEqual(3600, self.service.snapshot().remaining_seconds, delta=1)
        # 重新验证同一个试用码不能刷新计时
        self.service.activate(self.service.trial_code)
        self.assertAlmostEqual(3600, self.service.snapshot().remaining_seconds, delta=1)

        self.clock.advance(3601)
        expired = self.service.snapshot()
        self.assertIs(LicenseStatus.EXPIRED, expired.status)
        self.assertFalse(expired.is_usable)
        locked = self.service.lock_session()
        self.assertIs(LicenseStatus.LOCKED, locked.status)

    def test_trial_cannot_be_reset_by_deleting_state_file(self):
        self.service.activate(self.service.trial_code)
        self.clock.advance(7300)
        os.unlink(self.service.repo.state_path)
        restarted = make_service(self.root, self.clock)
        self.assertFalse(restarted.is_first_run())
        with self.assertRaises(LicenseExpiredError):
            restarted.activate(restarted.trial_code)

    def test_formal_activation_and_expiry(self):
        snapshot = self.service.activate(FORMAL_KEY_FUTURE)
        self.assertIs(LicenseKind.FORMAL, snapshot.kind)
        self.assertIs(LicenseStatus.ACTIVE, snapshot.status)
        self.assertIn("2099", snapshot.expire_text)

        with self.assertRaises(LicenseExpiredError):
            make_service(self.root, self.clock).activate(FORMAL_KEY_PAST)

    def test_expiring_soon_status(self):
        # 固定密钥 2030-01-01 到期，把假时钟设到到期前 1 小时
        near_expiry_epoch = datetime(2030, 1, 1).timestamp()
        clock = FakeClock(near_expiry_epoch - 3600)
        service = make_service(self.root, clock, expiring_soon_seconds=48 * 3600)
        snapshot = service.activate(FORMAL_KEY_NEAR_EXPIRY)
        self.assertIs(LicenseStatus.EXPIRING_SOON, snapshot.status)
        self.assertTrue(snapshot.is_usable)

    def test_clock_rollback_is_rejected(self):
        self.service.activate(self.service.trial_code)
        self.clock.advance(-3600)
        with self.assertRaises(LicenseClockError):
            self.service.activate(self.service.trial_code)


class StorageErrorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.clock = FakeClock()

    def test_corrupted_file_is_quarantined_and_reported(self):
        Path(self.root, "license_state.json").write_text("{broken", encoding="utf-8")
        service = make_service(self.root, self.clock)
        snapshot = service.snapshot()
        self.assertIs(LicenseStatus.UNACTIVATED, snapshot.status)
        self.assertIn("损坏", snapshot.warning)
        self.assertTrue(Path(self.root, "license_state.json.corrupted").exists())
        # 文件损坏 -> 强制重新激活；首次激活仍可正常开始试用
        reactivated = service.activate(service.trial_code)
        self.assertIs(LicenseStatus.ACTIVE, reactivated.status)
        self.assertAlmostEqual(7200, reactivated.remaining_seconds, delta=1)

    def test_tampered_checksum_is_rejected(self):
        service = make_service(self.root, self.clock)
        service.activate(service.trial_code)
        path = service.repo.state_path
        data = json.loads(path.read_text(encoding="utf-8"))
        data["key"] = "hacked"
        path.write_text(json.dumps(data), encoding="utf-8")
        snapshot = service.snapshot()
        self.assertIs(LicenseStatus.UNACTIVATED, snapshot.status)
        self.assertIn("已被修改", snapshot.warning)

    def test_corrupted_trial_marker_does_not_grant_new_trial(self):
        service = make_service(self.root, self.clock)
        service.activate(service.trial_code)
        service.repo.marker_path.write_text("not-a-number", encoding="utf-8")
        self.clock.advance(7300)
        with self.assertRaises(LicenseExpiredError):
            service.activate(service.trial_code)


class OnlineVerificationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clock = FakeClock()

    def test_network_failure_degrades_to_offline(self):
        def offline(*_args):
            raise LicenseNetworkError("boom")

        service = make_service(self._tmp.name, self.clock, online_verifier=offline)
        snapshot = service.activate(service.trial_code)
        self.assertIs(LicenseStatus.ACTIVE, snapshot.status)
        self.assertIn("网络", snapshot.warning)

    def test_network_failure_can_be_fatal_when_required(self):
        def offline(*_args):
            raise LicenseNetworkError("boom")

        service = make_service(
            self._tmp.name, self.clock, online_verifier=offline, require_online=True
        )
        with self.assertRaises(LicenseNetworkError):
            service.activate(service.trial_code)

    def test_server_rejection_blocks_activation(self):
        service = make_service(
            self._tmp.name, self.clock, online_verifier=lambda *_args: False
        )
        with self.assertRaises(InvalidActivationCode):
            service.activate(service.trial_code)


class StartupGateDecisionTests(unittest.TestCase):
    def test_gate_actions(self):
        from app.core.service.license_service import decide_license_gate

        self.assertIs(
            LicenseGateAction.REQUIRE_ACTIVATION,
            decide_license_gate(LicenseSnapshot(LicenseStatus.UNACTIVATED)),
        )
        self.assertIs(
            LicenseGateAction.ALLOW,
            decide_license_gate(LicenseSnapshot(LicenseStatus.ACTIVE, remaining_seconds=100)),
        )
        self.assertIs(
            LicenseGateAction.ALLOW,
            decide_license_gate(LicenseSnapshot(LicenseStatus.EXPIRING_SOON, remaining_seconds=10)),
        )
        self.assertIs(
            LicenseGateAction.EXPIRED,
            decide_license_gate(LicenseSnapshot(LicenseStatus.EXPIRED)),
        )
        self.assertIs(
            LicenseGateAction.REQUIRE_ACTIVATION,
            decide_license_gate(LicenseSnapshot(LicenseStatus.LOCKED)),
        )


if __name__ == "__main__":
    unittest.main()
