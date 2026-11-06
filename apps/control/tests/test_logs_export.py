"""日志导出 CSV 的回归测试。

数据直接写临时 SQLite（db_path fixture），不靠网关先跑流量，这样筛选/行数上限
的断言是确定性的。覆盖：权限、筛选语义、六类凭据脱敏、CSV 公式注入、行数上限提示。
"""

from __future__ import annotations

import csv
import io
import sqlite3
import time
import uuid

import httpx
import pytest

from app.routers.logs import EXPORT_COLUMNS, MAX_EXPORT_ROWS

pytestmark = pytest.mark.logs

# 六类凭据各一条明文，全部必须从导出内容里消失
JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ1c2VyMTIzNDU2Nzg5MCJ9.c2lnbmF0dXJlLXNpZ24"
SECRETS = (
    "u2a_secret12345678",                 # 自有前缀
    "Bearer tok_1234567890abcdef",        # Bearer
    JWT,                                  # JWT 三段式
    "?key=querysecret123456&lang=zh",     # URL 查询参数
    "sk-live-abcd1234",                   # OpenAI 风格
    "ghp_" + "A1b2C3d4E5f6G3h4i5j6k7l8",  # 厂商前缀
)


def _login_viewer(admin: httpx.Client) -> tuple[httpx.Client, str]:
    name = f"export-viewer-{uuid.uuid4().hex[:8]}"
    created = admin.post(
        "/api/users", json={"username": name, "password": "viewer-pass", "role": "viewer"}
    )
    assert created.status_code == 200, created.text
    client = httpx.Client(base_url=str(admin.base_url), trust_env=False, timeout=10.0)
    login = client.post("/api/login", json={"username": name, "password": "viewer-pass"})
    assert login.status_code == 200, login.text
    return client, name


def _new_key(admin: httpx.Client, name: str, owner: str | None = None) -> dict:
    body: dict = {"name": name}
    if owner is not None:
        body["owner_user_id"] = owner
    created = admin.post("/api/keys", json=body)
    assert created.status_code == 200, created.text
    return created.json()


@pytest.fixture()
def probe(db_path: str):
    """建一个独立 provider 标签的日志批次；用例结束整批删除，不污染后续文件。"""
    provider = f"export-probe-{uuid.uuid4().hex[:8]}"
    base = int(time.time())

    def seed(rows: list[dict]) -> list[int]:
        payload = [
            (
                base - index,
                provider,
                row.get("model", "export-model"),
                row["status"],
                row.get("prompt_tokens", 10),
                row.get("completion_tokens", 20),
                row.get("latency_ms", 100),
                row.get("stream", 0),
                row.get("error"),
                row.get("key_id"),
                row.get("key_name", ""),
            )
            for index, row in enumerate(rows)
        ]
        conn = sqlite3.connect(db_path)
        try:
            conn.executemany(
                "INSERT INTO request_logs(ts, provider, model, status, prompt_tokens, "
                "completion_tokens, latency_ms, stream, error, key_id, key_name) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                payload,
            )
            conn.commit()
            ids = [row[0] for row in conn.execute(
                "SELECT id FROM request_logs WHERE provider = ? ORDER BY id", (provider,)
            )]
        finally:
            conn.close()
        return ids

    yield provider, base, seed

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM request_logs WHERE provider = ?", (provider,))
        conn.commit()
    finally:
        conn.close()


def _records(resp: httpx.Response) -> list[dict[str, str]]:
    """按 UTF-8 BOM 解码后解析；表头必须与后端 EXPORT_COLUMNS 一致。"""
    assert resp.content.startswith(b"\xef\xbb\xbf"), "缺 UTF-8 BOM，Excel 打开会乱码"
    parsed = list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))
    header, *data = parsed
    assert header == list(EXPORT_COLUMNS), header
    return [dict(zip(header, row)) for row in data]


def test_export_open_to_viewer_and_keeps_json_list(admin: httpx.Client, probe) -> None:
    provider, _, seed = probe
    # 匿名日志（key_id 为空）无法归属，普通用户导出应为空表头
    seed([{"status": 200}, {"status": 502, "error": "upstream"}])

    viewer, _ = _login_viewer(admin)
    try:
        opened = viewer.get("/api/logs/export", params={"provider": provider})
        assert opened.status_code == 200, opened.text
        assert opened.headers["content-type"].startswith("text/csv")
        assert _records(opened) == []
    finally:
        viewer.close()

    got = admin.get("/api/logs/export", params={"provider": provider})
    assert got.status_code == 200, got.text
    assert got.headers["content-type"].startswith("text/csv")
    assert "attachment" in got.headers["content-disposition"]
    assert got.headers["cache-control"] == "no-store"
    assert len(_records(got)) == 2

    # 列表契约不变：仍是 JSON 的 {total, items}，没有被导出改动带偏
    listed = admin.get("/api/logs", params={"provider": provider}).json()
    assert listed["total"] == 2 and all("id" in item for item in listed["items"])


def test_viewer_export_scoped_and_foreign_key_denied(admin: httpx.Client, probe) -> None:
    provider, _, seed = probe
    viewer, name = _login_viewer(admin)
    tag = uuid.uuid4().hex[:8]
    own = _new_key(admin, f"own-{tag}", owner=name)
    other = _new_key(admin, f"admin-{tag}")
    leaked = "u2a_exportsecret123456"
    seed([
        {"status": 200, "key_id": own["id"], "key_name": own["name"]},
        {"status": 500, "key_id": own["id"], "key_name": own["name"], "error": "upstream 401: " + leaked},
        {"status": 200, "key_id": other["id"], "key_name": other["name"]},
        {"status": 200, "key_id": other["id"], "key_name": other["name"]},
    ])
    try:
        got = viewer.get("/api/logs/export", params={"provider": provider})
        assert got.status_code == 200, got.text
        rows = _records(got)
        assert len(rows) == 2, rows
        assert {row["key_id"] for row in rows} == {str(own["id"])}
        assert {row["key_name"] for row in rows} == {own["name"]}
        assert other["name"] not in got.text
        # 列表与导出同一套归属口径
        assert viewer.get("/api/logs", params={"provider": provider}).json()["total"] == 2

        # 管理员视图不变：四条都能导出
        assert len(_records(admin.get("/api/logs/export", params={"provider": provider}))) == 4

        # 显式传他人 key_id：403，不退化成空集
        denied = viewer.get(
            "/api/logs/export", params={"provider": provider, "key_id": other["id"]}
        )
        assert denied.status_code == 403, denied.text
        assert denied.json()["error"]["code"] == "insufficient_role"

        # 审计：普通用户在自己的审计页能看到成功与拒绝两条；只含元数据，不含正文
        own_audit = viewer.get("/api/audit-logs", params={"action": "logs.export"}).json()
        assert own_audit["total"] >= 2, own_audit
        assert {item["actor"] for item in own_audit["items"]} == {name}
        details = [item["detail"] for item in own_audit["items"]]
        ok_detail = next(detail for detail in details if "result=ok" in detail)
        denied_detail = next(detail for detail in details if "result=denied" in detail)
        assert "rows=2" in ok_detail and "matched=2" in ok_detail
        assert "duration_ms=" in ok_detail
        assert "status=403" in denied_detail and f"key_id={other['id']}" in denied_detail
        assert leaked not in ok_detail and leaked not in denied_detail

        # admin 审计页可按动作 + 执行者查到同一批记录
        admin_audit = admin.get(
            "/api/audit-logs", params={"action": "logs.export", "actor": name}
        ).json()
        assert admin_audit["total"] >= 2
        assert {item["action"] for item in admin_audit["items"]} == {"logs.export"}
    finally:
        viewer.close()
        admin.delete(f"/api/keys/{own['id']}")
        admin.delete(f"/api/keys/{other['id']}")


def test_export_applies_the_same_filters(admin: httpx.Client, probe) -> None:
    provider, base, seed = probe
    seed([
        {"status": 200, "model": "alpha-model", "key_id": 7, "key_name": "k7"},
        {"status": 200, "model": "alpha-model", "key_id": 8, "key_name": "k8", "stream": 1},
        {"status": 502, "model": "beta-model", "key_id": 8, "key_name": "k8", "error": "upstream"},
    ])

    def count(**params) -> int:
        return admin.get("/api/logs", params={"provider": provider, **params}).json()["total"]

    by_status = admin.get("/api/logs/export", params={"provider": provider, "status": "error"})
    rows = _records(by_status)
    assert len(rows) == count(status="error") == 1
    assert rows[0]["status"] == "502"

    by_model = _records(admin.get("/api/logs/export", params={"provider": provider, "model": "alpha"}))
    assert len(by_model) == count(model="alpha") == 2
    assert {row["model"] for row in by_model} == {"alpha-model"}

    by_key = _records(admin.get("/api/logs/export", params={"provider": provider, "key_id": 8}))
    assert len(by_key) == count(key_id=8) == 2
    assert {row["key_name"] for row in by_key} == {"k8"}

    # 时间窗同样收窄：只留 ts <= base-2 的最早一条
    ranged = _records(admin.get("/api/logs/export", params={"provider": provider, "end": base - 2}))
    assert len(ranged) == 1 and ranged[0]["status"] == "502"


def test_export_masks_all_six_credential_forms(admin: httpx.Client, probe) -> None:
    provider, _, seed = probe
    seed([{"status": 502, "error": "upstream 401: " + " | ".join(SECRETS)}])

    got = admin.get("/api/logs/export", params={"provider": provider})
    assert got.status_code == 200, got.text
    body = got.content.decode("utf-8-sig")
    records = _records(got)

    for secret in SECRETS:
        assert secret not in body, f"导出内容泄露明文：{secret}"
    error_cell = records[0]["error"]
    # 语义要留着，掩码形态要能辨认
    assert "upstream 401" in error_cell
    assert "***" in error_cell
    assert "u2a_" in error_cell and "5678" in error_cell
    assert "?key=***" in error_cell
    assert "lang=zh" in error_cell


def test_export_neutralizes_csv_formula_injection(admin: httpx.Client, probe) -> None:
    provider, _, seed = probe
    payloads = ["=SUM(A1:A9)", "+cmd|' /C calc'!A0", "-2+3", "@SUM(1,1)"]
    seed([
        {"status": 200, "model": payload, "key_name": payload, "error": payload}
        for payload in payloads
    ])

    records = _records(admin.get("/api/logs/export", params={"provider": provider}))
    assert len(records) == len(payloads)

    for record in records:
        for value in record.values():
            assert not value.startswith(("=", "+", "-", "@")), f"未防注入的单元格：{value!r}"
    joined = "\n".join(value for record in records for value in record.values())
    for payload in payloads:
        # 加了前导单引号后原文仍在，只是被强制成文本
        assert f"'{payload}" in joined


def test_export_over_limit_reports_instead_of_truncating(admin: httpx.Client, probe) -> None:
    provider, base, seed = probe
    viewer, name = _login_viewer(admin)
    tag = uuid.uuid4().hex[:8]
    own = _new_key(admin, f"limit-{tag}", owner=name)
    try:
        seed([
            {"status": 200, "key_id": own["id"], "key_name": own["name"]}
            for _ in range(MAX_EXPORT_ROWS + 1)
        ])

        got = viewer.get("/api/logs/export", params={"provider": provider})
        assert got.status_code == 400, got.text
        detail = got.json()["detail"]
        assert str(MAX_EXPORT_ROWS) in detail
        assert str(MAX_EXPORT_ROWS + 1) in detail
        assert "上限" in detail
        assert got.headers["content-type"].startswith("application/json")

        # 超限也落审计：命中数与上限都记下，普通用户自己能查到
        audit = viewer.get("/api/audit-logs", params={"action": "logs.export"}).json()
        rejected = audit["items"][0]["detail"]
        assert "result=too_many" in rejected
        assert f"matched={MAX_EXPORT_ROWS + 1}" in rejected
        assert f"limit={MAX_EXPORT_ROWS}" in rejected

        # 用时间窗收窄到上限以内就正常给 CSV，证明拒绝的是「超量」而不是导出本身坏了
        ok = viewer.get(
            "/api/logs/export",
            params={"provider": provider, "end": base - 100},
        )
        assert ok.status_code == 200, ok.text
        assert len(_records(ok)) == MAX_EXPORT_ROWS - 99
    finally:
        viewer.close()
        admin.delete(f"/api/keys/{own['id']}")
