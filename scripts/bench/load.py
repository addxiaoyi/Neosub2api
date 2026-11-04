"""网关异步压测：纯转发 / 带配额 / 失败限流三种场景，只用 httpx 现有依赖。

默认自起假上游 + 临时数据目录 + 控制面（端口取 UNIFIED_TEST_CTRL_PORT，缺省 18127），
跑完按并发档位输出 Markdown 表：p50/p95/p99、QPS、错误率、SQLite 是否出现 database is locked。

用法：
    python scripts/bench/load.py --concurrency 8,32,64 --duration 10 --provider codebuddy
    python scripts/bench/load.py --base-url http://127.0.0.1:18127 --api-key u2a_xxx --no-boot

退出码：脚本自身异常 > 0；被测服务 5xx 只记在错误率里，不中断。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[2]
CONTROL = REPO / "apps" / "control"
FAKE_UPSTREAM = CONTROL / "scripts" / "fake_upstream.py"
PY = sys.executable

CTRL_PORT = int(os.getenv("UNIFIED_TEST_CTRL_PORT", "18127"))
FAKE_PORT = int(os.getenv("UNIFIED_TEST_FAST_PORT", str(CTRL_PORT + 1)))
ADMIN_PASSWORD = "bench-admin"

SCENARIO_CHAT = "chat"
SCENARIO_QUOTA = "quota"
SCENARIO_RATELIMIT = "ratelimit"
ALL_SCENARIOS = (SCENARIO_CHAT, SCENARIO_QUOTA, SCENARIO_RATELIMIT)

# 假上游对 model 里含 err-<code> 的请求直接回该状态码，用来制造失败流量
FAILING_MODEL = "fake-chat-1-err-500"
PLAIN_MODEL = "fake-chat-1"


def _port_free(port: int) -> bool:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        sock.close()
    return True


def _wait_up(url: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=1.0, trust_env=False).status_code < 500:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


class Service:
    """假上游 + 控制面 + 管理员会话，一次压测只起一套。"""

    def __init__(self, failure_max: int, keep_data: bool, log_level: str = "warning",
                 ssl_shim: bool = True):
        self.failure_max = failure_max
        self.keep_data = keep_data
        self.log_level = log_level
        self.ssl_shim = ssl_shim
        self.procs: list[subprocess.Popen] = []
        self.data_dir = Path(tempfile.mkdtemp(prefix="u2a-bench-"))
        self.log_path = self.data_dir / "control.log"
        self.client = httpx.Client(
            base_url=f"http://127.0.0.1:{CTRL_PORT}", timeout=30.0, trust_env=False
        )

    def start(self) -> None:
        if not _port_free(FAKE_PORT) or not _port_free(CTRL_PORT):
            raise RuntimeError(f"端口被占用：ctrl={CTRL_PORT} fake={FAKE_PORT}，用 "
                               f"UNIFIED_TEST_CTRL_PORT / UNIFIED_TEST_FAST_PORT 换一组")
        fake_log = (self.data_dir / "fake.log").open("wb")
        self.procs.append(subprocess.Popen(
            [PY, str(FAKE_UPSTREAM), str(FAKE_PORT)],
            cwd=str(CONTROL), stdout=fake_log, stderr=subprocess.STDOUT,
        ))
        fake_log.close()
        env = os.environ.copy()
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
                     "https_proxy", "all_proxy", "NO_PROXY", "no_proxy"):
            env.pop(name, None)
        env.update({
            "UNIFIED_DATA_DIR": str(self.data_dir / "data"),
            "UNIFIED_DB_PATH": str(self.data_dir / "data" / "unified.db"),
            "UNIFIED_ADMIN_USER": "admin",
            "UNIFIED_ADMIN_PASSWORD": ADMIN_PASSWORD,
            "UNIFIED_WEB_DIR": str(self.data_dir / "no-web"),
            "UNIFIED_ALLOW_PRIVATE_UPSTREAMS": "1",
            "UNIFIED_ALLOW_ANONYMOUS_GATEWAY": "false",
            "UNIFIED_FAILURE_WINDOW_SEC": "60",
            "UNIFIED_FAILURE_WINDOW_MAX": str(self.failure_max),
            "UNIFIED_LOG_LEVEL": self.log_level,
            "CODEBUDDY_BASE": f"http://127.0.0.1:{FAKE_PORT}",
            "GROK_REGISTER_EXECUTOR_URL": "",
        })
        ctrl_log = self.log_path.open("wb")
        if self.ssl_shim:
            cmd = [PY, str(Path(__file__).resolve().parent / "serve.py"),
                   str(CTRL_PORT), self.log_level]
        else:
            cmd = [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                   "--port", str(CTRL_PORT), "--log-level", self.log_level]
        self.procs.append(subprocess.Popen(
            cmd, cwd=str(CONTROL), env=env, stdout=ctrl_log, stderr=subprocess.STDOUT,
        ))
        ctrl_log.close()
        if not _wait_up(f"http://127.0.0.1:{FAKE_PORT}/healthz"):
            raise RuntimeError("假上游启动失败")
        if not _wait_up(f"http://127.0.0.1:{CTRL_PORT}/healthz"):
            raise RuntimeError(f"控制面启动失败，见日志 {self.log_path}")
        self._login()

    def _login(self) -> None:
        resp = self.client.post("/api/login", json={"username": "admin", "password": ADMIN_PASSWORD})
        if resp.status_code != 200:
            raise RuntimeError(f"管理员登录失败：{resp.status_code} {resp.text[:200]}")

    def new_key(self, name: str) -> tuple[int, str]:
        resp = self.client.post("/api/keys", json={"name": name})
        if resp.status_code != 200:
            raise RuntimeError(f"创建密钥失败：{resp.status_code} {resp.text[:200]}")
        payload = resp.json()
        return int(payload["id"]), str(payload["key"])

    def set_request_quota(self, key_id: int, limit_requests: int) -> None:
        resp = self.client.post("/api/quota/policies", json={
            "key_id": key_id,
            "window": "day",
            "limit_requests": limit_requests,
            "thresholds": [],
        })
        if resp.status_code != 200:
            raise RuntimeError(f"创建配额策略失败：{resp.status_code} {resp.text[:200]}")

    def locked_in_log(self) -> int:
        try:
            return self.log_path.read_bytes().lower().count(b"database is locked")
        except FileNotFoundError:
            return 0

    def stop(self) -> None:
        self.client.close()
        for proc in reversed(self.procs):
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=3)
                except Exception:
                    pass
        if not self.keep_data:
            shutil.rmtree(self.data_dir, ignore_errors=True)


def _percentile(sorted_ms: list[float], pct: float) -> float:
    if not sorted_ms:
        return 0.0
    index = min(len(sorted_ms) - 1, max(0, int(round((pct / 100) * len(sorted_ms)) - 1)))
    return sorted_ms[index]


async def run_one(
    *,
    base_url: str,
    api_key: str,
    provider: str,
    model: str,
    concurrency: int,
    duration: float,
) -> dict:
    latencies: list[float] = []
    statuses: Counter[int] = Counter()
    exceptions: Counter[str] = Counter()
    locked = 0
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "bench"}],
        "max_tokens": 16,
    }
    headers = {"authorization": f"Bearer {api_key}", "x-provider": provider}
    deadline = time.perf_counter() + duration
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)

    async with httpx.AsyncClient(
        base_url=base_url, timeout=60.0, trust_env=False, limits=limits
    ) as client:
        async def worker() -> None:
            nonlocal locked
            while time.perf_counter() < deadline:
                started = time.perf_counter()
                try:
                    resp = await client.post("/v1/chat/completions", json=payload, headers=headers)
                except Exception as exc:  # 连接层失败单独计数，不当成 HTTP 状态
                    exceptions[type(exc).__name__] += 1
                    latencies.append((time.perf_counter() - started) * 1000)
                    continue
                latencies.append((time.perf_counter() - started) * 1000)
                statuses[resp.status_code] += 1
                if b"database is locked" in resp.content.lower():
                    locked += 1

        await asyncio.gather(*[worker() for _ in range(concurrency)])

    latencies.sort()
    total = len(latencies)
    failed = sum(count for status, count in statuses.items() if status >= 400) + sum(exceptions.values())
    ok = total - failed
    return {
        "concurrency": concurrency,
        "requests": total,
        "qps": round(ok / duration, 1),
        "total_qps": round(total / duration, 1),
        "p50": round(_percentile(latencies, 50), 1),
        "p95": round(_percentile(latencies, 95), 1),
        "p99": round(_percentile(latencies, 99), 1),
        "error_rate": round(failed / total, 3) if total else 0.0,
        "statuses": dict(sorted(statuses.items())),
        "exceptions": dict(exceptions),
        "locked_responses": locked,
    }


def _print_table(rows: list[dict], log_locked: int) -> None:
    print("\n| 场景 | 并发 | 请求数 | QPS(成功) | p50 ms | p95 ms | p99 ms | 错误率 | 状态分布 | locked |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in rows:
        statuses = ", ".join(f"{k}:{v}" for k, v in row["statuses"].items())
        if row["exceptions"]:
            statuses += ", " + ", ".join(f"{k}:{v}" for k, v in row["exceptions"].items())
        print(
            f"| {row['scenario']} | {row['concurrency']} | {row['requests']} | {row['qps']} | "
            f"{row['p50']} | {row['p95']} | {row['p99']} | {row['error_rate']} | {statuses} | "
            f"{row['locked_responses']} |"
        )
    print(f"\n控制面日志里 database is locked 出现次数：{log_locked}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="unified-2api 网关压测")
    parser.add_argument("--concurrency", default="8,32,64",
                        help="并发档位，逗号分隔，例如 8,32,64")
    parser.add_argument("--duration", type=float, default=10.0, help="每档持续秒数")
    parser.add_argument("--provider", default="codebuddy", help="x-provider 头")
    parser.add_argument("--scenario", default=",".join(ALL_SCENARIOS),
                        help="chat,quota,ratelimit 任意组合")
    parser.add_argument("--failure-max", type=int, default=10,
                        help="失败限流窗内上限（控制面 UNIFIED_FAILURE_WINDOW_MAX）")
    parser.add_argument("--quota-limit", type=int, default=10_000_000,
                        help="quota 场景的请求数上限，需大于压测量")
    parser.add_argument("--base-url", help="复用已启动的控制面时传入")
    parser.add_argument("--api-key", help="配合 --base-url 直接指定密钥")
    parser.add_argument("--no-boot", action="store_true", help="不自起服务")
    parser.add_argument("--keep-data", action="store_true", help="保留临时数据目录")
    parser.add_argument("--log-level", default="warning", help="控制面日志级别，排障时用 info")
    parser.add_argument("--no-ssl-shim", action="store_true",
                        help="不用 SSLContext 缓存垫片，复现裸 httpx 环境开销")
    parser.add_argument("--json", action="store_true", help="额外输出 JSON")
    args = parser.parse_args(argv)

    tiers = [int(item) for item in str(args.concurrency).split(",") if item.strip()]
    scenarios = [item.strip() for item in str(args.scenario).split(",") if item.strip()]
    unknown = [item for item in scenarios if item not in ALL_SCENARIOS]
    if unknown:
        parser.error(f"未知场景：{unknown}，可选 {ALL_SCENARIOS}")

    service: Service | None = None
    base_url = args.base_url or f"http://127.0.0.1:{CTRL_PORT}"
    rows: list[dict] = []
    try:
        keys: dict[str, str] = {}
        if args.no_boot:
            if not args.api_key:
                parser.error("--no-boot 需要 --api-key")
            for scenario in scenarios:
                keys[scenario] = args.api_key
        else:
            if args.base_url or args.api_key:
                parser.error("--base-url / --api-key 只能和 --no-boot 一起用")
            service = Service(
                failure_max=args.failure_max,
                keep_data=args.keep_data,
                log_level=args.log_level,
                ssl_shim=not args.no_ssl_shim,
            )
            print(f"[bench] 数据目录 {service.data_dir}", file=sys.stderr)
            service.start()
            # 每个场景独立密钥：失败限流按 key 计数，混用会污染其它场景
            for scenario in scenarios:
                key_id, token = service.new_key(f"bench-{scenario}")
                keys[scenario] = token
                if scenario == SCENARIO_QUOTA:
                    service.set_request_quota(key_id, args.quota_limit)

        for scenario in scenarios:
            model = FAILING_MODEL if scenario == SCENARIO_RATELIMIT else PLAIN_MODEL
            for concurrency in tiers:
                print(f"[bench] {scenario} concurrency={concurrency} duration={args.duration}s ...",
                      file=sys.stderr)
                row = asyncio.run(run_one(
                    base_url=base_url,
                    api_key=keys[scenario],
                    provider=args.provider,
                    model=model,
                    concurrency=concurrency,
                    duration=args.duration,
                ))
                row["scenario"] = scenario
                rows.append(row)

        log_locked = service.locked_in_log() if service else 0
        _print_table(rows, log_locked)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    finally:
        if service is not None:
            service.stop()


if __name__ == "__main__":
    raise SystemExit(main())
