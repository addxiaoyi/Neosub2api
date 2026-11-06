"""四家 sidecar 共用的传输层：健康探测、账号只读代理、错误翻译。

这里只做 HTTP 调用与措辞，不实现任何上游协议业务；sidecar 源码与二进制都不进主仓。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from ..services import sidecar_status
from .base import AccountsPage, ProviderError
from .openai_compat import OpenAICompatAdapter


class SidecarLink(OpenAICompatAdapter):
    """在 OpenAICompatAdapter 之上补三件事：短超时健康态、账号列表代理、链路错误人话。"""

    # sidecar 若自行开了鉴权，才需要服务商页的 api_key；由探测的 401 才能确认，这里只做提示
    requires_api_key = False

    def _port(self) -> int:
        return sidecar_status.port_of(self.spec.id, self.base_url)

    def _probe_path(self) -> str:
        return str(self.options.get("health_path") or sidecar_status.PROBE_PATH)

    def readiness_now(
        self,
        *,
        has_credential: bool,
        credential_problem: str,
        last_probe: dict[str, Any] | None,
        credential: str = "",
        options: dict[str, Any] | None = None,
    ) -> tuple[str, tuple[str, ...]]:
        missing = list(self.spec.requires)
        if self.requires_api_key and not has_credential:
            missing.append(
                "sidecar API Key（仅当 sidecar 开启鉴权时必需，填在服务商页 api_key；"
                "有没有由探测返回 401 判定）"
            )
        missing.append(
            "sidecar 存活与鉴权只能由探测结果判定（/api/providers 的 sidecar_status）；"
            "列表渲染阶段不外呼，进程是否在线此时不可知"
        )
        return "needs_config", tuple(missing)

    async def health(self) -> dict[str, Any]:
        probe = await sidecar_status.probe_sidecar(
            self.spec.id, self.base_url, self._probe_path(), api_key=self.api_key
        )
        return {
            "connected": probe.state == "alive",
            "state": probe.state,
            "latency_ms": probe.latency_ms,
            "error": "" if probe.state == "alive" else probe.message,
        }

    def _link_error(self, exc: ProviderError) -> ProviderError:
        """把 client 层笼统的「上游不可达」拆成超时 / 拒绝连接两种人话。"""
        if exc.status_code != 502:
            return exc
        cause = exc.__cause__
        port = self._port()
        if isinstance(cause, httpx.TimeoutException):
            return ProviderError(504, f"侧车无响应（端口 {port}）：请求超时，进程可能卡死")
        if isinstance(cause, httpx.ConnectError):
            return ProviderError(502, f"侧车未运行（端口 {port}）：连接被拒绝，请先启动对应进程")
        return exc

    async def _sidecar_forward(self, path: str, payload: dict[str, Any], *, stream: bool):
        try:
            return await self._client().forward(path, payload, stream=stream)
        except ProviderError as exc:
            raise self._link_error(exc) from exc.__cause__

    async def chat_completion(self, payload: dict[str, Any], *, stream: bool):
        return await self._sidecar_forward("/v1/chat/completions", self._transform_chat(payload), stream=stream)

    async def sync_accounts(self) -> AccountsPage:
        """只读代理账号列表；sidecar 没有该接口时给明确原因，不返回空数组冒充「没有账号」。"""
        path = str(self.options.get("accounts_path") or "/api/accounts")
        try:
            data = await self._client(timeout=8.0).get_json(path)
        except ProviderError as exc:
            if exc.status_code == 404:
                return AccountsPage(
                    accounts=[], pool_available=False,
                    note=f"sidecar 未提供账号列表接口（{path} 返回 404）：请到其管理面板查看，本仓不代管账号",
                )
            raise self._link_error(exc) from exc.__cause__
        rows = _extract_rows(data)
        if rows is None:
            return AccountsPage(
                accounts=[], pool_available=False,
                note=f"sidecar 返回的账号结构无法识别（{path}）：请到其管理面板查看",
            )
        accounts = [item for idx, raw in enumerate(rows) if (item := _account_row(raw, idx))]
        if not accounts and rows:
            return AccountsPage(
                accounts=[], pool_available=False,
                note=f"sidecar 返回了 {len(rows)} 条账号但字段不认识（{path}）：直接展示原始结构会泄露凭据，已跳过",
            )
        return AccountsPage(accounts=accounts, pool_available=True, note="")


def _extract_rows(data: Any) -> list[Any] | None:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("accounts", "data", "items", "list"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return None


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _account_row(raw: Any, idx: int) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    uid = str(raw.get("uid") or raw.get("id") or "")
    name = str(raw.get("file") or raw.get("filename") or uid or f"sidecar-{idx}")
    return {
        "file": name,
        "uid": uid,
        "nickname": str(raw.get("nickname") or raw.get("name") or ""),
        "enterprise_id": str(raw.get("enterprise_id") or raw.get("enterprise") or ""),
        "realm": str(raw.get("realm") or raw.get("region") or ""),
        "expires_at": _as_int(raw.get("expires_at") or raw.get("expire_at")),
        "credits": _as_int(raw.get("credits") or raw.get("quota") or raw.get("balance")),
        "note": str(raw.get("note") or ""),
        "disabled": 1 if (raw.get("disabled") or raw.get("status") == "disabled") else 0,
        "meta_json": json.dumps(raw, ensure_ascii=False),
    }
