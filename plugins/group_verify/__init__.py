"""
NoneBot2 插件入口。

负责：
1. 注册入群事件监听
2. 注册群消息验证码监听
3. 注册超级管理员控制命令
4. 绑定启动 / 关闭生命周期
"""

from __future__ import annotations

from nonebot import get_driver, on_message, on_notice, on_regex
from nonebot.adapters.onebot.v11 import GroupIncreaseNoticeEvent, GroupMessageEvent, MessageSegment
from nonebot.rule import to_me

from .service import verify_service
from .web_admin import open_admin_page_if_needed, register_admin_routes


driver = get_driver()


@driver.on_startup
async def _on_startup() -> None:
    """NoneBot 启动时初始化插件。"""
    register_admin_routes()
    await verify_service.startup()
    open_admin_page_if_needed()


@driver.on_shutdown
async def _on_shutdown() -> None:
    """NoneBot 关闭时释放插件任务。"""
    await verify_service.shutdown()


group_increase_notice = on_notice(priority=5, block=False)


@group_increase_notice.handle()
async def _handle_group_increase(bot, event) -> None:
    """仅处理群成员增加事件。"""
    if not isinstance(event, GroupIncreaseNoticeEvent):
        return
    await verify_service.handle_member_increase(bot, event)


verify_message_matcher = on_message(priority=10, block=False)


@verify_message_matcher.handle()
async def _handle_verify_message(bot, event) -> None:
    """仅处理群内待验证用户发送的纯文本消息。"""
    if not isinstance(event, GroupMessageEvent):
        return
    await verify_service.handle_group_message(bot, event)


verify_admin_mention_matcher = on_message(rule=to_me(), priority=1, block=True)
verify_admin_text_matcher = on_regex(
    r"^\s*(?:入群验证\s+)?(?:服务状态|状态图|status|验证记录|记录|records|列表|list|状态总览|状态|开启|关闭|设置超时|设置次数)(?:\s|$)",
    priority=1,
    block=True,
)


@verify_admin_mention_matcher.handle()
async def _handle_verify_admin_mention(bot, event) -> None:
    """超级管理员艾特机器人时执行管理命令；空消息默认返回帮助。"""
    if not isinstance(event, GroupMessageEvent):
        return
    raw_text = _normalize_admin_command_text(
        str(getattr(event, "get_plaintext", lambda: "")()).strip()
    )
    await _run_verify_admin_command(
        bot,
        event,
        raw_text,
        matcher=verify_admin_mention_matcher,
        triggered_by_mention=True,
    )


@verify_admin_text_matcher.handle()
async def _handle_verify_admin_plain_text(bot, event) -> None:
    """允许超级管理员直接发送纯命令，不再强制要求固定前缀。"""
    if not isinstance(event, GroupMessageEvent):
        return
    raw_text = _normalize_admin_command_text(
        str(getattr(event, "get_plaintext", lambda: "")()).strip()
    )
    await _run_verify_admin_command(
        bot,
        event,
        raw_text,
        matcher=verify_admin_text_matcher,
        triggered_by_mention=False,
    )


def _normalize_admin_command_text(raw_text: str) -> str:
    """兼容旧版“入群验证”前缀，同时支持直接发送子命令。"""
    normalized = raw_text.strip()
    if normalized.startswith("入群验证"):
        normalized = normalized[len("入群验证") :].strip()
    return normalized


async def _run_verify_admin_command(
    bot,
    event,
    raw_text: str,
    *,
    matcher,
    triggered_by_mention: bool,
) -> None:
    """执行超级管理员命令。"""
    superusers = await verify_service.get_superusers()
    if getattr(event, "user_id", 0) not in superusers:
        await matcher.finish("只有已配置的超级管理员可以执行该命令")

    if not raw_text:
        if triggered_by_mention:
            await matcher.finish(_render_verify_admin_help())
        return

    parts = raw_text.split()
    action = parts[0]

    if action in {"帮助", "help", "Help", "HELP"}:
        if triggered_by_mention:
            await matcher.finish(_render_verify_admin_help())
        return

    if action in {"服务状态", "状态图", "status"}:
        try:
            image_bytes = await verify_service.render_service_status_image()
        except Exception:
            snapshot = await verify_service.get_service_status_snapshot()
            summary = snapshot["summary"]
            primary_client = snapshot["primary_client"]
            system_resources = snapshot["system_resources"]
            await matcher.finish(
                "服务状态：\n"
                f"机器人：{'在线' if summary['bot_online'] else '离线'}\n"
                f"目标群：{summary['target_group_count']}\n"
                f"待验证：{summary['pending_count']}\n"
                f"已通过：{summary['passed_count']}\n"
                f"已踢出：{summary['kicked_count']}\n"
                f"内存任务：{summary['task_count']}\n"
                f"GPU：{system_resources['gpu_summary']}\n"
                f"主客户端：{primary_client['name'] if primary_client else '未检测到'}\n"
                f"运行时长：{snapshot['uptime_text']}"
            )
        await matcher.finish(MessageSegment.image(file=image_bytes))

    if action in {"验证记录", "记录", "records"}:
        limit = 10
        if len(parts) >= 2:
            if not parts[1].isdigit():
                await matcher.finish("格式错误，请使用：验证记录 条数")
            limit = int(parts[1])
        if limit < 1 or limit > 20:
            await matcher.finish("验证记录条数请设置在 1 到 20 之间。")
        records = await verify_service.get_recent_records(limit=limit)
        if not records:
            await matcher.finish("最近还没有验证记录。")
        lines = [f"最近验证记录（最近 {len(records)} 条）："]
        for index, record in enumerate(records, start=1):
            lines.append(
                f"{index}. {record.updated_at.strftime('%m-%d %H:%M:%S')} | 群 {record.group_id} | 用户 {record.user_id} | {record.status} | 错误 {record.error_count}"
            )
        await matcher.finish("\n".join(lines))

    if action in {"列表", "list", "状态总览"}:
        group_configs = await verify_service.get_target_group_configs()
        if not group_configs:
            await matcher.finish("当前还没有目标群配置，请先在管理台填写目标群号。")
        lines = ["当前目标群配置："]
        for item in group_configs:
            lines.append(
                f"群 {item.group_id} | {'开启' if item.enabled else '关闭'} | 超时 {item.timeout_minutes} 分钟 | 错误上限 {item.max_error_times} 次"
            )
        await matcher.finish("\n".join(lines))

    if action == "状态":
        try:
            group_id = _resolve_target_group_id(parts, event, action="状态")
        except ValueError as exc:
            await matcher.finish(str(exc))
        group_config = await verify_service.get_group_config(group_id)
        if group_config is None:
            await matcher.finish("该群还没有配置入群验证。")
        await matcher.finish(
            f"群 {group_id} 当前为{'开启' if group_config.enabled else '关闭'}状态，"
            f"超时 {group_config.timeout_minutes} 分钟，"
            f"最大错误次数 {group_config.max_error_times} 次。"
        )

    if action in {"开启", "关闭"}:
        try:
            group_id = _resolve_target_group_id(parts, event, action=action)
        except ValueError as exc:
            await matcher.finish(str(exc))
        target_groups = await verify_service.get_target_groups()
        if group_id not in target_groups:
            await matcher.finish("该群号不在当前配置的目标群列表中，请先在管理页面里加入目标群。")
        success = await verify_service.set_group_enabled(group_id, action == "开启")
        if not success:
            await matcher.finish("数据库中不存在该群配置，请检查管理台中的目标群设置。")
        await matcher.finish(f"群 {group_id} 的入群验证已{action}")

    if action == "设置超时":
        try:
            group_id, timeout_minutes = _resolve_group_and_value(parts, event, action="设置超时")
        except ValueError as exc:
            await matcher.finish(str(exc))
        if timeout_minutes < 1 or timeout_minutes > 120:
            await matcher.finish("超时时间请设置在 1 到 120 分钟之间。")
        success = await verify_service.update_group_timeout_minutes(group_id, timeout_minutes)
        if not success:
            await matcher.finish("该群还没有配置入群验证，请先在管理台加入目标群。")
        await matcher.finish(f"群 {group_id} 的验证超时已设置为 {timeout_minutes} 分钟。")

    if action == "设置次数":
        try:
            group_id, max_error_times = _resolve_group_and_value(parts, event, action="设置次数")
        except ValueError as exc:
            await matcher.finish(str(exc))
        if max_error_times < 1 or max_error_times > 10:
            await matcher.finish("最大错误次数请设置在 1 到 10 之间。")
        success = await verify_service.update_group_max_error_times(group_id, max_error_times)
        if not success:
            await matcher.finish("该群还没有配置入群验证，请先在管理台加入目标群。")
        await matcher.finish(f"群 {group_id} 的最大错误次数已设置为 {max_error_times} 次。")

    await matcher.finish(
        "不支持的子命令。\n"
        f"{_render_verify_admin_help()}"
    )


def _render_verify_admin_help() -> str:
    """返回群管命令帮助文本。"""
    return (
        "入群验证命令\n"
        "━━━━━━━━━━\n"
        "查看\n"
        "@机器人\n"
        "@机器人 帮助\n"
        "@机器人 服务状态\n"
        "@机器人 验证记录 [条数]\n"
        "@机器人 列表\n"
        "@机器人 状态 群号\n"
        "\n"
        "管理\n"
        "@机器人 开启 群号\n"
        "@机器人 关闭 群号\n"
        "@机器人 设置超时 群号 分钟\n"
        "@机器人 设置次数 群号 次数\n"
        "\n"
        "也支持直接发送\n"
        "服务状态 / 验证记录 / 列表 / 状态 / 开启 / 关闭 / 设置超时 / 设置次数\n"
        "群聊里可省略群号，例如：状态、开启、设置超时 8、设置次数 5\n"
        "验证记录默认 10 条，可写：验证记录 15"
    )


def _resolve_target_group_id(parts, event, *, action: str) -> int:
    """优先使用命令参数中的群号，否则回退到当前群。"""
    if len(parts) >= 2:
        if not parts[1].isdigit():
            raise ValueError(f"格式错误，请使用：{action} 群号")
        return int(parts[1])
    if isinstance(event, GroupMessageEvent):
        return int(event.group_id)
    raise ValueError("当前不是群聊环境，请补充群号。")


def _resolve_group_and_value(parts, event, *, action: str) -> tuple[int, int]:
    """支持“群号 + 数值”或“仅数值，群号取当前群”两种写法。"""
    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        return int(parts[1]), int(parts[2])
    if len(parts) == 2 and parts[1].isdigit() and isinstance(event, GroupMessageEvent):
        return int(event.group_id), int(parts[1])
    if action == "设置超时":
        raise ValueError("格式错误，请使用：设置超时 群号 分钟，或在群里直接发送：设置超时 分钟")
    raise ValueError("格式错误，请使用：设置次数 群号 次数，或在群里直接发送：设置次数 次数")
