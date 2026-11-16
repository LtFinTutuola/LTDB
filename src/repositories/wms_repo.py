from typing import List, Optional, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import func
from src.models.wms import Article, ArticleMovement, ArticlePrice
from src.models.pim import ArticleBlueprint
from src.schemas.wms import ArticleCreate, StockUpdate
from src.repositories.base import BaseRepository

class ArticleRepository(BaseRepository[Article, ArticleCreate, ArticleCreate]):
    def __init__(self):
        super().__init__(Article)

    def get_by_ean(self, db: Session, ean: str) -> List[Article]:
        return db.query(self.model).filter(self.model.ean == ean).all()

    def get_by_supplier_code(self, db: Session, supplier_code: str) -> List[Article]:
        return db.query(self.model).filter(self.model.supplier_code == supplier_code).all()

    def get_article_by_supplier_code_and_brand(
        self, db: Session, brand_id: str, supplier_code: str
    ) -> Optional[Article]:
        """
        Lookup an Article by its raw supplier_code (vendor code including color code),
        scoped to a specific brand via the ArticleBlueprint join.
        Used for Caso A photo resolution.
        """
        return (
            db.query(Article)
            .join(ArticleBlueprint, Article.article_blueprint_id == ArticleBlueprint.id)
            .filter(
                ArticleBlueprint.brand_id == brand_id,
                Article.supplier_code == supplier_code,
            )
            .first()
        )

    def get_articles_by_supplier_code_and_brand(
        self, db: Session, brand_id: str, supplier_code: str
    ) -> List[Article]:
        """
        Lookup all Articles by raw supplier_code scoped to a specific brand via ArticleBlueprint.
        Used by two-phase SQL lookup and color safeguard checks.
        """
        return (
            db.query(Article)
            .join(ArticleBlueprint, Article.article_blueprint_id == ArticleBlueprint.id)
            .filter(
                ArticleBlueprint.brand_id == brand_id,
                Article.supplier_code == supplier_code,
            )
            .order_by(Article.created_at.asc())
            .all()
        )

    def replace_color_across_brand_articles(
        self,
        db: Session,
        brand_id: str,
        old_colors: List[str],
        new_colors: List[str],
        commit_changes: bool = False,
    ) -> int:
        """
        Retroactively replace an erroneous color set across all WMS Articles of a brand
        and align associated ArticlePhoto.canonical_color_name values.
        """
        from src.core.logger import get_logger
        from src.models.pim import ArticlePhoto

        logger = get_logger()
        old_set = {str(c).strip().lower() for c in (old_colors or []) if c and str(c).strip()}
        new_list = list(dict.fromkeys(
            str(c).strip().lower() for c in (new_colors or []) if c and str(c).strip()
        ))
        if not brand_id or not old_set or not new_list or old_set == set(new_list):
            return 0

        articles = (
            db.query(Article)
            .join(ArticleBlueprint, Article.article_blueprint_id == ArticleBlueprint.id)
            .filter(ArticleBlueprint.brand_id == brand_id)
            .all()
        )

        updated_count = 0
        affected_bp_ids: set[str] = set()
        for art in articles:
            art_set = {str(c).strip().lower() for c in (art.colors or []) if c and str(c).strip()}
            if art_set == old_set:
                art.colors = list(new_list)
                updated_count += 1
                affected_bp_ids.add(str(art.article_blueprint_id))

        if affected_bp_ids:
            target_photo_color = new_list[0]
            photos = (
                db.query(ArticlePhoto)
                .filter(ArticlePhoto.article_blueprint_id.in_(list(affected_bp_ids)))
                .all()
            )
            by_bp: dict[str, list[ArticlePhoto]] = {}
            for ph in photos:
                by_bp.setdefault(str(ph.article_blueprint_id), []).append(ph)
            for bp_photos in by_bp.values():
                has_target = any(
                    (p.canonical_color_name or "").strip().lower() == target_photo_color
                    for p in bp_photos
                )
                for ph in bp_photos:
                    c_low = (ph.canonical_color_name or "").strip().lower()
                    if c_low in old_set and c_low != target_photo_color:
                        if has_target:
                            db.delete(ph)
                        else:
                            ph.canonical_color_name = target_photo_color
                            has_target = True

        if updated_count > 0:
            if commit_changes:
                db.commit()
            else:
                db.flush()
            logger.log_execution(
                "wms_repo",
                "retroactive_color_replaced",
                "ok",
                brand_id=brand_id,
                old_colors=sorted(old_set),
                new_colors=new_list,
                updated_count=updated_count,
            )
        return updated_count

    def get_current_stock(self, db: Session, article_id: str) -> int:
        """
        Calculates the aggregated physical stock by reading the append-only ledger.
        """
        result = (
            db.query(func.sum(ArticleMovement.reason.property.mapper.class_.sign)) # Needs explicit join
            .join(ArticleMovement.reason)
            .filter(ArticleMovement.article_id == article_id)
            .scalar()
        )
        return result or 0

    def add_movement(self, db: Session, movement_in: StockUpdate, commit_changes: bool = True) -> ArticleMovement:
        """
        Adds a new movement to the append-only ledger.
        """
        db_movement = ArticleMovement(
            article_id=movement_in.article_id,
            reason_id=movement_in.reason_id,
            notes=movement_in.notes
        )
        db.add(db_movement)
        if commit_changes:
            db.commit()
            db.refresh(db_movement)
        else:
            db.flush()
        return db_movement
    def get_reconciliation_candidates(self, db: Session, search_code: str, is_exchange: bool) -> List[Article]:
        from src.models.wms import ArticleStatus
        from sqlalchemy import or_, func
        from src.utils.vendor_code_normalizer import sterilize_separators

        target_status = ArticleStatus.SOLD if is_exchange else ArticleStatus.AVAILABLE
        clean_code = (search_code or "").strip()
        if not clean_code:
            return []

        code_lower = clean_code.lower()
        sterilized_lower = sterilize_separators(clean_code).lower()

        return (
            db.query(self.model)
            .filter(
                or_(
                    func.lower(self.model.supplier_code) == code_lower,
                    func.lower(self.model.ean) == code_lower,
                    func.lower(self.model.supplier_code) == sterilized_lower,
                ),
                self.model.status == target_status
            )
            .order_by(self.model.created_at.asc())
            .all()
        )

wms_repo = ArticleRepository()
