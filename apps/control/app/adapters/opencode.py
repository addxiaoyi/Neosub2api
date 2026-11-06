"""OpenCode Zen/Go 网关适配器（clean-room 重写）。

按 docs/providers/opencode2api.md 提炼的协议契约独立实现：模型发现、Chat 与
Anthropic Messages 转发、SSE 直通、可重试状态码换 key。不依赖 opencode2api 进程。
"""

import asyncio
import logging
from typing import Any

import httpx

from ..api_utils import exc_summary, safe_url
from ..config import Settings
from ..services.rotation import CredentialPool, RotationStore, pick_in_group, sticky_key_from_payload
from .base import ProviderAdapter, ProviderError, ProviderSpec, stub_capabilities
from .client import UnifiedApiClient

logger = logging.getLogger("unified.upstream")

# 请求级错误不重试，换 key/出口也改变不了结果
REQUEST_ERRORS = {400, 413, 422}
# 与池分档同集：401/403 鉴权、402 余额、408 超时、429 限流、5xx 服务端，全部换号并登记冷却
POOL_COOLDOWN_STATUSES = {401, 402, 403, 408, 429, 500, 502, 503, 504}
RETRYABLE = POOL_COOLDOWN_STATUSES
CLIENT_UA = "opencode/0.6.2"
# 只有鉴权类失败才值得改走匿名通道：网络/5xx 换不换凭证结果一样
AUTH_ERRORS = {401, 403}
NO_KEY_MESSAGE = "OpenCode 未配置上游密钥（服务商页填写 api_key 或 options.extra_keys）"
# 网络层错误与可重试状态码走同一套换 key 退避；连接失败/超时不是密钥问题也是出口问题
NETWORK_ERRORS = (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ConnectTimeout)


class _TrackedReply:
    """流式响应包装：读到正常收尾才回写健康度，中途断流只解绑粘性。

    拿到 200 响应头就记成功，会把随后上游断开的事件掩盖掉：该冷却的账号不会被
    冷却，后续请求还会继续撞同一把坏号（与 trae 流式口径一致）。
    """

    def __init__(self, inner, on_done, on_break) -> None:
        self.status_code = inner.status_code
        self.headers = inner.headers
        self.body = inner.body
        self.response = inner.response
        self._inner = inner
        self._on_done = on_done
        self._on_break = on_break

    async def iter_bytes(self):
        completed = False
        try:
            async for chunk in self._inner.iter_bytes():
                yield chunk
            completed = True
        except Exception:  # noqa: BLE001 - 断流/连接错误按出口问题处理，不判死账号
            await self._on_break()
            raise
        finally:
            if completed:
                await self._on_done()

    async def aclose(self) -> None:
        await self._inner.aclose()


class OpenCodeAdapter(ProviderAdapter):
    spec = ProviderSpec(
        id="opencode",
        name="OpenCode 2API",
        vendor="jasonxu114514/opencode2api（协议参考）",
        license="本仓自研（MIT）",
        runtime="主仓内置（直连 Zen/Go）",
        status="implemented",
        description="按公开协议契约重写的 Zen/Go 客户端：/v1/models 与 chat/messages 转发，SSE 直通。",
        default_base_url="https://opencode.ai/zen",
        capabilities=stub_capabilities(chat=True, messages=True, models=True),
        protocols=("openai.chat.completions", "anthropic.messages"),
        user_agent=CLIENT_UA,
        risk_level="warn",
        risk_note="上游 opencode2api 为 GPL-3.0，只能进程隔离；"
        "主仓实现为按协议 clean-room 重写，不含上游源码。商用前须确认隔离边界。",
        readiness="needs_config",
        requires=(
            "OpenCode Zen/Go 密钥（zen_keys / go_keys，静态无 OAuth 刷新；填 api_key 或 options.extra_keys）",
            "真实外呼未验证：需带 key 跑通 /v1/models 与 /v1/chat/completions 各一条",
        ),
    )

    def __init__(self, settings: Settings, base_url: str = "") -> None:
        self._settings = settings
        # 轮转组：组内粘滞成员落库，重启后同一组仍命中同一个号
        self._pool = CredentialPool()
        self._store = RotationStore(getattr(settings, "db_path", None))
        super().__init__(base_url)

    def _group(self) -> str:
        raw = self.options.get("group") or self.options.get("rotation_group") or "default"
        return str(raw).strip()[:64] or "default"

    def _tier_prefix(self) -> str:
        tier = str(self.options.get("tier") or "zen").strip().lower()
        # base_url 已经写到 /zen/go 时不再重复拼 /go
        if tier == "go" and not self.base_url.rstrip("/").endswith("/go"):
            return "/go"
        return ""

    def _keys(self) -> list[str]:
        raw = [self.api_key, *(self.api_keys or []), *(self.options.get("extra_keys") or [])]
        keys: list[str] = []
        for item in raw:
            key = str(item or "").strip()
            if key and key not in keys:
                keys.append(key)
        return keys

    def _attempts(self, key_count: int) -> int:
        configured = int(self.options.get("max_attempts") or 3)
        return max(1, min(configured, 10, max(key_count, 1) * 2))

    def readiness_now(
        self,
        *,
        has_credential: bool,
        credential_problem: str,
        last_probe: dict[str, Any] | None,
        credential: str = "",
        options: dict[str, Any] | None = None,
    ) -> tuple[str, tuple[str, ...]]:
        extra = (options or {}).get("extra_keys")
        if isinstance(extra, str):
            extra_keys = [part.strip() for part in extra.replace(",", " ").split()]
        elif isinstance(extra, (list, tuple)):
            extra_keys = [str(item).strip() for item in extra]
        else:
            extra_keys = []
        if credential_problem:
            return "needs_config", (credential_problem,)
        if has_credential or any(key for key in extra_keys):
            return "ready", ()
        # 一家都还没填：列出真实卡点，别只报 api_key 而漏掉「外呼未验证」
        return "needs_config", self.spec.requires

    def _client(self, key: str, *, anthropic: bool, timeout: float | None = None) -> UnifiedApiClient:
        headers = dict(self.options.get("extra_headers") or {}) if isinstance(self.options.get("extra_headers"), dict) else {}
        headers.setdefault("user-agent", self.client_user_agent() or CLIENT_UA)
        if anthropic:
            headers.setdefault("anthropic-version", "2023-06-01")
            if key:
                headers.setdefault("x-api-key", key)
        return UnifiedApiClient(
            self.base_url,
            api_key=key,
            timeout=timeout or float(self.options.get("timeout") or 300.0),
            proxy=str(self.options.get("proxy") or ""),
            extra_headers=headers,
        )

    async def _rotate_away(self, key: str) -> None:
        """网络层失败：解绑粘性并清掉轮转组当前成员，下一次尝试换号。

        不进冷却表：单次连接失败不代表账号坏，冷却反而会把可用号关在门外。
        """
        if not key:
            return
        await self._pool.report_network_error(key)
        self._store.clear("opencode", self._group())

    async def _note_failure(self, key: str, status: int, keys: list[str]) -> bool:
        """登记分档冷却；没有别的可用账号时不冷却，避免把唯一账号关进冷却后请求全 429。"""
        others = [item for item in keys if item != key]
        if others and await self._pool.available(others):
            await self._pool.report_failure(key, status)
            return True
        return False

    def _tracked(self, inner, key: str, sticky_key: str):
        async def done() -> None:
            await self._pool.report_success(key)
            if sticky_key:
                await self._pool.bind_sticky(sticky_key, key)

        async def broke() -> None:
            await self._pool.report_network_error(key)

        return _TrackedReply(inner, done, broke)

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        *,
        stream: bool,
        anthropic: bool,
        anonymous: bool = False,
        sticky_key: str = "",
    ):
        # 匿名通道只给模型发现用：上游 /v1/models 实测不带凭证也返回 200，换 key 对它没意义
        keys = [""] if anonymous else self._keys()
        if not keys:
            raise ProviderError(503, NO_KEY_MESSAGE)
        if not anonymous:
            # 权重每次请求前同步：options 改动后无需重建适配器
            self._pool.set_weights(keys, self.options.get("key_weights"))
        attempts = 1 if anonymous else self._attempts(len(keys))
        last: Any = None
        # 记录最后一次连接错误的原文，重试耗尽后如实抛给调用方，不伪造空响应
        network_error: ProviderError | None = None
        for attempt in range(attempts):
            if anonymous:
                key, rule = "", "anonymous"
            else:
                pick = await pick_in_group(
                    self._pool, self._store,
                    provider="opencode", group=self._group(), keys=keys, sticky_key=sticky_key,
                )
                if pick is None:
                    raise ProviderError(429, "OpenCode 轮转组内账号全部处于冷却中，请稍后重试")
                key, rule = pick.key, pick.rule
                if len(keys) > 1:
                    logger.info("OpenCode 选号 group=%s cred=%s 规则=%s", self._group(), pick.cred_id, rule)
            client = self._client(key, anthropic=anthropic)
            try:
                if method == "GET":
                    data = await client.get_json(path)
                    if key:
                        await self._pool.report_success(key)
                    return data
                reply = await client.forward(path, payload or {}, stream=stream)
            except ProviderError as exc:
                # client 抛出的 ProviderError 是连接层（502「上游不可达」）或读到的错误体；
                # 5xx 归为出口问题只解绑粘性，鉴权/限流类按池分档冷却
                last = exc
                if exc.status_code in REQUEST_ERRORS:
                    raise
                if exc.status_code >= 500:
                    network_error = exc
                    await self._rotate_away(key)
                elif key and exc.status_code in POOL_COOLDOWN_STATUSES:
                    await self._note_failure(key, exc.status_code, keys)
            except NETWORK_ERRORS as exc:
                last = network_error = ProviderError(502, f"上游连接失败（{safe_url(self.base_url)}）：{exc_summary(exc)}")
                await self._rotate_away(key)
            else:
                if reply.status_code < 400:
                    if stream:
                        # 流式成功只在读完后再回写，避免把中途断流记成健康
                        return self._tracked(reply, key, sticky_key if key else "")
                    await self._pool.report_success(key)
                    if sticky_key:
                        await self._pool.bind_sticky(sticky_key, key)
                    return reply
                last = reply
                if reply.status_code not in RETRYABLE:
                    return reply
                # 冷却按事实登记：即便这是最后一次尝试，健康度也要如实反映坏号
                cooled = await self._note_failure(key, reply.status_code, keys)
                if attempt == attempts - 1:
                    return reply
                # 没有替代账号时不换号也不冷却，原样透传这次响应
                if not cooled:
                    return reply
                await reply.aclose()
            # 500ms / 1s / 2s 退避，避免把上游限流打成雪崩
            await asyncio.sleep(min(0.5 * (2 ** attempt), 2.0))
        if network_error is not None:
            raise ProviderError(
                502,
                f"{network_error.message}；已轮换 {attempts} 次密钥仍未连通",
                upstream_code=network_error.upstream_code,
                upstream_message=network_error.upstream_message,
            )
        if isinstance(last, ProviderError):
            raise last
        return last

    async def _discover(self, path: str) -> dict[str, Any]:
        """模型发现与健康探测：设计入口带本地 key，被拒或没配时改走匿名通道。

        匿名通道不保证成功（docs/providers/opencode2api.md §4），仅作为发现类请求的
        兜底；推理调用不走这条路径。
        """
        keys = self._keys()
        if keys:
            try:
                return await self._request("GET", path, None, stream=False, anthropic=False)
            except ProviderError as exc:
                if exc.status_code not in AUTH_ERRORS:
                    raise
                logger.warning("OpenCode 模型发现带密钥被拒（%s），改走匿名通道", exc.message)
        try:
            return await self._request("GET", path, None, stream=False, anthropic=False, anonymous=True)
        except ProviderError as exc:
            # 匿名也被拒时，缺密钥的场景仍报那句可操作提示，而不是丢一个上游 401
            if not keys and exc.status_code in AUTH_ERRORS:
                raise ProviderError(503, NO_KEY_MESSAGE) from exc
            raise

    async def fetch_models(self) -> list[dict[str, Any]]:
        data = await self._discover(f"{self._tier_prefix()}/v1/models")
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict) and item.get("id")]

    async def health(self) -> dict[str, Any]:
        rotation = await self._pool.snapshot(self._keys(), group=self._group())
        try:
            data = await self._discover(f"{self._tier_prefix()}/v1/models")
        except ProviderError as exc:
            return {
                "connected": False,
                "error": exc.message,
                "rotation": rotation,
                "cooling": rotation.get("cooling", 0),
            }
        items = data.get("data") if isinstance(data, dict) else []
        count = len(items) if isinstance(items, list) else 0
        # 探测过程本身会改写池状态，快照以探测后为准
        rotation = await self._pool.snapshot(self._keys(), group=self._group())
        result: dict[str, Any] = {"connected": True, "models": count, "tier": str(self.options.get("tier") or "zen")}
        result["rotation"] = rotation
        result["cooling"] = rotation.get("cooling", 0)
        if count == 0:
            result["note"] = "上游在线但模型列表为空（可能是密钥无可用模型）"
        return result

    async def chat_completion(self, payload: dict[str, Any], *, stream: bool):
        return await self._request(
            "POST", f"{self._tier_prefix()}/v1/chat/completions", payload, stream=stream, anthropic=False,
            sticky_key=sticky_key_from_payload(payload),
        )

    async def messages(self, payload: dict[str, Any], *, stream: bool):
        return await self._request(
            "POST", f"{self._tier_prefix()}/v1/messages", payload, stream=stream, anthropic=True,
            sticky_key=sticky_key_from_payload(payload),
        )

    def close(self) -> None:
        """释放轮转组 sqlite 连接：Windows 上删除临时库要求文件句柄先归还。"""
        self._store.close()


def build(settings: Settings, base_url: str = "") -> OpenCodeAdapter:
    return OpenCodeAdapter(settings, base_url)
