"""codar 仓内直连实现：调用 Codar 私有 API，不再经过 codar2api sidecar。

边界（证据见 docs/providers/codar2api.md）：
- 自研：/v1/models（读私有目录 /v1/codar/models）、chat/completions、messages、
  responses 的转发与 SSE 透传；多 token 轮换与 401 续期。
- 不实现：微信扫码/短信登录、新 IP 验证、每日签到、领免费模型。这些依赖浏览器或
  人机交互，且随 Codar 客户端版本变化，继续留在 codar2api sidecar。
- 鉴权：服务商页 api_key 或 options.tokens 填 Codar 账号 JWT，按 Bearer 注入。
"""

import asyncio
import base64
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from ..config import Settings
from ..services.rotation import fingerprint
from ..services.scheduling import Plan, Scheduler, session_key
from .base import ProviderError, ProviderSpec, stub_capabilities
from .client import UnifiedApiClient
from .openai_compat import OpenAICompatAdapter

logger = logging.getLogger("unified.codar")

CATALOG_PATH = "/v1/codar/models"
REFRESH_PATH = "/v1/token/refresh"
DEFAULT_PAY_BASE = "https://api.kuaipaper.com"
MODEL_USER_AGENT = "codar-desktop/0.3.3"
ANTHROPIC_VERSION = "2023-06-01"
# 与上游 codar2api 的默认单请求最多尝试账号数保持一致
MAX_TOKEN_ATTEMPTS = 3
NETWORK_STATUS = 0
_TOKEN_SPLIT = re.compile(r"[\s,;]+")

# 失败动作表：0=网络层换号，401=先续期，402=登记冷却后停止透传，408/429=冷却后换号，5xx=换号
RETRY_ACTIONS = {NETWORK_STATUS: "rotate", 401: "refresh", 402: "stop", 408: "cool", 429: "cool"}


def _retryable(status: int) -> str:
    """按状态码给出失败动作；空串表示不重试、原样透传。"""
    if 500 <= status <= 599:
        return "rotate"
    return RETRY_ACTIONS.get(status, "")


def _enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}


def _loopback(host: str) -> bool:
    return host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"} or host.startswith("127.")


def _clean_token(value: Any) -> str:
    token = str(value or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


_MISSING_REASON = "尚未导入 Codar JWT"
_MISSING_NEXT = "在 codar2api sidecar 完成扫码/短信登录后，复制账号 JWT 到这里"
_INVALID_NEXT = "确认粘贴的是 Codar 账号 JWT，而不是其他 token 或半截字符串"
_EXPIRED_NEXT = "重新粘贴新的 Codar JWT（可先在 codar2api sidecar 重新登录）"


def _invalid(reason: str, next_step: str = _INVALID_NEXT) -> dict[str, Any]:
    return {
        "state": "invalid",
        "expires_at": None,
        "expires_in": None,
        "reason": reason,
        "next_step": next_step,
    }


def inspect_token(value: Any) -> dict[str, Any]:
    """只解码不验签：Codar 私有 JWT 没有可用公钥，验签无意义。"""
    token = _clean_token(value)
    if not token:
        return {
            "state": "missing",
            "expires_at": None,
            "expires_in": None,
            "reason": _MISSING_REASON,
            "next_step": _MISSING_NEXT,
        }
    parts = token.split(".")
    if len(parts) != 3 or not parts[1]:
        return _invalid("不是标准 JWT（应为三段 base64url）")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return _invalid("JWT payload 无法解码")
    if not isinstance(data, dict):
        return _invalid("JWT payload 不是对象")
    exp = data.get("exp")
    if exp is None:
        return {
            "state": "valid",
            "expires_at": None,
            "expires_in": None,
            "reason": "JWT 未声明到期时间，只能由上游判定",
            "next_step": "",
        }
    try:
        expires_at = int(exp)
    except (TypeError, ValueError):
        return _invalid("JWT 的 exp 字段不是整数时间戳")
    now = int(time.time())
    if expires_at <= now:
        at = datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()
        return {
            "state": "expired",
            "expires_at": expires_at,
            "expires_in": 0,
            "reason": f"JWT 已过期（到期时间 {at}）",
            "next_step": _EXPIRED_NEXT,
        }
    return {
        "state": "valid",
        "expires_at": expires_at,
        "expires_in": expires_at - now,
        "reason": "",
        "next_step": "",
    }


class CodarAdapter(OpenAICompatAdapter):
    spec = ProviderSpec(
        id="codar",
        name="Codar (仓内直连)",
        vendor="Codar 私有 API（行为参考 AiXiaoc/codar2api）",
        license="MIT（仅参考上游公开行为，未复制源码）",
        runtime="仓内 Python 适配器（无 sidecar）",
        status="implemented",
        description="直连 Codar 私有 API：models、chat/completions、messages、responses；"
        "扫码登录、短信登录与签到仍需 codar2api sidecar。",
        default_base_url="https://api.claw.speedai.com",
        capabilities=stub_capabilities(chat=True, messages=True, models=True, responses=True, count_tokens=True),
        protocols=("openai.chat.completions", "anthropic.messages", "openai.responses"),
        user_agent=MODEL_USER_AGENT,
        risk_level="safe",
        risk_note="只直连 Codar 私有接口，参考 MIT 上游的公开协议行为；"
        "Codar 平台限本人名下账号，转售/共享风险由部署方承担。",
        readiness="needs_config",
        requires=(
            "Codar 账号 JWT（扫码/短信登录仍需 codar2api sidecar）",
            "扫码/短信登录只能走 codar2api sidecar，本页不支持；本页只导入登录后拿到的 JWT",
        ),
        limitations={
            "scan_login": "本页不能扫码：微信扫码/短信登录必须在 codar2api sidecar 完成，再把拿到的 JWT 导入这里",
            "checkin": "每日签到、领取免费模型只在 codar2api sidecar 提供，本页不提供",
        },
    )

    def __init__(self, settings: Settings, base_url: str = "") -> None:
        self._settings = settings
        self._tokens: list[str] = []
        self._config_signature: tuple[str, ...] = ()
        # 账号调度：四档策略 + 冷却表，顺序决定与健康登记都在 services/scheduling.py
        self._scheduler = Scheduler()
        self._lock = asyncio.Lock()
        super().__init__(settings, base_url)

    def _configured_tokens(self) -> list[str]:
        """按 options.tokens → api_key → api_keys → options.token 收集 token，去重保序。"""
        raw = self.options.get("tokens")
        collected: list[str] = []
        if isinstance(raw, str):
            collected.extend(_TOKEN_SPLIT.split(raw))
        elif isinstance(raw, (list, tuple)):
            collected.extend(str(item) for item in raw)
        collected.append(self.api_key)
        collected.extend(self.api_keys or [])
        collected.append(str(self.options.get("token") or ""))
        tokens: list[str] = []
        for item in collected:
            token = _clean_token(item)
            if token and token not in tokens:
                tokens.append(token)
        return tokens

    def readiness_now(
        self,
        *,
        has_credential: bool,
        credential_problem: str,
        last_probe: dict[str, Any] | None,
        credential: str = "",
        options: dict[str, Any] | None = None,
    ) -> tuple[str, tuple[str, ...]]:
        options = options or {}
        collected: list[Any] = [credential, options.get("token")]
        raw = options.get("tokens")
        if isinstance(raw, str):
            collected.extend(_TOKEN_SPLIT.split(raw))
        elif isinstance(raw, (list, tuple)):
            collected.extend(raw)
        tokens = [token for token in (_clean_token(item) for item in collected) if token]
        if not tokens:
            missing = [
                "Codar 账号 JWT（服务商页 api_key 或 options.tokens；扫码/短信登录仍需 codar2api sidecar）"
            ]
            if credential_problem:
                missing.append(credential_problem)
            return "needs_config", tuple(missing)
        states = [inspect_token(token) for token in tokens]
        if any(state["state"] == "valid" for state in states):
            return "ready", ()
        if all(state["state"] == "expired" for state in states):
            return "needs_config", (f"Codar 账号 JWT 已过期：{states[0]['reason']}；{_EXPIRED_NEXT}",)
        bad = next(state for state in states if state["state"] != "valid")
        return "needs_config", (f"Codar 账号 JWT 无法使用：{bad['reason']}；{_INVALID_NEXT}",)

    async def _rotation(
        self, payload: dict[str, Any] | None = None, *, limit: int = MAX_TOKEN_ATTEMPTS
    ) -> Plan:
        """同步 token 列表并按四档策略给出本次请求的尝试顺序。

        策略来源：options.schedule_policy（sticky/priority/weighted/failover），
        权重来源：options.token_weights（与 token 顺序对齐的数组或 {序号: 权重}）。
        会话标识从请求体取（网关不向适配器传 HTTP 头），取不到时 sticky 退化为 failover。
        """
        configured = tuple(self._configured_tokens())
        async with self._lock:
            if configured != self._config_signature:
                self._tokens = list(configured)
                self._config_signature = configured
            tokens = list(self._tokens)
        if not tokens:
            return Plan([], "none", False, "尚未导入账号 JWT")
        return await self._scheduler.plan(
            tokens,
            policy=str(self.options.get("schedule_policy") or self.options.get("schedule") or ""),
            session_key=session_key(payload),
            limit=limit,
            weights=self.options.get("token_weights"),
        )

    async def _current_token(self, index: int) -> str:
        async with self._lock:
            return self._tokens[index]

    def _client(
        self,
        token: str,
        *,
        base_url: str = "",
        timeout: float | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> UnifiedApiClient:
        headers = {"user-agent": str(self.options.get("user_agent") or MODEL_USER_AGENT)}
        configured = self.options.get("extra_headers")
        if isinstance(configured, dict):
            headers.update({str(key): str(value) for key, value in configured.items()})
        if extra_headers:
            headers.update(extra_headers)
        return UnifiedApiClient(
            base_url or self.base_url,
            api_key=token,
            timeout=timeout or float(self.options.get("timeout") or 120.0),
            proxy=str(self.options.get("proxy") or ""),
            extra_headers=headers,
        )

    async def _refresh(self, index: int, token: str) -> str:
        """POST {pay_base}/v1/token/refresh，响应取 data.token 或 token。

        依据：上游 internal/gateway/upstream.go 的 renew() 用账号 JWT 调付费服务刷新。
        非标准接口，失败时静默降级到下一个 token。
        """
        if not _enabled(self.options.get("refresh", True)):
            return ""
        async with self._lock:
            if index >= len(self._tokens) or self._tokens[index] != token:
                return self._tokens[index] if index < len(self._tokens) else ""
        pay_base = str(self.options.get("pay_base") or DEFAULT_PAY_BASE).rstrip("/")
        pay_host = urlsplit(pay_base).hostname or ""
        if not _loopback(pay_host):
            # 续期是设计行为，但绝不能悄悄打外网：默认只在显式 allow_external_refresh 后外呼
            if not _enabled(self.options.get("allow_external_refresh", False)):
                logger.warning(
                    "Codar token 续期目标为外部域名 %s，已跳过：如需外呼请在 options 设 allow_external_refresh=true",
                    pay_host,
                )
                return ""
            logger.warning("Codar token 续期向外部域名 %s 发起请求（allow_external_refresh=true）", pay_host)
        try:
            data = await self._client(token, base_url=pay_base, timeout=30.0).post_json(REFRESH_PATH, {})
        except ProviderError as exc:
            logger.info("Codar token 续期失败：%s", exc.message)
            return ""
        payload = data.get("data") if isinstance(data, dict) else None
        fresh = _clean_token(payload.get("token") if isinstance(payload, dict) else "")
        if not fresh and isinstance(data, dict):
            fresh = _clean_token(data.get("token"))
        if not fresh or fresh == token:
            return ""
        async with self._lock:
            if index >= len(self._tokens) or self._tokens[index] != token:
                return self._tokens[index] if index < len(self._tokens) else ""
            self._tokens[index] = fresh
        return fresh

    async def _forward_with_tokens(
        self,
        path: str,
        payload: dict[str, Any] | None,
        *,
        stream: bool,
        extra_headers: dict[str, str] | None = None,
        session_payload: dict[str, Any] | None = None,
    ):
        # session_payload 是客户端原始请求体：chat 转发前会改写成上游格式，会话字段只在这里可读
        plan = await self._rotation(session_payload)
        if not plan.indices:
            raise ProviderError(
                401,
                "Codar 尚未导入账号 JWT：请在服务商页凭据库粘贴 Codar 账号 JWT；"
                "扫码/短信登录只能走 codar2api sidecar，本页不支持。",
            )
        logger.info(
            "Codar 调度 规则=%s 降级=%s 顺序=%s 说明=%s",
            plan.rule,
            plan.degraded,
            [f"#{index}" for index in plan.indices],
            plan.note or "-",
        )
        attempts = 0
        last_reply = None
        last_error: ProviderError | None = None
        while plan.indices and attempts < MAX_TOKEN_ATTEMPTS:
            index = plan.indices.pop(0)
            attempts += 1
            token = await self._current_token(index)
            headers = dict(extra_headers or {})
            if path.startswith("/v1/messages") and token:
                headers["x-api-key"] = token
            if token:
                logger.info("Codar 选号 cred=%s 规则=%s", fingerprint(token), plan.rule)
            try:
                reply = await self._client(token, extra_headers=headers or None).forward(
                    path, payload, stream=stream
                )
            except ProviderError as exc:
                # 连接失败/超时按状态 0 归类：换号或换出口再试，不是账号被拒
                last_error = exc
                logger.warning("Codar 网络层失败（%s），按状态 0 换号重试", exc.message)
                continue
            action = _retryable(reply.status_code)
            if not action:
                await self._scheduler.note_success(token)
                return reply
            if action == "stop":
                # 402 余额/付费受限是账号级判定，登记 900s 冷却后原样透传
                await self._scheduler.note_failure(token, reply.status_code)
                logger.warning("Codar 上游 402 付费受限，停止轮换并原样透传")
                return reply
            last_reply = reply
            if stream:
                # 流式响应已拿到响应头，换号会丢掉已建立的流：登记冷却后原样透传
                if action in {"cool", "rotate"}:
                    await self._scheduler.note_failure(token, reply.status_code)
                logger.warning("Codar 流式请求遇到 %s，原样透传不换号", reply.status_code)
                return reply
            if action == "cool":
                await self._scheduler.note_failure(token, reply.status_code)
                await reply.aclose()
                continue
            if action == "rotate":
                await self._scheduler.note_failure(token, reply.status_code)
                await reply.aclose()
                continue
            # action == "refresh"：401 先按既有口径续期，续期不可用再换号
            await self._scheduler.note_failure(token, reply.status_code)
            await reply.aclose()
            fresh = await self._refresh(index, token)
            if not fresh:
                continue
            attempts += 1
            if attempts > MAX_TOKEN_ATTEMPTS:
                break
            if path.startswith("/v1/messages"):
                headers["x-api-key"] = fresh
            try:
                reply = await self._client(fresh, extra_headers=headers or None).forward(
                    path, payload, stream=stream
                )
            except ProviderError as exc:
                last_error = exc
                logger.warning("Codar 续期后网络层失败（%s），换下一个账号", exc.message)
                continue
            if _retryable(reply.status_code) != "refresh":
                await self._scheduler.note_success(fresh)
                return reply
            last_reply = reply
            await reply.aclose()
        # 401 全部失效要明确抛错；429/5xx 保留最后一次上游响应原样透传（含 retry-after）
        if last_reply is not None and last_reply.status_code != 401:
            return last_reply
        if last_error is not None:
            raise last_error
        raise ProviderError(
            401,
            "Codar JWT 全部失效：过期请重新粘贴新的 JWT；无效请确认粘贴的是 Codar 账号 JWT（不是其他 token）。",
        )

    async def _json_with_tokens(self, path: str) -> dict[str, Any]:
        plan = await self._rotation()
        if not plan.indices:
            raise ProviderError(
                401,
                "Codar 尚未导入账号 JWT：请在服务商页凭据库粘贴 Codar 账号 JWT；"
                "扫码/短信登录只能走 codar2api sidecar，本页不支持。",
            )
        attempts = 0
        last_error: ProviderError | None = None
        while plan.indices and attempts < MAX_TOKEN_ATTEMPTS:
            index = plan.indices.pop(0)
            attempts += 1
            token = await self._current_token(index)
            try:
                return await self._client(token, timeout=30.0).request_json("GET", path)
            except ProviderError as exc:
                last_error = exc
                if exc.status_code != 401:
                    raise
                await self._scheduler.note_failure(token, exc.status_code)
                fresh = await self._refresh(index, token)
                if not fresh:
                    continue
                attempts += 1
                if attempts > MAX_TOKEN_ATTEMPTS:
                    break
                try:
                    return await self._client(fresh, timeout=30.0).request_json("GET", path)
                except ProviderError as inner:
                    last_error = inner
                    if inner.status_code != 401:
                        raise
        raise last_error or ProviderError(
            401,
            "Codar JWT 全部失效：过期请重新粘贴新的 JWT；无效请确认粘贴的是 Codar 账号 JWT（不是其他 token）。",
        )

    def validate_token(self, token: str = "") -> dict[str, Any]:
        """本地判断 JWT 有效/过期/无效/未配置；不发起网络请求。"""
        return inspect_token(token)

    async def fetch_models(self) -> list[dict[str, Any]]:
        data = await self._json_with_tokens(CATALOG_PATH)
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        return [
            {"id": str(item["id"]), "object": "model", "owned_by": "codar"}
            for item in items
            if isinstance(item, dict) and item.get("id")
        ]

    async def health(self) -> dict[str, Any]:
        tokens = self._configured_tokens()
        if not tokens:
            return {"connected": False, "error": "未配置 Codar 账号 token（服务商页 api_key 或 options.tokens）"}
        try:
            data = await self._json_with_tokens(CATALOG_PATH)
        except ProviderError as exc:
            return {"connected": False, "error": exc.message}
        items = data.get("data") if isinstance(data, dict) else None
        count = len(items) if isinstance(items, list) else 0
        rotation = await self._scheduler.snapshot(tokens)
        base = {"rotation": rotation, "cooling": rotation.get("cooling", 0)}
        if count:
            return {"connected": True, "models": count, **base}
        return {"connected": True, "models": 0, "note": "上游在线但模型目录为空", **base}

    async def chat_completion(self, payload: dict[str, Any], *, stream: bool):
        return await self._forward_with_tokens(
            "/v1/chat/completions", self._transform_chat(payload), stream=stream, session_payload=payload
        )

    async def messages(self, payload: dict[str, Any], *, stream: bool):
        headers = {"anthropic-version": ANTHROPIC_VERSION}
        beta = str(self.options.get("anthropic_beta") or "").strip()
        if beta:
            headers["anthropic-beta"] = beta
        return await self._forward_with_tokens(
            "/v1/messages", payload, stream=stream, extra_headers=headers, session_payload=payload
        )

    async def responses(self, payload: dict[str, Any], *, stream: bool):
        return await self._forward_with_tokens(
            "/v1/responses", payload, stream=stream, session_payload=payload
        )

    async def count_tokens(self, payload: dict[str, Any], *, stream: bool = False):
        # Anthropic 计数端点与 messages 同一套鉴权头；流式参数对计数无意义，固定非流式
        headers = {"anthropic-version": ANTHROPIC_VERSION}
        beta = str(self.options.get("anthropic_beta") or "").strip()
        if beta:
            headers["anthropic-beta"] = beta
        return await self._forward_with_tokens(
            "/v1/messages/count_tokens", payload, stream=False, extra_headers=headers, session_payload=payload
        )


def build(settings: Settings, base_url: str = "") -> CodarAdapter:
    return CodarAdapter(settings, base_url)
