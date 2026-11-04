#!/usr/bin/env bash
# 一键演示模式：进程内假上游 + 控制面 + 带 [demo] 标记的演示数据。
#
# 用法：
#   bash scripts/demo.sh              # 默认 127.0.0.1:8010
#   PORT=18150 bash scripts/demo.sh
#   bash scripts/demo.sh --clear      # 清除所有演示数据，不启动服务
#
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ctrl_dir="$root/apps/control"
# Git Bash 里 $root 是 POSIX 路径，Windows 版 Python 不认，需要转成盘符路径
ctrl_win="$ctrl_dir"
if command -v cygpath >/dev/null 2>&1; then
    ctrl_win="$(cygpath -w "$ctrl_dir")"
fi

find_python() {
    local bundled="$root/.runtime/python/python.exe"
    if [[ -n "${PYTHON:-}" ]]; then echo "$PYTHON"; return; fi
    if [[ -x "$bundled" ]]; then echo "$bundled"; return; fi
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then echo "$candidate"; return; fi
    done
    return 1
}

py="$(find_python)" || {
    echo "未找到 Python。请安装 Python 3.11+，或用 PYTHON=<路径> 指定解释器。" >&2
    exit 1
}

if [[ -f "$root/.env" ]]; then
    set -a
    # shellcheck disable=SC1090
    . "$root/.env"
    set +a
fi

to_win() {
    if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi
}

data_dir="${UNIFIED_DATA_DIR:-$root/data}"
db_path="${UNIFIED_DB_PATH:-$data_dir/unified.db}"
: "${UNIFIED_WEB_DIR:=$root/apps/web/out}"
: "${UNIFIED_PORT:=8010}"
export PORT="${PORT:-$UNIFIED_PORT}"
export UNIFIED_PORT="$PORT"
export UNIFIED_DATA_DIR="$(to_win "$data_dir")"
export UNIFIED_DB_PATH="$(to_win "$db_path")"

if [[ "${1:-}" == "--clear" ]]; then
    echo "==> 清除演示数据（provider=demo_local + [demo] 前缀密钥 + 策略/日志/流水）"
    cd "$ctrl_dir"
    "$py" -c "import sys; sys.path.insert(0, r'$ctrl_win'); from app.config import load_settings; from app.db import Database; from app.demo import clear_demo_data; s = load_settings(); print(clear_demo_data(Database(s.db_path), s))"
    echo "完成：只删除带 demo 标记的数据，其他行未改动。"
    exit 0
fi

# 演示模式强制回环绑定：不跟随 .env 里的 host
export UNIFIED_DEMO_MODE=1
export UNIFIED_HOST=127.0.0.1
mkdir -p "$data_dir"

echo "==> 初始化数据库（幂等建表）"
cd "$ctrl_dir"
"$py" -c "import sys; sys.path.insert(0, r'$ctrl_win'); from app.config import load_settings; from app.db import Database; s = load_settings(); Database(s.db_path); print('    DB:', s.db_path)"

base_url="http://127.0.0.1:$UNIFIED_PORT"
key_file="$data_dir/demo-credentials.txt"
admin_password="${UNIFIED_ADMIN_PASSWORD:-}"
if [[ -z "$admin_password" && -f "$data_dir/admin-password.txt" ]]; then
    admin_password="$(tr -d '\r\n' < "$data_dir/admin-password.txt")"
fi

cat <<EOF

==================================================
 这是演示数据，不要用于生产
 假上游只在本机进程内运行；演示 provider 不允许对公网开放
==================================================

页面地址: $base_url/
健康探针: $base_url/api/health/ready
网关地址: $base_url/v1
EOF
[[ -n "$admin_password" ]] && echo "管理员口令: $admin_password"
echo "演示密钥: $key_file（启动后生成）"
echo "模型: fake-chat-1 / fake-chat-2（假上游固定返回 [fake] 前缀内容）"
echo "清理演示数据: bash scripts/demo.sh --clear"
echo "按 Ctrl+C 停止。"
echo

echo "==> 启动控制面（UNIFIED_DEMO_MODE=1）"
exec "$py" -m uvicorn --app-dir "$ctrl_win" app.main:app --host "$UNIFIED_HOST" --port "$UNIFIED_PORT"
