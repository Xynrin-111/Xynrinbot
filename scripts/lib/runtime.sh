#!/usr/bin/env bash

runtime_mode() {
  local mode="${PYTHON_RUNTIME_MODE:-}"
  if [ -z "$mode" ] && [ -f "$PROJECT_DIR/config/appsettings.json" ]; then
    mode="$(python3 "$PROJECT_DIR/scripts/projectctl.py" get runtime.python_mode 2>/dev/null || true)"
  fi
  mode="${mode:-project}"
  case "$mode" in
    venv|project)
      printf '%s\n' "$mode"
      ;;
    *)
      echo "错误：PYTHON_RUNTIME_MODE 仅支持 venv 或 project，当前为 $mode" >&2
      return 1
      ;;
  esac
}

runtime_dir() {
  printf '%s\n' "$PROJECT_DIR/.runtime"
}

runtime_site_packages() {
  printf '%s\n' "$(runtime_dir)/site-packages"
}

runtime_browser_dir() {
  printf '%s\n' "$(runtime_dir)/playwright"
}

runtime_python_bin() {
  if [ "$(runtime_mode)" = "venv" ]; then
    printf '%s\n' "$PROJECT_DIR/.venv/bin/python"
    return
  fi
  printf '%s\n' "python3"
}

runtime_dep_check() {
  run_python - <<'PY'
import importlib
import sys

required = [
    "nonebot",
    "nonebot.adapters.onebot.v11",
    "sqlalchemy",
    "aiosqlite",
    "playwright",
]
missing = []
for name in required:
    try:
        importlib.import_module(name)
    except Exception:
        missing.append(name)
if missing:
    print(",".join(missing))
    raise SystemExit(1)
PY
}

runtime_exists() {
  if [ "$(runtime_mode)" = "venv" ]; then
    [ -x "$PROJECT_DIR/.venv/bin/python" ]
    return
  fi
  [ -d "$(runtime_site_packages)" ]
}

ensure_runtime_layout() {
  mkdir -p "$(runtime_dir)" "$(runtime_site_packages)" "$(runtime_browser_dir)"
}

runtime_print_summary() {
  if [ "$(runtime_mode)" = "venv" ]; then
    echo "Python 运行模式：venv (.venv)"
    return
  fi
  echo "Python 运行模式：project (.runtime/site-packages)"
}

ensure_python_runtime() {
  if [ "$(runtime_mode)" = "venv" ]; then
    if [ ! -d "$PROJECT_DIR/.venv" ]; then
      python3 -m venv "$PROJECT_DIR/.venv"
    fi
    return
  fi
  ensure_runtime_layout
}

run_python() {
  if [ "$(runtime_mode)" = "venv" ]; then
    "$PROJECT_DIR/.venv/bin/python" "$@"
    return
  fi
  PYTHONPATH="$(runtime_site_packages)${PYTHONPATH:+:$PYTHONPATH}" \
  PLAYWRIGHT_BROWSERS_PATH="$(runtime_browser_dir)" \
  python3 "$@"
}

exec_python() {
  if [ "$(runtime_mode)" = "venv" ]; then
    exec "$PROJECT_DIR/.venv/bin/python" "$@"
  fi
  exec env \
    PYTHONPATH="$(runtime_site_packages)${PYTHONPATH:+:$PYTHONPATH}" \
    PLAYWRIGHT_BROWSERS_PATH="$(runtime_browser_dir)" \
    python3 "$@"
}

ensure_python_deps() {
  if runtime_exists && runtime_dep_check >/dev/null 2>&1; then
    echo "Python 依赖已就绪，跳过重复安装。"
    return
  fi

  echo "同步 Python 依赖..."
  if [ "$(runtime_mode)" = "venv" ]; then
    "$PROJECT_DIR/.venv/bin/python" -m pip install -U --disable-pip-version-check pip >/dev/null
    "$PROJECT_DIR/.venv/bin/python" -m pip install --disable-pip-version-check --no-build-isolation --no-warn-conflicts -e . >/dev/null
    return
  fi
  ensure_runtime_layout
  python3 -m pip install \
    --disable-pip-version-check \
    --no-build-isolation \
    --no-warn-conflicts \
    --ignore-installed \
    --target "$(runtime_site_packages)" \
    . >/dev/null
}

ensure_playwright_browser() {
  echo "检查 Playwright Chromium..."
  if [ "$(runtime_mode)" = "venv" ]; then
    if [ ! -x "$PROJECT_DIR/.venv/bin/python" ]; then
      return
    fi
    if compgen -G "$HOME/.cache/ms-playwright/chromium*" >/dev/null; then
      echo "Playwright Chromium 已存在，跳过安装。"
      return
    fi
    "$PROJECT_DIR/.venv/bin/python" -m playwright install chromium
    return
  fi

  ensure_runtime_layout
  if compgen -G "$(runtime_browser_dir)/chromium*" >/dev/null; then
    echo "Playwright Chromium 已存在，跳过安装。"
    return
  fi
  PYTHONPATH="$(runtime_site_packages)${PYTHONPATH:+:$PYTHONPATH}" \
  PLAYWRIGHT_BROWSERS_PATH="$(runtime_browser_dir)" \
  python3 -m playwright install chromium
}
