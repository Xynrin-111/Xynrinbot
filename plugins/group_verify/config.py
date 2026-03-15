"""
插件配置解析。

这里统一处理 .env 中的插件相关配置，避免在业务代码里直接读取环境变量。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from nonebot import get_driver


def _parse_int_set(raw_value: object) -> set[int]:
    """把 .env 中的群号 / QQ 配置解析成整数集合。"""
    if raw_value is None:
        return set()
    if isinstance(raw_value, (set, list, tuple)):
        return {int(item) for item in raw_value}

    text = str(raw_value).strip()
    if not text:
        return set()

    # 兼容 NoneBot 常见的 ["1","2"] / 1,2 / 1 2 这几种写法。
    text = text.strip("[]")
    text = text.replace('"', "").replace("'", "")
    items = [item.strip() for item in text.replace(" ", ",").split(",") if item.strip()]
    return {int(item) for item in items}


@dataclass(slots=True)
class PluginSettings:
    """插件运行时配置。"""

    database_url: str
    project_root: Path
    deploy_profile: str
    platform_name: str
    onebot_provider: str
    target_groups: set[int]
    superusers: set[int]
    default_timeout_minutes: int
    default_max_error_times: int
    playwright_browser: str
    image_retry_times: int
    data_dir: Path
    managed_onebot_dir: Path
    managed_onebot_runtime_dir: Path
    admin_host: str
    admin_port: int
    admin_path: str
    admin_username: str
    admin_password: str
    auto_open_admin: bool
    admin_local_only: bool
    lagrange_qr_dir: Path | None

    @classmethod
    def from_driver(cls) -> "PluginSettings":
        """从 NoneBot 全局配置读取插件配置。"""
        config = get_driver().config
        project_root = Path(__file__).resolve().parents[2]
        data_dir = project_root / "data" / "group_verify"
        managed_onebot_dir = project_root / "third_party" / "onebot"
        managed_onebot_runtime_dir = managed_onebot_dir / "runtime"
        data_dir.mkdir(parents=True, exist_ok=True)
        managed_onebot_dir.mkdir(parents=True, exist_ok=True)
        managed_onebot_runtime_dir.mkdir(parents=True, exist_ok=True)
        (managed_onebot_dir / "napcat").mkdir(parents=True, exist_ok=True)
        (managed_onebot_dir / "lagrange").mkdir(parents=True, exist_ok=True)
        (managed_onebot_runtime_dir / "napcat").mkdir(parents=True, exist_ok=True)
        (managed_onebot_runtime_dir / "lagrange").mkdir(parents=True, exist_ok=True)

        deploy_profile = str(getattr(config, "app_deploy_profile", "desktop")).strip().lower() or "desktop"
        if deploy_profile not in {"desktop", "server"}:
            deploy_profile = "desktop"
        platform_name = str(getattr(config, "app_platform", "auto")).strip().lower() or "auto"
        onebot_provider = str(getattr(config, "verify_onebot_provider", "external")).strip().lower() or "external"
        superusers = _parse_int_set(getattr(config, "superusers", set()))
        target_groups = _parse_int_set(getattr(config, "verify_target_groups", ""))
        timeout_minutes = int(getattr(config, "verify_timeout_minutes", 5))
        max_error_times = int(getattr(config, "verify_max_error_times", 3))
        playwright_browser = str(getattr(config, "verify_playwright_browser", "chromium"))
        image_retry_times = int(getattr(config, "verify_image_retry_times", 2))
        admin_host = str(getattr(config, "host", "127.0.0.1"))
        admin_port = int(getattr(config, "port", 8080))
        admin_path = str(getattr(config, "verify_admin_path", "/admin")).strip() or "/admin"
        admin_username = str(getattr(config, "verify_admin_username", "admin")).strip() or "admin"
        admin_password = str(getattr(config, "verify_admin_password", "")).strip()
        auto_open_admin = str(getattr(config, "verify_auto_open_admin", "false")).lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        admin_local_only = str(getattr(config, "verify_admin_local_only", "true")).lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        lagrange_qr_dir_raw = str(getattr(config, "verify_lagrange_qr_dir", "")).strip()
        lagrange_qr_dir = Path(lagrange_qr_dir_raw).expanduser() if lagrange_qr_dir_raw else None

        return cls(
            database_url=f"sqlite+aiosqlite:///{(data_dir / 'group_verify.db').as_posix()}",
            project_root=project_root,
            deploy_profile=deploy_profile,
            platform_name=platform_name,
            onebot_provider=onebot_provider,
            target_groups=target_groups,
            superusers=superusers,
            default_timeout_minutes=timeout_minutes,
            default_max_error_times=max_error_times,
            playwright_browser=playwright_browser,
            image_retry_times=image_retry_times,
            data_dir=data_dir,
            managed_onebot_dir=managed_onebot_dir,
            managed_onebot_runtime_dir=managed_onebot_runtime_dir,
            admin_host=admin_host,
            admin_port=admin_port,
            admin_path=admin_path if admin_path.startswith("/") else f"/{admin_path}",
            admin_username=admin_username,
            admin_password=admin_password,
            auto_open_admin=auto_open_admin,
            admin_local_only=admin_local_only,
            lagrange_qr_dir=lagrange_qr_dir,
        )


plugin_settings = PluginSettings.from_driver()
