"""
本地 Web 管理台。

目标用户是普通使用者，因此页面尽量直接给出：
1. 当前机器人是否在线
2. 当前客户端是否可启动
3. 目标群、超时时间等配置表单
4. 最近的验证记录
"""

from __future__ import annotations

import asyncio
import html
import os
import secrets
import subprocess
import webbrowser
from urllib.parse import parse_qs
from typing import Any

from fastapi import BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from nonebot import get_driver, logger

from .config import plugin_settings
from .service import verify_service


_ROUTES_REGISTERED = False
_ADMIN_CSRF_TOKEN = secrets.token_urlsafe(32)


def register_admin_routes() -> None:
    """向 FastAPI 驱动挂载本地管理页面。"""
    global _ROUTES_REGISTERED
    if _ROUTES_REGISTERED:
        return

    driver = get_driver()
    app = getattr(driver, "server_app", None)
    if app is None:
        logger.warning("当前驱动没有可用的 FastAPI server_app，管理页面未启用")
        return

    admin_path = plugin_settings.admin_path.rstrip("/") or "/admin"

    @app.get(admin_path, response_class=HTMLResponse)
    async def admin_home(request: Request) -> HTMLResponse:
        _ensure_admin_request_allowed(request)
        runtime_settings = await verify_service.get_runtime_settings()
        summary = await verify_service.get_dashboard_summary()
        records = await verify_service.get_recent_records(limit=20)
        detected_clients = await verify_service.get_detected_onebot_clients()
        primary_client = await verify_service.get_primary_onebot_client()
        selected_client_root = str(primary_client["root"]) if primary_client else None
        qr_image = await verify_service.get_latest_qr_image(selected_client_root=selected_client_root)
        template_profile = await verify_service.get_active_verify_template_profile()
        template_presets = await verify_service.get_verify_template_presets()
        verify_message_template = await verify_service.get_verify_message_template()
        html_text = _render_admin_page(
            admin_path=admin_path,
            runtime_settings=runtime_settings,
            summary=summary,
            records=records,
            detected_clients=detected_clients,
            primary_client=primary_client,
            qr_image_url=f"{admin_path}/qr" if qr_image is not None else "",
            qr_image_path=str(qr_image) if qr_image is not None else "",
            qr_image_version=str(int(qr_image.stat().st_mtime)) if qr_image is not None else "",
            template_profile=template_profile,
            template_presets=template_presets,
            verify_message_template=verify_message_template,
            message=str(request.query_params.get("saved", "")),
            template_message=str(request.query_params.get("template", "")),
            verify_message_notice=str(request.query_params.get("message_template", "")),
            onebot_message=str(request.query_params.get("onebot", "")),
            system_message=str(request.query_params.get("system", "")),
            csrf_token=_ADMIN_CSRF_TOKEN,
        )
        return HTMLResponse(html_text)

    @app.get(f"{admin_path}/setup", response_class=HTMLResponse)
    async def setup_page(request: Request) -> RedirectResponse:
        _ensure_admin_request_allowed(request)
        query = request.url.query
        return RedirectResponse(url=f"{admin_path}?{query}" if query else admin_path, status_code=303)

    @app.post(f"{admin_path}/settings", response_class=HTMLResponse)
    async def save_settings(request: Request) -> RedirectResponse:
        _ensure_admin_request_allowed(request)
        form = await _parse_settings_form(request)
        _ensure_csrf_token(form)
        config_map = {
            "target_groups": str(form.get("target_groups", "")).strip(),
            "superusers": str(form.get("superusers", "")).strip(),
            "timeout_minutes": str(form.get("timeout_minutes", "")).strip(),
            "max_error_times": str(form.get("max_error_times", "")).strip(),
            "playwright_browser": str(form.get("playwright_browser", "chromium")).strip(),
            "image_retry_times": str(form.get("image_retry_times", "")).strip(),
            "lagrange_qr_dir": str(form.get("lagrange_qr_dir", "")).strip(),
        }
        await verify_service.update_app_configs(config_map)
        return RedirectResponse(url=f"{admin_path}?saved=1", status_code=303)

    @app.post(f"{admin_path}/onebot/start")
    async def start_onebot(request: Request) -> RedirectResponse:
        _ensure_admin_request_allowed(request)
        form = await _parse_settings_form(request)
        _ensure_csrf_token(form)
        client_root = str(form.get("client_root", "")).strip()
        success, info = await verify_service.launch_detected_onebot(client_root)
        status = "1" if success else "0"
        return RedirectResponse(
            url=f"{admin_path}?onebot={status}:{info}",
            status_code=303,
        )

    @app.post(f"{admin_path}/template/save")
    async def save_template(request: Request) -> RedirectResponse:
        _ensure_admin_request_allowed(request)
        form = await _parse_settings_form(request)
        _ensure_csrf_token(form)
        template_html = str(form.get("template_html", ""))
        success, info = await verify_service.save_verify_template_html(template_html)
        status = "1" if success else "0"
        return RedirectResponse(url=f"{admin_path}?template={status}:{info}", status_code=303)

    @app.post(f"{admin_path}/template/reset")
    async def reset_template(request: Request) -> RedirectResponse:
        _ensure_admin_request_allowed(request)
        form = await _parse_settings_form(request)
        _ensure_csrf_token(form)
        await verify_service.reset_verify_template_html()
        return RedirectResponse(url=f"{admin_path}?template=1:已恢复默认模板，并切回经典蓝预设", status_code=303)

    @app.post(f"{admin_path}/message-template/save")
    async def save_message_template(request: Request) -> RedirectResponse:
        _ensure_admin_request_allowed(request)
        form = await _parse_settings_form(request)
        _ensure_csrf_token(form)
        template_text = str(form.get("verify_message_template", ""))
        success, info = await verify_service.save_verify_message_template(template_text)
        status = "1" if success else "0"
        return RedirectResponse(
            url=f"{admin_path}?message_template={status}:{info}",
            status_code=303,
        )

    @app.post(f"{admin_path}/template/preset")
    async def switch_template_preset(request: Request) -> RedirectResponse:
        _ensure_admin_request_allowed(request)
        form = await _parse_settings_form(request)
        _ensure_csrf_token(form)
        preset_key = str(form.get("preset_key", "")).strip()
        success, info = await verify_service.activate_verify_template_preset(preset_key)
        status = "1" if success else "0"
        return RedirectResponse(url=f"{admin_path}?template={status}:{info}", status_code=303)

    @app.post(f"{admin_path}/system/restart")
    async def restart_system(request: Request, background_tasks: BackgroundTasks) -> HTMLResponse:
        _ensure_admin_request_allowed(request)
        form = await _parse_settings_form(request)
        _ensure_csrf_token(form)
        background_tasks.add_task(_restart_bot_process)
        return HTMLResponse(
            _render_restart_progress_page(admin_path=admin_path),
            status_code=202,
        )

    @app.get(f"{admin_path}/guide", response_class=HTMLResponse)
    async def guide_page(request: Request) -> RedirectResponse:
        _ensure_admin_request_allowed(request)
        return RedirectResponse(url=admin_path, status_code=303)

    @app.get(f"{admin_path}/qr")
    async def qr_image(request: Request) -> FileResponse:
        _ensure_admin_request_allowed(request)
        client_root = str(request.query_params.get("client_root", "")).strip() or None
        qr_path = await verify_service.get_latest_qr_image(selected_client_root=client_root)
        if qr_path is None or not qr_path.exists():
            raise HTTPException(status_code=404, detail="当前还没有检测到登录二维码")
        response = FileResponse(path=qr_path, media_type="image/png", filename=qr_path.name)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    _ROUTES_REGISTERED = True
    logger.info(f"本地管理页面已启用: http://{plugin_settings.admin_host}:{plugin_settings.admin_port}{admin_path}")


async def _parse_settings_form(request: Request) -> dict[str, str]:
    """兼容普通表单提交，避免强依赖 python-multipart。"""
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type == "application/x-www-form-urlencoded":
        body = (await request.body()).decode("utf-8", errors="ignore")
        return {key: values[-1] if values else "" for key, values in parse_qs(body, keep_blank_values=True).items()}

    form = await request.form()
    return {key: str(value) for key, value in form.items()}


def open_admin_page_if_needed() -> None:
    """按配置决定是否自动打开本地管理页面。"""
    if not plugin_settings.auto_open_admin:
        return
    admin_url = f"http://{plugin_settings.admin_host}:{plugin_settings.admin_port}{plugin_settings.admin_path}"
    try:
        webbrowser.open(admin_url)
    except Exception as exc:
        logger.warning(f"自动打开管理页面失败 url={admin_url} error={exc}")


def _ensure_admin_request_allowed(request: Request) -> None:
    """限制管理台为本机访问，避免直接暴露管理接口。"""
    if not plugin_settings.admin_local_only:
        return
    client_host = getattr(getattr(request, "client", None), "host", "")
    if client_host not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(status_code=403, detail="管理台仅允许本机访问")


def _ensure_csrf_token(form: dict[str, str]) -> None:
    """校验管理台写操作使用的随机 token。"""
    if str(form.get("csrf_token", "")).strip() != _ADMIN_CSRF_TOKEN:
        raise HTTPException(status_code=403, detail="无效的管理台表单令牌")


def _render_admin_page(
    *,
    admin_path: str,
    runtime_settings: dict[str, Any],
    summary: dict[str, Any],
    records: list[Any],
    detected_clients: list[dict[str, str | bool]],
    primary_client: dict[str, str | bool] | None,
    qr_image_url: str,
    qr_image_path: str,
    qr_image_version: str,
    template_profile: Any,
    template_presets: list[dict[str, str | bool]],
    verify_message_template: str,
    message: str,
    template_message: str,
    verify_message_notice: str,
    onebot_message: str,
    system_message: str,
    csrf_token: str,
) -> str:
    """渲染首页 HTML。"""
    target_groups_text = ",".join(str(item) for item in sorted(runtime_settings["target_groups"]))
    superusers_text = ",".join(str(item) for item in sorted(runtime_settings["superusers"]))
    onebot_notice = _render_onebot_notice(onebot_message)
    has_basic_config = bool(runtime_settings["target_groups"] and runtime_settings["superusers"])
    bot_online = bool(summary["bot_online"])
    selected_client_root = str(primary_client["root"]) if primary_client else ""
    qr_query_suffix = f"&t={html.escape(qr_image_version)}" if qr_image_version else ""
    qr_block = (
        f'<img src="{html.escape(qr_image_url)}?client_root={html.escape(selected_client_root)}{qr_query_suffix}" alt="机器人账号登录二维码" class="qr-image" />'
        if qr_image_url
        else '<div class="empty-box">还没有检测到二维码。先点击下面的“启动当前客户端”，等待 5 到 15 秒后刷新本页。</div>'
    )
    next_action = _render_admin_next_action(
        admin_path=admin_path,
        bot_online=bot_online,
        has_basic_config=has_basic_config,
        primary_client=primary_client,
        has_qr_image=bool(qr_image_url),
        qr_image_path=qr_image_path,
    )
    client_list = _render_detected_clients(
        clients=detected_clients,
        selected_client_root=selected_client_root,
    )
    primary_client_card = _render_primary_client_card(
        admin_path=admin_path,
        csrf_token=csrf_token,
        primary_client=primary_client,
    )
    rows = "".join(
        (
            "<tr>"
            f"<td>{record.id}</td>"
            f"<td>{record.user_id}</td>"
            f"<td>{record.group_id}</td>"
            f"<td>{html.escape(record.verify_code)}</td>"
            f"<td>{html.escape(record.status)}</td>"
            f"<td>{record.error_count}</td>"
            f"<td>{record.expire_time.strftime('%Y-%m-%d %H:%M:%S')}</td>"
            "</tr>"
        )
        for record in records
    )
    if not rows:
        rows = '<tr><td colspan="7">暂无验证记录</td></tr>'

    save_message = '<div class="notice success">基础配置已保存。</div>' if message else ""
    template_notice = _render_template_notice(template_message)
    verify_message_template_notice = _render_template_notice(verify_message_notice)
    system_notice = _render_system_notice(system_message)
    template_preset_cards = "".join(
        (
            '<div class="preset-card{active_class}">'
            f'<div class="preset-title-row"><div class="preset-title">{html.escape(str(preset["name"]))}</div>'
            f'<div class="preset-tag">{html.escape("当前使用" if bool(preset["active"]) else ("可编辑" if bool(preset["editable"]) else "内置预设"))}</div></div>'
            f'<div class="preset-desc">{html.escape(str(preset["description"]))}</div>'
            f'<form method="post" action="{html.escape(admin_path)}/template/preset">'
            f'<input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}" />'
            f'<input type="hidden" name="preset_key" value="{html.escape(str(preset["key"]))}" />'
            f'<button type="submit" {"disabled" if bool(preset["active"]) else ""}>{"当前主题" if bool(preset["active"]) else "一键切换"}</button>'
            "</form>"
            "</div>"
        ).format(active_class=" active" if bool(preset["active"]) else "")
        for preset in template_presets
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>入群验证管理台</title>
  <style>
    :root {{
      --bg: #f3f5f7;
      --panel: #ffffff;
      --text: #18212f;
      --muted: #6b7280;
      --line: #d8dee6;
      --accent: #2563eb;
      --success: #0f766e;
      --shadow: 0 18px 40px rgba(15, 23, 42, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: linear-gradient(180deg, #eef2f7 0%, #f7f9fb 100%);
      color: var(--text);
      font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
    }}
    .wrap {{
      max-width: 1280px;
      margin: 0 auto;
      padding: 24px;
    }}
    .hero {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
      margin-bottom: 20px;
    }}
    .hero h1 {{ margin: 0; font-size: 30px; }}
    .hero p {{ margin: 8px 0 0; color: var(--muted); }}
    .actions a {{
      display: inline-block;
      margin-left: 12px;
      text-decoration: none;
      color: white;
      background: var(--accent);
      border-radius: 10px;
      padding: 10px 16px;
      font-size: 14px;
    }}
    .header-btn {{
      margin-left: 12px;
      border: 1px solid rgba(37, 99, 235, 0.18);
      border-radius: 10px;
      background: white;
      color: var(--text);
      padding: 10px 16px;
      font-size: 14px;
      font-weight: 700;
    }}
    .grid {{
      display: grid;
      grid-template-columns: 0.9fr 1fr;
      gap: 20px;
    }}
    .stack {{
      display: grid;
      gap: 20px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid rgba(216, 222, 230, 0.9);
      border-radius: 18px;
      padding: 20px;
      box-shadow: var(--shadow);
    }}
    .panel h2 {{ margin: 0 0 16px; font-size: 22px; }}
    .hero-card {{
      margin-bottom: 20px;
      padding: 18px 20px;
      border-radius: 18px;
      border: 1px solid var(--line);
      background: linear-gradient(135deg, #f8fbff, #eef6ff);
    }}
    .hero-card h2 {{ margin: 0 0 8px; font-size: 22px; }}
    .hero-card p {{ margin: 0; color: var(--muted); line-height: 1.8; }}
    .hero-actions {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 14px;
    }}
    .hero-actions a {{
      display: inline-block;
      text-decoration: none;
      border-radius: 12px;
      padding: 10px 16px;
      font-size: 14px;
      font-weight: 700;
    }}
    .hero-actions .primary {{
      background: var(--accent);
      color: white;
    }}
    .hero-actions .secondary {{
      color: var(--text);
      background: white;
      border: 1px solid var(--line);
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(5, minmax(100px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }}
    .stat {{
      padding: 14px;
      border-radius: 14px;
      background: #f8fafc;
      border: 1px solid var(--line);
    }}
    .stat .label {{ font-size: 13px; color: var(--muted); }}
    .stat .value {{ margin-top: 8px; font-size: 24px; font-weight: 700; }}
    .status-online {{ color: #0f766e; }}
    .status-offline {{ color: #b91c1c; }}
    label {{
      display: block;
      font-size: 14px;
      font-weight: 700;
      margin-bottom: 8px;
    }}
    input, select {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 12px 14px;
      font-size: 14px;
      margin-bottom: 14px;
      background: #fff;
    }}
    textarea {{
      width: 100%;
      min-height: 420px;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 14px;
      font-size: 13px;
      line-height: 1.6;
      margin-bottom: 14px;
      background: #fff;
      font-family: "JetBrains Mono", "Consolas", monospace;
      resize: vertical;
    }}
    .tip {{
      margin-top: -6px;
      margin-bottom: 12px;
      font-size: 12px;
      color: var(--muted);
    }}
    button {{
      border: none;
      border-radius: 12px;
      background: var(--accent);
      color: white;
      padding: 12px 18px;
      font-size: 15px;
      font-weight: 700;
      cursor: pointer;
    }}
    .qr-image {{
      width: 100%;
      max-width: 360px;
      border-radius: 18px;
      border: 1px solid var(--line);
      background: white;
      display: block;
      margin: 0 auto;
    }}
    .empty-box {{
      border: 1px dashed var(--line);
      border-radius: 16px;
      padding: 28px;
      text-align: center;
      color: var(--muted);
      background: #f8fafc;
    }}
    .notice {{
      padding: 12px 14px;
      border-radius: 12px;
      margin-bottom: 16px;
      font-size: 14px;
    }}
    .success {{
      background: #ecfdf5;
      color: var(--success);
      border: 1px solid #a7f3d0;
    }}
    .warning {{
      background: #fff7ed;
      color: #c2410c;
      border: 1px solid #fdba74;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }}
    th, td {{
      border-bottom: 1px solid var(--line);
      text-align: left;
      padding: 10px 8px;
    }}
    th {{ background: #f8fafc; }}
    .help-list {{
      color: var(--muted);
      line-height: 1.8;
      font-size: 14px;
    }}
    .muted-box {{
      border: 1px solid var(--line);
      border-radius: 14px;
      background: #f8fafc;
      padding: 14px 16px;
      color: var(--muted);
      line-height: 1.8;
      font-size: 14px;
    }}
    .inline-actions {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .preset-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }}
    .preset-card {{
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px;
      background: #f8fafc;
    }}
    .preset-card.active {{
      background: linear-gradient(135deg, #eef6ff, #f8fbff);
      border-color: #93c5fd;
      box-shadow: inset 0 0 0 1px rgba(37, 99, 235, 0.08);
    }}
    .preset-title-row {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: flex-start;
    }}
    .preset-title {{
      font-size: 16px;
      font-weight: 700;
    }}
    .preset-tag {{
      white-space: nowrap;
      border-radius: 999px;
      background: white;
      border: 1px solid var(--line);
      padding: 4px 10px;
      font-size: 12px;
      color: var(--muted);
    }}
    .preset-desc {{
      margin: 10px 0 14px;
      color: var(--muted);
      line-height: 1.7;
      font-size: 13px;
      min-height: 44px;
    }}
    .template-head {{
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: flex-start;
      margin-bottom: 14px;
    }}
    .template-head .meta {{
      margin: 6px 0 0;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.7;
      text-align: left;
    }}
    .secondary-btn {{
      display: inline-block;
      border-radius: 12px;
      padding: 12px 18px;
      font-size: 15px;
      font-weight: 700;
      border: 1px solid var(--line);
      background: #fff;
      color: var(--text);
      cursor: pointer;
    }}
    details {{
      margin-top: 18px;
    }}
    summary {{
      cursor: pointer;
      font-weight: 700;
      color: var(--text);
    }}
    .qr-path {{
      margin-top: 10px;
      font-size: 12px;
      color: var(--muted);
      word-break: break-all;
    }}
    @media (max-width: 980px) {{
      .grid {{ grid-template-columns: 1fr; }}
      .stats {{ grid-template-columns: repeat(2, minmax(100px, 1fr)); }}
      .hero {{ flex-direction: column; align-items: flex-start; }}
      .actions a {{ margin-left: 0; margin-right: 12px; margin-top: 10px; }}
      .header-btn {{ margin-left: 0; margin-right: 12px; margin-top: 10px; }}
      .preset-grid {{ grid-template-columns: 1fr; }}
      .template-head {{ flex-direction: column; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <div>
        <h1>QQ群入群验证管理台</h1>
        <p>这里只有一个页面。先保存配置，再启动客户端并等待机器人在线，最后把机器人拉进群并设为管理员。</p>
      </div>
      <div class="actions">
        <a href="{html.escape(admin_path)}">首页</a>
        <a href="{html.escape(admin_path)}?refresh=1">手动刷新</a>
        <form method="post" action="{html.escape(admin_path)}/system/restart" style="display: inline-block;">
          <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}" />
          <button type="submit" class="header-btn">重启机器人</button>
        </form>
      </div>
    </div>

    <div class="panel">
      {save_message}
      {system_notice}
      <div class="hero-card">
        {next_action}
      </div>
      <div class="stats">
        <div class="stat"><div class="label">机器人在线状态</div><div class="value {'status-online' if summary['bot_online'] else 'status-offline'}">{'在线' if summary['bot_online'] else '离线'}</div></div>
        <div class="stat"><div class="label">目标群数量</div><div class="value">{summary['target_group_count']}</div></div>
        <div class="stat"><div class="label">待验证人数</div><div class="value">{summary['pending_count']}</div></div>
        <div class="stat"><div class="label">累计已通过</div><div class="value">{summary['passed_count']}</div></div>
        <div class="stat"><div class="label">累计已踢出</div><div class="value">{summary['kicked_count']}</div></div>
      </div>
      {onebot_notice}
    </div>

    <div class="grid">
      <div class="stack">
        <div class="panel">
          <h2>基础配置</h2>
          <form method="post" action="{html.escape(admin_path)}/settings">
            <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}" />
            <label>目标群号</label>
            <input name="target_groups" value="{html.escape(target_groups_text)}" placeholder="多个群号用英文逗号分隔" />
            <div class="tip">只有这里写入的群才会启用入群验证。</div>

            <label>超级管理员 QQ</label>
            <input name="superusers" value="{html.escape(superusers_text)}" placeholder="多个QQ用英文逗号分隔" />
            <div class="tip">这些账号可以在群里直接发送“开启 群号”等命令，或艾特机器人查看帮助。</div>

            <label>验证超时时间（分钟）</label>
            <input name="timeout_minutes" value="{runtime_settings['timeout_minutes']}" />

            <label>最大错误次数</label>
            <input name="max_error_times" value="{runtime_settings['max_error_times']}" />

            <details>
              <summary>低频高级选项</summary>
              <div style="margin-top: 14px;">
                <label>Playwright 浏览器</label>
                <select name="playwright_browser">
                  <option value="chromium" {'selected' if runtime_settings['playwright_browser'] == 'chromium' else ''}>chromium</option>
                  <option value="firefox" {'selected' if runtime_settings['playwright_browser'] == 'firefox' else ''}>firefox</option>
                  <option value="webkit" {'selected' if runtime_settings['playwright_browser'] == 'webkit' else ''}>webkit</option>
                </select>

                <label>图片渲染重试次数</label>
                <input name="image_retry_times" value="{runtime_settings['image_retry_times']}" />

                <label>登录二维码目录（可选）</label>
                <input name="lagrange_qr_dir" value="{html.escape(runtime_settings['lagrange_qr_dir'])}" placeholder="/path/to/Lagrange.OneBot" />
                <div class="tip">只有自动识别不到客户端时才需要填。</div>
              </div>
            </details>

            <button type="submit">保存基础配置</button>
          </form>
        </div>

        <div class="panel">
          <h2>登录机器人</h2>
          {primary_client_card}
          <div style="margin: 16px 0 10px; font-weight: 700;">二维码</div>
          {qr_block}
          <div class="qr-path">{'二维码文件：' + html.escape(qr_image_path) if qr_image_path else '当前还没有二维码文件。'}</div>
          <details>
            <summary>查看客户端识别详情</summary>
            <div style="margin-top: 14px;">
              {client_list}
            </div>
          </details>
          <div class="muted-box" style="margin-top: 14px;">
            你通常只需要三步：点“启动当前客户端” -> 用手机 QQ 扫码 -> 回本页刷新状态。
          </div>
        </div>
      </div>

      <div class="stack">
        <div class="panel">
          <h2>验证码模板</h2>
          {template_notice}
          <div class="muted-box" style="margin-bottom: 14px;">
            现在支持内置多主题预设一键切换，也支持继续直接改 HTML + CSS。必须保留 <code>id="verify-card"</code>，以及
            <code>{'{{verify_code}}'}</code>、<code>{'{{user_qq}}'}</code>、
            <code>{'{{group_name}}'}</code>、<code>{'{{expire_time}}'}</code> 四个占位符。
          </div>
          <div class="template-head">
            <div>
              <div style="font-size: 18px; font-weight: 700;">当前主题：{html.escape(template_profile.name)}</div>
              <div class="meta">来源：{'自定义模板' if template_profile.source == 'custom' else '内置预设'}；{html.escape(template_profile.description)}</div>
            </div>
            <div class="preset-tag">当前键：{html.escape(template_profile.key)}</div>
          </div>
          <div class="preset-grid">
            {template_preset_cards}
          </div>
          <form method="post" action="{html.escape(admin_path)}/template/save">
            <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}" />
            <textarea name="template_html" spellcheck="false">{html.escape(template_profile.html)}</textarea>
            <div class="inline-actions">
              <button type="submit">保存为自定义主题</button>
            </div>
          </form>
          <form method="post" action="{html.escape(admin_path)}/template/reset" style="margin-top: 10px;">
            <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}" />
            <button class="secondary-btn" type="submit">清空自定义并恢复经典蓝</button>
          </form>
        </div>

        <div class="panel">
          <h2>入群发送模板</h2>
          {verify_message_template_notice}
          <div class="muted-box" style="margin-bottom: 14px;">
            这里控制机器人发在验证码图片前面的文字内容。机器人仍会自动艾特新人并附上验证码图片。
            可用占位符：<code>{'{{user_qq}}'}</code>、<code>{'{{user_name}}'}</code>、
            <code>{'{{group_id}}'}</code>、<code>{'{{group_name}}'}</code>、
            <code>{'{{timeout_minutes}}'}</code>、<code>{'{{max_error_times}}'}</code>、
            <code>{'{{verify_code}}'}</code>、<code>{'{{expire_time}}'}</code>。
          </div>
          <form method="post" action="{html.escape(admin_path)}/message-template/save">
            <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}" />
            <textarea name="verify_message_template" spellcheck="false" style="min-height: 220px; font-family: 'Microsoft YaHei', 'PingFang SC', sans-serif;">{html.escape(verify_message_template)}</textarea>
            <div class="inline-actions">
              <button type="submit">保存入群发送模板</button>
            </div>
          </form>
        </div>

        <div class="panel">
          <details>
            <summary>查看最近验证记录</summary>
            <div style="margin-top: 14px;">
              <table>
                <thead>
                  <tr>
                    <th>ID</th>
                    <th>用户QQ</th>
                    <th>群号</th>
                    <th>验证码</th>
                    <th>状态</th>
                    <th>错误次数</th>
                    <th>过期时间</th>
                  </tr>
                </thead>
                <tbody>{rows}</tbody>
              </table>
            </div>
          </details>
        </div>
      </div>
    </div>
  </div>
</body>
</html>"""


def _render_setup_page(
    *,
    admin_path: str,
    runtime_settings: dict[str, Any],
    setup_status: dict[str, Any],
    detected_clients: list[dict[str, str | bool]],
    qr_image_url: str,
    qr_image_version: str,
    primary_client: dict[str, str | bool] | None,
    selected_client_root: str,
    message: str,
    onebot_message: str,
    csrf_token: str,
) -> str:
    """渲染首次启动向导页。"""
    target_groups_text = ",".join(str(item) for item in sorted(runtime_settings["target_groups"]))
    superusers_text = ",".join(str(item) for item in sorted(runtime_settings["superusers"]))
    has_basic_config = bool(setup_status["has_basic_config"])
    has_client = setup_status["detected_client_count"] > 0
    if setup_status["bot_online"]:
        current_stage = "机器人已在线，可以直接进入管理台。"
        current_hint = "最后一步：把机器人拉进目标群，并给它管理员权限。"
    elif setup_status["has_qr_image"]:
        current_stage = "已经检测到登录二维码，下一步请用手机 QQ 扫码。"
        current_hint = "扫码成功后返回本页刷新，状态会自动变成机器人在线。"
    elif setup_status["has_selected_client"]:
        current_stage = "已经检测到登录客户端，下一步请启动它并等待二维码出现。"
        current_hint = "页面会自动选一个最合适的客户端。NapCat 启动时看到 QQ 主程序日志属于正常现象。"
    elif has_basic_config:
        current_stage = "基础配置已完成，下一步请让页面检测到 NapCat 或 Lagrange。"
        current_hint = "推荐先把客户端放到项目目录 third_party/onebot 下，再点击自动启动；如果没反应，再手动启动。"
    else:
        current_stage = "先填写目标群和管理员 QQ，这是第一步。"
        current_hint = "下面的高级配置一般不用动，保存后把客户端放进项目目录，再处理登录。"
    qr_query_suffix = f"&t={html.escape(qr_image_version)}" if qr_image_version else ""
    qr_block = (
        f'<img src="{html.escape(qr_image_url)}?client_root={html.escape(selected_client_root)}{qr_query_suffix}" alt="机器人账号登录二维码" class="qr-image" />'
        if qr_image_url
        else '<div class="empty-box">当前自动使用的客户端还没有检测到登录二维码。先启动它，二维码出来后这个页面会显示。</div>'
    )
    onebot_notice = _render_onebot_notice(onebot_message)
    client_list = _render_detected_clients(
        clients=detected_clients,
        selected_client_root=selected_client_root,
    )
    primary_client_card = _render_primary_client_card(
        admin_path=admin_path,
        csrf_token=csrf_token,
        primary_client=primary_client,
    )
    save_message = (
        '<div class="notice success">已保存。继续按下面步骤完成向导。</div>'
        if message
        else ""
    )
    steps = [
        ("第 1 步", "填写目标群和管理员", setup_status["has_target_groups"] and setup_status["has_superusers"]),
        ("第 2 步", "检测到登录客户端", setup_status["has_selected_client"]),
        ("第 3 步", "看到登录二维码", setup_status["has_qr_image"]),
        ("第 4 步", "扫码后机器人在线", setup_status["bot_online"]),
    ]
    step_cards = "".join(
        f'<div class="step {"done" if done else ""}"><div class="step-index">{html.escape(index)}</div><div><div class="step-title">{html.escape(title)}</div><div class="step-state">{"已完成" if done else "待完成"}</div></div></div>'
        for index, title, done in steps
    )
    status_cards = "".join(
        (
            f'<div class="quick-card {"done" if done else ""}">'
            f'<div class="quick-label">{html.escape(title)}</div>'
            f'<div class="quick-value">{"已完成" if done else "未完成"}</div>'
            "</div>"
        )
        for title, done in (
            ("基础配置", has_basic_config),
            ("登录客户端", setup_status["has_selected_client"]),
            ("登录二维码", setup_status["has_qr_image"]),
            ("机器人在线", setup_status["bot_online"]),
        )
    )
    primary_action = _render_setup_primary_action(
        admin_path=admin_path,
        setup_status=setup_status,
        has_basic_config=has_basic_config,
        has_client=has_client,
    )
    finish_button = (
        f'<a class="secondary-link" href="{html.escape(admin_path)}">进入管理台</a>'
        if has_basic_config
        else '<button type="submit">保存配置</button>'
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>首次启动向导</title>
  <style>
    :root {{
      --bg: #eff4f8;
      --panel: #ffffff;
      --text: #152033;
      --muted: #667085;
      --line: #dbe3ec;
      --accent: #0f62fe;
      --accent-2: #10b981;
      --shadow: 0 18px 44px rgba(15, 23, 42, 0.10);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: radial-gradient(circle at top left, #e8f1ff 0%, #f6f9fc 45%, #eef4f8 100%);
      color: var(--text);
      font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
    }}
    .wrap {{
      max-width: 1220px;
      margin: 0 auto;
      padding: 28px 20px 40px;
    }}
    .hero {{
      background: linear-gradient(135deg, #183b66, #0f62fe);
      color: white;
      border-radius: 24px;
      padding: 26px 28px;
      box-shadow: var(--shadow);
      margin-bottom: 20px;
    }}
    .hero h1 {{ margin: 0 0 10px; font-size: 32px; }}
    .hero p {{ margin: 0; line-height: 1.8; color: rgba(255,255,255,0.92); }}
    .grid {{
      display: grid;
      grid-template-columns: 1.1fr 0.9fr;
      gap: 20px;
    }}
    .panel {{
      background: var(--panel);
      border-radius: 22px;
      padding: 22px;
      box-shadow: var(--shadow);
      border: 1px solid rgba(219, 227, 236, 0.9);
    }}
    .panel h2 {{ margin: 0 0 16px; font-size: 24px; }}
    .steps {{
      display: grid;
      gap: 12px;
      margin-bottom: 18px;
    }}
    .step {{
      display: flex;
      align-items: center;
      gap: 14px;
      padding: 14px 16px;
      border-radius: 16px;
      border: 1px solid var(--line);
      background: #f8fafc;
    }}
    .step.done {{
      background: #ecfdf5;
      border-color: #a7f3d0;
    }}
    .step-index {{
      width: 58px;
      min-width: 58px;
      height: 58px;
      border-radius: 16px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 700;
      background: #e6eef8;
      color: #274c77;
    }}
    .step.done .step-index {{
      background: #10b981;
      color: white;
    }}
    .step-title {{ font-size: 16px; font-weight: 700; }}
    .step-state {{ margin-top: 6px; font-size: 13px; color: var(--muted); }}
    label {{
      display: block;
      font-size: 14px;
      font-weight: 700;
      margin-bottom: 8px;
    }}
    input, select {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 12px 14px;
      font-size: 14px;
      margin-bottom: 14px;
      background: #fff;
    }}
    .tip {{
      margin-top: -6px;
      margin-bottom: 12px;
      font-size: 12px;
      color: var(--muted);
      line-height: 1.7;
    }}
    button, .link-btn {{
      border: none;
      border-radius: 12px;
      background: var(--accent);
      color: white;
      padding: 12px 18px;
      font-size: 15px;
      font-weight: 700;
      cursor: pointer;
      text-decoration: none;
      display: inline-block;
    }}
    .secondary {{
      background: #475467;
    }}
    .notice {{
      padding: 12px 14px;
      border-radius: 12px;
      margin-bottom: 16px;
      font-size: 14px;
      background: #ecfdf5;
      color: #047857;
      border: 1px solid #a7f3d0;
    }}
    .warning {{
      background: #fff7ed;
      color: #c2410c;
      border: 1px solid #fdba74;
    }}
    .qr-image {{
      width: 100%;
      max-width: 360px;
      border-radius: 18px;
      border: 1px solid var(--line);
      background: white;
      display: block;
      margin: 0 auto 16px;
    }}
    .empty-box {{
      border: 1px dashed var(--line);
      border-radius: 16px;
      padding: 28px;
      text-align: center;
      color: var(--muted);
      background: #f8fafc;
      margin-bottom: 16px;
    }}
    .tips {{
      font-size: 14px;
      color: var(--muted);
      line-height: 1.9;
    }}
    .hero-sub {{
      margin-top: 12px;
      display: inline-flex;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .hero-pill {{
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(255,255,255,0.16);
      border: 1px solid rgba(255,255,255,0.2);
      font-size: 13px;
    }}
    .flow-panel {{
      border: 1px solid var(--line);
      border-radius: 22px;
      padding: 22px;
      background: linear-gradient(180deg, #f8fbff 0%, #eef5ff 100%);
      box-shadow: var(--shadow);
      margin-bottom: 20px;
    }}
    .flow-panel h2 {{
      margin: 0 0 10px;
      font-size: 28px;
    }}
    .flow-panel p {{
      margin: 0;
      color: #355070;
      line-height: 1.8;
    }}
    .quick-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(120px, 1fr));
      gap: 12px;
      margin: 18px 0;
    }}
    .quick-card {{
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px;
      background: #fff;
    }}
    .quick-card.done {{
      background: #ecfdf5;
      border-color: #a7f3d0;
    }}
    .quick-label {{
      font-size: 13px;
      color: var(--muted);
    }}
    .quick-value {{
      margin-top: 8px;
      font-size: 20px;
      font-weight: 700;
    }}
    .primary-box {{
      border: 1px solid #bfdbfe;
      border-radius: 18px;
      background: #fff;
      padding: 18px;
    }}
    .primary-box h3 {{
      margin: 0 0 10px;
      font-size: 18px;
    }}
    .primary-box p {{
      margin: 0 0 14px;
      color: var(--muted);
      line-height: 1.8;
    }}
    .primary-actions {{
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
    }}
    .primary-actions form {{
      margin: 0;
    }}
    .secondary-link {{
      display: inline-block;
      border-radius: 12px;
      background: #e2e8f0;
      color: #1e293b;
      padding: 12px 18px;
      font-size: 15px;
      font-weight: 700;
      text-decoration: none;
    }}
    details {{
      margin-top: 18px;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: #fff;
    }}
    summary {{
      cursor: pointer;
      list-style: none;
      padding: 16px 18px;
      font-weight: 700;
    }}
    details > div {{
      padding: 0 18px 18px;
    }}
    .actions {{
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      margin-top: 8px;
    }}
    @media (max-width: 980px) {{
      .grid {{ grid-template-columns: 1fr; }}
      .quick-grid {{ grid-template-columns: repeat(2, minmax(120px, 1fr)); }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <h1>首次启动向导</h1>
      <p>这是给第一次部署的人准备的。你不用先理解 NoneBot、OneBot 这些名词，只要照着下面 4 步做就行。</p>
      <div class="hero-sub">
        <div class="hero-pill">1. 填配置</div>
        <div class="hero-pill">2. 启动客户端</div>
        <div class="hero-pill">3. 扫码登录</div>
        <div class="hero-pill">4. 机器人在线</div>
      </div>
    </div>

    <div class="flow-panel">
      <h2>{html.escape(current_stage)}</h2>
      <p>{html.escape(current_hint)}</p>
      <div class="quick-grid">{status_cards}</div>
      {save_message}
      {onebot_notice}
      <div class="primary-box">
        <h3>现在建议你做的事</h3>
        {primary_action}
      </div>
    </div>

    <div class="grid">
      <div class="panel">
        <h2>步骤进度</h2>
        <div class="steps">{step_cards}</div>
        <div class="tips">
          <div>建议顺序：先保存配置，再把 NapCat 或 Lagrange 放进项目目录并启动，看到登录二维码后用手机 QQ 扫码，最后确认机器人在线。</div>
          <div>机器人在线后，请把机器人拉进目标群并设为管理员，否则它无法踢出超时用户。</div>
        </div>

        <details>
          <summary>展开高级配置</summary>
          <div>
            <form method="post" action="{html.escape(admin_path)}/settings">
              <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}" />
              <label>目标群号</label>
              <input name="target_groups" value="{html.escape(target_groups_text)}" placeholder="多个群号用英文逗号分隔" />
              <div class="tip">例：123456789,987654321。只有这里填写的群才会触发入群验证。</div>

              <label>超级管理员 QQ</label>
              <input name="superusers" value="{html.escape(superusers_text)}" placeholder="多个QQ用英文逗号分隔" />
              <div class="tip">这些 QQ 可以在群内发送“入群验证 开启/关闭 群号”命令。</div>

              <label>超时时间（分钟）</label>
              <input name="timeout_minutes" value="{runtime_settings['timeout_minutes']}" />

              <label>最大错误次数</label>
              <input name="max_error_times" value="{runtime_settings['max_error_times']}" />

              <label>Playwright 浏览器</label>
              <select name="playwright_browser">
                <option value="chromium" {'selected' if runtime_settings['playwright_browser'] == 'chromium' else ''}>chromium</option>
                <option value="firefox" {'selected' if runtime_settings['playwright_browser'] == 'firefox' else ''}>firefox</option>
                <option value="webkit" {'selected' if runtime_settings['playwright_browser'] == 'webkit' else ''}>webkit</option>
              </select>

              <label>图片渲染重试次数</label>
              <input name="image_retry_times" value="{runtime_settings['image_retry_times']}" />

              <label>登录二维码目录（可选）</label>
              <input name="lagrange_qr_dir" value="{html.escape(runtime_settings['lagrange_qr_dir'])}" placeholder="/path/to/Lagrange.OneBot" />
              <div class="tip">推荐目录：{html.escape(str(plugin_settings.managed_onebot_dir))}。一般不需要填。只有自动扫描没找到二维码时，才把实际运行目录写这里。</div>

              <div class="actions">
                {finish_button}
                <a class="secondary-link" href="{html.escape(admin_path)}/guide">查看详细说明</a>
              </div>
            </form>
          </div>
        </details>
      </div>

      <div class="panel">
        <h2>登录机器人账号</h2>
        {qr_block}
        <div class="tips" style="margin-bottom: 12px;">已识别到的客户端：</div>
        {primary_client_card}
        {client_list}
        <div class="tips">
          <div>1. 先把 NapCat 或 Lagrange 放到项目目录 {html.escape(str(plugin_settings.managed_onebot_dir))} 下。</div>
          <div>2. 页面会自动选一个当前客户端，不需要你手动挑。</div>
          <div>3. 如果是 NapCat，日志里看到 <code>qq</code> 进程是正常的，因为它本来就跑在 QQ 主程序里。</div>
          <div>4. 用手机 QQ 扫码，登录你准备当机器人的那个 QQ 账号。</div>
          <div>5. 扫码后如果还是没在线，点刷新状态。</div>
        </div>
      </div>
    </div>
  </div>
</body>
</html>"""


def _render_setup_primary_action(
    *,
    admin_path: str,
    setup_status: dict[str, Any],
    has_basic_config: bool,
    has_client: bool,
) -> str:
    """根据当前状态渲染 setup 页的主操作。"""
    if setup_status["bot_online"]:
        return (
            "<p>机器人已经在线。现在可以进入管理台继续配置，或者把机器人拉进目标群并设为管理员。</p>"
            f'<div class="primary-actions"><a class="link-btn" href="{html.escape(admin_path)}">进入管理台</a>'
            f'<a class="secondary-link" href="{html.escape(admin_path)}/setup?refresh=1">刷新状态</a></div>'
        )

    if setup_status["has_qr_image"]:
        return (
            "<p>二维码已经出现。现在请用手机 QQ 扫码登录机器人账号；扫码完成后回到这里刷新状态。</p>"
            f'<div class="primary-actions"><a class="link-btn" href="{html.escape(admin_path)}/setup?refresh=1">我已扫码，刷新状态</a>'
            f'<a class="secondary-link" href="{html.escape(admin_path)}/guide">查看详细说明</a></div>'
        )

    if setup_status["has_selected_client"]:
        return (
            f"<p>已经检测到可用客户端。优先点击右侧卡片里的启动按钮；如果失败，再手动打开项目目录 {html.escape(str(plugin_settings.managed_onebot_dir))} 下对应目录，等二维码出现后刷新页面。</p>"
            f'<div class="primary-actions"><a class="secondary-link" href="{html.escape(admin_path)}/setup?refresh=1">重新检测客户端</a></div>'
        )

    if has_basic_config:
        return (
            f"<p>基础配置已经完成，但还没检测到登录客户端。请先把 NapCat 或 Lagrange 放到项目目录 {html.escape(str(plugin_settings.managed_onebot_dir))} 下，再刷新检测。</p>"
            f'<div class="primary-actions"><a class="secondary-link" href="{html.escape(admin_path)}/setup?refresh=1">刷新状态</a></div>'
        )

    return (
        "<p>先展开高级配置，填入目标群号和管理员 QQ，然后点保存。保存后再回来处理登录。</p>"
        f'<div class="primary-actions"><a class="secondary-link" href="{html.escape(admin_path)}/guide">先看一眼说明</a></div>'
    )


def _render_guide_page(*, admin_path: str) -> str:
    """渲染机器人登录说明页。"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>机器人登录说明</title>
  <style>
    body {{
      margin: 0;
      background: #f5f7fb;
      color: #1f2937;
      font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
    }}
    .wrap {{
      max-width: 860px;
      margin: 0 auto;
      padding: 28px 20px 40px;
    }}
    .card {{
      background: #fff;
      border-radius: 18px;
      padding: 24px;
      box-shadow: 0 18px 40px rgba(15, 23, 42, 0.08);
    }}
    h1 {{ margin-top: 0; }}
    ol {{ line-height: 1.9; padding-left: 22px; }}
    a {{
      display: inline-block;
      margin-top: 12px;
      text-decoration: none;
      background: #2563eb;
      color: white;
      border-radius: 10px;
      padding: 10px 16px;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>机器人账号登录说明</h1>
      <ol>
        <li>先启动 NoneBot 机器人进程，再把 NapCat 或 Lagrange 放到项目目录 <code>{html.escape(str(plugin_settings.managed_onebot_dir))}</code> 下并启动。</li>
        <li>NapCat 或 Lagrange 会生成机器人账号的登录二维码图片，例如 <code>qr-0.png</code>。</li>
        <li>管理台会优先扫描项目目录并显示这张二维码。</li>
        <li>只有自动识别失败时，才需要手动填写运行目录。</li>
        <li>用手机 QQ 扫码，登录你准备当机器人的那个 QQ 账号。</li>
        <li>扫码完成后，首页的“机器人在线状态”会变成在线。</li>
      </ol>
      <a href="{html.escape(admin_path)}">返回管理台</a>
    </div>
  </div>
</body>
</html>"""


def _render_onebot_notice(message: str) -> str:
    """渲染 OneBot 启动结果提示。"""
    if not message:
        return ""
    raw_text = str(message)
    is_success = raw_text.startswith("1:")
    text = raw_text.split(":", 1)[1] if ":" in raw_text else raw_text
    notice_class = "success" if is_success else "warning"
    return f'<div class="notice {notice_class}">{html.escape(text)}</div>'


def _render_template_notice(message: str) -> str:
    """渲染模板保存结果提示。"""
    if not message:
        return ""
    raw_text = str(message)
    is_success = raw_text.startswith("1:")
    text = raw_text.split(":", 1)[1] if ":" in raw_text else raw_text
    notice_class = "success" if is_success else "warning"
    return f'<div class="notice {notice_class}">{html.escape(text)}</div>'


def _render_system_notice(message: str) -> str:
    """渲染系统操作结果提示。"""
    if not message:
        return ""
    raw_text = str(message)
    is_success = raw_text.startswith("1:")
    text = raw_text.split(":", 1)[1] if ":" in raw_text else raw_text
    notice_class = "success" if is_success else "warning"
    return f'<div class="notice {notice_class}">{html.escape(text)}</div>'


def _render_restart_progress_page(*, admin_path: str) -> str:
    """返回重启中的过渡页面。"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta http-equiv="refresh" content="6;url={html.escape(admin_path)}?system=1:机器人已重启，请确认页面状态是否恢复" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>正在重启机器人</title>
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


async def _restart_bot_process() -> None:
    """后台拉起新的机器人进程，然后结束当前进程。"""
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
    os._exit(0)


def _render_admin_next_action(
    *,
    admin_path: str,
    bot_online: bool,
    has_basic_config: bool,
    primary_client: dict[str, str | bool] | None,
    has_qr_image: bool,
    qr_image_path: str,
) -> str:
    """首页顶部只保留一条最直接的行动提示。"""
    if bot_online:
        return (
            "<h2>现在可以用了</h2>"
            "<p>机器人已经在线。下一步只要把机器人拉进目标群，并给它群管理员权限；否则它没法自动踢人。</p>"
            f'<div class="hero-actions"><a class="primary" href="{html.escape(admin_path)}?refresh=1">刷新状态</a></div>'
        )

    if not has_basic_config:
        return (
            "<h2>先填基础配置</h2>"
            "<p>先在下面填写“目标群号”和“超级管理员 QQ”，然后点保存。其他项先不要动。</p>"
        )

    if has_qr_image:
        qr_hint = (
            f"<p>二维码已经出来了。现在只要用手机 QQ 扫下面这张码，登录机器人账号。扫码后点“我已扫码，刷新状态”。二维码文件在：<code>{html.escape(qr_image_path)}</code></p>"
            if qr_image_path
            else "<p>二维码已经出来了。现在只要用手机 QQ 扫下面这张码，登录机器人账号。扫码后点“我已扫码，刷新状态”。</p>"
        )
        return (
            "<h2>现在只要扫码</h2>"
            f"{qr_hint}"
            f'<div class="hero-actions"><a class="primary" href="{html.escape(admin_path)}?refresh=1">我已扫码，刷新状态</a></div>'
        )

    if primary_client is not None and bool(primary_client.get("launchable")):
        return (
            "<h2>先启动机器人客户端</h2>"
            "<p>下一步只做一件事：点右侧的“启动当前客户端”。点完后等 5 到 15 秒，再回到本页刷新。看到 QQ 窗口弹出是正常的。</p>"
            f'<div class="hero-actions"><a class="secondary" href="{html.escape(admin_path)}?refresh=1">刷新状态</a></div>'
        )

    if primary_client is not None:
        return (
            "<h2>已经识别到客户端目录</h2>"
            "<p>但这个目录暂时不能自动启动。请先手动启动它，等二维码出现后回到本页刷新。</p>"
            f'<div class="hero-actions"><a class="secondary" href="{html.escape(admin_path)}?refresh=1">刷新状态</a></div>'
        )

    return (
        "<h2>先让页面找到客户端</h2>"
        f"<p>请把 NapCat 放到项目目录 <code>{html.escape(str(plugin_settings.managed_onebot_dir))}</code> 下，或者启动已经安装好的客户端，然后回到本页刷新。</p>"
        f'<div class="hero-actions"><a class="secondary" href="{html.escape(admin_path)}?refresh=1">刷新状态</a></div>'
    )


def _render_detected_clients(
    *,
    clients: list[dict[str, str | bool]],
    selected_client_root: str,
) -> str:
    """渲染自动识别到的客户端列表。"""
    if not clients:
        return (
            '<div class="empty-box" style="margin-bottom: 16px;">'
            f'还没有扫描到可用的 NapCat 或 Lagrange 目录。请把客户端放到 {html.escape(str(plugin_settings.managed_onebot_dir))}。'
            "</div>"
        )

    rows = []
    for client in clients:
        status = "可自动启动" if client["launchable"] else "仅检测到目录"
        if client["running"]:
            status = "本页面已启动"
        elif client["selected"]:
            status = "当前自动使用"
        qr_hint = "已检测到二维码" if client["has_qr_image"] else "还没有二维码"
        selected_hint = (
            '<div style="font-size:12px;color:#047857;margin-top:10px;">这个客户端会被优先用于显示二维码和自动启动</div>'
            if str(client["root"]) == selected_client_root
            else ""
        )
        rows.append(
            '<div style="border:1px solid #d8dee6;border-radius:12px;padding:12px 14px;background:#fff;margin-bottom:10px;">'
            f'<div style="font-weight:700;">{html.escape(str(client["name"]))}</div>'
            f'<div style="font-size:12px;color:#6b7280;word-break:break-all;margin-top:4px;">{html.escape(str(client["root"]))}</div>'
            f'<div style="font-size:12px;color:#2563eb;margin-top:6px;">{html.escape(status)}</div>'
            f'<div style="font-size:12px;color:#6b7280;margin-top:4px;">{html.escape(qr_hint)}</div>'
            f"{selected_hint}"
            "</div>"
        )
    return "".join(rows)


def _render_primary_client_card(
    *,
    admin_path: str,
    csrf_token: str,
    primary_client: dict[str, str | bool] | None,
) -> str:
    """渲染当前自动选择的客户端摘要。"""
    if primary_client is None:
        return '<div class="empty-box" style="margin:12px 0 16px;">还没有可用的登录客户端，先把 NapCat 放到项目目录里。</div>'

    name = html.escape(str(primary_client["name"]))
    root = html.escape(str(primary_client["root"]))
    hint = "NapCat 实际会拉起内置的 QQ 主程序，日志里看到 qq 很正常。" if "napcat" in str(primary_client["name"]).lower() else "这是当前页面自动使用的客户端。"
    start_button = ""
    if primary_client["launchable"]:
        start_button = (
            f'<form method="post" action="{html.escape(admin_path)}/onebot/start" style="margin-top:12px;">'
            f'<input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}" />'
            f'<input type="hidden" name="client_root" value="{root}" />'
            '<button type="submit">启动当前客户端</button>'
            "</form>"
        )
    return (
        '<div style="border:1px solid #d8dee6;border-radius:16px;padding:14px 16px;background:#f8fafc;margin:12px 0 16px;">'
        '<div style="font-size:13px;color:#6b7280;">当前自动使用的客户端</div>'
        f'<div style="font-size:18px;font-weight:700;margin-top:6px;">{name}</div>'
        f'<div style="font-size:12px;color:#6b7280;word-break:break-all;margin-top:6px;">{root}</div>'
        f'<div style="font-size:12px;color:#2563eb;margin-top:8px;">{html.escape(hint)}</div>'
        f"{start_button}"
        "</div>"
    )
