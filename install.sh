#!/usr/bin/env bash

set -euo pipefail

REPO_SLUG="${REPO_SLUG:-Xynrin-111/Xynrinbot}"
REPO_REF="${REPO_REF:-main}"
TOOL_SUBDIR="${TOOL_SUBDIR:-}"
INSTALL_BASE_DIR="${INSTALL_BASE_DIR:-$PWD}"
INSTALL_DIR_DEFAULT_NAME="${INSTALL_DIR_DEFAULT_NAME:-$(basename "${TOOL_SUBDIR:-${REPO_SLUG##*/}}")}"
INSTALL_DIR="${INSTALL_DIR:-$INSTALL_BASE_DIR/$INSTALL_DIR_DEFAULT_NAME}"
BOOTSTRAP_AFTER_DOWNLOAD="${BOOTSTRAP_AFTER_DOWNLOAD:-1}"
AUTO_START="${AUTO_START:-0}"
FORCE_OVERWRITE="${FORCE_OVERWRITE:-0}"

INSTALL_PROFILE="${INSTALL_PROFILE:-}"
APP_HOST="${APP_HOST:-}"
APP_PORT="${APP_PORT:-}"
ADMIN_LOCAL_ONLY="${ADMIN_LOCAL_ONLY:-}"
AUTO_OPEN_ADMIN_UI="${AUTO_OPEN_ADMIN_UI:-}"
INSTALL_ONEBOT_CLIENT="${INSTALL_ONEBOT_CLIENT:-}"
VERIFY_ADMIN_PASSWORD="${VERIFY_ADMIN_PASSWORD:-}"
VERIFY_ADMIN_USERNAME="${VERIFY_ADMIN_USERNAME:-admin}"
PYTHON_RUNTIME_MODE="${PYTHON_RUNTIME_MODE:-project}"
INTERACTIVE_INSTALL="${INTERACTIVE_INSTALL:-auto}"

download_file() {
  local url="$1"
  local output="$2"

  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --retry 3 --retry-delay 2 -o "$output" "$url"
    return
  fi

  if command -v wget >/dev/null 2>&1; then
    wget -qO "$output" "$url"
    return
  fi

  echo "错误：未找到 curl 或 wget，无法在线下载项目。"
  exit 1
}

require_command() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "错误：缺少命令 $cmd"
    exit 1
  fi
}

is_interactive_install() {
  case "$INTERACTIVE_INSTALL" in
    1|true|yes|on)
      return 0
      ;;
    0|false|no|off)
      return 1
      ;;
    auto)
      [ -t 0 ] && [ -t 1 ]
      return
      ;;
    *)
      return 1
      ;;
  esac
}

prompt_text() {
  local prompt="$1"
  local default_value="${2:-}"
  local input=""

  if [ -n "$default_value" ]; then
    printf "%s [%s]: " "$prompt" "$default_value" >&2
  else
    printf "%s: " "$prompt" >&2
  fi
  IFS= read -r input || true
  if [ -z "$input" ]; then
    printf '%s\n' "$default_value"
    return
  fi
  printf '%s\n' "$input"
}

prompt_bool() {
  local prompt="$1"
  local default_value="${2:-true}"
  local suffix="Y/n"
  local input=""

  case "$default_value" in
    true)
      suffix="Y/n"
      ;;
    false)
      suffix="y/N"
      ;;
  esac

  while true; do
    printf "%s [%s]: " "$prompt" "$suffix" >&2
    IFS= read -r input || true
    input="$(printf '%s' "$input" | tr '[:upper:]' '[:lower:]')"
    if [ -z "$input" ]; then
      printf '%s\n' "$default_value"
      return
    fi
    case "$input" in
      y|yes|1|true)
        printf 'true\n'
        return
        ;;
      n|no|0|false)
        printf 'false\n'
        return
        ;;
    esac
    echo "请输入 y 或 n。" >&2
  done
}

prompt_choice() {
  local prompt="$1"
  local default_value="$2"
  shift 2
  local options=("$@")
  local input=""

  echo "$prompt" >&2
  for option in "${options[@]}"; do
    echo "  - $option" >&2
  done

  while true; do
    printf "请输入选项 [%s]: " "$default_value" >&2
    IFS= read -r input || true
    if [ -z "$input" ]; then
      printf '%s\n' "$default_value"
      return
    fi
    for option in "${options[@]}"; do
      if [ "$input" = "$option" ]; then
        printf '%s\n' "$input"
        return
      fi
    done
    echo "无效选项：$input" >&2
  done
}

is_valid_port() {
  local value="$1"
  case "$value" in
    ''|*[!0-9]*)
      return 1
      ;;
  esac
  [ "$value" -ge 1 ] && [ "$value" -le 65535 ]
}

apply_profile_defaults() {
  case "$INSTALL_PROFILE" in
    desktop)
      APP_HOST="${APP_HOST:-127.0.0.1}"
      APP_PORT="${APP_PORT:-8080}"
      ADMIN_LOCAL_ONLY="${ADMIN_LOCAL_ONLY:-true}"
      AUTO_OPEN_ADMIN_UI="${AUTO_OPEN_ADMIN_UI:-true}"
      INSTALL_ONEBOT_CLIENT="${INSTALL_ONEBOT_CLIENT:-none}"
      ;;
    server)
      APP_HOST="${APP_HOST:-127.0.0.1}"
      APP_PORT="${APP_PORT:-8080}"
      ADMIN_LOCAL_ONLY="${ADMIN_LOCAL_ONLY:-true}"
      AUTO_OPEN_ADMIN_UI="${AUTO_OPEN_ADMIN_UI:-false}"
      INSTALL_ONEBOT_CLIENT="${INSTALL_ONEBOT_CLIENT:-none}"
      ;;
    *)
      INSTALL_PROFILE="desktop"
      apply_profile_defaults
      ;;
  esac
}

collect_install_preferences() {
  if [ -n "$INSTALL_PROFILE" ] && [ -n "$APP_HOST" ] && [ -n "$APP_PORT" ] && [ -n "$ADMIN_LOCAL_ONLY" ] && [ -n "$AUTO_OPEN_ADMIN_UI" ] && [ -n "$INSTALL_ONEBOT_CLIENT" ]; then
    return
  fi

  if is_interactive_install; then
    echo "安装模式配置"
    INSTALL_PROFILE="$(prompt_choice "请选择安装模式" "${INSTALL_PROFILE:-desktop}" "desktop" "server")"
    apply_profile_defaults

    APP_HOST="$(prompt_text "WebUI 监听地址" "$APP_HOST")"
    while ! is_valid_port "${APP_PORT:-}"; do
      APP_PORT="$(prompt_text "WebUI 端口" "${APP_PORT:-8080}")"
      if ! is_valid_port "$APP_PORT"; then
        echo "端口必须是 1 到 65535 的整数。"
      fi
    done
    ADMIN_LOCAL_ONLY="$(prompt_bool "管理台是否仅允许本机访问" "$ADMIN_LOCAL_ONLY")"
    AUTO_OPEN_ADMIN_UI="$(prompt_bool "启动后是否自动打开本地管理台" "$AUTO_OPEN_ADMIN_UI")"
    INSTALL_ONEBOT_CLIENT="$(prompt_choice "是否自动安装 OneBot 客户端" "$INSTALL_ONEBOT_CLIENT" "none" "napcat")"
    if [ "$ADMIN_LOCAL_ONLY" = "false" ]; then
      while [ -z "$VERIFY_ADMIN_PASSWORD" ]; do
        VERIFY_ADMIN_PASSWORD="$(prompt_text "管理台对外开放时必须设置访问密码" "$VERIFY_ADMIN_PASSWORD")"
        if [ -z "$VERIFY_ADMIN_PASSWORD" ]; then
          echo "管理台暴露到网络时必须配置密码。"
        fi
      done
    fi

    if [ "$INSTALL_PROFILE" = "server" ] && [ "$APP_HOST" = "0.0.0.0" ] && [ "$ADMIN_LOCAL_ONLY" = "false" ]; then
      echo "警告：当前配置会把管理台直接暴露到外网。更安全的做法是保留本机访问，再走 SSH 隧道或反向代理。"
    fi
  else
    INSTALL_PROFILE="${INSTALL_PROFILE:-desktop}"
    apply_profile_defaults
  fi

  if ! is_valid_port "$APP_PORT"; then
    echo "错误：APP_PORT 必须是 1 到 65535 的整数。"
    exit 1
  fi
  if [ "$ADMIN_LOCAL_ONLY" = "false" ] && [ -z "$VERIFY_ADMIN_PASSWORD" ]; then
    echo "错误：管理台允许非本机访问时，必须提供 VERIFY_ADMIN_PASSWORD。"
    exit 1
  fi
}

write_install_metadata() {
  local target_dir="$1"
  cat > "$target_dir/.install-meta" <<EOF
REPO_SLUG=$REPO_SLUG
REPO_REF=$REPO_REF
TOOL_SUBDIR=$TOOL_SUBDIR
INSTALL_PROFILE=$INSTALL_PROFILE
APP_HOST=$APP_HOST
APP_PORT=$APP_PORT
ADMIN_LOCAL_ONLY=$ADMIN_LOCAL_ONLY
AUTO_OPEN_ADMIN_UI=$AUTO_OPEN_ADMIN_UI
INSTALL_ONEBOT_CLIENT=$INSTALL_ONEBOT_CLIENT
PYTHON_RUNTIME_MODE=$PYTHON_RUNTIME_MODE
EOF
}

configure_project_env() {
  local target_dir="$1"
  (
    cd "$target_dir"
    python3 scripts/projectctl.py init >/dev/null
    python3 scripts/projectctl.py set app.deploy_profile "$INSTALL_PROFILE" >/dev/null
    python3 scripts/projectctl.py set app.host "$APP_HOST" >/dev/null
    python3 scripts/projectctl.py set app.port "$APP_PORT" >/dev/null
    python3 scripts/projectctl.py set admin.local_only "$ADMIN_LOCAL_ONLY" >/dev/null
    python3 scripts/projectctl.py set admin.auto_open "$AUTO_OPEN_ADMIN_UI" >/dev/null
    python3 scripts/projectctl.py set admin.username "$VERIFY_ADMIN_USERNAME" >/dev/null
    python3 scripts/projectctl.py set admin.password "$VERIFY_ADMIN_PASSWORD" >/dev/null
    python3 scripts/projectctl.py set onebot.install_client "$INSTALL_ONEBOT_CLIENT" >/dev/null
    python3 scripts/projectctl.py set runtime.python_mode "$PYTHON_RUNTIME_MODE" >/dev/null
    python3 scripts/projectctl.py export-env >/dev/null
  )
}

render_install_summary() {
  echo
  echo "安装配置如下："
  echo "  模式：$INSTALL_PROFILE"
  echo "  WebUI：$APP_HOST:$APP_PORT"
  echo "  管理台仅本机访问：$ADMIN_LOCAL_ONLY"
  echo "  自动打开管理台：$AUTO_OPEN_ADMIN_UI"
  echo "  自动安装 OneBot：$INSTALL_ONEBOT_CLIENT"
  if [ "$ADMIN_LOCAL_ONLY" = "false" ]; then
    echo "  管理台账号：$VERIFY_ADMIN_USERNAME"
  fi
  echo "  Python 运行模式：$PYTHON_RUNTIME_MODE"
  echo
}

main() {
  local archive_url
  local tmp_dir
  local extract_root
  local source_dir

  require_command tar
  require_command mktemp
  require_command python3

  collect_install_preferences
  render_install_summary

  if [ -e "$INSTALL_DIR" ] && [ "$FORCE_OVERWRITE" != "1" ]; then
    echo "错误：目标目录已存在：$INSTALL_DIR"
    echo "如需覆盖，请先手动删除，或使用 FORCE_OVERWRITE=1。"
    exit 1
  fi

  archive_url="https://codeload.github.com/$REPO_SLUG/tar.gz/refs/heads/$REPO_REF"
  tmp_dir="$(mktemp -d)"

  echo "下载项目归档..."
  echo "仓库：$REPO_SLUG"
  echo "分支：$REPO_REF"
  if [ -n "$TOOL_SUBDIR" ]; then
    echo "子目录：$TOOL_SUBDIR"
  else
    echo "子目录：仓库根目录"
  fi
  download_file "$archive_url" "$tmp_dir/repo.tar.gz"

  echo "解压项目归档..."
  tar -xzf "$tmp_dir/repo.tar.gz" -C "$tmp_dir"
  extract_root="$(find "$tmp_dir" -maxdepth 1 -mindepth 1 -type d | head -n 1)"
  if [ -n "$TOOL_SUBDIR" ]; then
    source_dir="$extract_root/$TOOL_SUBDIR"
  else
    source_dir="$extract_root"
  fi

  if [ ! -d "$source_dir" ]; then
    if [ -n "$TOOL_SUBDIR" ]; then
      echo "错误：在仓库归档中未找到子目录：$TOOL_SUBDIR"
    else
      echo "错误：仓库归档根目录不存在，无法继续安装。"
    fi
    rm -rf "$tmp_dir"
    exit 1
  fi

  rm -rf "$INSTALL_DIR"
  mkdir -p "$(dirname "$INSTALL_DIR")"
  cp -r "$source_dir" "$INSTALL_DIR"
  write_install_metadata "$INSTALL_DIR"

  echo "项目已复制到：$INSTALL_DIR"

  if [ "$BOOTSTRAP_AFTER_DOWNLOAD" = "1" ]; then
    echo "执行初始化..."
    (
      cd "$INSTALL_DIR"
      INSTALL_PROFILE="$INSTALL_PROFILE" \
      APP_HOST="$APP_HOST" \
      APP_PORT="$APP_PORT" \
      ADMIN_LOCAL_ONLY="$ADMIN_LOCAL_ONLY" \
      AUTO_OPEN_ADMIN_UI="$AUTO_OPEN_ADMIN_UI" \
      INSTALL_ONEBOT_CLIENT="$INSTALL_ONEBOT_CLIENT" \
      PYTHON_RUNTIME_MODE="$PYTHON_RUNTIME_MODE" \
      INTERACTIVE_INSTALL=0 \
      bash install.sh --local-bootstrap
    )
  fi

  if [ "$AUTO_START" = "1" ]; then
    echo "执行启动..."
    (
      cd "$INSTALL_DIR"
      bash scripts/run.sh
    )
  fi

  rm -rf "$tmp_dir"

  echo
  echo "完成。"
  echo "项目目录：$INSTALL_DIR"
  if [ "$AUTO_START" != "1" ]; then
    echo "下一步可执行：cd \"$INSTALL_DIR\" && bash scripts/run.sh"
  fi
}

run_local_bootstrap() {
  local project_dir

  require_command python3

  project_dir="$(cd "$(dirname "$0")" && pwd)"
  cd "$project_dir"
  collect_install_preferences
  echo "初始化当前项目目录：$project_dir"
  ONEBOT_CLIENT="$INSTALL_ONEBOT_CLIENT" \
  PYTHON_RUNTIME_MODE="$PYTHON_RUNTIME_MODE" \
  bash scripts/run.sh --bootstrap-only
  configure_project_env "$project_dir"
  write_install_metadata "$project_dir"
  echo "已写入 WebUI 配置：$APP_HOST:$APP_PORT"
}

case "${1:-}" in
  --local-bootstrap)
    run_local_bootstrap
    ;;
  "")
    main
    ;;
  *)
    echo "不支持的参数：$1"
    echo "支持："
    echo "  install.sh"
    echo "  install.sh --local-bootstrap"
    exit 1
    ;;
esac
