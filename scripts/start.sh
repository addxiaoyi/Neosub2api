#!/usr/bin/env bash
# 启动控制面（本地运行，非容器）。前端静态产物 apps/web/out 由控制面直接托管。
# 用法：
#   ./scripts/start.sh                 # 默认 127.0.0.1:8010
#   UNIFIED_PORT=18109 ./scripts/start.sh
#   PYTHON=python3.12 ./scripts/start.sh
# 说明：口令不写死；优先读仓库根 .env，没设则首次启动随机生成并写入 data/admin-password.txt。
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
step() { printf '==> %s\n' "$1"; }
die() { printf '错误：%s\n' "$1" >&2; exit 1; }

# 读取 .env：只接受 KEY=VALUE，不覆盖外部已导出的变量
load_env() {
    local file="$1" line key value
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|'#'*) continue ;; esac
        key="${line%%=*}"
        value="${line#*=}"
        value="${value%$'\r'}"
        key="$(printf '%s' "$key" | tr -d '[:space:]')"
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
        if [ -z "${!key:-}" ]; then
            export "$key=$value"
        fi
    done < "$file"
}

step "检查 Python 运行时"
py="${PYTHON:-}"
if [ -z "$py" ]; then
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then py="$candidate"; break; fi
    done
fi
[ -n "$py" ] || die "未找到 Python。请安装 Python 3.11+，或用 PYTHON=/path/to/python 指定。"
"$py" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "Python 版本过低：$("$py" --version 2>&1)。控制面要求 3.11+。"
printf '    %s\n' "$("$py" --version 2>&1)"

if [ -f "$root/.env" ]; then
    step "读取 $root/.env（不覆盖已存在的环境变量）"
    load_env "$root/.env"
fi

export UNIFIED_DATA_DIR="${UNIFIED_DATA_DIR:-$root/data}"
export UNIFIED_DB_PATH="${UNIFIED_DB_PATH:-$UNIFIED_DATA_DIR/unified.db}"
export UNIFIED_HOST="${UNIFIED_HOST:-127.0.0.1}"
export UNIFIED_PORT="${UNIFIED_PORT:-8010}"
export UNIFIED_WEB_DIR="${UNIFIED_WEB_DIR:-$root/apps/web/out}"

mkdir -p "$UNIFIED_DATA_DIR"

ctrl="$root/apps/control"

step "初始化数据库（幂等建表）"
(
    cd "$ctrl"
    # 显式把 app 包目录塞进 sys.path，兼容忽略 cwd/PYTHONPATH 的嵌入式 Python
    "$py" -c "import sys; sys.path.insert(0, '$ctrl'); from app.config import load_settings; from app.db import Database; s = load_settings(); Database(s.db_path); print(f'    DB: {s.db_path}')"
)

if [ ! -f "$root/apps/web/out/index.html" ]; then
    printf '警告：未找到前端产物 apps/web/out；控制面只提供 API，页面会是提示 JSON。\n' >&2
    printf '    构建前端：cd apps/web && npm ci && npm run build:export\n' >&2
elif ! command -v node >/dev/null 2>&1; then
    printf '    提示：未检测到 Node.js；仅影响重新构建前端，已有 out 可直接使用。\n'
fi

display_host="$UNIFIED_HOST"
[ "$display_host" = "0.0.0.0" ] && display_host="127.0.0.1"
printf '\n前端页面: http://%s:%s/\n' "$display_host" "$UNIFIED_PORT"
printf '健康探针: http://%s:%s/healthz\n' "$display_host" "$UNIFIED_PORT"
printf 'API 入口: http://%s:%s/api/\n' "$display_host" "$UNIFIED_PORT"
printf '按 Ctrl+C 停止。\n\n'

step "启动控制面"
cd "$ctrl"
exec "$py" -m uvicorn --app-dir "$ctrl" app.main:app --host "$UNIFIED_HOST" --port "$UNIFIED_PORT"
