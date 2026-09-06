import enum
from typing import Optional
from sqlalchemy import Integer, String, JSON
from sqlalchemy.orm import Mapped, mapped_column
from src.models.base import Base, UUIDMixin, TimestampMixin


class JobStatus(enum.IntEnum):
    ACCEPTED = 1
    COMPLETED = 2
    ERROR = 3


class JobType(str, enum.Enum):
    DDT_IMPORT = "ddt_import"
    SINGLE_ITEM_IMPORT = "single_item_import"
    HEURISTIC_DEDUCTION = "heuristic_deduction"


class StagingArea(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "staging_area"

    status: Mapped[int] = mapped_column(Integer, nullable=False, default=JobStatus.ACCEPTED.value)
    job_type: Mapped[str] = mapped_column(String, nullable=False, default=JobType.DDT_IMPORT.value)
    file_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    revision_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
