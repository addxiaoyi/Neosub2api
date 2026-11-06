"""2-D7 端到端：提交 → 轮询 → 终态 → 计费（假上游，不碰真实凭据）。

- sub2api 上游只有 POST /v1/videos：创建响应即终态，轮询改读本地台账并回 billed_micro。
- muse 上游有按 id 查询：轮询推进台账，首次终态那一次按 seconds 计费且只计一次。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator

import httpx
import pytest

HITS: list[dict[str, Any]] = []
POLLS = {"vt-2": 0}


class _StubVideo(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:  # 静音 stub 访问日志
        pass

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 协议
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {}
        HITS.append({"method": "POST", "path": self.path, "model": body.get("model")})
        if self.path != "/v1/videos":
            self._json(404, {"error": {"message": f"stub 未实现 {self.path}"}})
            return
        if body.get("model") == "fake-video-1":
            # 创建即终态：sub2api 只能从创建响应里拿到状态与时长
            self._json(200, {"id": "vt-1", "object": "video.task",
                             "status": "completed", "seconds": 4})
        else:
            POLLS["vt-2"] = 0
            self._json(200, {"id": "vt-2", "object": "video.task", "status": "queued"})

    def do_GET(self) -> None:  # noqa: N802
        HITS.append({"method": "GET", "path": self.path, "model": None})
        if self.path != "/v1/videos/vt-2":
            self._json(404, {"error": {"message": f"stub 未实现 {self.path}"}})
            return
        POLLS["vt-2"] += 1
        if POLLS["vt-2"] == 1:
            self._json(200, {"id": "vt-2", "status": "running"})
        else:
            self._json(200, {"id": "vt-2", "status": "succeeded", "seconds": 6})


@pytest.fixture(scope="module")
def stub_video() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubVideo)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


# 整个套件共用一份库：账本断言必须按本用例的 key 归集，不能混进前面模块的 muse 流水
LEDGER_KEY_NAME = f"ledger-{uuid.uuid4().hex[:6]}"


@pytest.fixture(scope="module")
def ledger_key(admin: httpx.Client) -> str:
    created = admin.post("/api/keys", json={"name": LEDGER_KEY_NAME})
    assert created.status_code == 200, created.text
    return str(created.json()["key"])


@pytest.fixture(scope="module")
def video_prices(admin: httpx.Client, ledger_key: str) -> None:
    """按时长计价：$0.1/秒 = 100000 micro-USD/秒，provider 精确命中优先于全局 *。"""
    for provider in ("sub2api", "muse"):
        created = admin.post(
            "/api/pricing/rules",
            json={
                "provider": provider,
                "model": "*",
                "kind": "per_second",
                "unit_micro": 100000,
                "effective_from": 0,
                "note": "2-D7 端到端用例：测试价，不代表真实价目",
            },
        )
        assert created.status_code in (200, 201, 409), created.text


@pytest.fixture
def sub2api_on_video_stub(admin: httpx.Client, stub_video: str) -> Iterator[None]:
    gid = f"g-ledger-{uuid.uuid4().hex[:6]}"
    group = admin.post("/api/sub2api/groups", json={"id": gid, "name": "ledger-e2e"})
    assert group.status_code == 200, group.text
    account = admin.post(
        "/api/sub2api/accounts",
        json={"group_id": gid, "name": "a-ledger", "upstream_base": stub_video, "api_key": "stub-key"},
    )
    assert account.status_code == 200, account.text
    before = next(p for p in admin.get("/api/providers").json() if p["id"] == "sub2api")
    patched = admin.patch("/api/providers/sub2api", json={"enabled": True, "options": {"group": gid}})
    assert patched.status_code == 200, patched.text
    try:
        yield
    finally:
        admin.patch(
            "/api/providers/sub2api",
            json={"enabled": bool(before["enabled"]),
                  "options": {"group": str((before.get("options") or {}).get("group", "default"))}},
        )
        listed = admin.get("/api/sub2api/accounts", params={"group_id": gid})
        if listed.status_code == 200:
            for row in listed.json():
                admin.delete(f"/api/sub2api/accounts/{row.get('id')}")
        admin.delete(f"/api/sub2api/groups/{gid}")


@pytest.fixture
def muse_on_video_stub(admin: httpx.Client, stub_video: str) -> Iterator[None]:
    before = next(p for p in admin.get("/api/providers").json() if p["id"] == "muse")
    patched = admin.patch(
        "/api/providers/muse",
        json={"enabled": True, "base_url": stub_video, "api_key": "stub-key"},
    )
    assert patched.status_code == 200, patched.text
    try:
        yield
    finally:
        admin.patch(
            "/api/providers/muse",
            json={"enabled": bool(before["enabled"]), "base_url": before["base_url"],
                  "api_key": "***" if before["has_api_key"] else ""},
        )


def _auth(key: str, provider: str) -> dict[str, str]:
    return {"authorization": f"Bearer {key}", "x-provider": provider}


def _task(db_path: str, provider: str, task_id: str) -> sqlite3.Row | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT status, seconds, billed_micro FROM video_tasks "
            "WHERE provider = ? AND upstream_task_id = ?",
            (provider, task_id),
        ).fetchone()
    finally:
        conn.close()


def _billed_sum(db_path: str, provider: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(billed_micro), 0) FROM billing_ledger "
            "WHERE provider = ? AND key_name = ?",
            (provider, LEDGER_KEY_NAME),
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def test_sub2api_create_terminal_ledger_and_per_second_billing(
    anon: httpx.Client, ledger_key: str, db_path: str, video_prices: None, sub2api_on_video_stub: None
) -> None:
    HITS.clear()
    created = anon.post(
        "/v1/videos",
        headers=_auth(ledger_key, "sub2api"),
        json={"model": "fake-video-1", "prompt": "a spinning cube"},
    )
    assert created.status_code == 200, created.text
    assert created.json()["id"] == "vt-1"

    row = _task(db_path, "sub2api", "vt-1")
    assert row is not None, "创建成功后必须登记台账"
    assert row["status"] == "succeeded", "创建响应已终态，台账必须同步定格"
    assert row["seconds"] == 4

    polled = anon.get("/v1/videos/vt-1", headers=_auth(ledger_key, "sub2api"))
    assert polled.status_code == 200, polled.text
    ledger = polled.json()
    assert ledger["status"] == "succeeded"
    assert ledger["source"] == "local_ledger", "sub2api 无上游查询，必须标注来自本地台账"
    assert ledger["seconds"] == 4
    assert ledger["billed_micro"] == 4 * 100000
    # 本地台账兜底：不再向上游发按 id 查询
    assert not any(hit["path"] == "/v1/videos/vt-1" for hit in HITS)


def test_muse_poll_advances_ledger_and_bills_once(
    anon: httpx.Client, ledger_key: str, db_path: str, video_prices: None, muse_on_video_stub: None
) -> None:
    HITS.clear()
    created = anon.post(
        "/v1/videos",
        headers=_auth(ledger_key, "muse"),
        json={"model": "muse-video", "prompt": "a cube spinning"},
    )
    assert created.status_code == 200, created.text
    assert created.json()["id"] == "vt-2"

    row = _task(db_path, "muse", "vt-2")
    assert row is not None
    assert row["status"] == "pending"

    first = anon.get("/v1/videos/vt-2", headers=_auth(ledger_key, "muse"))
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "running"
    row = _task(db_path, "muse", "vt-2")
    assert row is not None and row["status"] == "running"
    assert _billed_sum(db_path, "muse") == 0, "未到终态不计费"

    second = anon.get("/v1/videos/vt-2", headers=_auth(ledger_key, "muse"))
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "succeeded"
    row = _task(db_path, "muse", "vt-2")
    assert row is not None
    assert row["status"] == "succeeded"
    assert row["seconds"] == 6
    assert row["billed_micro"] == 6 * 100000
    assert _billed_sum(db_path, "muse") == 6 * 100000

    # 第三次轮询：终态已定格，不再产生第二笔按秒计费
    third = anon.get("/v1/videos/vt-2", headers=_auth(ledger_key, "muse"))
    assert third.status_code == 200, third.text
    assert third.json()["status"] == "succeeded"
    row = _task(db_path, "muse", "vt-2")
    assert row is not None and row["billed_micro"] == 6 * 100000
    assert _billed_sum(db_path, "muse") == 6 * 100000
