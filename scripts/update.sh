#!/usr/bin/env bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
META_FILE="$PROJECT_DIR/.install-meta"

source "$PROJECT_DIR/scripts/lib/network.sh"
apply_network_proxy_env

REPO_SLUG_DEFAULT="Xynrin-111/Xynrinbot"
REPO_REF_DEFAULT="main"
TOOL_SUBDIR_DEFAULT=""

REPO_SLUG="${REPO_SLUG:-}"
REPO_REF="${REPO_REF:-}"
TOOL_SUBDIR="${TOOL_SUBDIR:-}"
PRESERVE_ITEMS=("config/appsettings.json" ".env" ".venv" ".runtime" "data" "third_party" ".install-meta" ".git")
PYTHON_RUNTIME_MODE="${PYTHON_RUNTIME_MODE:-}"

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

  echo "错误：未找到 curl 或 wget，无法在线更新项目。"
  exit 1
}

require_command() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "错误：缺少命令 $cmd"
    exit 1
  fi
}

load_metadata() {
  if [ -f "$META_FILE" ]; then
    REPO_SLUG="${REPO_SLUG:-$(awk -F= '$1=="REPO_SLUG"{print substr($0, index($0, "=")+1); exit}' "$META_FILE")}"
    REPO_REF="${REPO_REF:-$(awk -F= '$1=="REPO_REF"{print substr($0, index($0, "=")+1); exit}' "$META_FILE")}"
    TOOL_SUBDIR="${TOOL_SUBDIR:-$(awk -F= '$1=="TOOL_SUBDIR"{print substr($0, index($0, "=")+1); exit}' "$META_FILE")}"
    PYTHON_RUNTIME_MODE="${PYTHON_RUNTIME_MODE:-$(awk -F= '$1=="PYTHON_RUNTIME_MODE"{print substr($0, index($0, "=")+1); exit}' "$META_FILE")}"
  fi

  REPO_SLUG="${REPO_SLUG:-$REPO_SLUG_DEFAULT}"
  REPO_REF="${REPO_REF:-$REPO_REF_DEFAULT}"
  TOOL_SUBDIR="${TOOL_SUBDIR:-$TOOL_SUBDIR_DEFAULT}"
  PYTHON_RUNTIME_MODE="${PYTHON_RUNTIME_MODE:-project}"
}

preserve_runtime_state() {
  local preserve_dir="$1"
  local item

  mkdir -p "$preserve_dir"
  for item in "${PRESERVE_ITEMS[@]}"; do
    if [ -e "$PROJECT_DIR/$item" ]; then
      mkdir -p "$(dirname "$preserve_dir/$item")"
      mv "$PROJECT_DIR/$item" "$preserve_dir/$item"
    fi
  done
}

restore_runtime_state() {
  local preserve_dir="$1"
  local item

  for item in "${PRESERVE_ITEMS[@]}"; do
    if [ -e "$preserve_dir/$item" ]; then
      mkdir -p "$(dirname "$PROJECT_DIR/$item")"
      rm -rf "$PROJECT_DIR/$item"
      mv "$preserve_dir/$item" "$PROJECT_DIR/$item"
    fi
  done
}

move_project_to_backup() {
  local backup_dir="$1"
  local path

  mkdir -p "$backup_dir"
  for path in "$PROJECT_DIR"/.[!.]* "$PROJECT_DIR"/..?* "$PROJECT_DIR"/*; do
    [ -e "$path" ] || continue
    mv "$path" "$backup_dir/"
  done
}

restore_project_backup() {
  local backup_dir="$1"
  local path

  for path in "$PROJECT_DIR"/.[!.]* "$PROJECT_DIR"/..?* "$PROJECT_DIR"/*; do
    [ -e "$path" ] || continue
    rm -rf "$path"
  done
  for path in "$backup_dir"/.[!.]* "$backup_dir"/..?* "$backup_dir"/*; do
    [ -e "$path" ] || continue
    mv "$path" "$PROJECT_DIR/"
  done
}

main() {
  local archive_url
  local tmp_dir
  local extract_root
  local source_dir
  local preserve_dir
  local backup_dir
  local staged_dir

  require_command tar
  require_command mktemp

  load_metadata

  archive_url="https://codeload.github.com/$REPO_SLUG/tar.gz/refs/heads/$REPO_REF"
  tmp_dir="$(mktemp -d)"
  preserve_dir="$tmp_dir/preserve"
  backup_dir="$tmp_dir/backup"
  staged_dir="$tmp_dir/staged"

  echo "更新项目..."
  echo "仓库：$REPO_SLUG"
  echo "分支：$REPO_REF"
  if [ -n "$TOOL_SUBDIR" ]; then
    echo "子目录：$TOOL_SUBDIR"
  else
    echo "子目录：仓库根目录"
  fi

  download_file "$archive_url" "$tmp_dir/repo.tar.gz"
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
      echo "错误：仓库归档根目录不存在，无法继续更新。"
    fi
    rm -rf "$tmp_dir"
    exit 1
  fi

  mkdir -p "$staged_dir"
  cp -r "$source_dir"/. "$staged_dir"
  preserve_runtime_state "$preserve_dir"
  move_project_to_backup "$backup_dir"
  if ! cp -r "$staged_dir"/. "$PROJECT_DIR"; then
    echo "错误：复制新代码失败，正在回滚旧版本。"
    restore_project_backup "$backup_dir"
    restore_runtime_state "$preserve_dir"
    rm -rf "$tmp_dir"
    exit 1
  fi
  restore_runtime_state "$preserve_dir"

  echo "同步 Python 依赖并保留现有 OneBot 运行目录..."
  if ! (
    cd "$PROJECT_DIR"
    SKIP_ONEBOT_INSTALL=1 PYTHON_RUNTIME_MODE="$PYTHON_RUNTIME_MODE" bash scripts/run.sh --bootstrap-only
  ); then
    echo "错误：更新后的初始化失败，正在回滚旧版本。"
    restore_project_backup "$backup_dir"
    restore_runtime_state "$preserve_dir"
    rm -rf "$tmp_dir"
    exit 1
  fi

  rm -rf "$tmp_dir"

  echo
  echo "更新完成。"
  echo "现有配置、数据库和 third_party 目录已保留。"
  echo "下一步可执行：cd \"$PROJECT_DIR\" && bash scripts/run.sh"
}

main "$@"
