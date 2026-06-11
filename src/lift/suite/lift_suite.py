"""LIFT suite JSON 加载。"""

from __future__ import annotations

from pathlib import Path

from src.models import Suite


def load_lift_suite(file_path: str | Path) -> Suite:
    """读取 suite JSON 并解析为 ``Suite``（含 warmup_tasks / holdout_tasks）。"""
    return Suite.from_json_file(file_path)
