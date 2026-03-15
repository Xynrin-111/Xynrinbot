#!/usr/bin/env bash

proxy_config_file() {
  printf '%s\n' "$PROJECT_DIR/config/appsettings.json"
}

read_project_proxy_value() {
  local key="$1"
  if [ ! -f "$(proxy_config_file)" ]; then
    return 0
  fi
  python3 "$PROJECT_DIR/scripts/projectctl.py" get "proxy.$key" 2>/dev/null || true
}

apply_network_proxy_env() {
  local http_proxy_value
  local https_proxy_value
  local all_proxy_value
  local no_proxy_value

  http_proxy_value="${HTTP_PROXY:-${http_proxy:-}}"
  https_proxy_value="${HTTPS_PROXY:-${https_proxy:-}}"
  all_proxy_value="${ALL_PROXY:-${all_proxy:-}}"
  no_proxy_value="${NO_PROXY:-${no_proxy:-}}"

  if [ -z "$http_proxy_value" ]; then
    http_proxy_value="$(read_project_proxy_value http_proxy)"
  fi
  if [ -z "$https_proxy_value" ]; then
    https_proxy_value="$(read_project_proxy_value https_proxy)"
  fi
  if [ -z "$all_proxy_value" ]; then
    all_proxy_value="$(read_project_proxy_value all_proxy)"
  fi
  if [ -z "$no_proxy_value" ]; then
    no_proxy_value="$(read_project_proxy_value no_proxy)"
  fi

  if [ -n "$http_proxy_value" ]; then
    export HTTP_PROXY="$http_proxy_value"
    export http_proxy="$http_proxy_value"
  fi
  if [ -n "$https_proxy_value" ]; then
    export HTTPS_PROXY="$https_proxy_value"
    export https_proxy="$https_proxy_value"
  fi
  if [ -n "$all_proxy_value" ]; then
    export ALL_PROXY="$all_proxy_value"
    export all_proxy="$all_proxy_value"
  fi
  if [ -n "$no_proxy_value" ]; then
    export NO_PROXY="$no_proxy_value"
    export no_proxy="$no_proxy_value"
  fi
}

print_network_proxy_summary() {
  if [ -n "${HTTP_PROXY:-${http_proxy:-}}" ] || [ -n "${HTTPS_PROXY:-${https_proxy:-}}" ] || [ -n "${ALL_PROXY:-${all_proxy:-}}" ]; then
    echo "已启用统一代理环境。"
    return
  fi
  echo "当前未配置统一代理。"
}
