"""A 组媒体子工程端到端：图片 / 视频 / 文件 / 可编辑任务 / 队列。

gptGrok2api 的媒体端是独立子工程，这里用本地 stub 复刻它的路由与响应形状：
- 未配 options.media_base 时能力位为假，必须 501；
- 配了之后图片直通、视频走 2-D7 台账并按秒计费一次、文件三连、可编辑任务按 owner 隔离；
- 队列两条长任务能看到位次、能取消，终态再取消 404。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

HITS: list[dict[str, Any]] = []
VIDEO_POLLS = {"gv-1": 0}
EDITED = {"n": 0}


class _StubMedia(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:  # 静音 stub 访问日志
        pass

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("content-length") or 0))

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 协议
        raw = self._body()
        HITS.append({"method": "POST", "path": self.path, "auth": self.headers.get("authorization")})
        if self.path == "/v1/images/generations":
            body = json.loads(raw or b"{}")
            self._json(200, {"created": 123, "data": [{"url": "http://stub/img-1.png",
                                                       "revised_prompt": body.get("prompt")}]})
            return
        if self.path == "/v1/images/edits":
            self._json(200, {"created": 124, "data": [{"b64_json": "ZWRpdA=="}]})
            return
        if self.path == "/v1/videos":
            # 上游只收 multipart；网关负责 JSON -> 字段搬运，这里验关键字段确实到了
            text = raw.decode("latin-1")
            assert 'name="model"' in text and 'name="prompt"' in text, "视频字段必须按 multipart 送到上游"
            self._json(200, {"id": "gv-1", "object": "video", "status": "queued", "seconds": "5"})
            return
        if self.path == "/v1/files":
            self._json(200, {"id": "file-1", "object": "file", "bytes": len(raw),
                             "filename": "a.txt", "purpose": "assistants",
                             "uri": "http://stub/v1/files/file-1/content"})
            return
        if self.path == "/v1/editable-file-tasks":
            body = json.loads(raw or b"{}")
            EDITED["n"] += 1
            tid = f"et-q{EDITED['n']}" if "queue" in str(body.get("prompt") or "") else f"et-{EDITED['n']}"
            self._json(202, {"id": tid, "taskId": tid, "status": "queued",
                             "kind": body.get("kind"), "created_at": 1700000000})
            return
        self._json(404, {"error": {"message": f"stub 未实现 POST {self.path}"}})

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        HITS.append({"method": "GET", "path": self.path, "auth": self.headers.get("authorization")})
        if parsed.path == "/v1/videos/gv-1":
            VIDEO_POLLS["gv-1"] += 1
            if VIDEO_POLLS["gv-1"] == 1:
                self._json(200, {"id": "gv-1", "status": "in_progress"})
            else:
                self._json(200, {"id": "gv-1", "status": "completed", "seconds": "5"})
            return
        if parsed.path == "/v1/files":
            self._json(200, {"items": [{"id": "file-1", "object": "file", "bytes": 5,
                                        "filename": "a.txt"}], "videos": []})
            return
        if parsed.path == "/v1/editable-file-tasks":
            ids = parse_qs(parsed.query).get("ids", [""])[0]
            wanted = [item for item in ids.split(",") if item]
            items = []
            missing = []
            for tid in wanted or ["et-1"]:
                if tid.startswith("et-q"):
                    items.append({"id": tid, "taskId": tid, "status": "queued"})
                elif tid == "et-1":
                    items.append({"id": tid, "taskId": tid, "status": "completed",
                                  "result": {"download_url": "http://stub/et-1.pptx"}})
                else:
                    missing.append(tid)
            self._json(200, {"items": items, "missing_ids": missing})
            return
        if parsed.path.startswith("/v1/editable-file-tasks/"):
            tid = parsed.path.rsplit("/", 1)[-1]
            status = "queued" if tid.startswith("et-q") else "completed"
            payload: dict[str, Any] = {"id": tid, "taskId": tid, "status": status}
            if status == "completed":
                payload["result"] = {"download_url": f"http://stub/{tid}.pptx"}
            self._json(200, payload)
            return
        self._json(404, {"error": {"message": f"stub 未实现 GET {self.path}"}})

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        HITS.append({"method": "DELETE", "path": self.path, "auth": self.headers.get("authorization")})
        if parsed.path == "/v1/files":
            fid = parse_qs(parsed.query).get("id", [""])[0]
            if fid == "file-1":
                self._json(200, {"id": fid, "deleted": True})
            else:
                self._json(404, {"error": {"message": "file not found", "code": "file_not_found"}})
            return
        self._json(404, {"error": {"message": f"stub 未实现 DELETE {self.path}"}})


@pytest.fixture(scope="module")
def stub_media() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubMedia)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


KEY_NAME = f"media-{uuid.uuid4().hex[:6]}"
OTHER_NAME = f"media-other-{uuid.uuid4().hex[:6]}"


@pytest.fixture(scope="module", autouse=True)
def gptgrok_enabled(admin: httpx.Client) -> Iterator[None]:
    """gptgrok 默认停用；本文件要先把它打开，同时保证能力位还没配媒体地址。"""
    before = next(p for p in admin.get("/api/providers").json() if p["id"] == "gptgrok")
    admin.patch(
        "/api/providers/gptgrok",
        json={"enabled": True, "options": {"media_base": "", "media_api_key": ""}},
    )
    try:
        yield
    finally:
        admin.patch(
            "/api/providers/gptgrok",
            json={"enabled": bool(before["enabled"]),
                  "options": {"media_base": "", "media_api_key": ""}},
        )


@pytest.fixture(scope="module")
def media_key(admin: httpx.Client) -> str:
    created = admin.post("/api/keys", json={"name": KEY_NAME})
    assert created.status_code == 200, created.text
    return str(created.json()["key"])


@pytest.fixture(scope="module")
def other_key(admin: httpx.Client) -> str:
    created = admin.post("/api/keys", json={"name": OTHER_NAME})
    assert created.status_code == 200, created.text
    return str(created.json()["key"])


@pytest.fixture(scope="module")
def media_prices(admin: httpx.Client, media_key: str) -> None:
    created = admin.post(
        "/api/pricing/rules",
        json={"provider": "gptgrok", "model": "*", "kind": "per_second",
              "unit_micro": 100000, "effective_from": 0,
              "note": "A 组用例测试价，不代表真实价目"},
    )
    assert created.status_code in (200, 201, 409), created.text


@pytest.fixture
def media_on(admin: httpx.Client, stub_media: str) -> Iterator[None]:
    before = next(p for p in admin.get("/api/providers").json() if p["id"] == "gptgrok")
    patched = admin.patch(
        "/api/providers/gptgrok",
        json={"enabled": True, "options": {"media_base": stub_media, "media_api_key": "stub-media-key"}},
    )
    assert patched.status_code == 200, patched.text
    try:
        yield
    finally:
        # registry options 是合并写入，恢复时把地址置空才会重新收起媒体能力位
        admin.patch(
            "/api/providers/gptgrok",
            json={"enabled": bool(before["enabled"]),
                  "options": {"media_base": "", "media_api_key": ""}},
        )


def _auth(key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {key}", "x-provider": "gptgrok"}


def _ledger_row(db_path: str, task_id: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT status, seconds, billed_micro FROM video_tasks "
            "WHERE provider = 'gptgrok' AND upstream_task_id = ?",
            (task_id,),
        ).fetchone()
    finally:
        conn.close()


def _billed_sum(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(billed_micro), 0) FROM billing_ledger "
            "WHERE provider = 'gptgrok' AND key_name = ?",
            (KEY_NAME,),
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def test_media_capability_is_off_without_media_base(anon: httpx.Client, media_key: str) -> None:
    HITS.clear()
    resp = anon.post(
        "/v1/images/generations", headers=_auth(media_key),
        json={"model": "gpt-5", "prompt": "a cat"},
    )
    assert resp.status_code == 501, resp.text
    assert resp.json()["error"]["code"] == "capability_unsupported"
    assert HITS == [], "能力位为假时不得外呼媒体子工程"


def test_images_generate_and_edit_passthrough(
    anon: httpx.Client, admin: httpx.Client, media_key: str, media_on: None
) -> None:
    HITS.clear()
    row = next(p for p in admin.get("/api/providers").json() if p["id"] == "gptgrok")
    caps = row["capabilities"]
    assert caps["images"] is True and caps["videos"] is True and caps["media"] is True
    created = anon.post(
        "/v1/images/generations", headers=_auth(media_key),
        json={"model": "gpt-5", "prompt": "a cat", "n": 1},
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["data"][0]["url"] == "http://stub/img-1.png"
    assert body["data"][0]["revised_prompt"] == "a cat"

    edited = anon.post(
        "/v1/images/edits", headers=_auth(media_key),
        json={"model": "gpt-5", "prompt": "make it red", "image": "http://x/1.png"},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["data"][0]["b64_json"] == "ZWRpdA=="
    assert all(hit["auth"] == "Bearer stub-media-key" for hit in HITS)


def test_video_polls_to_terminal_and_bills_once(
    anon: httpx.Client, media_key: str, db_path: str, media_prices: None, media_on: None
) -> None:
    HITS.clear()
    VIDEO_POLLS["gv-1"] = 0
    created = anon.post(
        "/v1/videos", headers=_auth(media_key),
        json={"model": "gpt-5", "prompt": "a spinning cube", "seconds": 5},
    )
    assert created.status_code == 200, created.text
    assert created.json()["id"] == "gv-1"
    row = _ledger_row(db_path, "gv-1")
    assert row is not None and row["status"] == "pending" and row["seconds"] == 5

    first = anon.get("/v1/videos/gv-1", headers=_auth(media_key))
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "in_progress"
    row = _ledger_row(db_path, "gv-1")
    assert row is not None and row["status"] == "running"
    assert _billed_sum(db_path) == 0, "未到终态不计费"

    second = anon.get("/v1/videos/gv-1", headers=_auth(media_key))
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "completed"
    row = _ledger_row(db_path, "gv-1")
    assert row is not None and row["status"] == "succeeded" and row["seconds"] == 5
    assert row["billed_micro"] == 5 * 100000
    assert _billed_sum(db_path) == 5 * 100000

    anon.get("/v1/videos/gv-1", headers=_auth(media_key))
    assert _billed_sum(db_path) == 5 * 100000, "终态重复轮询不得二次计费"

    rejected = anon.post(
        "/v1/videos", headers=_auth(media_key),
        json={"model": "gpt-5", "prompt": "x", "input_reference": ["a.png"]},
    )
    assert rejected.status_code == 400, rejected.text
    assert rejected.json()["error"]["code"] == "input_reference_unsupported"


def test_files_upload_list_delete(
    anon: httpx.Client, media_key: str, media_on: None
) -> None:
    HITS.clear()
    uploaded = anon.post(
        "/v1/files", headers={"authorization": f"Bearer {media_key}"},
        files={"file": ("a.txt", b"hello", "text/plain")},
        data={"purpose": "assistants"},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["id"] == "file-1"

    listed = anon.get("/v1/files", headers=_auth(media_key))
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["id"] == "file-1"

    deleted = anon.delete("/v1/files/file-1", headers=_auth(media_key))
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted"] is True

    missing = anon.delete("/v1/files/file-404", headers=_auth(media_key))
    assert missing.status_code == 404, missing.text
    assert missing.json()["error"]["code"] == "file_not_found"

    wrong_type = anon.post(
        "/v1/files", headers={"authorization": f"Bearer {media_key}", "content-type": "application/json"},
        json={"file": "x"},
    )
    assert wrong_type.status_code == 415, wrong_type.text
    assert wrong_type.json()["error"]["code"] == "unsupported_media_type"

    too_big = anon.post(
        "/v1/files", headers={"authorization": f"Bearer {media_key}"},
        files={"file": ("big.bin", b"x" * (20 * 1024 * 1024 + 1), "application/octet-stream")},
        timeout=30.0,
    )
    assert too_big.status_code == 413, too_big.text
    assert too_big.json()["error"]["code"] == "file_too_large"


def test_editable_tasks_owner_isolation(
    anon: httpx.Client, media_key: str, other_key: str, media_on: None
) -> None:
    created = anon.post(
        "/v1/editable-file-tasks", headers=_auth(media_key),
        json={"prompt": "make a deck", "kind": "ppt"},
    )
    assert created.status_code == 202, created.text
    assert created.json()["id"].startswith("et-")

    fetched = anon.get("/v1/editable-file-tasks/et-1", headers=_auth(media_key))
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["status"] == "completed"
    assert fetched.json()["result"]["download_url"] == "http://stub/et-1.pptx"

    foreign = anon.get("/v1/editable-file-tasks/et-1", headers=_auth(other_key))
    assert foreign.status_code == 404, foreign.text
    assert foreign.json()["error"]["code"] == "editable_task_not_found"

    listed = anon.get(
        "/v1/editable-file-tasks", params={"ids": "et-1,et-404"},
        headers=_auth(media_key),
    )
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert [item["id"] for item in body["items"]] == ["et-1"]
    assert body["missing_ids"] == ["et-404"]

    foreign_list = anon.get(
        "/v1/editable-file-tasks", params={"ids": "et-1"}, headers=_auth(other_key)
    )
    assert foreign_list.status_code == 200
    assert foreign_list.json()["items"] == []
    assert foreign_list.json()["missing_ids"] == ["et-1"]

    invalid = anon.post(
        "/v1/editable-file-tasks", headers=_auth(media_key),
        json={"kind": "psd", "prompt": "x"},
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "editable_images_required"


def test_task_queue_position_and_cancel(
    anon: httpx.Client, media_key: str, media_on: None
) -> None:
    first = anon.post(
        "/v1/tasks", headers=_auth(media_key),
        json={"kind": "editable_file", "payload": {"prompt": "queue long task"}},
    )
    assert first.status_code == 202, first.text
    first_id = first.json()["id"]

    # 等 worker 把第一条转 running，第二条才会明确排在第 1 位
    state = "queued"
    for _ in range(200):
        state = anon.get(f"/v1/tasks/{first_id}", headers=_auth(media_key)).json()["status"]
        if state == "running":
            break
        time.sleep(0.1)
    assert state == "running", f"第一条应进入 running，实际 {state}"

    second = anon.post(
        "/v1/tasks", headers=_auth(media_key),
        json={"kind": "editable_file", "payload": {"prompt": "queue second task"}},
    )
    assert second.status_code == 202, second.text
    assert second.json()["queue_position"] == 1

    cancelled = anon.post(f"/v1/tasks/{second.json()['id']}/cancel", headers=_auth(media_key))
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["cancelled"] is True

    assert anon.post(f"/v1/tasks/{first_id}/cancel", headers=_auth(media_key)).status_code == 200
    again = anon.post(f"/v1/tasks/{first_id}/cancel", headers=_auth(media_key))
    assert again.status_code == 404, "终态不可再取消"

    unknown = anon.get("/v1/tasks/task_nope", headers=_auth(media_key))
    assert unknown.status_code == 404
    bad_kind = anon.post(
        "/v1/tasks", headers=_auth(media_key), json={"kind": "nope", "payload": {}}
    )
    assert bad_kind.status_code == 400
    assert bad_kind.json()["error"]["code"] == "task_kind_unsupported"
