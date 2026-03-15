"""
本地 Web 管理台。

目标用户是普通使用者，因此页面尽量直接给出：
1. 当前机器人是否在线
2. 当前客户端是否可启动
3. 目标群、超时时间等配置表单
4. 最近的验证记录
"""

from __future__ import annotations

import html
import secrets
from urllib.parse import parse_qs
from typing import Any

from fastapi import BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from nonebot import get_driver, logger

from .admin_actions import (
    open_admin_page_if_needed,
    render_restart_progress_page,
    render_shutdown_progress_page,
    restart_bot_process,
    stop_bot_process,
)
from .admin_security import ensure_admin_access
from .admin_view_parts import (
    render_admin_next_action,
    render_detected_clients,
    render_onebot_notice,
    render_primary_client_card,
    render_setup_primary_action,
    render_system_notice,
    render_template_notice,
)
from .config import plugin_settings
from .service import verify_service
from project_config import export_env_file, load_project_config, save_project_config, validate_project_config


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
        ensure_admin_access(request)
        setup_status = await verify_service.get_setup_status()
        if not (bool(setup_status["bot_online"]) and bool(setup_status["has_basic_config"])):
            return RedirectResponse(url=f"{admin_path}/setup", status_code=303)
        runtime_settings = await verify_service.get_runtime_settings()
        summary = await verify_service.get_dashboard_summary()
        records = await verify_service.get_recent_records(limit=20)
        system_resources = await verify_service.get_system_resource_snapshot()
        html_text = _render_overview_page(
            admin_path=admin_path,
            runtime_settings=runtime_settings,
            summary=summary,
            records=records,
            system_resources=system_resources,
            csrf_token=_ADMIN_CSRF_TOKEN,
        )
        return HTMLResponse(html_text)

    @app.get(f"{admin_path}/setup", response_class=HTMLResponse)
    async def setup_page(request: Request) -> HTMLResponse:
        ensure_admin_access(request)
        runtime_settings = await verify_service.get_runtime_settings()
        setup_status = await verify_service.get_setup_status()
        force_setup = str(request.query_params.get("force", "")).strip().lower() in {"1", "true", "yes", "on"}
        if not force_setup and bool(setup_status["bot_online"]) and bool(setup_status["has_basic_config"]):
            return RedirectResponse(url=admin_path, status_code=303)
        detected_clients = await verify_service.get_detected_onebot_clients()
        primary_client = await verify_service.get_primary_onebot_client()
        selected_client_root = str(primary_client["root"]) if primary_client else ""
        qr_image = await verify_service.get_latest_qr_image(selected_client_root=selected_client_root or None)
        group_overview = await verify_service.get_bot_group_overview()
        project_config = load_project_config(plugin_settings.project_root)
        html_text = _render_setup_page(
            admin_path=admin_path,
            runtime_settings=runtime_settings,
            setup_status=setup_status,
            detected_clients=detected_clients,
            qr_image_url=f"{admin_path}/qr",
            qr_image_version=str(int(qr_image.stat().st_mtime)) if qr_image is not None else "",
            primary_client=primary_client,
            selected_client_root=selected_client_root,
            message=str(request.query_params.get("saved", "")),
            onebot_message=str(request.query_params.get("onebot", "")),
            csrf_token=_ADMIN_CSRF_TOKEN,
            admin_username=str(project_config.get("admin", {}).get("username", "admin")),
            admin_local_only=bool(project_config.get("admin", {}).get("local_only", True)),
            group_overview=group_overview,
            account_message=str(request.query_params.get("account", "")),
            group_message=str(request.query_params.get("groups", "")),
        )
        return HTMLResponse(html_text)

    @app.get(f"{admin_path}/setup/state")
    async def setup_state(request: Request) -> JSONResponse:
        ensure_admin_access(request)
        setup_status = await verify_service.get_setup_status()
        group_overview = await verify_service.get_bot_group_overview()
        return JSONResponse(
            {
                "bot_online": bool(setup_status["bot_online"]),
                "has_basic_config": bool(setup_status["has_basic_config"]),
                "has_qr_image": bool(setup_status["has_qr_image"]),
                "has_selected_client": bool(setup_status["has_selected_client"]),
                "group_count": len(group_overview),
                "admin_group_count": sum(1 for item in group_overview if bool(item["is_admin"])),
            }
        )

    @app.post(f"{admin_path}/setup/account")
    async def save_setup_account(request: Request) -> RedirectResponse:
        ensure_admin_access(request)
        form = await _parse_settings_form(request)
        _ensure_csrf_token(form)
        username = str(form.get("admin_username", "")).strip() or "admin"
        password = str(form.get("admin_password", "")).strip()
        local_only = str(form.get("admin_local_only", "")).strip().lower() in {"1", "true", "on", "yes"}
        data = load_project_config(plugin_settings.project_root)
        data.setdefault("admin", {})
        data["admin"]["username"] = username
        data["admin"]["password"] = password
        data["admin"]["local_only"] = local_only
        errors, _warnings = validate_project_config(data)
        if errors:
            return RedirectResponse(url=f"{admin_path}/setup?account=0:{errors[0]}", status_code=303)
        save_project_config(plugin_settings.project_root, data)
        export_env_file(plugin_settings.project_root)
        return RedirectResponse(url=f"{admin_path}/setup?account=1:管理员账号已保存", status_code=303)

    @app.post(f"{admin_path}/setup/groups")
    async def save_setup_groups(request: Request) -> RedirectResponse:
        ensure_admin_access(request)
        form_lists = await _parse_settings_form_lists(request)
        form = await _parse_settings_form(request)
        _ensure_csrf_token(form)
        selected_group_items = [item.strip() for item in form_lists.get("target_group_items", []) if item.strip()]
        manual_target_groups = str(form.get("target_groups", "")).strip()
        superusers = str(form.get("superusers", "")).strip()
        merged_target_groups: list[str] = []
        for raw_value in ",".join(selected_group_items + ([manual_target_groups] if manual_target_groups else [])).split(","):
            value = raw_value.strip()
            if value and value not in merged_target_groups:
                merged_target_groups.append(value)
        target_groups = ",".join(merged_target_groups)
        if not target_groups:
            return RedirectResponse(url=f"{admin_path}/setup?groups=0:请至少选择一个目标群，或手动填写群号", status_code=303)
        if not superusers:
            return RedirectResponse(url=f"{admin_path}/setup?groups=0:请至少填写一个超级管理员 QQ", status_code=303)
        await verify_service.update_app_configs(
            {
                "target_groups": target_groups,
                "superusers": superusers,
            }
        )
        setup_status = await verify_service.get_setup_status()
        if bool(setup_status["bot_online"]) and bool(setup_status["has_basic_config"]):
            return RedirectResponse(url=f"{admin_path}?saved=1", status_code=303)
        return RedirectResponse(url=f"{admin_path}/setup?groups=1:基础配置已保存", status_code=303)

    @app.post(f"{admin_path}/settings", response_class=HTMLResponse)
    async def save_settings(request: Request) -> RedirectResponse:
        ensure_admin_access(request)
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
        return RedirectResponse(url=f"{admin_path}/settings?saved=1", status_code=303)

    @app.get(f"{admin_path}/settings", response_class=HTMLResponse)
    async def settings_page(request: Request) -> HTMLResponse:
        ensure_admin_access(request)
        runtime_settings = await verify_service.get_runtime_settings()
        project_settings = await verify_service.get_project_notification_settings()
        verify_message_template = await verify_service.get_verify_message_template()
        admin_command_aliases = await verify_service.get_admin_command_aliases()
        admin_help_template = await verify_service.get_admin_help_template()
        return HTMLResponse(
            _render_settings_page(
                admin_path=admin_path,
                runtime_settings=runtime_settings,
                project_settings=project_settings,
                verify_message_template=verify_message_template,
                admin_command_aliases=admin_command_aliases,
                admin_help_template=admin_help_template,
                message=str(request.query_params.get("saved", "")),
                notice=str(request.query_params.get("project", "")),
                smtp_notice=str(request.query_params.get("smtp", "")),
                verify_message_notice=str(request.query_params.get("message_template", "")),
                command_notice=str(request.query_params.get("command", "")),
                csrf_token=_ADMIN_CSRF_TOKEN,
            )
        )

    @app.post(f"{admin_path}/project-settings")
    async def save_project_settings(request: Request) -> RedirectResponse:
        ensure_admin_access(request)
        form = await _parse_settings_form(request)
        _ensure_csrf_token(form)
        success, message = await verify_service.save_project_notification_settings(
            smtp_settings={
                "host": str(form.get("smtp_host", "")).strip(),
                "port": str(form.get("smtp_port", "")).strip() or "465",
                "username": str(form.get("smtp_username", "")).strip(),
                "password": str(form.get("smtp_password", "")).strip(),
                "from_email": str(form.get("smtp_from_email", "")).strip(),
                "to_email": str(form.get("smtp_to_email", "")).strip(),
                "use_tls": str(form.get("smtp_use_tls", "")).strip().lower() in {"1", "true", "on", "yes"},
                "use_ssl": str(form.get("smtp_use_ssl", "")).strip().lower() in {"1", "true", "on", "yes"},
            },
            proxy_settings={
                "http_proxy": str(form.get("http_proxy", "")).strip(),
                "https_proxy": str(form.get("https_proxy", "")).strip(),
                "all_proxy": str(form.get("all_proxy", "")).strip(),
                "no_proxy": str(form.get("no_proxy", "")).strip(),
            },
        )
        status = "1" if success else "0"
        return RedirectResponse(url=f"{admin_path}/settings?project={status}:{message}", status_code=303)

    @app.post(f"{admin_path}/smtp/test")
    async def send_smtp_test(request: Request) -> RedirectResponse:
        ensure_admin_access(request)
        form = await _parse_settings_form(request)
        _ensure_csrf_token(form)
        success, message = await verify_service.send_test_email(
            to_email=str(form.get("test_to_email", "")).strip(),
            subject=str(form.get("test_subject", "")).strip(),
            content=str(form.get("test_content", "")).strip(),
        )
        status = "1" if success else "0"
        return RedirectResponse(url=f"{admin_path}/settings?smtp={status}:{message}", status_code=303)

    @app.post(f"{admin_path}/onebot/start")
    async def start_onebot(request: Request) -> RedirectResponse:
        ensure_admin_access(request)
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
        ensure_admin_access(request)
        form = await _parse_settings_form(request)
        _ensure_csrf_token(form)
        template_html = str(form.get("template_html", ""))
        template_name = str(form.get("template_name", "")).strip() or "自定义模板"
        based_on_key = str(form.get("based_on_key", "")).strip() or "preset:classic"
        success, info = await verify_service.create_verify_template_version(
            template_name=template_name,
            template_html=template_html,
            based_on=based_on_key,
        )
        status = "1" if success else "0"
        return RedirectResponse(url=f"{admin_path}/templates?template={status}:{info}", status_code=303)

    @app.post(f"{admin_path}/template/reset")
    async def reset_template(request: Request) -> RedirectResponse:
        ensure_admin_access(request)
        form = await _parse_settings_form(request)
        _ensure_csrf_token(form)
        await verify_service.reset_verify_template_html()
        return RedirectResponse(url=f"{admin_path}/templates?template=1:已切回经典蓝预设", status_code=303)

    @app.post(f"{admin_path}/message-template/save")
    async def save_message_template(request: Request) -> RedirectResponse:
        ensure_admin_access(request)
        form = await _parse_settings_form(request)
        _ensure_csrf_token(form)
        template_text = str(form.get("verify_message_template", ""))
        success, info = await verify_service.save_verify_message_template(template_text)
        status = "1" if success else "0"
        return RedirectResponse(
            url=f"{admin_path}/settings?message_template={status}:{info}",
            status_code=303,
        )

    @app.post(f"{admin_path}/command/save")
    async def save_command_config(request: Request) -> RedirectResponse:
        ensure_admin_access(request)
        form = await _parse_settings_form(request)
        _ensure_csrf_token(form)
        aliases = str(form.get("admin_command_aliases", "")).strip()
        help_template = str(form.get("admin_help_template", "")).strip()
        alias_ok, alias_info = await verify_service.save_admin_command_aliases(aliases)
        if not alias_ok:
            return RedirectResponse(url=f"{admin_path}/settings?command=0:{alias_info}", status_code=303)
        help_ok, help_info = await verify_service.save_admin_help_template(help_template)
        status = "1" if help_ok else "0"
        info = help_info if help_ok else help_info
        return RedirectResponse(url=f"{admin_path}/settings?command={status}:{info}", status_code=303)

    @app.post(f"{admin_path}/template/preset")
    async def switch_template_preset(request: Request) -> RedirectResponse:
        ensure_admin_access(request)
        form = await _parse_settings_form(request)
        _ensure_csrf_token(form)
        preset_key = str(form.get("preset_key", "")).strip()
        success, info = await verify_service.activate_verify_template_preset(preset_key)
        status = "1" if success else "0"
        return RedirectResponse(url=f"{admin_path}/templates?template={status}:{info}", status_code=303)

    @app.post(f"{admin_path}/template/delete")
    async def delete_template(request: Request) -> RedirectResponse:
        ensure_admin_access(request)
        form = await _parse_settings_form(request)
        _ensure_csrf_token(form)
        template_key = str(form.get("template_key", "")).strip()
        success, info = await verify_service.delete_verify_template_version(template_key)
        status = "1" if success else "0"
        return RedirectResponse(url=f"{admin_path}/templates?template={status}:{info}", status_code=303)

    @app.get(f"{admin_path}/templates", response_class=HTMLResponse)
    async def templates_page(request: Request) -> HTMLResponse:
        ensure_admin_access(request)
        template_profile = await verify_service.get_active_verify_template_profile()
        template_presets = await verify_service.get_verify_template_presets()
        return HTMLResponse(
            _render_templates_page(
                admin_path=admin_path,
                template_profile=template_profile,
                template_presets=template_presets,
                template_message=str(request.query_params.get("template", "")),
                csrf_token=_ADMIN_CSRF_TOKEN,
            )
        )

    @app.get(f"{admin_path}/system", response_class=HTMLResponse)
    async def system_page(request: Request) -> HTMLResponse:
        ensure_admin_access(request)
        summary = await verify_service.get_dashboard_summary()
        detected_clients = await verify_service.get_detected_onebot_clients()
        primary_client = await verify_service.get_primary_onebot_client()
        selected_client_root = str(primary_client["root"]) if primary_client else None
        qr_image = await verify_service.get_latest_qr_image(selected_client_root=selected_client_root)
        return HTMLResponse(
            _render_system_page(
                admin_path=admin_path,
                summary=summary,
                detected_clients=detected_clients,
                primary_client=primary_client,
                qr_image_url=f"{admin_path}/qr",
                qr_status_url=f"{admin_path}/qr/status",
                qr_image_path=str(qr_image) if qr_image is not None else "",
                qr_image_version=str(int(qr_image.stat().st_mtime)) if qr_image is not None else "",
                onebot_message=str(request.query_params.get("onebot", "")),
                system_message=str(request.query_params.get("system", "")),
                csrf_token=_ADMIN_CSRF_TOKEN,
            )
        )

    @app.post(f"{admin_path}/system/restart")
    async def restart_system(request: Request, background_tasks: BackgroundTasks) -> HTMLResponse:
        ensure_admin_access(request)
        form = await _parse_settings_form(request)
        _ensure_csrf_token(form)
        background_tasks.add_task(restart_bot_process)
        return HTMLResponse(
            render_restart_progress_page(admin_path=admin_path),
            status_code=202,
        )

    @app.post(f"{admin_path}/system/stop")
    async def stop_system(request: Request, background_tasks: BackgroundTasks) -> HTMLResponse:
        ensure_admin_access(request)
        form = await _parse_settings_form(request)
        _ensure_csrf_token(form)
        background_tasks.add_task(stop_bot_process)
        return HTMLResponse(render_shutdown_progress_page(), status_code=202)

    @app.post(f"{admin_path}/setup/reset")
    async def reset_setup_state(request: Request) -> RedirectResponse:
        ensure_admin_access(request)
        form = await _parse_settings_form(request)
        _ensure_csrf_token(form)
        await verify_service.reset_setup_state()
        return RedirectResponse(url=f"{admin_path}/setup?saved=1", status_code=303)

    @app.get(f"{admin_path}/guide", response_class=HTMLResponse)
    async def guide_page(request: Request) -> RedirectResponse:
        ensure_admin_access(request)
        return RedirectResponse(url=admin_path, status_code=303)

    @app.get(f"{admin_path}/logs", response_class=HTMLResponse)
    async def logs_page(request: Request) -> HTMLResponse:
        ensure_admin_access(request)
        level_filter = str(request.query_params.get("level", "")).strip().upper()
        date_filter = str(request.query_params.get("date", "")).strip()
        export_requested = str(request.query_params.get("export", "")).strip() == "1"
        logs = _collect_runtime_logs(level_filter=level_filter, date_filter=date_filter)
        if export_requested:
            text = "\n".join(item["line"] for item in logs) + ("\n" if logs else "")
            return HTMLResponse(
                content=text,
                headers={"Content-Disposition": 'attachment; filename="group-verify-logs.txt"'},
                media_type="text/plain; charset=utf-8",
            )
        return HTMLResponse(
            _render_logs_page(
                admin_path=admin_path,
                logs=logs,
                level_filter=level_filter,
                date_filter=date_filter,
                csrf_token=_ADMIN_CSRF_TOKEN,
            )
        )

    @app.get(f"{admin_path}/qr")
    async def qr_image(request: Request) -> FileResponse:
        ensure_admin_access(request)
        client_root = str(request.query_params.get("client_root", "")).strip() or None
        qr_path = await verify_service.get_latest_qr_image(selected_client_root=client_root)
        if qr_path is None or not qr_path.exists():
            raise HTTPException(status_code=404, detail="当前还没有检测到登录二维码")
        response = FileResponse(path=qr_path, media_type="image/png", filename=qr_path.name)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    @app.get(f"{admin_path}/qr/status")
    async def qr_status(request: Request) -> JSONResponse:
        ensure_admin_access(request)
        client_root = str(request.query_params.get("client_root", "")).strip() or None
        qr_path = await verify_service.get_latest_qr_image(selected_client_root=client_root)
        if qr_path is None or not qr_path.exists():
            return JSONResponse(
                {
                    "available": False,
                    "path": "",
                    "version": "",
                    "client_root": client_root or "",
                }
            )
        return JSONResponse(
            {
                "available": True,
                "path": str(qr_path),
                "version": str(int(qr_path.stat().st_mtime)),
                "client_root": client_root or "",
            }
        )

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


async def _parse_settings_form_lists(request: Request) -> dict[str, list[str]]:
    """读取表单中的重复字段，供多选框等场景使用。"""
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type == "application/x-www-form-urlencoded":
        body = (await request.body()).decode("utf-8", errors="ignore")
        parsed = parse_qs(body, keep_blank_values=True)
        return {key: [str(item) for item in values] for key, values in parsed.items()}

    form = await request.form()
    grouped: dict[str, list[str]] = {}
    multi_items = getattr(form, "multi_items", None)
    if callable(multi_items):
        for key, value in multi_items():
            grouped.setdefault(str(key), []).append(str(value))
        return grouped
    for key, value in form.items():
        grouped.setdefault(str(key), []).append(str(value))
    return grouped

def _ensure_csrf_token(form: dict[str, str]) -> None:
    """校验管理台写操作使用的随机 token。"""
    if str(form.get("csrf_token", "")).strip() != _ADMIN_CSRF_TOKEN:
        raise HTTPException(status_code=403, detail="无效的管理台表单令牌")


def _render_admin_shell(*, title: str, admin_path: str, content: str, csrf_token: str) -> str:
    navigation = "".join(
        (
            f'<a href="{html.escape(admin_path)}" class="nav-link">概览</a>',
            f'<a href="{html.escape(admin_path)}/settings" class="nav-link">配置中心</a>',
            f'<a href="{html.escape(admin_path)}/templates" class="nav-link">模板库</a>',
            f'<a href="{html.escape(admin_path)}/system" class="nav-link">系统与登录</a>',
            f'<a href="{html.escape(admin_path)}/logs" class="nav-link">运行日志</a>',
        )
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      --bg: #eef4f1;
      --panel: #ffffff;
      --text: #16302b;
      --muted: #5f6f6a;
      --line: #d5dfdb;
      --accent: #0f766e;
      --accent-strong: #115e59;
      --danger: #c2410c;
      --success: #0f766e;
      --shadow: 0 20px 48px rgba(22, 48, 43, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background:
        radial-gradient(circle at top left, rgba(15, 118, 110, 0.12), transparent 36%),
        linear-gradient(180deg, #edf6f2 0%, #f8fbfa 100%);
      color: var(--text);
      font-family: "Noto Sans SC", "Microsoft YaHei", "PingFang SC", sans-serif;
    }}
    .app-shell {{
      min-height: 100vh;
      display: grid;
      grid-template-columns: 280px minmax(0, 1fr);
    }}
    .sidebar {{
      background:
        linear-gradient(180deg, rgba(7, 25, 23, 0.98), rgba(13, 79, 72, 0.96)),
        linear-gradient(135deg, #0b1f1d, #0f766e);
      color: #effcf8;
      padding: 24px 18px;
      border-right: 1px solid rgba(255, 255, 255, 0.08);
    }}
    .brand {{
      padding: 10px 12px 18px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.1);
      margin-bottom: 18px;
    }}
    .brand h1 {{ margin: 0; font-size: 24px; }}
    .brand p {{ margin: 10px 0 0; font-size: 13px; line-height: 1.8; color: rgba(239, 252, 248, 0.78); }}
    .nav-section {{ margin-bottom: 18px; }}
    .nav-title {{
      padding: 0 12px 8px;
      font-size: 12px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: rgba(239, 252, 248, 0.58);
    }}
    .nav-link {{
      display: block;
      text-decoration: none;
      color: #effcf8;
      padding: 12px 14px;
      border-radius: 14px;
      margin-bottom: 8px;
      background: rgba(255, 255, 255, 0.04);
      border: 1px solid rgba(255, 255, 255, 0.06);
      font-weight: 700;
    }}
    .nav-link:hover {{ background: rgba(255, 255, 255, 0.09); }}
    .sidebar-actions {{ margin-top: 18px; }}
    .header-btn {{
      width: 100%;
      border: 1px solid rgba(255, 255, 255, 0.18);
      border-radius: 14px;
      background: #f5fffc;
      color: #123b37;
      padding: 12px 16px;
      font-size: 14px;
      font-weight: 700;
    }}
    .header-btn.danger {{
      margin-top: 10px;
      background: #fee2e2;
      color: #7f1d1d;
    }}
    .main {{
      padding: 26px 24px 34px;
    }}
    .hero {{
      margin-bottom: 22px;
      padding: 24px 26px;
      border-radius: 24px;
      background: linear-gradient(135deg, #123b37, #0f766e 62%, #34a38f 100%);
      color: #f5fffc;
      box-shadow: var(--shadow);
    }}
    .hero h2 {{ margin: 0; font-size: 30px; }}
    .hero p {{ margin: 10px 0 0; max-width: 840px; color: rgba(245, 255, 252, 0.88); line-height: 1.8; }}
    .panel {{
      background: var(--panel); border: 1px solid rgba(213, 223, 219, 0.9); border-radius: 22px;
      padding: 22px; box-shadow: var(--shadow); margin-bottom: 20px;
    }}
    .panel h2 {{ margin: 0 0 16px; font-size: 22px; }}
    .grid {{ display: grid; gap: 20px; grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    .stack {{ display: grid; gap: 20px; }}
    .notice {{ padding: 12px 14px; border-radius: 12px; margin-bottom: 16px; font-size: 14px; }}
    .success {{ background: #ecfdf5; color: var(--success); border: 1px solid #a7f3d0; }}
    .warning {{ background: #fff7ed; color: var(--danger); border: 1px solid #fdba74; }}
    input, select, textarea {{
      width: 100%; border: 1px solid var(--line); border-radius: 14px; padding: 12px 14px;
      font-size: 14px; margin-bottom: 14px; background: #fff;
    }}
    textarea {{ min-height: 320px; resize: vertical; font-family: "JetBrains Mono", "Consolas", monospace; line-height: 1.6; }}
    label {{ display: block; font-size: 14px; font-weight: 700; margin-bottom: 8px; }}
    button {{
      border: none; border-radius: 14px; background: var(--accent); color: white;
      padding: 12px 18px; font-size: 15px; font-weight: 700; cursor: pointer;
    }}
    button:hover {{ background: var(--accent-strong); }}
    .secondary-btn {{
      display: inline-flex; align-items: center; justify-content: center; border-radius: 14px;
      padding: 12px 18px; font-size: 14px; font-weight: 700; border: 1px solid var(--line);
      background: #fff; color: var(--text); text-decoration: none;
    }}
    .tip {{ margin-top: -6px; margin-bottom: 12px; font-size: 12px; color: var(--muted); line-height: 1.7; }}
    .stats {{ display: grid; grid-template-columns: repeat(5, minmax(120px, 1fr)); gap: 12px; }}
    .stat {{ padding: 16px; border-radius: 18px; background: linear-gradient(180deg, #fcfefd, #f4fbf8); border: 1px solid var(--line); }}
    .stat .label {{ font-size: 13px; color: var(--muted); }}
    .stat .value {{ margin-top: 8px; font-size: 26px; font-weight: 700; }}
    .status-online {{ color: #0f766e; }}
    .status-offline {{ color: #b91c1c; }}
    .template-grid {{ display: grid; gap: 12px; grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    .template-card, .client-card {{
      border: 1px solid var(--line); border-radius: 18px; padding: 14px 16px; background: #fff;
    }}
    .primary-client-card {{
      border: 1px solid var(--line); border-radius: 18px; padding: 14px 16px;
      background: linear-gradient(180deg, #f7fbfa, #ffffff); margin-bottom: 16px;
    }}
    .template-card.active {{ border-color: #7ed2bf; background: linear-gradient(135deg, #ecfffa, #f7fffc); }}
    .meta {{ color: var(--muted); font-size: 13px; line-height: 1.7; }}
    .inline-actions {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    .client-card-title, .primary-client-title {{ font-size: 18px; font-weight: 700; margin-top: 6px; }}
    .client-card-label, .client-card-path, .client-card-status, .client-card-meta, .client-card-hint {{
      font-size: 12px; color: var(--muted); line-height: 1.7;
    }}
    .client-card-status {{ color: var(--accent); }}
    .client-list {{ display: grid; gap: 10px; }}
    .empty-box {{
      border: 1px dashed var(--line); border-radius: 16px; padding: 28px; text-align: center;
      color: var(--muted); background: #f8fafc;
    }}
    .qr-image {{
      width: 100%; max-width: 320px; aspect-ratio: 1 / 1; object-fit: contain; border-radius: 20px;
      border: 1px solid var(--line); background: white; display: block; margin: 0 auto; padding: 10px;
    }}
    .qr-path {{ margin-top: 12px; font-size: 12px; color: var(--muted); word-break: break-all; }}
    .hidden {{ display: none !important; }}
    @media (max-width: 980px) {{
      .app-shell {{ grid-template-columns: 1fr; }}
      .sidebar {{ border-right: none; border-bottom: 1px solid rgba(255, 255, 255, 0.08); }}
      .grid, .template-grid {{ grid-template-columns: 1fr; }}
      .stats {{ grid-template-columns: repeat(2, minmax(100px, 1fr)); }}
      .main {{ padding: 18px 14px 26px; }}
    }}
    @media (max-width: 640px) {{
      .hero {{ padding: 22px 18px; border-radius: 20px; }}
      .hero h2 {{ font-size: 28px; }}
      .stats {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand">
        <h1>Group Verify</h1>
        <p>初始化完成后进入多级后台。这里统一管理配置、模板、登录状态和日志。</p>
      </div>
      <div class="nav-section">
        <div class="nav-title">管理台</div>
        <form method="post" action="{html.escape(admin_path)}/system/restart">
          <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}" />
          <button type="submit" class="header-btn">重启机器人</button>
        </form>
        <form method="post" action="{html.escape(admin_path)}/system/stop">
          <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}" />
          <button type="submit" class="header-btn danger">安全退出</button>
        </form>
      </div>
      <div class="nav-section">
        <div class="nav-title">导航</div>
        {navigation}
      </div>
      <div class="sidebar-actions">
        <form method="post" action="{html.escape(admin_path)}/setup/reset">
          <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}" />
          <button type="submit" class="header-btn">重置并返回初始化</button>
        </form>
      </div>
    </aside>
    <main class="main">
      <div class="hero">
        <h2>{html.escape(title)}</h2>
        <p>当前后台采用左侧导航结构。日志、模板、系统状态和配置均拆分到独立子界面，避免继续堆在同一页。</p>
      </div>
      {content}
    </main>
  </div>
</body>
</html>"""


def _render_settings_page(
    *,
    admin_path: str,
    runtime_settings: dict[str, Any],
    project_settings: dict[str, Any],
    verify_message_template: str,
    admin_command_aliases: list[str],
    admin_help_template: str,
    message: str,
    notice: str,
    smtp_notice: str,
    verify_message_notice: str,
    command_notice: str,
    csrf_token: str,
) -> str:
    target_groups_text = ",".join(str(item) for item in sorted(runtime_settings["target_groups"]))
    superusers_text = ",".join(str(item) for item in sorted(runtime_settings["superusers"]))
    smtp = project_settings["smtp"]
    proxy = project_settings["proxy"]
    content = f"""
    <div class="grid">
      <div class="panel">
        {'<div class="notice success">运行参数已保存。</div>' if message else ''}
        {render_template_notice(notice)}
        <h2>运行参数</h2>
        <form method="post" action="{html.escape(admin_path)}/settings">
          <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}" />
          <label>目标群号</label>
          <input name="target_groups" value="{html.escape(target_groups_text)}" />
          <label>超级管理员 QQ</label>
          <input name="superusers" value="{html.escape(superusers_text)}" />
          <label>验证超时时间（分钟）</label>
          <input name="timeout_minutes" value="{runtime_settings['timeout_minutes']}" />
          <label>最大错误次数</label>
          <input name="max_error_times" value="{runtime_settings['max_error_times']}" />
          <label>Playwright 浏览器</label>
          <select name="playwright_browser">
            <option value="chromium" selected>chromium</option>
          </select>
          <label>图片重试次数</label>
          <input name="image_retry_times" value="{runtime_settings['image_retry_times']}" />
          <label>Lagrange 二维码目录</label>
          <input name="lagrange_qr_dir" value="{html.escape(str(runtime_settings['lagrange_qr_dir']))}" />
          <button type="submit">保存运行参数</button>
        </form>
      </div>
      <div class="stack">
        <div class="panel">
          {render_template_notice(verify_message_notice)}
          {render_template_notice(command_notice)}
          <h2>管理员命令与提示模板</h2>
          <form method="post" action="{html.escape(admin_path)}/command/save">
            <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}" />
            <label>管理员命令别名</label>
            <textarea name="admin_command_aliases" style="min-height:120px;">{html.escape(",".join(admin_command_aliases))}</textarea>
            <div class="tip">支持逗号或换行分隔，例如：Xynrin,验证助手。</div>
            <label>管理员帮助模板</label>
            <textarea name="admin_help_template">{html.escape(admin_help_template)}</textarea>
            <button type="submit">保存管理员命令配置</button>
          </form>
          <form method="post" action="{html.escape(admin_path)}/message-template/save" style="margin-top:14px;">
            <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}" />
            <label>入群发送模板</label>
            <textarea name="verify_message_template">{html.escape(verify_message_template)}</textarea>
            <button type="submit">保存入群发送模板</button>
          </form>
        </div>
        <div class="panel">
          {render_template_notice(smtp_notice)}
          <h2>SMTP / 邮件服务</h2>
          <form method="post" action="{html.escape(admin_path)}/project-settings">
            <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}" />
            <label>SMTP 主机</label>
            <input name="smtp_host" value="{html.escape(str(smtp['host']))}" placeholder="smtp.example.com" />
            <label>端口</label>
            <input name="smtp_port" value="{smtp['port']}" />
            <label>用户名</label>
            <input name="smtp_username" value="{html.escape(str(smtp['username']))}" />
            <label>密码 / 授权码</label>
            <input type="password" name="smtp_password" value="{html.escape(str(smtp['password']))}" />
            <label>发件人邮箱</label>
            <input name="smtp_from_email" value="{html.escape(str(smtp['from_email']))}" />
            <label>默认收件人</label>
            <input name="smtp_to_email" value="{html.escape(str(smtp['to_email']))}" />
            <label><input type="checkbox" name="smtp_use_ssl" value="1" {'checked' if smtp['use_ssl'] else ''} style="width:auto;margin-right:8px;" />使用 SSL</label>
            <label><input type="checkbox" name="smtp_use_tls" value="1" {'checked' if smtp['use_tls'] else ''} style="width:auto;margin-right:8px;" />使用 STARTTLS</label>
            <label>HTTP_PROXY</label>
            <input name="http_proxy" value="{html.escape(str(proxy['http_proxy']))}" />
            <label>HTTPS_PROXY</label>
            <input name="https_proxy" value="{html.escape(str(proxy['https_proxy']))}" />
            <label>ALL_PROXY</label>
            <input name="all_proxy" value="{html.escape(str(proxy['all_proxy']))}" />
            <label>NO_PROXY</label>
            <input name="no_proxy" value="{html.escape(str(proxy['no_proxy']))}" />
            <div class="tip">保存后会同步回 `config/appsettings.json` 与 `.env`，脚本安装、Playwright 下载和 OneBot 下载会优先继承这些代理。</div>
            <button type="submit">保存 SMTP 与代理配置</button>
          </form>
        </div>
        <div class="panel">
          <h2>发送测试邮件</h2>
          <form method="post" action="{html.escape(admin_path)}/smtp/test">
            <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}" />
            <label>测试收件人</label>
            <input name="test_to_email" value="{html.escape(str(smtp['to_email']))}" />
            <label>主题</label>
            <input name="test_subject" value="Xynrin机器人 SMTP 测试邮件" />
            <label>内容</label>
            <textarea name="test_content">这是一封来自Xynrin机器人管理台的测试邮件。</textarea>
            <button type="submit">发送测试邮件</button>
          </form>
        </div>
      </div>
    </div>
    """
    return _render_admin_shell(title="Xynrin管理台 / 配置", admin_path=admin_path, content=content, csrf_token=csrf_token)


def _render_templates_page(
    *,
    admin_path: str,
    template_profile: Any,
    template_presets: list[dict[str, str | bool]],
    template_message: str,
    csrf_token: str,
) -> str:
    cards = []
    for template in template_presets:
        delete_form = ""
        if bool(template.get("deletable")):
            delete_form = (
                f'<form method="post" action="{html.escape(admin_path)}/template/delete">'
                f'<input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}" />'
                f'<input type="hidden" name="template_key" value="{html.escape(str(template["key"]))}" />'
                '<button type="submit">删除版本</button>'
                "</form>"
            )
        cards.append(
            '<div class="template-card{active_class}">'.format(active_class=" active" if bool(template["active"]) else "")
            + f'<div style="font-size:18px;font-weight:700;">{html.escape(str(template["name"]))}</div>'
            + f'<div class="meta">来源：{html.escape(str(template.get("source", "")))}'
            + (f' | 创建时间：{html.escape(str(template.get("created_at", "")))}' if str(template.get("created_at", "")) else "")
            + "</div>"
            + f'<p class="meta">{html.escape(str(template["description"]))}</p>'
            + '<div class="inline-actions">'
            + f'<form method="post" action="{html.escape(admin_path)}/template/preset">'
            + f'<input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}" />'
            + f'<input type="hidden" name="preset_key" value="{html.escape(str(template["key"]))}" />'
            + f'<button type="submit" {"disabled" if bool(template["active"]) else ""}>{"当前使用" if bool(template["active"]) else "切换到此版本"}</button>'
            + "</form>"
            + delete_form
            + "</div></div>"
        )
    content = f"""
    <div class="panel">
      {render_template_notice(template_message)}
      <h2>当前模板</h2>
      <div class="meta">当前键：{html.escape(template_profile.key)} | 来源：{html.escape(template_profile.source)} | 基于：{html.escape(template_profile.based_on or '内置默认')}</div>
      <p class="meta">{html.escape(template_profile.description)}</p>
    </div>
    <div class="grid">
      <div class="panel">
        <h2>模板版本库</h2>
        <div class="template-grid">{''.join(cards)}</div>
      </div>
      <div class="panel">
        <h2>创建新版本</h2>
        <form method="post" action="{html.escape(admin_path)}/template/save">
          <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}" />
          <input type="hidden" name="based_on_key" value="{html.escape(template_profile.key)}" />
          <label>版本名称</label>
          <input name="template_name" value="{html.escape(template_profile.name)} 副本" />
          <label>模板 HTML</label>
          <textarea name="template_html" spellcheck="false">{html.escape(template_profile.html)}</textarea>
          <div class="tip">每次保存都会生成一个新的模板库版本，不覆盖旧版本。</div>
          <button type="submit">保存为新版本</button>
        </form>
        <form method="post" action="{html.escape(admin_path)}/template/reset" style="margin-top:12px;">
          <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}" />
          <button type="submit">切回经典蓝</button>
        </form>
      </div>
    </div>
    """
    return _render_admin_shell(title="Xynrin管理台 / 模板库", admin_path=admin_path, content=content, csrf_token=csrf_token)


def _render_system_page(
    *,
    admin_path: str,
    summary: dict[str, Any],
    detected_clients: list[dict[str, str | bool]],
    primary_client: dict[str, str | bool] | None,
    qr_image_url: str,
    qr_status_url: str,
    qr_image_path: str,
    qr_image_version: str,
    onebot_message: str,
    system_message: str,
    csrf_token: str,
) -> str:
    selected_client_root = str(primary_client["root"]) if primary_client else ""
    client_list = render_detected_clients(clients=detected_clients, selected_client_root=selected_client_root)
    primary_client_card = render_primary_client_card(admin_path=admin_path, csrf_token=csrf_token, primary_client=primary_client)
    qr_image_base_src = f"{html.escape(qr_image_url)}?client_root={html.escape(selected_client_root)}" if selected_client_root else ""
    qr_image_src = f"{qr_image_base_src}&t={html.escape(qr_image_version)}" if qr_image_base_src and qr_image_version else qr_image_base_src
    qr_status_text = "已检测到可扫码二维码" if qr_image_src else "等待客户端生成二维码"
    content = f"""
    <div class="panel">
      {render_onebot_notice(onebot_message)}
      {render_system_notice(system_message)}
      <div class="stats">
        <div class="stat"><div class="label">机器人在线状态</div><div class="value {'status-online' if summary['bot_online'] else 'status-offline'}">{'在线' if summary['bot_online'] else '离线'}</div></div>
        <div class="stat"><div class="label">待验证人数</div><div class="value">{summary['pending_count']}</div></div>
        <div class="stat"><div class="label">累计已通过</div><div class="value">{summary['passed_count']}</div></div>
        <div class="stat"><div class="label">累计已踢出</div><div class="value">{summary['kicked_count']}</div></div>
        <div class="stat"><div class="label">目标群数量</div><div class="value">{summary['target_group_count']}</div></div>
      </div>
    </div>
    <div class="grid">
      <div class="panel">
        <h2>当前客户端</h2>
        {primary_client_card}
        <h2>已识别客户端</h2>
        {client_list}
      </div>
      <div class="panel">
        <h2>登录二维码</h2>
        <div class="meta">{html.escape(qr_status_text)}</div>
        {'<img src="' + qr_image_src + '" alt="二维码" class="qr-image" />' if qr_image_src else '<div class="empty-box">还没有检测到二维码。先启动客户端，等待 5 到 15 秒后再刷新。</div>'}
        <div class="qr-path">二维码状态接口：{html.escape(qr_status_url)}<br />二维码文件：{html.escape(qr_image_path or '暂无')}</div>
        <div class="inline-actions" style="margin-top:14px;">
          <a class="secondary-btn" href="{html.escape(admin_path)}/system">刷新系统页</a>
          <a class="secondary-btn" href="{html.escape(admin_path)}/qr?client_root={html.escape(selected_client_root)}">打开二维码原图</a>
        </div>
      </div>
    </div>
    """
    return _render_admin_shell(title="Xynrin管理台 / 系统", admin_path=admin_path, content=content, csrf_token=csrf_token)


def _render_overview_page(
    *,
    admin_path: str,
    summary: dict[str, Any],
    records: list[Any],
    runtime_settings: dict[str, Any],
    system_resources: dict[str, Any],
    csrf_token: str,
) -> str:
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
    ) or '<tr><td colspan="7">暂无验证记录</td></tr>'
    resource_cards = "".join(
        (
            '<div class="stat">'
            f'<div class="label">{html.escape(item["label"])}</div>'
            f'<div class="value">{html.escape(item["value"])}</div>'
            f'<div class="meta">{html.escape(item["detail"])}</div>'
            "</div>"
        )
        for item in system_resources["meter_cards"]
    )
    content = f"""
    <div class="panel">
      <h2>运行概览</h2>
      <div class="stats">
        <div class="stat"><div class="label">机器人状态</div><div class="value {'status-online' if summary['bot_online'] else 'status-offline'}">{'在线' if summary['bot_online'] else '离线'}</div></div>
        <div class="stat"><div class="label">目标群数量</div><div class="value">{summary['target_group_count']}</div></div>
        <div class="stat"><div class="label">待验证人数</div><div class="value">{summary['pending_count']}</div></div>
        <div class="stat"><div class="label">累计已通过</div><div class="value">{summary['passed_count']}</div></div>
        <div class="stat"><div class="label">累计已踢出</div><div class="value">{summary['kicked_count']}</div></div>
      </div>
    </div>
    <div class="grid">
      <div class="panel">
        <h2>物理资源占用</h2>
        <div class="stats">{resource_cards}</div>
      </div>
      <div class="panel">
        <h2>实时指标</h2>
        <div class="meta">CPU：{html.escape(system_resources["cpu_percent_text"])}</div>
        <div class="meta">内存：{html.escape(system_resources["memory_detail"])}</div>
        <div class="meta">磁盘：{html.escape(system_resources["disk_detail"])}</div>
        <div class="meta">网络：{html.escape(system_resources["network_detail"])}</div>
        <div class="meta">GPU：{html.escape(system_resources["gpu_summary"])}</div>
        <div class="meta">系统启动：{html.escape(system_resources["boot_time"])}</div>
        <div class="inline-actions" style="margin-top:14px;">
          <a class="secondary-btn" href="{html.escape(admin_path)}/settings">修改配置</a>
          <a class="secondary-btn" href="{html.escape(admin_path)}/templates">管理模板库</a>
          <a class="secondary-btn" href="{html.escape(admin_path)}/system">查看登录状态</a>
          <a class="secondary-btn" href="{html.escape(admin_path)}/logs">查看日志</a>
        </div>
      </div>
    </div>
    <div class="panel" id="message-template">
      <h2>最近验证记录</h2>
      <div class="table-wrap">
        <table>
          <thead>
            <tr><th>ID</th><th>用户 QQ</th><th>群号</th><th>验证码</th><th>状态</th><th>错误次数</th><th>过期时间</th></tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </div>
    """
    return _render_admin_shell(title="Xynrin管理台 / 概览", admin_path=admin_path, content=content, csrf_token=csrf_token)


def _render_admin_page(
    *,
    admin_path: str,
    runtime_settings: dict[str, Any],
    summary: dict[str, Any],
    records: list[Any],
    detected_clients: list[dict[str, str | bool]],
    primary_client: dict[str, str | bool] | None,
    qr_image_url: str,
    qr_status_url: str,
    qr_image_path: str,
    qr_image_version: str,
    template_profile: Any,
    template_presets: list[dict[str, str | bool]],
    verify_message_template: str,
    admin_command_aliases: list[str],
    admin_help_template: str,
    message: str,
    template_message: str,
    verify_message_notice: str,
    command_notice: str,
    onebot_message: str,
    system_message: str,
    csrf_token: str,
) -> str:
    """渲染首页 HTML。"""
    target_groups_text = ",".join(str(item) for item in sorted(runtime_settings["target_groups"]))
    superusers_text = ",".join(str(item) for item in sorted(runtime_settings["superusers"]))
    onebot_notice = render_onebot_notice(onebot_message)
    has_basic_config = bool(runtime_settings["target_groups"] and runtime_settings["superusers"])
    bot_online = bool(summary["bot_online"])
    selected_client_root = str(primary_client["root"]) if primary_client else ""
    qr_image_base_src = (
        f"{html.escape(qr_image_url)}?client_root={html.escape(selected_client_root)}"
        if qr_image_url
        else ""
    )
    qr_image_src = (
        f"{qr_image_base_src}&t={html.escape(qr_image_version)}"
        if qr_image_base_src and qr_image_version
        else qr_image_base_src
    )
    qr_empty_class = " hidden" if qr_image_src else ""
    qr_image_class = "qr-image" if qr_image_src else "qr-image hidden"
    qr_status_text = "已检测到可扫码二维码" if qr_image_src else "等待客户端生成二维码"
    qr_path_text = html.escape(qr_image_path) if qr_image_path else "当前还没有二维码文件。"
    qr_block = f"""
          <div class="qr-shell" data-qr-root="{html.escape(selected_client_root)}" data-qr-status-url="{html.escape(qr_status_url)}">
            <div class="qr-toolbar">
              <div>
                <div class="section-kicker">登录二维码</div>
                <div class="qr-status-text" data-qr-status-text>{qr_status_text}</div>
              </div>
              <button type="button" class="secondary-btn qr-refresh-btn" data-qr-refresh>刷新二维码</button>
            </div>
            <div class="qr-frame">
              <img
                src="{qr_image_src}"
                alt="机器人账号登录二维码"
                class="{qr_image_class}"
                data-qr-image
                data-base-src="{qr_image_base_src}"
              />
              <div class="empty-box qr-empty{qr_empty_class}" data-qr-empty>还没有检测到二维码。先点击下面的“启动当前客户端”，等待 5 到 15 秒后页面会自动刷新这里。</div>
            </div>
            <div class="qr-meta-grid">
              <div class="qr-meta-item">
                <span>当前客户端</span>
                <strong data-qr-root-text>{html.escape(selected_client_root) if selected_client_root else "未锁定客户端"}</strong>
              </div>
              <div class="qr-meta-item">
                <span>二维码版本</span>
                <strong data-qr-version>{html.escape(qr_image_version) if qr_image_version else "尚未生成"}</strong>
              </div>
            </div>
            <div class="qr-path" data-qr-path>{qr_path_text}</div>
          </div>
    """
    next_action = render_admin_next_action(
        admin_path=admin_path,
        bot_online=bot_online,
        has_basic_config=has_basic_config,
        primary_client=primary_client,
        has_qr_image=bool(qr_image_path),
        qr_image_path=qr_image_path,
    )
    client_list = render_detected_clients(
        clients=detected_clients,
        selected_client_root=selected_client_root,
    )
    primary_client_card = render_primary_client_card(
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
    template_notice = render_template_notice(template_message)
    verify_message_template_notice = render_template_notice(verify_message_notice)
    command_config_notice = render_template_notice(command_notice)
    system_notice = render_system_notice(system_message)
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
  <title>Xynrin管理台</title>
  <style>
    :root {{
      --bg: #eef4f1;
      --panel: #ffffff;
      --text: #16302b;
      --muted: #5f6f6a;
      --line: #d5dfdb;
      --accent: #0f766e;
      --accent-strong: #115e59;
      --accent-soft: #dff4ee;
      --danger: #c2410c;
      --success: #0f766e;
      --shadow: 0 20px 48px rgba(22, 48, 43, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background:
        radial-gradient(circle at top left, rgba(15, 118, 110, 0.12), transparent 36%),
        linear-gradient(180deg, #edf6f2 0%, #f8fbfa 100%);
      color: var(--text);
      font-family: "Noto Sans SC", "Microsoft YaHei", "PingFang SC", sans-serif;
    }}
    .wrap {{
      max-width: 1280px;
      margin: 0 auto;
      padding: 28px 20px 36px;
    }}
    .hero {{
      display: flex;
      justify-content: space-between;
      gap: 20px;
      align-items: flex-start;
      margin-bottom: 22px;
      padding: 26px 28px;
      border-radius: 24px;
      background: linear-gradient(135deg, #123b37, #0f766e 62%, #34a38f 100%);
      color: #f5fffc;
      box-shadow: var(--shadow);
    }}
    .hero h1 {{ margin: 0; font-size: 32px; }}
    .hero p {{
      margin: 10px 0 0;
      max-width: 760px;
      color: rgba(245, 255, 252, 0.88);
      line-height: 1.8;
    }}
    .actions {{
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      align-items: center;
      justify-content: flex-end;
    }}
    .actions a {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      text-decoration: none;
      color: #f5fffc;
      background: rgba(255, 255, 255, 0.12);
      border: 1px solid rgba(255, 255, 255, 0.22);
      border-radius: 999px;
      padding: 11px 16px;
      font-size: 14px;
      font-weight: 700;
    }}
    .header-btn {{
      border: 1px solid rgba(255, 255, 255, 0.22);
      border-radius: 999px;
      background: #f5fffc;
      color: #123b37;
      padding: 11px 16px;
      font-size: 14px;
      font-weight: 700;
    }}
    .grid {{
      display: grid;
      grid-template-columns: minmax(0, 0.88fr) minmax(0, 1.12fr);
      gap: 20px;
    }}
    .stack {{
      display: grid;
      gap: 20px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid rgba(213, 223, 219, 0.9);
      border-radius: 22px;
      padding: 22px;
      box-shadow: var(--shadow);
    }}
    .panel h2 {{ margin: 0 0 16px; font-size: 22px; letter-spacing: 0.01em; }}
    .hero-card {{
      margin-bottom: 22px;
      padding: 20px 22px;
      border-radius: 20px;
      border: 1px solid #b7dfd2;
      background: linear-gradient(135deg, #f2fffb, #e8f8f3);
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
      border-radius: 999px;
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
      grid-template-columns: repeat(5, minmax(120px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }}
    .stat {{
      padding: 16px;
      border-radius: 18px;
      background: linear-gradient(180deg, #fcfefd, #f4fbf8);
      border: 1px solid var(--line);
    }}
    .stat .label {{ font-size: 13px; color: var(--muted); }}
    .stat .value {{ margin-top: 8px; font-size: 26px; font-weight: 700; }}
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
      border-radius: 14px;
      padding: 12px 14px;
      font-size: 14px;
      margin-bottom: 14px;
      background: #fff;
    }}
    textarea {{
      width: 100%;
      min-height: 420px;
      border: 1px solid var(--line);
      border-radius: 16px;
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
      line-height: 1.7;
    }}
    button {{
      border: none;
      border-radius: 14px;
      background: var(--accent);
      color: white;
      padding: 12px 18px;
      font-size: 15px;
      font-weight: 700;
      cursor: pointer;
    }}
    button:hover {{
      background: var(--accent-strong);
    }}
    .qr-image {{
      width: 100%;
      max-width: 360px;
      aspect-ratio: 1 / 1;
      object-fit: contain;
      border-radius: 20px;
      border: 1px solid var(--line);
      background: white;
      display: block;
      margin: 0 auto;
      padding: 10px;
      box-shadow: inset 0 0 0 1px rgba(15, 118, 110, 0.05);
    }}
    .hidden {{
      display: none !important;
    }}
    .section-kicker {{
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
    }}
    .qr-shell {{
      border: 1px solid var(--line);
      border-radius: 20px;
      background: linear-gradient(180deg, #fafdfe 0%, #f3faf7 100%);
      padding: 18px;
    }}
    .qr-toolbar {{
      display: flex;
      gap: 12px;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 14px;
    }}
    .qr-status-text {{
      margin-top: 6px;
      font-size: 16px;
      font-weight: 700;
    }}
    .qr-frame {{
      min-height: 392px;
      border-radius: 20px;
      background: white;
      border: 1px dashed #bfd3cd;
      padding: 16px;
      display: flex;
      align-items: center;
      justify-content: center;
    }}
    .qr-empty {{
      width: 100%;
      margin: 0;
    }}
    .qr-meta-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      margin-top: 14px;
    }}
    .qr-meta-item {{
      border-radius: 14px;
      background: #fff;
      border: 1px solid var(--line);
      padding: 12px 14px;
    }}
    .qr-meta-item span {{
      display: block;
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 8px;
    }}
    .qr-meta-item strong {{
      display: block;
      font-size: 14px;
      word-break: break-all;
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
      color: var(--danger);
      border: 1px solid #fdba74;
    }}
    .table-wrap {{
      width: 100%;
      overflow-x: auto;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: #fff;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
      min-width: 720px;
    }}
    th, td {{
      border-bottom: 1px solid var(--line);
      text-align: left;
      padding: 12px 12px;
      vertical-align: top;
    }}
    th {{
      background: #f2f7f5;
      white-space: nowrap;
    }}
    .help-list {{
      color: var(--muted);
      line-height: 1.8;
      font-size: 14px;
    }}
    .muted-box {{
      border: 1px solid var(--line);
      border-radius: 16px;
      background: #f7fbf9;
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
      border-radius: 18px;
      padding: 14px;
      background: #f9fcfb;
    }}
    .preset-card.active {{
      background: linear-gradient(135deg, #ecfffa, #f7fffc);
      border-color: #7ed2bf;
      box-shadow: inset 0 0 0 1px rgba(15, 118, 110, 0.08);
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
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 14px;
      padding: 12px 18px;
      font-size: 14px;
      font-weight: 700;
      border: 1px solid var(--line);
      background: #fff;
      color: var(--text);
      cursor: pointer;
    }}
    .secondary-btn:hover {{
      background: #f3f7f5;
    }}
    .primary-client-card,
    .client-card {{
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 14px 16px;
      background: #fff;
    }}
    .primary-client-card {{
      background: linear-gradient(180deg, #f7fbfa, #ffffff);
      margin: 12px 0 16px;
    }}
    .client-card-label {{
      font-size: 12px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }}
    .primary-client-title,
    .client-card-title {{
      font-size: 18px;
      font-weight: 700;
      margin-top: 6px;
    }}
    .client-card-path {{
      font-size: 12px;
      color: var(--muted);
      word-break: break-all;
      margin-top: 6px;
    }}
    .client-card-status {{
      font-size: 12px;
      color: var(--accent);
      margin-top: 8px;
      line-height: 1.7;
    }}
    .client-card-meta {{
      font-size: 12px;
      color: var(--muted);
      margin-top: 4px;
    }}
    .client-card-hint {{
      font-size: 12px;
      color: #047857;
      margin-top: 10px;
    }}
    .client-list {{
      display: grid;
      gap: 10px;
    }}
    .client-launch-form {{
      margin-top: 12px;
    }}
    details {{
      margin-top: 18px;
      border-top: 1px solid rgba(213, 223, 219, 0.9);
      padding-top: 16px;
    }}
    summary {{
      cursor: pointer;
      font-weight: 700;
      color: var(--text);
    }}
    .qr-path {{
      margin-top: 12px;
      font-size: 12px;
      color: var(--muted);
      word-break: break-all;
      line-height: 1.7;
    }}
    @media (max-width: 980px) {{
      .grid {{ grid-template-columns: 1fr; }}
      .stats {{ grid-template-columns: repeat(2, minmax(100px, 1fr)); }}
      .hero {{ flex-direction: column; align-items: flex-start; }}
      .actions {{ justify-content: flex-start; }}
      .preset-grid {{ grid-template-columns: 1fr; }}
      .template-head {{ flex-direction: column; }}
      .qr-meta-grid {{ grid-template-columns: 1fr; }}
      .qr-frame {{ min-height: 320px; }}
    }}
    @media (max-width: 640px) {{
      .wrap {{ padding: 18px 14px 28px; }}
      .hero {{ padding: 22px 18px; border-radius: 20px; }}
      .hero h1 {{ font-size: 28px; }}
      .panel {{ padding: 18px; border-radius: 18px; }}
      .stats {{ grid-template-columns: 1fr; }}
      .qr-toolbar {{ flex-direction: column; align-items: stretch; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <div>
        <h1>QQ群Xynrin管理台</h1>
        <p>首页现在只保留概览。配置、模板库、系统与日志已拆到独立入口，避免继续把所有功能堆在一个页面里。</p>
      </div>
      <div class="actions">
        <a href="{html.escape(admin_path)}">首页</a>
        <a href="{html.escape(admin_path)}/settings">配置</a>
        <a href="{html.escape(admin_path)}/templates">模板库</a>
        <a href="{html.escape(admin_path)}/system">系统</a>
        <a href="{html.escape(admin_path)}/logs">日志</a>
        <a href="{html.escape(admin_path)}?refresh=1">手动刷新</a>
        <form method="post" action="{html.escape(admin_path)}/system/restart">
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
            <div class="tip">只有这里写入的群才会启用Xynrin。</div>

            <label>超级管理员 QQ</label>
            <input name="superusers" value="{html.escape(superusers_text)}" placeholder="多个QQ用英文逗号分隔" />
            <div class="tip">这些账号可以在群里发送“Xynrin 开启 群号”等命令，或艾特机器人查看帮助。</div>

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
                </select>
                <div class="tip">当前启动脚本只会自动安装 Chromium，为避免渲染失败，这里固定使用 chromium。</div>

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
          {qr_block}
          <details>
            <summary>查看客户端识别详情</summary>
            <div style="margin-top: 14px;">
              {client_list}
            </div>
          </details>
          <div class="muted-box" style="margin-top: 14px;">
            你通常只需要三步：点“启动当前客户端” -> 用手机 QQ 扫码 -> 等二维码区自动更新或手动刷新状态。
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
          <h2>管理员命令</h2>
          {command_config_notice}
          <div class="muted-box" style="margin-bottom: 14px;">
            这里可以修改群内命令前缀别名，以及管理员帮助文案。别名支持逗号或换行分隔，例如：Xynrin,验证助手。
          </div>
          <form method="post" action="{html.escape(admin_path)}/command/save">
            <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}" />
            <label>命令前缀别名</label>
            <input name="admin_command_aliases" value="{html.escape(','.join(admin_command_aliases))}" placeholder="Xynrin,验证助手" />
            <label>管理员帮助模板</label>
            <textarea name="admin_help_template" spellcheck="false" style="min-height: 260px; font-family: 'Microsoft YaHei', 'PingFang SC', sans-serif;">{html.escape(admin_help_template)}</textarea>
            <div class="inline-actions">
              <button type="submit">保存命令配置</button>
            </div>
          </form>
        </div>

        <div class="panel">
          <details>
            <summary>查看最近验证记录</summary>
            <div style="margin-top: 14px;">
              <div class="table-wrap">
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
            </div>
          </details>
        </div>
      </div>
    </div>
  </div>
  <script>
    (() => {{
      const shell = document.querySelector("[data-qr-status-url]");
      if (!shell) {{
        return;
      }}
      const image = shell.querySelector("[data-qr-image]");
      const empty = shell.querySelector("[data-qr-empty]");
      const statusText = shell.querySelector("[data-qr-status-text]");
      const pathText = shell.querySelector("[data-qr-path]");
      const versionText = shell.querySelector("[data-qr-version]");
      const rootText = shell.querySelector("[data-qr-root-text]");
      const refreshButton = shell.querySelector("[data-qr-refresh]");
      const statusUrl = shell.dataset.qrStatusUrl;
      const root = shell.dataset.qrRoot || "";
      let currentVersion = versionText ? versionText.textContent.trim() : "";

      const applyState = (payload) => {{
        const available = Boolean(payload && payload.available);
        const version = available ? String(payload.version || "") : "";
        const clientRoot = payload && payload.client_root ? String(payload.client_root) : root;
        if (rootText) {{
          rootText.textContent = clientRoot || "未锁定客户端";
        }}
        if (versionText) {{
          versionText.textContent = version || "尚未生成";
        }}
        if (pathText) {{
          pathText.textContent = available && payload.path ? payload.path : "当前还没有二维码文件。";
        }}
        if (statusText) {{
          statusText.textContent = available ? "已检测到可扫码二维码" : "等待客户端生成二维码";
        }}
        if (available && image) {{
          const baseSrc = image.dataset.baseSrc || "";
          if (baseSrc && (version !== currentVersion || image.classList.contains("hidden"))) {{
            image.src = `${{baseSrc}}&t=${{encodeURIComponent(version)}}`;
          }}
          image.classList.remove("hidden");
          empty?.classList.add("hidden");
          currentVersion = version;
          return;
        }}
        image?.classList.add("hidden");
        empty?.classList.remove("hidden");
        currentVersion = "";
      }};

      const refreshQr = async () => {{
        try {{
          const url = new URL(statusUrl, window.location.origin);
          if (root) {{
            url.searchParams.set("client_root", root);
          }}
          url.searchParams.set("_", String(Date.now()));
          const response = await fetch(url.toString(), {{
            headers: {{
              "Cache-Control": "no-cache",
            }},
          }});
          if (!response.ok) {{
            throw new Error(`HTTP ${{response.status}}`);
          }}
          const payload = await response.json();
          applyState(payload);
        }} catch (_error) {{
          if (statusText) {{
            statusText.textContent = "二维码刷新失败，请稍后重试";
          }}
        }}
      }};

      refreshButton?.addEventListener("click", refreshQr);
      refreshQr();
      window.setInterval(() => {{
        if (!document.hidden) {{
          refreshQr();
        }}
      }}, 4000);
    }})();
  </script>
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
    admin_username: str,
    admin_local_only: bool,
    group_overview: list[dict[str, Any]],
    account_message: str,
    group_message: str,
) -> str:
    """渲染首次启动向导页。"""
    target_groups_text = ",".join(str(item) for item in sorted(runtime_settings["target_groups"]))
    superusers_text = ",".join(str(item) for item in sorted(runtime_settings["superusers"]))
    qr_query_suffix = f"&t={html.escape(qr_image_version)}" if qr_image_version else ""
    qr_block = (
        f'<img src="{html.escape(qr_image_url)}?client_root={html.escape(selected_client_root)}{qr_query_suffix}" alt="机器人账号登录二维码" class="qr-image" />'
        if qr_image_version
        else '<div class="empty-box">当前还没有二维码。先启动客户端，等待几秒后刷新本页。</div>'
    )
    onebot_notice = render_onebot_notice(onebot_message)
    client_list = render_detected_clients(clients=detected_clients, selected_client_root=selected_client_root)
    primary_client_card = render_primary_client_card(
        admin_path=admin_path,
        csrf_token=csrf_token,
        primary_client=primary_client,
    )
    save_message = '<div class="notice success">初始化配置已保存。</div>' if message else ""
    account_notice = render_template_notice(account_message)
    group_notice = render_template_notice(group_message)
    steps = [
        ("1", "管理员账号", bool(admin_username)),
        ("2", "登录机器人 QQ", bool(setup_status["bot_online"])),
        ("3", "配置超级管理员和目标群", bool(setup_status["has_basic_config"])),
    ]
    step_cards = "".join(
        f'<div class="step {"done" if done else ""}"><div class="step-index">{index}</div><div><div class="step-title">{html.escape(title)}</div><div class="step-state">{"已完成" if done else "待完成"}</div></div></div>'
        for index, title, done in steps
    )
    group_cards = "".join(
        (
            '<label class="group-option">'
            f'<input type="checkbox" name="target_group_items" value="{item["group_id"]}" {"checked" if bool(item["selected"]) else ""} />'
            '<div>'
            f'<div class="group-name">{html.escape(str(item["group_name"]))}</div>'
            f'<div class="group-meta">机器人身份：{"管理员" if bool(item["is_admin"]) else "普通成员"}</div>'
            f'<details class="group-id-fold"><summary>展开群号</summary><div>群号 {item["group_id"]}</div></details>'
            "</div>"
            "</label>"
        )
        for item in group_overview
    ) or '<div class="empty-box">机器人当前还没有可用群列表。请先扫码并确认 OneBot 已连上。</div>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>首次启动向导</title>
  <style>
    :root {{
      --panel: #ffffff;
      --text: #16302b;
      --muted: #62756e;
      --line: #d4dfdb;
      --accent: #0f766e;
      --shadow: 0 18px 44px rgba(22, 48, 43, 0.10);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: radial-gradient(circle at top left, rgba(15, 118, 110, 0.12) 0%, #f8fbfa 46%, #eef6f2 100%);
      color: var(--text);
      font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
    }}
    .wrap {{ max-width: 1220px; margin: 0 auto; padding: 28px 20px 40px; }}
    .hero {{
      background: linear-gradient(135deg, #123b37, #0f766e);
      color: white;
      border-radius: 24px;
      padding: 26px 28px;
      box-shadow: var(--shadow);
      margin-bottom: 20px;
    }}
    .hero h1 {{ margin: 0 0 10px; font-size: 32px; }}
    .hero p {{ margin: 0; line-height: 1.8; color: rgba(255,255,255,0.92); }}
    .flow-panel, .panel {{
      background: var(--panel);
      border-radius: 22px;
      padding: 22px;
      box-shadow: var(--shadow);
      border: 1px solid rgba(212, 223, 219, 0.9);
    }}
    .flow-panel {{ margin-bottom: 20px; }}
    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
    .panel h2, .flow-panel h2 {{ margin: 0 0 16px; font-size: 24px; }}
    .quick-grid {{ display: grid; grid-template-columns: repeat(3, minmax(120px, 1fr)); gap: 12px; margin: 18px 0; }}
    .quick-card, .step, .group-option {{
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 14px 16px;
      background: #fff;
    }}
    .quick-card.done, .step.done {{ background: #ecfdf5; border-color: #a7f3d0; }}
    .quick-label, .step-state, .group-meta, .tip, .tips, .inline-note {{ color: var(--muted); }}
    .quick-value {{ margin-top: 8px; font-size: 20px; font-weight: 700; }}
    .steps {{ display: grid; gap: 12px; margin-bottom: 18px; }}
    .step {{ display: flex; align-items: center; gap: 14px; }}
    .step-index {{
      width: 52px; min-width: 52px; height: 52px; border-radius: 14px;
      display: flex; align-items: center; justify-content: center; font-weight: 700;
      background: #e6f4f1; color: var(--accent);
    }}
    .step-title {{ font-size: 16px; font-weight: 700; }}
    label {{ display: block; font-size: 14px; font-weight: 700; margin-bottom: 8px; }}
    input, textarea {{
      width: 100%; border: 1px solid var(--line); border-radius: 12px;
      padding: 12px 14px; font-size: 14px; margin-bottom: 14px; background: #fff;
    }}
    textarea {{ min-height: 100px; }}
    button, .secondary-link {{
      border: none; border-radius: 12px; padding: 12px 18px; font-size: 15px;
      font-weight: 700; text-decoration: none; display: inline-block; cursor: pointer;
    }}
    button {{ background: var(--accent); color: white; }}
    .secondary-link {{ background: #e2e8f0; color: #1e293b; }}
    .notice {{ padding: 12px 14px; border-radius: 12px; margin-bottom: 16px; font-size: 14px; }}
    .success {{ background: #ecfdf5; color: #047857; border: 1px solid #a7f3d0; }}
    .warning {{ background: #fff7ed; color: #c2410c; border: 1px solid #fdba74; }}
    .qr-image {{ width: 100%; max-width: 360px; border-radius: 18px; border: 1px solid var(--line); background: white; display: block; margin: 0 auto 16px; }}
    .empty-box {{ border: 1px dashed var(--line); border-radius: 16px; padding: 28px; text-align: center; background: #f8fafc; }}
    .tips, .inline-note {{ font-size: 14px; line-height: 1.9; }}
    .actions {{ display: flex; gap: 12px; flex-wrap: wrap; margin-top: 8px; }}
    .group-option {{ display: flex; align-items: flex-start; gap: 12px; cursor: pointer; margin-bottom: 12px; }}
    .group-option input {{ width: auto; margin: 2px 0 0; }}
    .group-name {{ font-weight: 700; }}
    .group-id-fold {{ margin-top: 6px; color: var(--muted); font-size: 12px; }}
    .group-id-fold summary {{ cursor: pointer; }}
    @media (max-width: 980px) {{
      .grid {{ grid-template-columns: 1fr; }}
      .quick-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <h1>首次启动向导</h1>
      <p>顺序固定：先设置管理台账号，再扫码登录机器人 QQ，最后自动读取机器人所在群并选择要启用验证的群。</p>
    </div>

    <div class="flow-panel">
      <h2>初始化状态</h2>
      <div class="quick-grid">
        <div class="quick-card {'done' if admin_username else ''}"><div class="quick-label">管理员账号</div><div class="quick-value">{html.escape(admin_username or '未设置')}</div></div>
        <div class="quick-card {'done' if setup_status['bot_online'] else ''}"><div class="quick-label">机器人状态</div><div class="quick-value">{'在线' if setup_status['bot_online'] else '离线'}</div></div>
        <div class="quick-card {'done' if setup_status['has_basic_config'] else ''}"><div class="quick-label">群与超级管理员</div><div class="quick-value">{'已完成' if setup_status['has_basic_config'] else '待配置'}</div></div>
      </div>
      {save_message}
      {account_notice}
      {group_notice}
      {onebot_notice}
    </div>

    <div class="grid">
      <div class="panel">
        <h2>步骤进度</h2>
        <div class="steps">{step_cards}</div>
        <div class="tips">
          <div>管理员账号保存后会立刻生效。如果你设置了密码，浏览器后续访问可能会要求 Basic 认证。</div>
          <div>群列表只有在机器人成功登录并连上 OneBot 后才能自动读取。</div>
        </div>
        <form method="post" action="{html.escape(admin_path)}/setup/account">
          <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}" />
          <label>管理员账号</label>
          <input name="admin_username" value="{html.escape(admin_username)}" placeholder="admin" />
          <label>管理员密码</label>
          <input name="admin_password" type="password" placeholder="建议立即设置强密码" />
          <label style="display:flex;align-items:center;gap:10px;margin-bottom:14px;">
            <input type="checkbox" name="admin_local_only" value="true" {"checked" if admin_local_only else ""} style="width:auto;margin:0;" />
            <span>仅允许本机访问管理台</span>
          </label>
          <div class="inline-note">如果关闭“仅本机访问”，请务必配置密码，并把服务放在受控网络环境中。</div>
          <div class="actions">
            <button type="submit">保存管理台账号</button>
          </div>
        </form>
      </div>

      <div class="panel">
        <h2>登录机器人账号</h2>
        {qr_block}
        {primary_client_card}
        {(
            f'<form method="post" action="{html.escape(admin_path)}/onebot/start" data-auto-start-form>'
            f'<input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}" />'
            f'<input type="hidden" name="client_root" value="{html.escape(str(primary_client["root"]))}" />'
            "</form>"
        ) if primary_client and bool(primary_client.get("launchable")) and not qr_image_version and not bool(setup_status["bot_online"]) else ""}
        {client_list}
        <div class="tips">
          <div>先把 NapCat 或 Lagrange 放到项目目录 {html.escape(str(plugin_settings.managed_onebot_dir))} 下。</div>
          <div>页面会优先自动尝试唤起当前客户端。你主要只需要刷新二维码并扫码。</div>
        </div>
      </div>

      <div class="panel">
        <h2>选择群聊与超级管理员</h2>
        <form method="post" action="{html.escape(admin_path)}/setup/groups">
          <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}" />
          <div class="inline-note">下面列表来自机器人当前真实加入的群。优先勾选目标群，并确保机器人在这些群里至少是管理员。</div>
          <div>{group_cards}</div>
          <label>目标群号</label>
          <input name="target_groups" value="{html.escape(target_groups_text)}" placeholder="会自动同步上面勾选的群号，也可手动补充" />
          <label>超级管理员 QQ</label>
          <input name="superusers" value="{html.escape(superusers_text)}" placeholder="多个 QQ 用英文逗号分隔" />
          <div class="actions">
            <button type="submit">保存并完成初始化</button>
            <a class="secondary-link" href="{html.escape(admin_path)}/setup">刷新状态</a>
            {f'<a class="secondary-link" href="{html.escape(admin_path)}">进入管理台</a>' if setup_status["bot_online"] and setup_status["has_basic_config"] else ""}
          </div>
        </form>
      </div>
    </div>
  </div>
  <script>
    (() => {{
      const selected = Array.from(document.querySelectorAll('input[name="target_group_items"]'));
      const targetGroupsInput = document.querySelector('input[name="target_groups"]');
      const sync = () => {{
        if (!targetGroupsInput) return;
        const values = selected.filter((item) => item.checked).map((item) => item.value);
        if (values.length > 0) {{
          targetGroupsInput.value = values.join(",");
          return;
        }}
        targetGroupsInput.value = "";
      }};
      selected.forEach((item) => item.addEventListener("change", sync));
      sync();
      const stateUrl = "{html.escape(admin_path)}/setup/state";
      const autoStartForm = document.querySelector("[data-auto-start-form]");
      const autoStartKey = "group-verify-auto-start";
      let lastState = {{
        bot_online: {"true" if bool(setup_status["bot_online"]) else "false"},
        has_basic_config: {"true" if bool(setup_status["has_basic_config"]) else "false"},
        group_count: {len(group_overview)}
      }};
      if (autoStartForm && !sessionStorage.getItem(autoStartKey)) {{
        sessionStorage.setItem(autoStartKey, "1");
        window.setTimeout(() => autoStartForm.submit(), 300);
      }}
      const poll = async () => {{
        try {{
          const response = await fetch(stateUrl, {{ cache: "no-store" }});
          if (!response.ok) return;
          const nextState = await response.json();
          if (nextState.bot_online && nextState.has_basic_config) {{
            window.location.href = "{html.escape(admin_path)}";
            return;
          }}
          const changed =
            nextState.bot_online !== lastState.bot_online ||
            nextState.has_basic_config !== lastState.has_basic_config ||
            nextState.group_count !== lastState.group_count;
          if (changed) {{
            window.location.reload();
            return;
          }}
          lastState = nextState;
        }} catch (_error) {{
        }}
      }};
      window.setInterval(poll, 2000);
    }})();
  </script>
</body>
</html>"""


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


def _collect_runtime_logs(*, level_filter: str, date_filter: str) -> list[dict[str, str]]:
    """收集运行日志，支持简单筛选。"""
    log_files = [
        plugin_settings.project_root / "data" / "group_verify" / "run.log",
        plugin_settings.project_root / "data" / "group_verify" / "restart.log",
    ]
    rows: list[dict[str, str]] = []
    for log_file in log_files:
        if not log_file.exists():
            continue
        for line in log_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            normalized = line.strip()
            if not normalized:
                continue
            upper_line = normalized.upper()
            if level_filter and f"[{level_filter}]" not in upper_line:
                continue
            if date_filter and date_filter not in normalized:
                continue
            rows.append({"source": log_file.name, "line": normalized})
    return rows[-500:]


def _render_logs_page(
    *,
    admin_path: str,
    logs: list[dict[str, str]],
    level_filter: str,
    date_filter: str,
    csrf_token: str,
) -> str:
    """渲染日志查看页。"""
    rows = "\n".join(
        f"[{item['source']}] {item['line']}"
        for item in logs
    ) or "当前筛选条件下没有日志。"
    export_query = f"?level={html.escape(level_filter)}&date={html.escape(date_filter)}&export=1"
    content = f"""
    <div class="panel">
      <h2>运行日志</h2>
      <div class="meta">当前聚合启动与重启日志，支持等级筛选、日期筛选和导出。</div>
      <form method="get" action="{html.escape(admin_path)}/logs" class="inline-actions" style="margin-top:16px;">
        <select name="level">
          <option value="" {"selected" if not level_filter else ""}>全部等级</option>
          <option value="INFO" {"selected" if level_filter == "INFO" else ""}>INFO</option>
          <option value="WARNING" {"selected" if level_filter == "WARNING" else ""}>WARNING</option>
          <option value="ERROR" {"selected" if level_filter == "ERROR" else ""}>ERROR</option>
        </select>
        <input name="date" value="{html.escape(date_filter)}" placeholder="日期，例如 03-15 或 2026-03-15" />
        <button type="submit">应用筛选</button>
        <a class="secondary-btn" href="{html.escape(admin_path)}/logs{export_query}">导出日志</a>
        <a class="secondary-btn" href="{html.escape(admin_path)}/logs">清空筛选</a>
      </form>
      <pre style="margin:0; background:#0b1220; color:#d7e3ff; border-radius:18px; padding:18px; min-height:520px; overflow:auto; font-size:13px; line-height:1.7; border:1px solid #1f2a44;">{html.escape(rows)}</pre>
    </div>
    """
    return _render_admin_shell(title="Xynrin管理台 / 日志", admin_path=admin_path, content=content, csrf_token=csrf_token)
