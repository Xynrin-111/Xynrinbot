"""
管理台辅助动作。
"""

from __future__ import annotations

import asyncio
import html
import os
import signal
import subprocess
import webbrowser

from nonebot import logger

from .config import plugin_settings


def open_admin_page_if_needed() -> None:
    """按配置决定是否自动打开本地管理页面。"""
    if not plugin_settings.auto_open_admin:
        return
    admin_url = f"http://{plugin_settings.admin_host}:{plugin_settings.admin_port}{plugin_settings.admin_path}"
    try:
        webbrowser.open(admin_url)
    except Exception as exc:
        logger.warning(f"自动打开管理页面失败 url={admin_url} error={exc}")


def render_restart_progress_page(*, admin_path: str) -> str:
    """返回重启中的过渡页面。"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta http-equiv="refresh" content="6;url={html.escape(admin_path)}?system=1:机器人已重启，请确认页面状态是否恢复" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Xynrin - 正在重启机器人</title>
  <style>
    body {{
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      background: linear-gradient(180deg, #eef2f7 0%, #f7f9fb 100%);
      font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
      color: #18212f;
      padding: 24px;
    }}
    .card {{
      width: min(560px, 100%);
      background: white;
      border: 1px solid #d8dee6;
      border-radius: 22px;
      box-shadow: 0 18px 40px rgba(15, 23, 42, 0.08);
      padding: 28px;
    }}
    h1 {{ margin: 0 0 12px; font-size: 28px; }}
    p {{ margin: 0; line-height: 1.9; color: #667085; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>正在重启机器人</h1>
    <p>已经收到重启请求。页面会在 6 秒后自动返回管理台；如果没有自动恢复，请手动刷新一次。</p>
  </div>
</body>
</html>"""


def render_shutdown_progress_page() -> str:
    """返回停止中的过渡页面。"""
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Xynrin - 正在停止机器人</title>
  <style>
    body {
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      background: linear-gradient(180deg, #eef2f7 0%, #f7f9fb 100%);
      font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
      color: #18212f;
      padding: 24px;
    }
    .card {
      width: min(560px, 100%);
      background: white;
      border: 1px solid #d8dee6;
      border-radius: 22px;
      box-shadow: 0 18px 40px rgba(15, 23, 42, 0.08);
      padding: 28px;
    }
    h1 { margin: 0 0 12px; font-size: 28px; }
    p { margin: 0; line-height: 1.9; color: #667085; }
  </style>
</head>
<body>
  <div class="card">
    <h1>机器人正在安全退出</h1>
    <p>当前进程会在几秒内停止。停止后你可以重新执行 bash scripts/run.sh，再按初始化流程继续。</p>
  </div>
</body>
</html>"""


async def restart_bot_process() -> None:
    """后台拉起新的机器人进程，然后请求当前进程优雅退出。"""
    project_root = plugin_settings.project_root
    restart_log = project_root / "data" / "group_verify" / "restart.log"
    restart_command = (
        f"sleep 2 && bash scripts/run.sh --start-only >> {restart_log.as_posix()} 2>&1"
    )
    subprocess.Popen(
        ["bash", "-lc", restart_command],
        cwd=project_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=os.environ.copy(),
    )
    await asyncio.sleep(1)
    os.kill(os.getpid(), signal.SIGTERM)


async def stop_bot_process() -> None:
    """优雅停止当前机器人进程。"""
    await asyncio.sleep(1)
    os.kill(os.getpid(), signal.SIGTERM)
