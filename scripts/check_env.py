"""
启动前配置检查脚本。

目的：
1. 给新手一个明确的“哪里没配好”的提示
2. 在正式启动机器人前尽早发现明显错误
"""

from __future__ import annotations

import sys
from pathlib import Path


def load_env_file(env_path: Path) -> dict[str, str]:
    """读取 .env 为简单字典。"""
    env_map: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        env_map[key.strip()] = value.strip()
    return env_map


def normalize_env_file(env_path: Path) -> tuple[dict[str, str], bool]:
    """
    自动修正常见的新手配置错误。

    目前主要修复：
    - SUPERUSERS 留空导致 NoneBot 启动时 JSON 解析失败
    """
    changed = False
    lines = env_path.read_text(encoding="utf-8").splitlines()
    new_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("SUPERUSERS="):
            key, value = line.split("=", 1)
            if not value.strip():
                new_lines.append(f"{key}=[]")
                changed = True
                continue
        new_lines.append(line)

    if changed:
        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    return load_env_file(env_path), changed


def main() -> int:
    """检查关键配置项。"""
    project_dir = Path(__file__).resolve().parents[1]
    env_path = project_dir / ".env"
    if not env_path.exists():
        print("错误：项目根目录下不存在 .env 文件。")
        print("请先执行：bash scripts/run.sh")
        return 1

    env_map, normalized = normalize_env_file(env_path)
    if normalized:
        print("已自动修正 .env 中的空 SUPERUSERS 配置为 []，避免 NoneBot 启动失败。")

    target_groups = [
        item.strip()
        for item in env_map.get("VERIFY_TARGET_GROUPS", "").replace(" ", ",").split(",")
        if item.strip()
    ]
    if target_groups and not all(group.isdigit() for group in target_groups):
        print("错误：VERIFY_TARGET_GROUPS 必须填写纯数字群号，多个群号用英文逗号分隔。")
        return 1

    superusers = env_map.get("SUPERUSERS", "").strip()
    if superusers in {"", "[]"}:
        print("提示：SUPERUSERS 还没配置，后续可进入本地管理台再填写。")

    if not target_groups:
        print("提示：VERIFY_TARGET_GROUPS 还没配置，后续可进入本地管理台再填写。")

    print("基础配置检查通过。")
    print("可以继续启动机器人，然后打开本地管理台完成图形化配置。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
