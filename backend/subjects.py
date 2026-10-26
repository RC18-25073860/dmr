"""Subject constraints for explicit platform-created archive records."""
from __future__ import annotations

from fastapi import HTTPException

from backend.service import clean_text


ALLOWED_ARCHIVE_SUBJECTS = {
    "深圳金茉",
    "广西由莱",
    "深圳由莱",
    "新加坡由莱",
    "越南由莱",
    "新加坡实业",
}


def validate_new_archive_subject(value: object) -> str:
    subject = clean_text(value)
    if subject not in ALLOWED_ARCHIVE_SUBJECTS:
        allowed = "、".join(sorted(ALLOWED_ARCHIVE_SUBJECTS))
        raise HTTPException(400, f"新归档主体只能选择：{allowed}")
    return subject
