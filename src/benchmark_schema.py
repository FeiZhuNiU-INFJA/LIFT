from __future__ import annotations

import json
from typing import Any
from pathlib import Path

from pydantic import BaseModel, Field, model_validator


class TaskRequirements(BaseModel):
    default_skills: list[str] = Field(default_factory=list)
    extra_skills_dir: str | None = None


class ExpectedResult(BaseModel):
    content_reqs: str = Field(default="", description="当前任务的内容相关需求")
    trajectory_reqs: str = Field(default="", description="当前任务的轨迹相关需求")

    @model_validator(mode="before")
    @classmethod
    def _compat_description_field(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        # Backward compatible with old schema: {"description": "..."}.
        if "content_reqs" not in data and "description" in data:
            data["content_reqs"] = data["description"]
        if "trajectory_reqs" not in data:
            data["trajectory_reqs"] = ""
        return data


class BenchmarkTask(BaseModel):
    name: str
    query: str
    requirements: TaskRequirements
    expected_result: ExpectedResult
    category_name: str | None = None


class BenchmarkSpec(BaseModel):
    name: str
    category: str
    tasks: list[BenchmarkTask] = Field(default_factory=list)

    @classmethod
    def from_json_file(cls, file_path: str | Path) -> BenchmarkSpec:
        data = json.loads(Path(file_path).read_text(encoding="utf-8"))
        spec = cls.model_validate(data)
        for task in spec.tasks:
            task.category_name = spec.category
        return spec
