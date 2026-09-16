from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import func
from src.models.pim import ArticlePhoto
from src.core.logger import get_logger

logger = get_logger()


class ArticlePhotoRepository:
    """Repository for ArticlePhoto CRUD operations."""

    def get_photo_by_blueprint_and_color(
        self, db: Session, blueprint_id: str, color_name: str
    ) -> Optional[ArticlePhoto]:
        """
        Exact case-insensitive lookup on (blueprint_id, canonical_color_name).
        Used for Caso A and Caso B.a resolution.
        """
        return (
            db.query(ArticlePhoto)
            .filter(
                ArticlePhoto.article_blueprint_id == blueprint_id,
                func.lower(ArticlePhoto.canonical_color_name) == color_name.lower(),
            )
            .first()
        )

    def get_all_photos_by_blueprint(
        self, db: Session, blueprint_id: str
    ) -> List[ArticlePhoto]:
        """
        Retrieve all photos for a given blueprint.
        Used for Gallery B2 in Sub-flow B.b.
        """
        return (
            db.query(ArticlePhoto)
            .filter(ArticlePhoto.article_blueprint_id == blueprint_id)
            .all()
        )

    def get_photo_by_id(self, db: Session, photo_id: str) -> Optional[ArticlePhoto]:
        """Lookup by primary key. Used by the photo serve endpoint."""
        return db.query(ArticlePhoto).filter(ArticlePhoto.id == photo_id).first()

    def create_photo(
        self,
        db: Session,
        blueprint_id: str,
        color_name: str,
        photo_data: bytes,
        commit_changes: bool = True,
    ) -> Optional[ArticlePhoto]:
        """
        Create a new ArticlePhoto record.
        If a photo for the same blueprint + color already exists (UniqueConstraint),
        the existing record is returned without creating a duplicate.
        """
        existing = self.get_photo_by_blueprint_and_color(db, blueprint_id, color_name)
        if existing:
            logger.log_execution(
                "photo_repo", "photo_create_skipped_duplicate", "ok",
                blueprint_id=blueprint_id, color_name=color_name,
            )
            return existing

        photo = ArticlePhoto(
            article_blueprint_id=blueprint_id,
            canonical_color_name=color_name.lower(),
            photo_data=photo_data,
        )
        db.add(photo)
        if commit_changes:
            db.commit()
            db.refresh(photo)
        else:
            db.flush()

        logger.log_execution(
            "photo_repo", "photo_created", "ok",
            photo_id=str(photo.id), blueprint_id=blueprint_id, color_name=color_name,
        )
        return photo


photo_repo = ArticlePhotoRepository()
