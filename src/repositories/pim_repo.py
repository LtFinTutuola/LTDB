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
        Deterministic lookup by exact EAN, supplier_code, normalized_vendor_code,
        or normalized code patterns (e.g. prefix matching, '#' wildcard resolution).
        Returns a list of dicts: [{"blueprint_id": str, "allowed_colors": list[str] | None}].
        """
        import re
        from sqlalchemy import func, or_
        from src.models.wms import Article

        clean_code = (code or "").strip()
        if not clean_code:
            return []

        code_lower = clean_code.lower()
        matches_by_bp: dict[str, Optional[set[str]]] = {}

        # 1. Primary check: Exact matches on Article.ean or Article.supplier_code
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

        # 2. Primary check: Exact match on ArticleBlueprint.normalized_vendor_code
        matched_bps = (
            db.query(ArticleBlueprint)
            .filter(func.lower(ArticleBlueprint.normalized_vendor_code) == code_lower)
            .all()
        )
        for bp in matched_bps:
            bp_id = str(bp.id)
            matches_by_bp[bp_id] = None

        # 3. Enhanced check: If no exact matches and query looks like a code pattern
        # (at least 3 characters, alphanumeric or common code separators)
        if not matches_by_bp and len(clean_code) >= 3 and not re.search(r"\s{2,}", clean_code):
            # 3a. Prefix match on Article.supplier_code (e.g. user entered "048519" for "04851901" or "151664" for "151664-1041-1CNU")
            prefix_articles = (
                db.query(Article)
                .filter(Article.supplier_code.ilike(f"{clean_code}%"))
                .all()
            )
            for art in prefix_articles:
                bp_id = str(art.article_blueprint_id)
                art_colors = [c.lower() for c in (art.colors or []) if c]
                if bp_id not in matches_by_bp:
                    matches_by_bp[bp_id] = set(art_colors) if art_colors else None
                elif matches_by_bp[bp_id] is not None and art_colors:
                    matches_by_bp[bp_id].update(art_colors)

            # 3b. Prefix match on ArticleBlueprint.normalized_vendor_code (e.g. "HWVG84" for "HWVG84#")
            prefix_bps = (
                db.query(ArticleBlueprint)
                .filter(ArticleBlueprint.normalized_vendor_code.ilike(f"{clean_code}%"))
                .all()
            )
            for bp in prefix_bps:
                bp_id = str(bp.id)
                matches_by_bp[bp_id] = None

            # 3c. Wildcard '#' pattern matching in ArticleBlueprint.normalized_vendor_code
            # (e.g. bp code "048519#" matches search "048519" or "04851901"; "151664-#-1CNU" matches "151664-1041-1CNU")
            wildcard_bps = (
                db.query(ArticleBlueprint)
                .filter(ArticleBlueprint.normalized_vendor_code.like("%#%"))
                .all()
            )
            clean_alnum = re.sub(r"[^a-zA-Z0-9]", "", clean_code).lower()
            for bp in wildcard_bps:
                bp_code = (bp.normalized_vendor_code or "").strip()
                # Construct regex replacing '#' with wildcard and '*' with literal '*'
                regex_pattern = "^" + re.escape(bp_code).replace(r"\#", r"[a-zA-Z0-9]*") + "$"
                bp_base = bp_code.replace("#", "").strip("-* ")
                bp_base_alnum = re.sub(r"[^a-zA-Z0-9]", "", bp_base).lower()

                if (
                    re.match(regex_pattern, clean_code, re.IGNORECASE)
                    or (bp_base and clean_code.lower().startswith(bp_base.lower()))
                    or (bp_base_alnum and clean_alnum.startswith(bp_base_alnum))
                ):
                    bp_id = str(bp.id)
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
        keywords: Optional[list[str]] = None,
        raw_query: Optional[str] = None,
    ) -> list[dict]:
        """
        Execute a deterministic SQLAlchemy filter query based on the structured SearchQueryPlan
        with multi-field keyword scoring and batch relationship loading to prevent N+1 queries.
        Automatically expands macro-categories to include all child sub-categories.
        """
        import re
        from collections import defaultdict
        from sqlalchemy import func, or_
        from sqlalchemy.orm import joinedload
        from src.models.wms import Article, ArticleStatus

        query = (
            db.query(ArticleBlueprint)
            .options(
                joinedload(ArticleBlueprint.brand),
                joinedload(ArticleBlueprint.category),
            )
            .join(Brand, ArticleBlueprint.brand_id == Brand.id)
        )

        has_brand_filter = False
        if brand_name and brand_name.strip():
            clean_brand = brand_name.strip()
            exact_brand = db.query(Brand).filter(func.lower(Brand.name) == clean_brand.lower()).first()
            if exact_brand:
                query = query.filter(ArticleBlueprint.brand_id == exact_brand.id)
                has_brand_filter = True
            else:
                query = query.filter(Brand.name.ilike(f"%{clean_brand}%"))
                has_brand_filter = True

        has_category_filter = False
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
            has_category_filter = True

        # Extract search tokens from keywords or raw_query
        stopwords = {
            "di", "con", "il", "la", "i", "gli", "le", "in", "per", "e", "ed", "da", "su",
            "a", "un", "uno", "una", "del", "della", "delle", "dei", "degli", "dello", "che"
        }
        search_tokens: list[str] = []
        if keywords:
            search_tokens.extend([k.strip().lower() for k in keywords if k and k.strip().lower() not in stopwords])
        if raw_query:
            tokens_from_query = [
                t.lower() for t in re.findall(r"[a-zA-Z0-9àèéìòùÀÈÉÌÒÙ#*]+", raw_query)
                if t.lower() not in stopwords and len(t) >= 2
            ]
            for t in tokens_from_query:
                if t not in search_tokens:
                    search_tokens.append(t)

        # If no brand and no category filter was applied, and search tokens exist, filter by keyword match
        if not has_brand_filter and not has_category_filter and search_tokens:
            keyword_clauses = []
            for tok in search_tokens:
                keyword_clauses.append(ArticleBlueprint.article_name.ilike(f"%{tok}%"))
                keyword_clauses.append(ArticleBlueprint.description.ilike(f"%{tok}%"))
                keyword_clauses.append(ArticleBlueprint.normalized_vendor_code.ilike(f"%{tok}%"))
            query = query.filter(or_(*keyword_clauses))

        blueprints: list[ArticleBlueprint] = query.all()
        if not blueprints:
            return []

        # Batch load all related articles for candidate blueprints (prevents N+1 queries)
        bp_ids = [bp.id for bp in blueprints]
        all_articles = db.query(Article).filter(Article.article_blueprint_id.in_(bp_ids)).all()
        articles_by_bp: dict[str, list[Article]] = defaultdict(list)
        for a in all_articles:
            articles_by_bp[str(a.article_blueprint_id)].append(a)

        # Batch load parent categories to avoid lazy-loading parent lookups
        parent_cat_ids = {bp.category.parent_id for bp in blueprints if bp.category and bp.category.parent_id}
        parent_cats_map = {
            str(c.id): c for c in db.query(Category).filter(Category.id.in_(list(parent_cat_ids))).all()
        } if parent_cat_ids else {}

        raw_query_clean = (raw_query or "").strip().lower()
        results: list[dict] = []
        for bp in blueprints:
            articles = articles_by_bp.get(str(bp.id), [])
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

            brand_obj = bp.brand
            cat_obj = bp.category
            main_cat_name: Optional[str] = None
            sub_cat_name: Optional[str] = None
            if cat_obj:
                if cat_obj.parent_id:
                    sub_cat_name = cat_obj.name
                    parent_cat = parent_cats_map.get(str(cat_obj.parent_id))
                    main_cat_name = parent_cat.name if parent_cat else None
                else:
                    main_cat_name = cat_obj.name

            # Compute lexical relevance score
            score = 0.0
            name_lower = (bp.article_name or "").lower()
            desc_lower = (bp.description or "").lower()
            code_lower = (bp.normalized_vendor_code or "").lower()
            tags_lower = [str(t).lower() for t in (bp.tags or [])]
            materials_lower = [str(m).lower() for m in (bp.materials or [])]

            if raw_query_clean and raw_query_clean in name_lower:
                score += 40.0
            for tok in search_tokens:
                if tok in name_lower:
                    score += 20.0
                if tok in code_lower:
                    score += 25.0
                if any(tok in t for t in tags_lower):
                    score += 10.0
                if any(tok in m for m in materials_lower):
                    score += 10.0
                if tok in desc_lower:
                    score += 5.0

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
                    "lexical_score": round(score, 2),
                }
            )

        # Sort results by lexical score if search tokens were present
        if search_tokens or raw_query_clean:
            results.sort(key=lambda item: item.get("lexical_score", 0.0), reverse=True)

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
    def create_heuristic(self, db: Session, brand_id: str, pattern: str, explanation: str = None, commit_changes: bool = True) -> BrandHeuristic:
        logger.log_execution("pim_repo", "create_brand_heuristic", "ok", brand_id=brand_id, pattern=pattern)
        heuristic = BrandHeuristic(brand_id=brand_id, pattern=pattern, explanation=explanation)
        db.add(heuristic)
        if commit_changes:
            db.commit()
            db.refresh(heuristic)
        else:
            db.flush()
        return heuristic

    def update_heuristic(
        self,
        db: Session,
        heuristic_id: str,
        pattern: str,
        explanation: Optional[str] = None,
        commit_changes: bool = True,
    ) -> Optional[BrandHeuristic]:
        heuristic = db.query(BrandHeuristic).filter(BrandHeuristic.id == heuristic_id).first()
        if not heuristic:
            return None
        heuristic.pattern = pattern
        if explanation is not None:
            heuristic.explanation = explanation
        if commit_changes:
            db.commit()
            db.refresh(heuristic)
        else:
            db.flush()
        logger.log_execution("pim_repo", "update_brand_heuristic", "ok", heuristic_id=heuristic_id, pattern=pattern)
        return heuristic

    def get_heuristics_by_brand(self, db: Session, brand_id: str) -> list[BrandHeuristic]:
        return db.query(BrandHeuristic).filter(BrandHeuristic.brand_id == brand_id).all()

    def find_matching_heuristic(self, db: Session, brand_id: str, sample_codes: list[str]) -> Optional[BrandHeuristic]:
        """
        Identify which BrandHeuristic record matches the provided sample codes (or fallback to the most recent heuristic for the brand).
        """
        import re
        heuristics = self.get_heuristics_by_brand(db, brand_id)
        if not heuristics:
            return None
        for h in heuristics:
            if not h.pattern:
                continue
            try:
                compiled = re.compile(h.pattern)
                if any(compiled.search(c) for c in sample_codes if c):
                    return h
            except re.error:
                continue
        return heuristics[-1]


pim_repo = ArticleBlueprintRepository()
category_repo = CategoryRepository()
heuristic_repo = BrandHeuristicRepository()
