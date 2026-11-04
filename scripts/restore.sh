#!/usr/bin/env bash
# 从单文件归档恢复 unified-2api：校验 sha256 → 留存当前库 → 原子替换 → 只读自检。
# 实际逻辑在 scripts/backup_core.py，与 scripts/restore.ps1 共用同一份实现。
# 恢复前必须停掉 control 服务。
# 用法：
#   ./scripts/restore.sh --latest
#   ./scripts/restore.sh --archive data/backups/unified-backup-20261103-190000.zip \
#       --ready-url http://127.0.0.1:8010/api/health/ready
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
db="${repo}/data/unified.db"
backups_dir="${repo}/data/backups"
archive=""
latest=0
with_config=0
ready_url=""
ready_timeout=20
require_sidecar=0
core="${repo}/scripts/backup_core.py"

usage() {
  cat <<'EOF'
usage: restore.sh (--archive PATH | --latest) [--db PATH] [--backups-dir DIR]
                  [--with-config] [--ready-url URL] [--ready-timeout SEC] [--require-sidecar]

  --archive PATH       .zip archive to restore (alias: --backup)
  --latest             use the newest archive in --backups-dir
  --db PATH            target database (default: <repo>/data/unified.db)
  --backups-dir DIR    where pre-restore snapshots are written (default: <repo>/data/backups)
  --with-config        also restore config/, providers/, upstreams/
  --ready-url URL      poll /api/health/ready after restore, require checks.database.ok
  --require-sidecar    fail when the archive has no .sha256 sidecar

env: PYTHON=/path/to/python3 to override interpreter discovery
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --archive|--backup) archive="$2"; shift 2 ;;
    --latest) latest=1; shift ;;
    --db) db="$2"; shift 2 ;;
    --backups-dir) backups_dir="$2"; shift 2 ;;
    --with-config) with_config=1; shift ;;
    --ready-url) ready_url="$2"; shift 2 ;;
    --ready-timeout) ready_timeout="$2"; shift 2 ;;
    --require-sidecar) require_sidecar=1; shift ;;
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

if [[ -z "${archive}" && ${latest} -eq 0 ]]; then
  echo "需要 --archive PATH 或 --latest" >&2
  usage >&2
  exit 64
fi

if [[ ${latest} -eq 1 && -z "${archive}" ]]; then
  archive="$("$py" "$core" latest --out-dir "$backups_dir")"
  echo "latest archive: ${archive}"
fi

if [[ ! -f "${archive}" ]]; then
  echo "archive not found: ${archive}" >&2
  exit 1
fi

mkdir -p "$backups_dir"
args=(restore --archive "$archive" --db "$db" --backups "$backups_dir" --repo "$repo" --ready-timeout "$ready_timeout")
(( with_config )) && args+=(--with-config)
[[ -n "${ready_url}" ]] && args+=(--ready-url "$ready_url")
(( require_sidecar )) && args+=(--require-sidecar)
exec "$py" "$core" "${args[@]}"
