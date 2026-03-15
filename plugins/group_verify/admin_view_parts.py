"""
管理台页面辅助渲染片段。
"""

from __future__ import annotations

import html

from .config import plugin_settings


def render_onebot_notice(message: str) -> str:
    """渲染 OneBot 启动结果提示。"""
    if not message:
        return ""
    raw_text = str(message)
    is_success = raw_text.startswith("1:")
    text = raw_text.split(":", 1)[1] if ":" in raw_text else raw_text
    notice_class = "success" if is_success else "warning"
    return f'<div class="notice {notice_class}">{html.escape(text)}</div>'


def render_template_notice(message: str) -> str:
    """渲染模板保存结果提示。"""
    if not message:
        return ""
    raw_text = str(message)
    is_success = raw_text.startswith("1:")
    text = raw_text.split(":", 1)[1] if ":" in raw_text else raw_text
    notice_class = "success" if is_success else "warning"
    return f'<div class="notice {notice_class}">{html.escape(text)}</div>'


def render_system_notice(message: str) -> str:
    """渲染系统操作结果提示。"""
    if not message:
        return ""
    raw_text = str(message)
    is_success = raw_text.startswith("1:")
    text = raw_text.split(":", 1)[1] if ":" in raw_text else raw_text
    notice_class = "success" if is_success else "warning"
    return f'<div class="notice {notice_class}">{html.escape(text)}</div>'


def render_admin_next_action(
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


def render_setup_primary_action(
    *,
    admin_path: str,
    setup_status: dict[str, bool | str | int],
    has_basic_config: bool,
    has_client: bool,
) -> str:
    """根据当前状态渲染 setup 页的主操作。"""
    if bool(setup_status["bot_online"]):
        return (
            "<p>机器人已经在线。现在可以进入管理台继续配置，或者把机器人拉进目标群并设为管理员。</p>"
            f'<div class="primary-actions"><a class="link-btn" href="{html.escape(admin_path)}">进入管理台</a>'
            f'<a class="secondary-link" href="{html.escape(admin_path)}/setup?refresh=1">刷新状态</a></div>'
        )

    if bool(setup_status["has_qr_image"]):
        return (
            "<p>二维码已经出现。现在请用手机 QQ 扫码登录机器人账号；扫码完成后回到这里刷新状态。</p>"
            f'<div class="primary-actions"><a class="link-btn" href="{html.escape(admin_path)}/setup?refresh=1">我已扫码，刷新状态</a>'
            f'<a class="secondary-link" href="{html.escape(admin_path)}/guide">查看详细说明</a></div>'
        )

    if bool(setup_status["has_selected_client"]):
        return (
            f"<p>已经检测到可用客户端。优先点击右侧卡片里的启动按钮；如果失败，再手动打开项目目录 {html.escape(str(plugin_settings.managed_onebot_dir))} 下对应目录，等二维码出现后刷新页面。</p>"
            f'<div class="primary-actions"><a class="secondary-link" href="{html.escape(admin_path)}/setup?refresh=1">重新检测客户端</a></div>'
        )

    if has_basic_config:
        return (
            f"<p>基础配置已经完成，但还没检测到登录客户端。请先把 NapCat 或 Lagrange 放到项目目录 {html.escape(str(plugin_settings.managed_onebot_dir))} 下，再刷新检测。</p>"
            f'<div class="primary-actions"><a class="secondary-link" href="{html.escape(admin_path)}/setup?refresh=1">刷新状态</a></div>'
        )

    _ = has_client
    return (
        "<p>先展开高级配置，填入目标群号和管理员 QQ，然后点保存。保存后再回来处理登录。</p>"
        f'<div class="primary-actions"><a class="secondary-link" href="{html.escape(admin_path)}/guide">先看一眼说明</a></div>'
    )


def render_detected_clients(
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
            '<div class="client-card-hint">这个客户端会被优先用于显示二维码和自动启动</div>'
            if str(client["root"]) == selected_client_root
            else ""
        )
        rows.append(
            '<div class="client-card">'
            f'<div class="client-card-title">{html.escape(str(client["name"]))}</div>'
            f'<div class="client-card-path">{html.escape(str(client["root"]))}</div>'
            f'<div class="client-card-status">{html.escape(status)}</div>'
            f'<div class="client-card-meta">{html.escape(qr_hint)}</div>'
            f"{selected_hint}"
            "</div>"
        )
    return f'<div class="client-list">{"".join(rows)}</div>'


def render_primary_client_card(
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
            f'<form method="post" action="{html.escape(admin_path)}/onebot/start" class="client-launch-form">'
            f'<input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}" />'
            f'<input type="hidden" name="client_root" value="{root}" />'
            '<button type="submit">重新尝试启动客户端</button>'
            "</form>"
        )
    return (
        '<div class="primary-client-card">'
        '<div class="client-card-label">当前自动使用的客户端</div>'
        f'<div class="primary-client-title">{name}</div>'
        f'<div class="client-card-path">{root}</div>'
        f'<div class="client-card-status">{html.escape(hint)}</div>'
        f"{start_button}"
        "</div>"
    )
