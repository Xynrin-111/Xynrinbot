#!/usr/bin/env python3
"""
项目配置与自检入口。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from project_config import (  # noqa: E402
    apply_project_config_to_env,
    config_file,
    ensure_project_config,
    export_env_file,
    get_config_value,
    load_project_config,
    save_project_config,
    set_config_value,
    validate_project_config,
)


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "help"
    if command == "init":
        path = ensure_project_config(PROJECT_ROOT)
        print(f"已初始化项目配置：{path}")
        return 0

    if command == "doctor":
        ensure_project_config(PROJECT_ROOT)
        data = load_project_config(PROJECT_ROOT)
        errors, warnings = validate_project_config(data)
        print(f"配置文件：{config_file(PROJECT_ROOT)}")
        print(json.dumps(data, ensure_ascii=False, indent=2))
        for warning in warnings:
            print(f"提示：{warning}")
        if errors:
            for error in errors:
                print(f"错误：{error}")
            return 1
        print("配置检查通过。")
        return 0

    if command == "export-env":
        ensure_project_config(PROJECT_ROOT)
        target = export_env_file(PROJECT_ROOT)
        print(f"已同步环境文件：{target}")
        return 0

    if command == "get":
        if len(sys.argv) < 3:
            print("用法：projectctl.py get <dotted.key>")
            return 1
        ensure_project_config(PROJECT_ROOT)
        data = load_project_config(PROJECT_ROOT)
        value = get_config_value(data, sys.argv[2])
        if isinstance(value, (dict, list)):
            print(json.dumps(value, ensure_ascii=False))
        else:
            print(value)
        return 0

    if command == "set":
        if len(sys.argv) < 4:
            print("用法：projectctl.py set <dotted.key> <value>")
            return 1
        ensure_project_config(PROJECT_ROOT)
        data = load_project_config(PROJECT_ROOT)
        updated = set_config_value(data, sys.argv[2], sys.argv[3])
        errors, warnings = validate_project_config(updated)
        if errors:
            for error in errors:
                print(f"错误：{error}")
            return 1
        save_project_config(PROJECT_ROOT, updated)
        export_env_file(PROJECT_ROOT)
        for warning in warnings:
            print(f"提示：{warning}")
        print(f"已更新配置：{sys.argv[2]}")
        return 0

    if command == "apply-env":
        ensure_project_config(PROJECT_ROOT)
        env_map = apply_project_config_to_env(PROJECT_ROOT)
        print(json.dumps(env_map, ensure_ascii=False, indent=2))
        return 0

    print(
        "可用命令：\n"
        "  init\n"
        "  doctor\n"
        "  export-env\n"
        "  get <dotted.key>\n"
        "  set <dotted.key> <value>\n"
        "  apply-env"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
