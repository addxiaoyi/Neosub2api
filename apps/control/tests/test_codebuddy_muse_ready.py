"""CodeBuddy / Muse2API 的「凭据一到就能真用」回归。

覆盖三件事：
1. 导入前的服务商特有校验（CodeBuddy 必须 wbt_，Muse 拒绝网页 hatch_sess Cookie）；
2. 探测/同步在 7864 管理端未起时必须报「管理端进程未运行」，不是笼统的「上游不可达」；
3. Muse 经 /v1 转发的端到端链路：200 / 401 / 403 / 429 / 5xx / 超时 / 流式中断，
   并断言用量落库。

全部用真实 HTTP（conftest 起好的控制面 + 假上游），不 mock 路由。
每个用例只动自己那条 provider 配置，Finally 还原，避免污染共享会话库。
"""

from __future__ import annotations

import sqlite3
import time

import httpx
import pytest

MUSE_PATHS = {"chat_path": "/muse/v1/chat/completions", "health_path": "/muse/v1/models"}
USERS = [{"role": "user", "content": "hi"}]


@pytest.fixture(scope="module")
def bearer_key(admin: httpx.Client) -> str:
    r = admin.post("/api/keys", json={"name": "codebuddy-muse-ready"})
    assert r.status_code == 200, r.text
    return str(r.json()["key"])


def _provider(admin: httpx.Client, pid: str) -> dict:
    items = admin.get("/api/providers").json()
    row = next((item for item in items if item["id"] == pid), None)
    assert row is not None, f"provider 列表里没有 {pid}"
    return row


def _patch(admin: httpx.Client, pid: str, body: dict) -> dict:
    r = admin.patch(f"/api/providers/{pid}", json=body)
    assert r.status_code == 200, f"{pid}: {r.status_code} {r.text}"
    return r.json()


def _restore(admin: httpx.Client, snap: dict, extra_options: dict | None = None) -> None:
    options = dict(snap.get("options") or {})
    options.update(extra_options or {})
    _patch(
        admin,
        snap["id"],
        {
            "enabled": bool(snap["enabled"]),
            "base_url": snap["base_url"],
            "api_key": "***" if snap.get("has_api_key") else "",
            "options": options,
        },
    )


@pytest.fixture
def muse_enabled(admin: httpx.Client) -> None:
    """只启用 muse，不改 api_key：让 readiness 只由导入的凭据决定。"""
    snap = _provider(admin, "muse")
    _patch(admin, "muse", {"enabled": True})
    try:
        yield
    finally:
        _restore(admin, snap)


@pytest.fixture
def muse_on_fake(admin: httpx.Client, fake_base: str) -> str:
    """把 muse 指到假上游并启用；退出时按快照还原（含清空本次注入的 options）。"""
    snap = _provider(admin, "muse")
    _patch(
        admin,
        "muse",
        {
            "enabled": True,
            "base_url": fake_base,
            "api_key": "tok-muse",
            "options": MUSE_PATHS,
        },
    )
    try:
        yield fake_base
    finally:
        _restore(admin, snap, {key: "" for key in (*MUSE_PATHS, "timeout", "max_attempts")})


# ---------- 1. 导入校验 ----------


def test_codebuddy_import_rejects_non_wbt(admin: httpx.Client):
    r = admin.post(
        "/api/accounts/import",
        json={
            "provider": "codebuddy",
            "label": "bad",
            "credential_type": "api_key",
            "credential": "sk-not-an-admin-token",
        },
    )
    assert r.status_code == 400, r.text
    assert "wbt_" in r.json()["detail"]

    r = admin.post(
        "/api/accounts/import",
        json={
            "provider": "codebuddy",
            "label": "bad",
            "credential_type": "cookie",
            "credential": "a=b",
        },
    )
    assert r.status_code == 400, r.text
    assert "只接受" in r.json()["detail"]

    r = admin.post(
        "/api/accounts/import",
        json={
            "provider": "codebuddy",
            "label": "bad",
            "credential_type": "api_key",
            "credential": "",
        },
    )
    assert r.status_code == 400, r.text
    assert "设置 → API Token" in r.json()["detail"]


def test_codebuddy_import_accepts_wbt_and_masks(admin: httpx.Client):
    token = "wbt_fake_admin_token_123"
    r = admin.post(
        "/api/accounts/import",
        json={
            "provider": "codebuddy",
            "label": "ready-probe",
            "credential_type": "api_key",
            "credential": token,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert token not in r.text, "响应不能回显明文凭证"
    try:
        assert body["provider"] == "codebuddy"
        assert body.get("local_only") is True
    finally:
        admin.delete(f"/api/accounts/{body['file']}")


def test_muse_import_rejects_cookie_and_web_session(
    admin: httpx.Client, muse_on_fake: str
):
    r = admin.post(
        "/api/accounts/import",
        json={
            "provider": "muse",
            "label": "bad-cookie",
            "credential_type": "cookie",
            "credential": "hatch_sess=abc",
        },
    )
    assert r.status_code == 400, r.text
    assert "api_key" in r.json()["detail"]

    r = admin.post(
        "/api/accounts/import",
        json={
            "provider": "muse",
            "label": "bad-session",
            "credential_type": "api_key",
            "credential": "hatch_sess=abc",
        },
    )
    assert r.status_code == 400, r.text
    assert "hatch_sess" in r.json()["detail"]


def test_muse_import_accepts_api_key(admin: httpx.Client, muse_enabled: None):
    token = "tok-muse-import-plaintext-abc"
    r = admin.post(
        "/api/accounts/import",
        json={
            "provider": "muse",
            "label": "muse-key",
            "credential_type": "api_key",
            "credential": token,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert token not in r.text, "响应不能回显明文凭证"
    try:
        assert body["provider"] == "muse"
        assert _provider(admin, "muse")["readiness"] == "ready", "凭据一到就应转为 ready"
    finally:
        admin.delete(f"/api/accounts/{body['file']}")
    assert _provider(admin, "muse")["readiness"] == "needs_config", "删掉凭据后应回到待配置"


def test_import_probe_without_credential_names_prerequisite(admin: httpx.Client):
    for provider, expect in (("codebuddy", "wbt_"), ("muse", "api_key")):
        r = admin.post(
            "/api/accounts/import/probe",
            json={
                "provider": provider,
                "label": "probe",
                "credential_type": "api_key",
                "credential": "",
            },
        )
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert expect in detail, f"{provider} 的缺项说明没有点名 {expect}：{detail}"
        assert "凭证不能为空" not in detail, "不能退回通用文案"


# ---------- 2. 7864 管理端未起 ----------


def test_codebuddy_probe_and_sync_report_admin_down(admin: httpx.Client):
    snap = _provider(admin, "codebuddy")
    # 端口 1 必然拒绝连接：用它复现「管理端进程未运行」
    _patch(admin, "codebuddy", {"base_url": "http://127.0.0.1:1"})
    try:
        r = admin.post(
            "/api/accounts/import/probe",
            json={
                "provider": "codebuddy",
                "label": "probe",
                "credential_type": "api_key",
                "credential": "wbt_probe_only",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "fail", body
        assert "管理端进程未运行" in body["message"], body

        synced = admin.post("/api/accounts/sync", params={"provider": "codebuddy"})
        assert synced.status_code == 200, synced.text
        note = synced.json()["codebuddy"]["note"]
        assert "管理端进程未运行" in note, note
    finally:
        _restore(admin, snap)


def test_muse_probe_ok(admin: httpx.Client, muse_on_fake: str):
    r = admin.post(
        "/api/accounts/import/probe",
        json={
            "provider": "muse",
            "label": "probe",
            "credential_type": "api_key",
            "credential": "tok-muse",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok" and body["connected"] is True, body


def test_muse_models_come_from_upstream(admin: httpx.Client, muse_on_fake: str):
    r = admin.get("/api/models", params={"provider": "muse"})
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["count"] >= 1, payload
    assert "fake-chat-1" in r.text
    assert payload["models"][0]["source"] == "upstream"


# ---------- 3. /v1 端到端 ----------


def _max_id(db_path: str, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _wait_ledger(db_path: str, after_id: int, provider: str, timeout: float = 5.0) -> sqlite3.Row:
    deadline = time.time() + timeout
    while time.time() < deadline:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM billing_ledger WHERE id > ? AND provider = ? ORDER BY id DESC LIMIT 1",
                (after_id, provider),
            ).fetchone()
        finally:
            conn.close()
        if row is not None:
            return row
        time.sleep(0.1)
    raise AssertionError(f"billing_ledger 在 {timeout}s 内没有 {provider} 的新行")


def _wait_log(admin: httpx.Client, model: str, provider: str, after_id: int, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = admin.get("/api/logs", params={"model": model, "limit": 100})
        assert got.status_code == 200, got.text
        for item in got.json()["items"]:
            if item["model"] == model and item["provider"] == provider and item["id"] > after_id:
                return item
        time.sleep(0.2)
    raise AssertionError(f"/api/logs 在 {timeout}s 内没有 {provider}/{model} 的新记录")


def _gateway(key: str) -> dict:
    return {"authorization": f"Bearer {key}", "x-provider": "muse"}


def test_muse_v1_non_stream_records_usage(
    admin: httpx.Client, anon: httpx.Client, bearer_key: str, muse_on_fake: str, db_path: str
):
    before = _max_id(db_path, "billing_ledger")
    r = anon.post(
        "/v1/chat/completions",
        headers=_gateway(bearer_key),
        json={"model": "muse-spark", "messages": USERS},
    )
    assert r.status_code == 200, r.text
    assert "[fake] muse" in r.json()["choices"][0]["message"]["content"]
    row = _wait_ledger(db_path, before, "muse")
    assert (row["input_tokens"], row["output_tokens"]) == (11, 7)


def test_muse_v1_stream_records_usage(
    admin: httpx.Client, anon: httpx.Client, bearer_key: str, muse_on_fake: str, db_path: str
):
    before = _max_id(db_path, "billing_ledger")
    chunks: list[str] = []
    with anon.stream(
        "POST",
        "/v1/chat/completions",
        headers=_gateway(bearer_key),
        json={"model": "muse-spark", "messages": USERS, "stream": True},
    ) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line:
                chunks.append(line)
    assert any("[DONE]" in line for line in chunks), "流式必须以 [DONE] 收尾"
    row = _wait_ledger(db_path, before, "muse")
    assert (row["input_tokens"], row["output_tokens"]) == (11, 7)


@pytest.mark.parametrize(
    ("upstream", "expected"),
    [(401, 401), (403, 403), (429, 429), (500, 502), (503, 503)],
)
def test_muse_v1_upstream_errors_map_and_log(
    admin: httpx.Client,
    anon: httpx.Client,
    bearer_key: str,
    muse_on_fake: str,
    db_path: str,
    upstream: int,
    expected: int,
):
    # 网关的既有映射：403 归一成 401（鉴权失败），5xx 归一成 502（上游不可用）
    model = f"muse-spark-err-{upstream}"
    _patch(admin, "muse", {"options": {"max_attempts": "1"}})
    before = _max_id(db_path, "request_logs")
    r = anon.post(
        "/v1/chat/completions",
        headers=_gateway(bearer_key),
        json={"model": model, "messages": USERS},
    )
    assert r.status_code == expected, r.text
    body = r.json()
    assert isinstance(body.get("error"), dict) and body["error"].get("message"), body
    assert body["error"].get("upstream_status") == upstream

    # 日志与响应必须同一个状态码：401/403 原样透传，5xx 按网关约定归一
    item = _wait_log(admin, model, "muse", before)
    assert item["status"] == expected
    assert item["error"]


def test_failure_budget_isolated_per_provider(
    admin: httpx.Client, anon: httpx.Client, bearer_key: str, muse_on_fake: str
):
    """一把 key 在 codebuddy 上失败 20 次，不应连坐 muse 的成功请求。"""
    headers = {"authorization": f"Bearer {bearer_key}", "x-provider": "codebuddy"}
    for _ in range(20):
        failed = anon.post(
            "/v1/chat/completions",
            headers=headers,
            json={"model": "fake-chat-1-err-400", "messages": USERS},
        )
        assert failed.status_code == 400, failed.text

    ok = anon.post(
        "/v1/chat/completions",
        headers=_gateway(bearer_key),
        json={"model": "muse-spark", "messages": USERS},
    )
    assert ok.status_code == 200, f"其他 provider 被连坐：{ok.status_code} {ok.text}"
    assert "[fake] muse" in ok.json()["choices"][0]["message"]["content"]


def test_muse_v1_timeout_maps_to_502(
    admin: httpx.Client, anon: httpx.Client, bearer_key: str, muse_on_fake: str, db_path: str
):
    _patch(admin, "muse", {"options": {"timeout": "0.3", "max_attempts": "1"}})
    model = "muse-spark-slow"
    before = _max_id(db_path, "request_logs")
    r = anon.post(
        "/v1/chat/completions",
        headers=_gateway(bearer_key),
        json={"model": model, "messages": USERS},
    )
    assert r.status_code in (502, 504), r.text
    item = _wait_log(admin, model, "muse", before)
    assert item["status"] in (502, 504)
    assert item["error"]


def test_muse_v1_stream_break_logs_error(
    admin: httpx.Client, anon: httpx.Client, bearer_key: str, muse_on_fake: str, db_path: str
):
    model = "muse-spark-stream-break"
    before = _max_id(db_path, "request_logs")
    pieces: list[str] = []
    with anon.stream(
        "POST",
        "/v1/chat/completions",
        headers=_gateway(bearer_key),
        json={"model": model, "messages": USERS, "stream": True},
    ) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line:
                pieces.append(line)
    assert any("partial" in line for line in pieces), pieces

    item = _wait_log(admin, model, "muse", before)
    assert item["error"], "流式中断必须落一条带 error 的请求日志"
