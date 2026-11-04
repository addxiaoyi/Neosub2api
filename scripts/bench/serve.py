"""压测专用控制面启动器。

生产代码每发一次上游请求都会新建 httpx.AsyncClient，而 httpx 初始化会重建 SSLContext；
本机 Windows 证书库加载单次约 1.2s（AsyncClient 构造 4~5s），会把网关自身开销完全淹没。
这里只缓存一份 SSLContext（不改任何业务代码），用来隔离该环境噪声；
`load.py --no-ssl-shim` 可回到裸 uvicorn 复现原始数字。
"""

from __future__ import annotations

import ssl
import sys
from pathlib import Path

CONTROL = Path(__file__).resolve().parents[2] / "apps" / "control"
sys.path.insert(0, str(CONTROL))

_cached_context = ssl.create_default_context()


def _reuse_context(*_args, **_kwargs) -> ssl.SSLContext:
    return _cached_context


ssl.create_default_context = _reuse_context

import uvicorn  # noqa: E402  必须在打补丁之后导入


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18127
    log_level = sys.argv[2] if len(sys.argv) > 2 else "warning"
    uvicorn.run("app.main:app", host="127.0.0.1", port=port, log_level=log_level)
