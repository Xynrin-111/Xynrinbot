"""
项目级配置管理。

目标：
1. 用结构化配置文件作为项目配置源，而不是让用户直接编辑 .env
2. 在启动前把配置同步到环境变量，兼容 NoneBot 当前的读取方式
3. 保留对旧版 .env 的向后兼容
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any


CONFIG_DIR_NAME = "config"
CONFIG_FILE_NAME = "appsettings.json"
CONFIG_EXAMPLE_FILE_NAME = "appsettings.json.example"


def project_root_from(path: str | Path | None = None) -> Path:
    if path is None:
        return Path(__file__).resolve().parent
    current = Path(path).resolve()
    return current if current.is_dir() else current.parent


def config_dir(project_root: Path) -> Path:
    return project_root / CONFIG_DIR_NAME


def config_file(project_root: Path) -> Path:
    return config_dir(project_root) / CONFIG_FILE_NAME


def config_example_file(project_root: Path) -> Path:
    return config_dir(project_root) / CONFIG_EXAMPLE_FILE_NAME


def default_project_config() -> dict[str, Any]:
    return {
        "app": {
            "host": "127.0.0.1",
            "port": 8080,
            "driver": "~fastapi",
            "log_level": "INFO",
            "deploy_profile": "desktop",
            "platform": "auto",
        },
        "admin": {
            "path": "/admin",
            "local_only": True,
            "username": "admin",
            "password": "",
            "auto_open": False,
        },
        "onebot": {
            "provider": "external",
            "access_token": "",
            "lagrange_qr_dir": "",
            "install_client": "none",
        },
        "smtp": {
            "host": "",
            "port": 465,
            "username": "",
            "password": "",
            "from_email": "",
            "to_email": "",
            "use_tls": False,
            "use_ssl": True,
        },
        "proxy": {
            "http_proxy": "",
            "https_proxy": "",
            "all_proxy": "",
            "no_proxy": "",
        },
        "verify": {
            "target_groups": [],
            "superusers": [],
            "timeout_minutes": 5,
            "max_error_times": 3,
            "playwright_browser": "chromium",
            "image_retry_times": 2,
        },
        "runtime": {
            "python_mode": "project",
        },
    }


def _project_default_config(project_root: Path | None = None) -> dict[str, Any]:
    base = copy.deepcopy(default_project_config())
    if project_root is None:
        return base

    example_path = config_example_file(project_root)
    if not example_path.exists():
        return base

    try:
        loaded = json.loads(example_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return base

    if isinstance(loaded, dict):
        _deep_update(base, loaded)
    return base


def ensure_project_config(project_root: Path) -> Path:
    cfg_dir = config_dir(project_root)
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = config_file(project_root)
    if cfg_file.exists():
        return cfg_file
    cfg_file.write_text(
        json.dumps(_project_default_config(project_root), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return cfg_file


def load_project_config(project_root: Path) -> dict[str, Any]:
    data = _project_default_config(project_root)
    cfg_file = config_file(project_root)
    if cfg_file.exists():
        loaded = json.loads(cfg_file.read_text(encoding="utf-8"))
        _deep_update(data, loaded)
        return data

    legacy_env = project_root / ".env"
    if legacy_env.exists():
        _deep_update(data, project_config_from_legacy_env(legacy_env))
    return data


def save_project_config(project_root: Path, data: dict[str, Any]) -> Path:
    cfg_file = ensure_project_config(project_root)
    cfg_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return cfg_file


def apply_project_config_to_env(project_root: Path) -> dict[str, str]:
    loaded = load_project_config(project_root)
    errors, _warnings = validate_project_config(loaded)
    if errors:
        raise ValueError("项目配置无效：" + "；".join(errors))
    data = normalize_project_config(loaded)
    env_map = project_config_to_env(data)
    for key, value in env_map.items():
        os.environ[key] = value
    return env_map


def project_config_to_env(data: dict[str, Any]) -> dict[str, str]:
    errors, _warnings = validate_project_config(data)
    if errors:
        raise ValueError("项目配置无效：" + "；".join(errors))
    normalized = normalize_project_config(data)
    app = normalized["app"]
    admin = normalized["admin"]
    onebot = normalized["onebot"]
    smtp = normalized["smtp"]
    proxy = normalized["proxy"]
    verify = normalized["verify"]
    runtime = normalized["runtime"]
    return {
        "HOST": str(app["host"]),
        "PORT": str(app["port"]),
        "DRIVER": str(app["driver"]),
        "LOG_LEVEL": str(app["log_level"]),
        "APP_DEPLOY_PROFILE": str(app["deploy_profile"]),
        "APP_PLATFORM": str(app["platform"]),
        "VERIFY_ADMIN_PATH": str(admin["path"]),
        "VERIFY_ADMIN_LOCAL_ONLY": _bool_str(admin["local_only"]),
        "VERIFY_ADMIN_USERNAME": str(admin["username"]),
        "VERIFY_ADMIN_PASSWORD": str(admin["password"]),
        "VERIFY_AUTO_OPEN_ADMIN": _bool_str(admin["auto_open"]),
        "VERIFY_ONEBOT_PROVIDER": str(onebot["provider"]),
        "ONEBOT_ACCESS_TOKEN": str(onebot["access_token"]),
        "VERIFY_LAGRANGE_QR_DIR": str(onebot["lagrange_qr_dir"]),
        "SMTP_HOST": str(smtp["host"]),
        "SMTP_PORT": str(smtp["port"]),
        "SMTP_USERNAME": str(smtp["username"]),
        "SMTP_PASSWORD": str(smtp["password"]),
        "SMTP_FROM_EMAIL": str(smtp["from_email"]),
        "SMTP_TO_EMAIL": str(smtp["to_email"]),
        "SMTP_USE_TLS": _bool_str(smtp["use_tls"]),
        "SMTP_USE_SSL": _bool_str(smtp["use_ssl"]),
        "HTTP_PROXY": str(proxy["http_proxy"]),
        "HTTPS_PROXY": str(proxy["https_proxy"]),
        "ALL_PROXY": str(proxy["all_proxy"]),
        "NO_PROXY": str(proxy["no_proxy"]),
        "SUPERUSERS": json.dumps([str(item) for item in verify["superusers"]], ensure_ascii=False),
        "VERIFY_TARGET_GROUPS": ",".join(str(item) for item in verify["target_groups"]),
        "VERIFY_TIMEOUT_MINUTES": str(verify["timeout_minutes"]),
        "VERIFY_MAX_ERROR_TIMES": str(verify["max_error_times"]),
        "VERIFY_PLAYWRIGHT_BROWSER": str(verify["playwright_browser"]),
        "VERIFY_IMAGE_RETRY_TIMES": str(verify["image_retry_times"]),
        "PYTHON_RUNTIME_MODE": str(runtime["python_mode"]),
    }


def export_env_file(project_root: Path, output_path: Path | None = None) -> Path:
    project_root = project_root.resolve()
    env_map = apply_project_config_to_env(project_root)
    target = output_path or (project_root / ".env")
    lines = [f"{key}={value}" for key, value in env_map.items()]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def normalize_project_config(data: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(default_project_config())
    _deep_update(result, data)

    app = result["app"]
    admin = result["admin"]
    onebot = result["onebot"]
    smtp = result["smtp"]
    proxy = result["proxy"]
    verify = result["verify"]
    runtime = result["runtime"]

    app["port"] = _safe_int(app.get("port"), 8080)
    app["deploy_profile"] = _normalize_choice(app.get("deploy_profile"), {"desktop", "server"}, "desktop")
    app["platform"] = _normalize_choice(app.get("platform"), {"auto", "linux", "windows", "macos", "android"}, "auto")
    admin["path"] = _normalize_admin_path(str(admin.get("path", "/admin")))
    admin["local_only"] = bool(admin.get("local_only", True))
    admin["auto_open"] = bool(admin.get("auto_open", False))
    admin["username"] = str(admin.get("username", "admin")).strip() or "admin"
    admin["password"] = str(admin.get("password", "")).strip()
    onebot["provider"] = _normalize_choice(onebot.get("provider"), {"external", "napcat", "lagrange"}, "external")
    onebot["access_token"] = str(onebot.get("access_token", "")).strip()
    onebot["lagrange_qr_dir"] = str(onebot.get("lagrange_qr_dir", "")).strip()
    onebot["install_client"] = _normalize_choice(onebot.get("install_client"), {"none", "napcat", "lagrange"}, "none")
    smtp["host"] = str(smtp.get("host", "")).strip()
    smtp["port"] = _safe_int(smtp.get("port"), 465)
    smtp["username"] = str(smtp.get("username", "")).strip()
    smtp["password"] = str(smtp.get("password", "")).strip()
    smtp["from_email"] = str(smtp.get("from_email", "")).strip()
    smtp["to_email"] = str(smtp.get("to_email", "")).strip()
    smtp["use_tls"] = bool(smtp.get("use_tls", False))
    smtp["use_ssl"] = bool(smtp.get("use_ssl", True))
    proxy["http_proxy"] = str(proxy.get("http_proxy", "")).strip()
    proxy["https_proxy"] = str(proxy.get("https_proxy", "")).strip()
    proxy["all_proxy"] = str(proxy.get("all_proxy", "")).strip()
    proxy["no_proxy"] = str(proxy.get("no_proxy", "")).strip()
    verify["target_groups"] = _normalize_int_list(verify.get("target_groups"))
    verify["superusers"] = _normalize_int_list(verify.get("superusers"))
    verify["timeout_minutes"] = _safe_int(verify.get("timeout_minutes"), 5)
    verify["max_error_times"] = _safe_int(verify.get("max_error_times"), 3)
    verify["playwright_browser"] = "chromium"
    verify["image_retry_times"] = _safe_int(verify.get("image_retry_times"), 2)
    runtime["python_mode"] = _normalize_choice(runtime.get("python_mode"), {"project", "venv"}, "project")
    return result


def validate_project_config(data: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    merged = _project_default_config()
    _deep_update(merged, data)

    app = merged["app"]
    admin = merged["admin"]
    onebot = merged["onebot"]
    smtp = merged["smtp"]
    proxy = merged["proxy"]
    verify = merged["verify"]
    runtime = merged["runtime"]

    port = _parse_int(app.get("port"))
    if port is None:
        errors.append("app.port 必须是 1 到 65535 的整数。")
    elif not (1 <= port <= 65535):
        errors.append("app.port 必须是 1 到 65535 的整数。")

    deploy_profile = _validate_choice(
        app.get("deploy_profile"),
        {"desktop", "server"},
        "app.deploy_profile",
        errors,
    )
    platform_name = _validate_choice(
        app.get("platform"),
        {"auto", "linux", "windows", "macos", "android"},
        "app.platform",
        errors,
    )
    _validate_choice(
        onebot.get("provider"),
        {"external", "napcat", "lagrange"},
        "onebot.provider",
        errors,
    )
    _validate_choice(
        onebot.get("install_client"),
        {"none", "napcat", "lagrange"},
        "onebot.install_client",
        errors,
    )
    _validate_choice(
        runtime.get("python_mode"),
        {"project", "venv"},
        "runtime.python_mode",
        errors,
    )
    smtp_port = _parse_int(smtp.get("port"))
    if smtp_port is None or not (1 <= smtp_port <= 65535):
        errors.append("smtp.port 必须是 1 到 65535 的整数。")
    if _parse_bool(smtp.get("use_tls"), False) and _parse_bool(smtp.get("use_ssl"), True):
        errors.append("smtp.use_tls 和 smtp.use_ssl 不能同时开启。")
    playwright_browser = str(verify.get("playwright_browser", "")).strip().lower()
    if playwright_browser and playwright_browser != "chromium":
        errors.append("verify.playwright_browser 当前仅支持 chromium。")

    timeout_minutes = _parse_int(verify.get("timeout_minutes"))
    if timeout_minutes is None or timeout_minutes < 1 or timeout_minutes > 120:
        errors.append("verify.timeout_minutes 必须在 1 到 120 之间。")

    max_error_times = _parse_int(verify.get("max_error_times"))
    if max_error_times is None or max_error_times < 1 or max_error_times > 10:
        errors.append("verify.max_error_times 必须在 1 到 10 之间。")

    image_retry_times = _parse_int(verify.get("image_retry_times"))
    if image_retry_times is None or image_retry_times < 0 or image_retry_times > 10:
        errors.append("verify.image_retry_times 必须在 0 到 10 之间。")

    normalized = normalize_project_config(data)

    app = normalized["app"]
    admin = normalized["admin"]
    onebot = normalized["onebot"]
    verify = normalized["verify"]

    if not admin["local_only"] and not admin["password"]:
        errors.append("admin.local_only=false 时必须配置 admin.password。")

    if deploy_profile == "server" and platform_name in {"windows", "macos", "android"}:
        warnings.append(f"{app['platform']} 不适合作为长期无人值守 server 环境。")

    if onebot["provider"] == "external":
        warnings.append("当前采用 external OneBot 提供方式，需自行保证 OneBot 端可用。")
    if smtp["host"] and not smtp["to_email"]:
        warnings.append("已配置 smtp.host，但 smtp.to_email 为空，管理台测试邮件需要单独填写收件人。")
    if proxy["http_proxy"] or proxy["https_proxy"] or proxy["all_proxy"]:
        warnings.append("已配置统一代理，脚本安装和下载链路会优先继承这些代理环境变量。")

    if not verify["superusers"]:
        warnings.append("verify.superusers 尚未配置，启动后只能先进入管理台补充。")

    if not verify["target_groups"]:
        warnings.append("verify.target_groups 尚未配置，启动后需在管理台补充。")

    return errors, warnings


def set_config_value(data: dict[str, Any], dotted_key: str, raw_value: str) -> dict[str, Any]:
    result = copy.deepcopy(data)
    parts = dotted_key.split(".")
    cursor: Any = result
    for part in parts[:-1]:
        if part not in cursor or not isinstance(cursor[part], dict):
            cursor[part] = {}
        cursor = cursor[part]
    cursor[parts[-1]] = _coerce_cli_value(raw_value)
    return result


def get_config_value(data: dict[str, Any], dotted_key: str) -> Any:
    cursor: Any = data
    for part in dotted_key.split("."):
        cursor = cursor[part]
    return cursor


def project_config_from_legacy_env(env_path: Path) -> dict[str, Any]:
    env_map: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        env_map[key.strip()] = value.strip()

    return {
        "app": {
            "host": env_map.get("HOST", "127.0.0.1"),
            "port": _safe_int(env_map.get("PORT", "8080"), 8080),
            "driver": env_map.get("DRIVER", "~fastapi"),
            "log_level": env_map.get("LOG_LEVEL", "INFO"),
            "deploy_profile": env_map.get("APP_DEPLOY_PROFILE", "desktop"),
            "platform": env_map.get("APP_PLATFORM", "auto"),
        },
        "admin": {
            "path": env_map.get("VERIFY_ADMIN_PATH", "/admin"),
            "local_only": _parse_bool(env_map.get("VERIFY_ADMIN_LOCAL_ONLY", "true"), True),
            "username": env_map.get("VERIFY_ADMIN_USERNAME", "admin"),
            "password": env_map.get("VERIFY_ADMIN_PASSWORD", ""),
            "auto_open": _parse_bool(env_map.get("VERIFY_AUTO_OPEN_ADMIN", "false"), False),
        },
        "onebot": {
            "provider": env_map.get("VERIFY_ONEBOT_PROVIDER", "external"),
            "access_token": env_map.get("ONEBOT_ACCESS_TOKEN", ""),
            "lagrange_qr_dir": env_map.get("VERIFY_LAGRANGE_QR_DIR", ""),
        },
        "smtp": {
            "host": env_map.get("SMTP_HOST", ""),
            "port": _safe_int(env_map.get("SMTP_PORT", "465"), 465),
            "username": env_map.get("SMTP_USERNAME", ""),
            "password": env_map.get("SMTP_PASSWORD", ""),
            "from_email": env_map.get("SMTP_FROM_EMAIL", ""),
            "to_email": env_map.get("SMTP_TO_EMAIL", ""),
            "use_tls": _parse_bool(env_map.get("SMTP_USE_TLS", "false"), False),
            "use_ssl": _parse_bool(env_map.get("SMTP_USE_SSL", "true"), True),
        },
        "proxy": {
            "http_proxy": env_map.get("HTTP_PROXY", ""),
            "https_proxy": env_map.get("HTTPS_PROXY", ""),
            "all_proxy": env_map.get("ALL_PROXY", ""),
            "no_proxy": env_map.get("NO_PROXY", ""),
        },
        "verify": {
            "target_groups": _normalize_int_list(env_map.get("VERIFY_TARGET_GROUPS", "")),
            "superusers": _normalize_int_list(env_map.get("SUPERUSERS", "[]")),
            "timeout_minutes": _safe_int(env_map.get("VERIFY_TIMEOUT_MINUTES", "5"), 5),
            "max_error_times": _safe_int(env_map.get("VERIFY_MAX_ERROR_TIMES", "3"), 3),
            "playwright_browser": env_map.get("VERIFY_PLAYWRIGHT_BROWSER", "chromium"),
            "image_retry_times": _safe_int(env_map.get("VERIFY_IMAGE_RETRY_TIMES", "2"), 2),
        },
        "runtime": {
            "python_mode": env_map.get("PYTHON_RUNTIME_MODE", "project"),
        },
    }


def _deep_update(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
            continue
        target[key] = value


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _bool_str(value: bool) -> str:
    return "true" if value else "false"


def _normalize_choice(value: Any, allowed: set[str], default: str) -> str:
    text = str(value).strip().lower() or default
    return text if text in allowed else default


def _validate_choice(
    value: Any,
    allowed: set[str],
    field_name: str,
    errors: list[str],
) -> str:
    text = str(value).strip().lower()
    if not text:
        return ""
    if text not in allowed:
        allowed_text = " / ".join(sorted(allowed))
        errors.append(f"{field_name} 只能是 {allowed_text}。")
    return text


def _normalize_admin_path(value: str) -> str:
    path = value.strip() or "/admin"
    return path if path.startswith("/") else f"/{path}"


def _normalize_int_list(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    else:
        text = str(value).strip().strip("[]")
        if not text:
            return []
        for old, new in (('"', ""), ("'", ""), (" ", ",")):
            text = text.replace(old, new)
        items = [item for item in text.split(",") if item]
    result: list[int] = []
    for item in items:
        try:
            number = int(str(item).strip())
        except (TypeError, ValueError):
            continue
        if number not in result:
            result.append(number)
    return result


def _coerce_cli_value(raw_value: str) -> Any:
    text = raw_value.strip()
    if text.lower() in {"true", "false"}:
        return text.lower() == "true"
    if text.startswith("[") and text.endswith("]"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    try:
        return int(text)
    except ValueError:
        return text
