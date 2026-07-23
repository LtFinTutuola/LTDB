from typing import Optional
from sqlalchemy.orm import Session
from src.models.staging import StagingArea


def create_job(db: Session, job_id: str, file_path: str) -> StagingArea:
    db_obj = StagingArea(id=job_id, file_path=file_path)
    db.add(db_obj)
    db.commit()
    db.refresh(db_obj)
    return db_obj


def get_job(db: Session, job_id: str) -> Optional[StagingArea]:
    return db.query(StagingArea).filter(StagingArea.id == job_id).first()


def get_job_by_status_and_path(db: Session, status: int, file_path: str) -> Optional[StagingArea]:
    return db.query(StagingArea).filter(
        StagingArea.status == status,
        StagingArea.file_path == file_path
    ).first()


def update_job(db: Session, job_id: str, status: int, data: dict) -> Optional[StagingArea]:
    db_obj = get_job(db, job_id)
    if db_obj:
        db_obj.status = status
        db_obj.data = data
        db.commit()
        db.refresh(db_obj)
    return db_obj


def delete_job(db: Session, job_id: str) -> None:
    db_obj = get_job(db, job_id)
    if db_obj:
        db.delete(db_obj)
        db.commit()
