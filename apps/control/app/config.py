import os
import secrets
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# 历史版本用过的固定默认口令：只用于识别，绝不作为实际口令回退
LEGACY_ADMIN_PASSWORD = "unified-admin"
ADMIN_PASSWORD_FILE = "admin-password.txt"


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _admin_password(data_dir: Path) -> tuple[str, str]:
    """管理员口令：环境变量 > data/admin-password.txt > 首次随机生成并落盘。

    不再有固定默认口令；来源写进启动日志，运维知道去哪儿取初始口令。
    """
    env = os.getenv("UNIFIED_ADMIN_PASSWORD", "").strip()
    if env and env != LEGACY_ADMIN_PASSWORD:
        return env, "env"
    path = data_dir / ADMIN_PASSWORD_FILE
    if path.is_file():
        stored = path.read_text(encoding="utf-8").strip()
        if stored:
            return stored, "file"
    generated = secrets.token_urlsafe(24)
    data_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(generated + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return generated, "generated"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    db_path: Path
    web_dir: Path
    admin_username: str
    admin_password: str
    session_ttl_sec: int
    cookie_secure: bool
    codebuddy_base: str
    codebuddy_api_key: str
    opencode_upstream_base: str
    grok_register_executor_url: str
    grok_register_max_concurrency: int
    grok_register_max_workers: int
    allow_anonymous_gateway: bool
    cors_origins: tuple[str, ...]
    admin_password_source: str = "env"
    host: str = "127.0.0.1"
    port: int = 8010
    # 上游地址守卫：默认拒绝环回/私网/链路本地；仅测试或内网部署时显式放开
    allow_private_upstreams: bool = False
    # 流式响应单请求最大时长（秒），0 表示不限制
    sse_max_seconds: int = 600


def load_settings() -> Settings:
    data_dir = Path(os.getenv("UNIFIED_DATA_DIR", str(REPO_ROOT / "data"))).resolve()
    db_path = Path(os.getenv("UNIFIED_DB_PATH", str(data_dir / "unified.db"))).resolve()
    web_dir = Path(os.getenv("UNIFIED_WEB_DIR", str(REPO_ROOT / "apps" / "web" / "out"))).resolve()
    origins = os.getenv("UNIFIED_CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
    password, password_source = _admin_password(data_dir)
    return Settings(
        data_dir=data_dir,
        db_path=db_path,
        web_dir=web_dir,
        admin_username=os.getenv("UNIFIED_ADMIN_USER", "admin"),
        admin_password=password,
        admin_password_source=password_source,
        session_ttl_sec=int(os.getenv("UNIFIED_SESSION_TTL_SEC", "86400")),
        cookie_secure=_bool("UNIFIED_COOKIE_SECURE", False),
        codebuddy_base=os.getenv("CODEBUDDY_BASE", "http://127.0.0.1:7864").rstrip("/"),
        codebuddy_api_key=os.getenv("CODEBUDDY_API_KEY", ""),
        # OpenCode Zen/Go 的直连上游（不再经过 opencode2api 进程；Go 层由 options.tier 选择）
        opencode_upstream_base=os.getenv("OPENCODE_UPSTREAM_BASE", "https://opencode.ai/zen").rstrip("/"),
        # 注册任务执行器：外部浏览器运行时的 HTTP 入口；留空则任务失败而不是假装成功
        grok_register_executor_url=os.getenv("GROK_REGISTER_EXECUTOR_URL", "").rstrip("/"),
        grok_register_max_concurrency=int(os.getenv("GROK_REGISTER_MAX_CONCURRENCY", "2")),
        grok_register_max_workers=int(os.getenv("GROK_REGISTER_MAX_WORKERS", "4")),
        allow_anonymous_gateway=_bool("UNIFIED_ALLOW_ANONYMOUS_GATEWAY", False),
        cors_origins=tuple(o.strip() for o in origins.split(",") if o.strip()),
        host=os.getenv("UNIFIED_HOST", "127.0.0.1").strip() or "127.0.0.1",
        port=int(os.getenv("UNIFIED_PORT", "8010")),
        allow_private_upstreams=_bool("UNIFIED_ALLOW_PRIVATE_UPSTREAMS", False),
        sse_max_seconds=int(os.getenv("UNIFIED_SSE_MAX_SECONDS", "600")),
    )
