import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .adapters.base import ProviderError
from .api_utils import mask_access_args
from .config import ADMIN_PASSWORD_FILE, LEGACY_ADMIN_PASSWORD, load_settings
from .db import Database
from .gateway_errors import PROTOCOL_ANTHROPIC, GatewayError, framework_payload, protocol_for_path
from .observability import (
    REQUEST_ID_HEADER,
    bind_request,
    configure_logging,
    log_access,
    record_http,
    request_id_from,
    reset_request,
)
from .registry import ProviderRegistry
from .routers import (
    accounts,
    auth,
    backup,
    grok,
    health,
    keys,
    logs,
    models,
    pricing,
    prompts,
    provider_credentials,
    providers,
    qoder_sessions,
    quota,
    security as security_router,
    settings as settings_router,
    stats,
    sub2api,
    system,
    tokens,
    v1,
)
from .security import hash_password, verify_password
from .services.grok_register import GrokRegisterService
from .services.provider_health import ProviderHealthService
from . import demo

logger = logging.getLogger("unified.security")


class _AccessLogRedactor(logging.Filter):
    """uvicorn 访问日志会记录完整 URL，落盘前把疑似 u2a_ 片段替换成掩码。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.args = mask_access_args(record.args)
        return True


logging.getLogger("uvicorn.access").addFilter(_AccessLogRedactor())


WEAK_PASSWORDS = {"unified-admin", "admin", "password", "123456", "12345678", "changeme"}


def _warn_if_weak(settings) -> None:
    """库里的管理员口令就是 settings 里这个时才有意义，别的来源无法读到明文。"""
    secret = settings.admin_password
    if len(secret) < 8 or secret.strip().lower() in WEAK_PASSWORDS:
        logger.warning("管理员口令偏弱，建议通过 UNIFIED_ADMIN_PASSWORD 或删除 %s 后重启更换", ADMIN_PASSWORD_FILE)


def _seed_admin(db: Database, settings) -> None:
    """首次建库写入管理员；已有库按口令来源决定是否轮换。

    环境变量是显式配置，和库里不一致就覆盖；文件/随机口令是权威来源，
    只覆盖历史默认口令 `unified-admin`，不碰运维自己设过的密码。
    """
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT username, password_hash FROM users WHERE role = 'admin' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users(username, password_hash, role, created_at) VALUES(?,?,?,?)",
                (settings.admin_username, hash_password(settings.admin_password), "admin", int(time.time())),
            )
            conn.commit()
            _announce_password(settings, created=True)
            _warn_if_weak(settings)
            return
        if settings.admin_password_source == "env":
            matches = verify_password(settings.admin_password, row["password_hash"])
            needs_rotate = not matches
        else:
            matches = False  # 无法读到库里的明文
            needs_rotate = verify_password(LEGACY_ADMIN_PASSWORD, row["password_hash"])
        if not needs_rotate:
            if matches:
                _warn_if_weak(settings)
            return
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (hash_password(settings.admin_password), row["username"]),
        )
        conn.execute("DELETE FROM sessions WHERE username = ?", (row["username"],))
        conn.commit()
        if settings.admin_password_source == "env":
            logger.warning("已按 UNIFIED_ADMIN_PASSWORD 轮换管理员「%s」口令，旧会话全部失效", row["username"])
        else:
            logger.warning("检测到历史默认口令，已轮换管理员「%s」口令，旧会话全部失效", row["username"])
            _announce_password(settings, created=False)
        _warn_if_weak(settings)
    finally:
        conn.close()


def _announce_password(settings, *, created: bool) -> None:
    if settings.admin_password_source == "generated":
        logger.warning(
            "首次启动：已随机生成管理员口令并写入 %s（权限 0600）。口令：%s",
            settings.data_dir / ADMIN_PASSWORD_FILE,
            settings.admin_password,
        )
    elif settings.admin_password_source == "file":
        logger.info("管理员口令来自 %s（如遗忘可删除该文件后重启重新生成）", settings.data_dir / ADMIN_PASSWORD_FILE)
    elif created:
        logger.info("管理员口令来自 UNIFIED_ADMIN_PASSWORD")


def create_app() -> FastAPI:
    # 日志格式在第一个 logger 使用前确定：UNIFIED_LOG_FORMAT=json 时全局换 formatter
    configure_logging()
    settings = load_settings()
    db = Database(settings.db_path)
    _seed_admin(db, settings)
    registry = ProviderRegistry(db, settings)
    grok_service = GrokRegisterService(settings)
    # 健康检测与 grok 队列同一机制：lifespan 里 start/stop 的 asyncio 周期任务
    health_service = ProviderHealthService(settings, db, registry)
    # 演示模式：默认关闭；开启时先校验回环地址，再起进程内假上游并注册 demo provider
    demo_runtime = demo.install(settings, db, registry) if settings.demo_mode else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if demo_runtime is not None:
            await demo_runtime.seed()
        grok_service.start()
        health_service.start()
        try:
            yield
        finally:
            await health_service.stop()
            await grok_service.stop()
            if demo_runtime is not None:
                demo_runtime.stop()

    app = FastAPI(
        title="unified-2api control plane",
        version=__version__,
        lifespan=lifespan,
        # 生产默认不暴露接口文档；本地调试显式 UNIFIED_ENABLE_DOCS=1（S-09）
        docs_url="/docs" if settings.enable_docs else None,
        redoc_url="/redoc" if settings.enable_docs else None,
        openapi_url="/openapi.json" if settings.enable_docs else None,
    )
    app.state.settings = settings
    app.state.db = db
    app.state.registry = registry
    app.state.grok_service = grok_service
    app.state.health_service = health_service
    app.state.demo = demo_runtime

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=True,
        # 精确放行管理面实际用到的方法/头，避免配置放宽后 allow_* 全通（S-11）
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["authorization", "content-type", "x-provider", "x-requested-with"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        """服务端安全头兜底；反代层没配时也能挡住 iframe 嵌入与 MIME 嗅探（S-16）。"""
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault(
            "Content-Security-Policy", "frame-ancestors 'none'; object-src 'none'; base-uri 'self'"
        )
        if settings.cookie_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    @app.middleware("http")
    async def request_tracking(request: Request, call_next):
        """request id 贯穿：透传调用方的 X-Request-Id，缺失就生成；回写响应头并落访问日志。

        最后注册 → 包在最外层，安全头/CORS 的响应也能带上这个头。
        """
        request_id = request_id_from(request.headers.get(REQUEST_ID_HEADER))
        token = bind_request(request_id, request.url.path)
        started = time.perf_counter()
        status = 500
        provider = "-"
        try:
            response = await call_next(request)
            status = response.status_code
            provider = response.headers.get("x-upstream-provider", "-") or "-"
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            # 路由模板而不是原始路径：避免把 /api/xxx/<id> 的 id 带进 Prometheus label
            route = request.scope.get("route")
            endpoint = getattr(route, "path", "") or "<unmatched>"
            record_http(request.method, endpoint, status, elapsed_ms, provider)
            log_access(
                request.method,
                request.url.path,
                status,
                elapsed_ms,
                request_id,
            )
            reset_request(token)

    if demo_runtime is not None:

        @app.middleware("http")
        async def demo_loopback_only(request: Request, call_next):
            """兜底：即使有人用 CLI 把 host 改成 0.0.0.0，非本机来源也一律拒绝。"""
            client_host = request.client.host if request.client else ""
            if not demo.is_loopback_host(client_host):
                return JSONResponse(
                    {"detail": "UNIFIED_DEMO_MODE 只接受本机请求，演示数据禁止对外提供服务"},
                    status_code=403,
                )
            return await call_next(request)

    @app.exception_handler(ProviderError)
    async def provider_error_handler(request: Request, exc: ProviderError):
        return JSONResponse({"detail": exc.message}, status_code=exc.status_code)

    @app.exception_handler(GatewayError)
    async def gateway_error_handler(request: Request, exc: GatewayError):
        """/v1 错误按端点协议出形状：messages 走 Anthropic，其余走 OpenAI。"""
        payload = (
            exc.anthropic_payload()
            if protocol_for_path(request.url.path) == PROTOCOL_ANTHROPIC
            else exc.openai_payload()
        )
        return JSONResponse(payload, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def framework_error_handler(request: Request, exc: StarletteHTTPException):
        """框架级错误：/v1/* 的未匹配路由与 405 按协议出壳，其余沿用 {detail}。"""
        path = request.url.path
        if path == "/v1" or path.startswith("/v1/"):
            return JSONResponse(
                framework_payload(path, exc.status_code, exc.detail),
                status_code=exc.status_code,
                headers=exc.headers,
            )
        return await http_error_handler(request, exc)

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException):
        """管理面保持 {detail}；403 额外给结构化原因，前端不必再猜「为什么被拒」。"""
        payload: dict[str, Any] = {"detail": exc.detail}
        if exc.status_code == 403:
            error: dict[str, Any] = {
                "type": "permission_denied",
                "code": "insufficient_role",
                "message": exc.detail if isinstance(exc.detail, str) else "权限不足",
                "required_role": getattr(exc, "required_role", "admin"),
            }
            role = getattr(exc, "role", None)
            if role:
                error["role"] = role
            payload["error"] = error
        return JSONResponse(payload, status_code=exc.status_code, headers=exc.headers)

    for module in (health, auth, providers, provider_credentials, accounts, keys, tokens, models, stats, settings_router,
                   sub2api, pricing, quota, grok, system, backup, v1, logs, prompts, security_router, qoder_sessions):
        app.include_router(module.router)

    @app.get("/healthz")
    def healthz():
        # 不返回数据库路径：未认证端点不该泄露部署目录（S-09）
        return {"ok": True, "providers": len(registry.list_public())}

    if settings.web_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.web_dir, html=True), name="web")
    else:

        @app.get("/")
        def root():
            return {
                "service": "unified-2api control plane",
                "hint": "未找到前端静态产物（apps/web/out），先执行 npm run build:export",
            }

    return app


def run() -> None:
    """直接启动入口：默认只绑 127.0.0.1，需要外部访问时显式设 UNIFIED_HOST（S-03）。"""
    import uvicorn

    settings = load_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level=os.getenv("UNIFIED_LOG_LEVEL", "info"),
    )


app = create_app()


if __name__ == "__main__":
    run()
