"""可观测性基础设施：请求 id 贯穿 + text/json 两种日志格式。

开关 `UNIFIED_LOG_FORMAT=text|json`，默认 text：text 模式只给新增的
`unified.access` 挂一个 handler，不动既有的 root/uvicorn formatter，
所以历史输出完全不变。json 模式把 root 与 uvicorn 三个 logger 的
formatter 换成单行 JSON，字段固定为 time / level / logger / message /
request_id / path / status / duration_ms。

脱敏口径与 main.py 的 `_AccessLogRedactor` 同源：`api_utils.mask_text`。
formatter 先脱敏 message，再对整行 JSON 做一次兜底替换（见 `mask_text`
的 `u2a_` 规则），因此 json 模式下 u2a_ 密钥同样不会以明文落盘。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from collections import deque
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlsplit

from .api_utils import mask_text

REQUEST_ID_HEADER = "X-Request-Id"
LOG_FORMAT_ENV = "UNIFIED_LOG_FORMAT"
TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

# 进程内指标：Prometheus 抓取要的是真实运行时状态，而不是再从日志表重算一遍。
# 端点/耗时这两个维度 request_logs 里没有（v1 只写 provider/model/status/latency），
# 所以在访问日志中间件里顺手累计；这些值随进程重启归零，与官方客户端库语义一致。
DURATION_BUCKETS_MS = (10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, float("inf"))
DURATION_SAMPLE_SIZE = 5000

_metric_lock = threading.Lock()
_http_counts: dict[tuple[str, str, str, str], int] = {}
_duration_bucket_counts: list[int] = [0] * len(DURATION_BUCKETS_MS)
_duration_samples: deque[float] = deque(maxlen=DURATION_SAMPLE_SIZE)
_duration_count = 0
_duration_sum_ms = 0.0

# 只接受可安全回写的字符；其余（换行、引号、控制字符）一律重新生成
_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9._:\-]{1,128}")

# 请求级上下文：中间件写入，formatter 读取，避免每条日志手动带 request id
_ctx: ContextVar[dict[str, str]] = ContextVar("u2a_request_ctx", default={})

access_logger = logging.getLogger("unified.access")

_RESERVED_ATTRS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
    "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
    "relativeCreated", "thread", "threadName", "processName", "process", "taskName",
    "message", "asctime", "color_message",
}


def request_id_from(raw: str | None) -> str:
    """透传调用方 X-Request-Id；缺失或含非法字符时重新生成。"""
    text = (raw or "").strip()
    return text if _SAFE_REQUEST_ID.fullmatch(text) else uuid.uuid4().hex


def bind_request(request_id: str, path: str):
    return _ctx.set({"request_id": request_id, "path": path})


def reset_request(token) -> None:
    _ctx.reset(token)


def current_context() -> dict[str, str]:
    return dict(_ctx.get())


def status_class(status: int) -> str:
    return f"{status // 100}xx" if 100 <= int(status) <= 599 else "unknown"


def record_http(method: str, endpoint: str, status: int, duration_ms: float,
                provider: str = "") -> None:
    """累计进程内 HTTP 指标；endpoint 传路由模板，避免把路径参数带进 label。

    provider 取自响应头 x-upstream-provider（v1 网关会写），控制面请求记为 "-"。
    """
    global _duration_count, _duration_sum_ms
    label = (method.upper() or "-", endpoint or "/", status_class(status), provider or "-")
    elapsed = max(0.0, float(duration_ms))
    with _metric_lock:
        _http_counts[label] = _http_counts.get(label, 0) + 1
        for index, boundary in enumerate(DURATION_BUCKETS_MS):
            if elapsed <= boundary:
                _duration_bucket_counts[index] += 1
                break
        _duration_samples.append(elapsed)
        _duration_count += 1
        _duration_sum_ms += elapsed


def http_metrics_snapshot() -> dict[str, Any]:
    with _metric_lock:
        return {
            "requests": [
                (method, endpoint, state, provider, count)
                for (method, endpoint, state, provider), count in _http_counts.items()
            ],
            "duration_buckets": list(_duration_bucket_counts),
            "duration_count": _duration_count,
            "duration_sum_ms": _duration_sum_ms,
            "duration_samples": sorted(_duration_samples),
        }


def sample_quantile(sorted_values: list[float], quantile: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, max(0, int(round((len(sorted_values) - 1) * quantile))))
    return sorted_values[index]


def log_access(method: str, path: str, status: int, duration_ms: float, request_id: str) -> None:
    """访问日志：text 模式是可读单行，json 模式由 formatter 展开成结构化字段。"""
    access_logger.info(
        "%s %s -> %s (%dms) request_id=%s",
        method, path, status, int(duration_ms), request_id or "-",
        extra={
            "request_id": request_id,
            "path": path,
            "status": int(status),
            "duration_ms": round(float(duration_ms), 2),
        },
    )


def _uvicorn_access_args(record: logging.LogRecord) -> tuple:
    """uvicorn.access 的 args 是 (client, method, full_path, http_version, status)。"""
    if record.name != "uvicorn.access" or not isinstance(record.args, tuple):
        return ()
    return record.args if len(record.args) >= 5 else ()


def _access_path(record: logging.LogRecord) -> str:
    args = _uvicorn_access_args(record)
    # 查询串不落日志的 path 字段：既避免密钥，也让 path 可聚合
    return urlsplit(str(args[2])).path if args else ""


def _access_status(record: logging.LogRecord) -> Any:
    args = _uvicorn_access_args(record)
    return args[4] if args else None


class JsonFormatter(logging.Formatter):
    """单行 JSON formatter；缺字段就不输出，绝不编造。"""

    def format(self, record: logging.LogRecord) -> str:
        ctx = _ctx.get()
        payload: dict[str, Any] = {
            "time": self._iso(record.created),
            "level": record.levelname,
            "logger": record.name,
            "message": mask_text(record.getMessage()),
            "request_id": str(getattr(record, "request_id", "") or ctx.get("request_id", "")),
            "path": str(getattr(record, "path", "") or ctx.get("path", "") or _access_path(record)),
        }
        status = getattr(record, "status", None)
        if status is None:
            status = _access_status(record)
        if status is not None:
            payload["status"] = int(status)
        duration = getattr(record, "duration_ms", None)
        if duration is not None:
            payload["duration_ms"] = round(float(duration), 2)
        if record.exc_info:
            payload["exc"] = mask_text(self.formatException(record.exc_info))
        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS or key in payload:
                continue
            payload[key] = value if isinstance(value, (str, int, float, bool, type(None))) else str(value)
        # 整行再扫一遍：message/extra 里零散拼接的密钥也在这一步兜住
        return mask_text(json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":")))

    @staticmethod
    def _iso(created: float) -> str:
        base = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(created))
        return f"{base}.{int(created % 1 * 1000):03d}"


_text_handler_ready = False


def _ensure_text_handler() -> None:
    """text 模式：只让新增的访问日志可见，不接管既有日志配置。"""
    global _text_handler_ready
    if _text_handler_ready:
        return
    if not access_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(TEXT_FORMAT))
        access_logger.addHandler(handler)
        access_logger.propagate = False
    _text_handler_ready = True


def configure_logging() -> str:
    """按 UNIFIED_LOG_FORMAT 切换；可重复调用（幂等）。"""
    # 访问日志是 INFO：root 缺省 WARNING，不显式设级别会被静默吞掉
    access_logger.setLevel(logging.INFO)
    mode = (os.getenv(LOG_FORMAT_ENV) or "text").strip().lower()
    if mode != "json":
        _ensure_text_handler()
        return "text"
    formatter = JsonFormatter()
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    for target in (
        root,
        logging.getLogger("uvicorn"),
        logging.getLogger("uvicorn.error"),
        logging.getLogger("uvicorn.access"),
    ):
        for handler in target.handlers:
            handler.setFormatter(formatter)
    return "json"
