"""凭据掩码盲区（docs/secret-leak-audit.md 第 1 条）的验证。

四层覆盖：模式命中 / 误杀用例 / 单条出口 / 端到端三处落点
（下游错误响应、/api/logs、request_logs.error 都不得出现上游带进来的明文凭据）。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

CONTROL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CONTROL))

from app.adapters import client as client_mod  # noqa: E402
from app.adapters import gptgrok, muse, opencode, zcode  # noqa: E402
from app.adapters.base import ProviderError  # noqa: E402
from app.adapters.client import UnifiedApiClient  # noqa: E402
from app.api_utils import exc_summary, mask_secret, mask_text, safe_url  # noqa: E402
from app.config import Settings  # noqa: E402
from app.gateway_errors import from_provider_error, from_upstream_reply  # noqa: E402

# 上游 401 体里的三种凭据；全部必须在下游三处出口消失
JWT_SAMPLE = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ1c2VyMTIzNDU2Nzg5MCJ9.c2lnbmF0dXJlLXNpZ24"
LEAK_MSG = f"auth failed for ?key=abcdef with Authorization: Bearer {JWT_SAMPLE} and key sk-test-xxxx"
LEAK_BODY = json.dumps({"error": {"message": LEAK_MSG, "code": "invalid_api_key"}}, ensure_ascii=False)
LEAK_MARKERS = ("sk-test-xxxx", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", "abcdef")
LEAK_MODEL = "leakprobe401"

HITS = [
    ("u2a_ 自有前缀", "retry failed for key u2a_abcdef12345678", ("u2a_abcdef12345678",), ("u2a_…",)),
    ("OpenAI sk-", "upstream rejected key sk-test-xxxx", ("sk-test-xxxx",), ("upstream rejected",)),
    ("OpenAI sk_", "charge declined for sk_live_9f8e7d6c5b4a3210", ("sk_live_9f8e7d6c5b4a3210",), ()),
    ("Bearer token", "Authorization: Bearer 1234567890abcdef1234567890", ("Bearer 1234567890abcdef1234567890",), ("Bearer ***",)),
    ("JWT 三段式", f"header carried {JWT_SAMPLE} only", (JWT_SAMPLE,), ()),
    ("URL ?key=", "redirect refused for https://a.example/v1?key=abcdef&lang=zh", ("abcdef",), ("?key=***", "lang=zh")),
    ("URL &api_key=", "probe said &api_key=supersecret99 next", ("supersecret99",), ("api_key=***",)),
    ("URL &token=", "leaked in &token=tok_live_12345678 there", ("tok_live_12345678",), ()),
    ("URL ?secret=", "rejected because ?secret=shhh-123456", ("shhh-123456",), ()),
    ("ghp_", "deploy failed with ghp_" + "A1b2C3d4E5f6G3h4i5j6k7l8m9n0o1p2q3r4s5", ( "A1b2C3d4E5f6G3h4i5j6k7l8m9n0o1p2q3r4s5",), ()),
    ("xoxb-", "slack said xoxb-" + "TESTVEC-0-0-NOT-A-REAL-TOKEN", ("xoxb-TESTVEC",), ()),
    ("AKIA", "sts refused AKIAIOSFODNN7EXAMPLE credentials", ("AKIAIOSFODNN7EXAMPLE",), ()),
]

MISS = [
    "The quick brown fox jumps over the lazy dog.",
    "risk-management beats task-tracking when the deadline slips.",
    "see https://example.com/products?category=shoes&page=2 for the listing",
    "Bearer certificates are traded on the open market every week.",
    "the short alias sk-8ch is only three characters after the dash",
    "token expired",  # 上游原话，语义必须留着
    "key=value mapping is a typo, not a credential",
    "AKIA is just an acronym in this sentence",
    "max_tokens=1024 and cache_key=none stay readable",
]


@pytest.mark.parametrize("label,raw,absent,present", HITS, ids=[case[0] for case in HITS])
def test_each_pattern_is_masked(label: str, raw: str, absent: tuple[str, ...], present: tuple[str, ...]) -> None:
    out = mask_text(raw)
    for needle in absent:
        assert needle not in out, f"{label} 未被掩掉：{out}"
    for needle in present:
        assert needle in out, f"{label} 语义片段丢了：{out}"


@pytest.mark.parametrize("raw", MISS, ids=[raw[:32] for raw in MISS])
def test_normal_text_is_not_masked(raw: str) -> None:
    assert mask_text(raw) == raw, f"误杀：{mask_text(raw)}"


def test_u2a_mask_shape_stays_unchanged() -> None:
    """既有口径：u2a_ 用前四位…后四位的掩码，不是替换成 ***。"""
    token = "u2a_" + "A1b2" * 5
    out = mask_text(f"upstream error key={token}&retry=1")
    assert token not in out
    assert mask_secret(token) in out
    assert out.endswith("&retry=1")


def test_safe_url_drops_userinfo_and_query() -> None:
    assert safe_url("http://user:pass@127.0.0.1:9000/v1?key=abcdef") == "http://127.0.0.1:9000/v1"
    assert safe_url("http://user:pass@host.example?key=abcdef") == "http://host.example"
    assert safe_url("https://api.example.com/health") == "https://api.example.com/health"
    assert safe_url("") == ""


def test_exc_summary_keeps_type_and_sanitized_detail() -> None:
    exc = httpx.ConnectError(f"All connection attempts failed for http://user:pass@127.0.0.1:9000/v1?key=abcdef")
    out = exc_summary(exc)
    assert out.startswith("ConnectError: ")
    assert "pass@" not in out and "user:pass" not in out and "key=abcdef" not in out
    # repr 的引号包法不得出现，关键信息本身要留着
    assert "All connection attempts failed" in out
    assert "ConnectError(" not in out


def test_gateway_error_masks_upstream_body() -> None:
    err = from_upstream_reply(status_code=401, body=LEAK_BODY.encode(), provider="codebuddy")
    payload = json.dumps(err.openai_payload(), ensure_ascii=False)
    for needle in LEAK_MARKERS:
        assert needle not in payload
    assert err.error["message"].startswith("上游返回 401")
    assert "auth failed" in err.error["message"]
    assert err.error["code"] == "upstream_auth_failed"


def test_gateway_error_masks_provider_exception_detail() -> None:
    exc = ProviderError(401, "vendor said key sk-test-xxxx", upstream_message="sk-test-xxxx expired")
    err = from_provider_error(exc, "codebuddy")
    payload = json.dumps(err.openai_payload(), ensure_ascii=False)
    assert "sk-test-xxxx" not in payload
    assert "vendor said" in err.error["message"]


# --------------------------- 本地日志出口 ---------------------------


def test_client_error_log_has_no_plaintext(leak_upstream: str) -> None:
    with patch.object(client_mod.logger, "warning") as warn:
        with pytest.raises(ProviderError):
            asyncio.run(UnifiedApiClient(leak_upstream).request_json("POST", "/v1/token/refresh", {"a": 1}))
    assert warn.called, "上游 401 片段本该进本地日志"
    fmt, *parts = warn.call_args.args
    logged = fmt % tuple(parts) if parts else str(fmt)
    for needle in LEAK_MARKERS:
        assert needle not in logged
    assert "***" in logged
    assert "返回 401" in logged


# --------------------------- 四个适配器的对外错误 ---------------------------

DEAD_BASE = "http://user:pass@127.0.0.1:9/upstream?key=zzzzzzzz"
USERS = [{"role": "user", "content": "hi"}]


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "unified.db",
        web_dir=tmp_path / "web",
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


@pytest.mark.parametrize("adapter_id", ["opencode", "muse", "zcode", "gptgrok"])
def test_adapter_network_error_hides_base_url_and_repr(tmp_path: Path, adapter_id: str) -> None:
    builders = {"opencode": opencode.build, "muse": muse.build, "zcode": zcode.build, "gptgrok": gptgrok.build}
    adapter = builders[adapter_id](_settings(tmp_path), DEAD_BASE)
    adapter.configure(api_key="tok-1", options={"max_attempts": 1})
    boom = httpx.ConnectError(f"All connection attempts failed for {DEAD_BASE}")
    with patch.object(UnifiedApiClient, "forward", new=_raising(boom)):
        with pytest.raises(ProviderError) as caught:
            asyncio.run(adapter.chat_completion({"model": "gpt-5", "messages": USERS}, stream=False))
    msg = caught.value.message
    assert caught.value.status_code == 502
    assert "user:pass" not in msg and "zzzzzzzz" not in msg and "key=zzzzzzzz" not in msg
    assert "ConnectError" in msg, "异常类型名要留着"
    assert "All connection attempts failed" in msg, "脱敏后的关键信息要留着"
    assert "ConnectError(" not in msg, "repr 原文不得出现"


def _raising(exc: BaseException):
    async def _boom(*_args, **_kwargs):
        raise exc

    return _boom


# --------------------------- 端到端：三处出口都无明文 ---------------------------


class _LeakHandler(BaseHTTPRequestHandler):
    """任何请求都回一条带凭据的 401，用来打穿错误响应 / 日志 / 落库三条出口。"""

    def do_POST(self):  # noqa: N802
        payload = LEAK_BODY.encode()
        self.send_response(401)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST

    def log_message(self, *_args) -> None:
        return


@pytest.fixture(scope="module")
def leak_upstream():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LeakHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def codebuddy_on_leak(admin: httpx.Client, leak_upstream: str):
    before = {item["id"]: item for item in admin.get("/api/providers").json()}["codebuddy"]
    patched = admin.patch("/api/providers/codebuddy", json={"enabled": True, "base_url": leak_upstream})
    assert patched.status_code == 200, patched.text
    try:
        yield leak_upstream
    finally:
        restore: dict[str, object] = {"enabled": bool(before.get("enabled"))}
        restore["base_url"] = before["base_url"] if before.get("base_url_source") == "db" else ""
        admin.patch("/api/providers/codebuddy", json=restore)


@pytest.fixture(scope="module")
def bearer_key(admin: httpx.Client) -> str:
    created = admin.post("/api/keys", json={"name": "secret-mask-probe"})
    assert created.status_code == 200, created.text
    return created.json()["key"]


def test_upstream_401_has_no_plaintext_in_all_three_exits(
    anon: httpx.Client, admin: httpx.Client, db_path: str, codebuddy_on_leak: None, bearer_key: str
) -> None:
    resp = anon.post(
        "/v1/chat/completions",
        headers={"authorization": f"Bearer {bearer_key}", "x-provider": "codebuddy"},
        json={"model": LEAK_MODEL, "messages": USERS},
    )
    assert resp.status_code == 401, resp.text

    # ① 下游错误响应
    err = resp.json()["error"]
    assert err["type"] == "authentication_error"
    assert err["code"] == "upstream_auth_failed"
    assert err["upstream_status"] == 401
    assert "上游返回 401" in err["message"]
    assert "auth failed" in err["message"]
    for needle in LEAK_MARKERS:
        assert needle not in resp.text, f"错误响应泄露 {needle}"

    # ② /api/logs
    logs = admin.get("/api/logs", params={"model": LEAK_MODEL, "status": "error", "limit": 5}).json()
    rows = [item for item in logs["items"] if item.get("model") == LEAK_MODEL]
    assert rows, "这条失败没进 /api/logs"
    for item in rows:
        for needle in LEAK_MARKERS:
            assert needle not in json.dumps(item, ensure_ascii=False), f"/api/logs 泄露 {needle}"
    assert "auth failed" in (rows[0]["error"] or "")

    # ③ 数据库里的 error 字段
    conn = sqlite3.connect(db_path)
    try:
        stored = [row[0] or "" for row in conn.execute("SELECT error FROM request_logs WHERE model = ?", (LEAK_MODEL,))]
    finally:
        conn.close()
    assert stored, "这条失败没落库"
    for text in stored:
        for needle in LEAK_MARKERS:
            assert needle not in text, f"request_logs.error 泄露 {needle}"
    assert any("auth failed" in text for text in stored)
