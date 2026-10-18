from typing import Optional
from fastapi import HTTPException, status
from sqlalchemy.orm import Session, joinedload
from src.models.pim import ArticleBlueprint, Brand, Category, BrandHeuristic
from src.schemas.pim import ArticleBlueprintCreate, ArticleBlueprintResponse  # placeholder for update schema
from src.repositories.base import BaseRepository
from src.core.logger import get_logger

logger = get_logger()

class ArticleBlueprintRepository(BaseRepository[ArticleBlueprint, ArticleBlueprintCreate, ArticleBlueprintCreate]):
    def __init__(self):
        super().__init__(ArticleBlueprint)

    def get_embeddings_by_brand(self, db: Session, brand_id: str) -> list[dict]:
        """
        Retrieve lightweight dictionary list of all blueprints for a brand where embedding is not null.
        """
        blueprints = (
            db.query(
                ArticleBlueprint.id,
                ArticleBlueprint.embedding,
                ArticleBlueprint.category_id,
                ArticleBlueprint.article_name,
                ArticleBlueprint.description,
                ArticleBlueprint.dimensions
            )
            .filter(ArticleBlueprint.brand_id == brand_id)
            .filter(ArticleBlueprint.embedding.isnot(None))
            .all()
        )
        result = [
            {
                "id": str(bp.id),
                "embedding": bp.embedding,
                "category_id": str(bp.category_id) if bp.category_id else None,
                "article_name": bp.article_name,
                "description": bp.description,
                "dimensions": bp.dimensions
            }
            for bp in blueprints
        ]
        logger.log_execution("pim_repo", "db_embeddings_fetched", "ok", brand_id=brand_id, count=len(result))
        return result

    def save_embedding(self, db: Session, blueprint_id: str, embedding: list[float], commit_changes: bool = True) -> None:
        """
        Update embedding vector for an existing article blueprint.
        """
        logger.log_execution("pim_repo", "embedding_saved", "ok", blueprint_id=blueprint_id, vector_dim=len(embedding) if embedding else 0)
        bp = db.query(ArticleBlueprint).filter(ArticleBlueprint.id == blueprint_id).first()
        if bp:
            bp.embedding = embedding
            if commit_changes:
                db.commit()
            else:
                db.flush()

    def get_blueprint_by_normalized_code(
        self, db: Session, brand_id: str, normalized_vendor_code: str
    ) -> Optional[ArticleBlueprint]:
        """
        Deterministic lookup by composite unique key (brand_id, normalized_vendor_code).
        Returns the ArticleBlueprint or None if not found.
        """
        return (
            db.query(ArticleBlueprint)
            .filter(
                ArticleBlueprint.brand_id == brand_id,
                ArticleBlueprint.normalized_vendor_code == normalized_vendor_code,
            )
            .first()
        )

    def get_all_embeddings(self, db: Session) -> list[dict]:
        """
        Retrieve lightweight dict list of ALL blueprints (cross-brand) where embedding is not null.
        Used for cross-brand similarity search.

        Note: Uses both SQL and Python-level filtering because SQLite JSON columns
        may not respond reliably to .isnot(None) for JSON-typed NULL values.
        """
        blueprints = (
            db.query(
                ArticleBlueprint.id,
                ArticleBlueprint.embedding,
                ArticleBlueprint.category_id,
                ArticleBlueprint.article_name,
                ArticleBlueprint.description,
                ArticleBlueprint.dimensions,
            )
            .filter(ArticleBlueprint.embedding.isnot(None))
            .all()
        )
        result = [
            {
                "id": str(bp.id),
                "embedding": bp.embedding,
                "category_id": str(bp.category_id) if bp.category_id else None,
                "article_name": bp.article_name,
                "description": bp.description,
                "dimensions": bp.dimensions,
            }
            for bp in blueprints
            if bp.embedding is not None  # Python-level safety net for SQLite JSON null quirk
        ]
        logger.log_execution("pim_repo", "db_all_embeddings_fetched", "ok", count=len(result))
        return result

    def search_by_filters(self, db: Session, filters: dict) -> list[ArticleBlueprint]:
        """
        Dynamic filter-based query on ArticleBlueprint.
        Supported filter keys: brand_name (ilike), category_name (ilike).
        Additional JSON-field filtering (colors, tags, status) is handled
        in Python by the search_service layer after this call.
        """
        from src.models.pim import Brand, Category  # local import to avoid circularity

        query = db.query(ArticleBlueprint).join(Brand, ArticleBlueprint.brand_id == Brand.id)

        brand_name = filters.get("brand_name")
        if brand_name:
            query = query.filter(Brand.name.ilike(f"%{brand_name}%"))

        category_name = filters.get("category_name")
        if category_name:
            query = query.join(Category, ArticleBlueprint.category_id == Category.id).filter(
                Category.name.ilike(f"%{category_name}%")
            )

        return query.limit(200).all()

    def search_by_exact_code(self, db: Session, code: str) -> list[dict]:
        """
        Short-circuit deterministic lookup by exact EAN, supplier_code, or normalized_vendor_code.
        Returns a list of dicts: [{"blueprint_id": str, "allowed_colors": list[str] | None}].
        """
        from sqlalchemy import func, or_
        from src.models.wms import Article

        clean_code = (code or "").strip()
        if not clean_code:
            return []

        code_lower = clean_code.lower()
        matches_by_bp: dict[str, Optional[set[str]]] = {}

        # 1. Check Article.ean and Article.supplier_code
        matched_articles = (
            db.query(Article)
            .filter(
                or_(
                    func.lower(Article.ean) == code_lower,
                    func.lower(Article.supplier_code) == code_lower,
                )
            )
            .all()
        )
        for art in matched_articles:
            bp_id = str(art.article_blueprint_id)
            art_colors = [c.lower() for c in (art.colors or []) if c]
            if bp_id not in matches_by_bp:
                matches_by_bp[bp_id] = set(art_colors) if art_colors else None
            else:
                if matches_by_bp[bp_id] is not None:
                    if art_colors:
                        matches_by_bp[bp_id].update(art_colors)
                    else:
                        matches_by_bp[bp_id] = None

        # 2. Check ArticleBlueprint.normalized_vendor_code
        matched_bps = (
            db.query(ArticleBlueprint)
            .filter(func.lower(ArticleBlueprint.normalized_vendor_code) == code_lower)
            .all()
        )
        for bp in matched_bps:
            bp_id = str(bp.id)
            # Full blueprint match means all color variants of this blueprint are allowed
            matches_by_bp[bp_id] = None

        result = [
            {
                "blueprint_id": bp_id,
                "allowed_colors": sorted(list(colors)) if colors is not None else None,
            }
            for bp_id, colors in matches_by_bp.items()
        ]
        logger.log_execution(
            "pim_repo",
            "exact_code_search",
            "ok",
            code=clean_code,
            matches_count=len(result),
            matches=result,
        )
        return result

    def get_catalog_taxonomy_for_search(self, db: Session) -> dict:
        """
        Retrieve all Brand names and the complete Category/Sub-category hierarchy
        for contextualizing the WarehouseSearchAgent query planner.
        """
        brands = [b.name for b in db.query(Brand).order_by(Brand.name).all()]
        macro_cats = (
            db.query(Category)
            .filter(Category.parent_id.is_(None))
            .order_by(Category.name)
            .all()
        )
        categories_hierarchy: dict = {}
        for macro in macro_cats:
            subs = (
                db.query(Category)
                .filter(Category.parent_id == macro.id)
                .order_by(Category.name)
                .all()
            )
            categories_hierarchy[macro.name] = {
                "description": macro.description,
                "sub_categories": {s.name: s.description for s in subs},
            }

        taxonomy = {"brands": brands, "categories": categories_hierarchy}
        logger.log_execution(
            "pim_repo",
            "search_taxonomy_fetched",
            "ok",
            brand_count=len(brands),
            macro_category_count=len(categories_hierarchy),
        )
        return taxonomy

    def get_candidate_blueprints_by_sql_plan(
        self,
        db: Session,
        brand_name: Optional[str] = None,
        category_name: Optional[str] = None,
        in_stock_only: bool = False,
    ) -> list[dict]:
        """
        Execute a deterministic SQLAlchemy filter query based on the structured SearchQueryPlan.
        Automatically expands macro-categories to include all child sub-categories.
        """
        from sqlalchemy import func
        from src.models.wms import Article, ArticleStatus

        query = db.query(ArticleBlueprint).join(Brand, ArticleBlueprint.brand_id == Brand.id)

        if brand_name and brand_name.strip():
            clean_brand = brand_name.strip()
            exact_brand = db.query(Brand).filter(func.lower(Brand.name) == clean_brand.lower()).first()
            if exact_brand:
                query = query.filter(ArticleBlueprint.brand_id == exact_brand.id)
            else:
                query = query.filter(Brand.name.ilike(f"%{clean_brand}%"))

        if category_name and category_name.strip():
            clean_cat = category_name.strip()
            matched_cats = (
                db.query(Category)
                .filter(func.lower(Category.name) == clean_cat.lower())
                .all()
            )
            if not matched_cats:
                matched_cats = (
                    db.query(Category)
                    .filter(Category.name.ilike(f"%{clean_cat}%"))
                    .all()
                )
            if not matched_cats:
                logger.log_execution(
                    "pim_repo",
                    "sql_plan_candidates_fetched",
                    "ok",
                    brand_name=brand_name,
                    category_name=category_name,
                    in_stock_only=in_stock_only,
                    count=0,
                    reason="category_not_found",
                )
                return []

            target_cat_ids: set[str] = set()
            for cat in matched_cats:
                target_cat_ids.add(str(cat.id))
                if cat.parent_id is None:
                    child_cats = db.query(Category).filter(Category.parent_id == cat.id).all()
                    for child in child_cats:
                        target_cat_ids.add(str(child.id))

            query = query.filter(ArticleBlueprint.category_id.in_(list(target_cat_ids)))

        blueprints: list[ArticleBlueprint] = query.limit(200).all()

        results: list[dict] = []
        for bp in blueprints:
            articles = db.query(Article).filter(Article.article_blueprint_id == bp.id).all()
            if in_stock_only:
                eligible_articles = [a for a in articles if a.status == ArticleStatus.AVAILABLE]
                if not eligible_articles:
                    continue
            else:
                eligible_articles = articles

            available_colors: set[str] = set()
            for a in eligible_articles:
                for c in (a.colors or []):
                    if c and str(c).strip():
                        available_colors.add(str(c).strip().lower())

            brand_obj = db.query(Brand).filter(Brand.id == bp.brand_id).first()
            cat_obj = db.query(Category).filter(Category.id == bp.category_id).first() if bp.category_id else None
            main_cat_name: Optional[str] = None
            sub_cat_name: Optional[str] = None
            if cat_obj:
                if cat_obj.parent_id:
                    sub_cat_name = cat_obj.name
                    parent_cat = db.query(Category).filter(Category.id == cat_obj.parent_id).first()
                    main_cat_name = parent_cat.name if parent_cat else None
                else:
                    main_cat_name = cat_obj.name

            results.append(
                {
                    "id": str(bp.id),
                    "article_name": bp.article_name,
                    "brand_name": brand_obj.name if brand_obj else "Unknown",
                    "category_name": main_cat_name,
                    "sub_category_name": sub_cat_name,
                    "description": bp.description,
                    "extended_description": bp.extended_description,
                    "tags": list(bp.tags or []),
                    "materials": list(bp.materials or []),
                    "embedding": bp.embedding,
                    "available_colors": sorted(list(available_colors)),
                }
            )

        logger.log_execution(
            "pim_repo",
            "sql_plan_candidates_fetched",
            "ok",
            brand_name=brand_name,
            category_name=category_name,
            in_stock_only=in_stock_only,
            count=len(results),
        )
        return results


class CategoryRepository:
    """
    Repository for querying the brand-category hierarchy.

    Returns the structured dict expected by DataIngestionAgent:
        {
            "<MacroCategoryName>": {
                "description": "...",
                "sub_categories": {"<SubCategoryName>": "...", ...}
            },
            ...
        }
    """

    def get_brand_hierarchy(self, db: Session, brand_id: str) -> dict:
        """
        Build the complete category hierarchy for a given brand.

        Args:
            db:         SQLAlchemy session.
            brand_id:   The brand ID string from the extraction request.

        Returns:
            Nested dict mapping macro-category names → CategoryNode-compatible dicts.

        Raises:
            HTTPException(404): If the brand is not found in the database.
        """
        brand: Optional[Brand] = (
            db.query(Brand)
            .options(
                joinedload(Brand.categories).joinedload(Category.subcategories)
            )
            .filter(Brand.id == brand_id)
            .first()
        )
        if not brand:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Brand ID '{brand_id}' not found in the database. "
                       "Please ensure the brand hierarchy has been seeded."
            )

        hierarchy: dict = {}
        # brand.categories contains the macro-level categories linked via BrandCategory
        for macro_cat in brand.categories:
            # Only process top-level categories (parent_id is None)
            if macro_cat.parent_id is not None:
                continue

            # Query sub-categories directly to avoid relying on the lazy-loaded
            # adjacency-list relationship (which has a known remote_side config issue)
            subs = (
                db.query(Category)
                .filter(Category.parent_id == macro_cat.id)
                .all()
            )
            sub_categories: dict[str, str] = {
                sub.name: sub.description for sub in subs
            }

            hierarchy[macro_cat.name] = {
                "description": macro_cat.description,
                "sub_categories": sub_categories,
            }

        total_subs = sum(len(h["sub_categories"]) for h in hierarchy.values())
        logger.log_execution("category_repo", "category_hierarchy_fetched", "ok", brand_id=brand_id, macro_categories=len(hierarchy), total_sub_categories=total_subs, hierarchy=hierarchy)
        return hierarchy


class BrandHeuristicRepository:
    def create_heuristic(self, db: Session, brand_id: str, pattern: str, explanation: str = None) -> BrandHeuristic:
        logger.log_execution("pim_repo", "create_brand_heuristic", "ok", brand_id=brand_id, pattern=pattern)
        heuristic = BrandHeuristic(brand_id=brand_id, pattern=pattern, explanation=explanation)
        db.add(heuristic)
        db.commit()
        db.refresh(heuristic)
        return heuristic

    def get_heuristics_by_brand(self, db: Session, brand_id: str) -> list[BrandHeuristic]:
        return db.query(BrandHeuristic).filter(BrandHeuristic.brand_id == brand_id).all()


pim_repo = ArticleBlueprintRepository()
category_repo = CategoryRepository()
heuristic_repo = BrandHeuristicRepository()
