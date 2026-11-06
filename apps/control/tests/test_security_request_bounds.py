"""安全边界回归：/v1 请求体上限 413、远端备份 endpoint 与 provider 共用地址守卫。

口径：
- 超限请求在鉴权之后、解析之前被拒，返回 413 request_too_large，不落成功日志；
- 守卫默认拒绝环回 / 私网 / 链路本地，云元数据地址即使放开私网也始终拒绝；
- 正常公网 URL 与限内请求照旧放行，不改流式与鉴权顺序。
"""

from __future__ import annotations

import asyncio
import dataclasses
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import load_settings
from app.db import Database
from app.gateway_errors import GatewayError
from app.routers import v1
from app.services.backup_remote import BackupRemoteService, ConfigError

pytestmark = pytest.mark.gateway

# conftest 把 UNIFIED_GATEWAY_MAX_BODY_BYTES 压到 2 MiB（现有最大用例约 1 MiB）
OVERSIZE_BODY_BYTES = 2 * 1024 * 1024 + 512


def _oversize_payload(model: str = "fake-chat-1") -> dict:
    pad = "x" * OVERSIZE_BODY_BYTES
    return {"model": model, "messages": [{"role": "user", "content": pad}]}


def _max_log_id(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM request_logs").fetchone()
    finally:
        conn.close()
    return int(row[0])


def _new_log_statuses(db_path: str, after_id: int) -> list[int]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT status FROM request_logs WHERE id > ?", (after_id,)
        ).fetchall()
    finally:
        conn.close()
    return [int(r[0]) for r in rows]


@pytest.fixture(scope="module")
def limited_key(admin: httpx.Client) -> str:
    r = admin.post("/api/keys", json={"name": "body-limit-suite"})
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    return r.json()["key"]


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/messages", "/v1/responses"])
def test_oversize_json_entry_points_return_413(
    anon: httpx.Client, db_path: str, limited_key: str, path: str
) -> None:
    payload = (
        {"model": "fake-chat-1", "input": "x" * OVERSIZE_BODY_BYTES}
        if path == "/v1/responses"
        else _oversize_payload()
    )
    before = _max_log_id(db_path)
    r = anon.post(path, headers={"authorization": f"Bearer {limited_key}"}, json=payload)
    assert r.status_code == 413, f"{path}: {r.status_code} {r.text[:300]}"
    body = r.json()
    assert body["error"]["code"] == "request_too_large"
    if path == "/v1/messages":
        # Anthropic 壳：413 的内层类型就是协议里的 request_too_large
        assert body.get("type") == "error"
        assert body["error"]["type"] == "request_too_large"
    else:
        assert body["error"]["type"] == "invalid_request_error"
    # 超限请求既不算成功也不能落成一次正常调用
    assert [s for s in _new_log_statuses(db_path, before) if s < 400] == []


def test_auth_still_wins_without_key(anon: httpx.Client) -> None:
    r = anon.post(
        "/v1/chat/completions",
        json={"model": "fake-chat-1", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_api_key"


def test_within_limit_request_still_succeeds(anon: httpx.Client, limited_key: str) -> None:
    r = anon.post(
        "/v1/chat/completions",
        headers={"authorization": f"Bearer {limited_key}"},
        json={"model": "fake-chat-1", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200, f"{r.status_code} {r.text[:300]}"
    assert r.json()["choices"][0]["message"]["content"]


def _guarded_app(limit: int) -> FastAPI:
    """把 _json_payload 挂到最小应用上，隔离测试「读流时超限」这一支。"""
    app = FastAPI()
    app.state.settings = SimpleNamespace(gateway_max_body_bytes=limit)

    @app.exception_handler(GatewayError)
    async def _on_gateway_error(request: Request, exc: GatewayError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.openai_payload())

    @app.post("/v1/chat/completions")
    async def _chat(request: Request) -> dict:
        await v1._json_payload(request)
        return {"ok": True}

    return app


def _post_to_app(app: FastAPI, *, content=None, json_body: dict | None = None) -> httpx.Response:
    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            if content is not None:
                return await client.post(
                    "/v1/chat/completions",
                    content=content,
                    headers={"content-type": "application/json"},
                )
            return await client.post("/v1/chat/completions", json=json_body)

    return asyncio.run(_run())


def test_streamed_body_over_limit_returns_413() -> None:
    """没有 Content-Length 的分块上传也要在累计超限时立刻拒绝。"""
    app = _guarded_app(4096)

    async def _chunks():
        yield b'{"model":"m","messages":[{"role":"user","content":"'
        yield b"x" * 6000
        yield b'"}]}'

    r = _post_to_app(app, content=_chunks())
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "request_too_large"


def test_content_length_over_limit_returns_413() -> None:
    app = _guarded_app(4096)
    r = _post_to_app(app, json_body={"pad": "x" * 5000})
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "request_too_large"


@pytest.fixture
def backup_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BackupRemoteService:
    monkeypatch.delenv("UNIFIED_ALLOW_PRIVATE_UPSTREAMS", raising=False)
    settings = dataclasses.replace(load_settings(), db_path=str(tmp_path / "unified.db"))
    return BackupRemoteService(settings, Database(Path(settings.db_path)))


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:9000",
        "http://localhost:9000",
        "http://[::1]:9000",
        "http://10.0.0.7:9000",
        "http://172.16.5.9:9000",
        "http://192.168.1.20:9000",
        "http://169.254.169.254",
        "http://[fd00:ec2::254]:9000",
        "http://metadata.google.internal",
    ],
)
def test_backup_endpoint_rejects_local_and_metadata(
    backup_service: BackupRemoteService, endpoint: str
) -> None:
    with pytest.raises(ConfigError):
        backup_service.save_config({"endpoint": endpoint})
    assert backup_service.config()["endpoint"] == "", "被拒的地址不能落库"


def test_backup_endpoint_metadata_rejected_even_when_private_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UNIFIED_ALLOW_PRIVATE_UPSTREAMS", "1")
    settings = dataclasses.replace(load_settings(), db_path=str(tmp_path / "unified.db"))
    service = BackupRemoteService(settings, Database(Path(settings.db_path)))
    service.save_config({"endpoint": "http://127.0.0.1:9000"})
    with pytest.raises(ConfigError):
        service.save_config({"endpoint": "http://169.254.169.254"})
    assert service.config()["endpoint"] == "http://127.0.0.1:9000"


def test_backup_endpoint_public_url_allowed(backup_service: BackupRemoteService) -> None:
    public = "https://93.184.216.34:9000"
    saved = backup_service.save_config({"endpoint": public})
    assert saved["endpoint"] == public


def test_backup_remote_route_rejects_metadata(admin: httpx.Client) -> None:
    r = admin.post("/api/system/backup-remote", json={"endpoint": "http://169.254.169.254"})
    assert r.status_code == 400, f"{r.status_code} {r.text[:300]}"
    assert "元数据" in r.json()["detail"]
