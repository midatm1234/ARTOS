"""Compute snapshots and a file-level data/artifact catalog.

This is the v0.1 inventory surface implemented minimally: compute facts come
from the platform plus ``nvidia-smi``; data resources are files fingerprinted
with :func:`artos.utils.lightweight_file_fingerprint`. Semantic metadata
extraction (NetCDF variables, coverage) remains on the v0.2 roadmap.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterator

from .store import Store
from .utils import atomic_write_json, lightweight_file_fingerprint, sha256_bytes, utc_now

KIND_BY_SUFFIX = {
    ".nc": "dataset", ".nc4": "dataset", ".zarr": "dataset", ".h5": "dataset", ".hdf5": "dataset",
    ".csv": "dataset", ".parquet": "dataset", ".tif": "dataset", ".tiff": "dataset",
    ".pt": "checkpoint", ".ckpt": "checkpoint", ".safetensors": "checkpoint",
    ".yaml": "config", ".yml": "config", ".json": "artifact",
}


def refresh_compute(root: Path, state_dir: Path) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "captured_at": utc_now(),
        "host": platform.node(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "python": platform.python_version(),
        "gpus": [],
    }
    exe = shutil.which("nvidia-smi")
    if exe:
        try:
            out = subprocess.run(
                [exe, "--query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=15, check=True).stdout
            for line in out.splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) == 6:
                    snapshot["gpus"].append(dict(zip(("index", "name", "memory_total_mib", "memory_used_mib", "memory_free_mib", "utilization_pct"), parts)))
        except (subprocess.SubprocessError, OSError) as exc:
            snapshot["gpu_error"] = str(exc)
    atomic_write_json(Path(state_dir) / "compute" / "latest.json", snapshot)
    return snapshot


def discover_paths(root: Path, max_files: int) -> Iterator[tuple[Path, str]]:
    count = 0
    root = Path(root)
    if root.is_file():
        yield root, KIND_BY_SUFFIX.get(root.suffix.lower(), "file")
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in sorted(filenames):
            path = Path(dirpath) / name
            yield path, KIND_BY_SUFFIX.get(path.suffix.lower(), "file")
            count += 1
            if count >= max_files:
                return


def register_path(store: Store, path: Path, hash_limit_mb: int = 64, max_files: int = 250000, kind: str | None = None, verify_content: bool = True) -> dict[str, Any]:
    path = Path(path).resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    if path.is_dir():
        files = list(discover_paths(path, max_files))
        resource = {"resource_id": "dir-" + sha256_bytes(str(path).encode())[:16], "kind": kind or "collection",
                    "path": str(path), "file_count": len(files), "registered_at": utc_now()}
    else:
        st = path.stat()
        fingerprint, method = lightweight_file_fingerprint(path, hash_limit_mb * 1024 * 1024) if verify_content else ("", "none")
        resource = {"resource_id": "file-" + sha256_bytes(str(path).encode())[:16],
                    "kind": kind or KIND_BY_SUFFIX.get(path.suffix.lower(), "file"), "path": str(path),
                    "size_bytes": st.st_size, "mtime": st.st_mtime, "fingerprint": fingerprint,
                    "fingerprint_method": method, "registered_at": utc_now()}
    store.upsert_resource(resource)
    return resource


def refresh_data_catalog(store: Store, state_dir: Path, roots: list[Path], max_files: int, hash_limit_mb: int) -> dict[str, Any]:
    scanned = 0
    for root in roots:
        if not Path(root).exists():
            continue
        for path, kind in discover_paths(Path(root), max_files - scanned):
            register_path(store, path, hash_limit_mb, max_files, kind)
            scanned += 1
    summary = {"scanned_at": utc_now(), "roots": [str(r) for r in roots], "files": scanned}
    atomic_write_json(Path(state_dir) / "data" / "last_scan.json", summary)
    export_catalog(store, state_dir)
    return summary


def export_catalog(store: Store, state_dir: Path) -> dict[str, Any]:
    resources = store.list_resources()
    catalog = {"exported_at": utc_now(), "count": len(resources), "resources": resources}
    atomic_write_json(Path(state_dir) / "data" / "catalog.json", catalog)
    return {"count": len(resources)}
