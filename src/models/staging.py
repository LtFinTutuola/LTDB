import enum
from typing import Optional
from sqlalchemy import Integer, String, JSON
from sqlalchemy.orm import Mapped, mapped_column
from src.models.base import Base, UUIDMixin, TimestampMixin


class JobStatus(enum.IntEnum):
    ACCEPTED = 1
    COMPLETED = 2
    ERROR = 3


class StagingArea(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "staging_area"

    status: Mapped[int] = mapped_column(Integer, nullable=False, default=JobStatus.ACCEPTED.value)
    file_path: Mapped[str] = mapped_column(String, nullable=False)
    data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    revision_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
