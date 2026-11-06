"""登录冷却的单元用例：直接给节流器注入时间，不依赖真实等待。

冷却按 (来源 IP, 用户名) 计数，覆盖三件事：
同名不同 IP 互不影响、同 IP 同名持续被挡、429 重试不会把窗口无限续期。
"""

from __future__ import annotations

import pytest

from app.routers import auth


def _guard(**overrides) -> auth.LoginGuard:
    cfg = {"max_failures": 3, "window_sec": 60, "cooldown_sec": 30, "cooldown_max_sec": 120}
    cfg.update(overrides)
    return auth.LoginGuard(**cfg)


@pytest.fixture(autouse=True)
def _clean_throttle():
    auth._login_failures.clear()
    yield
    auth._login_failures.clear()


def test_same_username_from_another_ip_is_not_locked():
    guard = _guard()
    attacker = auth._login_key("198.51.100.7", "alice")
    legit = auth._login_key("203.0.113.9", "Alice")

    for index in range(3):
        auth._login_failed(attacker, at=1000 + index)

    assert auth._login_wait(attacker, guard, at=1004) > 0, "同一 IP 仍要被挡"
    assert auth._login_wait(legit, guard, at=1004) == 0, "同名但不同 IP 不应被连坐"


def test_same_ip_and_username_stays_blocked_with_exponential_backoff():
    guard = _guard()
    key = auth._login_key("203.0.113.9", "alice")

    for index in range(3):
        auth._login_failed(key, at=1000 + index)
    first = auth._login_wait(key, guard, at=1004)
    assert first == 28, "冷却从最后一次失败(1002)起算 30 秒"

    auth._login_failed(key, at=1005)
    second = auth._login_wait(key, guard, at=1006)
    assert second == 59, "第 4 次失败后退避翻倍到 60 秒，但不超过上限 120"


def test_repeated_429_retries_do_not_extend_the_window():
    guard = _guard(cooldown_max_sec=30)
    key = auth._login_key("203.0.113.9", "alice")
    for index in range(3):
        auth._login_failed(key, at=1000 + index)

    # 429 拒绝不落失败记录；客户端每秒重试一次，冷却应随时间递减而不是被续期
    waits = [auth._login_wait(key, guard, at=1003 + offset) for offset in range(31)]

    assert len(auth._login_failures[key]) == 3, "重试不能变成新的失败记录"
    assert waits[0] > 0
    assert waits[-1] == 0, "冷却到期必须放行，不能被重试永久续上"
    assert waits == sorted(waits, reverse=True), f"剩余时间必须单调递减：{waits}"
