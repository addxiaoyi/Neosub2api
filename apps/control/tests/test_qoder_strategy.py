"""Qoder 五档账号选择策略 → 本地调度档位的映射与选号行为。

证据：上游 `buddy-switch-gateway/src/account_strategy.rs`（Current / Pinned / MaxCredits / Random / Sequential）。
本地档位：`services/scheduling.py` 的 priority / sticky / weighted / failover。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.adapters import qoder
from app.adapters.base import ProviderError
from app.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "unified.db",
        web_dir=tmp_path,
        admin_username="admin",
        admin_password="test",
        session_ttl_sec=3600,
        cookie_secure=False,
        codebuddy_base="http://127.0.0.1:7864",
        codebuddy_api_key="",
        opencode_upstream_base="http://127.0.0.1:1",
        grok_register_executor_url="",
        grok_register_max_concurrency=1,
        grok_register_max_workers=1,
        allow_anonymous_gateway=False,
        cors_origins=(),
        allow_private_upstreams=True,
    )


def _adapter(
    tmp_path: Path, api_key: str = "dt-one", api_keys: list[str] | None = None, **options
) -> qoder.QoderAdapter:
    adapter = qoder.QoderAdapter(_settings(tmp_path))
    adapter.configure(api_key=api_key, api_keys=api_keys or [], options=options)
    return adapter


# ---------- 映射表 ----------


def test_five_upstream_strategies_map_to_local_tiers() -> None:
    assert qoder.ACCOUNT_STRATEGY_POLICIES == {
        "current": "sticky",
        "sequential": "sticky",
        "pinned": "priority",
        "random": "weighted",
        "max_credits": "",
    }
    assert set(qoder.ACCOUNT_STRATEGY_POLICIES) == set(qoder.ACCOUNT_STRATEGIES)
    assert qoder.DEFAULT_ACCOUNT_STRATEGY == "current"


def test_parse_string_and_object_forms() -> None:
    assert qoder.parse_account_strategy("sequential")["kind"] == "sequential"
    parsed = qoder.parse_account_strategy({"kind": "pinned", "account_id": "drt-x"})
    assert parsed == {"kind": "pinned", "account_id": "drt-x", "policy": "priority"}
    assert qoder.parse_account_strategy(None)["kind"] == "current"
    assert qoder.parse_account_strategy("")["kind"] == "current"
    assert qoder.parse_account_strategy({"account_id": "x"})["kind"] == "current"


def test_parse_rejects_unknown_and_pinned_without_id() -> None:
    with pytest.raises(ProviderError) as unknown:
        qoder.parse_account_strategy("cheapest")
    assert unknown.value.status_code == 400
    assert "未知的账号选择策略" in unknown.value.message
    with pytest.raises(ProviderError) as pinned:
        qoder.parse_account_strategy({"kind": "pinned"})
    assert pinned.value.status_code == 400
    assert "account_id" in pinned.value.message


def test_unset_strategy_keeps_legacy_behaviour(tmp_path: Path) -> None:
    assert _adapter(tmp_path)._account_strategy() is None
    assert _adapter(tmp_path, account_strategy="")._account_strategy() is None


def test_weights_are_ignored_for_upstream_strategies_that_ignore_them(tmp_path: Path) -> None:
    weights = {qoder.fingerprint("dt-one"): 5.0}
    for kind in ("random", "pinned", "sequential"):
        adapter = _adapter(tmp_path, account_strategy={"kind": kind, "account_id": "dt-one"}
                            if kind == "pinned" else kind, key_weights=weights)
        assert adapter._selection_weights(adapter._account_strategy()) is None
    current = _adapter(tmp_path, account_strategy="current", key_weights=weights)
    assert current._selection_weights(current._account_strategy()) == weights


# ---------- 选号行为 ----------


async def test_random_picks_uniformly_among_healthy_candidates(tmp_path: Path, monkeypatch) -> None:
    adapter = _adapter(tmp_path, api_key="", api_keys=["dt-a", "dt-b", "dt-c"], account_strategy="random")
    keys = adapter._keys()
    assert len(keys) == 3
    await adapter._pool.cool(keys[1], 120)
    seen: list[list[str]] = []

    def _first(candidates):
        seen.append(list(candidates))
        return candidates[0]

    monkeypatch.setattr(qoder.random, "choice", _first)
    pick = await adapter._pick(adapter._account_strategy(), keys, "", "")
    assert pick is not None and pick.rule == "random"
    assert seen == [[keys[0], keys[2]]]
    assert pick.key == keys[0]


async def test_pinned_accepts_raw_credential_or_fingerprint(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path, api_key="", api_keys=["dt-a", "dt-b", "dt-c"],
                       account_strategy={"kind": "pinned", "account_id": "dt-b"})
    keys = adapter._keys()
    strategy = adapter._account_strategy()
    pick = await adapter._pick(strategy, keys, "", "")
    assert pick is not None and pick.key == "dt-b" and pick.rule == "pinned"

    by_print = _adapter(tmp_path, api_key="", api_keys=["dt-a", "dt-b", "dt-c"],
                        account_strategy={"kind": "pinned", "account_id": qoder.fingerprint("dt-c")})
    pick = await by_print._pick(by_print._account_strategy(), by_print._keys(), "", "")
    assert pick is not None and pick.key == "dt-c"


async def test_pinned_missing_account_is_loud(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path, account_strategy={"kind": "pinned", "account_id": "dt-ghost"})
    with pytest.raises(ProviderError) as caught:
        await adapter._pick(adapter._account_strategy(), adapter._keys(), "", "")
    assert caught.value.status_code == 401
    assert "不在凭据池" in caught.value.message


async def test_max_credits_is_refused_before_any_request(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path, account_strategy="max_credits")
    with pytest.raises(ProviderError) as caught:
        await adapter.fetch_models()
    assert caught.value.status_code == 400
    assert "max_credits 本地未实现" in caught.value.message


async def test_current_and_sequential_delegate_to_group_sticky(tmp_path: Path, monkeypatch) -> None:
    calls: list[dict] = []

    async def _fake_pick_in_group(pool, store, **kwargs):
        calls.append(kwargs)
        return qoder.Pick(kwargs["keys"][0], 0, "group_sticky", note="sentinel")

    monkeypatch.setattr(qoder, "pick_in_group", _fake_pick_in_group)
    for kind in ("current", "sequential"):
        adapter = _adapter(tmp_path, account_strategy=kind)
        strategy = adapter._account_strategy()
        assert strategy["policy"] == "sticky"
        pick = await adapter._pick(strategy, adapter._keys(), "session-1", "")
        assert pick is not None and pick.rule == "group_sticky"

    assert len(calls) == 2
    assert calls[0]["sticky_key"] == "session-1"
    assert calls[0]["group"] == "default"
