"""
验证码模板管理。
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class VerifyTemplateProfile:
    """验证码模板当前状态。"""

    key: str
    name: str
    description: str
    html: str
    source: str
    editable: bool
    created_at: str = ""
    based_on: str = ""


class VerifyTemplateManager:
    """统一处理验证码模板预设与自定义模板库。"""

    def __init__(self, base_dir: Path, data_dir: Path) -> None:
        theme_dir = base_dir / "templates" / "themes"
        verify_template_path = base_dir / "templates" / "verify_card.html"
        self.custom_template_path = data_dir / "verify_card.custom.html"
        self.library_dir = data_dir / "template_library"
        self.library_index_path = self.library_dir / "index.json"
        self.library_dir.mkdir(parents=True, exist_ok=True)
        self.presets: dict[str, dict[str, str]] = {
            "classic": {
                "name": "经典蓝",
                "description": "稳妥清爽，适合默认启用。",
                "file": str(verify_template_path),
            },
            "glass": {
                "name": "玻璃霓光",
                "description": "更强调质感和数字展示，适合偏现代风格。",
                "file": str(theme_dir / "verify_card.glass.html"),
            },
            "warning": {
                "name": "警示橙",
                "description": "突出时效和操作提醒，适合强调风控提示。",
                "file": str(theme_dir / "verify_card.warning.html"),
            },
        }
        self._migrate_legacy_custom_template()

    def normalize_key(self, raw_value: str) -> str:
        value = str(raw_value).strip().lower()
        if not value:
            return "preset:classic"
        if value == "custom":
            entries = self._load_custom_entries()
            if entries:
                return f'library:{entries[0]["id"]}'
            return "preset:classic"
        if value.startswith("preset:"):
            preset = value.split(":", 1)[1]
            return f"preset:{preset}" if preset in self.presets else "preset:classic"
        if value.startswith("library:"):
            template_id = value.split(":", 1)[1]
            return value if self._find_custom_entry(template_id) is not None else "preset:classic"
        if value in self.presets:
            return f"preset:{value}"
        return "preset:classic"

    def validate_template_html(self, template_html: str) -> tuple[bool, str]:
        if not template_html:
            return False, "模板内容不能为空。"
        if 'id="verify-card"' not in template_html and "id='verify-card'" not in template_html:
            return False, '模板里必须保留 id="verify-card" 的根节点。'
        required_placeholders = ("{{verify_code}}", "{{user_qq}}", "{{group_name}}", "{{expire_time}}")
        missing = [item for item in required_placeholders if item not in template_html]
        if missing:
            return False, f"模板缺少占位符：{', '.join(missing)}"
        return True, ""

    def list_templates(self, active_key: str) -> list[dict[str, str | bool]]:
        current_key = self.normalize_key(active_key)
        templates: list[dict[str, str | bool]] = []
        for key, meta in self.presets.items():
            full_key = f"preset:{key}"
            templates.append(
                {
                    "key": full_key,
                    "name": meta["name"],
                    "description": meta["description"],
                    "active": full_key == current_key,
                    "editable": False,
                    "deletable": False,
                    "source": "内置预设",
                    "created_at": "",
                }
            )
        for entry in self._load_custom_entries():
            full_key = f'library:{entry["id"]}'
            templates.append(
                {
                    "key": full_key,
                    "name": str(entry.get("name", "未命名模板")),
                    "description": str(entry.get("description", "来自管理台模板库。")),
                    "active": full_key == current_key,
                    "editable": True,
                    "deletable": True,
                    "source": "模板库版本",
                    "created_at": str(entry.get("created_at", "")),
                }
            )
        return templates

    def get_active_template_profile(self, template_key: str) -> VerifyTemplateProfile:
        normalized_key = self.normalize_key(template_key)
        if normalized_key.startswith("library:"):
            template_id = normalized_key.split(":", 1)[1]
            entry = self._find_custom_entry(template_id)
            if entry is not None:
                template_html = self._html_path_for_entry(entry).read_text(encoding="utf-8")
                return VerifyTemplateProfile(
                    key=normalized_key,
                    name=str(entry.get("name", "自定义模板")),
                    description=str(entry.get("description", "当前使用模板库中的自定义版本。")),
                    html=template_html,
                    source="library",
                    editable=True,
                    created_at=str(entry.get("created_at", "")),
                    based_on=str(entry.get("based_on", "")),
                )

        preset_key = normalized_key.split(":", 1)[1]
        preset_meta = self.presets[preset_key]
        template_html = Path(preset_meta["file"]).read_text(encoding="utf-8")
        return VerifyTemplateProfile(
            key=normalized_key,
            name=preset_meta["name"],
            description=preset_meta["description"],
            html=template_html,
            source="preset",
            editable=False,
        )

    def save_template_version(
        self,
        *,
        template_html: str,
        template_name: str,
        based_on: str,
    ) -> tuple[bool, str, str]:
        normalized = template_html.strip()
        success, error_message = self.validate_template_html(normalized)
        if not success:
            return False, "", error_message
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        template_id = f"tpl_{datetime.now().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(3)}"
        html_file = f"{template_id}.html"
        entry = {
            "id": template_id,
            "name": template_name.strip() or f"自定义模板 {now}",
            "description": f"创建于 {now}",
            "html_file": html_file,
            "created_at": now,
            "based_on": self.normalize_key(based_on),
        }
        entries = self._load_custom_entries()
        entries.insert(0, entry)
        self._html_path_for_entry(entry).write_text(normalized + "\n", encoding="utf-8")
        self._save_custom_entries(entries)
        return True, f"library:{template_id}", "验证码模板已保存到模板库，并已切换到新版本。"

    def delete_template(self, template_key: str) -> tuple[bool, str]:
        normalized_key = self.normalize_key(template_key)
        if not normalized_key.startswith("library:"):
            return False, "内置预设不能删除。"
        template_id = normalized_key.split(":", 1)[1]
        entries = self._load_custom_entries()
        remaining = [entry for entry in entries if str(entry.get("id")) != template_id]
        if len(remaining) == len(entries):
            return False, "未找到要删除的模板版本。"
        for entry in entries:
            if str(entry.get("id")) != template_id:
                continue
            html_path = self._html_path_for_entry(entry)
            if html_path.exists():
                html_path.unlink()
            break
        self._save_custom_entries(remaining)
        return True, "模板版本已删除。"

    def activate_template(self, template_key: str) -> tuple[bool, str]:
        normalized_key = self.normalize_key(template_key)
        profile = self.get_active_template_profile(normalized_key)
        if profile.source == "preset":
            return True, f"已切换到“{profile.name}”预设。"
        return True, f"已切换到模板库版本“{profile.name}”。"

    def _migrate_legacy_custom_template(self) -> None:
        if not self.custom_template_path.exists():
            return
        if self._load_custom_entries():
            return
        template_html = self.custom_template_path.read_text(encoding="utf-8").strip()
        success, _template_key, _message = self.save_template_version(
            template_html=template_html,
            template_name="历史自定义模板",
            based_on="preset:classic",
        )
        if success:
            try:
                self.custom_template_path.unlink()
            except OSError:
                pass

    def _load_custom_entries(self) -> list[dict[str, Any]]:
        if not self.library_index_path.exists():
            return []
        try:
            payload = json.loads(self.library_index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        entries = payload.get("templates", [])
        return entries if isinstance(entries, list) else []

    def _save_custom_entries(self, entries: list[dict[str, Any]]) -> None:
        self.library_index_path.write_text(
            json.dumps({"templates": entries}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _find_custom_entry(self, template_id: str) -> dict[str, Any] | None:
        for entry in self._load_custom_entries():
            if str(entry.get("id")) == template_id:
                return entry
        return None

    def _html_path_for_entry(self, entry: dict[str, Any]) -> Path:
        return self.library_dir / str(entry["html_file"])
