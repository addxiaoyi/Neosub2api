"""POST /api/settings/upstash/test 真实探测回归。

mock httpx 覆盖 5 类结果（成功 / 401 / 403 / 超时 / 非法 url），全部走 MockTransport，
不起真实网络；另补断连 502、缺 token、保存值回退等边界。
SSRF 加固回归：内网字面量 / 受限主机名 / DNS 解析到内网 / 重定向到内网 / 超限响应体分别被拒，
出站目标校验在发出任何请求之前完成。
"""

from __future__ import annotations

import ipaddress
import json
import socket
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app import security
from app.db import Database, set_json_setting
from app.routers import settings as settings_mod

PROBE_URL = "https://demo-12345.upstash.io"
TOKEN = "test-upstash-token"
ENDPOINT = "/api/settings/upstash/test"
PUBLIC_IP = ipaddress.ip_address("93.184.216.34")


def _control_app(db_path) -> FastAPI:
    """最小控制面：只挂 settings 路由，鉴权用 dependency_overrides 顶掉，库指向临时目录。"""
    app = FastAPI()
    app.state.db = Database(db_path)
    app.include_router(settings_mod.router)
    app.dependency_overrides[security.admin_user] = lambda: {"username": "admin", "role": "admin"}
    return app


@pytest.fixture
def app(tmp_path) -> FastAPI:
    return _control_app(tmp_path / "unified.db")


@pytest.fixture
def probe(monkeypatch):
    """替换出站客户端工厂与 DNS 解析：出站请求全由 MockTransport 接住，并记录生产构造参数。"""
    state = SimpleNamespace(
        requests=[],
        handler=None,
        trust_env=None,
        timeout=None,
        follow_redirects=None,
        resolve_calls=[],
        resolve_addrs=None,  # 为 None 时按公网结果放行，用例可改成内网地址模拟 rebinding
    )
    real_factory = settings_mod._upstash_client
    real_resolve = settings_mod._resolve_upstash
    state.real_resolve = real_resolve  # 个别用例要还原真实解析器再篡改 getaddrinfo

    def _default(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"result": "PONG"}])

    async def _handle(request: httpx.Request) -> httpx.Response:
        state.requests.append(request)
        handler = state.handler or _default
        return handler(request)  # handler 可直接抛 httpx 异常，用来模拟超时 / 断连

    def _factory() -> httpx.AsyncClient:
        client = real_factory()
        state.trust_env = client.trust_env
        state.timeout = client.timeout
        state.follow_redirects = client.follow_redirects
        client._transport = httpx.MockTransport(_handle)
        return client

    def _resolve(host: str, port: int) -> list:
        state.resolve_calls.append((host, port))
        if state.resolve_addrs is None:
            return [PUBLIC_IP]
        return list(state.resolve_addrs)

    monkeypatch.setattr(settings_mod, "_upstash_client", _factory)
    monkeypatch.setattr(settings_mod, "_resolve_upstash", _resolve)
    return state


async def _post(app: FastAPI, payload=None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://control.test", trust_env=False) as client:
        if payload is None:
            return await client.post(ENDPOINT)
        return await client.post(ENDPOINT, json=payload)


async def test_success_probe_is_readonly_ping(app, probe):
    resp = await _post(app, {"url": PROBE_URL, "token": TOKEN})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["verified"] is True
    assert isinstance(body["latency_ms"], int) and body["latency_ms"] >= 0
    assert "PING" in body["detail"]
    assert body["message"] == body["detail"]
    assert TOKEN not in resp.text, "成功响应不得回显 token"

    # 生产构造参数：不读环境代理，超时生效，且不跟随重定向
    assert probe.trust_env is False
    assert probe.timeout.read == settings_mod._UPSTASH_TIMEOUT_SEC
    assert probe.follow_redirects is False

    # 连接前先按域名做过解析校验（防 DNS rebinding），再发起唯一一次请求
    assert probe.resolve_calls == [("demo-12345.upstash.io", 443)]
    assert len(probe.requests) == 1
    sent = probe.requests[0]
    assert sent.method == "POST"
    assert sent.url.scheme == "https"
    assert sent.url.host == "demo-12345.upstash.io"
    assert sent.url.path == "/pipeline"
    assert sent.headers["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(sent.content) == [["PING"]], "探测只允许下发一条只读 PING"


async def test_upstream_401_maps_to_400_auth_code(app, probe):
    probe.handler = lambda request: httpx.Response(401, json={"error": "WRONGPASS invalid username-password pair"})

    resp = await _post(app, {"url": PROBE_URL, "token": TOKEN})

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "upstash_auth_rejected"
    assert body["error"]["type"] == "authentication_error"
    assert isinstance(body["detail"], str) and body["detail"]
    assert "401" in body["detail"]
    assert TOKEN not in resp.text
    # 确实带着 token 打到了上游，不是本地就给拒了
    assert probe.requests[0].headers["authorization"] == f"Bearer {TOKEN}"


async def test_upstream_403_maps_to_400_auth_code(app, probe):
    probe.handler = lambda request: httpx.Response(403, json={"error": "forbidden"})

    resp = await _post(app, {"url": PROBE_URL, "token": TOKEN})

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error"]["code"] == "upstash_auth_rejected"
    assert body["error"]["type"] == "authentication_error"
    assert TOKEN not in resp.text


async def test_timeout_maps_to_504(app, probe):
    def _timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    probe.handler = _timeout

    resp = await _post(app, {"url": PROBE_URL, "token": TOKEN})

    assert resp.status_code == 504, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "upstash_timeout"
    assert body["error"]["type"] == "upstream_error"


async def test_connect_error_maps_to_502(app, probe):
    def _refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("All connection attempts failed")

    probe.handler = _refused

    resp = await _post(app, {"url": PROBE_URL, "token": TOKEN})

    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["code"] == "upstash_unreachable"


async def test_upstream_5xx_maps_to_502(app, probe):
    probe.handler = lambda request: httpx.Response(503, text="upstream maintenance")

    resp = await _post(app, {"url": PROBE_URL, "token": TOKEN})

    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["code"] == "upstash_unavailable"


@pytest.mark.parametrize(
    "bad_url",
    [
        "",  # 缺失
        "http://demo-12345.upstash.io",  # 非 https（不做 upstream 那套内网白名单，只看协议）
        "ftp://demo-12345.upstash.io",
        "demo-12345.upstash.io",  # 无协议
        "https://",  # 无主机
        "https://bad host.upstash.io",  # 空白字符
        "https://user:pass@demo-12345.upstash.io",  # 携带用户信息
        "https://demo-12345.upstash.io/?x=1",  # 带 query
        "https://[::1",  # 坏 IPv6
        "https://demo-12345.upstash.io:99999",  # 端口越界
    ],
)
async def test_invalid_url_rejected_before_network(app, probe, bad_url):
    resp = await _post(app, {"url": bad_url, "token": TOKEN})

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "invalid_upstash_url"
    assert isinstance(body["detail"], str) and body["detail"]
    assert probe.requests == [], "非法地址不该发出任何出站请求"


async def test_empty_body_gets_structured_400(app, probe):
    resp = await _post(app)

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "invalid_upstash_url"
    assert isinstance(body["detail"], str)
    assert probe.requests == []


async def test_missing_token_rejected(app, probe):
    resp = await _post(app, {"url": PROBE_URL})

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "upstash_token_missing"
    assert probe.requests == []


async def test_blank_token_falls_back_to_stored(app, probe):
    conn = app.state.db.connect()
    try:
        set_json_setting(conn, "upstream_config", {"upstash": {"url": PROBE_URL, "token": "saved-upstash-token"}})
    finally:
        conn.close()

    resp = await _post(app, {"url": PROBE_URL, "token": ""})

    assert resp.status_code == 200, resp.text
    assert resp.json()["verified"] is True
    assert probe.requests[0].headers["authorization"] == "Bearer saved-upstash-token"


# --------------------------------------------------------------------------- #
# SSRF 加固：出站目标必须是公网 Upstash，任何内网 / 保留目标在建连前拒绝
# --------------------------------------------------------------------------- #

INTERNAL_TARGETS = [
    "https://127.0.0.1/pipeline",
    "https://127.1.2.3:8406/pipeline",
    "https://10.1.2.3/pipeline",
    "https://172.16.0.9/pipeline",
    "https://192.168.1.10/pipeline",
    "https://169.254.169.254/latest/meta-data",
    "https://[::1]/pipeline",
    "https://[fd00:ec2::254]/pipeline",
    "https://[fe80::1]/pipeline",
    "https://[::ffff:10.0.0.5]/pipeline",
    "https://[::ffff:169.254.169.254]/",
    "https://localhost:8406/pipeline",
    "https://localhost.localdomain/pipeline",
    "https://metadata.google.internal/computeMetadata/v1/",
]


@pytest.mark.parametrize("internal_url", INTERNAL_TARGETS)
async def test_internal_target_rejected_before_network(app, probe, internal_url):
    resp = await _post(app, {"url": internal_url, "token": TOKEN})

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "upstash_target_forbidden"
    assert body["error"]["type"] == "invalid_config"
    assert probe.requests == [], "内网目标不该发出任何出站请求"
    assert probe.resolve_calls == [], "字面量 / 受限主机名在 DNS 解析前就该拦下"


async def test_dns_resolving_to_internal_rejected(app, probe):
    """域名本身没毛病，解析结果指到 10.x —— DNS rebinding 场景。"""
    probe.resolve_addrs = [ipaddress.ip_address("10.0.0.5")]

    resp = await _post(app, {"url": "https://evil.example.test/pipeline", "token": TOKEN})

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "upstash_target_forbidden"
    assert "10.0.0.5" in resp.json()["detail"]
    assert probe.requests == [], "解析结果是内网地址时不该继续建连"
    assert probe.resolve_calls == [("evil.example.test", 443)]


async def test_dns_resolving_to_metadata_rejected(app, probe):
    probe.resolve_addrs = [ipaddress.ip_address("169.254.169.254")]

    resp = await _post(app, {"url": "https://rebind.example.test/pipeline", "token": TOKEN})

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "upstash_target_forbidden"
    assert probe.requests == []


async def test_dns_mixed_public_and_internal_rejected(app, probe):
    """多个解析结果里只要有一个指向禁网段就整体拒：httpx 可能连上任意一个。"""
    probe.resolve_addrs = [PUBLIC_IP, ipaddress.ip_address("192.168.1.10")]

    resp = await _post(app, {"url": "https://mixed.example.test/pipeline", "token": TOKEN})

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "upstash_target_forbidden"
    assert probe.requests == []


async def test_dns_failure_maps_to_unreachable(app, probe, monkeypatch):
    monkeypatch.setattr(settings_mod, "_resolve_upstash", probe.real_resolve)

    def _fail(*args, **kwargs):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(settings_mod.socket, "getaddrinfo", _fail)

    resp = await _post(app, {"url": "https://nxdomain.example.test/pipeline", "token": TOKEN})

    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["code"] == "upstash_unreachable"
    assert probe.requests == []


async def test_redirect_not_followed_even_to_internal(app, probe):
    """上游 302 指到内网：不跟随（follow_redirects=False），按重定向拒绝返回。"""
    probe.handler = lambda request: httpx.Response(
        302, headers={"location": "http://169.254.169.254/latest/meta-data"}
    )

    resp = await _post(app, {"url": PROBE_URL, "token": TOKEN})

    assert resp.status_code == 502, resp.text
    body = resp.json()
    assert body["error"]["code"] == "upstash_redirect_blocked"
    assert body["error"]["type"] == "upstream_error"
    assert len(probe.requests) == 1, "重定向只该发出原始那一次请求"
    assert probe.follow_redirects is False


async def test_oversized_response_aborted(app, probe):
    """超大响应体在读满上限时中止，不让上游把内存打爆。"""
    huge = b'{"result":"PONG"}' + b"x" * settings_mod._UPSTASH_MAX_BODY
    probe.handler = lambda request: httpx.Response(200, content=huge, headers={"content-type": "application/json"})

    resp = await _post(app, {"url": PROBE_URL, "token": TOKEN})

    assert resp.status_code == 502, resp.text
    body = resp.json()
    assert body["error"]["code"] == "upstash_response_too_large"
    assert body["error"]["type"] == "upstream_error"
    assert len(probe.requests) == 1
