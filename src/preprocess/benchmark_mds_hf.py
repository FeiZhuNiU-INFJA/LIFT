"""Download ``benchmark_mds`` from a HuggingFace dataset repo as an alternative to TOS."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from huggingface_hub import hf_hub_download
from huggingface_hub.utils import HfHubHTTPError

from src.paths import BENCHMARK_MDS_DIR, BENCHMARK_MDS_HF_PATH_IN_REPO
from src.preprocess.benchmark_mds_fetch import extract_benchmark_mds_zip

load_dotenv()


class BenchmarkMdsHfFetchError(RuntimeError):
    """Raised when benchmark markdown assets cannot be fetched from HuggingFace."""


def _hf_repo_id() -> str:
    repo_id = os.environ.get("BENCHMARK_HF_REPO", "").strip()
    if not repo_id:
        raise BenchmarkMdsHfFetchError(
            "BENCHMARK_HF_REPO required to download benchmark_mds.zip from HuggingFace. "
            "Set it in .env (e.g. BENCHMARK_HF_REPO=<user-or-org>/<dataset-name>)."
        )
    return repo_id


def download_benchmark_mds_zip_from_hf(destination_zip: Path) -> Path:
    """Download ``benchmark_mds.zip`` from the configured HuggingFace dataset repo."""
    destination_zip.parent.mkdir(parents=True, exist_ok=True)
    repo_id = _hf_repo_id()
    token = os.environ.get("HF_TOKEN", "").strip() or None
    try:
        cached_path = hf_hub_download(
            repo_id=repo_id,
            filename=BENCHMARK_MDS_HF_PATH_IN_REPO,
            repo_type="dataset",
            token=token,
        )
    except HfHubHTTPError as exc:
        raise BenchmarkMdsHfFetchError(
            f"Failed to download {BENCHMARK_MDS_HF_PATH_IN_REPO} from HuggingFace dataset "
            f"{repo_id}: {exc}"
        ) from exc
    shutil.copyfile(cached_path, destination_zip)
    return destination_zip


def ensure_benchmark_mds_from_hf(
    target_dir: Path | None = None,
    *,
    force: bool = False,
) -> Path:
    """Ensure markdown benchmark assets exist locally, downloading from HuggingFace when needed."""
    resolved_target = (target_dir or BENCHMARK_MDS_DIR).resolve()
    if resolved_target.exists() and any(resolved_target.iterdir()) and not force:
        return resolved_target

    with tempfile.TemporaryDirectory(prefix="benchmark_mds_hf_download_") as tmp:
        zip_path = Path(tmp) / BENCHMARK_MDS_HF_PATH_IN_REPO
        download_benchmark_mds_zip_from_hf(zip_path)
        extract_benchmark_mds_zip(zip_path, resolved_target)
    return resolved_target
