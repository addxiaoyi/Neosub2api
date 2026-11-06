"""演示模式（UNIFIED_DEMO_MODE）。

没有真实上游时，模型列表、账号、日志、统计、配额全是空的，第一次跑起来的人看不出
这个项目能做什么。演示模式用仓内 `scripts/fake_upstream.py` 顶替上游（HTTP 行为原样
复用，不另写协议），provider 走和真实上游完全相同的适配器 + 网关链路。

开关默认关闭；关闭时本模块不参与任何启动路径，既不注册 provider，也不写库。

演示数据全部带标记，可一键清除：
- provider id `demo_local`、`provider` 列等于 `demo_local` 的日志/流水/凭据/价格/账号；
- 名称以 `[demo] ` 开头的网关密钥；
- 归属这些演示密钥的配额策略；
- `demo_` 前缀的 sub2api 分组，及其账号池成员与扣减流水（/quota 页三块数据）；
- settings 表里的 `demo_seed`（记录 id 清单与演示密钥明文，供脚本打印和清理兜底）；
- `model_map` 里的演示映射键与 `audit_logs` 里 `demo_probe_user` 的失败登录行。
"""

from __future__ import annotations

import hashlib
import importlib.util
import ipaddress
import json
import logging
import os
import secrets
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from .adapters.base import ProviderSpec, stub_capabilities
from .adapters.openai_compat import OpenAICompatAdapter
from .api_utils import now
from .billing import extract_usage, period_bounds, record_usage, resolve_prices, settle, totals
from .services import secret_store
from .config import Settings, load_settings
from .db import Database, get_json_setting, set_json_setting
from .key_prefix import KEY_PREFIX, STORED_PREFIX_LEN
from .security import SESSION_COOKIE

logger = logging.getLogger("unified.demo")

DEMO_PROVIDER_ID = "demo_local"
DEMO_MARK = "[demo] "
DEMO_SEED_SETTING = "demo_seed"
DEMO_DISABLED_SETTING = "demo_disabled_providers"
DEMO_SEED_VERSION = 1
DEMO_CREDENTIALS_FILE = "demo-credentials.txt"
DEMO_ACCOUNT_FILES = ("demo_account_a.json", "demo_account_b.json")
DEMO_PRICE_INPUT_MICRO = 1_000_000  # $1 / 1M token
DEMO_PRICE_OUTPUT_MICRO = 2_000_000  # $2 / 1M token
FAKE_UPSTREAM_PATH = Path(__file__).resolve().parents[1] / "scripts" / "fake_upstream.py"

# ── /quota 页的三块数据：分组、账号池成员、按日期分布的扣减流水 ──────────────
DEMO_SUB2_GROUPS = (
    # (分组 id, 名称, 平台, 分组配额 token；0 = 共享池不限额)
    ("demo_team", "研发团队（日限额）", "openai", 40_000),
    ("demo_shared", "共享池（不限额）", "anthropic", 0),
)
DEMO_SUB2_ACCOUNTS = (
    # (分组 id, 名称, 权重, 冷却秒, 是否启用, 是否额度用尽) —— 冷却/停用/用尽各留一个用于展示徽标
    ("demo_team", "团队账号 A", 2, 0, 1, 0),
    ("demo_team", "团队账号 B（冷却中）", 1, 900, 1, 0),
    ("demo_team", "团队账号 C（额度用尽）", 1, 0, 1, 1),
    ("demo_shared", "共享账号 A", 1, 0, 1, 0),
    ("demo_shared", "共享账号 B（已停用）", 1, 0, 0, 0),
)
# (距今日零点的天数, 时, 分, 分组 id, 组内账号序号(None = 空池拒绝), 模型,
#  输入 token, 输出 token, HTTP 状态, 备注, 原因)
DEMO_SUB2_LEDGER = (
    (0, 9, 5, "demo_team", 0, "fake-chat-1", 1200, 460, 200, "", None),
    (0, 9, 40, "demo_team", 1, "fake-chat-2", 860, 300, 200, "", None),
    (0, 10, 15, "demo_shared", 0, "fake-chat-1", 1500, 620, 200, "", None),
    (0, 10, 50, "demo_team", 2, "fake-chat-1", 940, 350, 200, "", None),
    (0, 11, 20, "demo_shared", 0, "fake-chat-2", 700, 260, 200, "", None),
    (0, 11, 55, "demo_team", 0, "fake-chat-2", 1320, 540, 200, "", None),
    (0, 12, 30, "demo_shared", None, "fake-chat-1", 0, 0, 429, "空池拒绝", "no_available_account"),
    (1, 9, 10, "demo_team", 0, "fake-chat-1", 1400, 520, 200, "", None),
    (1, 14, 5, "demo_shared", 1, "fake-chat-2", 780, 310, 200, "", None),
    (1, 16, 40, "demo_team", 1, "fake-chat-1", 1100, 380, 200, "", None),
    (1, 20, 12, "demo_team", 2, "fake-chat-2", 640, 250, 200, "", None),
    (2, 8, 55, "demo_shared", 0, "fake-chat-1", 1600, 700, 200, "", None),
    (2, 13, 30, "demo_team", 0, "fake-chat-2", 1250, 480, 200, "", None),
    (2, 18, 45, "demo_team", 2, "fake-chat-1", 0, 0, 500, "", None),
    (2, 21, 3, "demo_shared", 0, "fake-chat-2", 890, 330, 200, "", None),
    (3, 10, 20, "demo_team", 1, "fake-chat-1", 1450, 560, 200, "", None),
    (3, 15, 45, "demo_shared", 0, "fake-chat-1", 1050, 430, 200, "", None),
    (3, 19, 30, "demo_team", 0, "fake-chat-1", 1180, 470, 200, "", None),
    (3, 22, 10, "demo_shared", 1, "fake-chat-1", 720, 290, 200, "", None),
    (4, 9, 40, "demo_team", 2, "fake-chat-2", 960, 380, 200, "", None),
    (4, 13, 15, "demo_shared", 0, "fake-chat-2", 1340, 550, 200, "", None),
    (4, 17, 55, "demo_team", 0, "fake-chat-1", 1420, 610, 200, "", None),
    (5, 11, 5, "demo_shared", 0, "fake-chat-1", 1150, 470, 200, "", None),
    (5, 16, 35, "demo_team", 1, "fake-chat-2", 830, 320, 200, "", None),
    (5, 21, 48, "demo_team", 2, "fake-chat-1", 560, 240, 200, "", None),
    (6, 8, 25, "demo_shared", 0, "fake-chat-1", 1280, 540, 200, "", None),
    (6, 12, 50, "demo_team", 0, "fake-chat-2", 1090, 430, 200, "", None),
    (6, 19, 15, "demo_team", None, "fake-chat-1", 0, 0, 429, "空池拒绝", "no_available_account"),
)
# 告警样例策略：挂在已停用的演示密钥 B 上，3 笔跑满即同时踩到 80%/100% 阈值，
# 且不影响密钥 A 的正常演示请求（额度用尽只落在 B 上）。
DEMO_ALERT_LIMIT_REQUESTS = 3
DEMO_ALERT_CALLS = (
    ("fake-chat-1", "演示请求：告警样例第 1 笔（演示密钥 B）。"),
    ("fake-chat-2", "演示请求：告警样例第 2 笔（演示密钥 B）。"),
    ("fake-chat-1", "演示请求：告警样例第 3 笔，触发 80%/100% 告警阈值。"),
)

# 安全页失败登录样例：专用探测账号错登一次，绝不拿 admin 或真实演示账号试（5 次/15 分钟会锁号）
DEMO_PROBE_USER = "demo_probe_user"
DEMO_PROBE_PASSWORD = "wrong-demo-password"
# 设置页模型映射样例：demo/ 前缀不是已知 adapter id，安全落进 alias 分支
DEMO_MODEL_MAP = {
    "demo/fake-chat-1": "fake-chat-1",
    "demo/fake-chat-2": "fake-chat-2",
    "demo-chat": "fake-chat-1",
}
# 跨天老库 day 窗用量归零时，真请求顶到触顶前最多先成功这么多笔；新库首笔即 429
DEMO_GATEWAY_MAX_ATTEMPTS = DEMO_ALERT_LIMIT_REQUESTS + 1


class DemoModeError(RuntimeError):
    """演示模式启动条件不满足（非回环地址、假上游起不来等），必须中止启动。"""


def _control_app() -> Any:
    """延迟取控制面 app：lifespan 播种时 main 已加载完，延迟导入避开循环引用。"""
    try:
        from .main import app as control_app
    except Exception as exc:  # noqa: BLE001 - 拿不到 app 就必须中止播种
        raise DemoModeError(f"演示播种拿不到控制面 app：{exc}") from exc
    return control_app


def _strip_brackets(host: str) -> str:
    raw = (host or "").strip()
    if raw.startswith("[") and raw.endswith("]"):
        return raw[1:-1].strip()
    return raw


def is_loopback_host(host: str) -> bool:
    """localhost / 127.0.0.0/8 / ::1 才算回环；空串、0.0.0.0、局域网 IP 一律不算。"""
    raw = _strip_brackets(host).lower()
    if raw == "localhost":
        return True
    try:
        return ipaddress.ip_address(raw).is_loopback
    except ValueError:
        return False


def ensure_loopback_bind(host: str) -> None:
    if is_loopback_host(host):
        return
    raise DemoModeError(
        f"UNIFIED_DEMO_MODE=1 只允许绑定回环地址，当前 UNIFIED_HOST={host!r}。"
        "演示模式会自动写入演示数据并启动假上游，对公网开放等于把假数据当生产用；"
        "请设置 UNIFIED_HOST=127.0.0.1 后重试。"
    )


def _load_fake_app() -> Any:
    """按文件路径加载假上游模块，复用它的 FastAPI app（协议行为只有一份）。"""
    if not FAKE_UPSTREAM_PATH.is_file():
        raise DemoModeError(f"找不到假上游脚本：{FAKE_UPSTREAM_PATH}")
    module_name = "_unified_demo_fake_upstream"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached.app
    spec = importlib.util.spec_from_file_location(module_name, FAKE_UPSTREAM_PATH)
    if spec is None or spec.loader is None:
        raise DemoModeError(f"无法加载假上游脚本：{FAKE_UPSTREAM_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module.app


class DemoUpstream:
    """进程内假上游：独立的 uvicorn 线程，只用 127.0.0.1 上随机端口。"""

    def __init__(self) -> None:
        self.port = 0
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, timeout: float = 10.0) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", 0))
            sock.listen(128)
            self.port = int(sock.getsockname()[1])
            config = uvicorn.Config(
                _load_fake_app(),
                host="127.0.0.1",
                port=self.port,
                log_level="warning",
                access_log=False,
            )
            self._sock = sock
            self._server = uvicorn.Server(config)
            self._thread = threading.Thread(
                target=self._server.run,
                kwargs={"sockets": [sock]},
                name="unified-demo-upstream",
                daemon=True,
            )
            self._thread.start()
        except OSError as exc:
            self.stop()
            raise DemoModeError(f"演示假上游启动失败：{exc}") from exc
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.alive():
                return
            time.sleep(0.05)
        self.stop()
        raise DemoModeError(f"演示假上游 {timeout:g}s 内没有就绪，启动已中止")

    def alive(self) -> bool:
        if self._thread is None or not self._thread.is_alive() or not self.port:
            return False
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                return True
        except OSError:
            return False

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5)
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._thread = None
        self._server = None
        self._sock = None


class DemoAdapter(OpenAICompatAdapter):
    """演示 provider：转发实现与真实 OpenAI 兼容上游完全相同，只换地址。"""

    spec = ProviderSpec(
        id=DEMO_PROVIDER_ID,
        name="演示假上游",
        vendor="仓库内 scripts/fake_upstream.py",
        license="MIT",
        runtime="控制面进程内 uvicorn 线程（127.0.0.1 随机端口）",
        status="demo",
        description=(
            "仅用于演示：上游是本进程内的假服务，固定返回 [fake] 前缀内容；"
            "页面上的日志/用量/金额都来自真实网关请求，但模型能力不代表任何真实厂商。"
        ),
        default_base_url="http://127.0.0.1:0",
        capabilities=stub_capabilities(chat=True, messages=True, models=True),
        protocols=("openai.chat.completions", "anthropic.messages"),
        readiness="ready",
    )
    catalog_mode = "upstream"

    async def messages(self, payload: dict[str, Any], *, stream: bool):
        # 假上游 /v1/messages 流式与非流式都实现（scripts/fake_upstream.py），原样转发
        return await self._client().forward("/v1/messages", payload, stream=stream)


class DemoRuntime:
    """演示模式运行态：假上游 + provider 注册 + 数据播种 + readiness 探针。"""

    def __init__(self, settings: Settings, db: Database, registry: Any, upstream: DemoUpstream) -> None:
        self.settings = settings
        self.db = db
        self.registry = registry
        self.upstream = upstream
        self.seed_result: dict[str, Any] | None = None

    def covers(self, provider_id: str) -> bool:
        return str(provider_id).startswith("demo_")

    def status(self) -> dict[str, Any]:
        alive = self.upstream.alive()
        return {
            "ok": alive,
            "provider": DEMO_PROVIDER_ID,
            "upstream": self.upstream.base_url,
            "detail": "" if alive else "进程内演示假上游未运行",
        }

    async def seed(self) -> dict[str, Any] | None:
        """幂等播种：已有 demo_seed 只刷新凭据并补齐新增实体，不重复插入数据。"""
        if self.seed_result is not None:
            return self.seed_result
        conn = self.db.connect()
        try:
            existing = get_json_setting(conn, DEMO_SEED_SETTING, None)
            if isinstance(existing, dict) and existing.get("version") == DEMO_SEED_VERSION:
                seed, fresh = existing, False
            else:
                seed, fresh = self._seed_rows(conn), True
        finally:
            conn.close()

        self._write_credentials_file(seed["keys"])
        alert_calls = await self._seed_pool_and_alert(seed)
        await self._seed_page_samples(seed)

        if not fresh:
            logger.info("演示数据已存在（写入于 %s），跳过重复播种", seed.get("created_at"))
            self.seed_result = seed
            return seed

        self._finalize(seed, alert_calls + await self._seed_calls(seed))
        return self.seed_result

    # ---------- 播种 ----------

    def _seed_rows(self, conn) -> dict[str, Any]:
        ts = now()
        keys = [
            {"name": f"{DEMO_MARK}演示密钥 A", "token": KEY_PREFIX + secrets.token_urlsafe(24), "enabled": True},
            {"name": f"{DEMO_MARK}演示密钥 B（已停用）", "token": KEY_PREFIX + secrets.token_urlsafe(24), "enabled": False},
        ]
        for key in keys:
            cur = conn.execute(
                "INSERT INTO api_keys(name, key_hash, prefix, enabled, created_at, owner_user_id) "
                "VALUES(?,?,?,?,?,?)",
                (
                    key["name"],
                    hashlib.sha256(key["token"].encode()).hexdigest(),
                    key["token"][:STORED_PREFIX_LEN],
                    1 if key["enabled"] else 0,
                    ts,
                    self.settings.admin_username,
                ),
            )
            key["id"] = int(cur.lastrowid)

        # 演示价格：让统计和流水有金额；note 明说不是真实报价
        for model in ("fake-chat-1", "fake-chat-2"):
            for token_class, price in (("input", DEMO_PRICE_INPUT_MICRO), ("output", DEMO_PRICE_OUTPUT_MICRO)):
                conn.execute(
                    "INSERT OR IGNORE INTO price_books(provider, model, token_class, price_micro_per_1m, "
                    "effective_from, created_by, note) VALUES(?,?,?,?,?,?,?)",
                    (DEMO_PROVIDER_ID, model, token_class, price, 0, "demo",
                     f"{DEMO_MARK}演示价格，仅用于展示计费链路，不是任何厂商的真实报价"),
                )

        # 假上游不校验凭证，这条只是为了让凭据链与真实 provider 一致
        cur = conn.execute(
            "INSERT INTO provider_credentials(provider, label, base_url, secret, enabled, sort_order, note, "
            "created_at, updated_at) VALUES(?,?,?,?,1,0,?,?,?)",
            (DEMO_PROVIDER_ID, f"{DEMO_MARK}假上游凭证", "", secret_store.seal("demo-local-token"),
             f"{DEMO_MARK}演示凭据，可一键清除", ts, ts),
        )
        credential_id = int(cur.lastrowid)

        for idx, file in enumerate(DEMO_ACCOUNT_FILES):
            conn.execute(
                "INSERT INTO accounts(file, provider, uid, nickname, enterprise_id, realm, expires_at, credits, "
                "note, disabled, created_at, meta_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    file,
                    DEMO_PROVIDER_ID,
                    f"demo-uid-{idx + 1}",
                    f"{DEMO_MARK}示例账号 {'AB'[idx]}",
                    "",
                    "cn",
                    ts + 30 * 86400,
                    None,  # 不编造余额
                    f"{DEMO_MARK}演示账号，与真实账号无关",
                    0,
                    ts,
                    json.dumps({"demo": True}, ensure_ascii=False),
                ),
            )

        policy_ids: list[int] = []
        policy_rows = (
            ("", "", "day", 200, 5_000_000),
            (DEMO_PROVIDER_ID, "fake-chat-1", "month", 0, 20_000_000),
        )
        for provider, model, window, limit_requests, limit_micro in policy_rows:
            cur = conn.execute(
                "INSERT INTO quota_policies(key_id, provider, model, window, limit_micro, limit_requests, "
                "enabled, created_at, updated_at) VALUES(?,?,?,?,?,?,1,?,?)",
                (keys[0]["id"], provider, model, window, limit_micro, limit_requests, ts, ts),
            )
            policy_id = int(cur.lastrowid)
            policy_ids.append(policy_id)
            for percent in (80, 100):
                conn.execute(
                    "INSERT OR IGNORE INTO quota_alert_thresholds(policy_id, percent, created_at) VALUES(?,?,?)",
                    (policy_id, percent, ts),
                )

        seed = {
            "version": DEMO_SEED_VERSION,
            "created_at": ts,
            "provider_id": DEMO_PROVIDER_ID,
            "upstream": self.upstream.base_url,
            "keys": keys,
            "accounts": list(DEMO_ACCOUNT_FILES),
            "credential_ids": [credential_id],
            "policy_ids": policy_ids,
            "calls": 0,
        }
        set_json_setting(conn, DEMO_SEED_SETTING, seed)
        conn.commit()
        return seed

    async def _seed_pool_and_alert(self, seed: dict[str, Any]) -> int:
        """分组/账号池/流水 + 告警样例策略；老库缺哪块补哪块，失败直接抛不吞。

        返回本轮真正跑出来的告警样例请求数（策略已存在则为 0，避免每次启动重复灌）。
        """
        conn = self.db.connect()
        try:
            seed["sub2"] = self._seed_sub2api(conn)
            policy_id = self._seed_alert_policy(conn, seed["keys"][1])
            if policy_id is not None:
                seed.setdefault("policy_ids", []).append(policy_id)
            conn.commit()
        finally:
            conn.close()

        written = 0
        if policy_id is not None:
            for model, prompt in DEMO_ALERT_CALLS:
                await self._send_demo_call(key=seed["keys"][1], model=model, prompt=prompt)
                written += 1
            seed["calls"] = int(seed.get("calls") or 0) + written
        self._persist_seed(seed)
        return written

    def _seed_sub2api(self, conn) -> dict[str, Any]:
        """/quota 页三块数据：分组 → 账号池成员 → 按相对时间分布的扣减流水。"""
        seeded = conn.execute(
            "SELECT id FROM sub2_groups WHERE id LIKE 'demo\\_%' ESCAPE '\\'"
        ).fetchone()
        if seeded is not None:
            return self._sub2_summary(conn)

        ts = now()
        # 流水时间按当日零点（UTC+8）推算，写死时间戳会让「今日」永远对不上
        day_start = period_bounds("day", ts)[0]
        created_at = ts - 6 * 86400
        for group_id, name, platform, quota_limit in DEMO_SUB2_GROUPS:
            conn.execute(
                "INSERT INTO sub2_groups(id, name, platform, quota_limit, quota_used, reset_at, enabled, "
                "created_at, updated_at) VALUES(?,?,?,?,0,?,1,?,?)",
                (
                    group_id,
                    DEMO_MARK + name,
                    platform,
                    quota_limit,
                    period_bounds("day", ts)[1] if quota_limit else 0,
                    created_at,
                    ts,
                ),
            )

        slot_of: dict[str, int] = {}
        account_ids: dict[tuple[str, int], int] = {}
        spent_out: list[int] = []
        for group_id, name, weight, cooldown, enabled, exhausted in DEMO_SUB2_ACCOUNTS:
            slot = slot_of.get(group_id, 0)
            slot_of[group_id] = slot + 1
            cur = conn.execute(
                "INSERT INTO sub2_accounts(group_id, name, upstream_base, api_key, weight, quota_limit, quota_used, "
                "enabled, cooldown_until, last_used_at, created_at) VALUES(?,?,?,?,?,0,0,?,?,0,?)",
                (
                    group_id,
                    DEMO_MARK + name,
                    self.upstream.base_url,  # 假上游地址随进程变，展示用，转发也指向本机
                    f"sk-demo-{group_id}-{slot + 1}",
                    weight,
                    enabled,
                    ts + cooldown if cooldown else 0,
                    created_at,
                ),
            )
            account_ids[(group_id, slot)] = int(cur.lastrowid)
            if exhausted:
                spent_out.append(int(cur.lastrowid))

        for day, hour, minute, group_id, slot, model, prompt, completion, status, note, reason in DEMO_SUB2_LEDGER:
            ts_row = day_start - day * 86400 + hour * 3600 + minute * 60
            if day == 0:
                ts_row = min(ts_row, ts)  # 今天的行不能落在当前时刻之后
            conn.execute(
                "INSERT INTO sub2_ledger(ts, group_id, account_id, model, prompt_tokens, completion_tokens, cost, "
                "status, note, reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    ts_row,
                    group_id,
                    account_ids.get((group_id, slot)),
                    model,
                    prompt,
                    completion,
                    prompt + completion,
                    status,
                    note,
                    reason,
                ),
            )

        # 流水落库后再回填用量，分组/账号的 quota_used 与流水总额始终一致
        conn.execute(
            "UPDATE sub2_accounts SET quota_used = "
            "(SELECT COALESCE(SUM(cost),0) FROM sub2_ledger l WHERE l.account_id = sub2_accounts.id), "
            "last_used_at = (SELECT COALESCE(MAX(ts),0) FROM sub2_ledger l WHERE l.account_id = sub2_accounts.id) "
            "WHERE group_id LIKE 'demo\\_%' ESCAPE '\\'"
        )
        conn.execute(
            "UPDATE sub2_groups SET quota_used = "
            "(SELECT COALESCE(SUM(cost),0) FROM sub2_ledger l WHERE l.group_id = sub2_groups.id) "
            "WHERE id LIKE 'demo\\_%' ESCAPE '\\'"
        )
        # 额度用尽的样例账号：上限取已用量，落进不可用状态好展示徽标
        for account_id in spent_out:
            conn.execute("UPDATE sub2_accounts SET quota_limit = quota_used WHERE id = ?", (account_id,))
        return self._sub2_summary(conn)

    def _sub2_summary(self, conn) -> dict[str, Any]:
        """回读 demo_ 前缀的三张表计数，记进 demo_seed 供清理与排障核对。"""
        row = conn.execute(
            "SELECT (SELECT COUNT(*) FROM sub2_groups WHERE id LIKE 'demo\\_%' ESCAPE '\\') AS groups, "
            "(SELECT COUNT(*) FROM sub2_accounts WHERE group_id LIKE 'demo\\_%' ESCAPE '\\') AS accounts, "
            "(SELECT COUNT(*) FROM sub2_ledger WHERE group_id LIKE 'demo\\_%' ESCAPE '\\') AS ledger"
        ).fetchone()
        group_ids = [
            str(item["id"])
            for item in conn.execute(
                "SELECT id FROM sub2_groups WHERE id LIKE 'demo\\_%' ESCAPE '\\' ORDER BY id"
            ).fetchall()
        ]
        return {"groups": int(row["groups"]), "accounts": int(row["accounts"]),
                "ledger": int(row["ledger"]), "group_ids": group_ids}

    def _seed_alert_policy(self, conn, key: dict[str, Any]) -> int | None:
        """给已停用的演示密钥 B 挂一条会触顶的日策略；已存在就不再建（返回 None）。"""
        existing = conn.execute(
            "SELECT id FROM quota_policies WHERE key_id = ? AND provider = '' AND model = '' AND window = 'day'",
            (int(key["id"]),),
        ).fetchone()
        if existing is not None:
            return None
        ts = now()
        cur = conn.execute(
            "INSERT INTO quota_policies(key_id, provider, model, window, limit_micro, limit_requests, enabled, "
            "created_at, updated_at) VALUES(?, '', '', 'day', 0, ?, 1, ?, ?)",
            (int(key["id"]), DEMO_ALERT_LIMIT_REQUESTS, ts, ts),
        )
        policy_id = int(cur.lastrowid)
        for percent in (80, 100):
            conn.execute(
                "INSERT OR IGNORE INTO quota_alert_thresholds(policy_id, percent, created_at) VALUES(?,?,?)",
                (policy_id, percent, ts),
            )
        return policy_id

    def _persist_seed(self, seed: dict[str, Any]) -> None:
        conn = self.db.connect()
        try:
            set_json_setting(conn, DEMO_SEED_SETTING, seed)
            conn.commit()
        finally:
            conn.close()

    # ---------- 页面样例：模型映射 / 配额拒绝 / 失败登录（真链路，逐项判重） ----------

    async def _seed_page_samples(self, seed: dict[str, Any]) -> dict[str, Any]:
        """三个页面样例；任一步失败抛 DemoModeError，不静默降级。"""
        conn = self.db.connect()
        try:
            added = self._seed_model_map(conn)
        except DemoModeError:
            raise
        except Exception as exc:  # noqa: BLE001 - 播种失败不允许静默降级
            raise DemoModeError(f"模型映射播种失败：{exc}") from exc
        finally:
            conn.close()
        denials = await self._seed_gateway_denial(seed)
        failures = await self._seed_login_failure()
        samples = {"model_map": added, "gateway_denials": denials, "login_failures": failures}
        seed["page_samples"] = samples
        self._persist_seed(seed)
        logger.info("演示页面样例播种：%s", json.dumps(samples, ensure_ascii=False))
        return samples

    def _seed_model_map(self, conn) -> int:
        """与真实表 merge：只补缺键，已有键（含用户改过的值）原样不动。"""
        mapping = get_json_setting(conn, "model_map", {})
        if mapping is None:
            mapping = {}
        if not isinstance(mapping, dict):
            raise DemoModeError(f"settings.model_map 不是对象，无法播种：{type(mapping).__name__}")
        added = 0
        for request_name, target in DEMO_MODEL_MAP.items():
            if request_name in mapping:
                continue
            mapping[request_name] = target
            added += 1
        if added:
            set_json_setting(conn, "model_map", mapping)
            conn.commit()
        return added

    def _key_b_state(self, key_id: int) -> tuple[bool, bool]:
        """配额拒绝行是否已存在、密钥当前是否启用（窗口自愈判定）。"""
        conn = self.db.connect()
        try:
            denied = (
                conn.execute(
                    "SELECT 1 FROM request_logs WHERE key_id = ? AND status = 429 "
                    "AND error LIKE '%quota_exceeded%'",
                    (key_id,),
                ).fetchone()
                is not None
            )
            row = conn.execute("SELECT enabled FROM api_keys WHERE id = ?", (key_id,)).fetchone()
        finally:
            conn.close()
        return denied, bool(row is not None and row["enabled"])

    async def _seed_gateway_denial(self, seed: dict[str, Any]) -> int:
        """配额拒绝样例：真经网关 _quota_guard；启停走真实接口，窗口结束必恢复停用。"""
        key = seed["keys"][1]
        key_id = int(key["id"])
        denied, enabled = self._key_b_state(key_id)
        if denied and not enabled:
            return 0

        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=_control_app()),
                base_url="http://127.0.0.1",
                trust_env=False,
            ) as client:
                session = await self._admin_session(client)
                if denied:
                    # 上次进程在启用窗口内被杀：只补一次真实接口恢复停用
                    await self._toggle_key(client, key_id, False, session)
                else:
                    await self._toggle_key(client, key_id, True, session)
                    try:
                        await self._call_until_denied(client, key)
                    finally:
                        await self._toggle_key(client, key_id, False, session)
                await client.post("/api/logout", headers=session)
        except DemoModeError:
            raise
        except Exception as exc:  # noqa: BLE001 - 传输层异常一律转成播种失败
            raise DemoModeError(f"配额拒绝样例播种失败：{exc}") from exc

        denied_after, enabled_after = self._key_b_state(key_id)
        if not denied_after or enabled_after:
            raise DemoModeError(
                f"配额拒绝样例复核失败：拒绝行={denied_after}，密钥B启用={enabled_after}"
            )
        return 0 if denied else 1

    async def _admin_session(self, client: httpx.AsyncClient) -> dict[str, str]:
        """真登录拿会话；显式 Cookie 头不受 cookie_secure 影响。"""
        resp = await client.post(
            "/api/login",
            json={
                "username": self.settings.admin_username,
                "password": self.settings.admin_password,
            },
        )
        if resp.status_code != 200:
            raise DemoModeError(f"演示播种管理员登录失败：HTTP {resp.status_code}")
        token = resp.cookies.get(SESSION_COOKIE)
        if not token:
            raise DemoModeError("演示播种登录未返回会话 cookie")
        return {"Cookie": f"{SESSION_COOKIE}={token}"}

    async def _toggle_key(
        self, client: httpx.AsyncClient, key_id: int, enabled: bool, session: dict[str, str]
    ) -> None:
        resp = await client.patch(f"/api/keys/{key_id}", json={"enabled": enabled}, headers=session)
        if resp.status_code != 200:
            raise DemoModeError(
                f"真实接口启停密钥 {key_id} 失败：enabled={enabled} HTTP {resp.status_code}"
            )

    async def _call_until_denied(self, client: httpx.AsyncClient, key: dict[str, Any]) -> None:
        """真打 /v1/chat/completions 直到 _quota_guard 拒出 429。"""
        headers = {"Authorization": f"Bearer {key['token']}", "x-provider": DEMO_PROVIDER_ID}
        payload = {
            "model": "fake-chat-1",
            "messages": [{"role": "user", "content": f"{DEMO_MARK}配额拒绝样例请求"}],
        }
        for _ in range(DEMO_GATEWAY_MAX_ATTEMPTS):
            resp = await client.post("/v1/chat/completions", json=payload, headers=headers)
            if resp.status_code == 429:
                return
        raise DemoModeError(
            f"配额拒绝样例未触发：{DEMO_GATEWAY_MAX_ATTEMPTS} 笔内无 429，"
            f"检查密钥 B 的 limit={DEMO_ALERT_LIMIT_REQUESTS} 日策略"
        )

    async def _seed_login_failure(self) -> int:
        """安全页失败登录样例：专用探测账号真错登一次，落 audit_logs。"""
        if self._login_failure_exists():
            return 0
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=_control_app()),
                base_url="http://127.0.0.1",
                trust_env=False,
            ) as client:
                resp = await client.post(
                    "/api/login",
                    json={"username": DEMO_PROBE_USER, "password": DEMO_PROBE_PASSWORD},
                )
        except DemoModeError:
            raise
        except Exception as exc:  # noqa: BLE001 - 传输层异常一律转成播种失败
            raise DemoModeError(f"失败登录样例播种失败：{exc}") from exc
        if resp.status_code != 401:
            raise DemoModeError(f"失败登录样例期望 401，实际 HTTP {resp.status_code}")
        if not self._login_failure_exists():
            raise DemoModeError("失败登录样例复核失败：audit_logs 未出现 login_failed 行")
        return 1

    def _login_failure_exists(self) -> bool:
        conn = self.db.connect()
        try:
            return (
                conn.execute(
                    "SELECT 1 FROM audit_logs WHERE actor = ? AND action = 'login_failed'",
                    (DEMO_PROBE_USER,),
                ).fetchone()
                is not None
            )
        finally:
            conn.close()

    async def _seed_calls(self, seed: dict[str, Any]) -> int:
        """通过适配器真实 HTTP 打到假上游，日志/用量/金额都是真跑出来的。"""
        key = next((item for item in seed["keys"] if item.get("enabled")), seed["keys"][0])
        plan = (
            ("fake-chat-1", "演示请求：一句话说明这个网关做了什么。"),
            ("fake-chat-2", "演示请求：列出一条请求经过的环节。"),
            ("fake-chat-1", "演示请求：解释演示数据为什么都带 [demo] 标记。"),
            ("fake-chat-2", "演示请求：演示上游返回的 usage 长什么样。"),
            ("fake-chat-1", "演示请求：这是一次普通非流式调用。"),
            ("fake-chat-err-429", "演示请求：故意触发上游 429，证明失败也进日志。"),
        )
        written = 0
        for model, prompt in plan:
            await self._send_demo_call(key=key, model=model, prompt=prompt)
            written += 1
        return written

    async def _send_demo_call(self, *, key: dict[str, Any], model: str, prompt: str) -> None:
        """一次真实播种请求；失败直接抛，不允许悄悄少掉日志/用量/告警。"""
        adapter = self.registry.get(DEMO_PROVIDER_ID)
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": f"{DEMO_MARK}演示系统提示（假上游会回显这行）"},
                {"role": "user", "content": prompt},
            ],
        }
        started = time.perf_counter()
        try:
            reply = await adapter.chat_completion(payload, stream=False)
        except Exception as exc:  # noqa: BLE001 - 传输层异常一律转成启动失败
            raise DemoModeError(f"演示请求 model={model} 播种失败：{exc}") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)
        body = reply.body or b""
        failed = reply.status_code >= 400
        self._write_call(
            key=key,
            model=model,
            status=reply.status_code,
            usage=None if failed else extract_usage(body),
            latency_ms=latency_ms,
            error=body[:200].decode("utf-8", "ignore") if failed else None,
        )

    def _write_call(
        self,
        *,
        key: dict[str, Any],
        model: str,
        status: int,
        usage: dict[str, Any] | None,
        latency_ms: int,
        error: str | None,
    ) -> None:
        ts = now()
        prompt_tokens, completion_tokens = totals(usage)
        conn = self.db.connect()
        try:
            log_cur = conn.execute(
                "INSERT INTO request_logs(ts, provider, model, status, prompt_tokens, completion_tokens, "
                "latency_ms, stream, error, key_id, key_name) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (ts, DEMO_PROVIDER_ID, model, status, prompt_tokens, completion_tokens,
                 latency_ms, 0, error, key["id"], key["name"]),
            )
            record_usage(
                conn,
                key_id=key["id"],
                key_name=key["name"],
                provider=DEMO_PROVIDER_ID,
                model=model,
                status=status,
                stream=False,
                usage=usage,
                latency_ms=latency_ms,
                ts=ts,
            )
            pricing = {
                "provider": DEMO_PROVIDER_ID,
                "model": model,
                "at": ts,
                "lines": resolve_prices(conn, provider=DEMO_PROVIDER_ID, model=model, at=ts),
            }
            settle(
                conn,
                ts=ts,
                key_id=key["id"],
                key_name=key["name"],
                provider=DEMO_PROVIDER_ID,
                model=model,
                status=status,
                usage=usage,
                pricing=pricing,
                log_id=int(log_cur.lastrowid),
            )
            conn.commit()
        finally:
            conn.close()

    def _finalize(self, seed: dict[str, Any], calls: int) -> None:
        conn = self.db.connect()
        try:
            stored = get_json_setting(conn, DEMO_SEED_SETTING, {})
            if not isinstance(stored, dict):
                stored = dict(seed)
            stored["calls"] = calls
            stored["upstream"] = self.upstream.base_url
            set_json_setting(conn, DEMO_SEED_SETTING, stored)
            conn.commit()
            self.seed_result = stored
        finally:
            conn.close()

    def _write_credentials_file(self, keys: list[dict[str, Any]]) -> None:
        path = Path(self.settings.data_dir) / DEMO_CREDENTIALS_FILE
        lines = [
            f"# {DEMO_MARK}UNIFIED_DEMO_MODE 演示密钥，仅限本机演示，禁止用于生产",
            f"# 网关地址：http://{self.settings.host}:{self.settings.port}/v1",
            "# 清除演示数据：scripts/demo.ps1 -Clear / scripts/demo.sh --clear",
        ]
        for key in keys:
            if isinstance(key, dict) and key.get("token"):
                lines.append(f"{key['name']} = {key['token']}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.chmod(path, 0o600)
        except OSError as exc:
            logger.warning("演示密钥文件写入失败 %s：%s", path, exc)

    def stop(self) -> None:
        self.upstream.stop()


def _quiesce_defaults(db: Database, registry: Any) -> list[str]:
    """临时停用「默认启用但零配置」的 provider。

    首次建库时 codebuddy 默认启用、指向未启动的 7864；留着它会让演示的 /v1/models
    和首次对话先为它的超时买单。只动同时满足三条的行：无凭据、base_url 为空
    （即还是 spec 默认地址）、readiness 不是 ready。清除演示数据时按清单恢复。
    """
    view = {str(item.get("id")): item for item in registry.list_public()}
    conn = db.connect()
    try:
        configured = {
            str(row["provider"])
            for row in conn.execute("SELECT DISTINCT provider FROM provider_credentials").fetchall()
        }
        target: list[str] = []
        for row in conn.execute("SELECT id, base_url FROM providers WHERE enabled = 1").fetchall():
            pid = str(row["id"])
            if pid == DEMO_PROVIDER_ID or pid in configured or str(row["base_url"] or "").strip():
                continue
            if str(view.get(pid, {}).get("readiness") or "") == "ready":
                continue
            target.append(pid)
        if target:
            ts = now()
            for pid in target:
                conn.execute("UPDATE providers SET enabled = 0, updated_at = ? WHERE id = ?", (ts, pid))
            previous = get_json_setting(conn, DEMO_DISABLED_SETTING, [])
            prev_list = [str(item) for item in previous] if isinstance(previous, list) else []
            merged = sorted({*prev_list, *target})
            set_json_setting(conn, DEMO_DISABLED_SETTING, merged)
            conn.commit()
            logger.warning("演示模式临时停用零配置 provider：%s（清除演示数据时恢复）", ", ".join(target))
        return target
    finally:
        conn.close()


def install(settings: Settings, db: Database, registry: Any) -> DemoRuntime:
    """同步安装：校验回环地址 → 起假上游 → 注册 demo provider（数据在 lifespan 里播种）。"""
    ensure_loopback_bind(settings.host)
    upstream = DemoUpstream()
    upstream.start()
    adapter = DemoAdapter(settings, upstream.base_url)
    registry.register_provider(adapter, enabled=True, label="演示假上游（demo，仅本机）")
    _quiesce_defaults(db, registry)
    logger.warning(
        "UNIFIED_DEMO_MODE=1：已启动进程内假上游 %s；数据仅用于演示，禁止用于生产",
        upstream.base_url,
    )
    return DemoRuntime(settings=settings, db=db, registry=registry, upstream=upstream)


def clear_demo_data(db: Database, settings: Settings | None = None) -> dict[str, int]:
    """按 demo 标记清除演示数据，不碰任何非 demo 行；可在未开启演示模式时执行。"""
    counts: dict[str, int] = {}
    conn = db.connect()

    def drop(label: str, sql: str, params: tuple = ()) -> None:
        cur = conn.execute(sql, params)
        if cur.rowcount:
            counts[label] = counts.get(label, 0) + int(cur.rowcount)

    try:
        demo_policies = [
            int(row["id"])
            for row in conn.execute(
                "SELECT id FROM quota_policies WHERE provider = ? "
                "OR key_id IN (SELECT id FROM api_keys WHERE name LIKE ?)",
                (DEMO_PROVIDER_ID, DEMO_MARK + "%"),
            ).fetchall()
        ]
        if demo_policies:
            marks = ",".join("?" for _ in demo_policies)
            drop(
                "quota_alerts",
                f"DELETE FROM quota_alerts WHERE policy_id IN ({marks})",
                tuple(demo_policies),
            )
            drop(
                "quota_alert_thresholds",
                f"DELETE FROM quota_alert_thresholds WHERE policy_id IN ({marks})",
                tuple(demo_policies),
            )
            drop(
                "quota_policies",
                f"DELETE FROM quota_policies WHERE id IN ({marks})",
                tuple(demo_policies),
            )
        drop("request_logs", "DELETE FROM request_logs WHERE provider = ?", (DEMO_PROVIDER_ID,))
        drop("usage_records", "DELETE FROM usage_records WHERE provider = ?", (DEMO_PROVIDER_ID,))
        drop("billing_ledger", "DELETE FROM billing_ledger WHERE provider = ?", (DEMO_PROVIDER_ID,))
        drop("price_book_snapshots", "DELETE FROM price_book_snapshots WHERE provider = ?", (DEMO_PROVIDER_ID,))
        # sub2api 分组按 id 前缀成组清：先流水、后账号、再分组，不留孤儿行
        drop("sub2_ledger", "DELETE FROM sub2_ledger WHERE group_id LIKE 'demo\\_%' ESCAPE '\\'")
        drop("sub2_accounts", "DELETE FROM sub2_accounts WHERE group_id LIKE 'demo\\_%' ESCAPE '\\'")
        drop("sub2_groups", "DELETE FROM sub2_groups WHERE id LIKE 'demo\\_%' ESCAPE '\\'")
        drop(
            "accounts",
            "DELETE FROM accounts WHERE provider = ? OR file LIKE 'demo\\_%' ESCAPE '\\'",
            (DEMO_PROVIDER_ID,),
        )
        drop("provider_credentials", "DELETE FROM provider_credentials WHERE provider = ?", (DEMO_PROVIDER_ID,))
        drop("price_books", "DELETE FROM price_books WHERE provider = ?", (DEMO_PROVIDER_ID,))
        drop("api_keys", "DELETE FROM api_keys WHERE name LIKE ?", (DEMO_MARK + "%",))
        drop("providers", "DELETE FROM providers WHERE id = ?", (DEMO_PROVIDER_ID,))
        drop("settings", "DELETE FROM settings WHERE key = ?", (DEMO_SEED_SETTING,))
        # 安全页样例：只删专用探测账号的失败登录行，真实账号的登录统计不动
        drop(
            "audit_login_failures",
            "DELETE FROM audit_logs WHERE actor = ? AND action = 'login_failed'",
            (DEMO_PROBE_USER,),
        )
        # 模型映射样例：按键摘除演示键（值被改过也摘）；剩非空写回、空表删行，重复调用幂等
        mapping = get_json_setting(conn, "model_map", {})
        if isinstance(mapping, dict):
            kept = {k: v for k, v in mapping.items() if k not in DEMO_MODEL_MAP}
            stripped = len(mapping) - len(kept)
            if stripped:
                if kept:
                    set_json_setting(conn, "model_map", kept)
                else:
                    conn.execute("DELETE FROM settings WHERE key = ?", ("model_map",))
                counts["model_map"] = stripped

        # 恢复被演示模式临时停用的零配置 provider
        paused = get_json_setting(conn, DEMO_DISABLED_SETTING, [])
        restored = 0
        if isinstance(paused, list):
            ts = now()
            for pid in paused:
                if not isinstance(pid, str) or pid == DEMO_PROVIDER_ID:
                    continue
                cur = conn.execute(
                    "UPDATE providers SET enabled = 1, updated_at = ? WHERE id = ? AND enabled = 0",
                    (ts, pid),
                )
                restored += int(cur.rowcount or 0)
        if restored:
            counts["providers_restored"] = restored
        drop("settings", "DELETE FROM settings WHERE key = ?", (DEMO_DISABLED_SETTING,))
        conn.commit()
    finally:
        conn.close()

    if settings is not None:
        path = Path(settings.data_dir) / DEMO_CREDENTIALS_FILE
        if path.exists():
            try:
                path.unlink()
                counts["credentials_file"] = 1
            except OSError as exc:
                logger.warning("演示密钥文件删除失败 %s：%s", path, exc)
    return counts


def main(argv: list[str] | None = None) -> int:
    """命令行入口：仅支持清除（python -m app.demo --clear）。"""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m app.demo", description="演示模式数据清理")
    parser.add_argument("--clear", action="store_true", help="清除所有带 demo 标记的演示数据")
    args = parser.parse_args(argv)
    if not args.clear:
        parser.print_help()
        return 2

    resolved = load_settings()
    db = Database(resolved.db_path)
    removed = clear_demo_data(db, resolved)
    print(json.dumps(removed, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
