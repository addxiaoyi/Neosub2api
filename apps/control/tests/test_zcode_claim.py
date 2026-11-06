"""2-C5：周末额度领取的开关、周内去重与严格成功判定（stub 上游，注入 now）。"""

import datetime
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread

import pytest

from app.adapters.zcode import ZCodeAdapter
from app.config import load_settings
from app.services.zcode_claim import CST, ZcodeClaimService

# 2026-11-07 周六、2026-11-05 周四（北京时间）
WEEKEND = datetime.datetime(2026, 11, 7, 12, 0, tzinfo=CST)
WEEKDAY = datetime.datetime(2026, 11, 5, 12, 0, tzinfo=CST)
TOKEN = "zcode-weekend-token"


class _StubServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address):
        super().__init__(address, _Handler)
        self.calls: list[dict] = []
        self.lock = Lock()
        self.status = 200
        self.reply: dict = {}


class _Handler(BaseHTTPRequestHandler):
    server: _StubServer

    def log_message(self, fmt, *args):
        return

    def _send(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def do_POST(self):  # noqa: N802
        if self.path != "/claim":
            self._send(404, {"error": "not found"})
            return
        body = self._body()
        with self.server.lock:
            self.server.calls.append(
                {"body": body, "authorization": self.headers.get("authorization")}
            )
            status, reply = self.server.status, self.server.reply
        self._send(status, reply)


@pytest.fixture
def stub():
    server = _StubServer(("127.0.0.1", 0))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _adapter(stub, tmp_path, monkeypatch, **options) -> tuple:
    monkeypatch.setenv("UNIFIED_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("UNIFIED_ADMIN_PASSWORD", "test")
    settings = load_settings()
    adapter = ZCodeAdapter(settings, f"http://127.0.0.1:{stub.server_address[1]}")
    adapter.configure(api_key=TOKEN, options=options)
    return settings, adapter


def _service(settings, adapter, tmp_path) -> ZcodeClaimService:
    return ZcodeClaimService(settings, adapter=adapter, state_dir=tmp_path / "state")


def _fingerprint() -> str:
    return hashlib.sha256(TOKEN.encode()).hexdigest()[:12]


def _stamp(moment: datetime.datetime) -> str:
    iso = moment.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


async def test_disabled_makes_zero_requests(stub, tmp_path, monkeypatch):
    settings, adapter = _adapter(
        stub, tmp_path, monkeypatch, weekend_claim=False, claim_path="/claim"
    )
    svc = _service(settings, adapter, tmp_path)

    assert svc.due(WEEKEND) is False
    result = await svc.run(adapter, now=WEEKEND)

    assert result == {"due": False, "checked": 0, "claimed": 0, "skipped": 0, "failed": 0}
    assert stub.calls == []
    assert not (tmp_path / "state").exists()


async def test_empty_claim_path_is_noop(stub, tmp_path, monkeypatch):
    settings, adapter = _adapter(
        stub, tmp_path, monkeypatch, weekend_claim=True, claim_path=""
    )
    svc = _service(settings, adapter, tmp_path)

    assert svc.due(WEEKEND) is False
    result = await svc.run(adapter, now=WEEKEND)

    assert result["checked"] == 0
    assert stub.calls == []


async def test_weekday_is_noop(stub, tmp_path, monkeypatch):
    settings, adapter = _adapter(
        stub,
        tmp_path,
        monkeypatch,
        weekend_claim=True,
        claim_path="/claim",
        claim_window="sat,sun",
    )
    svc = _service(settings, adapter, tmp_path)

    assert svc.due(WEEKEND) is True
    assert svc.due(WEEKDAY) is False
    await svc.run(adapter, now=WEEKDAY)

    assert stub.calls == []


async def test_same_week_claims_once(stub, tmp_path, monkeypatch):
    stub.reply = {"claimed": True}
    settings, adapter = _adapter(
        stub, tmp_path, monkeypatch, weekend_claim=True, claim_path="/claim"
    )
    svc = _service(settings, adapter, tmp_path)

    first = await svc.run(adapter, now=WEEKEND)
    assert first == {"due": True, "checked": 1, "claimed": 1, "skipped": 0, "failed": 0}
    assert len(stub.calls) == 1
    assert stub.calls[0]["authorization"] == f"Bearer {TOKEN}"

    second = await svc.run(adapter, now=WEEKEND)
    assert second["claimed"] == 0
    assert second["skipped"] == 1
    assert len(stub.calls) == 1

    state = json.loads(svc.state_path.read_text(encoding="utf-8"))
    assert state["claimed"] == {_fingerprint(): _stamp(WEEKEND)}


async def test_http_200_empty_body_not_claimed(stub, tmp_path, monkeypatch):
    stub.reply = {}
    settings, adapter = _adapter(
        stub, tmp_path, monkeypatch, weekend_claim=True, claim_path="/claim"
    )
    svc = _service(settings, adapter, tmp_path)

    first = await svc.run(adapter, now=WEEKEND)
    assert first["claimed"] == 0
    assert first["failed"] == 0
    assert not svc.state_path.exists()

    second = await svc.run(adapter, now=WEEKEND)
    assert second["claimed"] == 0
    assert len(stub.calls) == 2
