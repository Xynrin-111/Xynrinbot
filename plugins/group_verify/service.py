"""
入群验证核心服务。

这里集中处理：
1. 入群事件 -> 创建或重置验证记录、发送验证码图片
2. 群消息事件 -> 校验验证码、统计错误次数、验证通过
3. 超时任务 -> 到期自动踢人
4. 重启恢复 -> 启动时恢复仍未过期的待验证任务

本文件刻意保留了较多中文注释，方便初学者按函数定位理解整套流程。
"""

from __future__ import annotations

import asyncio
import html
import os
import random
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

try:
    import psutil
except ModuleNotFoundError:
    psutil = None

from nonebot import get_bots, logger
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupIncreaseNoticeEvent,
    GroupMessageEvent,
    Message,
    MessageSegment,
)
from playwright.async_api import async_playwright
from sqlalchemy import func, select

from .config import plugin_settings
from .db import AsyncSessionLocal, init_db
from .models import AppConfig, GroupConfig, VerifyRecord, VerifyStatus


# HTML 模板文件路径。Playwright 会读取模板内容并渲染成 PNG 图片。
VERIFY_TEMPLATE_PATH = Path(__file__).parent / "templates" / "verify_card.html"
VERIFY_THEME_DIR = Path(__file__).parent / "templates" / "themes"
SERVICE_STATUS_TEMPLATE_PATH = Path(__file__).parent / "templates" / "service_status.html"
CUSTOM_VERIFY_TEMPLATE_PATH = plugin_settings.data_dir / "verify_card.custom.html"
ONEBOT_DIR_HINTS = ("lagrange", "onebot", "napcat")
QR_FILE_PATTERNS = ("qr-*.png", "qrcode*.png", "*qr*.png")

VERIFY_TEMPLATE_PRESETS: dict[str, dict[str, str]] = {
    "classic": {
        "name": "经典蓝",
        "description": "稳妥清爽，适合默认启用。",
        "file": str(VERIFY_TEMPLATE_PATH),
    },
    "glass": {
        "name": "玻璃霓光",
        "description": "更强调质感和数字展示，适合偏现代风格。",
        "file": str(VERIFY_THEME_DIR / "verify_card.glass.html"),
    },
    "warning": {
        "name": "警示橙",
        "description": "突出时效和操作提醒，适合强调风控提示。",
        "file": str(VERIFY_THEME_DIR / "verify_card.warning.html"),
    },
}


@dataclass(slots=True)
class OneBotClient:
    """本机扫描到的 OneBot 客户端信息。"""

    name: str
    root: Path
    launch_command: list[str]
    launch_env: dict[str, str] | None = None

    @property
    def launchable(self) -> bool:
        return bool(self.launch_command)


@dataclass(slots=True)
class VerifyTemplateProfile:
    """验证码模板当前状态。"""

    key: str
    name: str
    description: str
    html: str
    source: str
    editable: bool


class VerifyService:
    """封装完整的新人入群验证流程。"""

    def __init__(self) -> None:
        # key 统一使用 (群号, 用户QQ)，这样一个用户在不同群的状态彼此独立。
        self._tasks: dict[tuple[int, int], asyncio.Task[None]] = {}
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._started_onebot_processes: dict[str, subprocess.Popen[Any]] = {}
        self._scan_cache: dict[str, tuple[float, Any]] = {}
        self._random = random.SystemRandom()
        self._started_at = datetime.now()
        self._default_app_config: dict[str, str] = {
            "target_groups": ",".join(str(group_id) for group_id in sorted(plugin_settings.target_groups)),
            "superusers": ",".join(str(user_id) for user_id in sorted(plugin_settings.superusers)),
            "timeout_minutes": str(plugin_settings.default_timeout_minutes),
            "max_error_times": str(plugin_settings.default_max_error_times),
            "playwright_browser": plugin_settings.playwright_browser,
            "image_retry_times": str(plugin_settings.image_retry_times),
            "lagrange_qr_dir": str(plugin_settings.lagrange_qr_dir) if plugin_settings.lagrange_qr_dir else "",
            "preferred_onebot_client": "",
            "verify_template_preset": "classic",
            "verify_message_template": (
                "欢迎入群，{{user_name}}。\n"
                "请在 {{timeout_minutes}} 分钟内发送图片中的 4 位验证码完成验证。\n"
                "超时或累计输错 {{max_error_times}} 次将被移出群聊。"
            ),
        }

    async def startup(self) -> None:
        """插件启动时初始化数据库并恢复待验证任务。"""
        try:
            await init_db()
            await self._ensure_app_configs()
            await self._ensure_group_configs()
            await self.restore_pending_tasks()
        except Exception as exc:
            logger.exception(f"入群验证插件启动失败 error={exc}")

    async def shutdown(self) -> None:
        """插件关闭时取消所有内存中的超时任务。"""
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()
        self._locks.clear()

    async def _ensure_app_configs(self) -> None:
        """初始化全局配置表，首次启动时把 .env 默认值写入数据库。"""
        async with AsyncSessionLocal() as session:
            for config_key, config_value in self._default_app_config.items():
                result = await session.execute(
                    select(AppConfig).where(AppConfig.config_key == config_key)
                )
                app_config = result.scalar_one_or_none()
                if app_config is None:
                    session.add(AppConfig(config_key=config_key, config_value=config_value))
            await session.commit()

    async def _ensure_group_configs(self) -> None:
        """把 .env 中声明的目标群同步到数据库，方便后续管理员动态开关。"""
        target_groups = await self.get_target_groups()
        async with AsyncSessionLocal() as session:
            for group_id in target_groups:
                result = await session.execute(
                    select(GroupConfig).where(GroupConfig.group_id == group_id)
                )
                group_config = result.scalar_one_or_none()
                if group_config is None:
                    session.add(
                        GroupConfig(
                            group_id=group_id,
                            enabled=True,
                            timeout_minutes=plugin_settings.default_timeout_minutes,
                            max_error_times=plugin_settings.default_max_error_times,
                        )
                    )
            await session.commit()

    async def get_app_config_map(self) -> dict[str, str]:
        """读取数据库中的全局配置键值对。"""
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(AppConfig))
            configs = result.scalars().all()
        config_map = self._default_app_config.copy()
        for item in configs:
            config_map[item.config_key] = item.config_value
        return config_map

    async def update_app_configs(self, config_map: dict[str, str]) -> None:
        """保存网页表单中的全局配置。"""
        async with AsyncSessionLocal() as session:
            for config_key, config_value in config_map.items():
                result = await session.execute(
                    select(AppConfig).where(AppConfig.config_key == config_key)
                )
                app_config = result.scalar_one_or_none()
                if app_config is None:
                    session.add(AppConfig(config_key=config_key, config_value=config_value))
                else:
                    app_config.config_value = config_value
                    app_config.updated_at = datetime.now()
            await session.commit()

        await self._ensure_group_configs()
        await self._sync_group_config_defaults()

    async def get_runtime_settings(self) -> dict[str, Any]:
        """返回当前生效的运行设置，供业务逻辑和 Web 页面共用。"""
        config_map = await self.get_app_config_map()
        return {
            "target_groups": self._parse_csv_int_set(config_map.get("target_groups", "")),
            "superusers": self._parse_csv_int_set(config_map.get("superusers", "")),
            "timeout_minutes": self._safe_int(
                config_map.get("timeout_minutes", ""),
                plugin_settings.default_timeout_minutes,
            ),
            "max_error_times": self._safe_int(
                config_map.get("max_error_times", ""),
                plugin_settings.default_max_error_times,
            ),
            "playwright_browser": config_map.get("playwright_browser", plugin_settings.playwright_browser)
            or plugin_settings.playwright_browser,
            "image_retry_times": self._safe_int(
                config_map.get("image_retry_times", ""),
                plugin_settings.image_retry_times,
            ),
            "lagrange_qr_dir": config_map.get("lagrange_qr_dir", "").strip(),
            "preferred_onebot_client": config_map.get("preferred_onebot_client", "").strip(),
            "verify_template_preset": self._normalize_verify_template_preset(
                config_map.get("verify_template_preset", "classic")
            ),
            "verify_message_template": config_map.get(
                "verify_message_template",
                self._default_app_config["verify_message_template"],
            ),
        }

    async def get_verify_template_html(self) -> str:
        """返回当前生效的验证码 HTML 模板。"""
        return (await self.get_active_verify_template_profile()).html

    async def get_verify_message_template(self) -> str:
        """返回当前生效的入群提示模板。"""
        runtime_settings = await self.get_runtime_settings()
        return str(runtime_settings["verify_message_template"]).strip() or str(
            self._default_app_config["verify_message_template"]
        )

    async def save_verify_message_template(self, template_text: str) -> tuple[bool, str]:
        """保存入群验证消息模板。"""
        normalized = template_text.replace("\r\n", "\n").strip()
        success, error_message = self._validate_verify_message_template(normalized)
        if not success:
            return False, error_message
        await self.update_app_configs({"verify_message_template": normalized})
        return True, "入群验证消息模板已保存。"

    async def get_verify_template_presets(self) -> list[dict[str, str | bool]]:
        """返回可切换的验证码模板预设列表。"""
        runtime_settings = await self.get_runtime_settings()
        active_key = runtime_settings["verify_template_preset"]
        if active_key == "custom" and not CUSTOM_VERIFY_TEMPLATE_PATH.exists():
            active_key = "classic"
        presets: list[dict[str, str | bool]] = []
        for key, meta in VERIFY_TEMPLATE_PRESETS.items():
            presets.append(
                {
                    "key": key,
                    "name": meta["name"],
                    "description": meta["description"],
                    "active": key == active_key,
                    "editable": False,
                }
            )
        if CUSTOM_VERIFY_TEMPLATE_PATH.exists():
            presets.append(
                {
                    "key": "custom",
                    "name": "自定义主题",
                    "description": "来自管理台编辑器，保存后自动生成。",
                    "active": active_key == "custom",
                    "editable": True,
                }
            )
        return presets

    async def get_active_verify_template_profile(self) -> VerifyTemplateProfile:
        """返回当前生效模板的完整信息。"""
        runtime_settings = await self.get_runtime_settings()
        preset_key = runtime_settings["verify_template_preset"]
        if preset_key == "custom" and CUSTOM_VERIFY_TEMPLATE_PATH.exists():
            template_html = CUSTOM_VERIFY_TEMPLATE_PATH.read_text(encoding="utf-8")
            return VerifyTemplateProfile(
                key="custom",
                name="自定义主题",
                description="当前使用管理台保存的自定义模板。",
                html=template_html,
                source="custom",
                editable=True,
            )

        normalized_key = self._normalize_verify_template_preset(preset_key)
        preset_meta = VERIFY_TEMPLATE_PRESETS[normalized_key]
        template_html = Path(preset_meta["file"]).read_text(encoding="utf-8")
        return VerifyTemplateProfile(
            key=normalized_key,
            name=preset_meta["name"],
            description=preset_meta["description"],
            html=template_html,
            source="preset",
            editable=False,
        )

    async def save_verify_template_html(self, template_html: str) -> tuple[bool, str]:
        """保存自定义验证码模板。"""
        normalized = template_html.strip()
        success, error_message = self._validate_verify_template_html(normalized)
        if not success:
            return False, error_message
        CUSTOM_VERIFY_TEMPLATE_PATH.write_text(normalized + "\n", encoding="utf-8")
        await self.update_app_configs({"verify_template_preset": "custom"})
        return True, "验证码模板已保存，并已切换到自定义主题。"

    async def reset_verify_template_html(self) -> None:
        """恢复默认验证码模板。"""
        if CUSTOM_VERIFY_TEMPLATE_PATH.exists():
            CUSTOM_VERIFY_TEMPLATE_PATH.unlink()
        await self.update_app_configs({"verify_template_preset": "classic"})

    async def activate_verify_template_preset(self, preset_key: str) -> tuple[bool, str]:
        """切换当前使用的验证码模板预设。"""
        normalized_key = self._normalize_verify_template_preset(preset_key)
        if normalized_key == "custom":
            if not CUSTOM_VERIFY_TEMPLATE_PATH.exists():
                return False, "当前还没有自定义主题可切换。"
            await self.update_app_configs({"verify_template_preset": "custom"})
            return True, "已切换到自定义主题。"

        await self.update_app_configs({"verify_template_preset": normalized_key})
        return True, f"已切换到“{VERIFY_TEMPLATE_PRESETS[normalized_key]['name']}”预设。"

    async def get_target_groups(self) -> set[int]:
        """获取当前生效的目标群集合。"""
        runtime_settings = await self.get_runtime_settings()
        return runtime_settings["target_groups"]

    async def get_superusers(self) -> set[int]:
        """获取当前生效的超级管理员集合。"""
        runtime_settings = await self.get_runtime_settings()
        return runtime_settings["superusers"]

    async def has_basic_setup(self) -> bool:
        """基础配置是否已满足进入管理台的最低要求。"""
        runtime_settings = await self.get_runtime_settings()
        return bool(runtime_settings["target_groups"] and runtime_settings["superusers"])

    async def set_preferred_onebot_client(self, client_root: str) -> bool:
        """保存当前选中的 OneBot 客户端。"""
        runtime_settings = await self.get_runtime_settings()
        client = self._find_onebot_client(
            client_root,
            runtime_dir_text=runtime_settings["lagrange_qr_dir"],
        )
        if client is None:
            return False
        await self.update_app_configs({"preferred_onebot_client": str(client.root)})
        self._scan_cache.clear()
        return True

    async def _sync_group_config_defaults(self) -> None:
        """把当前全局默认值同步到已存在的目标群配置。"""
        runtime_settings = await self.get_runtime_settings()
        target_groups = runtime_settings["target_groups"]
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(GroupConfig))
            group_configs = result.scalars().all()
            for group_config in group_configs:
                if group_config.group_id in target_groups:
                    group_config.timeout_minutes = runtime_settings["timeout_minutes"]
                    group_config.max_error_times = runtime_settings["max_error_times"]
                    group_config.updated_at = datetime.now()
            await session.commit()

    @staticmethod
    def _normalize_verify_template_preset(raw_value: str) -> str:
        """把模板预设名规范化为已支持的键。"""
        preset_key = str(raw_value).strip().lower() or "classic"
        if preset_key == "custom":
            return "custom"
        if preset_key not in VERIFY_TEMPLATE_PRESETS:
            return "classic"
        return preset_key

    @staticmethod
    def _validate_verify_template_html(template_html: str) -> tuple[bool, str]:
        """校验模板是否包含渲染所需的关键结构。"""
        if not template_html:
            return False, "模板内容不能为空。"
        if 'id="verify-card"' not in template_html and "id='verify-card'" not in template_html:
            return False, '模板里必须保留 id="verify-card" 的根节点。'
        required_placeholders = ("{{verify_code}}", "{{user_qq}}", "{{group_name}}", "{{expire_time}}")
        missing = [item for item in required_placeholders if item not in template_html]
        if missing:
            return False, f"模板缺少占位符：{', '.join(missing)}"
        return True, ""

    @staticmethod
    def _validate_verify_message_template(template_text: str) -> tuple[bool, str]:
        """校验入群提示消息模板。"""
        if not template_text:
            return False, "消息模板不能为空。"
        if len(template_text) > 600:
            return False, "消息模板过长，请控制在 600 个字符以内。"
        return True, ""

    async def set_group_enabled(self, group_id: int, enabled: bool) -> bool:
        """开启或关闭指定群验证功能。"""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(GroupConfig).where(GroupConfig.group_id == group_id)
            )
            group_config = result.scalar_one_or_none()
            if group_config is None:
                return False

            group_config.enabled = enabled
            group_config.updated_at = datetime.now()
            await session.commit()
            return True

    async def get_group_config(self, group_id: int) -> GroupConfig | None:
        """读取指定群在数据库中的验证配置。"""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(GroupConfig).where(GroupConfig.group_id == group_id)
            )
            return result.scalar_one_or_none()

    async def get_target_group_configs(self) -> list[GroupConfig]:
        """返回当前目标群对应的配置列表。"""
        target_groups = await self.get_target_groups()
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(GroupConfig))
            group_configs = result.scalars().all()
        return sorted(
            [item for item in group_configs if item.group_id in target_groups],
            key=lambda item: item.group_id,
        )

    async def update_group_timeout_minutes(self, group_id: int, timeout_minutes: int) -> bool:
        """更新单群验证超时时间。"""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(GroupConfig).where(GroupConfig.group_id == group_id)
            )
            group_config = result.scalar_one_or_none()
            if group_config is None:
                return False
            group_config.timeout_minutes = timeout_minutes
            group_config.updated_at = datetime.now()
            await session.commit()
            return True

    async def update_group_max_error_times(self, group_id: int, max_error_times: int) -> bool:
        """更新单群最大错误次数。"""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(GroupConfig).where(GroupConfig.group_id == group_id)
            )
            group_config = result.scalar_one_or_none()
            if group_config is None:
                return False
            group_config.max_error_times = max_error_times
            group_config.updated_at = datetime.now()
            await session.commit()
            return True

    async def handle_member_increase(self, bot: Bot, event: GroupIncreaseNoticeEvent) -> None:
        """
        处理新人入群事件。

        处理规则：
        1. 非目标群直接忽略
        2. 超级管理员直接放行
        3. 同一用户重复入群时，旧验证码立即失效并重置状态
        4. 创建新的 5 分钟超时任务
        """
        group_id = event.group_id
        user_id = event.user_id

        target_groups = await self.get_target_groups()
        if group_id not in target_groups:
            return

        superusers = await self.get_superusers()
        if user_id in superusers:
            return

        if str(user_id) == str(bot.self_id):
            return

        group_config = await self.get_group_config(group_id)
        if group_config is None or not group_config.enabled:
            return

        key = (group_id, user_id)
        async with self._get_lock(key):
            try:
                join_time = datetime.now()
                expire_time = join_time + timedelta(minutes=group_config.timeout_minutes)
                verify_code = await self._generate_unique_verify_code(group_id)
                record = await self._upsert_verify_record(
                    user_id=user_id,
                    group_id=group_id,
                    verify_code=verify_code,
                    join_time=join_time,
                    expire_time=expire_time,
                )

                # 新记录写入完成后，立刻创建新的超时任务。
                self._create_timeout_task(group_id, user_id, record.id, expire_time)

                group_name = await self._fetch_group_name(bot, group_id)
                image_bytes = await self._render_verify_image_with_retry(
                    verify_code=verify_code,
                    user_id=user_id,
                    group_name=group_name,
                    expire_time=expire_time,
                )

                verify_text = await self._render_verify_message_text(
                    user_id=user_id,
                    group_id=group_id,
                    group_name=group_name,
                    verify_code=verify_code,
                    timeout_minutes=group_config.timeout_minutes,
                    max_error_times=group_config.max_error_times,
                    expire_time=expire_time,
                )
                message = MessageSegment.at(user_id) + MessageSegment.text(verify_text)
                if image_bytes is not None:
                    message += MessageSegment.image(file=image_bytes)
                else:
                    # 按要求增加兜底：HTML 渲染彻底失败时仍然继续流程，防止插件中断。
                    message += MessageSegment.text(f"\n验证码：{verify_code}")

                await bot.send_group_msg(group_id=group_id, message=message)
            except Exception as exc:
                logger.exception(
                    f"处理入群验证流程失败 group_id={group_id} user_id={user_id} error={exc}"
                )

    async def handle_group_message(self, bot: Bot, event: GroupMessageEvent) -> None:
        """
        处理群消息中的验证码输入。

        这里只监听“待验证用户发送的纯文本消息”，避免误伤普通群聊消息。
        """
        target_groups = await self.get_target_groups()
        if event.group_id not in target_groups:
            return

        if not self._is_pure_text_message(event.message):
            return

        key = (event.group_id, event.user_id)
        async with self._get_lock(key):
            try:
                record = await self._get_pending_record(event.group_id, event.user_id)
                if record is None:
                    return

                now = datetime.now()
                if now >= record.expire_time:
                    await self._mark_record_status(record.id, VerifyStatus.TIMEOUT_KICKED)
                    self._cancel_timeout_task(key)
                    await bot.send(
                        event,
                        MessageSegment.at(event.user_id)
                        + MessageSegment.text("验证码已过期，您已被移出群聊"),
                    )
                    await self._kick_member(
                        bot=bot,
                        group_id=event.group_id,
                        user_id=event.user_id,
                        fail_message="验证超时，但机器人无踢人权限，请管理员手动处理",
                    )
                    return

                # 按要求忽略前后空格并且大小写不敏感。
                user_input = event.get_plaintext().strip().upper()
                if user_input == record.verify_code.upper():
                    await self._mark_record_status(record.id, VerifyStatus.PASSED)
                    self._cancel_timeout_task(key)
                    await bot.send(
                        event,
                        MessageSegment.at(event.user_id)
                        + MessageSegment.text("验证通过！欢迎加入本群"),
                    )
                    return

                group_config = await self.get_group_config(event.group_id)
                max_error_times = (
                    group_config.max_error_times
                    if group_config is not None
                    else (await self.get_runtime_settings())["max_error_times"]
                )
                new_error_count = await self._increase_error_count(record.id)
                remaining_times = max(max_error_times - new_error_count, 0)

                if new_error_count >= max_error_times:
                    await self._mark_record_status(record.id, VerifyStatus.KICKED)
                    self._cancel_timeout_task(key)
                    await bot.send(
                        event,
                        MessageSegment.at(event.user_id)
                        + MessageSegment.text("验证码错误次数已达上限，您将被移出群聊"),
                    )
                    await self._kick_member(
                        bot=bot,
                        group_id=event.group_id,
                        user_id=event.user_id,
                        fail_message="验证失败，但机器人无踢人权限，请管理员手动处理",
                    )
                    return

                await bot.send(
                    event,
                    MessageSegment.at(event.user_id)
                    + MessageSegment.text(
                        f"验证码错误，请重新输入，您还有{remaining_times}次机会"
                    ),
                )
            except Exception as exc:
                logger.exception(
                    f"处理验证码消息失败 group_id={event.group_id} user_id={event.user_id} error={exc}"
                )

    async def restore_pending_tasks(self) -> None:
        """
        程序重启后恢复仍未过期的待验证任务。

        同时会把数据库里“已经过期但还停留在待验证状态”的旧记录修正为超时状态，
        避免机器人重启后留下脏数据。
        """
        now = datetime.now()

        async with AsyncSessionLocal() as session:
            expired_result = await session.execute(
                select(VerifyRecord).where(
                    VerifyRecord.status == VerifyStatus.PENDING,
                    VerifyRecord.expire_time <= now,
                )
            )
            expired_records = expired_result.scalars().all()
            for record in expired_records:
                record.status = VerifyStatus.TIMEOUT_KICKED
                record.updated_at = now

            pending_result = await session.execute(
                select(VerifyRecord).where(
                    VerifyRecord.status == VerifyStatus.PENDING,
                    VerifyRecord.expire_time > now,
                )
            )
            pending_records = pending_result.scalars().all()
            await session.commit()

        for record in pending_records:
            self._create_timeout_task(
                record.group_id,
                record.user_id,
                record.id,
                record.expire_time,
            )

    async def _upsert_verify_record(
        self,
        *,
        user_id: int,
        group_id: int,
        verify_code: str,
        join_time: datetime,
        expire_time: datetime,
    ) -> VerifyRecord:
        """同一用户重复入群时复用同一条记录并重置验证码和状态。"""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(VerifyRecord).where(
                    VerifyRecord.user_id == user_id,
                    VerifyRecord.group_id == group_id,
                )
            )
            record = result.scalar_one_or_none()

            if record is None:
                record = VerifyRecord(
                    user_id=user_id,
                    group_id=group_id,
                    verify_code=verify_code,
                    join_time=join_time,
                    expire_time=expire_time,
                    status=VerifyStatus.PENDING,
                    error_count=0,
                )
                session.add(record)
            else:
                # 重复入群时视为一轮全新的验证流程，旧验证码立刻作废。
                record.verify_code = verify_code
                record.join_time = join_time
                record.expire_time = expire_time
                record.status = VerifyStatus.PENDING
                record.error_count = 0
                record.updated_at = datetime.now()

            await session.commit()
            await session.refresh(record)
            return record

    async def _get_pending_record(self, group_id: int, user_id: int) -> VerifyRecord | None:
        """只读取待验证状态的记录，其他状态一律忽略。"""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(VerifyRecord).where(
                    VerifyRecord.group_id == group_id,
                    VerifyRecord.user_id == user_id,
                    VerifyRecord.status == VerifyStatus.PENDING,
                )
            )
            return result.scalar_one_or_none()

    async def _mark_record_status(self, record_id: int, status: str) -> None:
        """更新验证记录状态。"""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(VerifyRecord).where(VerifyRecord.id == record_id)
            )
            record = result.scalar_one_or_none()
            if record is None:
                return
            record.status = status
            record.updated_at = datetime.now()
            await session.commit()

    async def _increase_error_count(self, record_id: int) -> int:
        """用户输错验证码时递增错误次数，并返回最新次数。"""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(VerifyRecord).where(VerifyRecord.id == record_id)
            )
            record = result.scalar_one_or_none()
            if record is None:
                return 0
            record.error_count += 1
            record.updated_at = datetime.now()
            await session.commit()
            return record.error_count

    async def _fetch_group_name(self, bot: Bot, group_id: int) -> str:
        """尽量获取真实群名，失败时退回到群号文本。"""
        try:
            group_info = await bot.get_group_info(group_id=group_id, no_cache=True)
            group_name = group_info.get("group_name", "")
            return group_name or str(group_id)
        except Exception as exc:
            logger.warning(f"获取群名失败 group_id={group_id} error={exc}")
            return str(group_id)

    async def _generate_unique_verify_code(self, group_id: int) -> str:
        """
        生成 4 位混合验证码，并尽量保证当前待验证记录中不重复。

        由于验证码长度固定为 4 位，不可能对全历史数据永久绝对唯一，
        这里保证“同一时刻的待验证用户之间不重复”，足够满足入群验证场景。
        """
        alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(VerifyRecord.verify_code).where(
                    VerifyRecord.group_id == group_id,
                    VerifyRecord.status == VerifyStatus.PENDING,
                )
            )
            used_codes = {code for code in result.scalars().all()}

        for _ in range(100):
            code = "".join(self._random.choice(alphabet) for _ in range(4))
            if code not in used_codes:
                return code

        # 极端情况下兜底返回，避免无限循环。
        return "".join(self._random.choice(alphabet) for _ in range(4))

    async def _render_verify_image_with_retry(
        self,
        *,
        verify_code: str,
        user_id: int,
        group_name: str,
        expire_time: datetime,
    ) -> bytes | None:
        """渲染验证码图片，失败后自动重试 2 次。"""
        runtime_settings = await self.get_runtime_settings()
        attempts = runtime_settings["image_retry_times"] + 1
        for attempt in range(1, attempts + 1):
            try:
                return await self._render_verify_image(
                    verify_code=verify_code,
                    user_id=user_id,
                    group_name=group_name,
                    expire_time=expire_time,
                )
            except Exception as exc:
                logger.exception(
                    f"验证码图片渲染失败 attempt={attempt}/{attempts} user_id={user_id} error={exc}"
                )
        return None

    async def _render_verify_image(
        self,
        *,
        verify_code: str,
        user_id: int,
        group_name: str,
        expire_time: datetime,
    ) -> bytes:
        """使用 Playwright 把 HTML 模板渲染为 PNG 图片。"""
        template_html = await self.get_verify_template_html()
        filled_html = (
            template_html.replace("{{verify_code}}", html.escape(verify_code))
            .replace("{{user_qq}}", html.escape(str(user_id)))
            .replace("{{group_name}}", html.escape(group_name))
            .replace(
                "{{expire_time}}",
                html.escape(expire_time.strftime("%Y-%m-%d %H:%M:%S")),
            )
        )

        async with async_playwright() as playwright:
            runtime_settings = await self.get_runtime_settings()
            browser_name = runtime_settings["playwright_browser"]
            browser_launcher = getattr(playwright, browser_name, None)
            if browser_launcher is None:
                raise ValueError(f"不支持的浏览器类型: {browser_name}")

            browser = await browser_launcher.launch()
            try:
                page = await browser.new_page(viewport={"width": 720, "height": 520})
                await page.set_content(filled_html, wait_until="networkidle")
                card = page.locator("#verify-card")
                image_bytes = await card.screenshot(type="png")
                return image_bytes
            finally:
                await browser.close()

    async def _render_verify_message_text(
        self,
        *,
        user_id: int,
        group_id: int,
        group_name: str,
        verify_code: str,
        timeout_minutes: int,
        max_error_times: int,
        expire_time: datetime,
    ) -> str:
        """根据管理台配置渲染入群验证提示文案。"""
        template_text = await self.get_verify_message_template()
        replacements = {
            "{{user_qq}}": str(user_id),
            "{{user_name}}": f"QQ {user_id}",
            "{{group_id}}": str(group_id),
            "{{group_name}}": group_name,
            "{{verify_code}}": verify_code,
            "{{timeout_minutes}}": str(timeout_minutes),
            "{{max_error_times}}": str(max_error_times),
            "{{expire_time}}": expire_time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        rendered = template_text
        for placeholder, value in replacements.items():
            rendered = rendered.replace(placeholder, value)
        return rendered.strip()

    def _create_timeout_task(
        self,
        group_id: int,
        user_id: int,
        record_id: int,
        expire_time: datetime,
    ) -> None:
        """创建新的超时踢人任务；若已有旧任务则先取消。"""
        key = (group_id, user_id)
        self._cancel_timeout_task(key)
        self._tasks[key] = asyncio.create_task(
            self._timeout_worker(group_id, user_id, record_id, expire_time)
        )

    def _cancel_timeout_task(self, key: tuple[int, int]) -> None:
        """取消指定用户已有的超时任务。"""
        task = self._tasks.pop(key, None)
        if task is not None and not task.done():
            task.cancel()

    async def _timeout_worker(
        self,
        group_id: int,
        user_id: int,
        record_id: int,
        expire_time: datetime,
    ) -> None:
        """
        延迟到过期时间后执行超时踢人。

        record_id 也会一起校验，避免“重复入群后旧任务误踢新人”。
        """
        key = (group_id, user_id)
        try:
            sleep_seconds = max((expire_time - datetime.now()).total_seconds(), 0)
            await asyncio.sleep(sleep_seconds)

            async with self._get_lock(key):
                record = await self._get_pending_record(group_id, user_id)
                if record is None:
                    return

                # 如果数据库里的记录已被刷新成新一轮验证，则这是旧任务，直接丢弃。
                if record.id != record_id:
                    return

                if datetime.now() < record.expire_time:
                    return

                await self._mark_record_status(record.id, VerifyStatus.TIMEOUT_KICKED)
                bot = self._get_available_bot()
                if bot is None:
                    logger.warning(
                        f"超时任务执行时没有可用机器人连接 group_id={group_id} user_id={user_id}"
                    )
                    return

                await bot.send_group_msg(
                    group_id=group_id,
                    message=MessageSegment.at(user_id)
                    + MessageSegment.text(" 因超时未完成入群验证，已被移出群聊"),
                )
                await self._kick_member(
                    bot=bot,
                    group_id=group_id,
                    user_id=user_id,
                    fail_message="验证超时，但机器人无踢人权限，请管理员手动处理",
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                f"超时踢人任务执行失败 group_id={group_id} user_id={user_id} error={exc}"
            )
        finally:
            current = self._tasks.get(key)
            if current is asyncio.current_task():
                self._tasks.pop(key, None)

    async def _kick_member(
        self,
        *,
        bot: Bot,
        group_id: int,
        user_id: int,
        fail_message: str,
    ) -> None:
        """调用 OneBot v11 踢人接口，并对权限不足场景做兜底提示。"""
        try:
            await bot.set_group_kick(
                group_id=group_id,
                user_id=user_id,
                reject_add_request=False,
            )
        except Exception as exc:
            logger.exception(f"踢人失败 group_id={group_id} user_id={user_id} error={exc}")
            try:
                await bot.send_group_msg(group_id=group_id, message=fail_message)
            except Exception as send_exc:
                logger.exception(
                    f"发送踢人失败兜底消息失败 group_id={group_id} user_id={user_id} error={send_exc}"
                )

    def _get_available_bot(self) -> Bot | None:
        """从当前在线机器人列表里取一个可用的 OneBot v11 连接。"""
        for bot in get_bots().values():
            if isinstance(bot, Bot):
                return bot
        return None

    @staticmethod
    def _format_uptime(delta: timedelta) -> str:
        """把运行时长格式化为简短文本。"""
        total_seconds = max(int(delta.total_seconds()), 0)
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours} 小时 {minutes} 分钟"
        if minutes > 0:
            return f"{minutes} 分钟 {seconds} 秒"
        return f"{seconds} 秒"

    def _get_lock(self, key: tuple[int, int]) -> asyncio.Lock:
        """为单个用户验证流程提供互斥锁，避免并发竞态。"""
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    @staticmethod
    def _is_pure_text_message(message: Message) -> bool:
        """仅接受纯文本消息，带图片、艾特、表情的消息全部忽略。"""
        if not message:
            return False
        return all(segment.type == "text" for segment in message)

    async def get_recent_records(self, limit: int = 50) -> list[VerifyRecord]:
        """读取最近的验证记录，供本地管理台展示。"""
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(VerifyRecord).order_by(VerifyRecord.updated_at.desc()).limit(limit)
            )
            return list(result.scalars().all())

    async def get_dashboard_summary(self) -> dict[str, Any]:
        """返回首页仪表盘需要的摘要信息。"""
        runtime_settings = await self.get_runtime_settings()
        async with AsyncSessionLocal() as session:
            pending_count = await session.scalar(
                select(func.count()).select_from(VerifyRecord).where(VerifyRecord.status == VerifyStatus.PENDING)
            )
            passed_count = await session.scalar(
                select(func.count()).select_from(VerifyRecord).where(VerifyRecord.status == VerifyStatus.PASSED)
            )
            kicked_count = await session.scalar(
                select(func.count()).select_from(VerifyRecord).where(
                    VerifyRecord.status.in_([VerifyStatus.KICKED, VerifyStatus.TIMEOUT_KICKED])
                )
            )
        return {
            "bot_online": self._get_available_bot() is not None,
            "target_group_count": len(runtime_settings["target_groups"]),
            "pending_count": int(pending_count or 0),
            "passed_count": int(passed_count or 0),
            "kicked_count": int(kicked_count or 0),
            "task_count": len(self._tasks),
        }

    async def get_service_status_snapshot(self) -> dict[str, Any]:
        """汇总服务端状态，供命令和管理台复用。"""
        runtime_settings = await self.get_runtime_settings()
        summary = await self.get_dashboard_summary()
        primary_client = await self.get_primary_onebot_client()
        qr_image = await self.get_latest_qr_image(
            selected_client_root=str(primary_client["root"]) if primary_client else None
        )
        system_resources = await self.get_system_resource_snapshot()
        return {
            "summary": summary,
            "runtime_settings": runtime_settings,
            "primary_client": primary_client,
            "system_resources": system_resources,
            "latest_qr_path": str(qr_image) if qr_image is not None else "",
            "latest_qr_mtime": datetime.fromtimestamp(qr_image.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            if qr_image is not None
            else "暂无",
            "started_at": self._started_at.strftime("%Y-%m-%d %H:%M:%S"),
            "uptime_text": self._format_uptime(datetime.now() - self._started_at),
        }

    async def render_service_status_image(self) -> bytes:
        """把服务状态渲染成图片。"""
        snapshot = await self.get_service_status_snapshot()
        summary = snapshot["summary"]
        runtime_settings = snapshot["runtime_settings"]
        primary_client = snapshot["primary_client"]
        system_resources = snapshot["system_resources"]
        resource_cards = "".join(
            (
                '<div class="metric-card">'
                f'<div class="metric-head"><span>{html.escape(item["label"])}</span><strong>{html.escape(item["value"])}</strong></div>'
                f'<div class="meter"><div class="meter-fill {html.escape(item["tone"])}" style="width: {item["percent"]:.2f}%"></div></div>'
                f'<div class="metric-foot">{html.escape(item["detail"])}</div>'
                "</div>"
            )
            for item in system_resources["meter_cards"]
        )
        process_rows = "".join(
            (
                "<tr>"
                f"<td>{html.escape(item['label'])}</td>"
                f"<td>{html.escape(item['value'])}</td>"
                "</tr>"
            )
            for item in system_resources["process_rows"]
        )
        template_html = SERVICE_STATUS_TEMPLATE_PATH.read_text(encoding="utf-8")
        filled_html = (
            template_html.replace("{{bot_status}}", "在线" if summary["bot_online"] else "离线")
            .replace("{{bot_status_class}}", "online" if summary["bot_online"] else "offline")
            .replace("{{started_at}}", html.escape(snapshot["started_at"]))
            .replace("{{uptime_text}}", html.escape(snapshot["uptime_text"]))
            .replace("{{task_count}}", str(summary["task_count"]))
            .replace("{{pending_count}}", str(summary["pending_count"]))
            .replace("{{passed_count}}", str(summary["passed_count"]))
            .replace("{{kicked_count}}", str(summary["kicked_count"]))
            .replace("{{target_group_count}}", str(summary["target_group_count"]))
            .replace(
                "{{superuser_count}}",
                str(len(runtime_settings["superusers"])),
            )
            .replace(
                "{{primary_client_name}}",
                html.escape(str(primary_client["name"])) if primary_client else "未检测到客户端",
            )
            .replace(
                "{{primary_client_root}}",
                html.escape(str(primary_client["root"])) if primary_client else "暂无",
            )
            .replace(
                "{{client_launchable}}",
                "可自动启动" if primary_client and bool(primary_client.get("launchable")) else "需手动处理",
            )
            .replace("{{latest_qr_mtime}}", html.escape(snapshot["latest_qr_mtime"]))
            .replace("{{latest_qr_path}}", html.escape(snapshot["latest_qr_path"] or "暂无"))
            .replace(
                "{{verify_template_preset}}",
                html.escape(str(runtime_settings["verify_template_preset"])),
            )
            .replace(
                "{{playwright_browser}}",
                html.escape(str(runtime_settings["playwright_browser"])),
            )
            .replace("{{cpu_percent}}", html.escape(system_resources["cpu_percent_text"]))
            .replace("{{load_average}}", html.escape(system_resources["load_average"]))
            .replace("{{memory_percent}}", html.escape(system_resources["memory_percent_text"]))
            .replace("{{memory_detail}}", html.escape(system_resources["memory_detail"]))
            .replace("{{disk_percent}}", html.escape(system_resources["disk_percent_text"]))
            .replace("{{disk_detail}}", html.escape(system_resources["disk_detail"]))
            .replace("{{gpu_summary}}", html.escape(system_resources["gpu_summary"]))
            .replace("{{network_detail}}", html.escape(system_resources["network_detail"]))
            .replace("{{boot_time}}", html.escape(system_resources["boot_time"]))
            .replace("{{resource_cards}}", resource_cards)
            .replace("{{process_rows}}", process_rows)
        )

        async with async_playwright() as playwright:
            browser_name = runtime_settings["playwright_browser"]
            browser_launcher = getattr(playwright, browser_name, None)
            if browser_launcher is None:
                raise ValueError(f"不支持的浏览器类型: {browser_name}")
            browser = await browser_launcher.launch()
            try:
                page = await browser.new_page(viewport={"width": 1360, "height": 980})
                await page.set_content(filled_html, wait_until="networkidle")
                card = page.locator("#service-status-card")
                return await card.screenshot(type="png")
            finally:
                await browser.close()

    async def get_system_resource_snapshot(self) -> dict[str, Any]:
        """采集服务端资源占用，供状态图展示。"""
        if psutil is None:
            return self._build_basic_system_resource_snapshot()

        cpu_percent = await asyncio.to_thread(psutil.cpu_percent, 0.15)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage(str(plugin_settings.project_root))
        process = psutil.Process(os.getpid())
        process_memory = process.memory_info().rss
        net_io = psutil.net_io_counters()
        gpu_snapshot = await asyncio.to_thread(self._get_gpu_resource_snapshot)
        try:
            load_avg_values = os.getloadavg()
            load_average = " / ".join(f"{value:.2f}" for value in load_avg_values)
        except (AttributeError, OSError):
            load_average = "当前系统不支持"

        process_memory_percent = (process_memory / memory.total) * 100 if memory.total else 0.0
        meter_cards = [
            {
                "label": "CPU",
                "value": f"{cpu_percent:.1f}%",
                "percent": max(min(cpu_percent, 100.0), 0.0),
                "detail": f"负载 {load_average}",
                "tone": self._resource_tone(cpu_percent),
            },
            {
                "label": "内存",
                "value": f"{memory.percent:.1f}%",
                "percent": max(min(float(memory.percent), 100.0), 0.0),
                "detail": f"{self._format_bytes(memory.used)} / {self._format_bytes(memory.total)}",
                "tone": self._resource_tone(float(memory.percent)),
            },
            {
                "label": "磁盘",
                "value": f"{disk.percent:.1f}%",
                "percent": max(min(float(disk.percent), 100.0), 0.0),
                "detail": f"{self._format_bytes(disk.used)} / {self._format_bytes(disk.total)}",
                "tone": self._resource_tone(float(disk.percent)),
            },
            {
                "label": "机器人进程",
                "value": self._format_bytes(process_memory),
                "percent": max(min(process_memory_percent, 100.0), 0.0),
                "detail": f"PID {process.pid} | 线程 {process.num_threads()}",
                "tone": self._resource_tone(process_memory_percent),
            },
        ]
        meter_cards.extend(gpu_snapshot["meter_cards"])
        return {
            "cpu_percent_text": f"{cpu_percent:.1f}%",
            "load_average": load_average,
            "memory_percent_text": f"{memory.percent:.1f}%",
            "memory_detail": f"{self._format_bytes(memory.used)} / {self._format_bytes(memory.total)}",
            "disk_percent_text": f"{disk.percent:.1f}%",
            "disk_detail": f"{self._format_bytes(disk.used)} / {self._format_bytes(disk.total)}",
            "gpu_summary": gpu_snapshot["summary"],
            "network_detail": f"↑ {self._format_bytes(net_io.bytes_sent)}  ↓ {self._format_bytes(net_io.bytes_recv)}",
            "boot_time": datetime.fromtimestamp(psutil.boot_time()).strftime("%Y-%m-%d %H:%M:%S"),
            "meter_cards": meter_cards,
            "process_rows": [
                {"label": "进程 PID", "value": str(process.pid)},
                {"label": "线程数", "value": str(process.num_threads())},
                {"label": "启动时间", "value": self._started_at.strftime("%Y-%m-%d %H:%M:%S")},
                {"label": "系统开机", "value": datetime.fromtimestamp(psutil.boot_time()).strftime("%Y-%m-%d %H:%M:%S")},
                {"label": "GPU 状态", "value": gpu_snapshot["summary"]},
                {"label": "网络累计", "value": f"↑ {self._format_bytes(net_io.bytes_sent)} / ↓ {self._format_bytes(net_io.bytes_recv)}"},
            ],
        }

    def _build_basic_system_resource_snapshot(self) -> dict[str, Any]:
        """在未安装 psutil 时提供基础资源快照，避免插件启动失败。"""
        disk = shutil.disk_usage(str(plugin_settings.project_root))
        disk_percent = (disk.used / disk.total * 100) if disk.total else 0.0
        try:
            load_avg_values = os.getloadavg()
            load_average = " / ".join(f"{value:.2f}" for value in load_avg_values)
        except (AttributeError, OSError):
            load_average = "当前系统不支持"

        meter_cards = [
            {
                "label": "CPU",
                "value": "未安装 psutil",
                "percent": 0.0,
                "detail": f"负载 {load_average}",
                "tone": "good",
            },
            {
                "label": "内存",
                "value": "未安装 psutil",
                "percent": 0.0,
                "detail": "安装 psutil 后显示实时内存占用",
                "tone": "good",
            },
            {
                "label": "磁盘",
                "value": f"{disk_percent:.1f}%",
                "percent": max(min(disk_percent, 100.0), 0.0),
                "detail": f"{self._format_bytes(disk.used)} / {self._format_bytes(disk.total)}",
                "tone": self._resource_tone(disk_percent),
            },
            {
                "label": "机器人进程",
                "value": "基础模式",
                "percent": 0.0,
                "detail": f"PID {os.getpid()} | 安装 psutil 后显示进程内存",
                "tone": "good",
            },
        ]
        gpu_snapshot = self._get_gpu_resource_snapshot()
        meter_cards.extend(gpu_snapshot["meter_cards"])
        return {
            "cpu_percent_text": "未安装 psutil",
            "load_average": load_average,
            "memory_percent_text": "未安装 psutil",
            "memory_detail": "安装 psutil 后显示",
            "disk_percent_text": f"{disk_percent:.1f}%",
            "disk_detail": f"{self._format_bytes(disk.used)} / {self._format_bytes(disk.total)}",
            "gpu_summary": gpu_snapshot["summary"],
            "network_detail": "安装 psutil 后显示",
            "boot_time": "安装 psutil 后显示",
            "meter_cards": meter_cards,
            "process_rows": [
                {"label": "进程 PID", "value": str(os.getpid())},
                {"label": "线程数", "value": "安装 psutil 后显示"},
                {"label": "启动时间", "value": self._started_at.strftime("%Y-%m-%d %H:%M:%S")},
                {"label": "GPU 状态", "value": gpu_snapshot["summary"]},
                {"label": "系统开机", "value": "安装 psutil 后显示"},
                {"label": "网络累计", "value": "安装 psutil 后显示"},
            ],
        }

    def _get_gpu_resource_snapshot(self) -> dict[str, Any]:
        """尽量通过 nvidia-smi 采集 GPU 占用；不可用时优雅降级。"""
        try:
            completed = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                check=True,
                text=True,
                timeout=2,
            )
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            return {"summary": "未检测到 NVIDIA GPU", "meter_cards": []}

        lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        if not lines:
            return {"summary": "未检测到 NVIDIA GPU", "meter_cards": []}

        meter_cards: list[dict[str, Any]] = []
        summaries: list[str] = []
        for line in lines:
            parts = [part.strip() for part in line.split(",")]
            if len(parts) < 6:
                continue
            index, name, utilization_text, memory_used_text, memory_total_text, temperature_text = parts[:6]
            try:
                utilization = float(utilization_text)
                memory_used = float(memory_used_text)
                memory_total = float(memory_total_text)
                temperature = int(float(temperature_text))
            except ValueError:
                continue
            memory_percent = (memory_used / memory_total * 100.0) if memory_total > 0 else 0.0
            summaries.append(
                f"GPU {index} {name} | 核心 {utilization:.0f}% | 显存 {memory_used:.0f}/{memory_total:.0f} MiB | {temperature}°C"
            )
            meter_cards.append(
                {
                    "label": f"GPU {index}",
                    "value": f"{utilization:.0f}%",
                    "percent": max(min(utilization, 100.0), 0.0),
                    "detail": f"{name} | 显存 {memory_used:.0f}/{memory_total:.0f} MiB | {temperature}°C",
                    "tone": self._resource_tone(max(utilization, memory_percent)),
                }
            )

        if not meter_cards:
            return {"summary": "GPU 信息读取失败", "meter_cards": []}
        return {"summary": " ; ".join(summaries), "meter_cards": meter_cards}

    async def get_setup_status(self) -> dict[str, Any]:
        """返回首次引导页需要的状态信息。"""
        runtime_settings = await self.get_runtime_settings()
        clients = await self.get_detected_onebot_clients()
        selected_client = self._resolve_selected_client(
            clients,
            runtime_settings["preferred_onebot_client"],
        )
        qr_image = await self.get_latest_qr_image(
            selected_client_root=str(selected_client["root"]) if selected_client else None
        )
        bot_online = self._get_available_bot() is not None
        has_basic_config = bool(runtime_settings["target_groups"] and runtime_settings["superusers"])
        return {
            "has_target_groups": bool(runtime_settings["target_groups"]),
            "has_superusers": bool(runtime_settings["superusers"]),
            "has_basic_config": has_basic_config,
            "has_qr_image": qr_image is not None,
            "bot_online": bot_online,
            "detected_client_count": len(clients),
            "has_selected_client": selected_client is not None,
            "selected_client_root": str(selected_client["root"]) if selected_client else "",
            "selected_client_name": str(selected_client["name"]) if selected_client else "",
            "can_auto_launch_onebot": any(bool(client.get("launchable")) for client in clients),
        }

    async def get_latest_qr_image(self, selected_client_root: str | None = None) -> Path | None:
        """自动查找最新二维码图片，优先使用当前选中的客户端。"""
        runtime_settings = await self.get_runtime_settings()
        cache_key = f"latest_qr_image:{selected_client_root or ''}:{runtime_settings['lagrange_qr_dir']}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached

        candidates: list[Path] = []
        if selected_client_root:
            client = self._find_onebot_client(
                selected_client_root,
                runtime_dir_text=runtime_settings["lagrange_qr_dir"],
            )
            if client is not None:
                candidates.extend(self._find_qr_images_for_client(client.root))
        else:
            qr_dir_text = runtime_settings["lagrange_qr_dir"]
            if qr_dir_text:
                qr_dir = Path(qr_dir_text).expanduser()
                candidates.extend(self._find_qr_images_in_dir(qr_dir))

            for candidate_dir in self._discover_qr_search_dirs(runtime_settings["lagrange_qr_dir"]):
                candidates.extend(self._find_qr_images_in_dir(candidate_dir))

        latest = self._pick_latest_file(candidates)
        self._set_cache(cache_key, latest)
        return latest

    async def get_detected_onebot_clients(self) -> list[dict[str, str | bool]]:
        """扫描本机可能的 OneBot 客户端目录。"""
        runtime_settings = await self.get_runtime_settings()
        cache_key = f"onebot_clients:{runtime_settings['lagrange_qr_dir']}"
        cached = self._get_cache(cache_key)
        if cached is None:
            selected_root = runtime_settings["preferred_onebot_client"]
            cached = [
                self._serialize_onebot_client(item, selected_root=selected_root)
                for item in self._discover_onebot_clients(runtime_settings["lagrange_qr_dir"])
            ]
            self._set_cache(cache_key, cached)
        return cached

    async def get_primary_onebot_client(self) -> dict[str, str | bool] | None:
        """返回当前最适合展示和启动的客户端。"""
        runtime_settings = await self.get_runtime_settings()
        clients = await self.get_detected_onebot_clients()
        return self._resolve_selected_client(
            clients,
            runtime_settings["preferred_onebot_client"],
        )

    async def launch_detected_onebot(self, client_root: str) -> tuple[bool, str]:
        """启动用户明确选择的 OneBot 客户端。"""
        runtime_settings = await self.get_runtime_settings()
        client = self._find_onebot_client(client_root, runtime_dir_text=runtime_settings["lagrange_qr_dir"])
        if client is None:
            return False, "未找到你选择的 OneBot 客户端，请先刷新页面后重新选择。"
        if not client.launchable:
            return False, "这个客户端目录只能检测，不能安全自动启动，请手动启动它。"

        process_key = str(client.root)
        process = self._started_onebot_processes.get(process_key)
        if process is not None and process.poll() is None:
            return True, f"{client.name} 已经在运行中。"

        try:
            process = subprocess.Popen(
                client.launch_command,
                cwd=client.root,
                env=client.launch_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as exc:
            logger.exception(f"自动启动 OneBot 失败 root={client.root} error={exc}")
            return False, f"自动启动失败：{exc}"

        self._started_onebot_processes[process_key] = process
        await self.update_app_configs({"preferred_onebot_client": str(client.root)})
        self._scan_cache.clear()
        return True, f"已尝试启动 {client.name}，请等待二维码生成。"

    def _discover_qr_search_dirs(self, runtime_dir_text: str = "") -> list[Path]:
        """扫描可能生成二维码的目录。"""
        candidate_dirs = self._collect_candidate_dirs(runtime_dir_text)
        qr_dirs: list[Path] = []
        for directory in candidate_dirs:
            if directory not in qr_dirs:
                qr_dirs.append(directory)
            for subdir in self._get_qr_related_subdirs(directory):
                if subdir not in qr_dirs:
                    qr_dirs.append(subdir)
        return qr_dirs

    def _discover_onebot_clients(self, runtime_dir_text: str = "") -> list[OneBotClient]:
        """扫描可能存在的 Lagrange / NapCat 客户端。"""
        clients: list[OneBotClient] = []
        seen_roots: set[Path] = set()
        for directory in self._collect_candidate_dirs(runtime_dir_text):
            if directory in seen_roots:
                continue
            seen_roots.add(directory)
            client = self._build_onebot_client(directory)
            if client is not None:
                clients.append(client)
        clients.sort(key=lambda item: (not item.launchable, item.name.lower(), str(item.root)))
        return clients

    def _collect_candidate_dirs(self, runtime_dir_text: str = "") -> list[Path]:
        """优先从项目内独立目录和显式配置目录里筛选候选目录。"""
        roots: list[Path] = []
        if runtime_dir_text:
            roots.append(Path(runtime_dir_text).expanduser())
        elif plugin_settings.lagrange_qr_dir is not None:
            roots.append(plugin_settings.lagrange_qr_dir.expanduser())
        roots.extend(self._get_project_candidate_roots())

        candidate_dirs: list[Path] = []
        seen: set[Path] = set()
        for root in roots:
            if not root.exists() or not root.is_dir():
                continue
            for directory in self._walk_candidate_dirs(root):
                if directory not in seen:
                    seen.add(directory)
                    candidate_dirs.append(directory)
        return candidate_dirs

    def _walk_candidate_dirs(self, root: Path, max_depth: int = 4) -> list[Path]:
        """限制深度扫描可疑目录，避免每次页面加载遍历整个磁盘。"""
        directories: list[Path] = []
        skip_names = {
            ".git",
            ".venv",
            "__pycache__",
            "node_modules",
            ".cache",
            ".local",
            ".cargo",
            ".rustup",
            ".npm",
        }
        allow_hidden = root.name.startswith(".")
        for current_root, dirnames, _filenames in os.walk(root):
            current_path = Path(current_root)
            depth = len(current_path.relative_to(root).parts)
            if depth > max_depth:
                dirnames[:] = []
                continue

            dirnames[:] = [
                name
                for name in dirnames
                if name not in skip_names and (allow_hidden or not name.startswith("."))
            ]
            if self._is_onebot_related_dir(current_path):
                directories.append(current_path)
        return directories

    def _get_project_candidate_roots(self) -> list[Path]:
        """项目内独立运行目录，避免误扫系统 QQ。"""
        return [
            plugin_settings.managed_onebot_dir,
            plugin_settings.managed_onebot_dir / "napcat",
            plugin_settings.managed_onebot_dir / "lagrange",
            plugin_settings.managed_onebot_runtime_dir,
            plugin_settings.managed_onebot_runtime_dir / "napcat",
            plugin_settings.managed_onebot_runtime_dir / "lagrange",
            plugin_settings.project_root / "third_party",
            plugin_settings.project_root / "data" / "group_verify",
            Path.home() / "Napcat",
        ]

    def _get_qr_related_subdirs(self, directory: Path) -> list[Path]:
        """补充更可能出现登录二维码的子目录。"""
        subdirs = [
            directory / "config",
            directory / "data",
            directory / "cache",
            directory / "QQ",
            directory / "qq",
            directory / "global",
            directory / "global" / "nt_data",
            directory / ".config" / "QQ",
            directory / ".config" / "NapCat",
            directory / ".config" / "napcat",
            directory / "opt" / "QQ" / "resources" / "app" / "app_launcher" / "napcat" / "cache",
        ]
        return [item for item in subdirs if item.exists() and item.is_dir()]

    @staticmethod
    def _is_valid_napcat_dir(directory: Path) -> bool:
        """仅把真正注入了 NapCat Shell 的目录识别为 NapCat。"""
        markers = (
            directory / "config" / "onebot11_qq.json",
            directory / "opt" / "QQ" / "resources" / "app" / "loadNapCat.js",
            directory / "opt" / "QQ" / "resources" / "app" / "app_launcher" / "napcat" / "napcat.mjs",
            directory / "QQ" / "resources" / "app" / "loadNapCat.js",
            directory / "QQ" / "resources" / "app" / "app_launcher" / "napcat" / "napcat.mjs",
        )
        return any(path.exists() for path in markers)

    def _is_onebot_related_dir(self, directory: Path) -> bool:
        """判断目录是否像是 Lagrange / NapCat 运行目录。"""
        lower_name = directory.name.lower()
        if "nonebot" in lower_name:
            return False
        if "lagrange" in lower_name:
            return True
        if self._is_valid_napcat_dir(directory):
            return True
        if "napcat" in lower_name:
            return False
        if (directory / "config" / "onebot11_qq.json").exists():
            return True
        if (directory / "QQ").exists():
            return True
        return False

    def _find_qr_images_in_dir(self, directory: Path) -> list[Path]:
        """从指定目录里查找二维码图片。"""
        if not directory.exists() or not directory.is_dir():
            return []

        candidates: list[Path] = []
        for pattern in QR_FILE_PATTERNS:
            for item in directory.glob(pattern):
                if item.is_file():
                    candidates.append(item)
        return candidates

    def _find_qr_images_for_client(self, client_root: Path) -> list[Path]:
        """查找某个客户端目录及其相关子目录中的二维码。"""
        directories = [client_root]
        for subdir in self._get_qr_related_subdirs(client_root):
            if subdir not in directories:
                directories.append(subdir)

        candidates: list[Path] = []
        for directory in directories:
            candidates.extend(self._find_qr_images_in_dir(directory))
        return candidates

    def _pick_latest_file(self, candidates: list[Path]) -> Path | None:
        """返回候选文件中最新的一个。"""
        unique_candidates: list[Path] = []
        seen: set[Path] = set()
        for item in candidates:
            try:
                resolved = item.resolve()
            except FileNotFoundError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            unique_candidates.append(item)
        if not unique_candidates:
            return None
        return max(unique_candidates, key=lambda item: item.stat().st_mtime)

    def _build_onebot_client(self, directory: Path) -> OneBotClient | None:
        """把候选目录解析为可展示/可启动的客户端。"""
        lower_name = directory.name.lower()
        name = "OneBot"
        launch_command: list[str] = []
        launch_env: dict[str, str] | None = None

        lagrange_exec = next(
            (
                path
                for path in (
                    directory / "Lagrange.OneBot",
                    directory / "Lagrange.OneBot.exe",
                )
                if path.exists() and path.is_file()
            ),
            None,
        )
        napcat_exec = next(
            (
                path
                for path in (
                    directory / "napcat",
                    directory / "NapCat",
                    directory / "NapCat.Shell",
                    directory / "opt" / "QQ" / "qq",
                )
                if path.exists() and path.is_file()
            ),
            None,
        )

        if lagrange_exec is not None:
            name = "Lagrange.OneBot"
            launch_command = [str(lagrange_exec)]
        elif napcat_exec is not None and self._is_valid_napcat_dir(directory):
            name = "NapCat"
            launch_command = [str(napcat_exec)]
            if napcat_exec.name == "qq":
                runtime_root = self._prepare_managed_runtime_dir("napcat")
                launch_command = [
                    str(napcat_exec),
                    f"--user-data-dir={runtime_root / 'chromium'}",
                ]
                launch_env = self._build_managed_launch_env(runtime_root)
        elif "lagrange" in lower_name:
            name = "Lagrange.OneBot"
        elif self._is_valid_napcat_dir(directory):
            name = "NapCat"

        if not self._is_onebot_related_dir(directory):
            return None
        return OneBotClient(
            name=name,
            root=directory,
            launch_command=launch_command,
            launch_env=launch_env,
        )

    def _prepare_managed_runtime_dir(self, client_name: str) -> Path:
        """为项目内隔离运行准备数据目录。"""
        runtime_root = plugin_settings.managed_onebot_runtime_dir / client_name
        for subdir in ("config", "data", "cache", "chromium", "home"):
            (runtime_root / subdir).mkdir(parents=True, exist_ok=True)
        return runtime_root

    def _build_managed_launch_env(self, runtime_root: Path) -> dict[str, str]:
        """构建隔离运行环境变量，避免复用系统 QQ 数据。"""
        env = os.environ.copy()
        env["XDG_CONFIG_HOME"] = str(runtime_root / "config")
        env["XDG_DATA_HOME"] = str(runtime_root / "data")
        env["XDG_CACHE_HOME"] = str(runtime_root / "cache")
        env["HOME"] = str(runtime_root / "home")
        return env

    def _serialize_onebot_client(
        self,
        client: OneBotClient,
        *,
        selected_root: str = "",
    ) -> dict[str, str | bool]:
        """把客户端对象转成页面可直接渲染的结构。"""
        process = self._started_onebot_processes.get(str(client.root))
        running = process is not None and process.poll() is None
        latest_qr = self._pick_latest_file(self._find_qr_images_for_client(client.root))
        return {
            "name": client.name,
            "root": str(client.root),
            "launchable": client.launchable,
            "running": running,
            "has_qr_image": latest_qr is not None,
            "selected": str(client.root) == selected_root,
        }

    def _find_onebot_client(self, client_root: str, *, runtime_dir_text: str = "") -> OneBotClient | None:
        """按目录定位具体客户端。"""
        target = Path(client_root).expanduser()
        for client in self._discover_onebot_clients(runtime_dir_text):
            if client.root == target:
                return client
        return None

    @staticmethod
    def _resolve_selected_client(
        clients: list[dict[str, str | bool]],
        preferred_root: str,
    ) -> dict[str, str | bool] | None:
        """解析当前应当使用的客户端。"""
        if preferred_root:
            for client in clients:
                if str(client["root"]) == preferred_root:
                    return client
        for predicate in (
            lambda item: bool(item.get("running")),
            lambda item: bool(item.get("has_qr_image")),
            lambda item: bool(item.get("launchable")),
            lambda item: True,
        ):
            for client in clients:
                if predicate(client):
                    return client
        return None

    def _get_cache(self, key: str, ttl_seconds: float = 5.0) -> Any | None:
        """读取短期缓存，减少页面刷新时重复扫描磁盘。"""
        cached = self._scan_cache.get(key)
        if cached is None:
            return None
        cached_at, value = cached
        if time.monotonic() - cached_at > ttl_seconds:
            self._scan_cache.pop(key, None)
            return None
        return value

    def _set_cache(self, key: str, value: Any) -> None:
        """写入短期缓存。"""
        self._scan_cache[key] = (time.monotonic(), value)

    @staticmethod
    def _parse_csv_int_set(raw_text: str) -> set[int]:
        """把网页表单中的逗号分隔数字解析成整数集合。"""
        result: set[int] = set()
        for item in raw_text.replace(" ", ",").split(","):
            text = item.strip()
            if text.isdigit():
                result.add(int(text))
        return result

    @staticmethod
    def _safe_int(raw_text: str, default: int) -> int:
        """安全转换整数字符串，失败时回退默认值。"""
        try:
            return int(str(raw_text).strip())
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _format_bytes(num_bytes: float) -> str:
        """把字节数格式化为易读文本。"""
        units = ["B", "KB", "MB", "GB", "TB"]
        value = float(num_bytes)
        for unit in units:
            if value < 1024 or unit == units[-1]:
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} TB"

    @staticmethod
    def _resource_tone(percent: float) -> str:
        """按占用率返回颜色等级。"""
        if percent >= 85:
            return "danger"
        if percent >= 65:
            return "warn"
        return "good"


verify_service = VerifyService()
