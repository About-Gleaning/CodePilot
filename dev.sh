#!/usr/bin/env bash

set -u

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_DIR="$ROOT_DIR/.run"
BACKEND_PID_FILE="$RUN_DIR/backend.pid"
FRONTEND_PID_FILE="$RUN_DIR/frontend.pid"
BACKEND_LOG_FILE="$RUN_DIR/backend.log"
FRONTEND_LOG_FILE="$RUN_DIR/frontend.log"
BACKEND_PORT=8000
FRONTEND_PORT=5173
BACKEND_GRACEFUL_SHUTDOWN_SECONDS=5
DEV_HOST="${CODEPILOT_HOST:-127.0.0.1}"

BACKEND_CMD=(
  uv run uvicorn codepilot.main:app
  --app-dir src
  --reload
  --host "$DEV_HOST"
  --port "$BACKEND_PORT"
  # 将 Uvicorn 的优雅退出时间限制在 stop 脚本等待窗口内，避免 SSE 长连接导致外部强杀。
  --timeout-graceful-shutdown "$BACKEND_GRACEFUL_SHUTDOWN_SECONDS"
)
FRONTEND_CMD=(pnpm dev --host "$DEV_HOST" --port "$FRONTEND_PORT")

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

  rm -f "$BACKEND_PID_FILE"
  launch_in_own_session "$ROOT_DIR/backend" "$BACKEND_LOG_FILE" "$BACKEND_PID_FILE" "${BACKEND_CMD[@]}"
  pid="$(read_pid "$BACKEND_PID_FILE")"
  echo "后端已启动，PID=${pid}，日志: ${BACKEND_LOG_FILE}"
}

start_frontend() {
  local pid
  pid="$(read_pid "$FRONTEND_PID_FILE")"
  if [[ -n "${pid:-}" ]] && is_pid_running "$pid" && pid_matches_command "$pid" "pnpm dev --host"; then
    echo "前端已在运行，PID=$pid"
    return
  fi

  rm -f "$FRONTEND_PID_FILE"
  launch_in_own_session "$ROOT_DIR/frontend" "$FRONTEND_LOG_FILE" "$FRONTEND_PID_FILE" "${FRONTEND_CMD[@]}"
  pid="$(read_pid "$FRONTEND_PID_FILE")"
  echo "前端已启动，PID=${pid}，日志: ${FRONTEND_LOG_FILE}"
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
  require_command "python3"
  require_file "$ROOT_DIR/backend/pyproject.toml"
  require_file "$ROOT_DIR/backend/uv.lock"
  require_file "$ROOT_DIR/frontend/package.json"
  require_file "$ROOT_DIR/frontend/pnpm-lock.yaml"

  start_backend
  start_frontend

  echo "启动完成。"
  echo "后端地址: http://${DEV_HOST}:8000"
  echo "前端地址: http://${DEV_HOST}:5173"
  echo "手机入口: http://${DEV_HOST}:5173/mobile"
  if [[ "$DEV_HOST" == "0.0.0.0" ]]; then
    echo "局域网访问时，请把 0.0.0.0 替换为本机局域网 IP。"
  fi
}

stop_all() {
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
