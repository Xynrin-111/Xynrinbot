#!/usr/bin/env bash

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_ROOT="${ONEBOT_INSTALL_DIR:-$PROJECT_DIR/third_party/onebot}"
CLIENT_NAME="${ONEBOT_CLIENT:-none}"

source "$PROJECT_DIR/scripts/lib/network.sh"
apply_network_proxy_env

mkdir -p "$INSTALL_ROOT"

detect_arch() {
  local arch
  arch="$(uname -m)"
  case "$arch" in
    x86_64|amd64)
      echo "x64"
      ;;
    aarch64|arm64)
      echo "arm64"
      ;;
    *)
      echo ""
      ;;
  esac
}

download_file() {
  local url="$1"
  local output="$2"

  if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 3 --retry-delay 2 -o "$output" "$url"
    return
  fi

  if command -v wget >/dev/null 2>&1; then
    wget -O "$output" "$url"
    return
  fi

  echo "未找到 curl 或 wget，无法自动下载 OneBot 客户端。"
  exit 1
}

has_command() {
  command -v "$1" >/dev/null 2>&1
}

is_arch_like() {
  [ -f /etc/os-release ] || return 1
  grep -Eiq '(^ID=arch$|^ID_LIKE=.*arch|^ID=endeavouros$)' /etc/os-release
}

patch_env_qr_dir() {
  local target_dir="$1"
  python3 "$PROJECT_DIR/scripts/projectctl.py" set onebot.lagrange_qr_dir "$target_dir" >/dev/null
}

install_lagrange() {
  echo "Lagrange.OneBot 官方仓库已归档，旧的自动下载直链也已失效。"
  echo "为避免下载到过期包，脚本不再自动安装 Lagrange。"
  echo "请改用默认的 NapCat：bash scripts/run.sh --bootstrap-only"
  echo "如果你坚持使用 Lagrange，请自行准备本体后再放到本机目录，网页管理台仍然会自动识别二维码。"
  exit 1
}

has_valid_napcat_install() {
  local install_dir="$1"
  [ -f "$install_dir/opt/QQ/resources/app/app_launcher/napcat/napcat.mjs" ] || return 1
  [ -f "$install_dir/opt/QQ/resources/app/loadNapCat.js" ] || return 1
  return 0
}

install_napcat() {
  local work_dir
  local installer_path
  local install_dir

  install_dir="$INSTALL_ROOT/napcat"
  if [ -d "$install_dir" ]; then
    if has_valid_napcat_install "$install_dir"; then
      echo "已检测到现有 NapCat：$install_dir"
      patch_env_qr_dir "$install_dir"
      exit 0
    fi
    echo "检测到已有目录，但 NapCat Shell 不完整，准备重新修复安装：$install_dir"
    rm -rf "$install_dir"
  fi

  if is_arch_like; then
    install_napcat_arch_rootless
    exit 0
  fi

  work_dir="$(mktemp -d)"
  installer_path="$work_dir/napcat-install.sh"

  echo "自动下载并安装 NapCat..."
  echo "来源：https://nclatest.znin.net/NapNeko/NapCat-Installer/main/script/install.sh"
  download_file "https://nclatest.znin.net/NapNeko/NapCat-Installer/main/script/install.sh" "$installer_path"
  chmod +x "$installer_path"
  bash "$installer_path" --docker n --cli n --force
  rm -rf "$work_dir"

  if [ -d "$install_dir" ]; then
    patch_env_qr_dir "$install_dir"
    echo "NapCat 已安装到：$install_dir"
    echo "后续网页管理台会自动识别二维码目录。"
    exit 0
  fi

  echo "NapCat 安装脚本已执行，但未在 $install_dir 找到安装结果。"
  exit 1
}

install_napcat_arch_rootless() {
  local arch
  local work_dir
  local qq_deb
  local qq_url
  local napcat_zip
  local install_dir
  local data_tar
  local extracted_dir
  local target_folder
  local package_json
  local qq_exec

  arch="$(uname -m)"
  install_dir="$INSTALL_ROOT/napcat"
  work_dir="$(mktemp -d)"
  qq_deb="$work_dir/QQ.deb"
  napcat_zip="$work_dir/NapCat.Shell.zip"

  echo "检测到 Arch 系发行版，切换为内置 Rootless 安装流程..."

  if ! has_command sudo; then
    echo "未找到 sudo，无法自动安装 Arch 系依赖。"
    exit 1
  fi

  echo "安装 Arch 系运行依赖（需要 sudo）..."
  sudo pacman -Sy --needed --noconfirm \
    unzip jq curl xorg-server-xvfb xorg-xauth procps-ng cpio binutils \
    nss mesa atk at-spi2-atk gtk3 alsa-lib pango cairo libdrm \
    libxcursor libxrandr libxdamage libxcomposite libxfixes libxrender \
    libxi libxtst libxss cups libxkbcommon fontconfig ttf-dejavu \
    xcb-util xcb-util-image xcb-util-wm xcb-util-keysyms xcb-util-renderutil

  case "$arch" in
    x86_64|amd64)
      qq_url="https://dldir1.qq.com/qqfile/qq/QQNT/7516007c/linuxqq_3.2.25-45758_amd64.deb"
      ;;
    aarch64|arm64)
      qq_url="https://dldir1.qq.com/qqfile/qq/QQNT/7516007c/linuxqq_3.2.25-45758_arm64.deb"
      ;;
    *)
      echo "当前架构 $arch 暂未适配 Arch Rootless 自动安装。"
      rm -rf "$work_dir"
      exit 1
      ;;
  esac

  echo "下载 LinuxQQ..."
  download_file "$qq_url" "$qq_deb"

  echo "下载 NapCat Shell 包..."
  download_file "https://github.com/NapNeko/NapCatQQ/releases/latest/download/NapCat.Shell.zip" "$napcat_zip"

  rm -rf "$install_dir"
  mkdir -p "$install_dir" "$work_dir/deb" "$work_dir/napcat"

  echo "解压 LinuxQQ..."
  (
    cd "$work_dir/deb"
    ar x "$qq_deb"
  )
  data_tar="$(find "$work_dir/deb" -maxdepth 1 -type f -name 'data.tar.*' | head -n 1)"
  if [ -z "$data_tar" ]; then
    echo "未找到 LinuxQQ 的 data.tar 数据包，解压失败。"
    rm -rf "$work_dir"
    exit 1
  fi
  tar -xf "$data_tar" -C "$install_dir"

  echo "解压 NapCat..."
  unzip -q -o "$napcat_zip" -d "$work_dir/napcat"

  extracted_dir="$work_dir/napcat"
  target_folder="$install_dir/opt/QQ/resources/app/app_launcher"
  package_json="$install_dir/opt/QQ/resources/app/package.json"
  qq_exec="$install_dir/opt/QQ/qq"

  if [ ! -f "$extracted_dir/napcat.mjs" ]; then
    extracted_dir="$(find "$work_dir/napcat" -type f -name 'napcat.mjs' -printf '%h\n' | head -n 1)"
  fi
  if [ -z "$extracted_dir" ] || [ ! -f "$extracted_dir/napcat.mjs" ]; then
    echo "NapCat Shell 包结构无法识别，未找到 napcat.mjs。"
    rm -rf "$work_dir"
    exit 1
  fi

  mkdir -p "$target_folder/napcat"
  cp -r -f "$extracted_dir/." "$target_folder/napcat/"
  chmod -R +x "$target_folder/napcat" || true

  cat > "$install_dir/opt/QQ/resources/app/loadNapCat.js" <<EOF
(async () => {await import('file:///$target_folder/napcat/napcat.mjs');})();
EOF

  jq '.main = "./loadNapCat.js"' "$package_json" > "$work_dir/package.json.tmp"
  mv "$work_dir/package.json.tmp" "$package_json"
  chmod +x "$qq_exec" || true

  patch_env_qr_dir "$install_dir"
  rm -rf "$work_dir"

  echo "NapCat Rootless 版已安装到：$install_dir"
  echo "后续网页管理台会自动识别二维码目录，并可尝试自动启动。"
}

case "$CLIENT_NAME" in
  none)
    echo "已跳过 OneBot 客户端安装。"
    ;;
  napcat)
    install_napcat
    ;;
  lagrange)
    install_lagrange
    ;;
  *)
    echo "暂不支持自动安装的客户端类型：$CLIENT_NAME"
    echo "当前支持：napcat、lagrange、none"
    exit 1
    ;;
esac
