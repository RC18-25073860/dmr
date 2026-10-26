import base64
import tempfile
from pathlib import Path

import pytest

from backend.service import DMRStore


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def archive_initial(store: DMRStore, *, module: str = "工艺文件") -> str:
    batch = store.analyze([{"name": "SOP-01-041_V1.0.pdf", "data": b64(b"initial")}])
    item = batch["items"][0]
    reviewed = store.review({
        "batch_id": batch["batch_id"],
        "item_id": item["id"],
        "expected_edit_token": item["edit_token"],
        "code": "SOP-01-041",
        "registrant": "深圳由莱",
        "product": "UI04",
        "module": module,
        "type": module,
        "project_category": "医疗",
        "version": "V1.0",
    })["item"]
    assert reviewed["status"] == "matched"
    assert store.commit_batch(batch["batch_id"])["committed"] == 1
    return store.execute("SELECT id FROM records WHERE business_code='SOP-01-041'").fetchone()["id"]


def test_process_doc_same_version_revision_requires_explicit_note_and_keeps_history():
    with tempfile.TemporaryDirectory() as tmp:
        store = DMRStore(Path(tmp).resolve())
        try:
            record_id = archive_initial(store)
            batch = store.analyze([{"name": "SOP-01-041_V1.0.pdf", "data": b64(b"revised content")}])
            item = batch["items"][0]

            with pytest.raises(ValueError, match="同版本修订确认必须填写核对说明"):
                store.review({
                    "batch_id": batch["batch_id"],
                    "item_id": item["id"],
                    "expected_edit_token": item["edit_token"],
                    "record_id": record_id,
                    "version": "V1.0",
                    "confirm_same_version_revision": True,
                })

            reviewed = store.review({
                "batch_id": batch["batch_id"],
                "item_id": item["id"],
                "expected_edit_token": item["edit_token"],
                "record_id": record_id,
                "version": "V1.0",
                "confirm_same_version_revision": True,
                "review_note": "新版工艺说明仅修订内容，版本保持 V1.0",
            })["item"]
            assert reviewed["status"] == "matched"
            assert reviewed["metadata"]["same_version_revision"]["base_version"] == "V1.0"

            result = store.commit_batch(batch["batch_id"])
            assert result["committed"] == 1
            files = store.execute("SELECT status,version,sha256 FROM files WHERE record_id=? ORDER BY created_at", (record_id,)).fetchall()
            assert [row["status"] for row in files] == ["历史版本", "当前有效"]
            assert {row["version"] for row in files} == {"V1.0"}
            assert len({row["sha256"] for row in files}) == 2
        finally:
            store.close()


def test_software_same_version_revision_is_not_allowed_by_confirmation_flag():
    with tempfile.TemporaryDirectory() as tmp:
        store = DMRStore(Path(tmp).resolve())
        try:
            record_id = archive_initial(store, module="软件")
            batch = store.analyze([{"name": "SOP-01-041_V1.0.hex", "data": b64(b"software revision")}])
            item = batch["items"][0]
            reviewed = store.review({
                "batch_id": batch["batch_id"],
                "item_id": item["id"],
                "expected_edit_token": item["edit_token"],
                "record_id": record_id,
                "version": "V1.0",
                "confirm_same_version_revision": True,
                "review_note": "软件不应走同版本修订替换",
            })["item"]
            assert reviewed["status"] == "conflict"
            assert "仅允许工艺文件或检验文件" in reviewed["recommendation"]
            assert store.commit_batch(batch["batch_id"])["blocked"] == 1
        finally:
            store.close()


def test_same_version_revision_revalidates_base_file_before_commit():
    with tempfile.TemporaryDirectory() as tmp:
        store = DMRStore(Path(tmp).resolve())
        try:
            record_id = archive_initial(store)
            batch = store.analyze([{"name": "SOP-01-041_V1.0.pdf", "data": b64(b"revised content")}])
            item = batch["items"][0]
            reviewed = store.review({
                "batch_id": batch["batch_id"],
                "item_id": item["id"],
                "expected_edit_token": item["edit_token"],
                "record_id": record_id,
                "version": "V1.0",
                "confirm_same_version_revision": True,
                "review_note": "确认同版本修订",
            })["item"]
            assert reviewed["status"] == "matched"

            store.execute("UPDATE files SET sha256='changed-after-review' WHERE record_id=? AND status='当前有效'", (record_id,))
            result = store.commit_batch(batch["batch_id"])
            assert result["blocked"] == 1
            assert "同版本但文件内容不同" in result["results"][0]["message"]
        finally:
            store.close()
