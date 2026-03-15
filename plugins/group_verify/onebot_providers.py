"""
OneBot provider 注册表。

把不同客户端的目录识别和启动逻辑分开，避免继续在运行时管理器里堆条件分支。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class OneBotClient:
    """本机扫描到的 OneBot 客户端信息。"""

    provider: str
    name: str
    root: Path
    launch_command: list[str]
    launch_env: dict[str, str] | None = None

    @property
    def launchable(self) -> bool:
        return bool(self.launch_command)


class BaseOneBotProvider:
    """OneBot provider 基类。"""

    key = "external"
    display_name = "External OneBot"

    def __init__(self, settings: Any) -> None:
        self._settings = settings

    def matches_directory(self, directory: Path) -> bool:
        lower_name = directory.name.lower()
        if "nonebot" in lower_name:
            return False
        return (
            "onebot" in lower_name
            or (directory / "config" / "onebot11_qq.json").exists()
            or (directory / "QQ").exists()
        )

    def build_client(self, directory: Path) -> OneBotClient | None:
        if not self.matches_directory(directory):
            return None
        return OneBotClient(
            provider=self.key,
            name=self.display_name,
            root=directory,
            launch_command=[],
        )


class LagrangeProvider(BaseOneBotProvider):
    key = "lagrange"
    display_name = "Lagrange.OneBot"

    def matches_directory(self, directory: Path) -> bool:
        lower_name = directory.name.lower()
        return (
            "lagrange" in lower_name
            or (directory / "Lagrange.OneBot").exists()
            or (directory / "Lagrange.OneBot.exe").exists()
        )

    def build_client(self, directory: Path) -> OneBotClient | None:
        if not self.matches_directory(directory):
            return None

        exec_path = next(
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
        return OneBotClient(
            provider=self.key,
            name=self.display_name,
            root=directory,
            launch_command=[str(exec_path)] if exec_path is not None else [],
        )


class NapCatProvider(BaseOneBotProvider):
    key = "napcat"
    display_name = "NapCat"

    def matches_directory(self, directory: Path) -> bool:
        lower_name = directory.name.lower()
        return self._is_valid_napcat_dir(directory) or "napcat" in lower_name

    def build_client(self, directory: Path) -> OneBotClient | None:
        if not self.matches_directory(directory):
            return None

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

        launch_command: list[str] = []
        launch_env: dict[str, str] | None = None
        if napcat_exec is not None and self._is_valid_napcat_dir(directory):
            launch_command = [str(napcat_exec)]
            if napcat_exec.name == "qq":
                runtime_root = self._prepare_managed_runtime_dir()
                launch_command = [
                    str(napcat_exec),
                    f"--user-data-dir={runtime_root / 'chromium'}",
                ]
                launch_env = self._build_managed_launch_env(runtime_root)

        return OneBotClient(
            provider=self.key,
            name=self.display_name,
            root=directory,
            launch_command=launch_command,
            launch_env=launch_env,
        )

    @staticmethod
    def _is_valid_napcat_dir(directory: Path) -> bool:
        markers = (
            directory / "config" / "onebot11_qq.json",
            directory / "opt" / "QQ" / "resources" / "app" / "loadNapCat.js",
            directory / "opt" / "QQ" / "resources" / "app" / "app_launcher" / "napcat" / "napcat.mjs",
            directory / "QQ" / "resources" / "app" / "loadNapCat.js",
            directory / "QQ" / "resources" / "app" / "app_launcher" / "napcat" / "napcat.mjs",
        )
        return any(path.exists() for path in markers)

    def _prepare_managed_runtime_dir(self) -> Path:
        runtime_root = self._settings.managed_onebot_runtime_dir / self.key
        for subdir in ("config", "data", "cache", "chromium", "home"):
            (runtime_root / subdir).mkdir(parents=True, exist_ok=True)
        return runtime_root

    @staticmethod
    def _build_managed_launch_env(runtime_root: Path) -> dict[str, str]:
        env = os.environ.copy()
        env["XDG_CONFIG_HOME"] = str(runtime_root / "config")
        env["XDG_DATA_HOME"] = str(runtime_root / "data")
        env["XDG_CACHE_HOME"] = str(runtime_root / "cache")
        env["HOME"] = str(runtime_root / "home")
        return env


def build_provider_registry(settings: Any) -> dict[str, BaseOneBotProvider]:
    """创建 provider 注册表。"""
    providers = [
        NapCatProvider(settings),
        LagrangeProvider(settings),
        BaseOneBotProvider(settings),
    ]
    return {provider.key: provider for provider in providers}
