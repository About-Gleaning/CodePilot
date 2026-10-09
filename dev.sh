#!/usr/bin/env bash

set -u

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_DIR="$ROOT_DIR/.run"
BACKEND_PID_FILE="$RUN_DIR/backend.pid"
FRONTEND_PID_FILE="$RUN_DIR/frontend.pid"
LAN_PROXY_PID_FILE="$RUN_DIR/lan-proxy.pid"
BACKEND_LOG_FILE="$RUN_DIR/backend.log"
FRONTEND_LOG_FILE="$RUN_DIR/frontend.log"
LAN_PROXY_LOG_FILE="$RUN_DIR/lan-proxy.log"
BACKEND_PORT=8000
FRONTEND_PORT=5173
LAN_PORT=5443
BACKEND_GRACEFUL_SHUTDOWN_SECONDS=5
DEV_HOST="${CODEPILOT_HOST:-127.0.0.1}"
LAN_ENABLED="${CODEPILOT_LAN_ENABLED:-0}"
NEW_BACKEND_PID=""
NEW_FRONTEND_PID=""
NEW_LAN_PROXY_PID=""
LAN_ADDRESSES=()

case "$DEV_HOST" in
  127.0.0.1|localhost|::1) ;;
  *)
    echo "错误: CodePilot 首版仅允许绑定本机回环地址。"
    exit 1
    ;;
esac
case "$LAN_ENABLED" in 0|1) ;; *) echo "错误: CODEPILOT_LAN_ENABLED 只能是 0 或 1。"; exit 1 ;; esac

BACKEND_CMD=(
  uv run --no-sync --offline uvicorn codepilot.main:app
  --app-dir src
  --reload
  # 只监听业务源码，避免 StatReload 递归扫描 .venv 导致持续高 CPU。
  --reload-dir "$ROOT_DIR/backend/src"
  --host "$DEV_HOST"
  --port "$BACKEND_PORT"
  # 将 Uvicorn 的优雅退出时间限制在 stop 脚本等待窗口内，避免 SSE 长连接导致外部强杀。
  --timeout-graceful-shutdown "$BACKEND_GRACEFUL_SHUTDOWN_SECONDS"
)
FRONTEND_CMD=(env COREPACK_ENABLE_NETWORK=0 COREPACK_ENABLE_AUTO_PIN=0 pnpm dev --host "$DEV_HOST" --port "$FRONTEND_PORT" --strictPort)

mkdir -p "$RUN_DIR"

print_usage() {
  echo "用法: ./dev.sh {start|stop|restart}"
}

require_command() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "错误: 未找到命令 ${cmd}，请先安装后再执行。"
    exit 1
  fi
}

require_file() {
  local path="$1"
  if [[ ! -f "$path" ]]; then
    echo "错误: 缺少文件 ${path}，请确认项目依赖已初始化。"
    exit 1
  fi
}

is_pid_running() {
  local pid="$1"
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
  kill -0 "$pid" >/dev/null 2>&1
}

get_process_group_id() {
  local pid="$1"
  local pgid
  pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | awk '{$1=$1;print}')"
  [[ -n "$pgid" ]] && printf '%s\n' "$pgid"
}

pid_matches_command() {
  local pid="$1"
  local expected_fragment="$2"
  local command_line
  command_line="$(ps -o command= -p "$pid" 2>/dev/null | awk '{$1=$1;print}')"
  [[ -n "$command_line" && "$command_line" == *"$expected_fragment"* ]]
}

ancestor_matches_command() {
  local pid="$1"
  local expected_fragment="$2"
  local current_pid="$pid"
  local parent_pid=""

  while [[ -n "${current_pid:-}" && "$current_pid" != "0" ]]; do
    if pid_matches_command "$current_pid" "$expected_fragment"; then
      return 0
    fi
    parent_pid="$(ps -o ppid= -p "$current_pid" 2>/dev/null | awk '{$1=$1;print}')"
    if [[ -z "${parent_pid:-}" || "$parent_pid" == "$current_pid" ]]; then
      break
    fi
    current_pid="$parent_pid"
  done

  return 1
}

find_listener_pid() {
  local port="$1"
  lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | head -n 1
}

read_pid() {
  local pid_file="$1"
  if [[ -f "$pid_file" ]]; then
    tr -d '[:space:]' <"$pid_file"
  fi
}

launch_in_own_session() {
  local work_dir="$1"
  local log_file="$2"
  local pid_file="$3"
  shift 3

  (
    cd "$work_dir" || exit 1
    # 为每个服务创建独立会话，确保 stop 时可以按进程组回收所有子进程。
    nohup python3 -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' "$@" </dev/null >>"$log_file" 2>&1 &
    echo $! >"$pid_file"
  )
}

stop_process_group() {
  local pid="$1"
  local signal="${2:-TERM}"
  local pgid
  pgid="$(get_process_group_id "$pid")"

  if [[ -n "${pgid:-}" ]]; then
    kill "-${signal}" -- "-${pgid}" >/dev/null 2>&1 || true
    return
  fi

  kill "-${signal}" "$pid" >/dev/null 2>&1 || true
}

start_backend() {
  local pid
  pid="$(read_pid "$BACKEND_PID_FILE")"
  if [[ -n "${pid:-}" ]] && is_pid_running "$pid" && pid_matches_command "$pid" "uvicorn codepilot.main:app"; then
    echo "后端已在运行，PID=$pid"
    return
  fi

  if [[ -n "$(find_listener_pid "$BACKEND_PORT")" ]]; then
    echo "错误: 后端启动前检查失败，端口 ${BACKEND_PORT} 已被占用。"
    return 1
  fi
  rm -f "$BACKEND_PID_FILE"
  launch_in_own_session "$ROOT_DIR/backend" "$BACKEND_LOG_FILE" "$BACKEND_PID_FILE" "${BACKEND_CMD[@]}" || return 1
  pid="$(read_pid "$BACKEND_PID_FILE")"
  NEW_BACKEND_PID="$pid"
  echo "后端进程已创建，等待就绪。"
}

start_frontend() {
  local pid
  pid="$(read_pid "$FRONTEND_PID_FILE")"
  if [[ -n "${pid:-}" ]] && is_pid_running "$pid" && pid_matches_command "$pid" "pnpm dev --host"; then
    echo "前端已在运行，PID=$pid"
    return
  fi

  if [[ -n "$(find_listener_pid "$FRONTEND_PORT")" ]]; then
    echo "错误: 前端启动前检查失败，端口 ${FRONTEND_PORT} 已被占用。"
    return 1
  fi
  rm -f "$FRONTEND_PID_FILE"
  launch_in_own_session "$ROOT_DIR/frontend" "$FRONTEND_LOG_FILE" "$FRONTEND_PID_FILE" "${FRONTEND_CMD[@]}" || return 1
  pid="$(read_pid "$FRONTEND_PID_FILE")"
  NEW_FRONTEND_PID="$pid"
  echo "前端进程已创建，等待就绪。"
}

discover_lan_addresses() {
  local interface address seen=" "
  LAN_ADDRESSES=()
  for interface in $(ifconfig -l); do
    address="$(ipconfig getifaddr "$interface" 2>/dev/null || true)"
    [[ "$address" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ && "$address" != 127.* ]] || continue
    [[ "$seen" == *" $address "* ]] && continue
    LAN_ADDRESSES+=("$address"); seen+="$address "
  done
  ((${#LAN_ADDRESSES[@]})) || { echo "错误: 未检测到可用的局域网 IPv4 地址。"; return 1; }
}

prepare_lan_certificate() {
  local certificate_dir key certificate san="" origins="" address
  certificate_dir="$RUN_DIR/lan"
  key="$certificate_dir/key.pem"
  certificate="$certificate_dir/cert.pem"
  mkdir -p "$certificate_dir" && chmod 700 "$certificate_dir" || return 1
  for address in "${LAN_ADDRESSES[@]}"; do san+="IP:$address,"; origins+="https://$address:$LAN_PORT,"; done
  umask 077
  # 每次地址集合变化时重建，确保证书 SAN 与实际可分享地址一致。
  openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=${LAN_ADDRESSES[0]}" \
    -addext "subjectAltName=${san%,}" -keyout "$key" -out "$certificate" >/dev/null 2>&1 || return 1
  chmod 600 "$key" && chmod 644 "$certificate" || return 1
  BACKEND_CMD=(env CODEPILOT_AUTH_MODE=lan_https CODEPILOT_PUBLIC_ORIGINS="${origins%,}" "${BACKEND_CMD[@]}")
  LAN_PROXY_CMD=(node "$ROOT_DIR/frontend/scripts/lan-https-proxy.mjs" "$key" "$certificate" "$LAN_PORT" "$BACKEND_PORT" "$FRONTEND_PORT")
}

start_lan_proxy() {
  local pid
  pid="$(read_pid "$LAN_PROXY_PID_FILE")"
  if [[ -n "${pid:-}" ]] && is_pid_running "$pid" && pid_matches_command "$pid" "lan-https-proxy.mjs"; then return; fi
  [[ -z "$(find_listener_pid "$LAN_PORT")" ]] || { echo "错误: 局域网 HTTPS 代理启动前检查失败，端口 ${LAN_PORT} 已被占用。"; return 1; }
  rm -f "$LAN_PROXY_PID_FILE"
  launch_in_own_session "$ROOT_DIR/frontend" "$LAN_PROXY_LOG_FILE" "$LAN_PROXY_PID_FILE" "${LAN_PROXY_CMD[@]}" || return 1
  NEW_LAN_PROXY_PID="$(read_pid "$LAN_PROXY_PID_FILE")"
}

cleanup_started() {
  local pid fragment pid_file index
  # 仅回收本次创建且仍与命令和独立进程组匹配的进程，不按端口兜底杀进程。
  for index in 0 1 2; do
    if [[ "$index" == 0 ]]; then
      pid="$NEW_BACKEND_PID"; fragment="uvicorn codepilot.main:app"; pid_file="$BACKEND_PID_FILE"
    elif [[ "$index" == 1 ]]; then
      pid="$NEW_FRONTEND_PID"; fragment="pnpm dev --host"; pid_file="$FRONTEND_PID_FILE"
    else
      pid="$NEW_LAN_PROXY_PID"; fragment="lan-https-proxy.mjs"; pid_file="$LAN_PROXY_PID_FILE"
    fi
    [[ -n "$pid" ]] || continue
    if is_pid_running "$pid" && pid_matches_command "$pid" "$fragment" \
      && [[ "$(get_process_group_id "$pid")" == "$pid" ]]; then
      stop_process_group "$pid" TERM
      sleep 0.5
      if is_pid_running "$pid" && pid_matches_command "$pid" "$fragment" \
        && [[ "$(get_process_group_id "$pid")" == "$pid" ]]; then
        stop_process_group "$pid" KILL
      fi
    fi
    if [[ "$(read_pid "$pid_file")" == "$pid" ]]; then rm -f "$pid_file"; fi
  done
}

listener_belongs_to() {
  local listener
  listener="$(find_listener_pid "$1")"
  [[ -n "$listener" ]] && [[ "$(get_process_group_id "$listener")" == "$(get_process_group_id "$2")" ]]
}

wait_until_ready() {
  local deadline=$((SECONDS + 20)) backend_ready=0 frontend_ready=0 lan_ready=0 pid host="$DEV_HOST"
  [[ "$host" != "::1" ]] || host="[::1]"
  # 两个组件共享截止时间；不使用代理，也不读取可能包含敏感内容的响应正文。
  while (( SECONDS < deadline )); do
    local components=(backend frontend)
    [[ "$LAN_ENABLED" == 0 ]] || components+=(lan)
    for component in "${components[@]}"; do
      if [[ "$component" == backend ]]; then
        pid="$(read_pid "$BACKEND_PID_FILE")"
      elif [[ "$component" == frontend ]]; then
        pid="$(read_pid "$FRONTEND_PID_FILE")"
      else
        pid="$(read_pid "$LAN_PROXY_PID_FILE")"
      fi
      if ! is_pid_running "$pid"; then
        if [[ "$component" == backend ]]; then echo "错误: 后端在就绪前退出，请查看本地后端日志。"
        elif [[ "$component" == frontend ]]; then echo "错误: 前端在就绪前退出，请查看本地前端日志。"
        else echo "错误: 局域网 HTTPS 代理在就绪前退出，请查看本地代理日志。"; fi
        return 1
      fi
    done
    backend_ready=0; frontend_ready=0
    if [[ "$(curl --noproxy '*' --connect-timeout 1 --max-time 1 -s -o /dev/null -w '%{http_code}' "http://${host}:${BACKEND_PORT}/api/health/ready")" == 200 ]] \
      && listener_belongs_to "$BACKEND_PORT" "$(read_pid "$BACKEND_PID_FILE")"; then backend_ready=1; fi
    (( SECONDS < deadline )) || break
    if [[ "$(curl --noproxy '*' --connect-timeout 1 --max-time 1 -s -o /dev/null -w '%{http_code}' "http://${host}:${FRONTEND_PORT}/")" == 200 ]] \
      && listener_belongs_to "$FRONTEND_PORT" "$(read_pid "$FRONTEND_PID_FILE")"; then frontend_ready=1; fi
    if [[ "$LAN_ENABLED" == 1 ]] && [[ "$(curl -k --noproxy '*' --connect-timeout 1 --max-time 1 -s -o /dev/null -w '%{http_code}' "https://127.0.0.1:${LAN_PORT}/api/health/ready")" == 200 ]] \
      && listener_belongs_to "$LAN_PORT" "$(read_pid "$LAN_PROXY_PID_FILE")"; then lan_ready=1; fi
    if (( backend_ready && frontend_ready )) && { [[ "$LAN_ENABLED" == 0 ]] || (( lan_ready )); } && (( SECONDS < deadline )); then return 0; fi
    sleep 0.2
  done
  (( backend_ready )) || echo "错误: 后端就绪检查超时（两个组件共用 20 秒），请检查本地日志、模型配置和迁移状态。"
  (( frontend_ready )) || echo "错误: 前端就绪检查超时（两个组件共用 20 秒），请检查本地前端日志。"
  [[ "$LAN_ENABLED" == 0 ]] || (( lan_ready )) || echo "错误: 局域网 HTTPS 代理就绪检查超时。"
  return 1
}

stop_service() {
  local name="$1"
  local pid_file="$2"
  local expected_fragment="$3"
  local fallback_fragment="$4"
  local port="$5"
  local pid
  pid="$(read_pid "$pid_file")"

  if [[ -z "${pid:-}" ]]; then
    pid="$(find_listener_pid "$port")"
    if [[ -z "${pid:-}" ]]; then
      echo "$name 未运行。"
      return
    fi
    if ! pid_matches_command "$pid" "$fallback_fragment" && ! ancestor_matches_command "$pid" "$fallback_fragment"; then
      echo "警告: $name 端口 ${port} 被其他进程占用，跳过停止，请手动检查。"
      return
    fi
    echo "$name 缺少 PID 文件，按端口回收残留进程。"
  fi

  if ! is_pid_running "$pid"; then
    rm -f "$pid_file"
    pid="$(find_listener_pid "$port")"
    if [[ -z "${pid:-}" ]]; then
      echo "$name 的 PID 文件已失效，已清理。"
      return
    fi
    if ! pid_matches_command "$pid" "$fallback_fragment" && ! ancestor_matches_command "$pid" "$fallback_fragment"; then
      echo "警告: $name 端口 ${port} 被其他进程占用，跳过停止，请手动检查。"
      return
    fi
    echo "$name 的 PID 文件已失效，按端口回收残留进程。"
  fi

  if ! pid_matches_command "$pid" "$expected_fragment" \
    && ! pid_matches_command "$pid" "$fallback_fragment" \
    && ! ancestor_matches_command "$pid" "$expected_fragment" \
    && ! ancestor_matches_command "$pid" "$fallback_fragment"; then
    echo "警告: $name 的 PID=${pid} 与预期启动命令不匹配，跳过停止，请手动检查。"
    return
  fi

  stop_process_group "$pid" "TERM"
  for _ in {1..20}; do
    if ! is_pid_running "$pid"; then
      rm -f "$pid_file"
      echo "$name 已停止。"
      return
    fi
    sleep 0.5
  done

  stop_process_group "$pid" "KILL"
  rm -f "$pid_file"
  echo "$name 超时未退出，已强制停止。"
}

start_all() {
  require_command "uv"
  require_command "pnpm"
  require_command "node"
  require_command "python3"
  require_command "curl"
  require_command "lsof"
  if [[ "$LAN_ENABLED" == 1 ]]; then require_command "openssl"; require_command "ipconfig"; require_file "$ROOT_DIR/frontend/scripts/lan-https-proxy.mjs"; fi
  require_file "$ROOT_DIR/backend/pyproject.toml"
  require_file "$ROOT_DIR/backend/uv.lock"
  require_file "$ROOT_DIR/frontend/package.json"
  require_file "$ROOT_DIR/frontend/pnpm-lock.yaml"
  require_file "$ROOT_DIR/backend/.venv/bin/python"
  require_file "$ROOT_DIR/backend/.venv/bin/uvicorn"
  require_file "$ROOT_DIR/frontend/node_modules/vite/bin/vite.js"
  if ! "$ROOT_DIR/backend/.venv/bin/python" -c 'import sys; assert (3,12) <= sys.version_info < (3,15); import uvicorn, fastapi, litellm' >/dev/null 2>&1; then
    echo "错误: 后端依赖检查失败，请在 backend 执行 uv sync --frozen。"
    return 1
  fi

  trap cleanup_started EXIT
  trap 'exit 1' INT TERM
  if [[ "$LAN_ENABLED" == 1 ]]; then
    discover_lan_addresses && prepare_lan_certificate || { echo "错误: 无法准备局域网 HTTPS 入口。"; return 1; }
  fi
  start_backend && start_frontend && { [[ "$LAN_ENABLED" == 0 ]] || start_lan_proxy; } && wait_until_ready || return 1
  trap - EXIT INT TERM

  echo "启动完成。"
  echo "后端地址: http://${DEV_HOST}:8000"
  echo "前端地址: http://${DEV_HOST}:5173"
  echo "手机入口: http://${DEV_HOST}:5173/mobile"
  if [[ "$LAN_ENABLED" == 1 ]]; then for address in "${LAN_ADDRESSES[@]}"; do echo "局域网入口: https://${address}:${LAN_PORT}"; done; fi
}

stop_all() {
  stop_service "局域网 HTTPS 代理" "$LAN_PROXY_PID_FILE" "lan-https-proxy.mjs" "lan-https-proxy.mjs" "$LAN_PORT"
  stop_service "后端" "$BACKEND_PID_FILE" "uvicorn codepilot.main:app" "uvicorn codepilot.main:app" "$BACKEND_PORT"
  stop_service "前端" "$FRONTEND_PID_FILE" "pnpm dev --host" "vite.js --host" "$FRONTEND_PORT"
}

restart_all() {
  stop_all
  start_all
}

case "${1:-}" in
  start)
    start_all
    ;;
  stop)
    stop_all
    ;;
  restart)
    restart_all
    ;;
  *)
    print_usage
    exit 1
    ;;
esac
