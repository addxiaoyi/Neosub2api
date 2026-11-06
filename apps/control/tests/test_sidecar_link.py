"""四家 sidecar 的进程生命周期与转发链路。

假 sidecar 起在动态端口（127.0.0.1:0），覆盖存活 / 超时 / 拒绝连接 / 500 / 流式分块。
不连真实 sidecar，也不复制任何上游源码。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from app.adapters import codar_sidecar, deepseek_sidecar, qoder_sidecar, trae_sidecar
from app.adapters.base import ProviderError
from app.config import Settings
from app.services import sidecar_status

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


def _json(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode()
    handler.send_response(status)
    handler.send_header("content-type", "application/json")
    handler.send_header("content-length", str(len(body)))
    handler.send_header("connection", "close")
    handler.end_headers()
    handler.wfile.write(body)


def _sse(handler: BaseHTTPRequestHandler, chunks: list[bytes]) -> None:
    handler.send_response(200)
    handler.send_header("content-type", "text/event-stream")
    handler.send_header("connection", "close")
    handler.end_headers()
    for chunk in chunks:
        handler.wfile.write(chunk)
        handler.wfile.flush()


@contextmanager
def fake_sidecar(routes: dict[tuple[str, str], Callable[[BaseHTTPRequestHandler], None]]):
    """routes: {(method, path): handler}；未命中的路径回 404，方便验「接口不存在」的措辞。"""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            path = self.path.split("?", 1)[0]
            fn = routes.get((method, path))
            if fn is None:
                return _json(self, 404, {"error": "not found", "path": path})
            try:
                fn(self)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _alive_routes(seen: list[tuple[str, str]] | None = None) -> dict:
    def models(handler: BaseHTTPRequestHandler) -> None:
        if seen is not None:
            seen.append(("GET", handler.path.split("?", 1)[0]))
        _json(handler, 200, {"object": "list", "data": [{"id": "fake-model"}]})

    return {("GET", "/v1/models"): models}


def _refused_base() -> str:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


# ---- 探测三态 ----


async def test_probe_alive(tmp_path: Path) -> None:
    with fake_sidecar(_alive_routes()) as base:
        status = await sidecar_status.probe_sidecar("codar_sidecar", base, timeout=1.0)
    assert status.state == "alive"
    assert "在线" in status.message
    assert str(status.port) in status.message


async def _raise_refused(*args: Any, **kwargs: Any) -> None:
    raise ConnectionRefusedError("connection refused")


async def test_probe_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # 本机 loopback 对关闭端口会走超时而不是 ECONNREFUSED，分类逻辑在 _dial 处注入验证
    monkeypatch.setattr(sidecar_status, "_dial", _raise_refused)
    status = await sidecar_status.probe_sidecar("trae_sidecar", "http://127.0.0.1:7871", timeout=1.0)
    assert status.state == "refused"
    assert "侧车未运行（端口" in status.message
    assert "sidecar" in status.message


async def test_probe_timeout() -> None:
    def slow(handler: BaseHTTPRequestHandler) -> None:
        time.sleep(2.5)
        _json(handler, 200, {"data": []})

    with fake_sidecar({("GET", "/v1/models"): slow}) as base:
        status = await sidecar_status.probe_sidecar("qoder_sidecar", base, timeout=0.4)
    assert status.state == "timeout"
    assert "无响应" in status.message
    assert str(status.port) in status.message


async def test_probe_http_500_is_port_occupied_wording() -> None:
    with fake_sidecar({("GET", "/v1/models"): lambda h: _json(h, 500, {"error": "boom"})}) as base:
        status = await sidecar_status.probe_sidecar("deepseek_sidecar", base, timeout=1.0)
    assert status.state == "http_error"
    assert "500" in status.message
    assert "占用" in status.message


async def test_probe_auth_points_at_api_key() -> None:
    with fake_sidecar({("GET", "/v1/models"): lambda h: _json(h, 401, {"error": "no key"})}) as base:
        status = await sidecar_status.probe_sidecar("codar_sidecar", base, timeout=1.0)
    assert status.state == "auth"
    assert "api_key" in status.message


# ---- readiness 落点 ----


def test_decorate_refused_sets_actionable_requires() -> None:
    sidecar_status.clear_cache()
    status = sidecar_status.SidecarStatus(
        pid="trae_sidecar", state="refused",
        message="侧车未运行（端口 7871）：sidecar 连接被拒绝，请先启动对应进程",
        port=7871, base_url="http://127.0.0.1:7871", path="/v1/models",
    )
    sidecar_status.remember(status)
    item = {
        "id": "trae_sidecar", "status": "sidecar",
        "readiness": "needs_config", "requires": ["sidecar 进程（trae2api，默认端口 7871）"],
    }
    sidecar_status.decorate_public(item)
    assert item["readiness"] == "needs_config"
    assert item["requires"] == [status.message]
    assert item["sidecar_status"]["state"] == "refused"
    assert "7871" in item["requires"][0]


def test_decorate_alive_marks_ready() -> None:
    sidecar_status.clear_cache()
    status = sidecar_status.SidecarStatus(
        pid="codar_sidecar", state="alive",
        message="sidecar 在线（端口 8801，12ms）",
        port=8801, base_url="http://127.0.0.1:8801", path="/v1/models", latency_ms=12,
    )
    sidecar_status.remember(status)
    item = {"id": "codar_sidecar", "status": "sidecar", "readiness": "needs_config", "requires": ["x"]}
    sidecar_status.decorate_public(item)
    assert item["readiness"] == "ready"
    assert item["requires"] == []
    assert item["sidecar_status"]["latency_ms"] == 12


def test_decorate_skips_non_sidecar() -> None:
    sidecar_status.clear_cache()
    status = sidecar_status.SidecarStatus(
        pid="muse", state="refused", message="x", port=0, base_url="", path="/v1/models",
    )
    sidecar_status.remember(status)
    item = {"id": "muse", "status": "implemented", "readiness": "ready", "requires": []}
    sidecar_status.decorate_public(item)
    assert "sidecar_status" not in item
    assert item["readiness"] == "ready"


# ---- 账号只读代理 ----


async def test_accounts_proxied_from_sidecar(tmp_path: Path) -> None:
    def accounts(handler: BaseHTTPRequestHandler) -> None:
        _json(handler, 200, {"accounts": [
            {"uid": "u1", "nickname": "主号", "credits": 12, "expires_at": 1760000000},
            {"id": "u2", "name": "备号", "status": "disabled"},
        ]})

    with fake_sidecar({("GET", "/api/accounts"): accounts}) as base:
        adapter = codar_sidecar.build(_settings(tmp_path), base_url=base)
        page = await adapter.sync_accounts()
    assert page.pool_available is True
    assert [row["uid"] for row in page.accounts] == ["u1", "u2"]
    assert page.accounts[0]["credits"] == 12
    assert page.accounts[1]["disabled"] == 1
    assert page.note == ""


async def test_accounts_404_says_where_to_look(tmp_path: Path) -> None:
    with fake_sidecar({}) as base:
        adapter = qoder_sidecar.build(_settings(tmp_path), base_url=base)
        page = await adapter.sync_accounts()
    assert page.pool_available is False
    assert "未提供账号列表接口" in page.note
    assert "管理面板" in page.note


async def test_accounts_unreachable_raises_with_port(tmp_path: Path) -> None:
    adapter = trae_sidecar.build(_settings(tmp_path), base_url=_refused_base())
    with pytest.raises(ProviderError) as caught:
        await adapter.sync_accounts()
    assert caught.value.status_code == 502
    assert "侧车未运行（端口" in caught.value.message


async def test_accounts_unknown_shape_is_not_empty_success(tmp_path: Path) -> None:
    with fake_sidecar({("GET", "/api/accounts"): lambda h: _json(h, 200, {"foo": 1})}) as base:
        adapter = deepseek_sidecar.build(_settings(tmp_path), base_url=base)
        page = await adapter.sync_accounts()
    assert page.pool_available is False
    assert "无法识别" in page.note


# ---- 转发链路：非流式 / 流式 / 错误码 ----


async def test_chat_non_stream_passthrough(tmp_path: Path) -> None:
    def chat(handler: BaseHTTPRequestHandler) -> None:
        _json(handler, 200, {"id": "chatcmpl-x", "object": "chat.completion", "model": "fake-model"})

    with fake_sidecar({("POST", "/v1/chat/completions"): chat}) as base:
        adapter = codar_sidecar.build(_settings(tmp_path), base_url=base)
        reply = await adapter.chat_completion({"model": "fake-model", "messages": []}, stream=False)
    assert reply.status_code == 200
    assert b"chatcmpl-x" in (reply.body or b"")


async def test_chat_stream_chunks_pass_through(tmp_path: Path) -> None:
    def stream(handler: BaseHTTPRequestHandler) -> None:
        _sse(handler, [b'data: {"delta":"he"}\n\n', b'data: {"delta":"llo"}\n\n', b"data: [DONE]\n\n"])

    with fake_sidecar({("POST", "/v1/chat/completions"): stream}) as base:
        adapter = qoder_sidecar.build(_settings(tmp_path), base_url=base)
        reply = await adapter.chat_completion({"model": "m", "stream": True}, stream=True)
        text = "".join([chunk.decode() async for chunk in reply.iter_bytes()])
    assert reply.status_code == 200
    assert 'data: {"delta":"he"}' in text
    assert "data: [DONE]" in text


async def test_upstream_500_keeps_body(tmp_path: Path) -> None:
    def boom(handler: BaseHTTPRequestHandler) -> None:
        _json(handler, 500, {"error": {"message": "sidecar exploded"}})

    with fake_sidecar({("POST", "/v1/chat/completions"): boom}) as base:
        adapter = trae_sidecar.build(_settings(tmp_path), base_url=base)
        reply = await adapter.chat_completion({"model": "m", "messages": []}, stream=False)
    assert reply.status_code == 500
    assert b"sidecar exploded" in (reply.body or b"")


async def test_refused_chat_becomes_human_502(tmp_path: Path) -> None:
    adapter = trae_sidecar.build(_settings(tmp_path), base_url=_refused_base())
    with pytest.raises(ProviderError) as caught:
        await adapter.chat_completion({"model": "m", "messages": []}, stream=False)
    assert caught.value.status_code == 502
    assert "侧车未运行（端口" in caught.value.message


async def test_deepseek_messages_path_rewrite_and_stream_501(tmp_path: Path) -> None:
    seen: list[str] = []

    def anthropic(handler: BaseHTTPRequestHandler) -> None:
        seen.append(handler.path)
        _json(handler, 200, {"type": "message"})

    with fake_sidecar({("POST", "/anthropic/v1/messages"): anthropic}) as base:
        adapter = deepseek_sidecar.build(_settings(tmp_path), base_url=base)
        reply = await adapter.messages({"model": "m", "messages": []}, stream=False)
    assert seen == ["/anthropic/v1/messages"]
    assert reply.status_code == 200
    with pytest.raises(ProviderError) as caught:
        await adapter.messages({"model": "m", "messages": []}, stream=True)
    assert caught.value.status_code == 501


async def test_health_reports_three_states(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with fake_sidecar(_alive_routes()) as base:
        alive = await codar_sidecar.build(_settings(tmp_path), base_url=base).health()
    monkeypatch.setattr(sidecar_status, "_dial", _raise_refused)
    down = await qoder_sidecar.build(_settings(tmp_path), base_url=_refused_base()).health()
    assert alive["connected"] is True and alive["state"] == "alive"
    assert down["connected"] is False and down["state"] == "refused"
    assert "侧车未运行（端口" in down["error"]


def test_providers_endpoint_reports_sidecar_state(admin: Any) -> None:
    """统一入口把四家 sidecar 的存活结论落到 readiness/requires，并单独暴露快照。"""
    rows = admin.get("/api/providers").json()
    sidecars = [row for row in rows if row["status"] == "sidecar"]
    assert len(sidecars) == 4
    for row in sidecars:
        payload = row["sidecar_status"]
        assert payload["state"] in {"alive", "auth", "timeout", "refused", "http_error"}
        assert "端口" in payload["message"]
        if payload["state"] != "alive":
            assert row["readiness"] == "needs_config"
            assert row["requires"] == [payload["message"]]
    snapshot = admin.get("/api/providers/sidecars").json()["sidecars"]
    assert {item["id"] for item in snapshot} == {row["id"] for row in sidecars}


# ---- 端到端：/v1 进来 → 控制面 → 侧车 → 回传 ----

_CONTROL_ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def control_plane(tmp: Path, extra_env: dict[str, str]):
    """独立起一个控制面（动态端口），sidecar 地址由用例注入，不碰会话级 fixture。"""
    port = _free_port()
    data = tmp / "ctrl-data"
    data.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "UNIFIED_DATA_DIR": str(data),
        "UNIFIED_DB_PATH": str(data / "unified.db"),
        "UNIFIED_ADMIN_USER": "admin",
        "UNIFIED_ADMIN_PASSWORD": "test",
        "UNIFIED_WEB_DIR": str(tmp / "no-web"),
        "UNIFIED_ALLOW_PRIVATE_UPSTREAMS": "1",
        "GROK_REGISTER_EXECUTOR_URL": "",
        **extra_env,
    }
    log = (tmp / "ctrl.stderr.log").open("wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(_CONTROL_ROOT), env=env, stdout=subprocess.DEVNULL, stderr=log,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                if httpx.get(f"{base}/healthz", timeout=1.0, trust_env=False).status_code < 500:
                    break
            except httpx.HTTPError:
                pass
            assert proc.poll() is None, (tmp / "ctrl.stderr.log").read_text("utf-8", "ignore")[-800:]
            time.sleep(0.2)
        else:
            raise AssertionError(f"控制面 20s 内未就绪，stderr：{(tmp / 'ctrl.stderr.log').read_text('utf-8', 'ignore')[-800:]}")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)
        log.close()


def _body_of(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    raw = handler.rfile.read(int(handler.headers.get("content-length") or 0))
    return json.loads(raw or b"{}")


def _e2e_routes() -> dict[tuple[str, str], Callable[[BaseHTTPRequestHandler], None]]:
    def models(handler: BaseHTTPRequestHandler) -> None:
        _json(handler, 200, {"object": "list", "data": [{"id": "fake-model"}]})

    def accounts(handler: BaseHTTPRequestHandler) -> None:
        _json(handler, 200, {"accounts": [{"uid": "u1", "nickname": "主号", "credits": 7}]})

    def chat(handler: BaseHTTPRequestHandler) -> None:
        body = _body_of(handler)
        if body.get("model") == "boom":
            return _json(handler, 500, {"error": {"message": "sidecar exploded"}})
        if body.get("stream"):
            return _sse(handler, [
                b'data: {"choices":[{"delta":{"content":"he"}}]}\n\n',
                b'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n',
                b"data: [DONE]\n\n",
            ])
        _json(handler, 200, {"id": "chatcmpl-e2e", "object": "chat.completion", "model": body.get("model")})

    return {
        ("GET", "/v1/models"): models,
        ("GET", "/api/accounts"): accounts,
        ("POST", "/v1/chat/completions"): chat,
    }


async def test_v1_gateway_reaches_fake_sidecar_end_to_end(tmp_path: Path) -> None:
    """四家共用的那一跳：/v1 → 控制面 → 侧车 → 回传（含流式、上游 500、侧车缺席）。"""
    trae_down = _refused_base()
    with fake_sidecar(_e2e_routes()) as base:
        with control_plane(tmp_path, {"CODAR_SIDECAR_BASE": base, "TRAE_SIDECAR_BASE": trae_down}) as ctrl:
            client = httpx.Client(base_url=ctrl, trust_env=False, timeout=15.0)
            try:
                assert client.post(
                    "/api/login", json={"username": "admin", "password": "test"}
                ).status_code == 200
                key = client.post("/api/keys", json={"name": "sidecar-e2e"}).json()["key"]
                for pid in ("codar_sidecar", "trae_sidecar"):
                    assert client.patch(f"/api/providers/{pid}", json={"enabled": True}).status_code == 200
                headers = {"authorization": f"Bearer {key}", "x-provider": "codar_sidecar"}

                rows = {row["id"]: row for row in client.get("/api/providers").json()}
                assert rows["codar_sidecar"]["readiness"] == "ready"
                assert rows["codar_sidecar"]["sidecar_status"]["state"] == "alive"
                assert rows["trae_sidecar"]["readiness"] == "needs_config"
                assert "端口" in rows["trae_sidecar"]["requires"][0]

                listed = client.get("/api/accounts", params={"provider": "codar_sidecar"}).json()
                assert listed["pool_available"] is True
                assert [row["uid"] for row in listed["accounts"]] == ["u1"]

                chat = client.post(
                    "/v1/chat/completions", headers=headers,
                    json={"model": "fake-model", "messages": [{"role": "user", "content": "hi"}]},
                )
                assert chat.status_code == 200
                assert chat.json()["id"] == "chatcmpl-e2e"

                with client.stream(
                    "POST", "/v1/chat/completions", headers=headers,
                    json={"model": "fake-model", "stream": True, "messages": []},
                ) as reply:
                    assert reply.status_code == 200
                    text = "".join(chunk.decode() for chunk in reply.iter_bytes())
                assert 'data: {"choices":[{"delta":{"content":"he"}}]}' in text
                assert "data: [DONE]" in text

                boom = client.post(
                    "/v1/chat/completions", headers=headers, json={"model": "boom", "messages": []}
                )
                assert boom.status_code == 502
                assert "sidecar exploded" in boom.json()["error"]["message"]

                # 侧车缺席：接口层必须给人话，而不是静默 500
                down = client.post(
                    "/v1/chat/completions",
                    headers={**headers, "x-provider": "trae_sidecar"},
                    json={"model": "m", "messages": []},
                )
                assert down.status_code in (502, 504), down.text
                assert "侧车" in down.json()["error"]["message"]
                assert "端口" in down.json()["error"]["message"]
            finally:
                client.close()
