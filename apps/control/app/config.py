import os
import secrets
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# 历史版本用过的固定默认口令：只用于识别，绝不作为实际口令回退
LEGACY_ADMIN_PASSWORD = "unified-admin"
ADMIN_PASSWORD_FILE = "admin-password.txt"

# 有 env 地址入口的 provider；用于区分「env 显式配置」与「代码内置默认值」
ENV_BASE_VARS = {
    "codebuddy": "CODEBUDDY_BASE",
    "opencode": "OPENCODE_UPSTREAM_BASE",
    "codar_sidecar": "CODAR_SIDECAR_BASE",
    "trae_sidecar": "TRAE_SIDECAR_BASE",
    "deepseek_sidecar": "DEEPSEEK_SIDECAR_BASE",
    "qoder_sidecar": "QODER_SIDECAR_BASE",
}

# 历史版本写进 providers 表默认地址的旧值；老库可能残留这些种子，启动时只告警、绝不改写
LEGACY_PROVIDER_BASES = {
    "deepseek_sidecar": ("http://127.0.0.1:7872",),
}


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
    # codar2api sidecar 地址；默认值便于 Settings(...) 在测试里省略该字段
    codar_sidecar_base: str = "http://127.0.0.1:8801"
    # 其余三家 sidecar 同链路；默认值与 .env.example 一致
    trae_sidecar_base: str = "http://127.0.0.1:7871"
    deepseek_sidecar_base: str = "http://127.0.0.1:5001"
    qoder_sidecar_base: str = "http://127.0.0.1:7873"
    # OpenAPI 文档默认关闭，生产不暴露 /docs /openapi.json /redoc（S-09）
    enable_docs: bool = False
    # 显式设置了 env 地址的 provider；registry 据此区分 env / default 两种来源
    env_base_pids: frozenset[str] = frozenset()
    # 失败请求滑动窗口限流：同一密钥窗内失败次数达上限即 429；0 表示关闭
    failure_window_sec: int = 60
    failure_window_max: int = 20
    # 上游实拉模型目录的进程内缓存时长（秒）；0 表示不缓存
    models_cache_ttl_sec: int = 60
    # 未知模型名兜底：默认关（404 model_not_found）；打开后回到「用首家启用的、支持该能力位的 provider」
    model_fallback_first: bool = False
    # 演示模式：进程内假上游 + 标注清楚的演示数据，只允许绑定回环地址（详见 docs/DEMO.md）
    demo_mode: bool = False


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
        # codar2api sidecar 的 HTTP 入口；未启动时该 provider 明确显示不可达
        codar_sidecar_base=os.getenv("CODAR_SIDECAR_BASE", "http://127.0.0.1:8801").rstrip("/"),
        trae_sidecar_base=os.getenv("TRAE_SIDECAR_BASE", "http://127.0.0.1:7871").rstrip("/"),
        deepseek_sidecar_base=os.getenv("DEEPSEEK_SIDECAR_BASE", "http://127.0.0.1:5001").rstrip("/"),
        qoder_sidecar_base=os.getenv("QODER_SIDECAR_BASE", "http://127.0.0.1:7873").rstrip("/"),
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
        enable_docs=_bool("UNIFIED_ENABLE_DOCS", False),
        # 失败请求限流：窗口长度与窗内失败上限，两个都 >0 才生效
        failure_window_sec=int(os.getenv("UNIFIED_FAILURE_WINDOW_SEC", "60")),
        failure_window_max=int(os.getenv("UNIFIED_FAILURE_WINDOW_MAX", "20")),
        models_cache_ttl_sec=int(os.getenv("UNIFIED_MODELS_CACHE_TTL_SEC", "60")),
        # 存量客户端逃生口：默认严格 404，不要默认打开
        model_fallback_first=_bool("UNIFIED_MODEL_FALLBACK_FIRST", False),
        demo_mode=_bool("UNIFIED_DEMO_MODE", False),
        env_base_pids=frozenset(
            pid for pid, var in ENV_BASE_VARS.items() if (os.getenv(var) or "").strip()
        ),
    )
