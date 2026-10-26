from __future__ import annotations

import uuid
from pathlib import Path


def store_blob(files_dir: Path, data_dir: Path, data: bytes, filename: str) -> tuple[str, str, int]:
    blob_id = str(uuid.uuid4())
    suffix = Path(filename).suffix[:20]
    target = files_dir / f"{blob_id}{suffix}"
    target.write_bytes(data)
    return blob_id, str(target.relative_to(data_dir)), len(data)


def read_blob(data_dir: Path, rel_path: str) -> bytes:
    path = (data_dir / rel_path).resolve()
    try:
        path.relative_to(data_dir)
    except ValueError as exc:
        raise FileNotFoundError("非法文件路径") from exc
    return path.read_bytes()
