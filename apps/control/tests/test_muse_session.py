"""muse 会话池回归：解析去重、轮换、到期判定、保活续期与原子落盘。"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

from app.adapters import muse
from app.services.muse_session import SessionPool, keepalive

MUSE_PATHS = {"chat_path": "/muse/v1/chat/completions", "health_path": "/muse/v1/models"}


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch) -> None:
    """每个用例独占状态目录，避免 cookie 寿命跨用例串账。"""
    monkeypatch.setenv("UNIFIED_MUSE_STATE_DIR", str(tmp_path))


def _settings(tmp: Path) -> Any:
    from app.config import Settings

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
        allow_private_upstreams=False,
    )


def _muse(base: str, tmp: Path, api_key: str, extra: list[str] | None = None, **options: Any):
    adapter = muse.build(_settings(tmp), base)
    adapter.configure(api_key=api_key, options={**MUSE_PATHS, "extra_keys": extra or [], **options})
    return adapter


@contextlib.contextmanager
def _refresh_server(replies: list[dict[str, Any]]) -> Iterator[tuple[str, list[str]]]:
    """本地续期端点：按顺序吐出预置响应，并记录每次收到的 Cookie。"""
    calls: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            calls.append(self.headers.get("Cookie") or "")
            payload = replies.pop(0) if replies else {}
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", calls
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_from_options_parses_and_dedupes(tmp_path: Path) -> None:
    now = int(time.time())
    pool = SessionPool.from_options(
        {
            "cookie_pool": [
                "hatch_sess=a",
                {"cookie": "hatch_sess=b", "cookies_exp": now + 7200, "label": "B"},
                "hatch_sess=a",
                {"cookie": ""},
            ],
            "cookie": "hatch_sess=fallback",
        },
        _settings(tmp_path),
    )
    assert [entry["cookie"] for entry in pool.entries] == ["hatch_sess=a", "hatch_sess=b"]
    assert pool.entries[0]["cookies_exp"] == 0, "纯字符串条目没有到期时间"
    assert pool.entries[1]["cookies_exp"] == now + 7200
    assert pool.entries[1]["label"] == "B"


def test_from_options_plain_cookie_only(tmp_path: Path) -> None:
    pool = SessionPool.from_options({"cookie": "hatch_sess=only"}, _settings(tmp_path))
    assert [entry["cookie"] for entry in pool.entries] == ["hatch_sess=only"]
    assert pool.entries[0]["cookies_exp"] == 0


def test_pick_round_robins_live_entries(tmp_path: Path) -> None:
    pool = SessionPool.from_options(
        {"cookie_pool": ["hatch_sess=a", "hatch_sess=b"]}, _settings(tmp_path)
    )
    first, second = pool.pick(), pool.pick()
    assert first != second
    assert {first, second} == {"hatch_sess=a", "hatch_sess=b"}
    assert pool.pick() == first, "两条都可用时按条目顺序轮换"


def test_pick_empty_when_all_expired(tmp_path: Path) -> None:
    now = int(time.time())
    pool = SessionPool.from_options(
        {
            "cookie_pool": [
                {"cookie": "hatch_sess=a", "cookies_exp": now - 60},
                {"cookie": "hatch_sess=b", "cookies_exp": now - 120},
            ]
        },
        _settings(tmp_path),
    )
    assert pool.pick(now=now) == ""


async def test_apply_expiry_never_inflates_local(tmp_path: Path) -> None:
    now = int(time.time())
    pool = SessionPool.from_options(
        {"cookie_pool": [{"cookie": "hatch_sess=a", "cookies_exp": now + 600}]},
        _settings(tmp_path),
    )

    assert await pool.apply_expiry("hatch_sess=a", now + 7200) is True
    assert pool.entries[0]["cookies_exp"] == now + 7200
    assert pool.entries[0]["updated_at"] >= now

    assert await pool.apply_expiry("hatch_sess=a", now + 7200) is False
    assert pool.entries[0]["cookies_exp"] == now + 7200, "相同值不得虚增寿命"
    assert pool.entries[0]["refresh_failures"] == 0

    assert await pool.apply_expiry("hatch_sess=a", 0) is False
    assert pool.entries[0]["cookies_exp"] == now + 7200, "不给值必须保持原寿命"
    assert pool.entries[0]["refresh_failures"] == 1


async def test_keepalive_only_refreshes_due_entries(tmp_path: Path) -> None:
    now = int(time.time())
    pool = SessionPool.from_options(
        {
            "cookie_pool": [
                {"cookie": "hatch_sess=a", "cookies_exp": now + 600},
                {"cookie": "hatch_sess=b", "cookies_exp": now + 900},
            ]
        },
        _settings(tmp_path),
    )
    with _refresh_server([{"cookies_exp": now + 7200}, {}]) as (base, calls):
        adapter = _muse(base, tmp_path, "tok-key", cookie_refresh_path="/session/refresh")
        stats = await keepalive(pool, adapter, now=now)

    assert stats == {"checked": 2, "refreshed": 1, "failed": 1}
    assert len(calls) == 2
    entries = {entry["cookie"]: entry for entry in pool.entries}
    assert entries["hatch_sess=a"]["cookies_exp"] == now + 7200
    assert entries["hatch_sess=a"]["refresh_failures"] == 0
    assert entries["hatch_sess=b"]["cookies_exp"] == now + 900, "空响应不得虚增寿命"
    assert entries["hatch_sess=b"]["refresh_failures"] == 1


def test_headers_use_pool_only_when_configured(tmp_path: Path) -> None:
    pooled = _muse(
        "http://127.0.0.1:1", tmp_path, "tok", cookie_pool=["hatch_sess=a", "hatch_sess=b"]
    )
    first = pooled._headers("tok", stream=False)["cookie"]
    second = pooled._headers("tok", stream=False)["cookie"]
    assert first != second
    assert {first, second} == {"hatch_sess=a", "hatch_sess=b"}

    legacy = _muse("http://127.0.0.1:1", tmp_path, "tok", cookie="hatch_sess=single")
    assert legacy._headers("tok", stream=False)["cookie"] == "hatch_sess=single"


async def test_state_file_roundtrip(tmp_path: Path) -> None:
    pool = SessionPool.from_options({"cookie_pool": ["hatch_sess=keep"]}, _settings(tmp_path))
    assert await pool.apply_expiry("hatch_sess=keep", 1_800_000_000)

    path = tmp_path / "muse_sessions.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["entries"][0]["cookies_exp"] == 1_800_000_000
    assert not path.with_suffix(".tmp").exists(), "原子替换后不能留下临时文件"

    again = SessionPool.from_options({"cookie_pool": ["hatch_sess=keep"]}, _settings(tmp_path))
    assert again.entries == pool.entries
