"""One-off uploader: mirror ``benchmark_mds.zip`` from TOS to a HuggingFace dataset repo.

Usage (run from project root):

    conda run -n lift python scripts/upload_benchmark_to_hf.py
    conda run -n lift python scripts/upload_benchmark_to_hf.py --zip path/to/benchmark_mds.zip

Requires the following ``.env`` entries:
    TOS_ACCESS_KEY / TOS_SECRET_KEY  (only if downloading from TOS; not needed with --zip)
    HF_TOKEN                         (write-permission token)
    BENCHMARK_HF_REPO                (e.g. ``<user-or-org>/<dataset-name>``)
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import zipfile
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from huggingface_hub import HfApi  # noqa: E402

from src.paths import (  # noqa: E402
    BENCHMARK_MDS_HF_PATH_IN_REPO,
    BENCHMARK_MDS_TOS_OBJECT_KEY,
    DEFAULT_BENCHMARK_HF_REPO,
)
from src.preprocess.benchmark_mds_fetch import download_benchmark_mds_zip  # noqa: E402

EXTRACTED_DIR_IN_REPO = "benchmark_mds"
"""HuggingFace 仓库内解压后浏览目录（仅用于网页预览，下载仍走 zip）。"""

load_dotenv()

# huggingface_hub uses httpx, which reads proxy env vars in UPPERCASE only.
# Mirror the lowercase variants users typically set (e.g. byted internal proxy)
# so this script works in environments without direct outbound access.
for _lower, _upper in (("http_proxy", "HTTP_PROXY"), ("https_proxy", "HTTPS_PROXY"), ("no_proxy", "NO_PROXY")):
    if os.environ.get(_lower) and not os.environ.get(_upper):
        os.environ[_upper] = os.environ[_lower]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--zip",
        type=Path,
        default=None,
        help="Local benchmark_mds.zip to upload. If omitted, fresh copy is downloaded from TOS.",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="Override BENCHMARK_HF_REPO env (e.g. user/dataset).",
    )
    parser.add_argument(
        "--commit-message",
        type=str,
        default="Update benchmark_mds.zip",
        help="Commit message for the HF upload.",
    )
    parser.add_argument(
        "--private",
        action="store_true",
        help="Create the dataset repo as private if it does not exist (default: public).",
    )
    parser.add_argument(
        "--skip-extracted",
        action="store_true",
        help=(
            "Only upload benchmark_mds.zip. By default the script also uploads the unzipped "
            f"tree under '{EXTRACTED_DIR_IN_REPO}/' so users can preview markdowns on the web."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    repo_id = (args.repo or os.environ.get("BENCHMARK_HF_REPO", "")).strip() or DEFAULT_BENCHMARK_HF_REPO

    token = os.environ.get("HF_TOKEN", "").strip()
    if not token:
        raise SystemExit("HF_TOKEN is required (write-permission token; set in .env).")

    api = HfApi(token=token)
    api.create_repo(
        repo_id=repo_id,
        repo_type="dataset",
        private=args.private,
        exist_ok=True,
    )

    if args.zip is not None:
        zip_path = args.zip.expanduser().resolve()
        if not zip_path.is_file():
            raise SystemExit(f"--zip file does not exist: {zip_path}")
        _upload(api, repo_id, zip_path, args.commit_message, upload_extracted=not args.skip_extracted)
        return

    with tempfile.TemporaryDirectory(prefix="benchmark_mds_upload_") as tmp:
        zip_path = Path(tmp) / BENCHMARK_MDS_TOS_OBJECT_KEY
        print(f"Downloading {BENCHMARK_MDS_TOS_OBJECT_KEY} from TOS to {zip_path} ...")
        download_benchmark_mds_zip(zip_path)
        _upload(api, repo_id, zip_path, args.commit_message, upload_extracted=not args.skip_extracted)


def _upload(
    api: HfApi,
    repo_id: str,
    zip_path: Path,
    commit_message: str,
    *,
    upload_extracted: bool,
) -> None:
    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"Uploading {zip_path.name} ({size_mb:.2f} MB) to dataset {repo_id} ...")
    api.upload_file(
        path_or_fileobj=str(zip_path),
        path_in_repo=BENCHMARK_MDS_HF_PATH_IN_REPO,
        repo_id=repo_id,
        repo_type="dataset",
        commit_message=commit_message,
    )

    if upload_extracted:
        _upload_extracted_tree(api, repo_id, zip_path, commit_message)

    print(f"Done. https://huggingface.co/datasets/{repo_id}")


def _upload_extracted_tree(
    api: HfApi,
    repo_id: str,
    zip_path: Path,
    commit_message: str,
) -> None:
    """Mirror the unzipped tree under ``benchmark_mds/`` for in-browser preview."""
    with tempfile.TemporaryDirectory(prefix="benchmark_mds_extract_") as tmp:
        extract_root = Path(tmp)
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(extract_root)

        source_root = _resolve_extracted_root(extract_root)
        file_count = sum(1 for _ in source_root.rglob("*") if _.is_file())
        print(
            f"Uploading extracted tree ({file_count} files) to "
            f"'{EXTRACTED_DIR_IN_REPO}/' for web preview ..."
        )
        api.upload_folder(
            folder_path=str(source_root),
            path_in_repo=EXTRACTED_DIR_IN_REPO,
            repo_id=repo_id,
            repo_type="dataset",
            commit_message=f"{commit_message} (extracted preview)",
            ignore_patterns=["__MACOSX/**", ".DS_Store", "**/.DS_Store"],
        )


def _resolve_extracted_root(extract_dir: Path) -> Path:
    """Return the directory that contains benchmark scene folders (skip single wrapper folder)."""
    children = [p for p in extract_dir.iterdir() if p.name != "__MACOSX"]
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return extract_dir


if __name__ == "__main__":
    main()
