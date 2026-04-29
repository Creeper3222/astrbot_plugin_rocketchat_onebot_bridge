from __future__ import annotations

from pathlib import Path


def resolve_astrbot_data_root(plugin_root: Path) -> Path:
    return plugin_root.resolve().parent.parent


def resolve_legacy_plugin_data_dir(plugin_root: Path) -> Path:
    return plugin_root / "data"


def resolve_plugin_data_dir(plugin_root: Path) -> Path:
    data_root = resolve_astrbot_data_root(plugin_root)
    target_dir = data_root / "plugin_data" / plugin_root.name
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir