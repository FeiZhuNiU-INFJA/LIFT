"""Download ``benchmark_mds`` from ByteDance TOS before markdown → JSON preprocess."""

from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from pathlib import Path

import bytedtos
from dotenv import load_dotenv

from src.paths import BENCHMARK_MDS_DIR, BENCHMARK_MDS_TOS_BUCKET, BENCHMARK_MDS_TOS_OBJECT_KEY

load_dotenv()

# BOE region endpoint; override via ``TOS_ENDPOINT`` if needed.
DEFAULT_TOS_BOE_ENDPOINT = "tos-cn-north-boe.byted.org"


class BenchmarkMdsFetchError(RuntimeError):
    """Raised when benchmark markdown assets cannot be fetched or extracted."""


def _tos_credentials() -> bytedtos.StaticCredentials:
    access_key = os.environ.get("TOS_ACCESS_KEY", "").strip()
    secret_key = os.environ.get("TOS_SECRET_KEY", "").strip()
    missing = [name for name, value in (("TOS_ACCESS_KEY", access_key), ("TOS_SECRET_KEY", secret_key)) if not value]
    if missing:
        raise BenchmarkMdsFetchError(
            f"{', '.join(missing)} required to download benchmark_mds.zip from TOS. "
            "Set personal access key / secret key in .env "
            f"(bucket: {BENCHMARK_MDS_TOS_BUCKET})."
        )
    return bytedtos.StaticCredentials(access_key, secret_key)


def _tos_client() -> bytedtos.Client:
    endpoint = os.environ.get("TOS_ENDPOINT", DEFAULT_TOS_BOE_ENDPOINT).strip()
    kwargs: dict[str, object] = {}
    if endpoint:
        kwargs["endpoint"] = endpoint
    return bytedtos.Client(
        BENCHMARK_MDS_TOS_BUCKET,
        _tos_credentials(),
        **kwargs,
    )


def _resolve_extracted_root(extract_dir: Path) -> Path:
    """Return the directory that contains benchmark scene folders."""
    children = [path for path in extract_dir.iterdir() if path.name not in {"__MACOSX"}]
    if len(children) == 1 and children[0].is_dir():
        if children[0].name == "benchmark_mds":
            return children[0]
        # Zip may use a single top-level folder with another name.
        return children[0]
    return extract_dir


def download_benchmark_mds_zip(destination_zip: Path) -> Path:
    """Download ``benchmark_mds.zip`` from TOS to *destination_zip*."""
    destination_zip.parent.mkdir(parents=True, exist_ok=True)
    client = _tos_client()
    response = client.get_object(BENCHMARK_MDS_TOS_OBJECT_KEY)
    destination_zip.write_bytes(response.data)
    return destination_zip


def extract_benchmark_mds_zip(zip_path: Path, target_dir: Path) -> Path:
    """Extract *zip_path* into *target_dir* and return the resolved root."""
    with tempfile.TemporaryDirectory(prefix="benchmark_mds_extract_") as tmp:
        extract_dir = Path(tmp) / "extract"
        extract_dir.mkdir()
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(extract_dir)
        source_root = _resolve_extracted_root(extract_dir)
        if target_dir.exists():
            shutil.rmtree(target_dir)
        shutil.copytree(source_root, target_dir)
    return target_dir


def ensure_benchmark_mds(
    target_dir: Path | None = None,
    *,
    force: bool = False,
) -> Path:
    """Ensure markdown benchmark assets exist locally, downloading from TOS when needed."""
    resolved_target = (target_dir or BENCHMARK_MDS_DIR).resolve()
    if resolved_target.exists() and any(resolved_target.iterdir()) and not force:
        return resolved_target

    with tempfile.TemporaryDirectory(prefix="benchmark_mds_download_") as tmp:
        zip_path = Path(tmp) / BENCHMARK_MDS_TOS_OBJECT_KEY
        download_benchmark_mds_zip(zip_path)
        extract_benchmark_mds_zip(zip_path, resolved_target)
    return resolved_target
