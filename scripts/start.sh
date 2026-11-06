#!/usr/bin/env bash
# 一键启动 / 停止（本机，非容器）。默认页面 18100、控制面 18101，端口占用自动顺延。
# 用法：
#   bash scripts/start.sh                     # 启动
#   bash scripts/start.sh --stop              # 停止（不需要记 PID）
#   bash scripts/start.sh --web-port 18110 --api-port 18111
# 脚本会检查 Python / 前端产物 / 控制面依赖 → 幂等建库补列 → 选端口
# → 起控制面并等 /healthz → 起静态前端并等首页 200 → 打印地址与账号。
set -uo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
run_dir="$root/.run"
state_file="$run_dir/start.json"
ctrl="$root/apps/control"
serve="$root/scripts/serve-web.mjs"
ctrl_log="$run_dir/control.log"
ctrl_err="$run_dir/control.err.log"
web_log="$run_dir/web.log"
web_err="$run_dir/web.err.log"
migrate_log="$run_dir/migrate.log"

web_port_want=18100
api_port_want=18101
mode=start

while [ $# -gt 0 ]; do
    case "$1" in
        --stop) mode=stop ;;
        --web-port) web_port_want="${2:?}"; shift ;;
        --api-port) api_port_want="${2:?}"; shift ;;
        *) printf '未知参数：%s\n' "$1" >&2; exit 1 ;;
    esac
    shift
done

step() { printf '==> %s\n' "$1"; }
fail() {
    printf '\n启动失败：%s\n下一步：%s\n' "$1" "$2" >&2
    exit "${3:-1}"
}

resolve_path() {
    case "$1" in
        /*) printf '%s' "$1" ;;
        *) printf '%s/%s' "$root" "$1" ;;
    esac
}

py=""
find_py() {
    if [ -n "${PYTHON:-}" ] && command -v "$PYTHON" >/dev/null 2>&1; then
        py="$PYTHON"
        return
    fi
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then py="$candidate"; return; fi
    done
}

find_py
[ -n "$py" ] || fail "未找到 Python（需要 3.11+）" "装 Python 3.11+，或用 PYTHON=/path/to/python 指定后重试。" 3
"$py" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
    || fail "Python 版本过低：$("$py" --version 2>&1)" "控制面要求 Python 3.11+；升级后重试。" 3

port_free() {
    "$py" - "$1" <<'PY'
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
}

http_ok() {
    "$py" - "$1" <<'PY'
import sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=2) as resp:
        sys.exit(0 if resp.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
}

wait_http() {
    local url="$1" seconds="$2" i=0
    while [ "$i" -lt $((seconds * 2)) ]; do
        if http_ok "$url"; then return 0; fi
        sleep 0.5
        i=$((i + 1))
    done
    return 1
}

find_free_port() {
    local start="$1" skip="${2:-0}" p
    for ((p = start; p < start + 40; p++)); do
        [ "$p" -eq "$skip" ] && continue
        if port_free "$p"; then printf '%s' "$p"; return 0; fi
    done
    return 1
}

read_state() {
    "$py" - "$state_file" "$1" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception:
    print("")
    raise SystemExit(0)
value = data.get(sys.argv[2])
print("" if value is None else value)
PY
}

process_alive() {
    [ -n "${1:-}" ] && [ "$1" -gt 0 ] 2>/dev/null && kill -0 "$1" 2>/dev/null
}

stop_tree() {
    local pid="$1" i=0
    [ -n "$pid" ] || return 0
    kill -TERM "$pid" 2>/dev/null || true
    while [ "$i" -lt 20 ]; do
        kill -0 "$pid" 2>/dev/null || return 0
        sleep 0.2
        i=$((i + 1))
    done
    kill -KILL "$pid" 2>/dev/null || true
}

if [ "$mode" = stop ]; then
    if [ ! -f "$state_file" ]; then
        printf '没有找到 .run/start.json：当前没有由本脚本启动的进程。\n'
        exit 0
    fi
    api_pid="$(read_state api_pid)"
    web_pid="$(read_state web_pid)"
    stop_tree "$web_pid"
    stop_tree "$api_pid"
    web_port="$(read_state web_port)"
    api_port="$(read_state api_port)"
    rm -f "$state_file"
    printf '已停止。释放端口：页面 %s，控制面 %s\n' "$web_port" "$api_port"
    exit 0
fi

mkdir -p "$run_dir"

# 幂等：已有进程在跑就只提示
if [ -f "$state_file" ]; then
    api_pid="$(read_state api_pid)"
    web_pid="$(read_state web_pid)"
    api_port="$(read_state api_port)"
    web_port="$(read_state web_port)"
    if process_alive "$api_pid" && { [ -z "$web_pid" ] || process_alive "$web_pid"; }; then
        printf '已经在运行，未重复启动。\n  页面：http://127.0.0.1:%s/\n  控制面：http://127.0.0.1:%s/healthz\n  停止：bash scripts/start.sh --stop\n' \
            "$web_port" "$api_port"
        exit 0
    fi
    printf '发现上次运行残留（进程已退出），先清理状态再启动。\n'
    stop_tree "$web_pid"
    stop_tree "$api_pid"
    rm -f "$state_file"
fi

# .env：只补空缺，不覆盖外部显式变量
if [ -f "$root/.env" ]; then
    step "读取 .env（不覆盖已存在的环境变量）"
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|'#'*) continue ;; esac
        key="${line%%=*}"; value="${line#*=}"
        value="${value%$'\r'}"
        key="$(printf '%s' "$key" | tr -d '[:space:]')"
        [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
        # 用 printenv 判断，兼容 macOS 自带的 bash 3.2（那里没有 ${!key} 间接展开）
        if [ -z "$(printenv "$key" 2>/dev/null || true)" ]; then export "$key=$value"; fi
    done < "$root/.env"
fi

export UNIFIED_DATA_DIR="$(resolve_path "${UNIFIED_DATA_DIR:-data}")"
export UNIFIED_DB_PATH="$(resolve_path "${UNIFIED_DB_PATH:-$UNIFIED_DATA_DIR/unified.db}")"
export UNIFIED_WEB_DIR="$(resolve_path "${UNIFIED_WEB_DIR:-apps/web/out}")"
export UNIFIED_HOST=127.0.0.1

step "检查前端产物"
if [ ! -f "$UNIFIED_WEB_DIR/index.html" ]; then
    fail "前端产物不存在：$UNIFIED_WEB_DIR/index.html" \
        $'先构建前端，再重跑本脚本：\n  cd apps/web\n  npm ci\n  npm run build:export\n没装 Node.js 的话先装 20+：https://nodejs.org/' 2
fi
printf '    %s\n' "$UNIFIED_WEB_DIR"

step "检查控制面依赖"
if ! "$py" -c 'import fastapi, uvicorn, httpx, pydantic, yaml, cryptography' 2>/dev/null; then
    fail "控制面依赖缺失" "装依赖后重跑：$py -m pip install -r apps/control/requirements.txt" 3
fi
printf '    依赖齐全\n'

fresh="$("$py" - 2>/dev/null <<'PY'
import json, os, sqlite3
path = os.environ["UNIFIED_DB_PATH"]
if not os.path.exists(path):
    print("fresh")
else:
    try:
        conn = sqlite3.connect(path)
        row = conn.execute("SELECT username FROM users WHERE role='admin' ORDER BY created_at LIMIT 1").fetchone()
        print("existing:" + (row[0] if row else ""))
    except Exception:
        print("unknown")
PY
)"
if [ -z "$fresh" ] || [ "$fresh" = "unknown" ]; then
    fail "数据库迁移失败（库文件打不开）" "确认 data 目录可写、库文件没有被另一个进程独占，或换一个 UNIFIED_DB_PATH 后重试。" 5
fi
used_default_password=0
if [ "$fresh" = "fresh" ]; then
    export UNIFIED_ADMIN_USER="${UNIFIED_ADMIN_USER:-addxiaoyi}"
    if [ -z "${UNIFIED_ADMIN_PASSWORD:-}" ]; then
        export UNIFIED_ADMIN_PASSWORD=Lsc123456
        used_default_password=1
    fi
fi

step "初始化数据库（幂等建表与补列）"
mkdir -p "$UNIFIED_DATA_DIR"
export CTRL_DIR="$ctrl"
if ( cd "$ctrl" && "$py" -c "import os, sys; sys.path.insert(0, os.environ['CTRL_DIR']); from app.config import load_settings; from app.db import Database; s = load_settings(); Database(s.db_path); print(f'DB: {s.db_path}')" ) >"$migrate_log" 2>&1; then
    sed 's/^/    /' "$migrate_log"
else
    fail "数据库迁移失败：$UNIFIED_DB_PATH" $'  常见原因：data 目录不可写、库文件被另一个进程占用、磁盘满。\n  详细输出：.run/migrate.log' 5
fi

step "选择端口"
[ "$web_port_want" -eq "$api_port_want" ] && api_port_want=$((web_port_want + 1))
web_port="$(find_free_port "$web_port_want" 0)" \
    || fail "端口全占：$web_port_want 起连续 40 个端口都不可用" "先释放这些端口，或用 --web-port 指定其他起始端口。" 4
api_port="$(find_free_port "$api_port_want" "$web_port")" \
    || fail "端口全占：$api_port_want 起连续 40 个端口都不可用" "先释放这些端口，或用 --api-port 指定其他起始端口。" 4
if [ "$web_port" -ne "$web_port_want" ]; then
    printf '    页面端口 %s 被占用，改用 %s\n' "$web_port_want" "$web_port"
else
    printf '    页面端口 %s\n' "$web_port"
fi
if [ "$api_port" -ne "$api_port_want" ]; then
    printf '    控制面端口 %s 被占用，改用 %s\n' "$api_port_want" "$api_port"
else
    printf '    控制面端口 %s\n' "$api_port"
fi

export UNIFIED_PORT="$api_port"

step "启动控制面"
cd "$ctrl" || fail "进不去控制面目录：$ctrl" "检查仓库是否完整。" 1
nohup "$py" -m uvicorn --app-dir "$ctrl" app.main:app --host 127.0.0.1 --port "$api_port" \
    >"$ctrl_log" 2>"$ctrl_err" &
api_pid=$!
cd "$root" || exit 1
if ! wait_http "http://127.0.0.1:$api_port/healthz" 45; then
    tail_text="$(tail -n 30 "$ctrl_err" 2>/dev/null || true)"
    stop_tree "$api_pid"
    case "$tail_text" in
        *"address already in use"*|*"Only one usage of each socket address"*)
            fail "控制面端口 $api_port 刚被别的进程抢走" "释放该端口，或换 --api-port 重新运行。" 6 ;;
        *ModuleNotFoundError*|*"No module named"*)
            fail "控制面依赖缺失（启动时导入失败）" "$py -m pip install -r apps/control/requirements.txt 后重试；详情见 .run/control.err.log。" 3 ;;
        *sqlite*|*OperationalError*|*"database is locked"*)
            fail "数据库不可用（被占用或损坏）" "关闭占用数据库的进程；详情见 .run/control.err.log。" 5 ;;
        *)
            fail "控制面在 45 秒内没有通过 /healthz" "看 .run/control.err.log 的前几行错误；修好后重跑本脚本。" 6 ;;
    esac
fi
printf '    http://127.0.0.1:%s/healthz 已通过\n' "$api_port"

web_pid=""
if command -v node >/dev/null 2>&1; then
    step "启动前端静态服务"
    nohup node "$serve" --port "$web_port" --backend "$api_port" --dir "$UNIFIED_WEB_DIR" \
        >"$web_log" 2>"$web_err" &
    web_pid=$!
    if ! wait_http "http://127.0.0.1:$web_port/" 20; then
        tail_text="$(tail -n 30 "$web_err" 2>/dev/null || true)"
        stop_tree "$web_pid"
        stop_tree "$api_pid"
        case "$tail_text" in
            *EADDRINUSE*) fail "页面端口 $web_port 刚被别的进程抢走" "释放该端口，或换 --web-port 重新运行。" 7 ;;
            *) fail "前端静态服务启动失败" "详情见 .run/web.err.log；也可以先不跑它，直接在控制面端口访问页面。" 7 ;;
        esac
    fi
    printf '    http://127.0.0.1:%s/ 已可访问\n' "$web_port"
else
    printf '    未检测到 Node.js：跳过独立静态服务，页面改由控制面在 %s 同端口托管。\n' "$api_port"
    printf '    要分开跑前端静态服务，请安装 Node.js 20+：https://nodejs.org/\n'
    web_port=""
fi

account="$("$py" - <<'PY'
import json, os, sqlite3
conn = sqlite3.connect(os.environ["UNIFIED_DB_PATH"])
row = conn.execute("SELECT username FROM users WHERE role='admin' ORDER BY created_at LIMIT 1").fetchone()
print(row[0] if row else "")
PY
)"

password_file="$UNIFIED_DATA_DIR/admin-password.txt"
file_pwd_matches="?"
if [ "$used_default_password" -eq 0 ] && [ -z "${UNIFIED_ADMIN_PASSWORD:-}" ] && [ -f "$password_file" ]; then
    file_pwd_matches="$(ADMIN_PW_FILE="$password_file" "$py" - 2>/dev/null <<'PY'
import os, sqlite3, sys
sys.path.insert(0, os.environ["CTRL_DIR"])
from app.security import verify_password
conn = sqlite3.connect(os.environ["UNIFIED_DB_PATH"])
row = conn.execute("SELECT password_hash FROM users WHERE role='admin' ORDER BY created_at LIMIT 1").fetchone()
with open(os.environ["ADMIN_PW_FILE"], encoding="utf-8") as fh:
    pw = fh.read().strip()
print("1" if row and verify_password(pw, row[0]) else "0")
PY
)"
fi
if [ "$used_default_password" -eq 1 ]; then
    password_text="$UNIFIED_ADMIN_PASSWORD（快速开始默认口令）"
elif [ -n "${UNIFIED_ADMIN_PASSWORD:-}" ]; then
    password_text="$UNIFIED_ADMIN_PASSWORD（来自 .env / 环境变量）"
elif [ -f "$password_file" ]; then
    file_password="$(tr -d '\r\n' < "$password_file")"
    if [ "$file_pwd_matches" = "1" ]; then
        password_text="$file_password（来自 data/admin-password.txt）"
    elif [ "$file_pwd_matches" = "0" ]; then
        password_text="已改过，data/admin-password.txt 里的初始口令已失效，请用你当前设置的口令"
    else
        password_text="$file_password（来自 data/admin-password.txt；若你改过密码，以新口令为准）"
    fi
else
    password_text="未知（库里已有管理员；用你设置过的口令，或设 UNIFIED_ADMIN_PASSWORD 后重启）"
fi

STARTED_AT="$(date '+%Y-%m-%dT%H:%M:%S')" \
STATE_FILE="$state_file" API_PORT="$api_port" WEB_PORT="$web_port" API_PID="$api_pid" WEB_PID="$web_pid" \
"$py" - <<'PY'
import json, os
data = {
    "started_at": os.environ["STARTED_AT"],
    "api_port": int(os.environ["API_PORT"]),
    "web_port": int(os.environ["WEB_PORT"]) if os.environ["WEB_PORT"] else None,
    "api_pid": int(os.environ["API_PID"]),
    "web_pid": int(os.environ["WEB_PID"]) if os.environ["WEB_PID"] else None,
}
with open(os.environ["STATE_FILE"], "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False)
PY

printf '\n一键启动已完成\n'
if [ -n "$web_port" ]; then
    printf '  页面      http://127.0.0.1:%s/\n' "$web_port"
else
    printf '  页面      http://127.0.0.1:%s/（未装 Node，由控制面同端口托管）\n' "$api_port"
fi
printf '  健康探针  http://127.0.0.1:%s/healthz\n' "$api_port"
printf '  API 入口  http://127.0.0.1:%s/api/\n' "$api_port"
printf '  停止      bash scripts/start.sh --stop\n'
if [ -n "$account" ]; then
    printf '  登录账号  %s\n' "$account"
    printf '  登录口令  %s\n' "$password_text"
    printf '  首次登录后请立刻改密（设置 → 用户），别把默认口令留在机器上。\n'
fi
printf '  日志      .run/control.err.log / .run/web.err.log\n\n'
