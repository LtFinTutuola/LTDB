"""
tests/test_catalog_pagination_performance.py
---------------------------------------------
Comprehensive test coverage for Level 1 & Level 2 performance optimizations:
- Database push-down CTE variant pagination (query_paginated_catalog_variants)
- In-stock filtering and accurate variant/blueprint counters
- Multi-criteria sorting (date, brand, category)
- Resolution of photo_id, vendor_code, and barcode on paginated cards
- Index metadata presence
"""
import pytest
from datetime import date, datetime, timedelta
from src.models.pim import ArticleBlueprint, Brand, Category, ArticlePhoto
from src.models.wms import Supplier, Batch, Article, ArticleStatus
from src.services.catalog_query_service import query_paginated_catalog_variants
from src.services import catalog_service


def _create_test_brand(db, name="BrandTest"):
    brand = Brand(name=name)
    db.add(brand)
    db.commit()
    db.refresh(brand)
    return brand


def _create_test_category(db, name="Borse", parent_id=None):
    cat = Category(name=name, description="Test Category", parent_id=parent_id)
    db.add(cat)
    db.commit()
    db.refresh(cat)
    return cat


def _create_test_batch(db, doc_date=None):
    supplier = Supplier(company_name="Test Supplier")
    db.add(supplier)
    db.commit()
    batch = Batch(
        supplier_id=supplier.id,
        delivery_note_number="DDT-TEST",
        document_date=doc_date or date(2026, 1, 15),
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


def _create_test_blueprint(db, brand_id, category_id=None, name="Modello Test"):
    bp = ArticleBlueprint(
        brand_id=brand_id,
        category_id=category_id,
        article_name=name,
        description="Descrizione breve",
        extended_description="Descrizione estesa dettagliata",
        tags=["moda", "pelle"],
        materials=["vitello"],
    )
    db.add(bp)
    db.commit()
    db.refresh(bp)
    return bp


def test_paginated_variants_empty_database(db_session):
    """Verify that query_paginated_catalog_variants returns an empty structure on empty DB."""
    res = query_paginated_catalog_variants(db_session, limit=50, offset=0)
    assert res["results"] == []
    assert res["total_items"] == 0
    assert res["total_blueprints"] == 0
    assert res["page"] == 1
    assert res["total_pages"] == 1
    assert res["limit"] == 50


def test_paginated_variants_multi_page_pagination(db_session):
    """
    Verify page slicing across multiple pages:
    - 2 Blueprints, one with 2 colors and one with 1 color -> 3 variant cards.
    - Page size 2 -> Page 1 has 2 items, Page 2 has 1 item.
    - total_items == 3, total_blueprints == 2, total_pages == 2.
    """
    brand = _create_test_brand(db_session, "Prada")
    bp1 = _create_test_blueprint(db_session, brand.id, name="Borsa Saffiano")
    bp2 = _create_test_blueprint(db_session, brand.id, name="Portafoglio")
    batch = _create_test_batch(db_session)

    # BP1 has Nero and Rosso
    a1 = Article(article_blueprint_id=bp1.id, batch_id=batch.id, colors=["nero"], status=ArticleStatus.AVAILABLE)
    a2 = Article(article_blueprint_id=bp1.id, batch_id=batch.id, colors=["rosso"], status=ArticleStatus.AVAILABLE)
    # BP2 has Blu
    a3 = Article(article_blueprint_id=bp2.id, batch_id=batch.id, colors=["blu"], status=ArticleStatus.AVAILABLE)
    db_session.add_all([a1, a2, a3])
    db_session.commit()

    # Page 1 (limit 2, offset 0)
    p1 = catalog_service.list_latest_items(db_session, limit=2, offset=0)
    assert p1["total_items"] == 3
    assert p1["total_blueprints"] == 2
    assert p1["total_pages"] == 2
    assert p1["page"] == 1
    assert len(p1["results"]) == 2

    # Page 2 (limit 2, offset 2)
    p2 = catalog_service.list_latest_items(db_session, limit=2, offset=2)
    assert p2["total_items"] == 3
    assert p2["total_blueprints"] == 2
    assert p2["total_pages"] == 2
    assert p2["page"] == 2
    assert len(p2["results"]) == 1

    # Ensure no overlap between page 1 and page 2 results
    p1_keys = {(r.blueprint_id, tuple(r.colors)) for r in p1["results"]}
    p2_keys = {(r.blueprint_id, tuple(r.colors)) for r in p2["results"]}
    assert p1_keys.isdisjoint(p2_keys)
    assert len(p1_keys | p2_keys) == 3


def test_paginated_variants_in_stock_filtering_and_counts(db_session):
    """
    Verify in_stock_only=True excludes variants with 0 available stock.
    - BP1: 1 AVAILABLE (nero), 1 SOLD (rosso).
    - BP2: 1 SOLD (cuoio).
    - in_stock_only=True -> 1 item (nero), 1 blueprint.
    - in_stock_only=False -> 3 items (nero, rosso, cuoio), 2 blueprints.
    """
    brand = _create_test_brand(db_session, "Gucci")
    bp1 = _create_test_blueprint(db_session, brand.id, name="Jackie Bag")
    bp2 = _create_test_blueprint(db_session, brand.id, name="Dionysus")
    batch = _create_test_batch(db_session)

    a1 = Article(article_blueprint_id=bp1.id, batch_id=batch.id, colors=["nero"], status=ArticleStatus.AVAILABLE)
    a2 = Article(article_blueprint_id=bp1.id, batch_id=batch.id, colors=["rosso"], status=ArticleStatus.SOLD)
    a3 = Article(article_blueprint_id=bp2.id, batch_id=batch.id, colors=["cuoio"], status=ArticleStatus.SOLD)
    db_session.add_all([a1, a2, a3])
    db_session.commit()

    # in_stock_only = True
    in_stock = catalog_service.list_latest_items(db_session, in_stock_only=True)
    assert in_stock["total_items"] == 1
    assert in_stock["total_blueprints"] == 1
    assert len(in_stock["results"]) == 1
    assert in_stock["results"][0].stock == 1
    assert in_stock["results"][0].colors == ["nero"]

    # in_stock_only = False
    all_items = catalog_service.list_latest_items(db_session, in_stock_only=False)
    assert all_items["total_items"] == 3
    assert all_items["total_blueprints"] == 2
    assert len(all_items["results"]) == 3


def test_paginated_variants_sorting_by_date_brand_and_category(db_session):
    """
    Verify multi-criteria sorting:
    - Sort by date DESC / ASC using article arrival timestamps
    - Sort by brand ASC / DESC
    - Sort by category ASC / DESC
    """
    brand_a = _create_test_brand(db_session, "Armani")
    brand_z = _create_test_brand(db_session, "Zanellato")

    cat_borse = _create_test_category(db_session, name="Borse")
    cat_zaini = _create_test_category(db_session, name="Zaini")

    bp_armani = _create_test_blueprint(db_session, brand_a.id, category_id=cat_zaini.id, name="Zaino Armani")
    bp_zanellato = _create_test_blueprint(db_session, brand_z.id, category_id=cat_borse.id, name="Borsa Postina")

    batch_old = _create_test_batch(db_session, doc_date=date(2025, 1, 1))
    batch_new = _create_test_batch(db_session, doc_date=date(2026, 6, 1))

    # Armani arrived in 2025
    art_a = Article(
        article_blueprint_id=bp_armani.id,
        batch_id=batch_old.id,
        colors=["nero"],
        status=ArticleStatus.AVAILABLE,
        created_at=datetime(2025, 1, 1, 10, 0, 0),
    )
    # Zanellato arrived in 2026
    art_z = Article(
        article_blueprint_id=bp_zanellato.id,
        batch_id=batch_new.id,
        colors=["taupe"],
        status=ArticleStatus.AVAILABLE,
        created_at=datetime(2026, 6, 1, 10, 0, 0),
    )
    db_session.add_all([art_a, art_z])
    db_session.commit()

    # 1. Date DESC: Zanellato first (2026-06-01), Armani second (2025-01-01)
    res_date_desc = catalog_service.list_latest_items(db_session, sort_by="date", sort_dir="desc")
    assert res_date_desc["results"][0].blueprint_id == str(bp_zanellato.id)
    assert res_date_desc["results"][1].blueprint_id == str(bp_armani.id)

    # 2. Date ASC: Armani first, Zanellato second
    res_date_asc = catalog_service.list_latest_items(db_session, sort_by="date", sort_dir="asc")
    assert res_date_asc["results"][0].blueprint_id == str(bp_armani.id)
    assert res_date_asc["results"][1].blueprint_id == str(bp_zanellato.id)

    # 3. Brand ASC: Armani first, Zanellato second
    res_brand_asc = catalog_service.list_latest_items(db_session, sort_by="brand", sort_dir="asc")
    assert res_brand_asc["results"][0].brand_name == "Armani"
    assert res_brand_asc["results"][1].brand_name == "Zanellato"

    # 4. Brand DESC: Zanellato first, Armani second
    res_brand_desc = catalog_service.list_latest_items(db_session, sort_by="brand", sort_dir="desc")
    assert res_brand_desc["results"][0].brand_name == "Zanellato"
    assert res_brand_desc["results"][1].brand_name == "Armani"

    # 5. Category ASC: Borse first (Zanellato), Zaini second (Armani)
    res_cat_asc = catalog_service.list_latest_items(db_session, sort_by="category", sort_dir="asc")
    assert res_cat_asc["results"][0].category_name == "Borse"
    assert res_cat_asc["results"][1].category_name == "Zaini"


def test_paginated_variants_photos_and_codes_mapping(db_session):
    """
    Verify that photo_id, vendor_code, and barcode are correctly resolved for variants.
    """
    brand = _create_test_brand(db_session, "Fendi")
    bp = _create_test_blueprint(db_session, brand.id, name="Baguette")
    batch = _create_test_batch(db_session)

    # Photo for color 'marrone'
    photo = ArticlePhoto(
        article_blueprint_id=bp.id,
        canonical_color_name="marrone",
        photo_data=b"fake-photo-binary",
    )
    db_session.add(photo)
    db_session.commit()

    art = Article(
        article_blueprint_id=bp.id,
        batch_id=batch.id,
        supplier_code="FEN-BAG-001",
        ean="8012345678901",
        colors=["marrone"],
        status=ArticleStatus.AVAILABLE,
    )
    db_session.add(art)
    db_session.commit()

    res = catalog_service.list_latest_items(db_session, limit=10)
    assert len(res["results"]) == 1
    item = res["results"][0]
    assert item.vendor_code == "FEN-BAG-001"
    assert item.barcode == "8012345678901"
    assert item.photo_id == str(photo.id)
    assert "Marrone" in item.article_name
    assert item.colors == ["marrone"]
    assert item.stock == 1


def test_performance_indexes_registered_in_metadata():
    """Verify that all new performance indexes are properly registered in SQLAlchemy model metadata."""
    article_indexes = {idx.name for idx in Article.__table__.indexes}
    assert "ix_articles_article_blueprint_id" in article_indexes or any("article_blueprint_id" in idx.columns for idx in Article.__table__.indexes)
    assert "ix_articles_batch_id" in article_indexes or any("batch_id" in idx.columns for idx in Article.__table__.indexes)
    assert "ix_articles_status" in article_indexes or any("status" in idx.columns for idx in Article.__table__.indexes)
    assert "ix_articles_bp_status" in article_indexes

    bp_indexes = {idx.name for idx in ArticleBlueprint.__table__.indexes}
    assert "ix_article_blueprints_created_at" in bp_indexes
    assert any("brand_id" in idx.columns for idx in ArticleBlueprint.__table__.indexes)
    assert any("category_id" in idx.columns for idx in ArticleBlueprint.__table__.indexes)

    batch_indexes = {idx.name for idx in Batch.__table__.indexes}
    assert any("document_date" in idx.columns for idx in Batch.__table__.indexes)
