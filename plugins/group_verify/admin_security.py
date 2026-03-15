"""
管理台访问控制。
"""

from __future__ import annotations

import base64
import secrets

from fastapi import HTTPException, Request

from .config import plugin_settings
from project_config import load_project_config


def ensure_admin_access(request: Request) -> None:
    """统一校验管理台来源和密码认证。"""
    _ensure_admin_request_allowed(request)
    _ensure_admin_authenticated(request)


def _ensure_admin_request_allowed(request: Request) -> None:
    """限制管理台为本机访问，避免被反代后错误暴露。"""
    admin_config = _load_admin_config()
    if not bool(admin_config["local_only"]):
        return
    if any(
        request.headers.get(header, "").strip()
        for header in ("x-forwarded-for", "x-real-ip", "forwarded")
    ):
        raise HTTPException(status_code=403, detail="管理台仅允许本机直连访问，不支持经代理转发。")
    client_host = _get_effective_client_host(request)
    if client_host not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(status_code=403, detail="管理台仅允许本机访问")


def _ensure_admin_authenticated(request: Request) -> None:
    """当配置了密码或开放远程访问时，强制要求 HTTP Basic 认证。"""
    admin_config = _load_admin_config()
    require_password = bool(admin_config["password"]) or not bool(admin_config["local_only"])
    if not require_password:
        return

    if not admin_config["password"]:
        raise HTTPException(
            status_code=503,
            detail="管理台未配置访问密码，已拒绝非本机访问。",
        )

    authorization = request.headers.get("authorization", "").strip()
    username, password = _parse_basic_auth(authorization)
    if not (
        secrets.compare_digest(username, str(admin_config["username"]))
        and secrets.compare_digest(password, str(admin_config["password"]))
    ):
        raise HTTPException(
            status_code=401,
            detail="管理台认证失败",
            headers={"WWW-Authenticate": 'Basic realm="Group Verify Admin"'},
        )


def _load_admin_config() -> dict[str, object]:
    """从项目配置文件读取当前生效的管理台账号配置。"""
    config = load_project_config(plugin_settings.project_root)
    admin = config.get("admin", {})
    return {
        "local_only": bool(admin.get("local_only", True)),
        "username": str(admin.get("username", plugin_settings.admin_username)).strip() or plugin_settings.admin_username,
        "password": str(admin.get("password", plugin_settings.admin_password)).strip(),
    }


def _get_effective_client_host(request: Request) -> str:
    """仅使用实际连接来源，不信任客户端自带代理头。"""
    return str(getattr(getattr(request, "client", None), "host", "")).strip()


def _parse_basic_auth(authorization: str) -> tuple[str, str]:
    """解析 HTTP Basic 认证头。"""
    if not authorization.lower().startswith("basic "):
        return "", ""
    encoded = authorization[6:].strip()
    try:
        decoded = base64.b64decode(encoded).decode("utf-8")
    except Exception:
        return "", ""
    if ":" not in decoded:
        return "", ""
    username, password = decoded.split(":", 1)
    return username, password
