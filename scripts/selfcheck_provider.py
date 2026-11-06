"""单个服务商自助排查：配置 → readiness → 连通 → 流式 → 计费与日志。

用法：
    python scripts/selfcheck_provider.py --provider demo_local --fake
    python scripts/selfcheck_provider.py --provider zcode
    python scripts/selfcheck_provider.py --provider zcode --live

默认只读真实配置库，不发任何外呼；--fake 用临时库 + 进程内假上游，绝不写真实库；
--live 在真实库建一次性 key 经进程内网关发最小请求，跑完按 key 精确删回本次三表行。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import socket
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "apps" / "control"
sys.path.insert(0, str(CONTROL))

from app.adapters import build_adapters  # noqa: E402
from app.adapters.base import ProviderError  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.routers.logs import _classify_credit, _credit_of  # noqa: E402

PROVIDERS = (
    "demo_local",
    "codebuddy",
    "muse",
    "grok-register",
    "gptgrok",
    "zcode",
    "opencode",
    "sub2api",
    "trae",
    "trae_sidecar",
    "deepseek_sidecar",
    "qoder",
    "qoder_sidecar",
    "codar",
    "codar_sidecar",
)

SIDECARS = {
    "trae_sidecar": (
        7871,
        "git clone --depth 1 https://github.com/JeffHu0912/trae2api.git ../trae2api && "
        "cd ../trae2api && mkdir -p auths && go run ./cmd/server -config config.example.json",
    ),
    "deepseek_sidecar": (
        5001,
        "git clone --depth 1 https://github.com/ColudAI/Deepseek2Go.git ../Deepseek2Go && "
        "cd ../Deepseek2Go && go run ./cmd/server",
    ),
    "qoder_sidecar": (
        7873,
        "git clone --depth 1 https://github.com/NextAgentX/qoder-trae-workbuddy-switch.git ../qoder && "
        "cd ../qoder && docker compose up buddy-switch-gateway",
    ),
    "codar_sidecar": (
        8801,
        "git clone --depth 1 https://github.com/AiXiaoc/codar2api.git && cd codar2api && "
        "go build -o codar2api ./cmd/codar && ./codar2api",
    ),
}

CONFIG_HINTS = {
    "codebuddy": "去 CodeBuddy 管理端（7864）生成 wbt_ token，填入 provider_config.api_key",
    "muse": "去 https://api.meta.ai 创建 API key，填入 provider_config.api_key",
    "grok-register": "设置 GROK_REGISTER_EXECUTOR_URL 指向浏览器执行器后重启控制面",
    "gptgrok": "去设置页 provider_config 填 api_key，再导入 options.sentinel_token",
    "zcode": "去设置页 provider_config 填 api_key 与 options.official_system（官方 system 原文）",
    "opencode": "去设置页 provider_config 填 api_key（或 options.extra_keys）",
    "sub2api": "先建一个分组和至少一个启用账号（/api/sub2api/groups、/api/sub2api/accounts）",
    "trae": "把 Trae 客户端登录后的 Cloud-IDE JWT 填入 provider_config.api_key",
    "qoder": "填 provider_config.api_key，并补 options.uid 或 options.refresh_token",
    "codar": "把 codar2api 登录后拿到的 Codar JWT 填入 provider_config.api_key",
    "codar_sidecar": "在 codar2api 面板（8801）创建 API Key 后填入 provider_config.api_key",
}

LIVE_TIMEOUT = 20.0
STREAM_TIMEOUT = 30.0
# 流式响应在客户端读完后才在 finally 里落账，留出等待写入的时间
LOG_WAIT_SECONDS = 3.0
CLEANUP_MARK = "selfcheck-"


@dataclass
class Stage:
    name: str
    status: str
    reason: str


@dataclass
class GatewayRun:
    connect: Stage
    streaming: Stage
    billing: Stage
    log_ids: list[int] = field(default_factory=list)
    usage_ids: list[int] = field(default_factory=list)
    ledger_ids: list[int] = field(default_factory=list)
    snapshot_ids: list[int] = field(default_factory=list)


@dataclass
class State:
    provider: str = ""
    enabled: bool = False
    row_base: str = ""
    base_url: str = ""
    keys: list[str] = field(default_factory=list)
    creds: list[dict[str, Any]] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)
    cfg: dict[str, Any] = field(default_factory=dict)


def emit(stage: Stage) -> None:
    print(f"[{stage.name}] {stage.status} {stage.reason}")


def _db_path(settings: Any) -> Path:
    return Path(settings.db_path)


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _json_setting(conn: sqlite3.Connection, key: str, default: Any) -> Any:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return default


def _env_base(pid: str, settings: Any) -> str:
    fields = {
        "codebuddy": "codebuddy_base",
        "opencode": "opencode_upstream_base",
        "codar_sidecar": "codar_sidecar_base",
        "trae_sidecar": "trae_sidecar_base",
        "deepseek_sidecar": "deepseek_sidecar_base",
        "qoder_sidecar": "qoder_sidecar_base",
    }
    name = fields.get(pid)
    if not name or pid not in settings.env_base_pids:
        return ""
    return str(getattr(settings, name, "") or "")


def read_state(pid: str, settings: Any, adapters: dict[str, Any]) -> State:
    state = State(provider=pid)
    path = _db_path(settings)
    if not path.is_file():
        raise FileNotFoundError(f"配置库不存在：{path}（先启动一次控制面）")
    conn = _ro(path)
    try:
        row = conn.execute("SELECT * FROM providers WHERE id = ?", (pid,)).fetchone()
        if row is not None:
            state.enabled = bool(row["enabled"])
            state.row_base = str(row["base_url"] or "")
        raw_cfg = _json_setting(conn, "provider_config", {})
        state.cfg = raw_cfg.get(pid, {}) if isinstance(raw_cfg, dict) else {}
        if not isinstance(state.cfg, dict):
            state.cfg = {}
        state.options = state.cfg.get("options") if isinstance(state.cfg.get("options"), dict) else {}
        state.creds = [
            dict(item)
            for item in conn.execute(
                "SELECT * FROM provider_credentials WHERE provider = ? AND enabled = 1 "
                "ORDER BY sort_order, id",
                (pid,),
            ).fetchall()
        ]
    finally:
        conn.close()

    raw_keys: list[Any] = [item["secret"] for item in state.creds]
    raw_keys.append(state.cfg.get("api_key"))
    if pid == "codebuddy":
        raw_keys.append(settings.codebuddy_api_key)
    for item in raw_keys:
        key = str(item or "").strip()
        if key and key not in state.keys:
            state.keys.append(key)

    spec = adapters[pid].spec if pid in adapters else None
    cred_base = next((str(item["base_url"]) for item in state.creds if item["base_url"]), "")
    if cred_base:
        state.base_url = cred_base
    elif state.row_base:
        state.base_url = state.row_base
    else:
        state.base_url = _env_base(pid, settings) or (spec.default_base_url if spec else "")
    return state


def _key_problem(key: str) -> str:
    if not key:
        return "空值"
    if len(key) < 8:
        return "长度过短（少于 8 字符）"
    if any(ch.isspace() for ch in key):
        return "含空白字符（复制时带了换行或空格）"
    if key in {"***", "changeme", "your-api-key", "your_api_key"}:
        return "仍是占位符"
    return ""


def stage_config(pid: str, state: State, settings: Any, adapters: dict[str, Any]) -> Stage:
    if pid == "demo_local":
        return Stage("配置", "PASS", "演示 provider 使用进程内假上游，无需真实密钥")

    if pid in SIDECARS and pid != "codar_sidecar":
        return Stage("配置", "PASS", "sidecar 不校验上游 key；本地进程就绪性在连通阶段检查")

    if pid == "sub2api":
        path = _db_path(settings)
        conn = _ro(path)
        try:
            groups = conn.execute("SELECT COUNT(*) FROM sub2_groups WHERE enabled = 1").fetchone()[0]
            accounts = conn.execute(
                "SELECT COUNT(*) FROM sub2_accounts WHERE enabled = 1 AND group_id IN "
                "(SELECT id FROM sub2_groups WHERE enabled = 1)"
            ).fetchone()[0]
        finally:
            conn.close()
        if groups == 0 or accounts == 0:
            return Stage("配置", "FAIL", f"缺少分组或启用账号（groups={groups}, accounts={accounts}）")
        return Stage("配置", "PASS", f"分组与账号已就绪（groups={groups}, accounts={accounts}），无上游 key 要求")

    if pid == "grok-register":
        url = str(getattr(settings, "grok_register_executor_url", "") or "")
        if not url:
            return Stage("配置", "FAIL", "缺少 GROK_REGISTER_EXECUTOR_URL")
        if urlparse(url).scheme not in {"http", "https"}:
            return Stage("配置", "FAIL", f"执行器地址格式不合法：{url[:48]}")
        return Stage("配置", "PASS", "浏览器执行器地址已配置")

    key = state.keys[0] if state.keys else ""
    if not key:
        extras = state.options.get("extra_keys")
        if isinstance(extras, list):
            key = next((str(item).strip() for item in extras if str(item or "").strip()), "")
    problem = _key_problem(key)
    if problem:
        return Stage(
            "配置",
            "FAIL",
            f"缺少可用 api_key：{problem}（凭据 {len(state.creds)} 条，provider_config="
            f"{'有' if state.cfg.get('api_key') else '无'}）",
        )

    if pid == "codebuddy" and not key.lower().startswith("wbt_"):
        return Stage("配置", "FAIL", "CodeBuddy 管理端 token 必须以 wbt_ 开头")
    if pid == "codar_sidecar" and not key.startswith("sk-codar-"):
        return Stage("配置", "FAIL", "codar_sidecar 的 api_key 必须是面板创建的 sk-codar- 开头密钥")
    if pid == "muse" and "hatch_sess=" in key:
        return Stage("配置", "FAIL", "这是网页会话 Cookie，Muse 没有对应的 HTTP 对话端点")
    if pid == "gptgrok" and not str(state.options.get("sentinel_token") or "").strip():
        return Stage("配置", "FAIL", "缺 options.sentinel_token（gpt-* 请求会被上游 403）")
    if pid == "zcode" and not str(state.options.get("official_system") or "").strip():
        return Stage("配置", "FAIL", "缺 options.official_system（上游会回 3012）")
    if pid == "qoder" and not (
        str(state.options.get("uid") or "").strip()
        or str(state.options.get("refresh_token") or "").strip()
    ):
        return Stage("配置", "FAIL", "缺身份字段：options.uid 或 options.refresh_token 至少填一个")
    return Stage("配置", "PASS", f"api_key 已存且格式合法（凭据 {len(state.creds)} 条）")


def _last_probe(settings: Any, pid: str) -> dict[str, Any] | None:
    path = _db_path(settings)
    conn = _ro(path)
    try:
        row = conn.execute(
            "SELECT status, error FROM provider_health_checks WHERE provider = ? "
            "ORDER BY id DESC LIMIT 1",
            (pid,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None


def stage_readiness(pid: str, state: State, settings: Any, adapters: dict[str, Any]) -> Stage:
    adapter = adapters.get(pid)
    if adapter is None:
        if pid == "demo_local":
            return Stage("readiness", "SKIP", "demo_local 只存在于 --fake / 演示模式，真实库里没有注册")
        return Stage("readiness", "FAIL", "该 provider 未在当前控制面注册")
    first = state.keys[0] if state.keys else ""
    probe = _last_probe(settings, pid)
    status_now, missing = adapter.readiness_now(
        has_credential=bool(state.keys),
        credential_problem=adapter.credential_problem("api_key", first),
        last_probe=probe,
    )
    if pid in SIDECARS:
        alive, port, command = _sidecar_alive(state)
        if not alive:
            return Stage(
                "readiness", "SKIP",
                f"registry={status_now}：sidecar 未启动（端口 {port}）；启动后重跑：{command}",
            )
    if status_now == "ready":
        if not state.enabled and pid not in SIDECARS:
            return Stage("readiness", "FAIL", "registry=ready，但 providers.enabled=0（网关不会选它）")
        return Stage("readiness", "PASS", "registry=ready，无待补项")
    reason = "；".join(missing[:3]) if missing else "registry 判定 needs_config"
    return Stage("readiness", "FAIL", f"registry=needs_config：{reason}")


def _port_alive(host: str, port: int, timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _sidecar_alive(state: State) -> tuple[bool, int, str]:
    port, command = SIDECARS[state.provider]
    parsed = urlparse(state.base_url)
    host = parsed.hostname or "127.0.0.1"
    real_port = parsed.port or port
    return _port_alive(host, real_port), real_port, command


def _model_for(adapter: Any, state: State) -> str:
    configured = str(state.options.get("selfcheck_model") or "").strip()
    if configured:
        return configured
    for item in adapter.declared_models():
        model = str(item.get("id") or "").strip()
        if model:
            return model
    return ""


def _payload(model: str, stream: bool) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": stream,
    }


def _usage_of(body: Any) -> dict[str, int]:
    if not isinstance(body, dict):
        return {}
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return {}
    out: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            out[key] = int(value)
    return out


def _max_id(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0])


def _stream_done(lines: list[str], *, strict: bool) -> tuple[bool, str]:
    if strict:
        last = lines[-1].strip() if lines else "空"
        return (bool(lines) and last == "data: [DONE]"), last
    done = any(
        line.strip() in {"data: [DONE]", "event: message_stop"}
        or '"type": "message_stop"' in line
        for line in lines
    )
    return done, (lines[-1].strip()[:80] if lines else "空")


async def _wait_logs(db: Any, key_id: int, base_id: int, expect: int) -> None:
    """流式日志在客户端读完后才在 finally 落库；等到记录出现再查表。"""
    deadline = time.monotonic() + LOG_WAIT_SECONDS
    while True:
        conn = db.connect()
        try:
            count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM request_logs WHERE id > ? AND key_id = ?",
                    (base_id, key_id),
                ).fetchone()[0]
            )
        finally:
            conn.close()
        if count >= expect or time.monotonic() >= deadline:
            return
        await asyncio.sleep(0.1)


async def _gateway_flow(
    app: Any,
    db: Any,
    *,
    token: str,
    provider_id: str,
    model: str,
    key_id: int,
    max_tokens: int,
    strict: bool,
) -> GatewayRun:
    """经进程内网关发非流式 + 流式各一次，再按本次 key 查三张表。"""
    import httpx

    conn = db.connect()
    try:
        base_log = _max_id(conn, "request_logs")
        base_usage = _max_id(conn, "usage_records")
        base_ledger = _max_id(conn, "billing_ledger")
    finally:
        conn.close()

    headers = {"Authorization": f"Bearer {token}", "x-provider": provider_id}
    connect = Stage("连通", "FAIL", "未发出请求")
    streaming = Stage("流式", "SKIP", "前置连通失败")
    sent = 0
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
        trust_env=False,
        timeout=max(LIVE_TIMEOUT, STREAM_TIMEOUT),
    ) as client:
        try:
            resp = await client.post(
                "/v1/chat/completions",
                json={**_payload(model, False), "max_tokens": max_tokens},
                headers=headers,
            )
            sent += 1
            if resp.status_code != 200:
                connect = Stage(
                    "连通", "FAIL", f"网关返回 HTTP {resp.status_code}：{resp.text[:160]}"
                )
            else:
                try:
                    body = resp.json()
                except ValueError:
                    body = None
                usage = _usage_of(body)
                if strict and not usage:
                    connect = Stage("连通", "FAIL", "网关响应没有 usage，计费链路会缺数据")
                else:
                    detail = f"usage={usage}" if usage else "响应无 usage"
                    connect = Stage("连通", "PASS", f"网关 200，{detail}")
        except Exception as exc:  # noqa: BLE001 - 装配/传输异常原样报出
            sent += 1
            connect = Stage("连通", "FAIL", f"{type(exc).__name__}: {str(exc)[:160]}")

        if strict and connect.status == "FAIL":
            return GatewayRun(
                connect,
                Stage("流式", "SKIP", "前置连通失败"),
                Stage("计费与日志", "SKIP", "前置连通失败"),
            )

        sent += 1
        try:
            stream_lines: list[str] = []
            async with client.stream(
                "POST",
                "/v1/chat/completions",
                json={**_payload(model, True), "max_tokens": max_tokens},
                headers=headers,
            ) as sresp:
                if sresp.status_code != 200:
                    streaming = Stage("流式", "FAIL", f"网关返回 HTTP {sresp.status_code}")
                else:
                    async for line in sresp.aiter_lines():
                        if line.strip():
                            stream_lines.append(line)
                    done, tail = _stream_done(stream_lines, strict=strict)
                    streaming = (
                        Stage("流式", "PASS", f"SSE 正常收尾（{len(stream_lines)} 行）")
                        if done
                        else Stage("流式", "FAIL", f"SSE 未以 [DONE] 收尾，末行={tail[:80]}")
                    )
        except Exception as exc:  # noqa: BLE001
            streaming = Stage("流式", "FAIL", f"{type(exc).__name__}: {str(exc)[:160]}")

    await _wait_logs(db, key_id, base_log, sent)

    conn = db.connect()
    try:
        logs = conn.execute(
            "SELECT id, status, error, prompt_tokens, completion_tokens FROM request_logs "
            "WHERE id > ? AND key_id = ? ORDER BY id",
            (base_log, key_id),
        ).fetchall()
        usage_rows = conn.execute(
            "SELECT id FROM usage_records WHERE id > ? AND key_id = ? ORDER BY id",
            (base_usage, key_id),
        ).fetchall()
        ledger_rows = conn.execute(
            "SELECT id, log_id, billed_micro, note, price_snapshot_id FROM billing_ledger "
            "WHERE id > ? AND key_id = ? ORDER BY id",
            (base_ledger, key_id),
        ).fetchall()
    finally:
        conn.close()

    credits = {
        int(row["log_id"]): _classify_credit(int(row["billed_micro"]), str(row["note"]))
        for row in ledger_rows
        if row["log_id"] is not None
    }
    statuses = [_credit_of(row, credits)[1] for row in logs]
    amounts = [_credit_of(row, credits)[0] for row in logs]
    total_micro = sum(int(amount) for amount in amounts if amount is not None)
    facts = (
        f"request_logs={len(logs)}，usage_records={len(usage_rows)}，"
        f"billing_ledger={len(ledger_rows)}，credit_status={statuses}，"
        f"金额 {total_micro} micro-USD"
    )
    if len(logs) < sent:
        billing = Stage("计费与日志", "FAIL", f"{facts}；应有 {sent} 条日志")
    elif strict:
        broken = [
            f"log#{row['id']}={status}"
            for row, status in zip(logs, statuses)
            if status != "billed"
        ]
        broken += [
            f"log#{row['id']}=usage 为 0"
            for row in logs
            if int(row["prompt_tokens"]) + int(row["completion_tokens"]) <= 0
        ]
        billing = (
            Stage("计费与日志", "FAIL", f"{facts}；异常：{'，'.join(broken)}")
            if broken
            else Stage("计费与日志", "PASS", f"{facts}，{len(logs)} 条日志可检索")
        )
    else:
        problems = []
        if len(usage_rows) < len(logs):
            problems.append(f"usage_records 少于日志（{len(usage_rows)}/{len(logs)}）")
        if not ledger_rows:
            problems.append("没有任何 billing_ledger 流水")
        if "unlinked" in statuses:
            problems.append("有成功日志未关联流水")
        billing = (
            Stage("计费与日志", "FAIL", f"{facts}；{'，'.join(problems)}")
            if problems
            else Stage("计费与日志", "PASS", facts)
        )
    return GatewayRun(
        connect,
        streaming,
        billing,
        log_ids=[int(row["id"]) for row in logs],
        usage_ids=[int(row["id"]) for row in usage_rows],
        ledger_ids=[int(row["id"]) for row in ledger_rows],
        snapshot_ids=sorted(
            {int(row["price_snapshot_id"]) for row in ledger_rows if row["price_snapshot_id"]}
        ),
    )


def _demo_fake() -> tuple[Stage, Stage, Stage]:
    import logging

    logging.disable(logging.CRITICAL)
    temp = Path(tempfile.mkdtemp(prefix="u2a-selfcheck-"))
    os.environ.update(
        {
            "UNIFIED_DATA_DIR": str(temp / "data"),
            "UNIFIED_DB_PATH": str(temp / "data" / "selfcheck.db"),
            "UNIFIED_WEB_DIR": str(temp / "web"),
            "UNIFIED_DEMO_MODE": "1",
            "UNIFIED_HOST": "127.0.0.1",
            "UNIFIED_ALLOW_PRIVATE_UPSTREAMS": "1",
            "UNIFIED_ADMIN_PASSWORD": "selfcheck-admin-pass",
            "UNIFIED_LOG_LEVEL": "warning",
        }
    )
    try:
        from app.main import app

        async def run() -> tuple[Stage, Stage, Stage]:
            async with app.router.lifespan_context(app):
                db = app.state.db
                conn = db.connect()
                try:
                    seed = _json_setting(conn, "demo_seed", {})
                finally:
                    conn.close()
                key = next(
                    (item for item in (seed.get("keys") or []) if item.get("enabled")),
                    None,
                )
                if not key or not key.get("token") or key.get("id") is None:
                    return (
                        Stage("连通", "FAIL", "演示库没有拿到启用密钥，demo_seed 播种失败"),
                        Stage("流式", "SKIP", "前置连通失败"),
                        Stage("计费与日志", "SKIP", "前置连通失败"),
                    )
                result = await _gateway_flow(
                    app,
                    db,
                    token=str(key["token"]),
                    provider_id="demo_local",
                    model="fake-chat-1",
                    key_id=int(key["id"]),
                    max_tokens=16,
                    strict=True,
                )
                return result.connect, result.streaming, result.billing

        return asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - 装配失败要原样告诉用户
        return (
            Stage("连通", "FAIL", f"演示链路装配失败：{type(exc).__name__}: {str(exc)[:180]}"),
            Stage("流式", "SKIP", "前置连通失败"),
            Stage("计费与日志", "SKIP", "前置连通失败"),
        )
    finally:
        try:
            import app.main as main_module

            runtime = getattr(main_module.app.state, "demo", None)
            if runtime is not None:
                runtime.stop()
        except Exception:  # noqa: BLE001 - 清理失败不影响结论
            pass
        shutil.rmtree(temp, ignore_errors=True)


def _gateway_app(settings: Any) -> Any:
    """只装网关所需状态，不跑 lifespan：不监听端口、不启健康检查/演示上游。"""
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException

    from app.db import Database
    from app.gateway_errors import (
        PROTOCOL_ANTHROPIC,
        GatewayError,
        framework_payload,
        protocol_for_path,
    )
    from app.registry import ProviderRegistry
    from app.routers import v1

    app = FastAPI()
    app.state.settings = settings
    app.state.db = Database(settings.db_path)
    app.state.registry = ProviderRegistry(app.state.db, settings)
    app.include_router(v1.router)

    @app.exception_handler(ProviderError)
    async def provider_error_handler(request: Request, exc: ProviderError):
        return JSONResponse({"detail": exc.message}, status_code=exc.status_code)

    @app.exception_handler(GatewayError)
    async def gateway_error_handler(request: Request, exc: GatewayError):
        payload = (
            exc.anthropic_payload()
            if protocol_for_path(request.url.path) == PROTOCOL_ANTHROPIC
            else exc.openai_payload()
        )
        return JSONResponse(payload, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def framework_error_handler(request: Request, exc: StarletteHTTPException):
        path = request.url.path
        if path == "/v1" or path.startswith("/v1/"):
            return JSONResponse(
                framework_payload(path, exc.status_code, exc.detail),
                status_code=exc.status_code,
                headers=exc.headers,
            )
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)

    return app


def _create_temp_key(db: Any, owner: str) -> tuple[int, str]:
    """真实库建一次性 key；api_keys 没有有效期字段，靠 finally 立即删除收口。"""
    from app.routers.keys import create_key_record, delete_key_cascade

    conn = db.connect()
    key_id: int | None = None
    try:
        key_id, token = create_key_record(
            conn, f"{CLEANUP_MARK}{secrets.token_hex(4)}", owner
        )
        # 最小配额：两次请求（非流式 + 流式）刚好够，跑完随 key 一起 cascade 删除
        ts = int(time.time())
        conn.execute(
            "INSERT INTO quota_policies("
            "key_id, provider, model, window, limit_micro, limit_requests, enabled, created_at, updated_at"
            ") VALUES(?,?,?,?,?,?,1,?,?)",
            (key_id, "", "", "day", 0, 2, ts, ts),
        )
        conn.commit()
        return key_id, token
    except Exception:
        conn.rollback()
        if key_id is not None:
            try:
                delete_key_cascade(conn, key_id)
                conn.commit()
            except Exception:  # noqa: BLE001 - 创建失败后尽力回收
                conn.rollback()
        raise
    finally:
        conn.close()


def _key_leftovers(db: Any, key_id: int, snapshot_ids: list[int]) -> dict[str, int]:
    conn = db.connect()
    try:
        counts = {
            "api_keys": int(
                conn.execute("SELECT COUNT(*) FROM api_keys WHERE id = ?", (key_id,)).fetchone()[0]
            ),
            "request_logs": int(
                conn.execute(
                    "SELECT COUNT(*) FROM request_logs WHERE key_id = ?", (key_id,)
                ).fetchone()[0]
            ),
            "usage_records": int(
                conn.execute(
                    "SELECT COUNT(*) FROM usage_records WHERE key_id = ?", (key_id,)
                ).fetchone()[0]
            ),
            "billing_ledger": int(
                conn.execute(
                    "SELECT COUNT(*) FROM billing_ledger WHERE key_id = ?", (key_id,)
                ).fetchone()[0]
            ),
        }
        if snapshot_ids:
            marks = ",".join("?" for _ in snapshot_ids)
            counts["price_book_snapshots"] = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM price_book_snapshots WHERE id IN ({marks})",
                    tuple(snapshot_ids),
                ).fetchone()[0]
            )
        else:
            counts["price_book_snapshots"] = 0
    finally:
        conn.close()
    return counts


def _delete_key_rows(
    db: Any,
    key_id: int,
    snapshot_ids: set[int],
    log_ids: list[int],
    usage_ids: list[int],
    ledger_ids: list[int],
) -> None:
    from app.routers.keys import delete_key_cascade

    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT price_snapshot_id FROM billing_ledger WHERE key_id = ?", (key_id,)
        ).fetchall()
        snapshot_ids.update(
            int(row["price_snapshot_id"]) for row in rows if row["price_snapshot_id"] is not None
        )
        # 先按捕获 id 精确删本次行，再按一次性 key_id 扫尾（key 刚建，不会命中历史）
        for table, ids in (
            ("billing_ledger", ledger_ids),
            ("usage_records", usage_ids),
            ("request_logs", log_ids),
        ):
            if ids:
                marks = ",".join("?" for _ in ids)
                conn.execute(
                    f"DELETE FROM {table} WHERE key_id = ? AND id IN ({marks})",
                    (key_id, *ids),
                )
            conn.execute(f"DELETE FROM {table} WHERE key_id = ?", (key_id,))
        delete_key_cascade(conn, key_id)
        if snapshot_ids:
            marks = ",".join("?" for _ in snapshot_ids)
            conn.execute(
                f"DELETE FROM price_book_snapshots WHERE id IN ({marks})",
                tuple(sorted(snapshot_ids)),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _cleanup_key_rows(
    db: Any,
    key_id: int,
    snapshot_ids: list[int],
    log_ids: list[int],
    usage_ids: list[int],
    ledger_ids: list[int],
) -> tuple[dict[str, int], list[int]]:
    """两遍删除并复核，给流式 finally 的迟到写入留 200ms。"""
    snapshots = set(int(item) for item in snapshot_ids)
    for attempt in range(2):
        _delete_key_rows(db, key_id, snapshots, log_ids, usage_ids, ledger_ids)
        if not any(_key_leftovers(db, key_id, sorted(snapshots)).values()):
            break
        if attempt == 0:
            time.sleep(0.2)
    return _key_leftovers(db, key_id, sorted(snapshots)), sorted(snapshots)


def run_provider(pid: str, mode: str, settings: Any, adapters: dict[str, Any]) -> tuple[list[Stage], str]:
    try:
        state = read_state(pid, settings, adapters)
    except Exception as exc:  # noqa: BLE001 - 缺库/坏库也按阶段报
        failed = Stage("配置", "FAIL", f"读取配置失败：{exc}")
        skips = [Stage("readiness", "SKIP", "前置配置读取失败")]
        if not mode:
            skips.append(Stage("连通", "SKIP", "未加 --live/--fake，不做外呼"))
        else:
            skips.append(Stage("连通", "SKIP", "前置配置读取失败"))
        skips += [Stage("流式", "SKIP", "前置阶段失败"), Stage("计费与日志", "SKIP", "前置阶段失败")]
        return [failed, *skips], CONFIG_HINTS.get(pid, "先修好配置库再重试")

    config = stage_config(pid, state, settings, adapters)
    readiness = stage_readiness(pid, state, settings, adapters)
    if mode == "fake" and pid == "demo_local":
        readiness = Stage("readiness", "PASS", "演示模式 registry=ready（临时库装配）")

    if mode == "fake":
        if pid == "demo_local":
            connect, streaming, billing = _demo_fake()
        else:
            connect = Stage("连通", "SKIP", "fake 模式只装配 demo_local 假上游；该家请改用 --live")
            streaming = Stage("流式", "SKIP", "fake 模式未装配该家")
            billing = Stage("计费与日志", "SKIP", "fake 模式未装配该家")
    elif mode == "live":
        if pid == "demo_local":
            connect = Stage("连通", "SKIP", "demo_local 没有真实上游；请用 --fake")
            streaming = Stage("流式", "SKIP", "demo_local 请用 --fake")
            billing = Stage("计费与日志", "SKIP", "demo_local 请用 --fake")
        elif pid == "grok-register":
            connect = Stage("连通", "SKIP", "grok-register 只负责注册任务，没有 chat 能力")
            streaming = Stage("流式", "SKIP", "该 provider 无 chat 能力")
            billing = Stage("计费与日志", "SKIP", "该 provider 无 chat 能力")
        elif config.status != "PASS":
            connect = Stage("连通", "SKIP", "配置未通过，没有可用凭据，未外呼")
            streaming = Stage("流式", "SKIP", "前置配置失败")
            billing = Stage("计费与日志", "SKIP", "前置配置失败")
        elif not state.enabled:
            connect = Stage("连通", "SKIP", "providers.enabled=0，网关不会选中该家，未外呼")
            streaming = Stage("流式", "SKIP", "provider 未启用")
            billing = Stage("计费与日志", "SKIP", "provider 未启用")
        elif pid in SIDECARS:
            alive, port, command = _sidecar_alive(state)
            if not alive:
                connect = Stage(
                    "连通", "SKIP", f"sidecar 进程未起（端口 {port}）：启动命令 {command}"
                )
                streaming = Stage("流式", "SKIP", "sidecar 未运行")
                billing = Stage("计费与日志", "SKIP", "sidecar 未运行")
            else:
                connect, streaming, billing = _live_flow(pid, state, adapters, settings)
        else:
            connect, streaming, billing = _live_flow(pid, state, adapters, settings)
    else:
        reason = "未加 --live/--fake，不做外呼"
        if pid == "demo_local":
            reason += "；demo_local 请加 --fake"
        elif pid in SIDECARS:
            alive, port, command = _sidecar_alive(state)
            reason = (
                f"sidecar 已监听 {port}；未加 --live，不做外呼"
                if alive
                else f"sidecar 进程未起（端口 {port}）：启动命令 {command}"
            )
        connect = Stage("连通", "SKIP", reason)
        streaming = Stage("流式", "SKIP", "未外呼，无法验证 SSE")
        billing = Stage("计费与日志", "SKIP", "未外呼，没有本次请求可查")

    stages = [config, readiness, connect, streaming, billing]
    return stages, _next_action(stages, pid)


def _live_flow(
    pid: str, state: State, adapters: dict[str, Any], settings: Any
) -> tuple[Stage, Stage, Stage]:
    import logging

    logging.disable(logging.CRITICAL)
    adapter = adapters[pid]
    adapter.configure(
        base_url=state.base_url,
        api_key=state.keys[0] if state.keys else "",
        options=state.options,
        api_keys=state.keys,
    )
    model = _model_for(adapter, state)
    if not model:
        return (
            Stage("连通", "SKIP", "未找到可用模型：请在 provider_config.options.selfcheck_model 指定"),
            Stage("流式", "SKIP", "没有模型可发"),
            Stage("计费与日志", "SKIP", "没有外呼"),
        )

    app = None
    key_id: int | None = None
    outcome: tuple[Stage, Stage, Stage] | None = None
    snapshot_ids: list[int] = []
    log_ids: list[int] = []
    usage_ids: list[int] = []
    ledger_ids: list[int] = []
    cleanup_counts: dict[str, int] | None = None
    try:
        app = _gateway_app(settings)
        key_id, token = _create_temp_key(app.state.db, settings.admin_username)
        run = asyncio.run(
            _gateway_flow(
                app,
                app.state.db,
                token=token,
                provider_id=pid,
                model=model,
                key_id=key_id,
                max_tokens=1,
                strict=False,
            )
        )
        outcome = (
            Stage("连通", run.connect.status, f"model={model} {run.connect.reason}"),
            Stage("流式", run.streaming.status, f"model={model} {run.streaming.reason}"),
            Stage("计费与日志", run.billing.status, run.billing.reason),
        )
        snapshot_ids = run.snapshot_ids
        log_ids = run.log_ids
        usage_ids = run.usage_ids
        ledger_ids = run.ledger_ids
    except Exception as exc:  # noqa: BLE001 - 装配失败原样报出
        outcome = (
            Stage(
                "连通", "FAIL",
                f"live 网关装配失败：{type(exc).__name__}: {str(exc)[:180]}",
            ),
            Stage("流式", "SKIP", "前置连通失败"),
            Stage("计费与日志", "SKIP", "前置连通失败"),
        )
    finally:
        if app is not None and key_id is not None:
            try:
                cleanup_counts, snapshot_ids = _cleanup_key_rows(
                    app.state.db, key_id, snapshot_ids, log_ids, usage_ids, ledger_ids
                )
            except Exception as exc:  # noqa: BLE001 - 清理失败必须显式暴露
                cleanup_counts = {"cleanup_error": 1}
                print(
                    f"[清理] FAIL 临时 key/本次行删除失败：{type(exc).__name__}: {str(exc)[:160]}",
                    file=sys.stderr,
                )

    assert outcome is not None
    leftover = {k: v for k, v in (cleanup_counts or {}).items() if v}
    if cleanup_counts is None:
        cleanup = "清理 SKIP（未创建临时 key）"
    elif leftover:
        cleanup = f"清理 FAIL（残留 {leftover}）"
    else:
        cleanup = "清理 PASS（临时 key 与本次三表行已归零）"
    billing = outcome[2]
    if leftover:
        billing = Stage("计费与日志", "FAIL", f"{billing.reason}；{cleanup}")
    else:
        billing = Stage("计费与日志", billing.status, f"{billing.reason}；{cleanup}")
    return outcome[0], outcome[1], billing


def _next_action(stages: list[Stage], pid: str) -> str:
    failed = next((s for s in stages if s.status == "FAIL"), None)
    skipped = next((s for s in stages if s.status == "SKIP"), None)
    if failed is None and skipped is None:
        if pid == "demo_local":
            return "演示链路已跑通；换真实服务商请执行 --provider <id> --live"
        return "该服务商在当前模式下全部通过；可持续观察日志与流水"
    if failed is not None:
        if failed.name == "配置":
            return CONFIG_HINTS.get(pid, f"修配置：{failed.reason}")
        if failed.name == "readiness":
            if pid == "demo_local":
                return "demo_local 只在演示链路里存在：执行 --provider demo_local --fake"
            if pid == "sub2api":
                return "sub2api 的 registry 固定 needs_config；分组账号已建则可加 --live 验证网关链路"
            if pid in SIDECARS:
                _, command = SIDECARS[pid]
                return f"先启动 sidecar：{command}"
            return f"按 requires 补齐配置：{failed.reason}"
        if failed.name in {"连通", "流式"}:
            return f"检查上游地址/凭据/网络后重试：{failed.reason}"
        return f"检查本次请求的日志与流水口径：{failed.reason}"
    assert skipped is not None
    if skipped.name == "配置":
        return CONFIG_HINTS.get(pid, f"修配置：{skipped.reason}")
    if skipped.name == "readiness":
        if pid == "demo_local":
            return "请执行 --provider demo_local --fake"
        if pid in SIDECARS:
            return f"先启动 sidecar：{SIDECARS[pid][1]}"
        return f"补齐 readiness 缺项后重试：{skipped.reason}"
    if skipped.name == "连通":
        if pid in SIDECARS and "未起" in skipped.reason:
            return f"按提示启动 sidecar 后加 --live 重试：{skipped.reason}"
        return f"确认配置后加 --live 外呼验证：{skipped.reason}"
    return f"完成前置阶段后重试：{skipped.reason}"


def list_providers(adapters: dict[str, Any]) -> None:
    print("可选 provider：")
    for pid in PROVIDERS:
        adapter = adapters.get(pid)
        name = adapter.spec.name if adapter else "演示假上游"
        print(f"  {pid:<16} {name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="单个服务商自助排查")
    parser.add_argument("--provider", help="provider id")
    parser.add_argument("--live", action="store_true", help="对真实上游打一次最小请求")
    parser.add_argument("--fake", action="store_true", help="用临时库 + 假上游跑网关链路")
    parser.add_argument("--list", action="store_true", help="列出可选 provider")
    args = parser.parse_args(argv)

    os.environ.setdefault("UNIFIED_ADMIN_PASSWORD", f"selfcheck-{time.time():.0f}")
    os.environ.setdefault("CI", "")
    settings = load_settings()
    adapters = {adapter.spec.id: adapter for adapter in build_adapters(settings)}

    if args.list:
        list_providers(adapters)
        return 0
    if not args.provider:
        print("缺少 --provider。可选 id：")
        list_providers(adapters)
        return 1
    pid = args.provider.strip()
    if pid not in PROVIDERS:
        print(f"未知 provider：{pid}")
        print("可选 id：")
        for item in PROVIDERS:
            print(f"  {item}")
        return 1
    if args.live and args.fake:
        print("--live 与 --fake 互斥：fake 不碰真实上游，live 不造假上游")
        return 1

    mode = "fake" if args.fake else ("live" if args.live else "")
    adapter = adapters.get(pid)
    name = adapter.spec.name if adapter else "演示假上游"
    print(f"provider={pid}（{name}） mode={mode or '默认（不外呼）'}")
    if mode == "fake":
        print("配置库=临时库（--fake 不读写真实配置库）")
    else:
        print(f"配置库={_db_path(settings)}")

    stages, next_action = run_provider(pid, mode, settings, adapters)
    for stage in stages:
        emit(stage)
    print(f"下一件该做的事：{next_action}")

    if any(stage.status == "FAIL" for stage in stages):
        return 1
    if any(stage.status == "SKIP" for stage in stages):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
