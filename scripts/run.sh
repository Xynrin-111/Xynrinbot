#!/usr/bin/env bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"
source "$PROJECT_DIR/scripts/lib/runtime.sh"
source "$PROJECT_DIR/scripts/lib/network.sh"
SKIP_ONEBOT_INSTALL="${SKIP_ONEBOT_INSTALL:-0}"

apply_network_proxy_env

find_existing_bot_pid() {
  local pid=""
  pid="$(pgrep -f "$PROJECT_DIR/bot.py" | head -n 1 || true)"
  if [ -n "$pid" ]; then
    printf '%s\n' "$pid"
    return 0
  fi
  pid="$(pgrep -f "python3 bot.py" | head -n 1 || true)"
  printf '%s\n' "$pid"
}

port_is_listening() {
  local host="$1"
  local port="$2"
  python3 - "$host" "$port" <<'PY'
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])
targets = []
if host == "0.0.0.0":
    targets = ["127.0.0.1"]
else:
    targets = [host]
for target in targets:
    try:
        with socket.create_connection((target, port), timeout=1):
            raise SystemExit(0)
    except OSError:
        continue
raise SystemExit(1)
PY
}

BOOTSTRAP_ONLY=0
START_ONLY=0
FOREGROUND=0

for arg in "$@"; do
  case "$arg" in
    --bootstrap-only)
      BOOTSTRAP_ONLY=1
      ;;
    --start-only)
      START_ONLY=1
      ;;
    --foreground)
      FOREGROUND=1
      ;;
    *)
      echo "不支持的参数：$arg"
      echo "可选参数：--bootstrap-only / --start-only / --foreground"
      exit 1
      ;;
  esac
done

need_bootstrap=0
if ! runtime_exists || [ ! -f "config/appsettings.json" ]; then
  need_bootstrap=1
fi

if [ "$START_ONLY" -eq 0 ] && [ "$need_bootstrap" -eq 1 ] || [ "$BOOTSTRAP_ONLY" -eq 1 ]; then
  echo "[1/6] 检查 Python3"
  python3 --version

  echo "[2/6] 准备 Python 运行环境"
  ensure_python_runtime
  runtime_print_summary

  echo "[3/6] 安装 Python 依赖"
  ensure_python_deps

  echo "[4/6] 安装 Playwright Chromium"
  ensure_playwright_browser

  echo "[5/6] 初始化项目配置"
  run_python scripts/projectctl.py init >/dev/null
  run_python scripts/projectctl.py export-env >/dev/null
  echo "已同步 config/appsettings.json 与 .env。"

  if [ "$SKIP_ONEBOT_INSTALL" = "1" ]; then
    echo "[6/6] 已按要求跳过 OneBot 客户端安装"
  else
    echo "[6/6] 安装 OneBot 客户端（默认跳过，可用 ONEBOT_CLIENT=napcat 显式启用）"
    bash scripts/install_onebot.sh
  fi
fi

if [ "$BOOTSTRAP_ONLY" -eq 1 ]; then
  echo
  echo "初始化完成。下一步运行：bash scripts/run.sh"
  exit 0
fi

if ! runtime_exists || [ ! -f "config/appsettings.json" ]; then
  echo "缺少 Python 运行环境或 config/appsettings.json，无法启动。请先运行：bash scripts/run.sh"
  exit 1
fi

echo "检查并补齐运行依赖..."
ensure_python_deps

echo "同步环境变量兼容文件..."
run_python scripts/projectctl.py export-env >/dev/null

echo "检查配置..."
run_python scripts/check_env.py --quiet

app_host="$(run_python scripts/projectctl.py get app.host)"
app_port="$(run_python scripts/projectctl.py get app.port)"
admin_path="$(run_python scripts/projectctl.py get admin.path)"
app_host="${app_host:-127.0.0.1}"
app_port="${app_port:-8080}"
admin_path="${admin_path:-/admin}"
if [[ "$admin_path" != /* ]]; then
  admin_path="/$admin_path"
fi

if [ "$app_host" = "0.0.0.0" ]; then
  echo "初始化向导：http://127.0.0.1:$app_port${admin_path}/setup"
else
  echo "初始化向导：http://$app_host:$app_port${admin_path}/setup"
fi
echo "启动服务..."
mkdir -p data/group_verify
run_log="$PROJECT_DIR/data/group_verify/run.log"
pid_file="$PROJECT_DIR/data/group_verify/bot.pid"

if [ "$FOREGROUND" -eq 1 ]; then
  exec_python bot.py
fi

if [ -f "$pid_file" ]; then
  existing_pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [ -n "$existing_pid" ] && kill -0 "$existing_pid" 2>/dev/null; then
  echo "检测到机器人已在后台运行，PID=$existing_pid"
  echo "管理台入口：http://${app_host/0.0.0.0/127.0.0.1}:$app_port${admin_path}"
  exit 0
fi
fi

existing_pid="$(find_existing_bot_pid)"
if [ -n "$existing_pid" ] && kill -0 "$existing_pid" 2>/dev/null; then
  echo "$existing_pid" > "$pid_file"
  echo "检测到已有机器人进程正在运行，PID=$existing_pid"
  echo "管理台入口：http://${app_host/0.0.0.0/127.0.0.1}:$app_port${admin_path}"
  exit 0
fi

if port_is_listening "$app_host" "$app_port"; then
  echo "端口 $app_host:$app_port 已被占用。"
  echo "如果这是本项目，请直接打开：http://${app_host/0.0.0.0/127.0.0.1}:$app_port${admin_path}/setup"
  exit 0
fi

if [ "$(runtime_mode)" = "venv" ]; then
  if command -v setsid >/dev/null 2>&1; then
    setsid "$(runtime_python_bin)" "$PROJECT_DIR/bot.py" </dev/null >> "$run_log" 2>&1 &
  else
    nohup "$(runtime_python_bin)" "$PROJECT_DIR/bot.py" </dev/null >> "$run_log" 2>&1 &
  fi
else
  if command -v setsid >/dev/null 2>&1; then
    setsid env \
      PYTHONPATH="$(runtime_site_packages)${PYTHONPATH:+:$PYTHONPATH}" \
      PLAYWRIGHT_BROWSERS_PATH="$(runtime_browser_dir)" \
      python3 "$PROJECT_DIR/bot.py" </dev/null >> "$run_log" 2>&1 &
  else
    nohup env \
      PYTHONPATH="$(runtime_site_packages)${PYTHONPATH:+:$PYTHONPATH}" \
      PLAYWRIGHT_BROWSERS_PATH="$(runtime_browser_dir)" \
      python3 "$PROJECT_DIR/bot.py" </dev/null >> "$run_log" 2>&1 &
  fi
fi
bot_pid=$!
echo "$bot_pid" > "$pid_file"
sleep 1
if kill -0 "$bot_pid" 2>/dev/null; then
  display_host="$app_host"
  if [ "$display_host" = "0.0.0.0" ]; then
    display_host="127.0.0.1"
  fi
  echo "启动成功，请进入初始化向导：http://$display_host:$app_port${admin_path}/setup"
  exit 0
fi

echo "后台启动失败，请查看日志：$run_log"
exit 1
