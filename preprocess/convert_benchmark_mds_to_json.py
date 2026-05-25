from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.models import BenchmarkSpec  # noqa: E402


TASK_DIR_RE = re.compile(r"^q(?P<index>\d+)(?:[_-].+)?$", re.IGNORECASE)
SECTION_NAMES = ("query", "要求", "轨迹要求")


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


def build_task_entry(scene_dir: Path, task_dir: Path) -> dict[str, object]:
    match = TASK_DIR_RE.match(task_dir.name)
    if match is None:
        raise ValueError(f"Invalid task directory name: {task_dir.name}")

    md_path = find_task_markdown(task_dir)
    sections = parse_markdown_sections(md_path.read_text(encoding="utf-8"), md_path)
    materials_dir = task_dir / "materials"
    skills_dir = scene_dir / "skills"

    return {
        "name": f"Q{int(match.group('index'))}",
        "query": sections["query"],
        "requirements": {
            "default_skills": [],
            "extra_skills_dir": to_project_relative(skills_dir) if skills_dir.exists() else "",
            "material_dir": to_project_relative(materials_dir) if materials_dir.exists() else "",
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


def build_benchmark_spec(scene_dir: Path) -> dict[str, object]:
    task_dirs = iter_task_dirs(scene_dir)
    if not task_dirs:
        raise ValueError(f"No task directories found in benchmark scene: {scene_dir}")

    return {
        "name": scene_dir.name,
        "category": scene_dir.name,
        "tasks": [build_task_entry(scene_dir, task_dir) for task_dir in task_dirs],
    }


def convert_all(input_root: Path, output_root: Path) -> list[Path]:
    output_root.mkdir(parents=True, exist_ok=True)
    written_files: list[Path] = []

    for scene_dir in sorted(path for path in input_root.iterdir() if path.is_dir()):
        benchmark_data = build_benchmark_spec(scene_dir)
        BenchmarkSpec.model_validate(benchmark_data)

        output_path = output_root / f"{scene_dir.name}.json"
        output_path.write_text(
            json.dumps(benchmark_data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        written_files.append(output_path)

    return written_files


def preprocess_benchmark_mds(
    input_root: Path | None = None,
    output_root: Path | None = None,
) -> list[Path]:
    resolved_input_root = (input_root or (PROJECT_ROOT / "assets" / "benchmark_mds")).resolve()
    resolved_output_root = (output_root or (PROJECT_ROOT / "assets" / "benchmarks")).resolve()
    return convert_all(resolved_input_root, resolved_output_root)


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert markdown benchmarks into JSON benchmark specs.")
    parser.add_argument(
        "--input-root",
        type=Path,
        default=PROJECT_ROOT / "assets" / "benchmark_mds",
        help="Directory containing markdown benchmark scene folders.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=PROJECT_ROOT / "assets" / "benchmarks",
        help="Directory to write generated benchmark JSON files into.",
    )
    args = parser.parse_args()

    written_files = preprocess_benchmark_mds(args.input_root, args.output_root)
    for output_path in written_files:
        print(output_path)


if __name__ == "__main__":
    main()
