"""Uploader: mirror ``benchmark_mds`` (tree + zip) to a ModelScope dataset repo.

Usage (run from project root):

    conda run -n lift python scripts/upload_benchmark_to_modelscope.py
    conda run -n lift python scripts/upload_benchmark_to_modelscope.py --zip path/to/benchmark_mds.zip

Requires the following ``.env`` entries:
    TOS_ACCESS_KEY / TOS_SECRET_KEY  (only if downloading from TOS; not needed with --zip / --local-tree)
    MODELSCOPE_API_TOKEN             (write-permission SDK token; https://modelscope.cn/my/myaccesstoken)
    BENCHMARK_MODELSCOPE_REPO        (e.g. ``<user-or-org>/<dataset-name>``; falls back to Evolvon/EALE)

Behaviour: uploads a staging directory with the layout expected by consumers ---
``benchmark_mds/`` (browsable tree) + ``benchmark_mds.zip`` (one-shot archive).
Optional ``README.md`` / ``DATASHEET.md`` next to the tree are picked up as-is.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.paths import (  # noqa: E402
    BENCHMARK_MDS_DIR,
    BENCHMARK_MDS_TOS_OBJECT_KEY,
    DEFAULT_BENCHMARK_MODELSCOPE_REPO,
)
from src.preprocess.benchmark_mds_fetch import download_benchmark_mds_zip  # noqa: E402

EXTRACTED_DIR_IN_REPO = "benchmark_mds"
"""ModelScope 仓库内解压后浏览目录（既用于网页预览，也是 preprocess 拉取的目录树）。"""

ZIP_PATH_IN_REPO = "benchmark_mds.zip"
"""ModelScope 仓库内 zip 归档路径。"""

DATASET_RELEASE_DIR = PROJECT_ROOT / "docs" / "neurips" / "dataset-release"
"""README.md / DATASHEET.md 的规范位置；如果存在会被拷进 staging。"""

load_dotenv()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--zip",
        type=Path,
        default=None,
        help="Local benchmark_mds.zip to upload. If omitted, fresh copy is downloaded from TOS.",
    )
    parser.add_argument(
        "--local-tree",
        type=Path,
        default=None,
        help=(
            "Local benchmark_mds/ directory tree to upload. If omitted, the tree is derived "
            "from the zip (see --zip / TOS fallback)."
        ),
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="Override BENCHMARK_MODELSCOPE_REPO env (e.g. org/dataset).",
    )
    parser.add_argument(
        "--commit-message",
        type=str,
        default="Update benchmark_mds",
        help="Commit message for the ModelScope upload.",
    )
    parser.add_argument(
        "--private",
        action="store_true",
        help="Create the dataset repo as private if it does not exist (default: public).",
    )
    parser.add_argument(
        "--skip-zip",
        action="store_true",
        help="Only upload the tree; skip benchmark_mds.zip.",
    )
    parser.add_argument(
        "--skip-tree",
        action="store_true",
        help="Only upload benchmark_mds.zip; skip the extracted tree.",
    )
    parser.add_argument(
        "--skip-docs",
        action="store_true",
        help=f"Skip uploading README.md / DATASHEET.md from {DATASET_RELEASE_DIR.relative_to(PROJECT_ROOT)}/.",
    )
    parser.add_argument(
        "--chinese-name",
        type=str,
        default="EALE: 智能体演化评测数据集",
        help="Chinese display name used when creating the dataset for the first time.",
    )
    parser.add_argument(
        "--license",
        type=str,
        default="CC-BY-4.0",
        help="License string used when creating the dataset for the first time.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    if args.skip_zip and args.skip_tree:
        raise SystemExit("--skip-zip and --skip-tree cannot both be set; nothing to upload.")

    repo_id = (args.repo or os.environ.get("BENCHMARK_MODELSCOPE_REPO", "")).strip() or DEFAULT_BENCHMARK_MODELSCOPE_REPO
    namespace, _, dataset_name = repo_id.partition("/")
    if not namespace or not dataset_name:
        raise SystemExit(f"Invalid repo id: {repo_id!r}; expected ``<namespace>/<dataset>``.")

    token = os.environ.get("MODELSCOPE_API_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "MODELSCOPE_API_TOKEN is required (SDK write token; set in .env). "
            "Generate one at https://modelscope.cn/my/myaccesstoken."
        )

    try:
        from modelscope.hub.api import HubApi  # noqa: PLC0415
    except ImportError as exc:
        raise SystemExit(
            "modelscope SDK is required. Install via `pip install modelscope`."
        ) from exc

    api = HubApi()
    api.login(token)

    _ensure_dataset(
        api,
        namespace=namespace,
        dataset_name=dataset_name,
        chinese_name=args.chinese_name,
        license_str=args.license,
        private=args.private,
    )

    with tempfile.TemporaryDirectory(prefix="benchmark_mds_upload_") as tmp:
        staging = Path(tmp) / "staging"
        staging.mkdir(parents=True, exist_ok=True)

        zip_path = _prepare_zip(args, staging_dir=Path(tmp))
        tree_root = _prepare_tree(args, zip_path=zip_path, staging_dir=Path(tmp))

        if not args.skip_tree:
            dest_tree = staging / EXTRACTED_DIR_IN_REPO
            print(f"[stage] tree: {tree_root} -> {dest_tree}")
            shutil.copytree(tree_root, dest_tree)

        if not args.skip_zip:
            dest_zip = staging / ZIP_PATH_IN_REPO
            print(f"[stage] zip:  {zip_path} -> {dest_zip}")
            shutil.copy2(zip_path, dest_zip)

        if not args.skip_docs:
            _stage_docs(staging)

        file_count = sum(1 for _ in staging.rglob("*") if _.is_file())
        print(f"[upload] {file_count} file(s) -> {repo_id}")
        api.upload_folder(
            repo_id=repo_id,
            folder_path=str(staging),
            repo_type="dataset",
            commit_message=args.commit_message,
        )

    print(f"[done] https://modelscope.cn/datasets/{repo_id}")


def _ensure_dataset(
    api,
    *,
    namespace: str,
    dataset_name: str,
    chinese_name: str,
    license_str: str,
    private: bool,
) -> None:
    """Idempotently create the dataset repo (ignore ``already exists``)."""
    try:
        api.create_dataset(
            dataset_name=dataset_name,
            namespace=namespace,
            chinese_name=chinese_name,
            license=license_str,
            visibility=1 if private else 5,
            description=(
                "EALE (Evaluating Agent Loaded Evolution): the benchmark suite of the LIFT framework."
            ),
        )
        print(f"[create_dataset] ok: {namespace}/{dataset_name}")
    except Exception as exc:  # noqa: BLE001 - SDK raises plain RuntimeError for duplicates
        msg = str(exc).lower()
        if any(keyword in msg for keyword in ("exists", "already", "duplicate")):
            print(f"[create_dataset] already exists, continuing: {namespace}/{dataset_name}")
            return
        raise


def _prepare_zip(args: argparse.Namespace, *, staging_dir: Path) -> Path:
    """Return a local zip path (either user-supplied, built from tree, or fetched from TOS)."""
    if args.zip is not None:
        zip_path = args.zip.expanduser().resolve()
        if not zip_path.is_file():
            raise SystemExit(f"--zip file does not exist: {zip_path}")
        return zip_path

    if args.local_tree is not None:
        source_tree = args.local_tree.expanduser().resolve()
        if not source_tree.is_dir():
            raise SystemExit(f"--local-tree directory does not exist: {source_tree}")
        return _build_zip_from_tree(source_tree, staging_dir / BENCHMARK_MDS_TOS_OBJECT_KEY)

    # Fallback A: reuse repo-local benchmark_mds/ if present.
    if BENCHMARK_MDS_DIR.is_dir() and any(BENCHMARK_MDS_DIR.iterdir()):
        return _build_zip_from_tree(BENCHMARK_MDS_DIR, staging_dir / BENCHMARK_MDS_TOS_OBJECT_KEY)

    # Fallback B: download from TOS.
    zip_path = staging_dir / BENCHMARK_MDS_TOS_OBJECT_KEY
    print(f"[fetch] {BENCHMARK_MDS_TOS_OBJECT_KEY} from TOS -> {zip_path}")
    download_benchmark_mds_zip(zip_path)
    return zip_path


def _prepare_tree(
    args: argparse.Namespace,
    *,
    zip_path: Path,
    staging_dir: Path,
) -> Path:
    """Return a local directory tree to upload as ``benchmark_mds/``."""
    if args.local_tree is not None:
        return args.local_tree.expanduser().resolve()

    if args.zip is None and BENCHMARK_MDS_DIR.is_dir() and any(BENCHMARK_MDS_DIR.iterdir()):
        return BENCHMARK_MDS_DIR

    extract_root = staging_dir / "extracted"
    extract_root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(extract_root)
    return _resolve_extracted_root(extract_root)


def _build_zip_from_tree(tree_root: Path, dest_zip: Path) -> Path:
    """Zip *tree_root* as ``benchmark_mds/...`` entries (matches TOS/HF layout)."""
    zip_bin = shutil.which("zip")
    if zip_bin is None:
        raise SystemExit("`zip` binary not found; install via `apt-get install zip`.")

    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    if dest_zip.exists():
        dest_zip.unlink()

    subprocess.run(
        [
            zip_bin,
            "-qr",
            str(dest_zip),
            "benchmark_mds",
            "-x",
            "**/__MACOSX/*",
            "-x",
            "**/.DS_Store",
        ],
        check=True,
        cwd=tree_root.parent,
    )
    print(f"[zip] built {dest_zip} from {tree_root}")
    return dest_zip


def _stage_docs(staging: Path) -> None:
    """Copy README.md / DATASHEET.md next to the dataset tree, if available."""
    for name in ("README.md", "DATASHEET.md"):
        source = DATASET_RELEASE_DIR / name
        if source.is_file():
            shutil.copy2(source, staging / name)
            print(f"[stage] doc:  {source} -> {staging / name}")


def _resolve_extracted_root(extract_dir: Path) -> Path:
    """Return the directory that contains benchmark scene folders (skip single wrapper folder)."""
    children = [p for p in extract_dir.iterdir() if p.name != "__MACOSX"]
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return extract_dir


if __name__ == "__main__":
    main()
