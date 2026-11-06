import logging
import time
from dataclasses import dataclass
from typing import Any

from .adapters import build_adapters
from .adapters.base import ProviderAdapter, ProviderError
from .api_utils import mask_secret, now
from .config import LEGACY_PROVIDER_BASES, Settings
from .db import Database, get_json_setting, set_json_setting
from .net_guard import UpstreamAddressError, guard_upstream_url, is_loopback_url
from .services.credential_state import (
    INVALIDATED,
    SUPPORT_EXPIRY,
    SUPPORT_NONE,
    SUPPORT_PROBE,
    SUPPORT_REQUEST,
    CredentialState,
    classify,
    is_jwt,
)

logger = logging.getLogger("unified.registry")

# provider id → Settings 上的地址字段；只有 pid 同时出现在 settings.env_base_pids 时才算 env 来源
ENV_BASE_FIELDS = {
    "codebuddy": "codebuddy_base",
    "opencode": "opencode_upstream_base",
    "codar_sidecar": "codar_sidecar_base",
    "trae_sidecar": "trae_sidecar_base",
    "deepseek_sidecar": "deepseek_sidecar_base",
    "qoder_sidecar": "qoder_sidecar_base",
}


@dataclass(frozen=True)
class ResolvedProvider:
    """适配器最终生效的统一视图：地址、地址来源、按顺序取用的凭据链。"""

    base_url: str
    base_url_source: str
    api_keys: tuple[str, ...]
    credentials: tuple[dict[str, Any], ...]

    def public_credentials(self) -> list[dict[str, Any]]:
        return [
            {
                "id": row["id"],
                "label": row["label"],
                "base_url": row["base_url"],
                "secret_masked": mask_secret(row["secret"]),
                "sort_order": int(row["sort_order"]),
            }
            for row in self.credentials
        ]


class ProviderRegistry:
    """provider 注册表：适配器清单 + 数据库里的启用状态/地址/凭据，进程内单例。

    地址解析顺序：provider_credentials.base_url → providers.base_url → env → spec 默认。
    """

    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self._adapters: dict[str, ProviderAdapter] = {
            adapter.spec.id: adapter for adapter in build_adapters(settings)
        }
        self._seed()
        self._warn_legacy_bases()

    def allows_loopback(self, pid: str) -> bool:
        """这家 provider 是否允许 loopback 上游：由 spec 默认地址推导，不写死名单。"""
        adapter = self._adapters.get(pid)
        return bool(adapter) and is_loopback_url(adapter.spec.default_base_url)

    def register_provider(self, adapter: ProviderAdapter, *, enabled: bool = True, label: str = "") -> None:
        """运行期注册额外 provider（仅演示模式使用）：写/更新 providers 行并接入适配器表。

        与 _seed 的差别：_seed 只处理进程启动时就存在的静态适配器，演示 provider 的地址
        要到假上游端口确定后才能算出来，所以单独走这个入口，幂等（重启只更新地址）。
        """
        pid = adapter.spec.id
        conn = self.db.connect()
        try:
            exists = conn.execute("SELECT id FROM providers WHERE id = ?", (pid,)).fetchone()
            ts = int(time.time())
            name = label or adapter.spec.name
            if exists is None:
                conn.execute(
                    "INSERT INTO providers(id, enabled, base_url, label, updated_at) VALUES(?,?,?,?,?)",
                    (pid, 1 if enabled else 0, adapter.base_url, name, ts),
                )
            else:
                conn.execute(
                    "UPDATE providers SET enabled = ?, base_url = ?, label = ?, updated_at = ? WHERE id = ?",
                    (1 if enabled else 0, adapter.base_url, name, ts, pid),
                )
            conn.commit()
        finally:
            conn.close()
        self._adapters[pid] = adapter
        self.get(pid)

    @staticmethod
    def _is_legacy_base(pid: str, base_url: str) -> bool:
        if not base_url or pid not in LEGACY_PROVIDER_BASES:
            return False
        return base_url.rstrip("/") in LEGACY_PROVIDER_BASES[pid]

    @classmethod
    def legacy_base_warning(cls, pid: str, base_url: str) -> str:
        """base_url 命中历史默认值时给出人话提示；空串表示无可疑之处。"""
        if not cls._is_legacy_base(pid, base_url):
            return ""
        return (
            f"{base_url} 是历史默认地址（Deepseek2Go 上游实际为 5001），"
            "可能是旧库残留的种子值；已保留原值，确认后请改成 http://127.0.0.1:5001"
        )

    @classmethod
    def legacy_credential_warning(cls, pid: str, row: dict[str, Any]) -> str:
        """凭据行命中历史默认值时的提示，带凭据 id 与 label 便于前端定位。"""
        base_url = str(row.get("base_url") or "")
        if not cls._is_legacy_base(pid, base_url):
            return ""
        label = str(row.get("label") or "").strip()
        name = f"#{row.get('id')}" + (f"（{label}）" if label else "")
        return f"{pid} 凭据 {name}：{cls.legacy_base_warning(pid, base_url)}"

    def _warn_legacy_bases(self) -> None:
        conn = self.db.connect()
        try:
            rows = conn.execute("SELECT id, base_url FROM providers").fetchall()
            creds = conn.execute(
                "SELECT id, provider, label, base_url FROM provider_credentials "
                "WHERE enabled = 1 ORDER BY provider, sort_order, id"
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            warning = self.legacy_base_warning(str(row["id"]), str(row["base_url"] or ""))
            if warning:
                logger.warning("provider %s：%s", row["id"], warning)
        for row in creds:
            warning = self.legacy_credential_warning(str(row["provider"]), dict(row))
            if warning:
                logger.warning("provider_credentials 残留：%s", warning)

    def _env_base(self, pid: str) -> str:
        """仅当对应 env 变量被显式设置时返回其值，否则返回空串。"""
        field = ENV_BASE_FIELDS.get(pid)
        if field is None or pid not in self.settings.env_base_pids:
            return ""
        return str(getattr(self.settings, field, "") or "")

    def _seed(self) -> None:
        conn = self.db.connect()
        now_ts = int(time.time())
        try:
            for pid, adapter in self._adapters.items():
                row = conn.execute(
                    "SELECT id, base_url, label FROM providers WHERE id = ?", (pid,)
                ).fetchone()
                if row is None:
                    # 新行不写默认地址：留空才能让 db>env>default 的解析链继续生效
                    conn.execute(
                        "INSERT INTO providers(id, enabled, base_url, label, updated_at) "
                        "VALUES(?, ?, ?, ?, ?)",
                        (pid, 1 if pid == "codebuddy" else 0, "", adapter.spec.name, now_ts),
                    )
                    continue
                if not str(row["label"] or ""):
                    conn.execute("UPDATE providers SET label = ? WHERE id = ?", (adapter.spec.name, pid))
                # 旧版 _seed 把当时的默认地址写进了 base_url；与当前兜底值相同的一律视为未配置
                if str(row["base_url"] or "") in {self._env_base(pid), adapter.spec.default_base_url}:
                    conn.execute("UPDATE providers SET base_url = '' WHERE id = ?", (pid,))
            conn.commit()
        finally:
            conn.close()

    def _snapshot(
        self,
        conn=None,
    ) -> tuple[
        dict[str, dict[str, Any]],
        dict[str, dict[str, Any]],
        dict[str, list[dict[str, Any]]],
    ]:
        """一次连接读齐 providers / provider_config / 已启用凭据，避免每 provider 反复开库。

        conn：借调用方的连接（网关准入段共用连接），不传则自开自关，其它调用方行为不变。
        """
        own = conn is None
        if own:
            conn = self.db.connect()
        try:
            rows = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM providers").fetchall()}
            raw = get_json_setting(conn, "provider_config", {})
            creds = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM provider_credentials WHERE enabled = 1 ORDER BY sort_order, id"
                ).fetchall()
            ]
        finally:
            if own:
                conn.close()
        configs = {str(k): v for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in creds:
            grouped.setdefault(row["provider"], []).append(row)
        return rows, configs, grouped

    def _rows(self, conn=None) -> dict[str, dict[str, Any]]:
        own = conn is None
        if own:
            conn = self.db.connect()
        try:
            rows = conn.execute("SELECT * FROM providers").fetchall()
            return {row["id"]: dict(row) for row in rows}
        finally:
            if own:
                conn.close()

    def _provider_configs(self, conn=None) -> dict[str, dict[str, Any]]:
        own = conn is None
        if own:
            conn = self.db.connect()
        try:
            raw = get_json_setting(conn, "provider_config", {})
        finally:
            if own:
                conn.close()
        if not isinstance(raw, dict):
            return {}
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}

    def _key_chain(
        self, pid: str, credentials: tuple[dict[str, Any], ...], cfg: dict[str, Any]
    ) -> tuple[str, ...]:
        """凭据顺序：统一凭据库 → 旧版 provider_config.api_key → env（仅 codebuddy 有）。"""
        raw: list[Any] = [row["secret"] for row in credentials]
        raw.append(cfg.get("api_key"))
        if pid == "codebuddy":
            raw.append(self.settings.codebuddy_api_key)
        keys: list[str] = []
        for item in raw:
            key = str(item or "").strip()
            if key and key not in keys:
                keys.append(key)
        return tuple(keys)

    def resolved(self, pid: str, *, snapshot=None, conn=None) -> ResolvedProvider:
        if pid not in self._adapters:
            raise ProviderError(404, f"未知的 provider：{pid}")
        rows, configs, grouped = snapshot or self._snapshot(conn)
        credentials = tuple(grouped.get(pid, ()))
        row = rows.get(pid, {})
        cfg = configs.get(pid, {})

        cred_base = next((str(c["base_url"]) for c in credentials if c["base_url"]), "")
        row_base = str(row.get("base_url") or "")
        if cred_base:
            base_url, source = cred_base, "db"
        elif row_base:
            base_url, source = row_base, "db"
        else:
            env = self._env_base(pid)
            base_url = env or self._adapters[pid].spec.default_base_url
            source = "env" if env else "default"

        return ResolvedProvider(
            base_url=base_url,
            base_url_source=source,
            api_keys=self._key_chain(pid, credentials, cfg),
            credentials=credentials,
        )

    def _last_probes(self) -> dict[str, dict[str, Any]]:
        """最近一次健康探测结果：只读库，用于按配置推导 readiness，渲染时不外呼。"""
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT h.provider, h.status, h.error, h.upstream_status, h.auth_failed, h.finished_at "
                "FROM provider_health_checks h JOIN ("
                "  SELECT provider, MAX(id) AS id FROM provider_health_checks GROUP BY provider"
                ") m ON h.id = m.id"
            ).fetchall()
        finally:
            conn.close()
        return {
            row["provider"]: {
                "status": row["status"],
                "error": row["error"],
                "upstream_status": row["upstream_status"],
                "auth_failed": bool(row["auth_failed"]),
                "checked_at": int(row["finished_at"]),
            }
            for row in rows
        }

    def _auth_failure_at(self) -> dict[str, int]:
        """真实请求留下的鉴权失败留痕：provider → 最近一次 401/403 的时间。

        401/403 可能是这把 provider 凭据被上游拒，也可能是账号级问题；只取时间戳做上限，
        具体归属交给凭据状态判定，不据此推断别的结论。
        """
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT provider, MAX(ts) AS ts FROM request_logs "
                "WHERE status IN (401, 403) AND provider IS NOT NULL AND provider != '' "
                "GROUP BY provider"
            ).fetchall()
        finally:
            conn.close()
        return {str(row["provider"]): int(row["ts"]) for row in rows if row["ts"]}

    def list_public(self) -> list[dict[str, Any]]:
        rows, configs, grouped = self._snapshot()
        probes = self._last_probes()
        auth_failures = self._auth_failure_at()
        out: list[dict[str, Any]] = []
        for pid, adapter in self._adapters.items():
            row = rows.get(pid, {})
            spec = adapter.spec
            view = self.resolved(pid, snapshot=(rows, configs, grouped))
            cfg = configs.get(pid, {})
            item = spec.to_public(
                enabled=bool(row.get("enabled", 0)),
                base_url=view.base_url,
                label=str(row.get("label") or ""),
            )
            item["base_url_source"] = view.base_url_source
            # 由 spec.default_base_url 推导：前端与文档据此解释本地地址为什么能/不能保存
            item["allows_local_base_url"] = self.allows_loopback(pid)
            # 历史默认值残留只做提示，字段为空即无异常
            item["base_url_warning"] = self.legacy_base_warning(pid, str(row.get("base_url") or ""))
            item["credential_base_warnings"] = [
                {
                    "provider": pid,
                    "id": int(cred["id"]),
                    "label": str(cred.get("label") or ""),
                    "base_url": str(cred.get("base_url") or ""),
                    "suggested_base_url": str(spec.default_base_url or ""),
                    "message": warning,
                }
                for cred in view.credentials
                if (warning := self.legacy_credential_warning(pid, cred))
            ]
            item["has_api_key"] = bool(view.api_keys)
            item["credential_count"] = len(view.credentials)
            item["credentials"] = view.public_credentials()
            options = cfg.get("options") if isinstance(cfg.get("options"), dict) else {}
            item["options"] = options
            # readiness 按「已存配置 + 上次探测结果」推导，渲染时绝不外呼：列表要稳，探活要准
            # 凭据链里可能混着失效的旧行，只有「全部有问题」才算缺配置；坏的首条不能挡住后面的好 key
            problems = [adapter.credential_problem("api_key", key) for key in view.api_keys]
            usable_key = next((key for key, problem in zip(view.api_keys, problems) if not problem), "")
            first_problem = next((problem for problem in problems if problem), "")
            state, missing = adapter.readiness_now(
                has_credential=bool(usable_key),
                credential_problem="" if usable_key else first_problem,
                last_probe=probes.get(pid),
                credential=usable_key or (view.api_keys[0] if view.api_keys else ""),
                options=options,
            )
            item["readiness"] = state
            item["requires"] = list(missing)
            # 凭据状态：只按「已存的探活/真实请求证据 + 本地确定性结论」分类，不猜也不外呼
            stored_key = usable_key or (view.api_keys[0] if view.api_keys else "")
            credential_state = self._credential_state(
                adapter,
                has_credential=bool(view.api_keys),
                format_problem="" if usable_key else first_problem,
                credential=stored_key,
                options=options,
                probe=probes.get(pid),
                auth_failure_at=auth_failures.get(pid),
                credential_updated_at=max(
                    [int(cred.get("updated_at") or 0) for cred in view.credentials]
                    + [int(cfg.get("api_key_updated_at") or 0)]
                ),
            )
            support = self._credential_state_support(adapter, stored_key)
            item.update(credential_state.to_public(support))
            # 失效是 readiness 的强信号：文案要说清「重新粘贴」，不能退回笼统的「待配置」
            if credential_state.state == INVALIDATED:
                item["readiness"] = "needs_config"
                # 适配器自己也算出了「过期/重新粘贴」时不重复展示，只保留真正还没解决的缺项
                rest = [
                    text
                    for text in item["requires"]
                    if text not in credential_state.reason
                    and not any(hint in text for hint in ("重新粘贴", "已过期", "已失效"))
                ]
                item["requires"] = [credential_state.reason, *rest]
            # 侧车存活探测结果来自 services 的短 TTL 缓存，这里只贴结果、不发请求
            from .services.sidecar_status import decorate_public

            decorate_public(item)
            out.append(item)
        return out

    @staticmethod
    def _credential_state(
        adapter: ProviderAdapter,
        *,
        has_credential: bool,
        format_problem: str,
        credential: str,
        options: dict[str, Any],
        probe: dict[str, Any] | None,
        auth_failure_at: int | None,
        credential_updated_at: int,
    ) -> CredentialState:
        expiry_problem = (
            adapter.credential_expiry_hint(credential, options) if has_credential else ""
        )
        return classify(
            has_credential=has_credential,
            format_problem=format_problem,
            expiry_problem=expiry_problem,
            probe=probe,
            auth_failure_at=auth_failure_at,
            credential_updated_at=credential_updated_at,
        )

    @staticmethod
    def _credential_state_support(adapter: ProviderAdapter, credential: str) -> str:
        """这家能判到什么程度：探活自报 401/403 > 本地 JWT 过期 > 只能靠真实请求留痕 > 判不了。"""
        if getattr(adapter, "auth_probe_supported", False):
            return SUPPORT_PROBE
        if is_jwt(credential):
            return SUPPORT_EXPIRY
        if adapter.spec.capabilities.get("chat"):
            return SUPPORT_REQUEST
        return SUPPORT_NONE

    def update(
        self,
        pid: str,
        *,
        enabled: bool | None = None,
        base_url: str | None = None,
        label: str | None = None,
        api_key: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if pid not in self._adapters:
            raise ProviderError(404, f"未知的 provider：{pid}")
        if enabled is None and base_url is None and label is None and api_key is None and options is None:
            raise ProviderError(400, "没有需要更新的字段")
        conn = self.db.connect()
        try:
            sets: list[str] = []
            values: list[Any] = []
            if enabled is not None:
                sets.append("enabled = ?")
                values.append(1 if enabled else 0)
            if base_url is not None:
                try:
                    cleaned = guard_upstream_url(
                        base_url,
                        allow_private=self.settings.allow_private_upstreams,
                        allow_loopback=self.allows_loopback(pid),
                        provider=pid,
                        field="base_url",
                    )
                except UpstreamAddressError as exc:
                    raise ProviderError(400, str(exc)) from exc
                sets.append("base_url = ?")
                values.append(cleaned)
            if label is not None:
                sets.append("label = ?")
                values.append(label.strip()[:64])
            if sets:
                sets.append("updated_at = ?")
                values.append(int(time.time()))
                values.append(pid)
                conn.execute(f"UPDATE providers SET {', '.join(sets)} WHERE id = ?", values)
            if api_key is not None or options is not None:
                configs = self._provider_configs()
                cfg = configs.get(pid, {})
                if api_key is not None:
                    # 前端掩码回显约定："***" = 不变，空串 = 清除
                    if api_key != "***":
                        cfg["api_key"] = api_key.strip()
                        # 重新粘贴后旧探活/请求证据不再代表这把 key，记下时间让判定失效
                        cfg["api_key_updated_at"] = int(time.time())
                if options is not None:
                    merged = cfg.get("options") if isinstance(cfg.get("options"), dict) else {}
                    merged = {**merged, **options}
                    cfg["options"] = merged
                configs[pid] = cfg
                set_json_setting(conn, "provider_config", configs)
            conn.commit()
        finally:
            conn.close()
        # 立即应用到进程内适配器实例：设置保存即生效，不等重启
        self.get(pid)
        return next(item for item in self.list_public() if item["id"] == pid)

    def get(self, pid: str, *, conn=None) -> ProviderAdapter:
        adapter = self._adapters.get(pid)
        if adapter is None:
            raise ProviderError(404, f"未知的 provider：{pid}")
        view = self.resolved(pid, conn=conn)
        cfg = self._provider_configs(conn).get(pid, {})
        adapter.base_url = view.base_url
        adapter.configure(
            api_key=view.api_keys[0] if view.api_keys else "",
            options=cfg.get("options") if isinstance(cfg.get("options"), dict) else {},
            api_keys=list(view.api_keys),
        )
        return adapter

    def enabled(self, conn=None) -> list[ProviderAdapter]:
        rows = self._rows(conn)
        return [self.get(pid, conn=conn) for pid, row in rows.items() if row.get("enabled") and pid in self._adapters]

    def resolve(self, model: str, explicit: str | None = None) -> tuple[ProviderAdapter, str]:
        """把请求里的 model 解析成 (适配器, 去掉 provider 前缀后的模型名)。"""
        pid = (explicit or "").strip()
        if pid:
            return self.get(pid), model

        if "/" in model:
            prefix, rest = model.split("/", 1)
            if prefix in self._adapters and rest:
                return self.get(prefix), rest

        conn = self.db.connect()
        try:
            alias_map = get_json_setting(conn, "model_map", {})
        finally:
            conn.close()
        if isinstance(alias_map, dict) and model in alias_map:
            target = str(alias_map[model])
            if "/" in target:
                prefix, rest = target.split("/", 1)
                if prefix in self._adapters and rest:
                    return self.get(prefix), rest
            model = target

        enabled = self.enabled()
        capable = [a for a in enabled if a.spec.capabilities.get("chat")]
        if capable:
            return capable[0], model
        raise ProviderError(503, "没有已启用的 chat provider，请先在「服务商」页启用至少一个")

    # ---------- 统一凭据库 CRUD（写操作由路由层校验 admin 并写审计） ----------

    def credentials(self, provider: str | None = None) -> list[dict[str, Any]]:
        conn = self.db.connect()
        try:
            if provider:
                rows = conn.execute(
                    "SELECT * FROM provider_credentials WHERE provider = ? "
                    "ORDER BY sort_order, id",
                    (provider,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM provider_credentials ORDER BY provider, sort_order, id"
                ).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]

    def credential(self, cid: int) -> dict[str, Any]:
        conn = self.db.connect()
        try:
            row = conn.execute("SELECT * FROM provider_credentials WHERE id = ?", (cid,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise ProviderError(404, f"凭据不存在：{cid}")
        return dict(row)

    def _clean_base(self, pid: str, base_url: str) -> str:
        try:
            return guard_upstream_url(
                base_url,
                allow_private=self.settings.allow_private_upstreams,
                allow_loopback=self.allows_loopback(pid),
                provider=pid,
                field="base_url",
            )
        except UpstreamAddressError as exc:
            raise ProviderError(400, str(exc)) from exc

    def create_credential(
        self,
        provider: str,
        *,
        label: str = "",
        base_url: str = "",
        secret: str = "",
        enabled: bool = True,
        sort_order: int | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        pid = (provider or "").strip()
        if pid not in self._adapters:
            raise ProviderError(404, f"未知的 provider：{pid}")
        if len(secret or "") > 4096:
            raise ProviderError(400, "secret 超过 4096 字符")
        cleaned_base = self._clean_base(pid, base_url)
        ts = now()
        conn = self.db.connect()
        try:
            if sort_order is None:
                row = conn.execute(
                    "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next FROM provider_credentials "
                    "WHERE provider = ?",
                    (pid,),
                ).fetchone()
                sort_order = int(row["next"])
            cur = conn.execute(
                "INSERT INTO provider_credentials(provider, label, base_url, secret, enabled, "
                "sort_order, note, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    pid,
                    (label or "").strip()[:64],
                    cleaned_base,
                    (secret or "").strip(),
                    1 if enabled else 0,
                    int(sort_order),
                    (note or "").strip()[:200],
                    ts,
                    ts,
                ),
            )
            conn.commit()
            cid = int(cur.lastrowid)
        finally:
            conn.close()
        # 立即刷新该 provider 的进程内适配器
        self.get(pid)
        return self.credential(cid)

    def update_credential(
        self,
        cid: int,
        *,
        label: str | None = None,
        base_url: str | None = None,
        secret: str | None = None,
        enabled: bool | None = None,
        sort_order: int | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        current = self.credential(cid)
        if label is None and base_url is None and secret is None and enabled is None and sort_order is None and note is None:
            raise ProviderError(400, "没有需要更新的字段")
        sets: list[str] = []
        values: list[Any] = []
        if label is not None:
            sets.append("label = ?")
            values.append((label or "").strip()[:64])
        if base_url is not None:
            sets.append("base_url = ?")
            values.append(self._clean_base(current["provider"], base_url))
        if secret is not None:
            # 前端掩码回显约定："***" = 不变，空串 = 清除
            if secret != "***":
                if len(secret) > 4096:
                    raise ProviderError(400, "secret 超过 4096 字符")
                sets.append("secret = ?")
                values.append((secret or "").strip())
        if enabled is not None:
            sets.append("enabled = ?")
            values.append(1 if enabled else 0)
        if sort_order is not None:
            sets.append("sort_order = ?")
            values.append(int(sort_order))
        if note is not None:
            sets.append("note = ?")
            values.append((note or "").strip()[:200])
        sets.append("updated_at = ?")
        values.append(now())
        values.append(cid)
        conn = self.db.connect()
        try:
            conn.execute(f"UPDATE provider_credentials SET {', '.join(sets)} WHERE id = ?", values)
            conn.commit()
        finally:
            conn.close()
        self.get(current["provider"])
        return self.credential(cid)

    def delete_credential(self, cid: int) -> dict[str, Any]:
        current = self.credential(cid)
        conn = self.db.connect()
        try:
            conn.execute("DELETE FROM provider_credentials WHERE id = ?", (cid,))
            conn.commit()
        finally:
            conn.close()
        self.get(current["provider"])
        return current
