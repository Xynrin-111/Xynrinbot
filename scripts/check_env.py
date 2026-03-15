"""
启动前配置检查脚本。

现在以 config/appsettings.json 为主配置源，并兼容旧 .env。
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from project_config import ensure_project_config, load_project_config, validate_project_config  # noqa: E402


def main() -> int:
    config_path = ensure_project_config(PROJECT_ROOT)
    config = load_project_config(PROJECT_ROOT)
    errors, warnings = validate_project_config(config)

    app = config["app"]
    admin = config["admin"]
    onebot = config["onebot"]
    verify = config["verify"]

    quiet_mode = "--quiet" in sys.argv[1:]

    actual_platform = detect_platform_name()
    declared_platform = str(app["platform"]).strip().lower() or "auto"
    if declared_platform == "auto":
        declared_platform = actual_platform

    if not quiet_mode:
        print(f"配置文件：{config_path}")
        print(f"部署类型：{app['deploy_profile']}")
        print(f"声明平台：{declared_platform}")
        print(f"实际平台：{actual_platform}")
        print(f"OneBot 提供方式：{onebot['provider']}")
        print(f"管理台路径：{admin['path']}")
        print(f"目标群数量：{len(verify['target_groups'])}")
        print(f"超级管理员数量：{len(verify['superusers'])}")

    if declared_platform != actual_platform:
        warnings.append("配置中的 app.platform 与当前实际平台不一致；若非交叉部署，建议改回 auto。")

    for warning in warnings:
        print(f"提示：{warning}")

    if errors:
        for error in errors:
            print(f"错误：{error}")
        return 1

    if quiet_mode:
        print("基础配置检查通过，可进入初始化向导。")
    else:
        print("基础配置检查通过。")
        print("可以继续启动机器人，然后打开管理台完成图形化配置。")
    return 0


def detect_platform_name() -> str:
    system_name = platform.system().lower()
    if system_name == "linux":
        return "linux"
    if system_name == "windows":
        return "windows"
    if system_name == "darwin":
        return "macos"
    return system_name


if __name__ == "__main__":
    sys.exit(main())
