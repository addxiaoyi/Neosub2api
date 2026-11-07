"""四档调度策略与错误分类：粘滞命中、优先级、权重、故障转移、全冷却降级。

策略规则不靠猜：每个用例只锁一条规则，命中原因都能在 plan.rule / plan.note / 日志里对上。
"""

from __future__ import annotations

import logging
import random
from pathlib import Path
from typing import Any

import pytest

from app.adapters import codar, trae
from app.adapters.codar import _retryable
from app.config import Settings
from app.services.rotation import fingerprint
from app.services.scheduling import Scheduler, session_key

pytestmark = pytest.mark.providers


def _settings(tmp: Path) -> Settings:
    return Settings(
        data_dir=tmp,
        db_path=tmp / "unified.db",
        web_dir=tmp / "web",
        admin_username="admin",
        admin_password="test",
        session_ttl_sec=3600,
        cookie_secure=False,
        codebuddy_base="http://127.0.0.1:7899",
        codebuddy_api_key="",
        opencode_upstream_base="http://127.0.0.1:7899",
        grok_register_executor_url="",
        grok_register_max_concurrency=1,
        grok_register_max_workers=2,
        allow_anonymous_gateway=False,
        cors_origins=(),
    )


def _codar(base: str, tmp: Path, *, api_key: str = "", options: dict[str, Any] | None = None) -> codar.CodarAdapter:
    adapter = codar.build(_settings(tmp), base_url=base)
    adapter.configure(api_key=api_key, options=options or {})
    return adapter


# ---------- 策略规则 ----------


async def test_priority_always_prefers_first_healthy_token():
    scheduler = Scheduler()
    keys = ["tok-a", "tok-b"]
    plan = await scheduler.plan(keys, policy="priority", limit=1)
    assert (plan.rule, plan.indices) == ("priority", [0])
    await scheduler.note_failure("tok-a", 401)
    shifted = await scheduler.plan(keys, policy="priority", limit=1)
    assert shifted.indices == [1], "主号冷却后优先级必须落到下一个健康号"


async def test_sticky_reuses_session_token_and_rotates_other_sessions():
    scheduler = Scheduler()
    keys = ["tok-a", "tok-b", "tok-c"]
    first = await scheduler.plan(keys, policy="sticky", session_key="sess-1", limit=2)
    again = await scheduler.plan(keys, policy="sticky", session_key="sess-1", limit=2)
    assert first.indices[0] == again.indices[0], "同一会话两次请求必须命中同一账号"
    other = await scheduler.plan(keys, policy="sticky", session_key="sess-2", limit=2)
    assert other.indices[0] != first.indices[0]


async def test_sticky_without_session_key_degrades_to_failover():
    scheduler = Scheduler()
    plan = await scheduler.plan(["tok-a", "tok-b"], policy="sticky", session_key="")
    assert plan.rule == "failover"
    assert "粘滞退化为故障转移" in plan.note


async def test_weighted_policy_biases_configured_weights():
    scheduler = Scheduler(rng=random.Random(3))
    keys = ["light", "heavy"]
    seen = {"light": 0, "heavy": 0}
    for _ in range(200):
        plan = await scheduler.plan(keys, policy="weighted", limit=1, weights=[1, 4])
        assert plan.rule == "weighted"
        seen[keys[plan.indices[0]]] += 1
    assert seen["heavy"] > seen["light"] * 2


async def test_weighted_without_weights_degrades_to_failover():
    scheduler = Scheduler()
    plan = await scheduler.plan(["tok-a", "tok-b"], policy="weighted", limit=1)
    assert plan.rule == "failover", "没配权重时不能把轮转谎报成 weighted"
    assert "未配置有效权重" in plan.note


async def test_weighted_with_valid_weights_really_picks_by_weight():
    scheduler = Scheduler(rng=random.Random(7))
    keys = ["light", "heavy"]
    seen = {"light": 0, "heavy": 0}
    for _ in range(300):
        plan = await scheduler.plan(keys, policy="weighted", limit=1, weights=[1, 5])
        assert plan.rule == "weighted"
        seen[keys[plan.indices[0]]] += 1
    assert seen["heavy"] > seen["light"] * 3, "有权重时必须按权重选中，而不是退化成均权轮转"


async def test_failover_rotates_and_skips_failed_token():
    scheduler = Scheduler()
    keys = ["tok-a", "tok-b", "tok-c"]
    first = await scheduler.plan(keys, policy="failover", limit=1)
    second = await scheduler.plan(keys, policy="failover", limit=1)
    assert first.indices == [0] and second.indices == [1]
    await scheduler.note_failure("tok-b", 429)
    third = await scheduler.plan(keys, policy="failover", limit=1)
    assert third.indices == [0], "冷却中的账号不能再被选中"


async def test_all_tokens_cooled_degrades_and_says_why(caplog):
    scheduler = Scheduler()
    keys = ["tok-a", "tok-b"]
    for key in keys:
        await scheduler.note_failure(key, 429)
    with caplog.at_level(logging.WARNING, logger="unified.rotation"):
        plan = await scheduler.plan(keys, policy="failover", limit=2)
    assert plan.degraded is True
    assert plan.indices == [0, 1], "降级时必须保留可尝试顺序，而不是直接 429 空转"
    assert "冷却" in plan.note
    assert "降级" in caplog.text and "全冷却" in caplog.text


async def test_plan_limit_truncates_attempt_order():
    scheduler = Scheduler()
    keys = ["tok-a", "tok-b", "tok-c", "tok-d"]
    plan = await scheduler.plan(keys, policy="failover", limit=2)
    assert len(plan.indices) == 2


# ---------- 模型级冷却 ----------


async def test_plan_model_cooled_token_still_serves_other_models():
    scheduler = Scheduler()
    keys = ["tok-a", "tok-b"]
    await scheduler.note_failure("tok-a", 429, model="cn:m-a")

    same_model = await scheduler.plan(keys, policy="priority", limit=1, model="cn:m-a")
    assert keys[same_model.indices[0]] == "tok-b", "同模型 429 后不能再撞 tok-a"

    other_model = await scheduler.plan(keys, policy="priority", limit=1, model="cn:m-b")
    assert keys[other_model.indices[0]] == "tok-a", "同一账号换了模型必须回到首选"


async def test_plan_account_level_failure_cools_every_model():
    scheduler = Scheduler()
    keys = ["tok-a", "tok-b"]
    await scheduler.note_failure("tok-a", 401, model="cn:m-a")
    other_model = await scheduler.plan(keys, policy="priority", limit=1, model="cn:m-b")
    assert keys[other_model.indices[0]] == "tok-b", "401 是账号级失败，换模型也不能选它"


async def test_plan_all_model_cooled_degrades():
    scheduler = Scheduler()
    keys = ["tok-a", "tok-b"]
    for key in keys:
        await scheduler.note_failure(key, 429, model="cn:m-a")
    plan = await scheduler.plan(keys, policy="priority", limit=2, model="cn:m-a")
    assert plan.degraded is True
    assert plan.indices == [0, 1], "全模型冷却时仍要给出可尝试顺序，而不是直接空转"
    assert "冷却" in plan.note


async def test_snapshot_reports_model_cooling_without_account_cooling():
    scheduler = Scheduler()
    keys = ["tok-a", "tok-b"]
    await scheduler.note_failure("tok-a", 429, model="cn:m-a")
    state = await scheduler.snapshot(keys)
    assert state["cooling"] == 0, "模型 429 不能把账号算进账号级冷却"
    assert state["available"] == 2
    assert state["model_cooling"] == 1
    assert state["model_cooldowns"][fingerprint("tok-a")] == {"cn:m-a": 60.0}
    assert state["policy"]


async def test_unknown_policy_falls_back_with_warning(caplog):
    scheduler = Scheduler()
    with caplog.at_level(logging.WARNING, logger="unified.scheduling"):
        plan = await scheduler.plan(["tok-a", "tok-b"], policy="round-robin-ish")
    assert plan.rule in ("sticky", "failover")
    assert "未知调度策略" in caplog.text


def test_session_key_uses_payload_fields():
    assert session_key({"session_id": "s-1"}) == "s-1"
    assert session_key({"conversation_id": "c-1"}) == "c-1"
    assert session_key({"messages": []}) == ""


# ---------- Codar 失败动作表 ----------


def test_codar_retry_table_covers_five_status_classes():
    assert _retryable(0) == "rotate", "网络层失败换号"
    assert _retryable(401) == "refresh", "401 先续期"
    assert _retryable(402) == "stop", "402 付费受限不再重试"
    assert _retryable(429) == "cool", "429 冷却后换号"
    assert _retryable(500) == _retryable(503) == "rotate"
    assert _retryable(400) == _retryable(403) == "", "其余状态原样透传，不进重试表"


# ---------- Trae 错误分类 ----------


def test_trae_classification_maps_plan_rate_session(tmp_path: Path):
    adapter = trae.build(_settings(tmp_path))
    dead = adapter._classify_error(401, None, "jwt rejected")
    assert dead.status_code == 401 and dead.session_dead is True
    plan = adapter._classify_error(400, 1005, "credits not enough")
    assert plan.status_code == 402 and plan.plan_limit is True
    rate = adapter._classify_error(429, None, "slow down")
    assert rate.status_code == 429 and rate.soft_rate is True
    other = adapter._classify_error(400, None, "bad request")
    assert other.status_code == 400 and not getattr(other, "session_dead", False)


# ---------- Codar 适配器接线 ----------


async def test_codar_same_session_hits_same_token_and_steps_over_cooled(fake_base, tmp_path):
    adapter = _codar(
        fake_base,
        tmp_path,
        options={"tokens": ["codar-token-1", "codar-token-2"], "schedule_policy": "sticky"},
    )
    used: list[str] = []
    original = adapter._client

    def spy(token: str, **kwargs: Any):
        used.append(token)
        return original(token, **kwargs)

    adapter._client = spy
    body = {
        "model": "claude-3-5-sonnet",
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "hi"}],
        "session_id": "sess-1",
    }
    assert (await adapter.chat_completion(body, stream=False)).status_code == 200
    assert (await adapter.chat_completion(body, stream=False)).status_code == 200
    assert used[0] == used[1] == "codar-token-1", "同一 session_id 必须命中同一上游 token"

    other = await adapter.chat_completion(dict(body, session_id="sess-2"), stream=False)
    assert other.status_code == 200
    assert used[2] == "codar-token-2"

    await adapter._scheduler.note_failure("codar-token-1", 429)
    third = await adapter.chat_completion(dict(body, session_id="sess-3"), stream=False)
    assert third.status_code == 200
    assert used[-1] == "codar-token-2", "冷却中的 token 必须被跳过"


async def test_codar_count_tokens_is_declared_and_reachable(fake_base, tmp_path):
    assert codar.CodarAdapter.spec.capabilities["count_tokens"] is True
    adapter = _codar(fake_base, tmp_path, api_key="codar-token-1")
    reply = await adapter.count_tokens(
        {"model": "claude-3-5-sonnet", "messages": [{"role": "user", "content": "hi"}]}, stream=False
    )
    assert reply.status_code == 200
