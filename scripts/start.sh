#!/usr/bin/env bash
# One-shot start/stop. Publishes the app port; reverse proxy stays on the host.
#   ./scripts/start.sh --docker --local -p 8000 --password 'secret'
#   ./scripts/start.sh --dev --public -p 8000
#   ./scripts/start.sh --origin-check
#   ./scripts/start.sh stop
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PORT="${PRVAPT_PORT:-8000}"
ACCESS="local"
URL=""
PASSWORD="${PRVAPT_ADMIN_PASSWORD:-}"
ACTION="up"
BUILD=1
ENGINE=""          # docker | dev
ORIGIN_CHECK=0

usage() {
  cat <<'EOF'
PrvAptMirror 一键启动（只暴露应用端口，反代交给宿主机）

用法:
  ./scripts/start.sh [选项]
  ./scripts/start.sh stop | logs | status

选项:
  -p, --port PORT         主机端口 (默认 8000)
      --local             仅本机 127.0.0.1（默认），启动后打印本机地址
      --public            监听 0.0.0.0，启动后打印本机 + 所有网卡地址
  -a, --access MODE       local | public（同上）
      --dev               本机 uvicorn
      --docker            Docker 只跑 app 并映射端口（默认：有 Compose 就用 Docker）
  -P, --password PASS     管理员密码
      --origin-check      打开 Origin/Referer 地址校验（默认关闭）
  -u, --url URL           覆盖 apt 片段里的 origin
      --no-build          Docker 时不 --build
  -h, --help              帮助

示例:
  ./scripts/start.sh --docker --local -p 8000 --password 'change-me'
  ./scripts/start.sh --dev --public -p 8000
  ./scripts/start.sh --origin-check --docker --public
  ./scripts/start.sh stop
EOF
}

die() { echo "错误: $*" >&2; exit 1; }

normalize_access() {
  local raw
  raw="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  case "$raw" in
    local|本机|loopback|localhost) echo local ;;
    public|外网|lan|内网|external|wan) echo public ;;
    *) die "未知访问模式 '$1'（local / public）" ;;
  esac
}

have_compose() {
  command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1
}

ipv4_addrs() {
  if command -v ip >/dev/null 2>&1; then
    ip -4 -o addr show scope global 2>/dev/null | awk '
      $2 ~ /^(docker|br-|veth|cni|flannel|virbr|lxc)/ { next }
      { split($4, a, "/"); if (a[1] != "") print a[1] }
    '
  elif command -v hostname >/dev/null 2>&1; then
    hostname -I 2>/dev/null | tr ' ' '\n'
  fi
}

primary_ipv4() {
  local ip=""
  if command -v ip >/dev/null 2>&1; then
    ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}')" || true
  fi
  if [ -z "$ip" ]; then
    ip="$(ipv4_addrs | awk '$1!="127.0.0.1"{print; exit}')"
  fi
  printf '%s' "$ip"
}

origin_of() { printf '%s' "${1%/}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    stop|down) ACTION=stop; shift ;;
    logs) ACTION=logs; shift ;;
    status) ACTION=status; shift ;;
    -p|--port) PORT="${2:-}"; shift 2 ;;
    --local) ACCESS=local; shift ;;
    --public) ACCESS=public; shift ;;
    -a|--access) ACCESS="$(normalize_access "${2:-}")"; shift 2 ;;
    --dev|--uvicorn) ENGINE=dev; shift ;;
    --docker) ENGINE=docker; shift ;;
    -P|--password) PASSWORD="${2:-}"; shift 2 ;;
    --origin-check|--verify-origin|--verify-addr) ORIGIN_CHECK=1; shift ;;
    -u|--url) URL="${2:-}"; shift 2 ;;
    --no-build) BUILD=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数: $1  （./scripts/start.sh --help）" ;;
  esac
done

case "$PORT" in
  ''|*[!0-9]*) die "端口必须是数字" ;;
esac
if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
  die "端口超出范围"
fi

RUNTIME="$ROOT/.env.runtime"
PIDFILE="$ROOT/data/uvicorn.pid"

compose() {
  if [ -f "$RUNTIME" ]; then
    docker compose --env-file "$RUNTIME" "$@"
  else
    docker compose "$@"
  fi
}

if [ "$ACTION" = stop ]; then
  if have_compose; then
    compose down 2>/dev/null || true
  fi
  if [ -f "$PIDFILE" ]; then
    pid="$(cat "$PIDFILE" 2>/dev/null || true)"
    if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" || true
      sleep 0.3
    fi
    rm -f "$PIDFILE"
  fi
  echo "已停止。"
  exit 0
fi

if [ "$ACTION" = logs ]; then
  if have_compose && compose ps -q app 2>/dev/null | grep -q .; then
    compose logs -f app
  elif [ -f "$PIDFILE" ]; then
    exec tail -f "$ROOT/data/uvicorn.log"
  else
    die "没有在跑的实例"
  fi
  exit 0
fi

if [ "$ACTION" = status ]; then
  if have_compose; then compose ps || true; fi
  if [ -f "$RUNTIME" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$RUNTIME"
    set +a
  fi
  curl -fsS "http://127.0.0.1:${PRVAPT_PORT:-$PORT}/healthz" && echo || echo "(healthz 不可达)"
  exit 0
fi

BIND_HOST=127.0.0.1
ACCESS_LABEL="local"
PUBLIC_URL="http://127.0.0.1:${PORT}"
WARN=""

case "$ACCESS" in
  local)
    BIND_HOST=127.0.0.1
    ACCESS_LABEL="local"
    PUBLIC_URL="$(origin_of "${URL:-http://127.0.0.1:${PORT}}")"
    ;;
  public)
    BIND_HOST=0.0.0.0
    ACCESS_LABEL="public"
    PRIMARY="$(primary_ipv4)"
    if [ -n "$URL" ]; then
      PUBLIC_URL="$(origin_of "$URL")"
    elif [ -n "$PRIMARY" ]; then
      PUBLIC_URL="http://${PRIMARY}:${PORT}"
    else
      PUBLIC_URL="http://127.0.0.1:${PORT}"
    fi
    WARN="public 监听 0.0.0.0。未认证 /apt/ 等于公开软件目录。"
    ;;
esac

if [ -n "$URL" ]; then
  PUBLIC_URL="$(origin_of "$URL")"
fi

ORIGINS="http://127.0.0.1:${PORT},http://localhost:${PORT}"
PRIMARY="$(primary_ipv4 || true)"
if [ -n "${PRIMARY:-}" ]; then
  ORIGINS="${ORIGINS},http://${PRIMARY}:${PORT}"
fi
ORIGINS="${ORIGINS},${PUBLIC_URL}"
ORIGINS="$(printf '%s' "$ORIGINS" | tr ',' '\n' | sed '/^$/d' | awk 'NF && !seen[$0]++ { if (n++) printf(","); printf("%s", $0) }')"

COOKIE_SECURE=auto
case "$PUBLIC_URL" in
  https://*) COOKIE_SECURE=true ;;
esac

mkdir -p "$ROOT/data"
if [ "$(id -u)" = 0 ]; then
  chown 1000:1000 "$ROOT/data" 2>/dev/null || true
fi

umask 077
{
  echo "PRVAPT_HOST_DATA=$ROOT/data"
  echo "PRVAPT_BIND_HOST=$BIND_HOST"
  echo "PRVAPT_PORT=$PORT"
  echo "PRVAPT_PUBLIC_URL=$PUBLIC_URL"
  echo "PRVAPT_ADMIN_ORIGINS=$ORIGINS"
  echo "PRVAPT_ADMIN_USER=${PRVAPT_ADMIN_USER:-admin}"
  echo "PRVAPT_ADMIN_PASSWORD=$PASSWORD"
  echo "PRVAPT_COOKIE_SECURE=$COOKIE_SECURE"
  echo "PRVAPT_ORIGIN_CHECK=$ORIGIN_CHECK"
  echo "PRVAPT_DATA_DIR=$ROOT/data"
} > "$RUNTIME"
chmod 600 "$RUNTIME"

set -a
# shellcheck disable=SC1090
. "$RUNTIME"
set +a

if [ -z "$ENGINE" ]; then
  if have_compose; then ENGINE=docker; else ENGINE=dev; fi
fi

print_urls() {
  echo "  访问地址:"
  if [ "$ACCESS" = local ]; then
    echo "    后台  http://127.0.0.1:${PORT}/admin/"
    echo "    后台  http://localhost:${PORT}/admin/"
    echo "    apt   http://127.0.0.1:${PORT}/apt/"
    echo "    apt   http://localhost:${PORT}/apt/"
    return
  fi
  echo "    后台  http://127.0.0.1:${PORT}/admin/"
  echo "    apt   http://127.0.0.1:${PORT}/apt/"
  ipv4_addrs | awk '!seen[$0]++ && $0!="" && $0!="127.0.0.1" {
    printf "    后台  http://%s:'"$PORT"'/admin/\n", $0
    printf "    apt   http://%s:'"$PORT"'/apt/\n", $0
  }'
}

banner() {
  cat <<EOF

PrvAptMirror 已启动
  模式:     ${ENGINE} / ${ACCESS_LABEL}
  监听:     ${BIND_HOST}:${PORT}
  地址校验: $([ "$ORIGIN_CHECK" = 1 ] && echo "开 (--origin-check)" || echo "关（默认）")
EOF
  print_urls
  echo "  健康检查: http://127.0.0.1:${PORT}/healthz"
  if [ -n "$PASSWORD" ]; then
    echo "  管理员:   ${PRVAPT_ADMIN_USER:-admin} / （已设置 --password）"
  elif [ -f "$ROOT/data/admin-bootstrap.txt" ]; then
    echo "  初始密码: $ROOT/data/admin-bootstrap.txt"
  fi
  if [ -n "$WARN" ]; then
    echo
    echo "  注意: $WARN"
  fi
  echo
  echo "  日志: ./scripts/start.sh logs"
  echo "  停止: ./scripts/start.sh stop"
  echo
}

wait_health() {
  local i
  for i in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

if [ "$ENGINE" = docker ]; then
  have_compose || die "没有 Docker Compose。改用: ./scripts/start.sh --dev ..."
  args=(up -d --force-recreate --remove-orphans)
  if [ "$BUILD" = 1 ]; then args+=(--build); fi
  compose "${args[@]}"
  if ! wait_health; then
    echo "健康检查超时，最近日志：" >&2
    compose logs --tail=80 app >&2 || true
    exit 1
  fi
  banner
  exit 0
fi

UVICORN=""
if [ -x "$ROOT/.venv/bin/uvicorn" ]; then
  UVICORN="$ROOT/.venv/bin/uvicorn"
elif command -v uvicorn >/dev/null 2>&1; then
  UVICORN="$(command -v uvicorn)"
else
  die "没有 uvicorn。先: python3 -m venv .venv && .venv/bin/pip install -e ."
fi

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  die "dev 进程已在跑 (PID $(cat "$PIDFILE"))。先 ./scripts/start.sh stop"
fi

export PRVAPT_PUBLIC_URL PRVAPT_ADMIN_ORIGINS PRVAPT_ADMIN_PASSWORD PRVAPT_COOKIE_SECURE
export PRVAPT_ORIGIN_CHECK PRVAPT_DATA_DIR

nohup "$UVICORN" prvaptmirror.main:app \
  --host "$BIND_HOST" --port "$PORT" --workers 1 \
  >"$ROOT/data/uvicorn.log" 2>&1 &
echo $! > "$PIDFILE"

if ! wait_health; then
  echo "健康检查超时，日志 $ROOT/data/uvicorn.log :" >&2
  tail -n 80 "$ROOT/data/uvicorn.log" >&2 || true
  exit 1
fi
banner
echo "  dev 日志: $ROOT/data/uvicorn.log"
