from __future__ import annotations

from sqlalchemy import Column, Integer, MetaData, String, Table, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


metadata = MetaData()


class Base(DeclarativeBase):
    metadata = metadata


class Record(Base):
    __tablename__ = "records"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    business_code: Mapped[str] = mapped_column(String, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    registrant: Mapped[str] = mapped_column(String, index=True, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    project_category: Mapped[str] = mapped_column(String, nullable=False, default="待确认")
    project_category_source: Mapped[str | None] = mapped_column(String)
    product: Mapped[str] = mapped_column(String, index=True, nullable=False)
    module: Mapped[str] = mapped_column(String, index=True, nullable=False)
    version: Mapped[str] = mapped_column(String, nullable=False)
    previous: Mapped[str | None] = mapped_column(String)
    file_name: Mapped[str | None] = mapped_column(String)
    date: Mapped[str | None] = mapped_column(String)
    controlled: Mapped[str | None] = mapped_column(String)
    source: Mapped[str | None] = mapped_column(String)
    owner: Mapped[str | None] = mapped_column(String)
    size: Mapped[str | None] = mapped_column(String)
    source_type: Mapped[str | None] = mapped_column(String)
    linked_bom: Mapped[str | None] = mapped_column(String)
    metadata_json: Mapped[str | None] = mapped_column("metadata", Text)
    created_at: Mapped[str | None] = mapped_column(String)
    updated_at: Mapped[str | None] = mapped_column(String)
    edit_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class LibraryFile(Base):
    __tablename__ = "files"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    record_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, index=True, nullable=False)
    date: Mapped[str | None] = mapped_column(String)
    source: Mapped[str | None] = mapped_column(String)
    size: Mapped[str | None] = mapped_column(String)
    type: Mapped[str | None] = mapped_column(String)
    storage: Mapped[str | None] = mapped_column(String)
    sha256: Mapped[str] = mapped_column(String, nullable=False)
    blob_path: Mapped[str] = mapped_column(String, nullable=False)
    metadata_json: Mapped[str | None] = mapped_column("metadata", Text)
    linked_bom: Mapped[str | None] = mapped_column(String)
    link_status: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[str | None] = mapped_column(String)


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)


class BatchItem(Base):
    __tablename__ = "batch_items"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    batch_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    file_name: Mapped[str] = mapped_column(String, nullable=False)
    staging_file_id: Mapped[str | None] = mapped_column(String)
    record_id: Mapped[str | None] = mapped_column(String)
    context_record_id: Mapped[str | None] = mapped_column(String)
    code: Mapped[str | None] = mapped_column(String)
    registrant: Mapped[str | None] = mapped_column(String)
    type: Mapped[str | None] = mapped_column(String)
    project_category: Mapped[str | None] = mapped_column(String)
    project_category_source: Mapped[str | None] = mapped_column(String)
    product: Mapped[str | None] = mapped_column(String)
    module: Mapped[str | None] = mapped_column(String)
    version: Mapped[str | None] = mapped_column(String)
    linked_bom: Mapped[str | None] = mapped_column(String)
    notes: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[str | None] = mapped_column("metadata", Text)
    current_version: Mapped[str | None] = mapped_column(String)
    detected_version: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False)
    recommendation: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String, nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    result_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str | None] = mapped_column(String)
    updated_at: Mapped[str | None] = mapped_column(String)
    edit_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ExceptionRecord(Base):
    __tablename__ = "exceptions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    record_id: Mapped[str | None] = mapped_column(String)
    item_id: Mapped[str | None] = mapped_column(String)
    batch_id: Mapped[str | None] = mapped_column(String)
    record_no: Mapped[str | None] = mapped_column(String)
    recommendation: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str | None] = mapped_column(String)
    resolved_at: Mapped[str | None] = mapped_column(String)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    time: Mapped[str | None] = mapped_column(String)
    title: Mapped[str | None] = mapped_column(String)
    detail: Mapped[str | None] = mapped_column(Text)
    audience: Mapped[str | None] = mapped_column(Text)
    product: Mapped[str | None] = mapped_column(String)
    read: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str | None] = mapped_column(String)


class OperationLog(Base):
    __tablename__ = "logs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    time: Mapped[str | None] = mapped_column(String)
    user: Mapped[str | None] = mapped_column(String)
    action: Mapped[str | None] = mapped_column(String)
    object: Mapped[str | None] = mapped_column(String)
    change: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String)
    result: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[str | None] = mapped_column(String)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
