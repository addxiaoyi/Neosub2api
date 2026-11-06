"""凭据状态：按已存证据把「这条凭据」分成 有效 / 缺失 / 已失效 / 未知。

证据来源只有三处，全部是既成事实，不做任何推测、不发起外呼：
- 上一次探活（provider_health_checks 的最近一条）；
- 上一次真实请求留下的鉴权失败（request_logs 里 401/403 的行）；
- 本地可判的确定性结论（JWT 的 exp 已过期、导入前格式校验不通过）。

时间顺序有意义：凭据在上一次证据之后被重新粘贴过，旧证据就不能再算数，
否则用户明明换了新 key，页面还一直挂着「已失效」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

VALID = "valid"
MISSING = "missing"
INVALIDATED = "invalidated"
UNKNOWN = "unknown"
STATES = (VALID, MISSING, INVALIDATED, UNKNOWN)

# 证据类型：探活 / 真实请求 / 本地过期判定 / 本地格式判定 / 无
EVIDENCE_PROBE = "probe"
EVIDENCE_REQUEST = "request"
EVIDENCE_EXPIRY = "expiry"
EVIDENCE_FORMAT = "format"
EVIDENCE_NONE = "none"

# 探活能不能直接带出 401/403，决定了这家是否可能自己判出「已失效」
SUPPORT_PROBE = "auth_probe"
SUPPORT_EXPIRY = "token_expiry"
SUPPORT_REQUEST = "request_log"
SUPPORT_NONE = "unavailable"

# 只能给「未知」时写清为什么，前端原样展示
SUPPORT_NOTES = {
    SUPPORT_REQUEST: "探活不回传 401/403，只能依据最近一次真实请求的鉴权失败留痕判断；没有留痕时状态为未知",
    SUPPORT_NONE: "探活与真实请求都拿不到鉴权失败证据，凭据是否失效只能人工确认",
}


@dataclass(frozen=True)
class CredentialState:
    state: str
    reason: str
    evidence: str
    checked_at: int | None

    def to_public(self, support: str) -> dict[str, Any]:
        return {
            "credential_state": self.state,
            "credential_state_reason": self.reason,
            "credential_state_checked_at": self.checked_at,
            "credential_state_evidence": self.evidence,
            "credential_state_support": support,
            "credential_state_note": SUPPORT_NOTES.get(support, ""),
        }


def is_jwt(text: str) -> bool:
    """三段式 base64url 即按 JWT 看待；只用于解释「本地能否判过期」，不做校验。"""
    raw = _strip_bearer(text)
    parts = raw.split(".")
    return len(parts) == 3 and bool(parts[0]) and bool(parts[1])


def _strip_bearer(text: str) -> str:
    raw = (text or "").strip()
    return raw[7:].strip() if raw.lower().startswith("bearer ") else raw


def _probe_event(probe: dict[str, Any] | None) -> tuple[int, str, str] | None:
    """把最近一次探活压成 (时刻, 结论, 说明)；没有探活返回 None。"""
    if not probe:
        return None
    checked_at = int(probe.get("checked_at") or 0)
    if not checked_at:
        return None
    status = str(probe.get("status") or "")
    # 鉴权失败优先于「连通性 ok」：管理端可能在线但明确拒了这把 token
    if probe.get("auth_failed"):
        upstream = probe.get("upstream_status")
        hint = f"上游返回 {upstream}" if upstream else "上游判定凭据被拒"
        return checked_at, "auth", hint
    if status == "ok":
        return checked_at, "ok", "上次探活通过"
    if status == "unsupported":
        return checked_at, "unsupported", "该服务商没有可探测的健康检查"
    if status == "failed":
        detail = str(probe.get("error") or "").strip()
        return checked_at, "failed", f"上次探活失败：{detail}" if detail else "上次探活失败"
    return None


def classify(
    *,
    has_credential: bool,
    format_problem: str = "",
    expiry_problem: str = "",
    probe: dict[str, Any] | None = None,
    auth_failure_at: int | None = None,
    credential_updated_at: int = 0,
) -> CredentialState:
    """按证据判定凭据状态。credential_updated_at 用于让重新粘贴后的旧证据失效。"""
    if not has_credential:
        return CredentialState(MISSING, "未配置凭据", EVIDENCE_NONE, None)
    if expiry_problem:
        return CredentialState(INVALIDATED, expiry_problem, EVIDENCE_EXPIRY, None)
    if format_problem:
        return CredentialState(
            INVALIDATED, f"凭据不可用，需要重新粘贴：{format_problem}", EVIDENCE_FORMAT, None
        )

    events: list[tuple[int, str, str, str]] = []
    probe_event = _probe_event(probe)
    if probe_event:
        events.append((*probe_event, EVIDENCE_PROBE))
    if auth_failure_at:
        events.append(
            (int(auth_failure_at), "auth", "最近一次真实请求被上游以鉴权错误拒绝", EVIDENCE_REQUEST)
        )
    if not events:
        return CredentialState(
            UNKNOWN, "从未探活，也没有失败的请求记录，凭据是否可用尚未验证", EVIDENCE_NONE, None
        )

    checked_at, kind, detail, evidence = max(events, key=lambda item: item[0])
    if credential_updated_at and credential_updated_at > checked_at:
        return CredentialState(
            UNKNOWN,
            "凭据在上一次证据之后更新过，尚未重新验证",
            EVIDENCE_NONE,
            None,
        )
    if kind == "auth":
        return CredentialState(INVALIDATED, f"凭据已失效，需要重新粘贴：{detail}", evidence, checked_at)
    if kind == "ok":
        return CredentialState(VALID, "上次探活通过", evidence, checked_at)
    return CredentialState(UNKNOWN, detail, evidence, checked_at)
