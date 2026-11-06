"""sub2api 配额域持久化：分组、账号池、扣减流水。

表结构见 db.py 的 sub2_groups / sub2_accounts / sub2_ledger；语义见
docs/providers/sub2api.md。扣减单位统一为 token 数。
"""

import sqlite3
import time
from typing import Any

from ..billing import totals
from ..db import Database
from ..net_guard import UpstreamAddressError, guard_upstream_url
from .base import ProviderError

DEFAULT_COOLDOWN_SEC = 60


def _group_public(row: sqlite3.Row) -> dict[str, Any]:
    limit = float(row["quota_limit"] or 0)
    used = float(row["quota_used"] or 0)
    return {
        "id": row["id"],
        "name": row["name"],
        "platform": row["platform"],
        "quota_limit": limit,
        "quota_used": used,
        "quota_remaining": max(limit - used, 0) if limit > 0 else None,
        "reset_at": row["reset_at"],
        "enabled": bool(row["enabled"]),
        "accounts": int(row["accounts"] or 0) if "accounts" in row.keys() else 0,
        "active_accounts": int(row["active_accounts"] or 0) if "active_accounts" in row.keys() else 0,
    }


def _account_public(row: sqlite3.Row) -> dict[str, Any]:
    limit = float(row["quota_limit"] or 0)
    used = float(row["quota_used"] or 0)
    return {
        "id": row["id"],
        "group_id": row["group_id"],
        "name": row["name"],
        "upstream_base": row["upstream_base"],
        "weight": int(row["weight"] or 1),
        "quota_limit": limit,
        "quota_used": used,
        "quota_remaining": max(limit - used, 0) if limit > 0 else None,
        "enabled": bool(row["enabled"]),
        "cooldown_until": row["cooldown_until"],
        "last_used_at": row["last_used_at"],
        "has_api_key": bool(row["api_key"]),
    }


class QuotaStore:
    def __init__(self, db_path, *, allow_private_upstreams: bool = False) -> None:
        self._db = Database(db_path)
        self._allow_private = allow_private_upstreams

    def _check_base(self, value: str) -> str:
        """与 provider.base_url 共用一套地址守卫，避免 sub2 账号绕过（S-04）。"""
        try:
            return guard_upstream_url(value, allow_private=self._allow_private, field="upstream_base")
        except UpstreamAddressError as exc:
            raise ProviderError(400, str(exc)) from exc

    # ---- 分组 ----

    def list_groups(self) -> list[dict[str, Any]]:
        conn = self._db.connect()
        try:
            rows = conn.execute(
                "SELECT g.*, "
                "(SELECT COUNT(*) FROM sub2_accounts a WHERE a.group_id = g.id) AS accounts, "
                "(SELECT COUNT(*) FROM sub2_accounts a WHERE a.group_id = g.id AND a.enabled = 1 "
                " AND (a.quota_limit = 0 OR a.quota_used < a.quota_limit) "
                " AND a.cooldown_until <= ?) AS active_accounts "
                "FROM sub2_groups g ORDER BY g.created_at",
                (int(time.time()),),
            ).fetchall()
            return [_group_public(row) for row in rows]
        finally:
            conn.close()

    def get_group(self, group_id: str) -> dict[str, Any] | None:
        conn = self._db.connect()
        try:
            row = conn.execute("SELECT * FROM sub2_groups WHERE id = ?", (group_id,)).fetchone()
            return _group_public(row) if row else None
        finally:
            conn.close()

    def upsert_group(self, group_id: str, *, name: str = "", platform: str = "openai", quota_limit: float = 0) -> dict[str, Any]:
        now = int(time.time())
        conn = self._db.connect()
        try:
            conn.execute(
                "INSERT INTO sub2_groups(id, name, platform, quota_limit, quota_used, reset_at, enabled, created_at, updated_at) "
                "VALUES(?,?,?,?,0,0,1,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, platform=excluded.platform, "
                "quota_limit=excluded.quota_limit, updated_at=excluded.updated_at",
                (group_id.strip(), name.strip() or group_id.strip(), platform.strip() or "openai", max(quota_limit, 0), now, now),
            )
            conn.commit()
        finally:
            conn.close()
        found = self.get_group(group_id)
        assert found is not None
        return found

    def update_group(
        self,
        group_id: str,
        *,
        name: str | None = None,
        platform: str | None = None,
        quota_limit: float | None = None,
        enabled: bool | None = None,
        reset_quota: bool = False,
    ) -> dict[str, Any]:
        if not self.get_group(group_id):
            raise ProviderError(404, f"未知的配额分组：{group_id}")
        sets: list[str] = []
        values: list[Any] = []
        if name is not None:
            sets.append("name = ?")
            values.append(name.strip() or group_id)
        if platform is not None:
            sets.append("platform = ?")
            values.append(platform.strip() or "openai")
        if quota_limit is not None:
            sets.append("quota_limit = ?")
            values.append(max(quota_limit, 0))
        if enabled is not None:
            sets.append("enabled = ?")
            values.append(1 if enabled else 0)
        if reset_quota:
            sets.append("quota_used = 0")
            sets.append("reset_at = ?")
            values.append(int(time.time()))
        if sets:
            sets.append("updated_at = ?")
            values.append(int(time.time()))
            values.append(group_id)
            conn = self._db.connect()
            try:
                conn.execute(f"UPDATE sub2_groups SET {', '.join(sets)} WHERE id = ?", values)
                conn.commit()
            finally:
                conn.close()
        found = self.get_group(group_id)
        assert found is not None
        return found

    def delete_group(self, group_id: str) -> None:
        conn = self._db.connect()
        try:
            conn.execute("DELETE FROM sub2_accounts WHERE group_id = ?", (group_id,))
            conn.execute("DELETE FROM sub2_groups WHERE id = ?", (group_id,))
            conn.commit()
        finally:
            conn.close()

    # ---- 账号池 ----

    def list_accounts(self, group_id: str | None = None) -> list[dict[str, Any]]:
        conn = self._db.connect()
        try:
            if group_id:
                rows = conn.execute(
                    "SELECT * FROM sub2_accounts WHERE group_id = ? ORDER BY id", (group_id,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM sub2_accounts ORDER BY group_id, id").fetchall()
            return [_account_public(row) for row in rows]
        finally:
            conn.close()

    def add_account(
        self,
        group_id: str,
        *,
        name: str = "",
        upstream_base: str = "",
        api_key: str = "",
        weight: int = 1,
        quota_limit: float = 0,
    ) -> dict[str, Any]:
        if not self.get_group(group_id):
            raise ProviderError(404, f"未知的配额分组：{group_id}")
        conn = self._db.connect()
        try:
            cursor = conn.execute(
                "INSERT INTO sub2_accounts(group_id, name, upstream_base, api_key, weight, quota_limit, "
                "quota_used, enabled, cooldown_until, last_used_at, created_at) "
                "VALUES(?,?,?,?,?,?,0,1,0,0,?)",
                (
                    group_id,
                    name.strip(),
                    self._check_base(upstream_base.strip()),
                    api_key.strip(),
                    max(int(weight), 1),
                    max(quota_limit, 0),
                    int(time.time()),
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM sub2_accounts WHERE id = ?", (cursor.lastrowid,)).fetchone()
            return _account_public(row)
        finally:
            conn.close()

    def update_account(
        self,
        account_id: int,
        *,
        name: str | None = None,
        upstream_base: str | None = None,
        api_key: str | None = None,
        weight: int | None = None,
        quota_limit: float | None = None,
        enabled: bool | None = None,
        clear_cooldown: bool = False,
    ) -> dict[str, Any]:
        sets: list[str] = []
        values: list[Any] = []
        if name is not None:
            sets.append("name = ?")
            values.append(name.strip())
        if upstream_base is not None:
            sets.append("upstream_base = ?")
            values.append(self._check_base(upstream_base.strip()))
        if api_key is not None:
            sets.append("api_key = ?")
            values.append(api_key.strip())
        if weight is not None:
            sets.append("weight = ?")
            values.append(max(int(weight), 1))
        if quota_limit is not None:
            sets.append("quota_limit = ?")
            values.append(max(quota_limit, 0))
        if enabled is not None:
            sets.append("enabled = ?")
            values.append(1 if enabled else 0)
        if clear_cooldown:
            sets.append("cooldown_until = 0")
        if not sets:
            raise ProviderError(400, "没有需要更新的字段")
        conn = self._db.connect()
        try:
            conn.execute(f"UPDATE sub2_accounts SET {', '.join(sets)} WHERE id = ?", (*values, account_id))
            conn.commit()
            row = conn.execute("SELECT * FROM sub2_accounts WHERE id = ?", (account_id,)).fetchone()
            if row is None:
                raise ProviderError(404, f"未知的账号：{account_id}")
            return _account_public(row)
        finally:
            conn.close()

    def delete_account(self, account_id: int) -> None:
        conn = self._db.connect()
        try:
            cursor = conn.execute("DELETE FROM sub2_accounts WHERE id = ?", (account_id,))
            conn.commit()
            if cursor.rowcount == 0:
                raise ProviderError(404, f"未知的账号：{account_id}")
        finally:
            conn.close()

    def reset_account_quota(self, account_id: int) -> dict[str, Any]:
        conn = self._db.connect()
        try:
            cursor = conn.execute(
                "UPDATE sub2_accounts SET quota_used = 0, cooldown_until = 0 WHERE id = ?", (account_id,)
            )
            conn.commit()
            if cursor.rowcount == 0:
                raise ProviderError(404, f"未知的账号：{account_id}")
        finally:
            conn.close()
        for item in self.list_accounts():
            if item["id"] == account_id:
                return item
        raise ProviderError(404, f"未知的账号：{account_id}")

    # ---- 分发与扣减 ----

    def require_group(self, group_id: str) -> dict[str, Any]:
        group = self.get_group(group_id)
        if group is None:
            raise ProviderError(
                503,
                f"sub2api 缺少可用的配额分组「{group_id}」：请打开「配额」页 → 新建分组 → "
                "在分组下添加账号（填上游地址与 api_key），保存后重试。"
                "（技术细节：也可直接 POST /api/sub2api/groups 与 /api/sub2api/accounts）",
            )
        if not group["enabled"]:
            raise ProviderError(403, f"sub2api 分组已停用：{group_id}")
        if group["quota_limit"] > 0 and group["quota_used"] >= group["quota_limit"]:
            raise ProviderError(429, f"sub2api 分组订阅额度已耗尽：{group_id}")
        return group

    def pick_account_credentials(self, group_id: str) -> dict[str, Any] | None:
        """按冷却/额度过滤后取一个账号，并返回转发所需的密钥字段（仅内部使用）。"""
        now = int(time.time())
        conn = self._db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM sub2_accounts WHERE group_id = ? AND enabled = 1 "
                "AND (quota_limit = 0 OR quota_used < quota_limit) AND cooldown_until <= ? "
                "ORDER BY last_used_at ASC, weight DESC, id ASC LIMIT 1",
                (group_id, now),
            ).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE sub2_accounts SET last_used_at = ? WHERE id = ?", (now, row["id"]))
            conn.commit()
            return {
                "id": row["id"],
                "upstream_base": row["upstream_base"],
                "api_key": row["api_key"],
            }
        finally:
            conn.close()

    def peek_account_credentials(self, group_id: str) -> dict[str, Any] | None:
        """只读探测：不更新 last_used_at，避免模型发现干扰分发轮转。"""
        now = int(time.time())
        conn = self._db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM sub2_accounts WHERE group_id = ? AND enabled = 1 "
                "AND (quota_limit = 0 OR quota_used < quota_limit) AND cooldown_until <= ? "
                "ORDER BY weight DESC, id ASC LIMIT 1",
                (group_id, now),
            ).fetchone()
            if row is None:
                return None
            return {"id": row["id"], "upstream_base": row["upstream_base"], "api_key": row["api_key"]}
        finally:
            conn.close()

    def cool_down(self, account_id: int, seconds: int = DEFAULT_COOLDOWN_SEC) -> None:
        conn = self._db.connect()
        try:
            conn.execute(
                "UPDATE sub2_accounts SET cooldown_until = ? WHERE id = ?",
                (int(time.time()) + max(seconds, 1), account_id),
            )
            conn.commit()
        finally:
            conn.close()

    def charge(
        self,
        group_id: str,
        account_id: int | None,
        *,
        model: str,
        usage: dict[str, Any] | None,
        status: int = 200,
        note: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        prompt, completion = totals(usage)
        cost = prompt + completion
        conn = self._db.connect()
        try:
            if account_id is not None:
                conn.execute(
                    "UPDATE sub2_accounts SET quota_used = quota_used + ? WHERE id = ?", (cost, account_id)
                )
            conn.execute(
                "UPDATE sub2_groups SET quota_used = quota_used + ? WHERE id = ?", (cost, group_id)
            )
            conn.execute(
                "INSERT INTO sub2_ledger(ts, group_id, account_id, model, prompt_tokens, completion_tokens, "
                "cost, status, note, reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (int(time.time()), group_id, account_id, model, prompt, completion, cost, status, note, reason or None),
            )
            conn.commit()
            row = conn.execute("SELECT quota_limit, quota_used FROM sub2_groups WHERE id = ?", (group_id,)).fetchone()
            limit = float(row["quota_limit"] or 0) if row else 0
            used = float(row["quota_used"] or 0) if row else 0
            return {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "cost": cost,
                "quota_limit": limit,
                "quota_used": used,
                "quota_remaining": max(limit - used, 0) if limit > 0 else None,
            }
        finally:
            conn.close()

    def ledger(self, group_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        conn = self._db.connect()
        try:
            if group_id:
                rows = conn.execute(
                    "SELECT * FROM sub2_ledger WHERE group_id = ? ORDER BY id DESC LIMIT ?",
                    (group_id, max(1, min(limit, 500))),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM sub2_ledger ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),)
                ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()


def build_store(settings) -> QuotaStore:
    return QuotaStore(settings.db_path, allow_private_upstreams=settings.allow_private_upstreams)
