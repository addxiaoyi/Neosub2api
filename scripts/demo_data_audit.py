#!/usr/bin/env python3
"""演示实例页面数据核对：逐个调用前端页面依赖的接口，输出三档结论。

三档口径：
  OK   = 有数据且字段齐（页面可正常渲染）
  GAP  = 有数据但页面要的字段缺（页面会缺列/缺徽标）
  EMPTY = 返回空（页面会空白；note 里区分「演示没播种」还是「查询条件问题」）

只调 HTTP 接口 + 只读 sqlite_master，不修改 apps/control/app 与 apps/web。
用法（演示实例需已按 scripts/demo.ps1 起好，数据目录指向临时目录）：

  python scripts/demo_data_audit.py \
      --base http://127.0.0.1:8403 \
      --admin-password '<管理员口令>' \
      --db '<临时数据目录>/unified.db'

环境变量等价：AUDIT_BASE_URL / AUDIT_ADMIN_PASSWORD / AUDIT_DB /
AUDIT_VIEWER（默认 audit_viewer）/ AUDIT_VIEWER_PASSWORD（默认 AuditViewer-2026!）。
httpx 显式 trust_env=False，避免代理环境变量劫持回环请求。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Any, Callable

import httpx

OK = "有数据且字段齐"
GAP = "有数据但字段缺"
EMPTY = "返回空"
DENIED = "按设计拒绝(403/400)"  # 权限/参数闸门，不是数据缺失

VIEWER_KEY_NAME = "[audit] viewer key"
PROBE_USERNAME = "audit_probe"
GATEWAY_MODEL = "fake-chat-1"

rows: list[tuple[str, str, str]] = []


def record(label: str, tier: str, note: str = "") -> str:
    rows.append((label, tier, note))
    return tier


def missing_fields(obj: dict[str, Any], fields: tuple[str, ...]) -> list[str]:
    return [f for f in fields if f not in obj]


def parse_json_error(raw: Any) -> dict[str, Any] | None:
    """error 列里的 denial JSON；非 JSON（上游错误原文）返回 None。"""
    if not isinstance(raw, str) or not raw.strip().startswith("{"):
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


class Audit:
    def __init__(self, base: str, admin_user: str, admin_password: str,
                 viewer: str, viewer_password: str, db_path: str | None):
        self.base = base.rstrip("/")
        self.admin_user = admin_user
        self.admin_password = admin_password
        self.viewer = viewer
        self.viewer_password = viewer_password
        self.db_path = db_path
        # trust_env=False：显式绕过 http_proxy 等环境变量，只打本机回环
        self.admin = httpx.Client(base_url=self.base, timeout=30.0, trust_env=False)
        self.guest = httpx.Client(base_url=self.base, timeout=30.0, trust_env=False)

    # ---------- 登录与场景准备 ----------

    def login_admin(self) -> None:
        r = self.admin.post("/api/login", json={
            "username": self.admin_user, "password": self.admin_password})
        if r.status_code != 200:
            raise SystemExit(f"管理员登录失败 {r.status_code}: {r.text[:300]}")

    def login_viewer(self) -> None:
        r = self.guest.post("/api/login", json={
            "username": self.viewer, "password": self.viewer_password})
        if r.status_code != 200:
            raise SystemExit(f"viewer 登录失败 {r.status_code}: {r.text[:300]}")

    def ensure_viewer(self) -> None:
        r = self.admin.post("/api/users", json={
            "username": self.viewer, "password": self.viewer_password, "role": "viewer"})
        if r.status_code not in (200, 409):
            raise SystemExit(f"创建 viewer 失败 {r.status_code}: {r.text[:300]}")

    def ensure_viewer_key(self) -> tuple[int, str]:
        """确保存在归属 viewer 的密钥；返回 (key_id, 明文 token)。复跑时复用已有 key。"""
        keys = self.admin.get("/api/keys").json()
        for item in keys:
            if item.get("owner_user_id") == self.viewer and item.get("enabled"):
                return int(item["id"]), ""
        r = self.admin.post("/api/keys", json={
            "name": VIEWER_KEY_NAME, "owner_user_id": self.viewer})
        if r.status_code != 200:
            raise SystemExit(f"为 viewer 建 key 失败 {r.status_code}: {r.text[:300]}")
        body = r.json()
        return int(body["id"]), str(body["key"])

    def ensure_viewer_policy(self, key_id: int) -> None:
        """日窗口、1 次请求上限、80/100 阈值：一笔成功触发告警，第二笔被拒产生
        含 used/limit/unit 的拒绝日志。upsert 幂等，复跑只刷新。"""
        r = self.admin.post("/api/quota/policies", json={
            "key_id": key_id, "window": "day", "provider": "", "model": "",
            "limit_micro": 0, "limit_requests": 1, "thresholds": [80, 100],
            "enabled": True})
        if r.status_code != 200:
            raise SystemExit(f"建 viewer 配额策略失败 {r.status_code}: {r.text[:300]}")

    def gateway_calls(self, token: str, budget: int = 3) -> list[int]:
        """用该 key 打网关，直到出现 429 配额拒绝；返回观察到的状态码序列。"""
        statuses: list[int] = []
        for i in range(budget):
            r = self.guest.post("/v1/chat/completions",
                                headers={"Authorization": f"Bearer {token}"},
                                json={"model": GATEWAY_MODEL, "messages": [
                                    {"role": "user", "content": f"demo audit call {i}"}]})
            statuses.append(r.status_code)
            if r.status_code == 429:
                break
            if r.status_code >= 500:
                break
        return statuses

    def probe_login_failure(self) -> None:
        """补一条登录失败审计，供 /api/security/login-failures 展示；用独立用户名，
        不占用管理员的失败节流计数。"""
        anon = httpx.Client(base_url=self.base, timeout=15.0, trust_env=False)
        try:
            anon.post("/api/login", json={"username": PROBE_USERNAME, "password": "x-wrong"})
        finally:
            anon.close()

    # ---------- 管理员侧接口三档 ----------

    def check_status(self) -> None:
        body = self.admin.get("/api/status").json()
        if "connected" not in body:
            record("/api/status", GAP, f"缺 connected 字段: {sorted(body)}")
        else:
            note = f"connected={body['connected']}"
            if body.get("error"):
                note += f"; error={str(body['error'])[:80]}"
            record("/api/status", OK, note)

    def check_models(self) -> None:
        body = self.admin.get("/api/models").json()
        models = body.get("models") or []
        if not models:
            record("/api/models", EMPTY, "count=0，模型页无任何可渲染行")
            return
        want = ("id", "provider", "providers", "provider_states",
                "readiness", "requires", "available", "enabled", "unavailable_reason")
        bad: list[str] = []
        blind_states: list[str] = []
        providers: set[str] = set()
        for m in models:
            miss = missing_fields(m, want)
            if miss:
                bad.append(f"{m.get('id')}缺{miss}")
            for pid, st in (m.get("provider_states") or {}).items():
                providers.add(pid)
                if ("readiness" not in st or "requires" not in st
                        or "available" not in st or "enabled" not in st
                        or "unavailable_reason" not in st):
                    bad.append(f"{m.get('id')}/{pid} state缺字段")
                if st.get("enabled") and not st.get("available") and not st.get("unavailable_reason"):
                    blind_states.append(f"{m.get('id')}/{pid}")
        note = f"count={len(models)} providers={sorted(providers)}"
        # 覆盖「有 enabled 却没进 models 的 provider」：health/ready 扫全部 provider
        ready = self.admin.get("/api/health/ready").json()
        for p in (ready.get("checks", {}).get("providers", {}) or {}).get("items", []) or []:
            if p.get("enabled") and not p.get("available") and not p.get("requires"):
                blind_states.append(f"health_ready/{p.get('id')}")
        if blind_states:
            note += f"; enabled但available=false且无原因: {blind_states}"
        if bad:
            record("/api/models", GAP, "; ".join(bad[:4]) + f"; {note}")
        elif blind_states:
            record("/api/models", GAP, note)
        else:
            record("/api/models", OK, note)

    def check_keys_and_tokens(self) -> None:
        keys = self.admin.get("/api/keys").json()
        want = ("id", "name", "prefix", "enabled", "created_at", "owner_user_id")
        if not isinstance(keys, list) or not keys:
            record("/api/keys", EMPTY, "密钥页与日志页的 key 过滤下拉都会空")
        else:
            miss = [missing_fields(k, want) for k in keys if missing_fields(k, want)]
            demo = sum(1 for k in keys if str(k.get("name", "")).startswith("[demo]"))
            record("/api/keys", GAP if miss else OK,
                   f"n={len(keys)} demo标记={demo}" + (f" 缺字段={miss[0]}" if miss else ""))

        tokens = self.admin.get("/api/tokens").json()
        want_t = ("id", "name", "prefix", "scope", "enabled", "created_at", "last_used_at")
        if not isinstance(tokens, list) or not tokens:
            record("/api/tokens", EMPTY, "密钥令牌页无行")
        else:
            miss = [missing_fields(t, want_t) for t in tokens if missing_fields(t, want_t)]
            record("/api/tokens", GAP if miss else OK,
                   f"n={len(tokens)}" + (f" 缺字段={miss[0]}" if miss else ""))

    def check_logs(self, demo_key_id: int | None) -> None:
        page1 = self.admin.get("/api/logs", params={"page": 1, "size": 5}).json()
        total = int(page1.get("total") or 0)
        items = page1.get("items") or []
        if total <= 0 or not items:
            record("/api/logs", EMPTY, "日志页首屏空白（分页 total=0）")
            return
        want = ("id", "ts", "key_id", "key_name", "key_prefix", "provider", "model",
                "status", "prompt_tokens", "completion_tokens", "latency_ms",
                "error", "stream")
        miss = [missing_fields(it, want) for it in items if missing_fields(it, want)]

        page2 = self.admin.get("/api/logs", params={"page": 2, "size": 5}).json()
        paged = bool(page2.get("items")) and page2["items"][0]["id"] != items[0]["id"]

        key_note = ""
        if demo_key_id is not None:
            by_key = self.admin.get("/api/logs", params={"key_id": demo_key_id}).json()
            all_owned = all(it["key_id"] == demo_key_id for it in by_key.get("items") or [])
            key_note = f" key过滤: total={by_key.get('total')} 命中={all_owned}"
            if not by_key.get("total"):
                record("/api/logs", GAP, f"按 demo key_id={demo_key_id} 过滤结果为空，"
                       "日志页按 key 下拉过滤会显示空表" + key_note)
                return

        # 错误详情三件套：denial JSON 里的 used/limit/unit（日志详情抽屉按字段拆开渲染）
        errs = self.admin.get("/api/logs", params={"status": "error", "size": 50}).json()
        trio = None
        for it in errs.get("items") or []:
            parsed = parse_json_error(it.get("error"))
            if parsed and {"used", "limit", "unit"} <= set(parsed):
                trio = {k: parsed[k] for k in ("type", "used", "limit", "unit")}
                break
        note = (f"total={total} 分页翻页={paged} 错误行={len(errs.get('items') or [])}"
                f"{key_note}")
        if miss:
            record("/api/logs", GAP, f"首屏缺字段={miss[0]}; {note}")
        elif not trio:
            record("/api/logs", GAP,
                   note + "；错误详情无 used/limit/unit 三件套"
                          "（无配额拒绝样例，详情抽屉只会显示原始串）")
        else:
            record("/api/logs", OK, note + f" 三件套={trio}")

    def check_stats(self) -> None:
        summary = self.admin.get("/api/stats/summary").json()
        want = ("today_requests", "today_tokens", "today_credit", "week_credit",
                "total_credit", "week_requests", "total_requests", "active_keys")
        miss = missing_fields(summary, want)
        if miss:
            record("/api/stats/summary", GAP, f"缺字段={miss}")
        elif int(summary.get("total_requests") or 0) <= 0:
            record("/api/stats/summary", EMPTY,
                   "total_requests=0：仪表盘五张卡全是 0（演示应有真跑请求）")
        else:
            record("/api/stats/summary", OK,
                   f"today_req={summary['today_requests']} week_req={summary['week_requests']} "
                   f"total_req={summary['total_requests']} "
                   f"today_credit={summary['today_credit']} total_credit={summary['total_credit']}")

        daily = self.admin.get("/api/stats/daily", params={"days": 7}).json()
        want_d = ("day", "requests", "prompt_tokens", "completion_tokens", "credit", "failed")
        miss = [missing_fields(d, want_d) for d in daily if missing_fields(d, want_d)]
        active = sum(int(d.get("requests") or 0) + int(d.get("failed") or 0) for d in daily)
        if not daily:
            record("/api/stats/daily", EMPTY, "days=7 返回空列表")
        elif miss:
            record("/api/stats/daily", GAP, f"缺字段={miss[0]}")
        elif active <= 0:
            record("/api/stats/daily", EMPTY, "7 天桶全为 0，曲线图是平线")
        else:
            record("/api/stats/daily", OK, f"桶数={len(daily)} 7天有量桶={active}")

    def check_quota(self) -> None:
        policies = self.admin.get("/api/quota/policies").json()
        want = ("id", "key_id", "key_name", "provider", "model", "window",
                "limit_micro", "limit_usd", "limit_requests", "enabled", "thresholds")
        if not isinstance(policies, list) or not policies:
            record("/api/quota/policies", EMPTY, "配额策略页空表（演示应播种 3 条）")
        else:
            miss = [missing_fields(p, want) for p in policies if missing_fields(p, want)]
            record("/api/quota/policies", GAP if miss else OK,
                   f"n={len(policies)}" + (f" 缺字段={miss[0]}" if miss else ""))

        status = self.admin.get("/api/quota/status").json()
        items = status.get("items") or []
        want_s = ("policy", "period_start", "period_end", "reset_at", "used_micro",
                  "used_usd", "used_requests", "remaining_micro", "ratio_micro",
                  "ratio_requests", "exceeded", "alerts")
        if not items:
            record("/api/quota/status", EMPTY, "用量状态页空表")
        else:
            miss = [missing_fields(i, want_s) for i in items if missing_fields(i, want_s)]
            exceeded = [i for i in items if i.get("exceeded")]
            record("/api/quota/status", GAP if miss else OK,
                   f"items={len(items)} exceeded={len(exceeded)}"
                   + (f" 缺字段={miss[0]}" if miss else ""))
        # 「/quota 某些情形仍空」探针：空串/完整密钥两种入参都应 400，而非空集
        probe = self.admin.get("/api/quota/status", params={"key_id": ""})
        probe2 = self.admin.get("/api/quota/status", params={"key_id": "u2a_probe000000"})
        both_rejected = probe.status_code == 400 and probe2.status_code == 400
        record("/api/quota/status?key_id=空串/u2a_", DENIED if both_rejected else GAP,
               f"空串→{probe.status_code} u2a_→{probe2.status_code}"
               f"（{str(probe.json().get('detail'))[:60]}）")

        alerts = self.admin.get("/api/quota/alerts").json()
        want_a = ("policy_id", "threshold_percent", "window", "period_start",
                  "triggered_at", "metric", "used_micro", "limit_micro",
                  "used_requests", "limit_requests", "key_id", "key_name", "key_prefix")
        items = alerts.get("items") or []
        if not isinstance(alerts, dict) or not alerts.get("total"):
            record("/api/quota/alerts", EMPTY, "告警历史空（演示应有 80%/100% 两条）")
        else:
            miss = [missing_fields(a, want_a) for a in items if missing_fields(a, want_a)]
            record("/api/quota/alerts", GAP if miss else OK,
                   f"total={alerts['total']} 本页={len(items)}"
                   + (f" 缺字段={miss[0]}" if miss else ""))
        # 分页契约：total 为真实总数，limit 生效
        paged = self.admin.get("/api/quota/alerts", params={"limit": 1, "offset": 0}).json()
        ok_paged = (int(paged.get("total") or 0) == int(alerts.get("total") or 0)
                    and len(paged.get("items") or []) <= 1)
        record("/api/quota/alerts?limit=1", OK if ok_paged else GAP,
               f"total={paged.get('total')} 本页={len(paged.get('items') or [])}")

    def check_sub2api(self) -> None:
        groups = self.admin.get("/api/sub2api/groups").json()
        want_g = ("id", "name", "platform", "quota_limit", "quota_used", "enabled")
        if not isinstance(groups, list) or not groups:
            record("/api/sub2api/groups", EMPTY, "/quota 页分组 Tab 空白")
        else:
            miss = [missing_fields(g, want_g) for g in groups if missing_fields(g, want_g)]
            record("/api/sub2api/groups", GAP if miss else OK,
                   f"n={len(groups)}" + (f" 缺字段={miss[0]}" if miss else ""))

        accounts = self.admin.get("/api/sub2api/accounts").json()
        want_a = ("id", "group_id", "name", "weight", "enabled",
                  "quota_limit", "quota_used", "cooldown_until")
        if not isinstance(accounts, list) or not accounts:
            record("/api/sub2api/accounts", EMPTY, "/quota 页账号池 Tab 空白")
        else:
            miss = [missing_fields(a, want_a) for a in accounts if missing_fields(a, want_a)]
            record("/api/sub2api/accounts", GAP if miss else OK,
                   f"n={len(accounts)}" + (f" 缺字段={miss[0]}" if miss else ""))

        ledger = self.admin.get("/api/sub2api/ledger", params={"limit": 50}).json()
        want_l = ("ts", "group_id", "model", "prompt_tokens", "completion_tokens",
                  "cost", "status")
        if not isinstance(ledger, list) or not ledger:
            record("/api/sub2api/ledger", EMPTY, "/quota 页流水 Tab 空白")
        else:
            miss = [missing_fields(e, want_l) for e in ledger if missing_fields(e, want_l)]
            record("/api/sub2api/ledger", GAP if miss else OK,
                   f"n={len(ledger)}(上限50)" + (f" 缺字段={miss[0]}" if miss else ""))

    def check_accounts(self) -> None:
        body = self.admin.get("/api/accounts").json()
        want = ("total", "accounts", "pool_synced", "pool_available")
        miss = missing_fields(body, want)
        accounts = body.get("accounts") or []
        if miss:
            record("/api/accounts", GAP, f"缺字段={miss}")
        elif not accounts:
            record("/api/accounts", EMPTY, "账号页空列表（演示应播种 2 个账号）")
        else:
            item_want = ("file", "uid", "nickname", "enterprise_id", "provider",
                         "realm", "expires_at", "is_expired", "remain_seconds",
                         "credits", "note", "disabled")
            item_miss = [missing_fields(a, item_want) for a in accounts
                         if missing_fields(a, item_want)]
            record("/api/accounts", GAP if item_miss else OK,
                   f"total={body['total']} pool_synced={body.get('pool_synced')} "
                   f"pool_available={body.get('pool_available')}"
                   + (f" 行缺字段={item_miss[0]}" if item_miss else ""))

        caps = self.admin.get("/api/accounts/capabilities").json()
        providers = caps.get("providers") or []
        if not providers:
            record("/api/accounts/capabilities", EMPTY, "能力接口无 provider，操作按钮全空")
            return
        want_c = ("id", "label", "enabled", "readiness", "requires", "managed", "ops")
        miss = [missing_fields(p, want_c) for p in providers if missing_fields(p, want_c)]
        enabled = [p["id"] for p in providers if p.get("enabled")]
        record("/api/accounts/capabilities", GAP if miss else OK,
               f"providers={len(providers)} enabled={enabled}"
               + (f" 缺字段={miss[0]}" if miss else ""))

    def check_security(self) -> None:
        guard = self.admin.get("/api/security/guard").json()
        want = ("allow_private_upstreams", "blocked_hostnames", "blocked_addresses",
                "allowed_schemes")
        miss = missing_fields(guard, want)
        record("/api/security/guard", GAP if miss else OK,
               f"allow_private={guard.get('allow_private_upstreams')} "
               f"hostnames={len(guard.get('blocked_hostnames') or [])}"
               + (f" 缺字段={miss}" if miss else ""))

        lf = self.admin.get("/api/security/login-failures").json()
        want_l = ("scope", "window_sec", "max_failures", "active", "stats", "recent")
        miss = missing_fields(lf, want_l)
        recent = lf.get("recent") or []
        if miss:
            record("/api/security/login-failures", GAP, f"缺字段={miss}")
        elif not recent:
            record("/api/security/login-failures", EMPTY,
                   "recent=[]：安全页登录失败表空白（演示未播种失败记录）")
        else:
            record("/api/security/login-failures", OK,
                   f"stats={lf.get('stats')} recent={len(recent)}")

        audit = self.admin.get("/api/audit-logs", params={"limit": 50}).json()
        items = audit.get("items") or []
        want_a = ("ts", "actor", "action", "target", "detail", "ip")
        miss = [missing_fields(a, want_a) for a in items if missing_fields(a, want_a)]
        if not items:
            record("/api/audit-logs (+/api/security/audit)", EMPTY, "审计流空表")
        else:
            record("/api/audit-logs (+/api/security/audit)", GAP if miss else OK,
                   f"total={audit.get('total')}" + (f" 缺字段={miss[0]}" if miss else ""))
        alias = self.admin.get("/api/security/audit", params={"limit": 5}).json()
        record("/api/security/audit", OK if isinstance(alias.get("items"), list) else GAP,
               f"与 audit-logs 同实现 total={alias.get('total')}")

    def check_settings_and_system(self) -> None:
        up = self.admin.get("/api/settings/upstream").json()
        if not isinstance(up, dict) or not up:
            record("/api/settings/upstream", EMPTY, "上游配置页无对象可渲染")
        elif up.get("available") is False:
            record("/api/settings/upstream", GAP,
                   f"available=false error={str(up.get('error'))[:80]}："
                   "页面可显示原因，但 listen/api_key_masked 等内容区为空")
        else:
            record("/api/settings/upstream", OK,
                   f"keys={sorted(up)[:6]} available={up.get('available')}")

        gw = self.admin.get("/api/settings/gateway").json()
        want = ("allow_anonymous", "configured")
        miss = missing_fields(gw, want)
        record("/api/settings/gateway", GAP if miss else OK,
               f"allow_anonymous={gw.get('allow_anonymous')} configured={gw.get('configured')}"
               + (f" 缺字段={miss}" if miss else ""))

        mm = self.admin.get("/api/settings/model-map").json()
        if not isinstance(mm, dict):
            record("/api/settings/model-map", GAP, f"返回非对象: {type(mm).__name__}")
        elif not mm:
            record("/api/settings/model-map", EMPTY,
                   "映射表 {}：演示未播种映射，属配置类空态；"
                   "前端 settings/page.tsx:1817 有空态提示，不会报错但列表区无内容")
        else:
            record("/api/settings/model-map", OK, f"映射数={len(mm)}")

        rc = self.admin.get("/api/settings/runtime-config").json()
        if not isinstance(rc, dict) or not rc:
            record("/api/settings/runtime-config", EMPTY, "运行时配置页空对象")
        else:
            record("/api/settings/runtime-config", OK, f"keys={len(rc)}")

        cl = self.admin.get("/api/system/changelog").json()
        versions = cl.get("versions") or []
        if cl.get("available") is False:
            record("/api/system/changelog", GAP, f"available=false error={cl.get('error')}")
        elif not versions:
            record("/api/system/changelog", EMPTY, "versions=[]，更新日志页空白")
        else:
            record("/api/system/changelog", OK,
                   f"versions={len(versions)} current={cl.get('current')}")

        us = self.admin.get("/api/system/update-status").json()
        want = ("available", "current_version", "version")
        miss = missing_fields(us, want)
        record("/api/system/update-status", GAP if miss else OK,
               f"version={us.get('version')} reason={str(us.get('reason'))[:40]}"
               + (f" 缺字段={miss}" if miss else ""))

        metrics = self.admin.get("/api/metrics")
        try:
            body = metrics.json()
        except ValueError:
            body = None
        want_m = ("generated_at", "window_seconds", "requests", "providers",
                  "failure_rate_limited")
        if not isinstance(body, dict):
            record("/api/metrics", EMPTY, f"HTTP {metrics.status_code} 非 JSON 对象")
        else:
            miss = missing_fields(body, want_m)
            req = body.get("requests") or {}
            record("/api/metrics", GAP if miss else OK,
                   f"window={body.get('window_seconds')}s requests={req} "
                   f"providers={len(body.get('providers') or [])}"
                   + (f" 缺字段={miss}" if miss else ""))

    # ---------- viewer 侧隔离核对 ----------

    def check_viewer(self, viewer_key_id: int, demo_key_id: int | None) -> None:
        keys = self.guest.get("/api/keys").json()
        own_ids = {int(k["id"]) for k in keys}
        if not keys:
            record("viewer /api/keys", EMPTY,
                   "自己名下无 key：密钥页空表（数据未分配属正常；若已分配仍空才是过滤问题）")
        else:
            leak = [k["name"] for k in keys if k.get("owner_user_id") != self.viewer]
            record("viewer /api/keys", GAP if leak else OK,
                   f"n={len(keys)} 归属泄漏={leak}")

        tokens = self.guest.get("/api/tokens").json()
        if not tokens:
            record("viewer /api/tokens", EMPTY, "自己名下无令牌")
        else:
            leak = [t["name"] for t in tokens if int(t["id"]) not in own_ids]
            record("viewer /api/tokens", GAP if leak else OK,
                   f"n={len(tokens)} 泄漏={leak}")

        logs = self.guest.get("/api/logs", params={"page": 1, "size": 20}).json()
        total = int(logs.get("total") or 0)
        items = logs.get("items") or []
        foreign = [i["key_name"] for i in items if i.get("key_id") not in own_ids]
        demo_leak = [i["key_name"] for i in items
                     if str(i.get("key_name") or "").startswith("[demo]")]
        if total <= 0:
            record("viewer /api/logs", EMPTY,
                   "有自己 key 且已打过网关仍 total=0：归属过滤过严")
        else:
            record("viewer /api/logs", GAP if (foreign or demo_leak) else OK,
                   f"total={total} 他人行={foreign} demo行={demo_leak}")

        # 显式按他人 key 过滤应是结构化 403，而非空集合
        if demo_key_id is not None:
            r = self.guest.get("/api/logs", params={"key_id": demo_key_id})
            record("viewer /api/logs?key_id=demo", DENIED if r.status_code == 403 else GAP,
                   f"HTTP {r.status_code}（期望 403，不返回空集假装成功）")

        # 自己的拒绝日志要带 used/limit/unit 三件套
        errs = self.guest.get("/api/logs", params={"status": "error", "size": 50}).json()
        trio = None
        for it in errs.get("items") or []:
            parsed = parse_json_error(it.get("error"))
            if parsed and {"used", "limit", "unit"} <= set(parsed):
                trio = {k: parsed[k] for k in ("used", "limit", "unit")}
                break
        record("viewer 错误详情三件套", OK if trio else GAP,
               f"{trio}" if trio else "自己的错误行里没有 used/limit/unit JSON")

        admin_summary = self.admin.get("/api/stats/summary").json()
        summary = self.guest.get("/api/stats/summary").json()
        own_total = int(summary.get("total_requests") or 0)
        leak = (admin_summary.get("total_requests") == own_total
                and own_total > 0)
        if own_total <= 0:
            record("viewer /api/stats/summary", EMPTY, "有自己日志仍全 0：聚合过滤过严")
        else:
            record("viewer /api/stats/summary", GAP if leak else OK,
                   f"own total={own_total} admin total={admin_summary.get('total_requests')} "
                   f"today_credit={summary.get('today_credit')}")

        daily = self.guest.get("/api/stats/daily", params={"days": 7}).json()
        active = sum(int(d.get("requests") or 0) + int(d.get("failed") or 0) for d in daily)
        record("viewer /api/stats/daily", OK if active else EMPTY,
               f"7天有量={active}")

        policies = self.guest.get("/api/quota/policies").json()
        foreign_p = [p["id"] for p in policies if int(p.get("key_id") or 0) != viewer_key_id]
        if not policies:
            record("viewer /api/quota/policies", EMPTY,
                   "自己 key 已配策略仍空：归属过滤过严")
        else:
            record("viewer /api/quota/policies", GAP if foreign_p else OK,
                   f"n={len(policies)} 他人策略={foreign_p}")

        status = self.guest.get("/api/quota/status").json()
        items = status.get("items") or []
        foreign_s = [i.get("policy", {}).get("id") for i in items
                     if int(i.get("policy", {}).get("key_id") or -1) != viewer_key_id]
        exceeded = [i for i in items if i.get("exceeded")]
        if not items:
            record("viewer /api/quota/status", EMPTY, "有策略仍空")
        else:
            record("viewer /api/quota/status", GAP if foreign_s else OK,
                   f"items={len(items)} exceeded={len(exceeded)} 他人={foreign_s}")

        alerts = self.guest.get("/api/quota/alerts").json()
        foreign_a = [a["policy_id"] for a in (alerts.get("items") or [])
                     if int(a.get("key_id") or 0) != viewer_key_id]
        if not alerts.get("total"):
            record("viewer /api/quota/alerts", EMPTY,
                   "阈值已触发仍 total=0：归属过滤过严")
        else:
            record("viewer /api/quota/alerts", GAP if foreign_a else OK,
                   f"total={alerts['total']} 他人告警={foreign_a}")

        audit = self.guest.get("/api/audit-logs", params={"limit": 50}).json()
        foreign_rows = [a["actor"] for a in (audit.get("items") or [])
                        if a.get("actor") != self.viewer]
        if not audit.get("total"):
            record("viewer /api/audit-logs", EMPTY,
                   "自己登录过仍为空（登录审计应含自己）")
        else:
            record("viewer /api/audit-logs", GAP if foreign_rows else OK,
                   f"total={audit['total']} 他人actor={foreign_rows}")

        # 配额域只读闸门：非 admin 应 403，不是空数据
        for path in ("/api/sub2api/groups", "/api/sub2api/accounts", "/api/sub2api/ledger"):
            r = self.guest.get(path)
            tier = DENIED if r.status_code == 403 else (OK if r.status_code == 200 else GAP)
            record(f"viewer {path}", tier, f"HTTP {r.status_code}")

    # ---------- 索引核对 ----------

    def check_indexes(self) -> None:
        if not self.db_path:
            record("sqlite_master 索引", EMPTY, "未提供 --db/AUDIT_DB，无法核对")
            return
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            record("sqlite_master 索引", EMPTY, f"打开库失败: {exc}")
            return
        try:
            found = {row["name"]: row for row in conn.execute(
                "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index'")}
        finally:
            conn.close()
        # 今天新增的索引（未提交改动：db.py 里 idx_billing_ledger_log + perf_indexes.py 两枚）
        for name, table in (("idx_billing_ledger_log", "billing_ledger"),
                            ("idx_billing_ledger_scope", "billing_ledger"),
                            ("idx_request_logs_failure", "request_logs"),
                            ("idx_api_keys_owner", "api_keys"),
                            ("idx_request_logs_key_ts", "request_logs")):
            row = found.get(name)
            if row is None:
                record(f"索引 {name}", EMPTY, "启动后未落地")
            elif row["tbl_name"] != table:
                record(f"索引 {name}", GAP, f"挂在 {row['tbl_name']}，期望 {table}")
            else:
                sql = (row["sql"] or "").replace("\n", " ")
                partial = "WHERE" in sql.upper()
                record(f"索引 {name}", OK, f"ON {table}" + (" 部分索引" if partial else ""))

    # ---------- 汇总 ----------

    def run(self) -> int:
        self.login_admin()
        self.ensure_viewer()
        self.login_viewer()

        demo_key_id = next(
            (int(k["id"]) for k in self.admin.get("/api/keys").json()
             if str(k.get("name", "")).startswith("[demo]")), None)
        viewer_key_id, token = self.ensure_viewer_key()
        self.ensure_viewer_policy(viewer_key_id)
        # 复跑时 token 为空（明文只在创建时返回）：网关场景不补打，
        # 三件套核对仍依赖首跑落库的拒绝行，不影响判定。
        statuses = self.gateway_calls(token) if token else []
        self.probe_login_failure()

        self.check_status()
        self.check_models()
        self.check_keys_and_tokens()
        self.check_logs(demo_key_id)
        self.check_stats()
        self.check_quota()
        self.check_sub2api()
        self.check_accounts()
        self.check_security()
        self.check_settings_and_system()
        self.check_viewer(viewer_key_id, demo_key_id)
        self.check_indexes()
        return self.report(statuses)

    def report(self, gateway_statuses: list[int]) -> int:
        print("== 演示实例接口数据核对 ==")
        print(f"base={self.base} db={self.db_path or '(未提供)'} "
              f"网关场景状态码={gateway_statuses}")
        print()
        problems = [r for r in rows if r[1] in (GAP, EMPTY)]
        for tier in (EMPTY, GAP, DENIED, OK):
            group = [r for r in rows if r[1] == tier]
            if not group:
                continue
            print(f"-- {tier}（{len(group)}）--")
            for label, _, note in group:
                print(f"  {label} | {note}")
            print()
        print(f"结论：空白/缺字段接口 {len(problems)} 个")
        return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="演示实例页面数据三档核对")
    parser.add_argument("--base", default="")
    parser.add_argument("--admin-user", default="admin")
    parser.add_argument("--admin-password", default="")
    parser.add_argument("--viewer", default="audit_viewer")
    parser.add_argument("--viewer-password", default="AuditViewer-2026!")
    parser.add_argument("--db", default="")
    args = parser.parse_args()

    import os
    admin_password = (args.admin_password
                      or os.environ.get("AUDIT_ADMIN_PASSWORD", ""))
    if not admin_password:
        raise SystemExit("缺少管理员口令：--admin-password 或 AUDIT_ADMIN_PASSWORD")
    base = (args.base or os.environ.get("AUDIT_BASE_URL")
            or "http://127.0.0.1:8403")
    db_path = (args.db or os.environ.get("AUDIT_DB", "")) or None
    viewer_password = os.environ.get("AUDIT_VIEWER_PASSWORD", args.viewer_password)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    audit = Audit(base, args.admin_user, admin_password,
                  args.viewer, viewer_password, db_path)
    try:
        return audit.run()
    finally:
        audit.admin.close()
        audit.guest.close()


if __name__ == "__main__":
    raise SystemExit(main())
