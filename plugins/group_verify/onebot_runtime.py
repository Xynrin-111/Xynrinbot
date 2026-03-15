"""
OneBot 客户端发现、二维码扫描与隔离启动。

这部分和入群验证核心流程无关，单独拆出来，避免 service.py 继续膨胀。
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from nonebot import logger

from .onebot_providers import OneBotClient, build_provider_registry


QR_FILE_PATTERNS = ("qr-*.png", "qrcode*.png", "*qr*.png")


class OneBotRuntimeManager:
    """统一处理 OneBot 客户端扫描、二维码发现和隔离启动。"""

    def __init__(
        self,
        settings: Any,
        started_processes: dict[str, subprocess.Popen[Any]],
        scan_cache: dict[str, tuple[float, Any]],
    ) -> None:
        self._settings = settings
        self._started_processes = started_processes
        self._scan_cache = scan_cache
        self._providers = build_provider_registry(settings)

    def clear_cache(self) -> None:
        self._scan_cache.clear()

    async def get_latest_qr_image(
        self,
        runtime_settings: dict[str, Any],
        *,
        selected_client_root: str | None = None,
    ) -> Path | None:
        """自动查找最新二维码图片，优先使用当前选中的客户端。"""
        cache_key = (
            f"latest_qr_image:{selected_client_root or ''}:"
            f"{runtime_settings['lagrange_qr_dir']}:{runtime_settings['onebot_provider']}"
        )
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached

        candidates: list[Path] = []
        if selected_client_root:
            client = self.find_onebot_client(
                selected_client_root,
                runtime_settings=runtime_settings,
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

    async def get_detected_onebot_clients(
        self,
        runtime_settings: dict[str, Any],
    ) -> list[dict[str, str | bool]]:
        """扫描本机可能的 OneBot 客户端目录。"""
        cache_key = f"onebot_clients:{runtime_settings['lagrange_qr_dir']}:{runtime_settings['onebot_provider']}"
        cached = self._get_cache(cache_key)
        if cached is None:
            selected_root = runtime_settings["preferred_onebot_client"]
            cached = [
                self._serialize_onebot_client(item, selected_root=selected_root)
                for item in self._discover_onebot_clients(runtime_settings)
            ]
            self._set_cache(cache_key, cached)
        return cached

    async def get_primary_onebot_client(
        self,
        runtime_settings: dict[str, Any],
    ) -> dict[str, str | bool] | None:
        """返回当前最适合展示和启动的客户端。"""
        clients = await self.get_detected_onebot_clients(runtime_settings)
        return self.resolve_selected_client(
            clients,
            runtime_settings["preferred_onebot_client"],
        )

    async def launch_detected_onebot(
        self,
        client_root: str,
        runtime_settings: dict[str, Any],
        persist_preferred_client: Callable[[str], Awaitable[None]],
    ) -> tuple[bool, str]:
        """启动用户明确选择的 OneBot 客户端。"""
        client = self.find_onebot_client(client_root, runtime_settings=runtime_settings)
        if client is None:
            return False, "未找到你选择的 OneBot 客户端，请先刷新页面后重新选择。"
        if not client.launchable:
            return False, "这个客户端目录只能检测，不能安全自动启动，请手动启动它。"

        process_key = str(client.root)
        process = self._started_processes.get(process_key)
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

        self._started_processes[process_key] = process
        await persist_preferred_client(str(client.root))
        self.clear_cache()
        return True, f"已尝试启动 {client.name}，请等待二维码生成。"

    def find_onebot_client(
        self,
        client_root: str,
        *,
        runtime_settings: dict[str, Any],
    ) -> OneBotClient | None:
        """按目录定位具体客户端。"""
        target = Path(client_root).expanduser()
        for client in self._discover_onebot_clients(runtime_settings):
            if client.root == target:
                return client
        return None

    @staticmethod
    def resolve_selected_client(
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

    def _discover_onebot_clients(self, runtime_settings: dict[str, Any]) -> list[OneBotClient]:
        """扫描可能存在的 OneBot 客户端。"""
        clients: list[OneBotClient] = []
        provider_keys = self._resolve_provider_keys(runtime_settings["onebot_provider"])
        seen_roots: set[Path] = set()
        for directory in self._collect_candidate_dirs(runtime_settings["lagrange_qr_dir"], provider_keys):
            if directory in seen_roots:
                continue
            seen_roots.add(directory)
            client = self._build_onebot_client(directory, provider_keys)
            if client is not None:
                clients.append(client)
        clients.sort(key=lambda item: (not item.launchable, item.name.lower(), str(item.root)))
        return clients

    def _collect_candidate_dirs(
        self,
        runtime_dir_text: str = "",
        provider_keys: list[str] | None = None,
    ) -> list[Path]:
        """优先从项目内独立目录和显式配置目录里筛选候选目录。"""
        roots: list[Path] = []
        if runtime_dir_text:
            roots.append(Path(runtime_dir_text).expanduser())
        elif self._settings.lagrange_qr_dir is not None:
            roots.append(self._settings.lagrange_qr_dir.expanduser())
        roots.extend(self._get_project_candidate_roots())

        candidate_dirs: list[Path] = []
        seen: set[Path] = set()
        for root in roots:
            if not root.exists() or not root.is_dir():
                continue
            for directory in self._walk_candidate_dirs(root, provider_keys or ["external"]):
                if directory not in seen:
                    seen.add(directory)
                    candidate_dirs.append(directory)
        return candidate_dirs

    def _walk_candidate_dirs(
        self,
        root: Path,
        provider_keys: list[str],
        max_depth: int = 4,
    ) -> list[Path]:
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
            ".runtime",
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
            if self._is_onebot_related_dir(current_path, provider_keys):
                directories.append(current_path)
        return directories

    def _get_project_candidate_roots(self) -> list[Path]:
        """项目内独立运行目录，避免误扫系统 QQ。"""
        return [
            self._settings.managed_onebot_dir,
            self._settings.managed_onebot_dir / "napcat",
            self._settings.managed_onebot_dir / "lagrange",
            self._settings.managed_onebot_runtime_dir,
            self._settings.managed_onebot_runtime_dir / "napcat",
            self._settings.managed_onebot_runtime_dir / "lagrange",
            self._settings.project_root / "third_party",
            self._settings.project_root / "data" / "group_verify",
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

    def _is_onebot_related_dir(self, directory: Path, provider_keys: list[str]) -> bool:
        """判断目录是否像是已启用 provider 的运行目录。"""
        return any(self._providers[key].matches_directory(directory) for key in provider_keys)

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

    def _build_onebot_client(self, directory: Path, provider_keys: list[str]) -> OneBotClient | None:
        """把候选目录按 provider 注册表解析为可展示/可启动的客户端。"""
        for key in provider_keys:
            client = self._providers[key].build_client(directory)
            if client is not None:
                return client
        return None

    def _serialize_onebot_client(
        self,
        client: OneBotClient,
        *,
        selected_root: str = "",
    ) -> dict[str, str | bool]:
        """把客户端对象转成页面可直接渲染的结构。"""
        process = self._started_processes.get(str(client.root))
        running = process is not None and process.poll() is None
        latest_qr = self._pick_latest_file(self._find_qr_images_for_client(client.root))
        return {
            "provider": client.provider,
            "name": client.name,
            "root": str(client.root),
            "launchable": client.launchable,
            "running": running,
            "has_qr_image": latest_qr is not None,
            "selected": str(client.root) == selected_root,
        }

    def _resolve_provider_keys(self, provider_name: str) -> list[str]:
        """根据配置决定本次启用哪些 provider。"""
        normalized = str(provider_name).strip().lower() or "external"
        if normalized == "napcat":
            return ["napcat", "external"]
        if normalized == "lagrange":
            return ["lagrange", "external"]
        return ["napcat", "lagrange", "external"]

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
