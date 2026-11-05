"""可观测性出口回归：Prometheus 文本、JSON 兼容、鉴权、真实计数、三类告警、label 脱敏。"""

from __future__ import annotations

import re
import sqlite3
import uuid

import httpx
import pytest

pytestmark = pytest.mark.gateway

SAMPLE_LINE = re.compile(r'^[a-zA-Z_:][a-zA-Z0-9_:]*(\{.*\})? [^\s]+$')
LABEL_PAIR = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


@pytest.fixture(scope="module")
def viewer(admin: httpx.Client) -> httpx.Client:
    name = f"metrics-viewer-{uuid.uuid4().hex[:8]}"
    created = admin.post("/api/users", json={"username": name, "password": "viewer-pass", "role": "viewer"})
    assert created.status_code == 200, created.text
    client = httpx.Client(base_url=str(admin.base_url), trust_env=False, timeout=10.0)
    login = client.post("/api/login", json={"username": name, "password": "viewer-pass"})
    assert login.status_code == 200, login.text
    return client


def _new_key(admin: httpx.Client, name: str) -> dict:
    created = admin.post("/api/keys", json={"name": name})
    assert created.status_code == 200, created.text
    return created.json()


def _chat(anon: httpx.Client, key: str, model: str) -> httpx.Response:
    return anon.post(
        "/v1/chat/completions",
        headers={"authorization": f"Bearer {key}", "x-provider": "codebuddy"},
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
    )


def _parse(text: str) -> list[tuple[str, dict[str, str], float]]:
    samples = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        assert SAMPLE_LINE.match(line), f"Prometheus 文本行不合法：{line!r}"
        head, _, value = line.rpartition(" ")
        labels: dict[str, str] = {}
        if head.endswith("}"):
            name, _, raw = head.partition("{")
            for key, val in LABEL_PAIR.findall(raw):
                labels[key] = val.replace('\\"', '"').replace("\\\\", "\\")
        else:
            name = head
        samples.append((name, labels, float(value)))
    return samples


def _value(samples, name: str, **labels) -> float:
    for metric, present, value in samples:
        if metric == name and all(present.get(k) == v for k, v in labels.items()):
            return value
    return 0.0


def _metrics(admin: httpx.Client) -> str:
    r = admin.get("/api/metrics")
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    return r.text


def test_metrics_content_type_and_format(admin: httpx.Client) -> None:
    r = admin.get("/api/metrics")
    assert r.status_code == 200
    content_type = r.headers["content-type"].lower()
    assert content_type.startswith("text/plain"), content_type
    assert "version=0.0.4" in content_type, content_type
    samples = _parse(r.text)
    assert any(name == "unified_http_requests_total" for name, _, _ in samples)
    assert any(name == "unified_request_duration_ms_bucket" for name, _, _ in samples)
    assert "# TYPE unified_request_duration_ms histogram" in r.text


def test_counts_grow_after_real_request(admin: httpx.Client, anon: httpx.Client) -> None:
    key = _new_key(admin, f"metrics-count-{uuid.uuid4().hex[:6]}")
    before = _parse(_metrics(admin))
    r = _chat(anon, key["key"], "fake-chat-1")
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    after = _parse(_metrics(admin))

    endpoint_before = _value(before, "unified_http_requests_total",
                             method="POST", endpoint="/v1/chat/completions", status="2xx",
                             provider="codebuddy")
    endpoint_after = _value(after, "unified_http_requests_total",
                            method="POST", endpoint="/v1/chat/completions", status="2xx",
                            provider="codebuddy")
    assert endpoint_after == endpoint_before + 1

    provider_before = _value(before, "unified_requests_window", provider="codebuddy", status="2xx")
    provider_after = _value(after, "unified_requests_window", provider="codebuddy", status="2xx")
    assert provider_after == provider_before + 1


def test_json_metrics_kept_on_new_path(admin: httpx.Client) -> None:
    r = admin.get("/api/metrics/json", params={"window_seconds": 3600})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert set(body) >= {"generated_at", "window_seconds", "requests", "providers", "failure_rate_limited"}
    assert body["window_seconds"] == 3600
    assert isinstance(body["requests"]["total"], int)


def test_metrics_requires_admin(admin: httpx.Client, viewer: httpx.Client) -> None:
    # admin 与 anon 是同一个 session client（conftest 里登录复用了它），这里另起无凭据 client
    fresh = httpx.Client(base_url=str(admin.base_url), trust_env=False, timeout=10.0)
    assert fresh.get("/api/metrics").status_code == 401
    assert viewer.get("/api/metrics").status_code == 403
    assert fresh.get("/api/metrics/json").status_code == 401
    assert viewer.get("/api/metrics/json").status_code == 403


def test_labels_do_not_leak_key_or_user(admin: httpx.Client) -> None:
    marker = f"label-secret-{uuid.uuid4().hex[:8]}"
    key = _new_key(admin, marker)
    text = _metrics(admin)
    assert key["key"] not in text, "密钥明文进了 Prometheus 输出"
    assert marker not in text, "密钥名进了 label"
    assert "u2a_" not in text, "密钥前缀进了 label"
    assert "key_name" not in text
    assert "username" not in text


def test_quota_remaining_alert_triggered_and_listed(admin: httpx.Client, anon: httpx.Client) -> None:
    key = _new_key(admin, f"metrics-quota-{uuid.uuid4().hex[:6]}")
    policy = admin.post(
        "/api/quota/policies",
        json={"key_id": key["id"], "window": "day", "limit_requests": 1, "thresholds": []},
    )
    assert policy.status_code == 200, policy.text
    try:
        r = _chat(anon, key["key"], "fake-chat-1")
        assert r.status_code == 200, f"{r.status_code} {r.text}"

        _metrics(admin)  # 抓取时评估告警
        rows = admin.get("/api/quota/alerts", params={"key_id": key["id"], "limit": 100}).json()["items"]
        hits = [item for item in rows if item["kind"] == "quota" and item["threshold_percent"] == 80]
        assert hits, f"剩余额度 <=20% 未落告警：{rows}"
        assert hits[0]["limit_requests"] == 1 and hits[0]["used_requests"] >= 1

        _metrics(admin)  # 再抓一次不能重复落库
        rows = admin.get("/api/quota/alerts", params={"key_id": key["id"], "limit": 100}).json()["items"]
        assert len([item for item in rows if item["kind"] == "quota" and item["threshold_percent"] == 80]) == 1
    finally:
        # 会话级共享 DB，其它测试会断言策略集合，必须清掉本测试造的策略
        admin.delete(f"/api/quota/policies/{policy.json()['id']}")


def test_provider_failure_streak_alert(admin: httpx.Client, anon: httpx.Client) -> None:
    key = _new_key(admin, f"metrics-fail-{uuid.uuid4().hex[:6]}")
    for _ in range(3):
        r = _chat(anon, key["key"], "err-500")
        assert r.status_code == 502, f"{r.status_code} {r.text}"

    _metrics(admin)
    rows = admin.get("/api/quota/alerts", params={"limit": 1000}).json()["items"]
    hits = [item for item in rows
            if item["kind"] == "provider_failure" and item["provider"] == "codebuddy"]
    assert hits, f"连续 3 次失败未落告警：{[i for i in rows if i['kind'] != 'quota'][:5]}"
    assert hits[0]["threshold_percent"] == 3
    assert hits[0]["used_requests"] == 3


def test_usage_missing_ratio_alert(admin: httpx.Client, db_path: str) -> None:
    """直接种入 200 条 usage_missing 流水：占比规则要能看见「计费在漏」。"""
    from time import time

    conn = sqlite3.connect(db_path, timeout=10)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        stamp = int(time())
        for _ in range(200):
            cur = conn.execute(
                "INSERT INTO request_logs(ts, provider, model, status) VALUES(?,?,?,?)",
                (stamp, "seed-usage", "seed-model", 200),
            )
            conn.execute(
                "INSERT INTO billing_ledger(ts, key_id, provider, model, status, note, log_id) "
                "VALUES(?,?,?,?,?,?,?)",
                (stamp, 0, "seed-usage", "seed-model", 200, "usage_missing", cur.lastrowid),
            )
        conn.commit()
    finally:
        conn.close()

    try:
        _metrics(admin)
        rows = admin.get("/api/quota/alerts", params={"limit": 1000}).json()["items"]
        hits = [item for item in rows if item["kind"] == "usage_missing_ratio"]
        assert hits, "usage_missing 占比升高未落告警"
        assert hits[0]["used_requests"] >= 200
        assert hits[0]["threshold_percent"] == 50
    finally:
        cleanup = sqlite3.connect(db_path, timeout=10)
        try:
            cleanup.execute("DELETE FROM billing_ledger WHERE provider = 'seed-usage'")
            cleanup.execute("DELETE FROM request_logs WHERE provider = 'seed-usage'")
            cleanup.commit()
        finally:
            cleanup.close()
