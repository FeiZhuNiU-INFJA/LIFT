from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from tqdm import tqdm


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.models import SuiteSpec  # noqa: E402


TASK_DIR_RE = re.compile(r"^q(?P<index>\d+)(?:[_-].+)?$", re.IGNORECASE)
SECTION_NAMES = ("query", "要求", "轨迹要求")
IGNORED_DIR_NAMES = {"__MACOSX"}
DEFAULT_INPUT_ROOT = PROJECT_ROOT / "assets" / "benchmark_mds"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "assets" / "benchmarks"


def to_project_relative(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT).as_posix()


def parse_markdown_sections(markdown_text: str, source_path: Path) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current_section: str | None = None

    for raw_line in markdown_text.splitlines():
        line = raw_line.rstrip()
        heading = line.strip()
        if heading.startswith("### "):
            section_name = heading[4:].strip()
            if section_name in SECTION_NAMES:
                current_section = section_name
                sections[current_section] = []
                continue
        if current_section is not None:
            sections[current_section].append(line)

    missing_sections = [name for name in SECTION_NAMES if name not in sections]
    if missing_sections:
        missing = ", ".join(missing_sections)
        raise ValueError(f"Markdown task file missing sections [{missing}]: {source_path}")

    return {name: normalize_section_text(sections[name]) for name in SECTION_NAMES}


def normalize_section_text(lines: list[str]) -> str:
    stripped_lines = [line.strip() for line in lines]
    while stripped_lines and not stripped_lines[0]:
        stripped_lines.pop(0)
    while stripped_lines and not stripped_lines[-1]:
        stripped_lines.pop()
    return "\n".join(line for line in stripped_lines if line)


def find_task_markdown(task_dir: Path) -> Path:
    expected_md = task_dir / f"{task_dir.name}.md"
    if expected_md.exists():
        return expected_md

    markdown_files = sorted(path for path in task_dir.iterdir() if path.is_file() and path.suffix.lower() == ".md")
    if len(markdown_files) == 1:
        return markdown_files[0]

    raise ValueError(f"Unable to determine task markdown file in {task_dir}")


def resolve_scene_dir(scene_dir: Path) -> Path:
    if (scene_dir / "train").is_dir() or (scene_dir / "test").is_dir():
        return scene_dir

    nested_candidates = [
        path
        for path in scene_dir.iterdir()
        if path.is_dir() and path.name not in IGNORED_DIR_NAMES
    ]

    preferred_nested = [path for path in nested_candidates if path.name == scene_dir.name]
    for candidate in preferred_nested + nested_candidates:
        if (candidate / "train").is_dir() or (candidate / "test").is_dir():
            return candidate

    raise ValueError(f"No train/test split directories found in suite scene: {scene_dir}")


def find_materials_dir(task_dir: Path, task_index: int) -> Path | None:
    preferred_dir = task_dir / f"q{task_index}_materials"
    if preferred_dir.exists():
        return preferred_dir

    legacy_dir = task_dir / "materials"
    if legacy_dir.exists():
        return legacy_dir

    return None


def build_task_entry(scene_dir: Path, task_dir: Path, split_dir: Path | None = None) -> dict[str, object]:
    match = TASK_DIR_RE.match(task_dir.name)
    if match is None:
        raise ValueError(f"Invalid task directory name: {task_dir.name}")

    task_index = int(match.group("index"))
    md_path = find_task_markdown(task_dir)
    sections = parse_markdown_sections(md_path.read_text(encoding="utf-8"), md_path)
    materials_dir = find_materials_dir(task_dir, task_index)
    skills_dir = split_dir / "skills" if split_dir is not None else None

    return {
        "name": f"Q{task_index}",
        "query": sections["query"],
        "requirements": {
            "default_skills": [],
            "extra_skills_dir": (
                to_project_relative(skills_dir)
                if skills_dir is not None and skills_dir.exists()
                else ""
            ),
            "material_dir": to_project_relative(materials_dir) if materials_dir is not None else "",
        },
        "expected_result": {
            "content_reqs": sections["要求"],
            "trajectory_reqs": sections["轨迹要求"],
        },
    }


def iter_task_dirs(scene_dir: Path) -> list[Path]:
    task_dirs = [
        path
        for path in scene_dir.iterdir()
        if path.is_dir() and TASK_DIR_RE.match(path.name)
    ]
    return sorted(task_dirs, key=lambda path: int(TASK_DIR_RE.match(path.name).group("index")))


def build_split_tasks(scene_dir: Path, split_name: str) -> list[dict[str, object]]:
    split_dir = scene_dir / split_name
    if not split_dir.is_dir():
        return []
    task_dirs = iter_task_dirs(split_dir)
    return [build_task_entry(scene_dir, task_dir, split_dir) for task_dir in task_dirs]


def build_suite_spec(scene_dir: Path) -> dict[str, object]:
    resolved_scene_dir = resolve_scene_dir(scene_dir)
    train_tasks = build_split_tasks(resolved_scene_dir, "train")
    test_tasks = build_split_tasks(resolved_scene_dir, "test")

    return {
        "name": resolved_scene_dir.name,
        "category": resolved_scene_dir.name,
        "train": train_tasks,
        "test": test_tasks,
    }


def convert_all(input_root: Path, output_root: Path) -> list[Path]:
    output_root.mkdir(parents=True, exist_ok=True)
    written_files: list[Path] = []

    scene_dirs = sorted(
        path
        for path in input_root.iterdir()
        if path.is_dir() and path.name not in IGNORED_DIR_NAMES
    )
    progress = tqdm(scene_dirs, unit="suite")
    for scene_dir in progress:
        progress.set_description(scene_dir.name)
        suite_data = build_suite_spec(scene_dir)
        SuiteSpec.model_validate(suite_data)

        output_path = output_root / f"{scene_dir.name}.json"
        output_path.write_text(
            json.dumps(suite_data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        written_files.append(output_path)

    return written_files


def preprocess_suite_mds(
    input_root: Path | None = None,
    output_root: Path | None = None,
) -> list[Path]:
    resolved_input_root = (input_root or DEFAULT_INPUT_ROOT).resolve()
    resolved_output_root = (output_root or DEFAULT_OUTPUT_ROOT).resolve()
    return convert_all(resolved_input_root, resolved_output_root)


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert markdown suite folders into JSON suite specs.")
    parser.add_argument(
        "--input-root",
        type=Path,
        default=DEFAULT_INPUT_ROOT,
        help="Directory containing markdown suite scene folders.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Directory to write generated suite JSON files into.",
    )
    args = parser.parse_args()

    written_files = preprocess_suite_mds(args.input_root, args.output_root)
    for output_path in written_files:
        print(output_path)


if __name__ == "__main__":
    main()
