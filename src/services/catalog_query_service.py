"""
src/services/catalog_query_service.py
-------------------------------------
Unified variant-row builder, multi-criteria sorter, and server-side paginator
used consistently across the entire warehouse catalog and search workflows.
"""
from __future__ import annotations
import math
from datetime import date, datetime
from typing import List, Dict, Optional, Tuple, Any, Set
from sqlalchemy.orm import Session
from sqlalchemy import func

from src.models.pim import ArticleBlueprint, Brand, Category, ArticlePhoto
from src.models.wms import Article, ArticleStatus, Batch
from src.schemas.catalog import SearchResultItem
from src.utils.text_normalize import fold_text


# Standard page size
DEFAULT_PAGE_SIZE = 50
MAX_SEARCH_RESULTS_CAP = 200


def build_variant_rows_for_blueprints(
    db: Session,
    blueprint_entries: List[Dict[str, Any]],
    in_stock_only: bool = False,
) -> List[SearchResultItem]:
    """
    Build unified SearchResultItem cards (one card per color variant).

    Args:
        db: SQLAlchemy session.
        blueprint_entries: list of dicts with at least 'id' and optionally:
            - 'allowed_colors': Optional[List[str]] (if set, filter variants to these colors)
            - 'relevance_score': float (relevance score for keyword matching)
            - 'blueprint': Optional[ArticleBlueprint]
        in_stock_only: When True, excludes variants with stock <= 0.
    """
    if not blueprint_entries:
        return []

    bp_ids = [str(e["id"]) for e in blueprint_entries if e.get("id")]
    if not bp_ids:
        return []

    allowed_colors_by_bp: Dict[str, Optional[Set[str]]] = {
        str(e["id"]): (
            {str(c).strip().lower() for c in e.get("allowed_colors") if c and str(c).strip()}
            if e.get("allowed_colors") is not None
            else None
        )
        for e in blueprint_entries
    }
    relevance_by_bp: Dict[str, float] = {
        str(e["id"]): float(e.get("relevance_score", 0.0))
        for e in blueprint_entries
    }

    # Batch load all related blueprints if not passed
    blueprints_map: Dict[str, ArticleBlueprint] = {
        str(e["id"]): e["blueprint"]
        for e in blueprint_entries
        if e.get("blueprint") and isinstance(e["blueprint"], ArticleBlueprint)
    }
    missing_bp_ids = [bid for bid in bp_ids if bid not in blueprints_map]
    if missing_bp_ids:
        for bp in db.query(ArticleBlueprint).filter(ArticleBlueprint.id.in_(missing_bp_ids)).all():
            blueprints_map[str(bp.id)] = bp

    # Batch load brands and categories
    brands_map = {str(b.id): b for b in db.query(Brand).all()}
    cats_map = {str(c.id): c for c in db.query(Category).all()}
    batches_map = {str(b.id): b for b in db.query(Batch).all()}

    # Batch load articles
    all_articles = db.query(Article).filter(Article.article_blueprint_id.in_(bp_ids)).all()
    articles_by_bp: Dict[str, List[Article]] = {}
    for a in all_articles:
        articles_by_bp.setdefault(str(a.article_blueprint_id), []).append(a)

    # Batch load photos
    all_photos = db.query(ArticlePhoto).filter(ArticlePhoto.article_blueprint_id.in_(bp_ids)).all()
    photos_by_bp: Dict[str, List[ArticlePhoto]] = {}
    for p in all_photos:
        photos_by_bp.setdefault(str(p.article_blueprint_id), []).append(p)

    results: List[SearchResultItem] = []

    for bp_id in bp_ids:
        bp = blueprints_map.get(bp_id)
        if not bp:
            continue

        brand = brands_map.get(str(bp.brand_id)) if bp.brand_id else None
        category = cats_map.get(str(bp.category_id)) if bp.category_id else None

        main_category = None
        sub_category = None
        if category:
            if category.parent_id:
                sub_category = category
                main_category = cats_map.get(str(category.parent_id))
            else:
                main_category = category

        articles = articles_by_bp.get(bp_id, [])

        def _extract_article_timestamp(a: Article) -> datetime:
            if isinstance(a.created_at, datetime):
                return a.created_at
            if isinstance(a.created_at, date):
                return datetime.combine(a.created_at, datetime.min.time())
            if batches_map and a.batch_id:
                b = batches_map.get(str(a.batch_id))
                if b and b.document_date:
                    if isinstance(b.document_date, datetime):
                        return b.document_date
                    if isinstance(b.document_date, date):
                        return datetime.combine(b.document_date, datetime.min.time())
            return datetime.min

        articles = articles_by_bp.get(bp_id, [])
        articles.sort(key=_extract_article_timestamp, reverse=True)

        # Group articles by color
        colors_dict: Dict[str, List[Article]] = {}
        for a in articles:
            color_keys = [str(c).strip().lower() for c in (a.colors or []) if c and str(c).strip()]
            if not color_keys:
                color_keys = ["unknown"]
            for c in color_keys:
                colors_dict.setdefault(c, []).append(a)

        if not colors_dict:
            colors_dict["unknown"] = []

        bp_photos = photos_by_bp.get(bp_id, [])
        allowed_set = allowed_colors_by_bp.get(bp_id)

        for color, variant_articles in colors_dict.items():
            if allowed_set is not None and color.lower() not in allowed_set:
                continue

            stock = len([a for a in variant_articles if a.status == ArticleStatus.AVAILABLE])
            if in_stock_only and stock <= 0:
                continue

            photo = None
            if color != "unknown":
                photo = next((p for p in bp_photos if p.canonical_color_name and p.canonical_color_name.strip().lower() == color), None)
            if not photo and bp_photos:
                photo = bp_photos[0]

            name = f"{bp.article_name} - {color.capitalize()}" if color and color != "unknown" else bp.article_name
            vendor_code = next((a.supplier_code for a in variant_articles if a.supplier_code), None)
            barcode = next((a.ean for a in variant_articles if a.ean), None)

            # Determine latest physical article creation timestamp for date-sorting
            latest_timestamp: datetime = datetime.min
            if variant_articles:
                latest_timestamp = max((_extract_article_timestamp(a) for a in variant_articles), default=datetime.min)
            if latest_timestamp == datetime.min and bp.created_at:
                if isinstance(bp.created_at, datetime):
                    latest_timestamp = bp.created_at
                elif isinstance(bp.created_at, date):
                    latest_timestamp = datetime.combine(bp.created_at, datetime.min.time())

            item = SearchResultItem(
                blueprint_id=str(bp.id),
                article_name=name,
                brand_name=brand.name if brand else "Unknown",
                brand_id=str(brand.id) if brand else None,
                category_name=main_category.name if main_category else None,
                sub_category_name=sub_category.name if sub_category else None,
                description=bp.description,
                extended_description=bp.extended_description,
                vendor_code=vendor_code,
                barcode=barcode,
                photo_id=str(photo.id) if photo else None,
                colors=[color] if color != "unknown" else [],
                tags=list(bp.tags or []),
                materials=list(bp.materials or []),
                stock=stock,
            )
            # Attach transient metadata for sorting
            item._relevance_score = relevance_by_bp.get(bp_id, 0.0)
            item._latest_date = latest_timestamp
            results.append(item)

    return results


def sort_variant_rows(
    rows: List[SearchResultItem],
    sort_by: str = "date",
    sort_dir: str = "desc",
) -> List[SearchResultItem]:
    """
    Sort SearchResultItem rows deterministically by:
      - 'date' (latest physical article creation datetime or blueprint creation datetime)
      - 'category' (main category, sub category)
      - 'brand' (brand name folded)
      - 'relevance' (relevance score)
    Preserves adjacent variants of the same blueprint where possible and uses
    stable tie-breakers.
    """
    if not rows:
        return []

    is_desc = (sort_dir or "desc").lower() == "desc"
    sort_field = (sort_by or "date").lower()

    def _sort_key(item: SearchResultItem):
        # Tie-breakers
        raw_date = getattr(item, "_latest_date", datetime.min) or datetime.min
        if isinstance(raw_date, datetime):
            date_key = raw_date
        elif isinstance(raw_date, date):
            date_key = datetime.combine(raw_date, datetime.min.time())
        else:
            date_key = datetime.min

        bp_key = item.blueprint_id or ""
        color_key = item.colors[0] if item.colors else ""

        if sort_field == "relevance":
            rel = getattr(item, "_relevance_score", 0.0) or 0.0
            return (rel, date_key, bp_key, color_key)

        elif sort_field == "category":
            cat = fold_text(item.category_name) or "\uffff"  # Nulls last
            sub = fold_text(item.sub_category_name) or "\uffff"
            return (cat, sub, date_key, bp_key, color_key)

        elif sort_field == "brand":
            brand = fold_text(item.brand_name) or "\uffff"
            return (brand, date_key, bp_key, color_key)

        else:  # default "date"
            return (date_key, bp_key, color_key)

    sorted_rows = sorted(rows, key=_sort_key, reverse=is_desc)
    return sorted_rows


def paginate_rows(
    rows: List[SearchResultItem],
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> Dict[str, Any]:
    """
    Slice sorted SearchResultItem rows for the requested page.
    """
    total_items = len(rows)
    page_size = max(1, page_size)
    total_pages = max(1, math.ceil(total_items / page_size)) if total_items > 0 else 1

    current_page = max(1, min(page, total_pages)) if total_items > 0 else 1
    offset = (current_page - 1) * page_size
    page_slice = rows[offset : offset + page_size]

    # Count distinct blueprints in the entire result set
    distinct_blueprints = len({r.blueprint_id for r in rows})

    return {
        "results": page_slice,
        "total_items": total_items,
        "total_blueprints": distinct_blueprints,
        "page": current_page,
        "limit": page_size,
        "total_pages": total_pages,
    }
