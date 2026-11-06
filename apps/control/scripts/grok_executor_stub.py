"""本地注册执行器 stub：纯标准库 HTTP 服务，无浏览器依赖。

用途：在没有任何真执行器时，让控制面的注册任务链路（提交→轮询→结果→账号落库）
端到端跑通，并演示 pending → running → done/failed 的状态流转。
协议：grok-register-executor/v1（docs/providers/grok-register.md）。

用法：
    python grok_executor_stub.py --port 0
    # stdout 首行输出 PORT=<实际端口>，把它填进 GROK_REGISTER_EXECUTOR_URL=http://127.0.0.1:<port>

每个任务的行为也可由任务 payload 覆盖：
    {"stub": {"mode": "success|fail|hang|badjson|http500", "polls": 2, "delay": 0.2}}
"""

import argparse
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODES = ("success", "fail", "hang", "badjson", "http500")


class _Job:
    def __init__(self, body: dict[str, Any], defaults: dict[str, Any]) -> None:
        stub = body.get("payload")
        stub = stub.get("stub") if isinstance(stub, dict) else None
        stub = stub if isinstance(stub, dict) else {}
        mode = str(stub.get("mode") or defaults["mode"]).lower()
        self.id = uuid.uuid4().hex[:12]
        self.mode = mode if mode in MODES else "success"
        self.count = max(1, min(int(body.get("count") or 1), 10))
        self.workers = max(1, int(body.get("workers") or 1))
        self.polls = max(1, int(stub.get("polls", defaults["polls"])))
        self.delay = max(0.0, float(stub.get("delay", defaults["delay"])))
        self.seen = 0
        self.status = "pending"
        self.error = ""
        self.created_at = int(time.time())
        self.accounts: list[dict[str, Any]] = []

    def snapshot(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "job_id": self.id,
            "status": self.status,
            "progress": {"done": len(self.accounts), "total": self.count},
        }
        if self.error:
            body["error"] = self.error
        if self.accounts:
            body["accounts"] = self.accounts
        return body

    def advance(self) -> dict[str, Any]:
        """每次轮询推进一步；返回当前快照。"""
        self.seen += 1
        if self.status in {"done", "failed", "cancelled"}:
            return self.snapshot()
        if self.mode == "hang":
            self.status = "running" if self.seen > 1 else "pending"
            return self.snapshot()
        if self.mode == "fail":
            if self.seen >= 2:
                self.status = "failed"
                self.error = "stub: 上游返回 403，注册被风控拦截"
            else:
                self.status = "running"
            return self.snapshot()
        if self.seen >= self.polls + 1:
            self.status = "done"
            self.accounts = [
                {
                    "uid": f"stub-{self.id}-{i + 1}",
                    "nickname": f"stub-user-{i + 1}",
                    "cookie": f"stub-cookie-{self.id}-{i + 1}",
                    "token": f"stub-token-{self.id}-{i + 1}",
                    "expires_at": int(time.time()) + 3600,
                    "realm": "intl",
                    "credits": 0,
                }
                for i in range(self.count)
            ]
        else:
            self.status = "running"
        return self.snapshot()


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, *, mode: str, polls: int, delay: float) -> None:
        super().__init__(address, handler)
        self.mode = mode
        self.polls = polls
        self.delay = delay
        self.jobs: dict[str, _Job] = {}
        self.lock = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默：测试里不要刷屏
        return

    def _send(self, status: int, payload: Any, content_type: str = "application/json") -> None:
        raw = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._send(200, {"ok": True, "mode": self.server.mode})
            return
        if self.path.startswith("/jobs/"):
            job_id = self.path[len("/jobs/"):].split("?")[0]
            with self.server.lock:
                job = self.server.jobs.get(job_id)
                if job is None:
                    self._send(404, {"error": f"unknown job {job_id}"})
                    return
                if job.delay:
                    time.sleep(job.delay)
                if job.mode == "badjson":
                    self._send(200, b"<html>not json</html>", "text/html")
                    return
                if job.mode == "http500":
                    self._send(500, {"error": "stub: 执行器内部错误"})
                    return
                self._send(200, job.advance())
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/jobs":
            body = self._body()
            with self.server.lock:
                job = _Job(
                    body,
                    {
                        "mode": self.server.mode,
                        "polls": self.server.polls,
                        "delay": self.server.delay,
                    },
                )
                self.server.jobs[job.id] = job
            self._send(201, {"job_id": job.id, "status": "pending"})
            return
        if self.path.startswith("/jobs/") and self.path.endswith("/cancel"):
            job_id = self.path[len("/jobs/"): -len("/cancel")]
            with self.server.lock:
                job = self.server.jobs.get(job_id)
                if job is None:
                    self._send(404, {"error": f"unknown job {job_id}"})
                    return
                job.status = "cancelled"
                job.error = "stub: 已按取消请求终止"
                self._send(200, job.snapshot())
            return
        self._send(404, {"error": "not found"})


def create_server(
    host: str = "127.0.0.1", port: int = 0, *, mode: str = "success", polls: int = 2, delay: float = 0.0
) -> _Server:
    """建服务但不启动；返回对象带 server_address / serve() 可直接在测试线程里跑。"""
    if mode not in MODES:
        mode = "success"
    return _Server((host, port), _Handler, mode=mode, polls=polls, delay=delay)


def main() -> None:
    parser = argparse.ArgumentParser(description="Grok 注册执行器 stub（无浏览器依赖）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="0 表示由系统分配空闲端口")
    parser.add_argument("--mode", choices=MODES, default="success")
    parser.add_argument("--polls", type=int, default=2, help="第几次轮询后进入终态")
    parser.add_argument("--delay", type=float, default=0.0, help="每次轮询人为延迟秒数")
    args = parser.parse_args()

    server = create_server(args.host, args.port, mode=args.mode, polls=args.polls, delay=args.delay)
    print(f"PORT={server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
