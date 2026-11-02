import time
from typing import Any

from .adapters import build_adapters
from .adapters.base import ProviderAdapter, ProviderError
from .config import Settings
from .db import Database, get_json_setting, set_json_setting
from .net_guard import UpstreamAddressError, guard_upstream_url


class ProviderRegistry:
    """provider 注册表：适配器清单 + 数据库里的启用状态/地址，进程内单例。"""

    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self._adapters: dict[str, ProviderAdapter] = {
            adapter.spec.id: adapter for adapter in build_adapters(settings)
        }
        self._seed()

    def _default_base(self, pid: str) -> str:
        if pid == "codebuddy":
            return self.settings.codebuddy_base
        if pid == "opencode":
            return self.settings.opencode_upstream_base
        return self._adapters[pid].spec.default_base_url

    def _seed(self) -> None:
        conn = self.db.connect()
        now = int(time.time())
        try:
            for pid, adapter in self._adapters.items():
                row = conn.execute("SELECT id FROM providers WHERE id = ?", (pid,)).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO providers(id, enabled, base_url, label, updated_at) "
                        "VALUES(?, ?, ?, ?, ?)",
                        (pid, 1 if pid == "codebuddy" else 0, self._default_base(pid), adapter.spec.name, now),
                    )
                else:
                    conn.execute(
                        "UPDATE providers SET label = ? WHERE id = ? AND label = ''",
                        (adapter.spec.name, pid),
                    )
            conn.commit()
        finally:
            conn.close()

    def _rows(self) -> dict[str, dict[str, Any]]:
        conn = self.db.connect()
        try:
            rows = conn.execute("SELECT * FROM providers").fetchall()
            return {row["id"]: dict(row) for row in rows}
        finally:
            conn.close()

    def _provider_configs(self) -> dict[str, dict[str, Any]]:
        conn = self.db.connect()
        try:
            raw = get_json_setting(conn, "provider_config", {})
        finally:
            conn.close()
        if not isinstance(raw, dict):
            return {}
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}

    def list_public(self) -> list[dict[str, Any]]:
        rows = self._rows()
        configs = self._provider_configs()
        out: list[dict[str, Any]] = []
        for pid, adapter in self._adapters.items():
            row = rows.get(pid, {})
            spec = adapter.spec
            base_url = str(row.get("base_url") or "") or self._default_base(pid)
            cfg = configs.get(pid, {})
            item = spec.to_public(
                enabled=bool(row.get("enabled", 0)),
                base_url=base_url,
                label=str(row.get("label") or ""),
            )
            item["has_api_key"] = bool(cfg.get("api_key"))
            item["options"] = cfg.get("options") if isinstance(cfg.get("options"), dict) else {}
            out.append(item)
        return out

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

    def get(self, pid: str) -> ProviderAdapter:
        adapter = self._adapters.get(pid)
        if adapter is None:
            raise ProviderError(404, f"未知的 provider：{pid}")
        row = self._rows().get(pid, {})
        adapter.base_url = str(row.get("base_url") or "") or self._default_base(pid)
        cfg = self._provider_configs().get(pid, {})
        adapter.configure(
            api_key=str(cfg.get("api_key") or ""),
            options=cfg.get("options") if isinstance(cfg.get("options"), dict) else {},
        )
        return adapter

    def enabled(self) -> list[ProviderAdapter]:
        rows = self._rows()
        return [self.get(pid) for pid, row in rows.items() if row.get("enabled") and pid in self._adapters]

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
