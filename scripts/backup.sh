#!/usr/bin/env bash
# 备份 unified-2api：单文件归档（库快照 + 配置 + 版本元数据 + sha256）。
# 实际逻辑在 scripts/backup_core.py，与 scripts/backup.ps1 共用同一份实现。
# 用法：
#   ./scripts/backup.sh
#   ./scripts/backup.sh --keep 7
#   ./scripts/backup.sh --db /var/lib/unified/unified.db --out /var/backups/unified
#   ./scripts/backup.sh --include-demo     # 保留演示数据（默认剔除并记录）
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
db="${repo}/data/unified.db"
out="${repo}/data/backups"
keep=14
include_demo=0
core="${repo}/scripts/backup_core.py"

usage() {
  cat <<'EOF'
usage: backup.sh [--db PATH] [--out DIR] [--keep N] [--include-demo]

  --db PATH        database to snapshot (default: <repo>/data/unified.db)
  --out DIR        archive directory (default: <repo>/data/backups, git-ignored)
  --keep N         keep the newest N archives (default: 14)
  --include-demo   keep demo rows (provider=demo_local / "[demo] " keys)

env: PYTHON=/path/to/python3 to override interpreter discovery
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --db) db="$2"; shift 2 ;;
    --out) out="$2"; shift 2 ;;
    --keep) keep="$2"; shift 2 ;;
    --include-demo) include_demo=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 64 ;;
  esac
done

if [[ "${PYTHON:-}" != "" ]]; then
  py="$PYTHON"
elif [[ -x "${repo}/.runtime/python/bin/python3" ]]; then
  py="${repo}/.runtime/python/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
  py="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  py="$(command -v python)"
else
  echo "python3 not found (looked at \$PYTHON, .runtime/python/bin/python3, python3, python)" >&2
  exit 1
fi

if [[ ! "${keep}" =~ ^[0-9]+$ ]] || (( keep < 1 )); then
  echo "--keep must be an integer >= 1" >&2
  exit 64
fi

args=(backup --db "$db" --out-dir "$out" --keep "$keep")
(( include_demo )) && args+=(--include-demo)
exec "$py" "$core" "${args[@]}"
