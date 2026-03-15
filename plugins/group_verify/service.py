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
import json
import os
import random
import shutil
import smtplib
import subprocess
from datetime import datetime, timedelta
from email.message import EmailMessage
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
from .onebot_runtime import OneBotRuntimeManager
from .verify_templates import VerifyTemplateManager, VerifyTemplateProfile
from project_config import export_env_file, load_project_config, save_project_config


# HTML 模板文件路径。Playwright 会读取模板内容并渲染成 PNG 图片。
SERVICE_STATUS_TEMPLATE_PATH = Path(__file__).parent / "templates" / "service_status.html"


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
        self._onebot_runtime = OneBotRuntimeManager(
            plugin_settings,
            self._started_onebot_processes,
            self._scan_cache,
        )
        self._template_manager = VerifyTemplateManager(Path(__file__).parent, plugin_settings.data_dir)
        self._default_app_config: dict[str, str] = {
            "target_groups": ",".join(str(group_id) for group_id in sorted(plugin_settings.target_groups)),
            "superusers": ",".join(str(user_id) for user_id in sorted(plugin_settings.superusers)),
            "timeout_minutes": str(plugin_settings.default_timeout_minutes),
            "max_error_times": str(plugin_settings.default_max_error_times),
            "playwright_browser": plugin_settings.playwright_browser,
            "image_retry_times": str(plugin_settings.image_retry_times),
            "lagrange_qr_dir": str(plugin_settings.lagrange_qr_dir) if plugin_settings.lagrange_qr_dir else "",
            "onebot_provider": plugin_settings.onebot_provider,
            "preferred_onebot_client": "",
            "verify_template_preset": "classic",
            "verify_message_template": (
                "欢迎入群，{{user_name}}。\n"
                "请在 {{timeout_minutes}} 分钟内发送图片中的 4 位验证码完成验证。\n"
                "超时或累计输错 {{max_error_times}} 次将被移出群聊。"
            ),
            "admin_command_aliases": json.dumps(["入群验证"], ensure_ascii=False),
            "admin_help_template": (
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
                "也支持直接发送带前缀命令\n"
                "入群验证 服务状态 / 入群验证 验证记录 / 入群验证 列表\n"
                "入群验证 状态 / 开启 / 关闭 / 设置超时 / 设置次数\n"
                "群聊里可省略群号，例如：入群验证 状态、入群验证 开启、入群验证 设置超时 8、入群验证 设置次数 5\n"
                "验证记录默认 10 条，可写：@机器人 验证记录 15 或 入群验证 验证记录 15"
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
        previous_runtime_settings = await self.get_runtime_settings()
        async with AsyncSessionLocal() as session:
            for config_key, config_value in config_map.items():
                if config_key == "playwright_browser":
                    config_value = self._normalize_playwright_browser(config_value)
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
        updated_runtime_settings = await self.get_runtime_settings()
        removed_target_groups = previous_runtime_settings["target_groups"] - updated_runtime_settings["target_groups"]
        if removed_target_groups:
            await self._cancel_pending_records_for_groups(
                removed_target_groups,
                reason="目标群配置已移除，取消仍在等待中的验证任务。",
            )

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
            "playwright_browser": self._normalize_playwright_browser(
                config_map.get("playwright_browser", plugin_settings.playwright_browser)
                or plugin_settings.playwright_browser
            ),
            "image_retry_times": self._safe_int(
                config_map.get("image_retry_times", ""),
                plugin_settings.image_retry_times,
            ),
            "lagrange_qr_dir": config_map.get("lagrange_qr_dir", "").strip(),
            "onebot_provider": config_map.get("onebot_provider", plugin_settings.onebot_provider).strip()
            or plugin_settings.onebot_provider,
            "preferred_onebot_client": config_map.get("preferred_onebot_client", "").strip(),
            "verify_template_preset": self._template_manager.normalize_key(
                config_map.get("verify_template_preset", "classic")
            ),
            "verify_message_template": config_map.get(
                "verify_message_template",
                self._default_app_config["verify_message_template"],
            ),
            "admin_command_aliases": self._parse_text_list(
                config_map.get("admin_command_aliases", self._default_app_config["admin_command_aliases"])
            ),
            "admin_help_template": config_map.get(
                "admin_help_template",
                self._default_app_config["admin_help_template"],
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

    async def get_admin_command_aliases(self) -> list[str]:
        """返回管理员命令前缀别名。"""
        runtime_settings = await self.get_runtime_settings()
        aliases = [str(item).strip() for item in runtime_settings["admin_command_aliases"] if str(item).strip()]
        return aliases or ["入群验证"]

    async def save_admin_command_aliases(self, raw_text: str) -> tuple[bool, str]:
        """保存管理员命令前缀别名。"""
        aliases = self._parse_text_list(raw_text)
        if not aliases:
            return False, "至少要保留一个管理员命令前缀别名。"
        await self.update_app_configs(
            {"admin_command_aliases": json.dumps(aliases, ensure_ascii=False)}
        )
        return True, "管理员命令别名已保存。"

    async def get_admin_help_template(self) -> str:
        """返回管理员帮助文案模板。"""
        runtime_settings = await self.get_runtime_settings()
        return str(runtime_settings["admin_help_template"]).strip() or str(
            self._default_app_config["admin_help_template"]
        )

    async def save_admin_help_template(self, raw_text: str) -> tuple[bool, str]:
        """保存管理员帮助文案模板。"""
        normalized = raw_text.replace("\r\n", "\n").strip()
        if not normalized:
            return False, "管理员帮助模板不能为空。"
        if len(normalized) > 3000:
            return False, "管理员帮助模板过长，请控制在 3000 个字符以内。"
        await self.update_app_configs({"admin_help_template": normalized})
        return True, "管理员帮助模板已保存。"

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
        return self._template_manager.list_templates(runtime_settings["verify_template_preset"])

    async def get_active_verify_template_profile(self) -> VerifyTemplateProfile:
        """返回当前生效模板的完整信息。"""
        runtime_settings = await self.get_runtime_settings()
        return self._template_manager.get_active_template_profile(runtime_settings["verify_template_preset"])

    async def save_verify_template_html(self, template_html: str) -> tuple[bool, str]:
        """保存自定义验证码模板。"""
        success, template_key, message = self._template_manager.save_template_version(
            template_html=template_html,
            template_name="自定义模板",
            based_on=(await self.get_runtime_settings())["verify_template_preset"],
        )
        if not success:
            return False, message
        await self.update_app_configs({"verify_template_preset": template_key})
        return True, message

    async def reset_verify_template_html(self) -> None:
        """恢复默认验证码模板。"""
        await self.update_app_configs({"verify_template_preset": "preset:classic"})

    async def activate_verify_template_preset(self, preset_key: str) -> tuple[bool, str]:
        """切换当前使用的验证码模板预设。"""
        normalized_key = self._template_manager.normalize_key(preset_key)
        success, message = self._template_manager.activate_template(normalized_key)
        if not success:
            return False, message
        await self.update_app_configs({"verify_template_preset": normalized_key})
        return True, message

    async def create_verify_template_version(
        self,
        *,
        template_name: str,
        template_html: str,
        based_on: str,
    ) -> tuple[bool, str]:
        """保存新的模板库版本并立即切换。"""
        success, template_key, message = self._template_manager.save_template_version(
            template_html=template_html,
            template_name=template_name,
            based_on=based_on,
        )
        if not success:
            return False, message
        await self.update_app_configs({"verify_template_preset": template_key})
        return True, message

    async def delete_verify_template_version(self, template_key: str) -> tuple[bool, str]:
        """删除指定模板库版本。"""
        runtime_settings = await self.get_runtime_settings()
        success, message = self._template_manager.delete_template(template_key)
        if not success:
            return False, message
        if self._template_manager.normalize_key(runtime_settings["verify_template_preset"]) == self._template_manager.normalize_key(template_key):
            await self.update_app_configs({"verify_template_preset": "preset:classic"})
        return True, message

    async def get_project_notification_settings(self) -> dict[str, Any]:
        """读取项目级 SMTP 与代理配置。"""
        project_config = load_project_config(plugin_settings.project_root)
        smtp_config = project_config.get("smtp", {})
        proxy_config = project_config.get("proxy", {})
        return {
            "smtp": {
                "host": str(smtp_config.get("host", "")).strip(),
                "port": int(smtp_config.get("port", 465) or 465),
                "username": str(smtp_config.get("username", "")).strip(),
                "password": str(smtp_config.get("password", "")).strip(),
                "from_email": str(smtp_config.get("from_email", "")).strip(),
                "to_email": str(smtp_config.get("to_email", "")).strip(),
                "use_tls": bool(smtp_config.get("use_tls", False)),
                "use_ssl": bool(smtp_config.get("use_ssl", True)),
            },
            "proxy": {
                "http_proxy": str(proxy_config.get("http_proxy", "")).strip(),
                "https_proxy": str(proxy_config.get("https_proxy", "")).strip(),
                "all_proxy": str(proxy_config.get("all_proxy", "")).strip(),
                "no_proxy": str(proxy_config.get("no_proxy", "")).strip(),
            },
        }

    async def save_project_notification_settings(
        self,
        *,
        smtp_settings: dict[str, Any],
        proxy_settings: dict[str, Any],
    ) -> tuple[bool, str]:
        """保存项目级 SMTP 与代理配置。"""
        try:
            smtp_port = int(str(smtp_settings.get("port", "465")).strip() or 465)
        except ValueError:
            return False, "SMTP 端口必须是整数。"
        if smtp_port < 1 or smtp_port > 65535:
            return False, "SMTP 端口必须在 1 到 65535 之间。"
        if bool(smtp_settings.get("use_tls", False)) and bool(smtp_settings.get("use_ssl", True)):
            return False, "SMTP 不能同时开启 SSL 和 STARTTLS。"
        project_config = load_project_config(plugin_settings.project_root)
        project_config["smtp"] = {
            "host": str(smtp_settings.get("host", "")).strip(),
            "port": smtp_port,
            "username": str(smtp_settings.get("username", "")).strip(),
            "password": str(smtp_settings.get("password", "")).strip(),
            "from_email": str(smtp_settings.get("from_email", "")).strip(),
            "to_email": str(smtp_settings.get("to_email", "")).strip(),
            "use_tls": bool(smtp_settings.get("use_tls", False)),
            "use_ssl": bool(smtp_settings.get("use_ssl", True)),
        }
        project_config["proxy"] = {
            "http_proxy": str(proxy_settings.get("http_proxy", "")).strip(),
            "https_proxy": str(proxy_settings.get("https_proxy", "")).strip(),
            "all_proxy": str(proxy_settings.get("all_proxy", "")).strip(),
            "no_proxy": str(proxy_settings.get("no_proxy", "")).strip(),
        }
        save_project_config(plugin_settings.project_root, project_config)
        export_env_file(plugin_settings.project_root)
        return True, "SMTP 与统一代理配置已保存，并已同步到 .env。"

    async def send_test_email(
        self,
        *,
        to_email: str,
        subject: str,
        content: str,
    ) -> tuple[bool, str]:
        """按项目 SMTP 配置发送测试邮件。"""
        project_settings = await self.get_project_notification_settings()
        smtp_settings = project_settings["smtp"]
        host = str(smtp_settings["host"]).strip()
        if not host:
            return False, "SMTP 主机未配置。"
        recipient = to_email.strip() or str(smtp_settings["to_email"]).strip()
        if not recipient:
            return False, "请先配置默认收件人，或填写测试收件邮箱。"
        sender = str(smtp_settings["from_email"]).strip() or str(smtp_settings["username"]).strip()
        if not sender:
            return False, "SMTP 发件人为空，请至少配置 from_email 或 username。"

        message = EmailMessage()
        message["From"] = sender
        message["To"] = recipient
        message["Subject"] = subject.strip() or "入群验证机器人 SMTP 测试邮件"
        message.set_content(content.strip() or "这是一封来自入群验证机器人管理台的测试邮件。", subtype="plain")

        def _send() -> None:
            port = int(smtp_settings["port"])
            username = str(smtp_settings["username"]).strip()
            password = str(smtp_settings["password"]).strip()
            use_ssl = bool(smtp_settings["use_ssl"])
            use_tls = bool(smtp_settings["use_tls"])
            if use_ssl:
                server: smtplib.SMTP | smtplib.SMTP_SSL = smtplib.SMTP_SSL(host, port, timeout=20)
            else:
                server = smtplib.SMTP(host, port, timeout=20)
            try:
                server.ehlo()
                if use_tls:
                    server.starttls()
                    server.ehlo()
                if username:
                    server.login(username, password)
                server.send_message(message)
            finally:
                try:
                    server.quit()
                except Exception:
                    server.close()

        try:
            await asyncio.to_thread(_send)
        except Exception as exc:
            logger.warning(f"SMTP 测试邮件发送失败 error={exc}")
            return False, f"测试邮件发送失败：{exc}"
        return True, f"测试邮件已发送到 {recipient}。"

    async def reset_setup_state(self) -> None:
        """清空初始化向导相关状态，便于重新走首次流程。"""
        await self.update_app_configs(
            {
                "target_groups": "",
                "superusers": "",
                "preferred_onebot_client": "",
            }
        )
        runtime_root = plugin_settings.managed_onebot_runtime_dir
        for child_name in ("napcat", "lagrange"):
            child = runtime_root / child_name
            if child.exists():
                shutil.rmtree(child, ignore_errors=True)
                child.mkdir(parents=True, exist_ok=True)

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
        client = self._onebot_runtime.find_onebot_client(
            client_root,
            runtime_settings=runtime_settings,
        )
        if client is None:
            return False
        await self.update_app_configs({"preferred_onebot_client": str(client.root)})
        self._onebot_runtime.clear_cache()
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
    def _validate_verify_message_template(template_text: str) -> tuple[bool, str]:
        """校验入群提示消息模板。"""
        if not template_text:
            return False, "消息模板不能为空。"
        if len(template_text) > 600:
            return False, "消息模板过长，请控制在 600 个字符以内。"
        return True, ""

    @staticmethod
    def _normalize_playwright_browser(raw_value: str) -> str:
        """当前运行脚本只安装 Chromium，因此统一收敛到 chromium。"""
        browser_name = str(raw_value).strip().lower() or "chromium"
        if browser_name != "chromium":
            return "chromium"
        return browser_name

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
            if not enabled:
                await self._cancel_pending_records_for_groups(
                    {group_id},
                    reason="该群已关闭入群验证，取消仍在等待中的验证任务。",
                )
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

    async def get_bot_group_overview(self) -> list[dict[str, Any]]:
        """读取机器人当前所在群列表，并补充管理员状态。"""
        bot = self._get_available_bot()
        if bot is None:
            return []

        runtime_settings = await self.get_runtime_settings()
        selected_groups = set(runtime_settings["target_groups"])
        records: list[dict[str, Any]] = []
        try:
            group_list = await bot.get_group_list()
        except Exception as exc:
            logger.warning(f"获取机器人群列表失败 error={exc}")
            return []

        for item in group_list:
            group_id = int(item.get("group_id", 0))
            if group_id <= 0:
                continue
            group_name = str(item.get("group_name", "")).strip() or str(group_id)
            role = "unknown"
            try:
                member_info = await bot.get_group_member_info(
                    group_id=group_id,
                    user_id=int(bot.self_id),
                    no_cache=True,
                )
                role = str(member_info.get("role", "unknown")).strip().lower() or "unknown"
            except Exception as exc:
                logger.warning(f"获取机器人群成员状态失败 group_id={group_id} error={exc}")
            records.append(
                {
                    "group_id": group_id,
                    "group_name": group_name,
                    "selected": group_id in selected_groups,
                    "is_admin": role in {"admin", "owner"},
                    "role": role,
                }
            )
        return sorted(records, key=lambda item: (not bool(item["selected"]), str(item["group_name"])))

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
            record: VerifyRecord | None = None
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
            except Exception as exc:
                if record is not None:
                    await self._cancel_pending_record(
                        group_id=group_id,
                        user_id=user_id,
                        reason="入群验证准备阶段失败，已取消本轮待验证记录。",
                    )
                logger.exception(
                    f"处理入群验证准备阶段失败 group_id={group_id} user_id={user_id} error={exc}"
                )
                return

            try:
                await bot.send_group_msg(group_id=group_id, message=message)
            except Exception as exc:
                await self._cancel_pending_record(
                    group_id=group_id,
                    user_id=user_id,
                    reason="验证码消息发送失败，已取消本轮待验证记录。",
                )
                logger.exception(
                    f"发送入群验证消息失败 group_id={group_id} user_id={user_id} error={exc}"
                )
                return

            self._create_timeout_task(group_id, user_id, record.id, expire_time)

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
            if not await self._is_group_verification_active(record.group_id):
                await self._cancel_pending_record(
                    group_id=record.group_id,
                    user_id=record.user_id,
                    reason="群验证配置已关闭，跳过恢复旧的待验证任务。",
                )
                continue
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

    async def _cancel_pending_record(self, *, group_id: int, user_id: int, reason: str = "") -> None:
        """取消某个用户当前仍待处理的验证。"""
        key = (group_id, user_id)
        self._cancel_timeout_task(key)
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(VerifyRecord).where(
                    VerifyRecord.group_id == group_id,
                    VerifyRecord.user_id == user_id,
                    VerifyRecord.status == VerifyStatus.PENDING,
                )
            )
            record = result.scalar_one_or_none()
            if record is None:
                return
            record.status = VerifyStatus.CANCELLED
            record.updated_at = datetime.now()
            await session.commit()
        if reason:
            logger.info(f"{reason} group_id={group_id} user_id={user_id}")

    async def _cancel_pending_records_for_groups(self, group_ids: set[int], *, reason: str = "") -> None:
        """批量取消指定群里仍待处理的验证，并清理内存中的超时任务。"""
        if not group_ids:
            return
        pending_keys = [key for key in self._tasks if key[0] in group_ids]
        for key in pending_keys:
            self._cancel_timeout_task(key)

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(VerifyRecord).where(
                    VerifyRecord.group_id.in_(group_ids),
                    VerifyRecord.status == VerifyStatus.PENDING,
                )
            )
            records = result.scalars().all()
            for record in records:
                record.status = VerifyStatus.CANCELLED
                record.updated_at = datetime.now()
            await session.commit()

        if reason and records:
            logger.info(f"{reason} groups={sorted(group_ids)} affected={len(records)}")

    async def _is_group_verification_active(self, group_id: int) -> bool:
        """确认某个群当前仍受入群验证管理。"""
        target_groups = await self.get_target_groups()
        if group_id not in target_groups:
            return False
        group_config = await self.get_group_config(group_id)
        return bool(group_config is not None and group_config.enabled)

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
        if task is not None and not task.done() and task is not asyncio.current_task():
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

                if not await self._is_group_verification_active(group_id):
                    await self._cancel_pending_record(
                        group_id=group_id,
                        user_id=user_id,
                        reason="超时任务执行前检测到群验证已关闭，忽略旧任务。",
                    )
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
        selected_client = self._onebot_runtime.resolve_selected_client(
            clients, runtime_settings["preferred_onebot_client"]
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
        return await self._onebot_runtime.get_latest_qr_image(
            runtime_settings,
            selected_client_root=selected_client_root,
        )

    async def get_detected_onebot_clients(self) -> list[dict[str, str | bool]]:
        """扫描本机可能的 OneBot 客户端目录。"""
        runtime_settings = await self.get_runtime_settings()
        return await self._onebot_runtime.get_detected_onebot_clients(runtime_settings)

    async def get_primary_onebot_client(self) -> dict[str, str | bool] | None:
        """返回当前最适合展示和启动的客户端。"""
        runtime_settings = await self.get_runtime_settings()
        return await self._onebot_runtime.get_primary_onebot_client(runtime_settings)

    async def launch_detected_onebot(self, client_root: str) -> tuple[bool, str]:
        """启动用户明确选择的 OneBot 客户端。"""
        runtime_settings = await self.get_runtime_settings()
        return await self._onebot_runtime.launch_detected_onebot(
            client_root,
            runtime_settings,
            lambda preferred_root: self.update_app_configs(
                {"preferred_onebot_client": preferred_root}
            ),
        )

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
    def _parse_text_list(raw_value: str | list[str]) -> list[str]:
        """把 JSON 数组或逗号换行文本解析成去重字符串列表。"""
        if isinstance(raw_value, list):
            items = raw_value
        else:
            text = str(raw_value).strip()
            if not text:
                return []
            try:
                loaded = json.loads(text)
            except json.JSONDecodeError:
                normalized_text = text.replace("，", ",").replace("\n", ",")
                items = [item for item in normalized_text.split(",") if item.strip()]
            else:
                if isinstance(loaded, list):
                    items = loaded
                else:
                    items = [str(loaded)]
        result: list[str] = []
        for item in items:
            text = str(item).strip()
            if text and text not in result:
                result.append(text)
        return result

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
