"""发布源探测：本地 HTTP 假服务覆盖 404 / 超时 / 缺签名 / 版本相同 / 正常验签。

全程只连 127.0.0.1 的临时端口（代码对回环放开 http），不依赖外网。
"""

from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app import __version__
from app.services.update_feed import UpdateFeed
from app.services.update_signing import public_pem, sign

SEED = bytes(range(32))


@dataclass
class Settings:
    data_dir: Path
    db_path: Path
    update_enabled: bool = True
    update_manifest_url: str = ""
    update_pubkey: Path = Path("deploy/release-pubkey.pem")
    update_check_ttl_sec: int = 21600
    update_max_bytes: int = 268435456
    service_name: str = ""
    port: int = 18999


def manifest_bytes(version: str, *, channel: str = "stable", platform: str = "any") -> bytes:
    body = {
        "manifest_version": 1,
        "version": version,
        "channel": channel,
        "platform": platform,
        "issued_at": 1793877187,
        "commit": "deadbeef",
        "min_current_version": "0.0.0",
        "files": [{"path": "apps/control/app/main.py", "sha256": "0" * 64, "size": 0}],
    }
    return json.dumps(body, ensure_ascii=False, sort_keys=True).encode()


class _Handler(BaseHTTPRequestHandler):
    routes: dict[str, bytes] = {}
    hits: list[str] = []

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        _Handler.hits.append(self.path)
        if self.path.startswith("/slow"):
            time.sleep(1.0)
        payload = _Handler.routes.get(self.path)
        try:
            if payload is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端已超时断开

    def log_message(self, *args):  # 静音，pytest 输出干净
        pass


@dataclass
class FeedServer:
    base: str
    routes: dict[str, bytes] = field(default_factory=dict)
    hits: list[str] = field(default_factory=list)


@pytest.fixture()
def feed_server(tmp_path: Path, monkeypatch):
    routes: dict[str, bytes] = {}
    hits: list[str] = []
    monkeypatch.setattr(_Handler, "routes", routes)
    monkeypatch.setattr(_Handler, "hits", hits)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield FeedServer(base=base, routes=routes, hits=hits)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def publish(server: FeedServer, prefix: str, version: str, *, signature: bool = True) -> str:
    """往假服务上架一份 manifest（可选带 base64 签名），返回 manifest URL。"""
    raw = manifest_bytes(version)
    route = f"/{prefix}/release-manifest.json"
    server.routes[route] = raw
    if signature:
        server.routes[route + ".sig"] = base64.b64encode(sign(SEED, raw))
    return server.base + route


def make_settings(tmp_path: Path, url: str) -> Settings:
    pubkey = tmp_path / "release-pubkey.pem"
    pubkey.write_text(public_pem(SEED), encoding="utf-8")
    return Settings(
        data_dir=tmp_path,
        db_path=tmp_path / "unified.db",
        update_manifest_url=url,
        update_pubkey=pubkey,
    )


def test_signature_is_valid_for_published_manifest(tmp_path: Path, feed_server: FeedServer):
    url = publish(feed_server, "ok", "9.9.9")
    settings = make_settings(tmp_path, url)
    status = UpdateFeed(settings, timeout=2.0).summary()
    assert status["available"] is True
    assert status["latest_version"] == "9.9.9"
    assert status["signature"] == "verified"
    assert status["reason"] == ""
    assert status["current_version"] == __version__
    for key in ("channel", "updater_found", "in_container", "can_apply", "cached"):
        assert key in status


def test_manifest_404_reports_reason(tmp_path: Path, feed_server: FeedServer):
    settings = make_settings(tmp_path, feed_server.base + "/missing/release-manifest.json")
    status = UpdateFeed(settings, timeout=2.0).summary()
    assert status["available"] is False
    assert status["signature"] == "none"
    assert "HTTP 404" in status["reason"]


def test_timeout_reports_reason(tmp_path: Path, feed_server: FeedServer):
    settings = make_settings(tmp_path, feed_server.base + "/slow/release-manifest.json")
    status = UpdateFeed(settings, timeout=0.2).summary()
    assert status["available"] is False
    assert "超时" in status["reason"]


def test_missing_signature_is_refused(tmp_path: Path, feed_server: FeedServer):
    url = publish(feed_server, "nosig", "9.9.9", signature=False)
    settings = make_settings(tmp_path, url)
    status = UpdateFeed(settings, timeout=2.0).summary()
    assert status["available"] is False
    assert status["signature"] == "unsigned"
    assert "签名" in status["reason"]


def test_same_version_is_not_offered(tmp_path: Path, feed_server: FeedServer):
    url = publish(feed_server, "same", __version__)
    settings = make_settings(tmp_path, url)
    status = UpdateFeed(settings, timeout=2.0).summary()
    assert status["available"] is False
    assert status["signature"] == "verified"
    assert "拒绝降级" in status["reason"]


def test_disabled_does_not_touch_network(tmp_path: Path, feed_server: FeedServer):
    url = publish(feed_server, "ok", "9.9.9")
    settings = make_settings(tmp_path, url)
    settings.update_enabled = False
    before = len(feed_server.hits)
    status = UpdateFeed(settings, timeout=2.0).summary()
    assert len(feed_server.hits) == before
    assert status["available"] is False
    assert "未启用" in status["reason"]
    assert status["can_apply"] is False


def test_cache_avoids_second_request(tmp_path: Path, feed_server: FeedServer):
    url = publish(feed_server, "cached", "9.9.9")
    settings = make_settings(tmp_path, url)
    feed = UpdateFeed(settings, timeout=2.0)
    feed.summary()
    first = len(feed_server.hits)
    assert feed.summary()["cached"] is True
    assert len(feed_server.hits) == first


def test_archive_url_convention():
    from app.services.update_manifest import archive_url

    assert archive_url("https://example.com/a/release-manifest.json") == "https://example.com/a/release.zip"
    assert archive_url("https://example.com/a/manifest.json") == "https://example.com/a/manifest.json.zip"


def test_rejects_plain_http_outside_loopback(tmp_path: Path):
    settings = make_settings(tmp_path, "http://example.com/release-manifest.json")
    status = UpdateFeed(settings, timeout=2.0).summary()
    assert status["available"] is False
    assert "HTTPS" in status["reason"]
