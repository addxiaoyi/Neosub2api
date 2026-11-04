"""日志逐条扣费：credit / credit_status 与 billing_ledger.log_id 精确关联的回归。

成功计费 / 未配价 / 断流走真实网关 + 假上游，验证 settle→log_id→/api/logs 全链路；
归属与无关联状态用直接写临时库（断言确定性）。只新增本文件，不改其他测试。
"""

from __future__ import annotations

import sqlite3
import time
import uuid

import httpx
import pytest

pytestmark = pytest.mark.logs

# 假上游固定 usage：prompt 11 / completion 7（scripts/fake_upstream.py）
# 录价输入 2.00、输出 8.00 美元/百万 token → 11×2 + 7×8 = 78 micro-USD
EXPECTED_MICRO = 78
INPUT_MICRO = 2_000_000
OUTPUT_MICRO = 8_000_000


def _tag() -> str:
    return uuid.uuid4().hex[:8]


def _new_key(admin: httpx.Client, name: str, owner: str | None = None) -> dict:
    body: dict = {"name": name}
    if owner is not None:
        body["owner_user_id"] = owner
    created = admin.post("/api/keys", json=body)
    assert created.status_code == 200, created.text
    return created.json()


def _seed_price(admin: httpx.Client, model: str) -> None:
    for token_class, micro in (("input", INPUT_MICRO), ("output", OUTPUT_MICRO)):
        r = admin.post(
            "/api/pricing/books",
            json={
                "provider": "codebuddy",
                "model": model,
                "token_class": token_class,
                "price_micro_per_1m": micro,
                "effective_from": 0,
            },
        )
        assert r.status_code in (200, 409), f"{r.status_code} {r.text}"


def _chat(anon: httpx.Client, key: str, model: str, *, stream: bool = False) -> httpx.Response:
    return anon.post(
        "/v1/chat/completions",
        headers={"authorization": f"Bearer {key}", "x-provider": "codebuddy"},
        json={"model": model, "stream": stream, "messages": [{"role": "user", "content": "hi"}]},
    )


def _wait_log_item(admin: httpx.Client, model: str, timeout: float = 5.0) -> dict:
    """落库在响应完成附近但不强依赖时序，轮到 /api/logs 出现该模型的记录为止。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = admin.get("/api/logs", params={"model": model, "limit": 50})
        assert got.status_code == 200, got.text
        for item in got.json()["items"]:
            if item["model"] == model:
                return item
        time.sleep(0.2)
    raise AssertionError(f"/api/logs 在 {timeout}s 内没出现 {model} 的记录")


def _seed_log_with_credit(db_path: str, provider: str, *, key: dict, billed: int, note: str = "") -> int:
    """直接写库：一条成功日志 + 用 log_id 精确挂上的流水（与 settle 落库同口径）。"""
    model = f"credit-model-{key['id']}-{uuid.uuid4().hex[:6]}"
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO request_logs(ts, provider, model, status, prompt_tokens, "
            "completion_tokens, latency_ms, stream, error, key_id, key_name) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), provider, model, 200, 10, 20, 100, 0, None, key["id"], key["name"]),
        )
        conn.execute(
            "INSERT INTO billing_ledger(ts, key_id, key_name, provider, model, status, "
            "billed_micro, note, log_id) VALUES(?,?,?,?,?,?,?,?,?)",
            (int(time.time()), key["id"], key["name"], provider, model, 200, billed, note,
             int(cur.lastrowid)),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def test_existing_ledger_gains_log_id_column(tmp_path):
    """老库迁移幂等：已有 billing_ledger（真实旧结构，仅缺 log_id）补列 + 索引，不动旧数据。"""
    from app.db import Database

    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.execute(
        "CREATE TABLE billing_ledger ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, key_id INTEGER, "
        "key_name TEXT NOT NULL DEFAULT '', billing_account_id INTEGER, "
        "provider TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '', "
        "status INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0, "
        "output_tokens INTEGER NOT NULL DEFAULT 0, cache_read_tokens INTEGER NOT NULL DEFAULT 0, "
        "cache_write_tokens INTEGER NOT NULL DEFAULT 0, reasoning_tokens INTEGER NOT NULL DEFAULT 0, "
        "billed_micro INTEGER NOT NULL DEFAULT 0, price_snapshot_id INTEGER, "
        "estimated INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '')"
    )
    conn.execute("INSERT INTO billing_ledger(ts, key_id, note) VALUES(1, 7, 'unpriced:input')")
    conn.commit()
    conn.close()

    Database(legacy)

    conn = sqlite3.connect(legacy)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(billing_ledger)")}
        idx = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_billing_ledger_log'"
        ).fetchone()[0]
        old = conn.execute("SELECT log_id, note FROM billing_ledger WHERE ts=1").fetchone()
    finally:
        conn.close()
    assert "log_id" in cols, "老库要补上 log_id 列"
    assert int(idx) == 1, "关联键列要有索引"
    assert old is not None and old[0] is None and old[1] == "unpriced:input", "旧流水数据不受影响"


def test_billed_request_exposes_exact_micro_credit(admin: httpx.Client, anon: httpx.Client, db_path: str):
    """成功且已计费：credit 给 micro-USD 整数，且流水必须挂在显示的那条日志上。"""
    tag = _tag()
    model = f"credit-billed-{tag}"
    key = _new_key(admin, f"credit-billed-{tag}")
    _seed_price(admin, model)

    r = _chat(anon, key["key"], model)
    assert r.status_code == 200, f"{r.status_code} {r.text}"

    item = _wait_log_item(admin, model)
    assert item["credit"] == EXPECTED_MICRO, f"11×2 + 7×8 应为 78 micro-USD，实际 {item['credit']}"
    assert item["credit_status"] == "billed"

    # 关联键必须行级精确：流水的 log_id 就是 /api/logs 显示的日志 id
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT log_id, billed_micro FROM billing_ledger WHERE model = ? ORDER BY id DESC LIMIT 1",
            (model,),
        ).fetchone()
        unlinked = conn.execute(
            "SELECT COUNT(*) AS n FROM billing_ledger WHERE model = ? AND log_id IS NULL", (model,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "计费流水应已落库"
    assert row["billed_micro"] == EXPECTED_MICRO
    assert row["log_id"] == item["id"], "流水必须精确挂在该条日志上，不允许模糊配对"
    assert unlinked["n"] == 0, "新写入的流水必须带 log_id"


def test_unpriced_request_keeps_credit_null(admin: httpx.Client, anon: httpx.Client):
    """未配价：不编金额，credit 为 null 但状态可区分。"""
    tag = _tag()
    model = f"credit-unpriced-{tag}"
    key = _new_key(admin, f"credit-unpriced-{tag}")

    # 不录价：prices.yaml 没有该模型，也没有 model '*' 通配 → settle 记 unpriced
    r = _chat(anon, key["key"], model)
    assert r.status_code == 200, f"{r.status_code} {r.text}"

    item = _wait_log_item(admin, model)
    assert item["credit"] is None, "未配价不能编造金额"
    assert item["credit_status"] == "unpriced"


def test_stream_break_is_not_billed(admin: httpx.Client, anon: httpx.Client, db_path: str):
    """断流：不写 billing_ledger，日志 credit 为 null 且状态是 not_billed。"""
    tag = _tag()
    model = f"stream-break-credit-{tag}"  # 假上游约定：model 含 stream-break 即第一帧后断流
    key = _new_key(admin, f"credit-break-{tag}")

    r = _chat(anon, key["key"], model, stream=True)
    assert r.status_code == 200, f"出帧后断流 HTTP 仍是 200：{r.status_code} {r.text}"
    assert "partial" in r.text, "断流前的分片要照常转发"

    item = _wait_log_item(admin, model)
    assert item["credit"] is None, "断流不产生金额，更不能补 0 冒充已计费"
    assert item["credit_status"] == "not_billed"

    conn = sqlite3.connect(db_path)
    try:
        ledger = conn.execute(
            "SELECT COUNT(*) FROM billing_ledger WHERE model = ?", (model,)
        ).fetchone()[0]
    finally:
        conn.close()
    assert int(ledger) == 0, f"断流请求不写 billing_ledger（有日志无流水是正常口径）：{ledger}"


def test_usage_missing_and_unlinked_statuses_distinguishable(admin: httpx.Client, db_path: str):
    """成功但无 usage / 存量无关联行：credit 都是 null，但状态必须可区分。"""
    tag = _tag()
    provider = f"credit-status-{tag}"
    key = _new_key(admin, f"credit-status-{tag}")

    _seed_log_with_credit(db_path, provider, key=key, billed=0, note="usage_missing")
    _seed_log_with_credit(db_path, provider, key=key, billed=55, note="")

    conn = sqlite3.connect(db_path)
    try:
        # 无任何流水的纯日志行（迁移前存量口径）
        conn.execute(
            "INSERT INTO request_logs(ts, provider, model, status, prompt_tokens, "
            "completion_tokens, latency_ms, stream, error, key_id, key_name) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), provider, f"credit-plain-{tag}", 200, 1, 1, 10, 0, None,
             key["id"], key["name"]),
        )
        conn.commit()
    finally:
        conn.close()

    got = admin.get("/api/logs", params={"provider": provider, "limit": 50})
    assert got.status_code == 200, got.text
    by_status = {row["credit_status"]: row for row in got.json()["items"]}
    assert set(by_status) == {"usage_missing", "billed", "unlinked"}
    assert by_status["usage_missing"]["credit"] is None
    assert by_status["billed"]["credit"] == 55, "note 为空的确定金额照常回填"
    assert by_status["unlinked"]["credit"] is None, "成功却无关联流水：如实标 unlinked，不猜金额"


def test_credit_scoped_per_key_for_non_admin(admin: httpx.Client, db_path: str):
    """跨 key 不可见：非 admin 只看得到自己 key 的日志与金额，他人 key 显式 403。"""
    tag = _tag()
    viewer_name = f"credit-viewer-{tag}"
    created = admin.post(
        "/api/users", json={"username": viewer_name, "password": "viewer-pass", "role": "viewer"}
    )
    assert created.status_code == 200, created.text
    viewer = httpx.Client(base_url=str(admin.base_url), trust_env=False, timeout=10.0)
    login = viewer.post("/api/login", json={"username": viewer_name, "password": "viewer-pass"})
    assert login.status_code == 200, login.text

    own = _new_key(admin, f"credit-own-{tag}", owner=viewer_name)
    other = _new_key(admin, f"credit-other-{tag}")
    provider = f"credit-scope-{tag}"
    try:
        _seed_log_with_credit(db_path, provider, key=own, billed=123)
        _seed_log_with_credit(db_path, provider, key=other, billed=456)

        got = viewer.get("/api/logs", params={"provider": provider, "limit": 50})
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["total"] == 1, "非 admin 只能看自己 key 的日志"
        item = body["items"][0]
        assert item["key_id"] == own["id"]
        assert item["credit"] == 123, "自己 key 的金额照常回填"
        assert item["credit_status"] == "billed"
        assert other["name"] not in got.text, "他人密钥名不能出现在响应里"

        denied = viewer.get("/api/logs", params={"key_id": other["id"]})
        assert denied.status_code == 403, denied.text
        assert "456" not in denied.json()["error"].get("message", ""), "403 不附带他人金额"

        site = admin.get("/api/logs", params={"provider": provider, "limit": 50})
        assert site.status_code == 200, site.text
        assert site.json()["total"] == 2
        assert {row["credit"] for row in site.json()["items"]} == {123, 456}
    finally:
        viewer.close()


def test_key_prefix_frozen_at_write_survives_deletion(admin: httpx.Client, anon: httpx.Client, db_path: str):
    """写时固化：删 key 后历史行仍显示掩码前缀；库存 10 位原前缀（与 /api/keys 同口径）。"""
    tag = _tag()
    model = f"prefix-frozen-{tag}"
    key = _new_key(admin, f"prefix-frozen-{tag}")
    masked = f"u2a_{key['key'][4:8]}****"

    r = _chat(anon, key["key"], model)
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    item = _wait_log_item(admin, model)
    assert item["key_prefix"] == masked, item

    conn = sqlite3.connect(db_path)
    try:
        stored = conn.execute(
            "SELECT key_prefix FROM request_logs WHERE model = ?", (model,)
        ).fetchone()
    finally:
        conn.close()
    assert stored is not None and stored[0] == key["key"][:10], f"库存原值应为 10 位前缀：{stored}"

    assert admin.delete(f"/api/keys/{key['id']}").status_code == 200
    got = admin.get("/api/logs", params={"model": model, "limit": 10})
    assert got.status_code == 200, got.text
    rows = [row for row in got.json()["items"] if row["model"] == model]
    assert rows, got.text
    assert rows[0]["key_prefix"] == masked, "删 key 后前缀不能消失或变空"


def test_legacy_null_key_prefix_falls_back_then_null(admin: httpx.Client, anon: httpx.Client, db_path: str):
    """老行（列为 NULL）：key 还在回退 join 拿掩码，删掉后给 null 而不是空串。"""
    tag = _tag()
    model = f"prefix-legacy-{tag}"
    key = _new_key(admin, f"prefix-legacy-{tag}")
    masked = f"u2a_{key['key'][4:8]}****"

    r = _chat(anon, key["key"], model)
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    _wait_log_item(admin, model)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE request_logs SET key_prefix = NULL WHERE model = ?", (model,))
        conn.commit()
    finally:
        conn.close()

    joined = admin.get("/api/logs", params={"model": model, "limit": 10})
    assert joined.status_code == 200, joined.text
    rows = [row for row in joined.json()["items"] if row["model"] == model]
    assert rows and rows[0]["key_prefix"] == masked, rows

    assert admin.delete(f"/api/keys/{key['id']}").status_code == 200
    orphaned = admin.get("/api/logs", params={"model": model, "limit": 10})
    assert orphaned.status_code == 200, orphaned.text
    rows = [row for row in orphaned.json()["items"] if row["model"] == model]
    assert rows and rows[0]["key_prefix"] is None, "解析不到必须是 null，不是空串"


def test_viewer_cannot_read_deleted_key_logs(admin: httpx.Client, anon: httpx.Client, db_path: str):
    """删 key 前后各查一次：owner 删前读自己的行，删后 403/空集且零泄露。"""
    tag = _tag()
    viewer_name = f"viewer-del-{tag}"
    created = admin.post(
        "/api/users", json={"username": viewer_name, "password": "viewer-pass", "role": "viewer"}
    )
    assert created.status_code == 200, created.text
    viewer = httpx.Client(base_url=str(admin.base_url), trust_env=False, timeout=10.0)
    try:
        login = viewer.post("/api/login", json={"username": viewer_name, "password": "viewer-pass"})
        assert login.status_code == 200, login.text

        model = f"viewer-del-{tag}"
        key = _new_key(admin, f"viewer-del-{tag}", owner=viewer_name)
        masked = f"u2a_{key['key'][4:8]}****"
        r = _chat(anon, key["key"], model)
        assert r.status_code == 200, f"{r.status_code} {r.text}"
        _wait_log_item(admin, model)

        before = viewer.get("/api/logs", params={"key_id": key["id"], "limit": 10})
        assert before.status_code == 200, before.text
        assert before.json()["total"] >= 1, before.text
        assert before.json()["items"][0]["key_prefix"] == masked, before.text

        assert admin.delete(f"/api/keys/{key['id']}").status_code == 200

        after = viewer.get("/api/logs", params={"key_id": key["id"], "limit": 10})
        assert after.status_code == 403 or (
            after.status_code == 200 and after.json()["total"] == 0
        ), f"{after.status_code} {after.text}"
        unfiltered = viewer.get("/api/logs", params={"limit": 50})
        assert unfiltered.status_code == 200, unfiltered.text
        assert unfiltered.json()["total"] == 0, unfiltered.text
        for blob, where in ((after.text, "key_id 查询"), (unfiltered.text, "全量列表")):
            for needle, label in (
                (key["key"], "明文"),
                (key["prefix"], "10 位前缀"),
                (masked, "掩码前缀"),
                (key["name"], "key_name"),
            ):
                assert needle not in blob, f"{where}泄露{label}：{needle!r}"
    finally:
        viewer.close()


def test_legacy_request_logs_gains_key_prefix_column(tmp_path):
    """幂等补列：老库缺 key_prefix，启动迁移补上，老行保持 NULL 走读取端回退。"""
    from app.db import Database

    legacy = tmp_path / "legacy_logs.db"
    conn = sqlite3.connect(legacy)
    conn.execute(
        "CREATE TABLE request_logs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, provider TEXT, model TEXT, "
        "status INTEGER NOT NULL, prompt_tokens INTEGER NOT NULL DEFAULT 0, "
        "completion_tokens INTEGER NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL DEFAULT 0, "
        "stream INTEGER NOT NULL DEFAULT 0, error TEXT)"
    )
    conn.execute("INSERT INTO request_logs(ts, model, status) VALUES(1, 'legacy-model', 200)")
    conn.commit()
    conn.close()

    Database(legacy)

    conn = sqlite3.connect(legacy)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(request_logs)")}
        stored = conn.execute(
            "SELECT key_prefix FROM request_logs WHERE model = 'legacy-model'"
        ).fetchone()
    finally:
        conn.close()
    assert "key_prefix" in cols, cols
    assert stored is not None and stored[0] is None, f"老行应保持 NULL：{stored}"
