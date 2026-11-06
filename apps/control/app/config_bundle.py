"""服务商配置 / 账号 / 凭据的打包导出与预检导入。

导出默认脱敏：不含任何明文凭据；只有显式 include_secrets 才带 secret 与 api_key，
并在文件头写警告。导入先全量预检（provider 存在、必填字段、重复身份），
有任一 fail 就整批拒绝；通过后在同一 SQLite 事务里写入，异常回滚。

身份键：provider=id，account=file，credential=(provider, label)
（label 为空时用 (provider, "sort#<sort_order>")，因为原库 id 换机器不可迁移）。
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Callable

from .db import get_json_setting
from .services import secret_store

FORMAT_ID = "unified-2api-config"
VERSION = 1
SENSITIVE_WARNING = "含明文密钥，勿提交、勿外传、勿放进任何共享目录"

PROVIDER_FIELDS = ("enabled", "base_url", "label")
ACCOUNT_FIELDS = (
    "provider", "uid", "nickname", "enterprise_id", "realm",
    "expires_at", "credits", "note", "disabled", "owner_user_id", "meta_json",
)
CREDENTIAL_FIELDS = ("label", "base_url", "enabled", "sort_order", "note")

CleanBase = Callable[[str, str], str]


class BundleRejected(Exception):
    """预检未通过：携带完整报告，调用方据此整批拒绝。"""

    def __init__(self, report: dict[str, Any]) -> None:
        super().__init__("预检未通过，整批拒绝")
        self.report = report


class BundleWriteFailed(Exception):
    """写入中途失败：事务已回滚，带失败位置与原因。"""

    def __init__(self, path: str, error: str) -> None:
        super().__init__(f"{path} 写入失败：{error}")
        self.path = path
        self.error = error


# ---------------------------------------------------------------- 导出


def _as_bool(value: Any) -> bool:
    return bool(int(value)) if isinstance(value, (int, float)) else bool(value)


def _spec_snapshot(adapter: Any) -> dict[str, Any]:
    spec = adapter.spec
    return {
        "name": spec.name,
        "status": spec.status,
        "readiness": spec.readiness,
        "requires": list(spec.requires),
        "default_base_url": spec.default_base_url,
    }


def build_bundle(
    conn: sqlite3.Connection,
    registry: Any | None = None,
    *,
    include_secrets: bool = False,
) -> dict[str, Any]:
    """把 providers / accounts / provider_credentials 打成单个字典。

    registry 为空时按库里的 providers 行导出（CLI 停服场景）；传了注册表则以
    适配器清单为准，保证 15 家一个都不漏（含 disabled）。
    """
    rows = {str(r["id"]): dict(r) for r in conn.execute("SELECT * FROM providers").fetchall()}
    raw_cfg = get_json_setting(conn, "provider_config", {})
    configs = raw_cfg if isinstance(raw_cfg, dict) else {}

    providers: list[dict[str, Any]] = []
    if registry is not None:
        adapters = list(registry._adapters.values())
        # 适配器清单 ∪ 库里已存在的 provider：任何一家都不因为「没注册适配器」被漏掉
        ordered = [a.spec.id for a in adapters]
        ids = ordered + sorted(set(rows) - set(ordered))
    else:
        adapters = []
        ids = sorted(rows)

    for pid in ids:
        row = rows.get(pid, {})
        cfg = configs.get(pid) if isinstance(configs.get(pid), dict) else {}
        adapter = next((a for a in adapters if a.spec.id == pid), None)
        options = cfg.get("options") if isinstance(cfg.get("options"), dict) else {}
        item: dict[str, Any] = {
            "id": pid,
            "enabled": _as_bool(row.get("enabled", 0)),
            "base_url": str(row.get("base_url") or ""),
            "label": str(row.get("label") or (adapter.spec.name if adapter else pid)),
            "options": dict(options),
        }
        if adapter is not None:
            item["spec"] = _spec_snapshot(adapter)
        if include_secrets and str(cfg.get("api_key") or ""):
            item["api_key"] = secret_store.read_secret(cfg["api_key"])[0]
        providers.append(item)

    accounts: list[dict[str, Any]] = []
    for r in conn.execute("SELECT * FROM accounts ORDER BY provider, file").fetchall():
        meta = str(r["meta_json"] or "{}")
        item = {
            "file": str(r["file"]),
            "provider": str(r["provider"]),
            "uid": str(r["uid"]),
            "nickname": str(r["nickname"] or ""),
            "enterprise_id": str(r["enterprise_id"] or ""),
            "realm": str(r["realm"] or "cn"),
            "expires_at": int(r["expires_at"] or 0),
            "credits": None if r["credits"] is None else float(r["credits"]),
            "note": str(r["note"] or ""),
            "disabled": _as_bool(r["disabled"]),
            "owner_user_id": r["owner_user_id"],
            # meta_json 可能藏会话令牌，按敏感数据处理：默认只给 has_meta
            "has_meta": bool(meta.strip() and meta.strip() != "{}"),
        }
        if include_secrets:
            item["meta_json"] = meta
        accounts.append(item)

    credentials: list[dict[str, Any]] = []
    for r in conn.execute(
        "SELECT * FROM provider_credentials ORDER BY provider, sort_order, id"
    ).fetchall():
        secret = str(r["secret"] or "")
        item = {
            "provider": str(r["provider"]),
            "label": str(r["label"] or ""),
            "base_url": str(r["base_url"] or ""),
            "enabled": _as_bool(r["enabled"]),
            "sort_order": int(r["sort_order"] or 0),
            "note": str(r["note"] or ""),
            "has_secret": bool(secret.strip()),
        }
        if include_secrets:
            item["secret"] = secret_store.read_secret(secret)[0]
        credentials.append(item)

    return {
        "format": FORMAT_ID,
        "version": VERSION,
        "contains_secrets": bool(include_secrets),
        "warning": SENSITIVE_WARNING if include_secrets else "",
        "exported_at": int(time.time()),
        "providers": providers,
        "accounts": accounts,
        "credentials": credentials,
    }


# ---------------------------------------------------------------- 预检


def _issue(path: str, index: int, column: str, level: str, reason: str) -> dict[str, Any]:
    return {"path": path, "index": index, "column": column, "level": level, "reason": reason}


def credential_key(item: dict[str, Any]) -> tuple[str, str]:
    provider = str(item.get("provider") or "")
    label = str(item.get("label") or "").strip()
    return (provider, label or f"sort#{int(item.get('sort_order') or 0)}")


def validate_bundle(
    doc: Any,
    *,
    known_ids: set[str],
    clean_base: CleanBase | None = None,
) -> list[dict[str, Any]]:
    """结构与语义预检。返回逐条问题；level=fail 表示整批必须拒绝。"""
    issues: list[dict[str, Any]] = []
    if not isinstance(doc, dict):
        return [_issue("$", 0, "-", "fail", "文件根节点必须是对象")]
    if doc.get("format") != FORMAT_ID:
        issues.append(_issue("$.format", 0, "format", "fail", f"必须是 {FORMAT_ID}"))
    if doc.get("version") != VERSION:
        issues.append(_issue("$.version", 0, "version", "fail", f"仅支持 version={VERSION}"))

    for name in ("providers", "accounts", "credentials"):
        if not isinstance(doc.get(name), list):
            issues.append(_issue(f"$.{name}", 0, name, "fail", "必须是数组"))

    providers = doc.get("providers") if isinstance(doc.get("providers"), list) else []
    seen_ids: set[str] = set()
    for i, item in enumerate(providers, start=1):
        path = f"providers[{i}]"
        if not isinstance(item, dict):
            issues.append(_issue(path, i, "-", "fail", "条目必须是对象"))
            continue
        pid = str(item.get("id") or "")
        if not pid:
            issues.append(_issue(f"{path}.id", i, "id", "fail", "id 不能为空"))
        elif pid not in known_ids:
            issues.append(_issue(f"{path}.id", i, "id", "fail", f"未知 provider：{pid}"))
        elif pid in seen_ids:
            issues.append(_issue(f"{path}.id", i, "id", "fail", f"重复 provider：{pid}"))
        else:
            seen_ids.add(pid)
        if not isinstance(item.get("enabled"), bool):
            issues.append(_issue(f"{path}.enabled", i, "enabled", "fail", "必须是布尔值"))
        base_url = item.get("base_url", "")
        if not isinstance(base_url, str):
            issues.append(_issue(f"{path}.base_url", i, "base_url", "fail", "必须是字符串"))
        elif base_url and clean_base is not None:
            try:
                clean_base(pid, base_url)
            except Exception as exc:  # ProviderError 等统一转成预检问题
                issues.append(_issue(f"{path}.base_url", i, "base_url", "fail", str(exc)))
        if "options" in item and not isinstance(item["options"], dict):
            issues.append(_issue(f"{path}.options", i, "options", "fail", "必须是对象"))

    accounts = doc.get("accounts") if isinstance(doc.get("accounts"), list) else []
    seen_files: set[str] = set()
    for i, item in enumerate(accounts, start=1):
        path = f"accounts[{i}]"
        if not isinstance(item, dict):
            issues.append(_issue(path, i, "-", "fail", "条目必须是对象"))
            continue
        for column in ("file", "provider", "uid"):
            if not str(item.get(column) or "").strip():
                issues.append(_issue(f"{path}.{column}", i, column, "fail", f"{column} 不能为空"))
        pid = str(item.get("provider") or "")
        if pid and pid not in known_ids:
            issues.append(_issue(f"{path}.provider", i, "provider", "fail", f"未知 provider：{pid}"))
        file = str(item.get("file") or "")
        if file and file in seen_files:
            issues.append(_issue(f"{path}.file", i, "file", "fail", f"重复账号：{file}"))
        if file:
            seen_files.add(file)
        expires_at = item.get("expires_at", 0)
        if not isinstance(expires_at, int) or isinstance(expires_at, bool):
            issues.append(_issue(f"{path}.expires_at", i, "expires_at", "fail", "必须是整数"))
        credits = item.get("credits")
        if credits is not None and (isinstance(credits, bool) or not isinstance(credits, (int, float))):
            issues.append(_issue(f"{path}.credits", i, "credits", "fail", "必须是数字或 null"))
        if not isinstance(item.get("disabled", False), bool):
            issues.append(_issue(f"{path}.disabled", i, "disabled", "fail", "必须是布尔值"))
        meta = item.get("meta_json")
        if meta is not None:
            if not isinstance(meta, str):
                issues.append(_issue(f"{path}.meta_json", i, "meta_json", "fail", "必须是 JSON 字符串"))
            else:
                try:
                    json.loads(meta or "{}")
                except json.JSONDecodeError:
                    issues.append(_issue(f"{path}.meta_json", i, "meta_json", "fail", "不是合法 JSON"))

    credentials = doc.get("credentials") if isinstance(doc.get("credentials"), list) else []
    seen_creds: set[tuple[str, str]] = set()
    for i, item in enumerate(credentials, start=1):
        path = f"credentials[{i}]"
        if not isinstance(item, dict):
            issues.append(_issue(path, i, "-", "fail", "条目必须是对象"))
            continue
        pid = str(item.get("provider") or "")
        if not pid:
            issues.append(_issue(f"{path}.provider", i, "provider", "fail", "provider 不能为空"))
        elif pid not in known_ids:
            issues.append(_issue(f"{path}.provider", i, "provider", "fail", f"未知 provider：{pid}"))
        key = credential_key(item)
        if key in seen_creds:
            issues.append(_issue(f"{path}.label", i, "label", "fail", f"重复凭据身份：{key[0]} / {key[1]}"))
        seen_creds.add(key)
        if not isinstance(item.get("sort_order", 0), int) or isinstance(item.get("sort_order", 0), bool):
            issues.append(_issue(f"{path}.sort_order", i, "sort_order", "fail", "必须是整数"))
        if not isinstance(item.get("enabled", True), bool):
            issues.append(_issue(f"{path}.enabled", i, "enabled", "fail", "必须是布尔值"))
        label = item.get("label", "")
        if not isinstance(label, str):
            issues.append(_issue(f"{path}.label", i, "label", "fail", "必须是字符串"))
        elif len(label) > 64:
            issues.append(_issue(f"{path}.label", i, "label", "fail", "超过 64 字符"))
        if "secret" in item:
            secret = item["secret"]
            if not isinstance(secret, str):
                issues.append(_issue(f"{path}.secret", i, "secret", "fail", "必须是字符串"))
            elif secret == "***":
                issues.append(_issue(f"{path}.secret", i, "secret", "fail", "不能是掩码占位 ***"))
            elif len(secret) > 4096:
                issues.append(_issue(f"{path}.secret", i, "secret", "fail", "超过 4096 字符"))
        base_url = item.get("base_url", "")
        if not isinstance(base_url, str):
            issues.append(_issue(f"{path}.base_url", i, "base_url", "fail", "必须是字符串"))
        elif base_url and clean_base is not None:
            try:
                clean_base(pid, base_url)
            except Exception as exc:
                issues.append(_issue(f"{path}.base_url", i, "base_url", "fail", str(exc)))

    return issues


# ---------------------------------------------------------------- 计划


def _norm(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return float(value)
    if value is None:
        return None
    return str(value)


def _diff_fields(current: dict[str, Any], item: dict[str, Any], fields: tuple[str, ...]) -> list[str]:
    return [f for f in fields if _norm(current.get(f)) != _norm(item.get(f))]


def _entry(path: str, key: str, action: str, reason: str, diff: list[str] | None = None) -> dict[str, Any]:
    return {"path": path, "key": key, "action": action, "reason": reason, "diff": diff or []}


def plan_import(
    doc: Any,
    *,
    conn: sqlite3.Connection,
    mode: str,
    known_ids: set[str],
    clean_base: CleanBase | None = None,
    dry_run_as: str = "merge",
) -> dict[str, Any]:
    """给出现状与文件的差异：create / overwrite / skip / fail 四类逐条。

    dry-run 只是「不写库」，仍要按一种语义来分类；用 dry_run_as 指定 merge/overwrite。
    """
    if mode not in ("merge", "overwrite", "dry-run"):
        raise ValueError(f"未知 mode：{mode}")
    if dry_run_as not in ("merge", "overwrite"):
        raise ValueError(f"未知 dry_run_as：{dry_run_as}")
    semantics = dry_run_as if mode == "dry-run" else mode
    issues = validate_bundle(doc, known_ids=known_ids, clean_base=clean_base)
    doc_providers = doc.get("providers") if isinstance(doc, dict) and isinstance(doc.get("providers"), list) else []
    doc_accounts = doc.get("accounts") if isinstance(doc, dict) and isinstance(doc.get("accounts"), list) else []
    doc_creds = doc.get("credentials") if isinstance(doc, dict) and isinstance(doc.get("credentials"), list) else []

    failed_paths = {issue["path"].split(".")[0] for issue in issues if issue["level"] == "fail"}
    report: dict[str, Any] = {
        "mode": mode,
        "semantics": semantics,
        "create": [], "overwrite": [], "skip": [], "fail": [],
        "issues": issues, "totals": {},
    }

    rows = {str(r["id"]): dict(r) for r in conn.execute("SELECT * FROM providers").fetchall()}
    raw_cfg = get_json_setting(conn, "provider_config", {})
    configs = raw_cfg if isinstance(raw_cfg, dict) else {}
    for i, item in enumerate(doc_providers, start=1):
        path = f"providers[{i}]"
        pid = str(item.get("id") or "")
        if path in failed_paths:
            report["fail"].append(_entry(path, pid, "fail", "预检未通过，见 issues"))
            continue
        current = rows.get(pid)
        if current is None:
            report["create"].append(_entry(path, pid, "create", "库里没有该 provider"))
            continue
        old_opts = (configs.get(pid) or {}).get("options") if isinstance(configs.get(pid), dict) else {}
        diff = _diff_fields(current, item, PROVIDER_FIELDS)
        if dict(old_opts or {}) != dict(item.get("options") or {}):
            diff.append("options")
        # provider 是部署级单例配置，两种模式都以文件为准；否则「改一项配置」永远还原不了。
        # merge 与 overwrite 的差别落在账号/凭据（同名是否保留）。
        if diff:
            report["overwrite"].append(_entry(path, pid, "overwrite", "以文件为准", diff))
        else:
            report["skip"].append(_entry(path, pid, "skip", "内容一致，无需写入"))

    acc_rows = {str(r["file"]): dict(r) for r in conn.execute("SELECT * FROM accounts").fetchall()}
    for i, item in enumerate(doc_accounts, start=1):
        path = f"accounts[{i}]"
        file = str(item.get("file") or "")
        if path in failed_paths:
            report["fail"].append(_entry(path, file, "fail", "预检未通过，见 issues"))
            continue
        current = acc_rows.get(file)
        if current is None:
            report["create"].append(_entry(path, file, "create", "库里没有该账号"))
            continue
        # 默认导出不带 meta_json：不把它算进差异，否则每条都会显示「覆盖」
        fields = tuple(f for f in ACCOUNT_FIELDS if f != "meta_json" or "meta_json" in item)
        diff = _diff_fields(current, item, fields)
        if semantics == "merge":
            report["skip"].append(_entry(path, file, "skip", "merge：同名保留库里的记录", diff))
        elif diff:
            report["overwrite"].append(_entry(path, file, "overwrite", "overwrite：以文件为准", diff))
        else:
            report["skip"].append(_entry(path, file, "skip", "内容一致，无需写入"))

    cred_rows = [dict(r) for r in conn.execute("SELECT * FROM provider_credentials ORDER BY id").fetchall()]
    cred_map = {credential_key(r): r for r in cred_rows}
    for i, item in enumerate(doc_creds, start=1):
        path = f"credentials[{i}]"
        key = credential_key(item)
        if path in failed_paths:
            report["fail"].append(_entry(path, f"{key[0]}/{key[1]}", "fail", "预检未通过，见 issues"))
            continue
        current = cred_map.get(key)
        if current is None:
            report["create"].append(_entry(path, f"{key[0]}/{key[1]}", "create", "库里没有该凭据"))
            continue
        diff = _diff_fields(current, item, CREDENTIAL_FIELDS)
        if "secret" in item and secret_store.read_secret(current.get("secret"))[0] != str(item["secret"]):
            diff.append("secret")
        if semantics == "merge":
            report["skip"].append(_entry(path, f"{key[0]}/{key[1]}", "skip", "merge：同名保留库里的记录", diff))
        elif diff:
            report["overwrite"].append(_entry(path, f"{key[0]}/{key[1]}", "overwrite", "overwrite：以文件为准", diff))
        else:
            report["skip"].append(_entry(path, f"{key[0]}/{key[1]}", "skip", "内容一致，无需写入"))

    report["totals"] = {
        "create": len(report["create"]),
        "overwrite": len(report["overwrite"]),
        "skip": len(report["skip"]),
        "fail": len(report["fail"]),
        "issues": len(issues),
    }
    return report


# ---------------------------------------------------------------- 落库


def _save_provider_config(conn: sqlite3.Connection, configs: dict[str, Any]) -> None:
    # 不走 set_json_setting：那个函数自带 commit，会破坏本函数所在的单事务。
    from .db import put_settings

    put_settings(
        conn,
        {"provider_config": json.dumps(secret_store.seal_provider_config(configs), ensure_ascii=False)},
    )


def apply_import(
    doc: Any,
    *,
    conn: sqlite3.Connection,
    mode: str,
    known_ids: set[str],
    clean_base: CleanBase | None = None,
    registry: Any | None = None,
    dry_run_as: str = "merge",
) -> dict[str, Any]:
    """预检 → 单事务写入。任何一步失败都回滚，库里不会留半套数据。"""
    if mode == "dry-run":
        report = plan_import(
            doc, conn=conn, mode=mode, known_ids=known_ids, clean_base=clean_base,
            dry_run_as=dry_run_as,
        )
        report["ok"] = not any(issue["level"] == "fail" for issue in report["issues"])
        report["written"] = 0
        return report

    report = plan_import(doc, conn=conn, mode=mode, known_ids=known_ids, clean_base=clean_base)
    # 顶层坏字段（如 format/version）不会落到某个条目，必须按 issues 判定整批拒绝
    if any(issue["level"] == "fail" for issue in report["issues"]):
        raise BundleRejected(report)

    providers = doc.get("providers") or []
    accounts = doc.get("accounts") or []
    credentials = doc.get("credentials") or []
    now_ts = int(time.time())
    touched: set[str] = set()
    expected = {
        "providers": [str(e["key"]) for e in report["create"] + report["overwrite"] if e["path"].startswith("providers[")],
        "accounts": [str(e["key"]) for e in report["create"] + report["overwrite"] if e["path"].startswith("accounts[")],
        "credentials": [str(e["key"]).split("/", 1)[0] for e in report["create"] + report["overwrite"] if e["path"].startswith("credentials[")],
    }

    conn.execute("BEGIN IMMEDIATE")
    try:
        raw_cfg = get_json_setting(conn, "provider_config", {})
        configs = raw_cfg if isinstance(raw_cfg, dict) else {}
        for i, item in enumerate(providers, start=1):
            path = f"providers[{i}]"
            pid = str(item["id"])
            action = "create" if path in {e["path"] for e in report["create"]} else (
                "overwrite" if path in {e["path"] for e in report["overwrite"]} else ""
            )
            if not action:
                continue
            try:
                if action == "create":
                    conn.execute(
                        "INSERT INTO providers(id, enabled, base_url, label, updated_at) VALUES(?,?,?,?,?)",
                        (pid, 1 if item.get("enabled") else 0, str(item.get("base_url") or ""),
                         str(item.get("label") or "")[:64], now_ts),
                    )
                else:
                    conn.execute(
                        "UPDATE providers SET enabled = ?, base_url = ?, label = ?, updated_at = ? WHERE id = ?",
                        (1 if item.get("enabled") else 0, str(item.get("base_url") or ""),
                         str(item.get("label") or "")[:64], now_ts, pid),
                    )
                if "options" in item:
                    cfg = configs.get(pid) if isinstance(configs.get(pid), dict) else {}
                    old = cfg.get("options") if isinstance(cfg.get("options"), dict) else {}
                    cfg["options"] = {**old, **(item.get("options") or {})}
                    if "api_key" in item:
                        cfg["api_key"] = secret_store.seal(str(item["api_key"]))
                    configs[pid] = cfg
                touched.add(pid)
            except sqlite3.Error as exc:
                raise BundleWriteFailed(path, str(exc)) from exc

        for i, item in enumerate(accounts, start=1):
            path = f"accounts[{i}]"
            file = str(item["file"])
            action = "create" if path in {e["path"] for e in report["create"]} else (
                "overwrite" if path in {e["path"] for e in report["overwrite"]} else ""
            )
            if not action:
                continue
            provider = str(item.get("provider") or "")
            uid = str(item.get("uid") or "")
            nickname = str(item.get("nickname") or "")
            enterprise = str(item.get("enterprise_id") or "")
            realm = str(item.get("realm") or "cn")
            expires = int(item.get("expires_at") or 0)
            credits = None if item.get("credits") is None else float(item["credits"])
            note = str(item.get("note") or "")
            disabled = 1 if item.get("disabled") else 0
            owner = item.get("owner_user_id")
            meta = str(item.get("meta_json") or "{}")
            try:
                if action == "create":
                    conn.execute(
                        "INSERT INTO accounts(file, provider, uid, nickname, enterprise_id, realm, expires_at, "
                        "credits, note, disabled, meta_json, owner_user_id, created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (file, provider, uid, nickname, enterprise, realm, expires, credits, note,
                         disabled, meta, owner, now_ts),
                    )
                elif "meta_json" in item:
                    conn.execute(
                        "UPDATE accounts SET provider = ?, uid = ?, nickname = ?, enterprise_id = ?, realm = ?, "
                        "expires_at = ?, credits = ?, note = ?, disabled = ?, owner_user_id = ?, meta_json = ? "
                        "WHERE file = ?",
                        (provider, uid, nickname, enterprise, realm, expires, credits, note,
                         disabled, owner, meta, file),
                    )
                else:
                    # 默认导出没有 meta_json：覆盖其余字段，保留库里原有 meta（可能含令牌）
                    conn.execute(
                        "UPDATE accounts SET provider = ?, uid = ?, nickname = ?, enterprise_id = ?, realm = ?, "
                        "expires_at = ?, credits = ?, note = ?, disabled = ?, owner_user_id = ? "
                        "WHERE file = ?",
                        (provider, uid, nickname, enterprise, realm, expires, credits, note,
                         disabled, owner, file),
                    )
                touched.add(str(item.get("provider") or ""))
            except sqlite3.Error as exc:
                raise BundleWriteFailed(path, str(exc)) from exc

        for i, item in enumerate(credentials, start=1):
            path = f"credentials[{i}]"
            key = credential_key(item)
            action = "create" if path in {e["path"] for e in report["create"]} else (
                "overwrite" if path in {e["path"] for e in report["overwrite"]} else ""
            )
            if not action:
                continue
            provider = str(item.get("provider") or "")
            label = str(item.get("label") or "")[:64]
            base = str(item.get("base_url") or "")
            try:
                if action == "create":
                    secret = secret_store.seal(str(item.get("secret") or ""))
                    conn.execute(
                        "INSERT INTO provider_credentials(provider, label, base_url, secret, enabled, "
                        "sort_order, note, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (provider, label, base, secret, 1 if item.get("enabled", True) else 0,
                         int(item.get("sort_order") or 0), str(item.get("note") or "")[:200], now_ts, now_ts),
                    )
                else:
                    if key[1].startswith("sort#"):
                        current = conn.execute(
                            "SELECT * FROM provider_credentials WHERE provider = ? AND label = '' "
                            "AND sort_order = ? ORDER BY id LIMIT 1",
                            (key[0], int(item.get("sort_order") or 0)),
                        ).fetchone()
                    else:
                        current = conn.execute(
                            "SELECT * FROM provider_credentials WHERE provider = ? AND label = ? "
                            "ORDER BY id LIMIT 1", (key[0], key[1]),
                        ).fetchone()
                    cid = int(current["id"])
                    conn.execute(
                        "UPDATE provider_credentials SET label = ?, base_url = ?, enabled = ?, sort_order = ?, "
                        "note = ?, updated_at = ? WHERE id = ?",
                        (label, base, 1 if item.get("enabled", True) else 0,
                         int(item.get("sort_order") or 0), str(item.get("note") or "")[:200], now_ts, cid),
                    )
                    # 文件没带 secret（默认导出）时保留库里的密钥，不清空
                    if "secret" in item:
                        conn.execute(
                            "UPDATE provider_credentials SET secret = ? WHERE id = ?",
                            (secret_store.seal(str(item["secret"])), cid),
                        )
                touched.add(provider)
            except (sqlite3.Error, TypeError) as exc:
                raise BundleWriteFailed(path, str(exc)) from exc

        _save_provider_config(conn, configs)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    if registry is not None:
        for pid in sorted(touched):
            if not pid:
                continue
            try:
                registry.get(pid)
            except Exception:
                pass  # 已提交的事实不改写；刷新失败只影响进程内缓存

    report["ok"] = True
    report["written"] = len(report["create"]) + len(report["overwrite"])
    report["expected_keys"] = expected
    return report
