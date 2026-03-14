#!/usr/bin/env bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"
SKIP_ONEBOT_INSTALL="${SKIP_ONEBOT_INSTALL:-0}"

read_env_value() {
  local key="$1"
  local env_file="$PROJECT_DIR/.env"

  if [ ! -f "$env_file" ]; then
    return
  fi

  awk -F= -v target="$key" '
    $0 !~ /^[[:space:]]*#/ && index($0, "=") > 0 {
      key=$1
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", key)
      if (key == target) {
        value=substr($0, index($0, "=") + 1)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
        print value
        exit
      }
    }
  ' "$env_file"
}

ensure_python_deps() {
  echo "同步 Python 依赖..."
  .venv/bin/python -m pip install -U pip >/dev/null
  .venv/bin/python -m pip install -e . >/dev/null
}

ensure_playwright_browser() {
  if [ ! -x ".venv/bin/playwright" ]; then
    return
  fi

  if ! compgen -G "$HOME/.cache/ms-playwright/chromium*" >/dev/null; then
    echo "安装 Playwright Chromium..."
    .venv/bin/playwright install chromium
    return
  fi

  echo "Playwright Chromium 已存在，跳过安装。"
}

BOOTSTRAP_ONLY=0
START_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --bootstrap-only)
      BOOTSTRAP_ONLY=1
      ;;
    --start-only)
      START_ONLY=1
      ;;
    *)
      echo "不支持的参数：$arg"
      echo "可选参数：--bootstrap-only / --start-only"
      exit 1
      ;;
  esac
done

need_bootstrap=0
if [ ! -d ".venv" ] || [ ! -f ".env" ]; then
  need_bootstrap=1
fi

if [ "$START_ONLY" -eq 0 ] && [ "$need_bootstrap" -eq 1 ] || [ "$BOOTSTRAP_ONLY" -eq 1 ]; then
  echo "[1/6] 检查 Python3"
  python3 --version

  echo "[2/6] 创建虚拟环境（已存在则直接复用）"
  if [ ! -d ".venv" ]; then
    python3 -m venv .venv
  fi

  echo "[3/6] 安装 Python 依赖"
  ensure_python_deps

  echo "[4/6] 安装 Playwright Chromium"
  ensure_playwright_browser

  echo "[5/6] 初始化 .env"
  if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "已自动创建 .env。"
  else
    echo ".env 已存在，跳过复制。"
  fi

  if [ "$SKIP_ONEBOT_INSTALL" = "1" ]; then
    echo "[6/6] 已按要求跳过 OneBot 客户端安装"
  else
    echo "[6/6] 自动安装 OneBot 客户端（默认 NapCat，可用 ONEBOT_CLIENT=none 跳过）"
    bash scripts/install_onebot.sh
  fi
fi

if [ "$BOOTSTRAP_ONLY" -eq 1 ]; then
  echo
  echo "初始化完成。下一步运行：bash scripts/run.sh"
  exit 0
fi

if [ ! -d ".venv" ] || [ ! -f ".env" ]; then
  echo "缺少 .venv 或 .env，无法启动。请先运行：bash scripts/run.sh"
  exit 1
fi

echo "检查并补齐运行依赖..."
ensure_python_deps

echo "开始检查配置..."
.venv/bin/python scripts/check_env.py

app_host="$(read_env_value HOST)"
app_port="$(read_env_value PORT)"
admin_path="$(read_env_value VERIFY_ADMIN_PATH)"
app_host="${app_host:-127.0.0.1}"
app_port="${app_port:-8080}"
admin_path="${admin_path:-/admin}"
if [[ "$admin_path" != /* ]]; then
  admin_path="/$admin_path"
fi

if [ "$app_host" = "0.0.0.0" ]; then
  echo "当前监听地址：0.0.0.0:$app_port"
  echo "首次启动向导地址：http://127.0.0.1:$app_port${admin_path}/setup"
  echo "正式管理台地址：http://127.0.0.1:$app_port${admin_path}"
else
  echo "首次启动向导地址：http://$app_host:$app_port${admin_path}/setup"
  echo "正式管理台地址：http://$app_host:$app_port${admin_path}"
fi
echo "启动 NoneBot2..."
exec .venv/bin/python bot.py
