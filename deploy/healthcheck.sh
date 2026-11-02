#!/usr/bin/env sh
# 探 /healthz 与静态首页；任一非 200 则退出码 1。容器内可直接用（busybox wget 兜底）。
# 用法：./healthcheck.sh [base-url]
#   HEALTHCHECK_INSECURE=1 ./healthcheck.sh https://127.0.0.1   # 自签名证书
set -u

base="${1:-https://127.0.0.1}"
base="${base%/}"
fail=0

probe() {
  url="$1"
  label="$2"
  code=000
  if command -v curl >/dev/null 2>&1; then
    opts="-sS -L -o /dev/null -w %{http_code} --max-time 8"
    [ "${HEALTHCHECK_INSECURE:-0}" = "1" ] && opts="$opts -k"
    # shellcheck disable=SC2086
    code=$(curl $opts "$url" 2>/dev/null) || code=000
  else
    # busybox wget（nginx:alpine 自带）只给成功/失败，失败统一记 000
    wget -q -O /dev/null -T 8 "$url" 2>/dev/null && code=200
  fi

  if [ "$code" = "200" ]; then
    printf 'OK   %-8s %s 200\n' "$label" "$url"
  else
    printf 'FAIL %-8s %s status=%s\n' "$label" "$url" "$code"
    fail=1
  fi
}

probe "$base/healthz" healthz
probe "$base/" homepage

exit "$fail"
