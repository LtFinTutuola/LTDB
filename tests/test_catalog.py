"""
tests/test_catalog.py
---------------------
Tests for the new Catalog endpoints and services:
  - catalog_service.update_blueprint (patch blueprint fields)
  - pim_repo.get_all_embeddings (cross-brand embedding retrieval)
  - category_repo.get_brand_hierarchy (existing, exercised via catalog path)
  - Brand listing query (used by catalog_router GET /brands)
"""
import pytest
from unittest.mock import patch, MagicMock

from src.models.pim import Brand, Category, BrandCategory, ArticleBlueprint
from src.schemas.pim import BrandCreate
from src.schemas.catalog import CatalogUpdateRequest
from src.repositories.base import BaseRepository
from src.repositories.pim_repo import pim_repo, category_repo


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_brand(db, name="TestBrand"):
    repo = BaseRepository(Brand)
    return repo.create(db, obj_in=BrandCreate(name=name))


def _make_category(db, name="Borse", description="Categoria borse", parent_id=None):
    cat = Category(name=name, description=description, parent_id=parent_id)
    db.add(cat)
    db.commit()
    db.refresh(cat)
    return cat


def _make_blueprint(db, brand_id, article_name="Borsa Milano",
                    description="Una bella borsa",
                    extended_description="Molto lunga descrizione.",
                    tags=None, materials=None, embedding=None, category_id=None):
    bp = ArticleBlueprint(
        brand_id=brand_id,
        category_id=category_id,
        article_name=article_name,
        description=description,
        extended_description=extended_description,
        tags=tags or ["pelle"],
        materials=materials or ["pelle bovina"],
        embedding=embedding,
    )
    db.add(bp)
    db.commit()
    db.refresh(bp)
    return bp


# ---------------------------------------------------------------------------
# Brand listing
# ---------------------------------------------------------------------------

class TestBrandListing:
    def test_list_brands_returns_all(self, db_session):
        _make_brand(db_session, "Gucci")
        _make_brand(db_session, "Prada")
        brands = db_session.query(Brand).order_by(Brand.name).all()
        assert len(brands) == 2
        assert brands[0].name == "Gucci"
        assert brands[1].name == "Prada"

    def test_list_brands_empty_db(self, db_session):
        assert db_session.query(Brand).all() == []

    def test_list_brands_ordered_by_name(self, db_session):
        for name in ["Zara", "Armani", "Balenciaga"]:
            _make_brand(db_session, name)
        brands = db_session.query(Brand).order_by(Brand.name).all()
        names = [b.name for b in brands]
        assert names == sorted(names)


# ---------------------------------------------------------------------------
# Category hierarchy
# ---------------------------------------------------------------------------

class TestCategoryHierarchy:
    def test_hierarchy_returns_correct_structure(self, db_session):
        brand = _make_brand(db_session, "HierarchyBrand")
        macro = _make_category(db_session, name="Borse", description="Borse di lusso")
        sub   = _make_category(db_session, name="Borse a mano",
                                description="Con manico", parent_id=macro.id)
        db_session.add(BrandCategory(brand_id=brand.id, category_id=macro.id))
        db_session.add(BrandCategory(brand_id=brand.id, category_id=sub.id))
        db_session.commit()

        hierarchy = category_repo.get_brand_hierarchy(db_session, brand.id)
        assert "Borse" in hierarchy
        assert hierarchy["Borse"]["description"] == "Borse di lusso"
        assert "Borse a mano" in hierarchy["Borse"]["sub_categories"]

    def test_hierarchy_raises_404_for_unknown_brand(self, db_session):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            category_repo.get_brand_hierarchy(db_session, "nonexistent-id")
        assert exc_info.value.status_code == 404

    def test_hierarchy_empty_categories(self, db_session):
        brand = _make_brand(db_session, "EmptyBrand")
        assert category_repo.get_brand_hierarchy(db_session, brand.id) == {}


# ---------------------------------------------------------------------------
# catalog_service.update_blueprint
# ---------------------------------------------------------------------------

class TestUpdateBlueprint:
    def test_update_article_name(self, db_session):
        brand = _make_brand(db_session)
        bp = _make_blueprint(db_session, brand.id)
        with patch("src.services.catalog_service.LLMClient") as MockLLM:
            mock_instance = MagicMock()
            mock_instance.generate_embedding_sync.return_value = [0.1] * 768
            MockLLM.return_value = mock_instance
            from src.services import catalog_service
            result = catalog_service.update_blueprint(
                db_session, bp.id, CatalogUpdateRequest(article_name="Borsa Roma")
            )
        assert result.status == "success"
        assert "article_name" in result.updated_fields
        db_session.refresh(bp)
        assert bp.article_name == "Borsa Roma"

    def test_update_multiple_fields(self, db_session):
        brand = _make_brand(db_session)
        bp = _make_blueprint(db_session, brand.id)
        with patch("src.services.catalog_service.LLMClient") as MockLLM:
            mock_instance = MagicMock()
            mock_instance.generate_embedding_sync.return_value = [0.1] * 768
            MockLLM.return_value = mock_instance
            from src.services import catalog_service
            result = catalog_service.update_blueprint(
                db_session, bp.id,
                CatalogUpdateRequest(
                    article_name="Borsa Firenze",
                    tags=["pelle", "artigianale"],
                    materials=["pelle di vitello"],
                )
            )
        assert result.status == "success"
        assert set(result.updated_fields) == {"article_name", "tags", "materials"}
        db_session.refresh(bp)
        assert bp.article_name == "Borsa Firenze"
        assert "artigianale" in bp.tags

    def test_update_non_descriptive_field_skips_embedding(self, db_session):
        brand = _make_brand(db_session)
        bp = _make_blueprint(db_session, brand.id)
        with patch("src.services.catalog_service.LLMClient") as MockLLM:
            mock_instance = MagicMock()
            MockLLM.return_value = mock_instance
            from src.services import catalog_service
            result = catalog_service.update_blueprint(
                db_session, bp.id, CatalogUpdateRequest(dimensions="30x20x10 cm")
            )
            MockLLM.assert_not_called()
        assert result.status == "success"
        db_session.refresh(bp)
        assert bp.dimensions == "30x20x10 cm"

    def test_update_empty_tags_raises_400(self, db_session):
        from fastapi import HTTPException
        brand = _make_brand(db_session)
        bp = _make_blueprint(db_session, brand.id)
        from src.services import catalog_service
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.update_blueprint(
                db_session, bp.id, CatalogUpdateRequest(tags=[])
            )
        assert exc_info.value.status_code == 400

    def test_update_empty_materials_raises_400(self, db_session):
        from fastapi import HTTPException
        brand = _make_brand(db_session)
        bp = _make_blueprint(db_session, brand.id)
        from src.services import catalog_service
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.update_blueprint(
                db_session, bp.id, CatalogUpdateRequest(materials=[])
            )
        assert exc_info.value.status_code == 400

    def test_update_description_empty_raises_400(self, db_session):
        from fastapi import HTTPException
        brand = _make_brand(db_session)
        bp = _make_blueprint(db_session, brand.id)
        from src.services import catalog_service
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.update_blueprint(
                db_session, bp.id, CatalogUpdateRequest(description="")
            )
        assert exc_info.value.status_code == 400
        assert "non può essere vuoto o nullo" in exc_info.value.detail

    def test_update_description_null_raises_400(self, db_session):
        from fastapi import HTTPException
        brand = _make_brand(db_session)
        bp = _make_blueprint(db_session, brand.id)
        from src.services import catalog_service
        # Simulate payload parsing where description is explicitly set to None
        req = CatalogUpdateRequest()
        req.description = None
        # In pydantic v2, setting an attribute explicitly might not add it to __fields_set__
        # if using assignment without proper model construction.
        # But for CatalogUpdateRequest(description=None), it will be in model_dump(exclude_unset=True)
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.update_blueprint(
                db_session, bp.id, CatalogUpdateRequest(description=None)
            )
        assert exc_info.value.status_code == 400
        assert "non può essere vuoto o nullo" in exc_info.value.detail

    def test_update_blueprint_not_found_raises_404(self, db_session):
        from fastapi import HTTPException
        from src.services import catalog_service
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.update_blueprint(
                db_session, "nonexistent-uuid", CatalogUpdateRequest(article_name="X")
            )
        assert exc_info.value.status_code == 404

    def test_update_no_fields_raises_400(self, db_session):
        from fastapi import HTTPException
        brand = _make_brand(db_session)
        bp = _make_blueprint(db_session, brand.id)
        from src.services import catalog_service
        with pytest.raises(HTTPException) as exc_info:
            catalog_service.update_blueprint(db_session, bp.id, CatalogUpdateRequest())
        assert exc_info.value.status_code == 400

    def test_update_does_not_touch_unset_fields(self, db_session):
        brand = _make_brand(db_session)
        bp = _make_blueprint(db_session, brand.id,
                             article_name="Original Name", tags=["originale"])
        with patch("src.services.catalog_service.LLMClient") as MockLLM:
            mock_instance = MagicMock()
            mock_instance.generate_embedding_sync.return_value = [0.1] * 768
            MockLLM.return_value = mock_instance
            from src.services import catalog_service
            catalog_service.update_blueprint(
                db_session, bp.id, CatalogUpdateRequest(dimensions="10x5 cm")
            )
        db_session.refresh(bp)
        assert bp.article_name == "Original Name"
        assert "originale" in bp.tags


# ---------------------------------------------------------------------------
# pim_repo.get_all_embeddings
# ---------------------------------------------------------------------------

class TestGetAllEmbeddings:
    def test_returns_only_blueprints_with_embeddings(self, db_session):
        brand = _make_brand(db_session)
        _make_blueprint(db_session, brand.id, article_name="With Embedding",
                        embedding=[0.1, 0.2, 0.3])
        _make_blueprint(db_session, brand.id, article_name="No Embedding",
                        embedding=None)
        results = pim_repo.get_all_embeddings(db_session)
        assert len(results) == 1
        assert results[0]["article_name"] == "With Embedding"

    def test_returns_empty_list_when_no_embeddings(self, db_session):
        brand = _make_brand(db_session)
        _make_blueprint(db_session, brand.id)
        assert pim_repo.get_all_embeddings(db_session) == []

    def test_returns_embeddings_across_brands(self, db_session):
        brand1 = _make_brand(db_session, "BrandA")
        brand2 = _make_brand(db_session, "BrandB")
        _make_blueprint(db_session, brand1.id, article_name="A", embedding=[0.1, 0.2])
        _make_blueprint(db_session, brand2.id, article_name="B", embedding=[0.3, 0.4])
        assert len(pim_repo.get_all_embeddings(db_session)) == 2

    def test_result_has_required_keys(self, db_session):
        brand = _make_brand(db_session)
        _make_blueprint(db_session, brand.id, embedding=[0.5, 0.6])
        results = pim_repo.get_all_embeddings(db_session)
        assert len(results) == 1
        for key in ("id", "embedding", "article_name", "description"):
            assert key in results[0], f"Missing key: {key}"

from fastapi.testclient import TestClient
from src.main import app

def test_list_items(db_session):
    client = TestClient(app)
    response = client.get("/api/v1/catalog/items")
    assert response.status_code == 200
    data = response.json()
    assert "results" in data
    assert "total_blueprints" in data
    assert "page" in data
    assert "total_pages" in data


class TestCatalogPaginationAndStock:
    def test_list_items_pagination_metadata_and_slices(self, db_session):
        brand = _make_brand(db_session, name="BrandPagination")
        bp1 = _make_blueprint(db_session, brand.id, article_name="Item 1")
        bp2 = _make_blueprint(db_session, brand.id, article_name="Item 2")
        bp3 = _make_blueprint(db_session, brand.id, article_name="Item 3")

        client = TestClient(app)
        
        # Page 1 (limit=2, offset=0)
        res1 = client.get("/api/v1/catalog/items?limit=2&offset=0")
        assert res1.status_code == 200
        data1 = res1.json()
        assert data1["total_blueprints"] == 3
        assert data1["page"] == 1
        assert data1["limit"] == 2
        assert data1["total_pages"] == 2
        assert len(data1["results"]) == 2

        # Page 2 (limit=2, offset=2)
        res2 = client.get("/api/v1/catalog/items?limit=2&offset=2")
        assert res2.status_code == 200
        data2 = res2.json()
        assert data2["total_blueprints"] == 3
        assert data2["page"] == 2
        assert data2["limit"] == 2
        assert data2["total_pages"] == 2
        assert len(data2["results"]) == 1

        # Check distinct items
        names1 = {it["article_name"] for it in data1["results"]}
        names2 = {it["article_name"] for it in data2["results"]}
        assert names1.isdisjoint(names2)

    def test_list_items_in_stock_only_filter(self, db_session):
        from datetime import date
        from src.models.wms import Supplier, Batch, Article, ArticleStatus

        brand = _make_brand(db_session, name="BrandStock")
        bp_avail = _make_blueprint(db_session, brand.id, article_name="Available Bag")
        bp_sold = _make_blueprint(db_session, brand.id, article_name="Sold Bag")

        sup = Supplier(company_name="StockSupplier")
        db_session.add(sup)
        db_session.commit()
        batch = Batch(supplier_id=sup.id, delivery_note_number="DDT-STK", document_date=date(2026, 1, 1))
        db_session.add(batch)
        db_session.commit()

        # Available article for bp_avail
        art_avail = Article(
            article_blueprint_id=bp_avail.id,
            batch_id=batch.id,
            status=ArticleStatus.AVAILABLE,
            colors=["blue"],
        )
        # Sold article for bp_sold
        art_sold = Article(
            article_blueprint_id=bp_sold.id,
            batch_id=batch.id,
            status=ArticleStatus.SOLD,
            colors=["black"],
        )
        db_session.add_all([art_avail, art_sold])
        db_session.commit()

        client = TestClient(app)

        # In stock only = False: both returned
        res_all = client.get("/api/v1/catalog/items?in_stock_only=false")
        assert res_all.status_code == 200
        data_all = res_all.json()
        assert data_all["total_blueprints"] == 2
        all_names = [it["article_name"] for it in data_all["results"]]
        assert any("Available Bag" in n for n in all_names)
        assert any("Sold Bag" in n for n in all_names)

        # In stock only = True: only available returned
        res_stock = client.get("/api/v1/catalog/items?in_stock_only=true")
        assert res_stock.status_code == 200
        data_stock = res_stock.json()
        assert data_stock["total_blueprints"] == 1
        stock_names = [it["article_name"] for it in data_stock["results"]]
        assert len(stock_names) == 1
        assert "Available Bag" in stock_names[0]

    def test_list_items_variant_stock_filtering(self, db_session):
        from datetime import date
        from src.models.wms import Supplier, Batch, Article, ArticleStatus
        from src.services import catalog_service

        brand = _make_brand(db_session, name="BrandVariants")
        bp = _make_blueprint(db_session, brand.id, article_name="MultiColor Bag")

        sup = Supplier(company_name="VarSupplier")
        db_session.add(sup)
        db_session.commit()
        batch = Batch(supplier_id=sup.id, delivery_note_number="DDT-VAR", document_date=date(2026, 1, 1))
        db_session.add(batch)
        db_session.commit()

        art_red = Article(
            article_blueprint_id=bp.id,
            batch_id=batch.id,
            status=ArticleStatus.AVAILABLE,
            colors=["red"],
        )
        art_green = Article(
            article_blueprint_id=bp.id,
            batch_id=batch.id,
            status=ArticleStatus.SOLD,
            colors=["green"],
        )
        db_session.add_all([art_red, art_green])
        db_session.commit()

        # Service level check with in_stock_only=True
        result_stock = catalog_service.list_latest_items(db_session, in_stock_only=True)
        assert result_stock["total_blueprints"] == 1
        assert len(result_stock["results"]) == 1
        assert "Red" in result_stock["results"][0].article_name

        # in_stock_only=False returns both variants
        result_all = catalog_service.list_latest_items(db_session, in_stock_only=False)
        assert result_all["total_blueprints"] == 1
        assert len(result_all["results"]) == 2

    def test_pagination_slices_by_variant_row(self, db_session):
        from datetime import date
        from src.models.wms import Supplier, Batch, Article, ArticleStatus

        brand = _make_brand(db_session, name="BrandMultiVar")
        bp = _make_blueprint(db_session, brand.id, article_name="T-Shirt")

        sup = Supplier(company_name="MultiVarSupplier")
        db_session.add(sup)
        db_session.commit()
        batch = Batch(supplier_id=sup.id, delivery_note_number="DDT-MV", document_date=date(2026, 1, 1))
        db_session.add(batch)
        db_session.commit()

        # 3 variants for the same blueprint
        a_white = Article(article_blueprint_id=bp.id, batch_id=batch.id, colors=["white"], status=ArticleStatus.AVAILABLE)
        a_black = Article(article_blueprint_id=bp.id, batch_id=batch.id, colors=["black"], status=ArticleStatus.AVAILABLE)
        a_blue = Article(article_blueprint_id=bp.id, batch_id=batch.id, colors=["blue"], status=ArticleStatus.AVAILABLE)
        db_session.add_all([a_white, a_black, a_blue])
        db_session.commit()

        client = TestClient(app)

        # Total variants = 3, Blueprint = 1
        # Page 1 (limit=2, offset=0)
        res1 = client.get("/api/v1/catalog/items?limit=2&offset=0")
        assert res1.status_code == 200
        d1 = res1.json()
        assert d1["total_blueprints"] == 1
        assert d1["total_items"] == 3
        assert d1["page"] == 1
        assert d1["total_pages"] == 2
        assert len(d1["results"]) == 2

        # Page 2 (limit=2, offset=2)
        res2 = client.get("/api/v1/catalog/items?limit=2&offset=2")
        assert res2.status_code == 200
        d2 = res2.json()
        assert d2["total_blueprints"] == 1
        assert d2["total_items"] == 3
        assert d2["page"] == 2
        assert d2["total_pages"] == 2
        assert len(d2["results"]) == 1

        # The variant names across pages should not overlap
        p1_names = {it["article_name"] for it in d1["results"]}
        p2_names = {it["article_name"] for it in d2["results"]}
        assert p1_names.isdisjoint(p2_names)
        assert len(p1_names | p2_names) == 3

    def test_warehouse_ui_pagination_styles_and_scripts_rendered(self, db_session):
        client = TestClient(app)
        res = client.get("/magazzino")
        assert res.status_code == 200
        assert "warehouse-pagination" in res.text
        assert "warehouse-pagination__btn" in res.text
        assert "warehouse-pagination__info" in res.text
        assert "main.js?v=" in res.text
        # Verify scrollbar rules for desktop and tablet/mobile
        assert "#catalog-results.warehouse-table-wrapper::-webkit-scrollbar" in res.text
        assert "scrollbar-color: var(--border-color) transparent" in res.text
        # Verify pagination banner is not sticky
        assert "position: sticky;\n    bottom: 0;\n    z-index: 30;\n    display: flex;\n    align-items: center;\n    justify-content: space-between;\n    padding: 10px 16px;\n    margin-top: 14px;\n    margin-bottom: 2px;\n    background: var(--bg-surface);\n    border: 1px solid var(--border-color);\n    border-radius: var(--radius-md);\n    box-shadow:" not in res.text


class TestListLatestItemsCodes:
    def test_vendor_code_and_barcode_exposed(self, db_session):
        from datetime import date
        from src.models.wms import Supplier, Batch, Article
        from src.services import catalog_service

        brand = _make_brand(db_session)
        bp = _make_blueprint(db_session, brand.id)
        sup = Supplier(company_name="Sup")
        db_session.add(sup)
        db_session.commit()
        batch = Batch(supplier_id=sup.id, delivery_note_number="DDT1", document_date=date(2026, 1, 1))
        db_session.add(batch)
        db_session.commit()
        db_session.add(Article(article_blueprint_id=bp.id, batch_id=batch.id,
                               supplier_code="ABC123", ean="8000000000017", colors=["red"]))
        db_session.commit()

        result = catalog_service.list_latest_items(db_session)
        assert result["results"][0].vendor_code == "ABC123"
        assert result["results"][0].barcode == "8000000000017"


class TestWarehouseEditAndDelete:
    def test_update_blueprint_vendor_code_and_barcode_by_color(self, db_session):
        from datetime import date
        from src.models.wms import Supplier, Batch, Article
        from src.services import catalog_service

        brand = _make_brand(db_session)
        bp = _make_blueprint(db_session, brand.id, article_name="Borsa Milano")
        sup = Supplier(company_name="SupEdit")
        db_session.add(sup)
        db_session.commit()
        batch = Batch(supplier_id=sup.id, delivery_note_number="DDT-EDIT", document_date=date(2026, 2, 1))
        db_session.add(batch)
        db_session.commit()

        art_black = Article(
            article_blueprint_id=bp.id,
            batch_id=batch.id,
            supplier_code="OLD-BLK",
            ean="8000000000011",
            colors=["black"],
        )
        art_white = Article(
            article_blueprint_id=bp.id,
            batch_id=batch.id,
            supplier_code="OLD-WHT",
            ean="8000000000022",
            colors=["white"],
        )
        db_session.add_all([art_black, art_white])
        db_session.commit()

        with patch("src.services.catalog_service.LLMClient") as MockLLM:
            mock_instance = MagicMock()
            mock_instance.generate_embedding_sync.return_value = [0.1] * 768
            MockLLM.return_value = mock_instance

            result = catalog_service.update_blueprint(
                db_session,
                bp.id,
                CatalogUpdateRequest(
                    article_name="Borsa Torino - Black",
                    vendor_code="NEW-BLK-99",
                    barcode="8000000000099",
                    color="black",
                ),
            )

        assert result.status == "success"
        assert "vendor_code" in result.updated_fields
        assert "barcode" in result.updated_fields
        db_session.refresh(bp)
        db_session.refresh(art_black)
        db_session.refresh(art_white)

        assert bp.article_name == "Borsa Torino"
        assert art_black.supplier_code == "NEW-BLK-99"
        assert art_black.ean == "8000000000099"
        assert art_white.supplier_code == "OLD-WHT"
        assert art_white.ean == "8000000000022"

    def test_delete_catalog_item_specific_color_variant_preserves_other_variants(self, db_session):
        from datetime import date
        from src.models.wms import Supplier, Batch, Article
        from src.models.pim import ArticlePhoto
        from src.services import catalog_service

        brand = _make_brand(db_session)
        bp = _make_blueprint(db_session, brand.id, article_name="Zaino Multi")
        sup = Supplier(company_name="SupDel")
        db_session.add(sup)
        db_session.commit()
        batch = Batch(supplier_id=sup.id, delivery_note_number="DDT-DEL", document_date=date(2026, 2, 1))
        db_session.add(batch)
        db_session.commit()

        art_red = Article(article_blueprint_id=bp.id, batch_id=batch.id, colors=["red"])
        art_blue = Article(article_blueprint_id=bp.id, batch_id=batch.id, colors=["blue"])
        photo_red = ArticlePhoto(article_blueprint_id=bp.id, canonical_color_name="red", photo_data=b"redbytes")
        photo_blue = ArticlePhoto(article_blueprint_id=bp.id, canonical_color_name="blue", photo_data=b"bluebytes")
        db_session.add_all([art_red, art_blue, photo_red, photo_blue])
        db_session.commit()

        res = catalog_service.delete_catalog_item(db_session, bp.id, color="red")
        assert res.blueprint_id == bp.id
        assert res.deleted_blueprint is False
        assert res.deleted_articles_count == 1

        # Blueprint and blue variant still exist
        assert db_session.query(ArticleBlueprint).filter_by(id=bp.id).first() is not None
        remaining_articles = db_session.query(Article).filter_by(article_blueprint_id=bp.id).all()
        assert len(remaining_articles) == 1
        assert remaining_articles[0].colors == ["blue"]
        remaining_photos = db_session.query(ArticlePhoto).filter_by(article_blueprint_id=bp.id).all()
        assert len(remaining_photos) == 1
        assert remaining_photos[0].canonical_color_name == "blue"

    def test_delete_catalog_item_last_variant_deletes_blueprint_and_dependents(self, db_session):
        from datetime import date
        from src.models.wms import Supplier, Batch, Article, ArticlePrice, MovementReason, ArticleMovement
        from src.models.pim import ArticlePhoto
        from src.services import catalog_service

        brand = _make_brand(db_session)
        bp = _make_blueprint(db_session, brand.id, article_name="Pochette Unica")
        sup = Supplier(company_name="SupSingle")
        reason = MovementReason(code="INTAKE_DEL_TEST", sign=1)
        db_session.add_all([sup, reason])
        db_session.commit()
        batch = Batch(supplier_id=sup.id, delivery_note_number="DDT-SGL", document_date=date(2026, 2, 1))
        db_session.add(batch)
        db_session.commit()

        art = Article(article_blueprint_id=bp.id, batch_id=batch.id, colors=["gold"])
        db_session.add(art)
        db_session.commit()

        price = ArticlePrice(article_id=art.id, list_price=120.0)
        mov = ArticleMovement(article_id=art.id, reason_id=reason.id, notes="Test")
        photo = ArticlePhoto(article_blueprint_id=bp.id, canonical_color_name="gold", photo_data=b"goldbytes")
        db_session.add_all([price, mov, photo])
        db_session.commit()

        res = catalog_service.delete_catalog_item(db_session, bp.id, color="gold")
        assert res.blueprint_id == bp.id
        assert res.deleted_blueprint is True
        assert res.deleted_articles_count == 1

        assert db_session.query(ArticleBlueprint).filter_by(id=bp.id).first() is None
        assert db_session.query(Article).filter_by(id=art.id).first() is None
        assert db_session.query(ArticlePrice).filter_by(article_id=art.id).first() is None
        assert db_session.query(ArticleMovement).filter_by(article_id=art.id).first() is None
        assert db_session.query(ArticlePhoto).filter_by(article_blueprint_id=bp.id).first() is None

    def test_delete_catalog_endpoint_and_404(self, db_session):
        client = TestClient(app)
        brand = _make_brand(db_session)
        bp = _make_blueprint(db_session, brand.id, article_name="Da Eliminare API")

        res = client.delete(f"/api/v1/catalog/{bp.id}")
        assert res.status_code == 200
        data = res.json()
        assert data["blueprint_id"] == bp.id
        assert data["deleted_blueprint"] is True

        res_404 = client.delete(f"/api/v1/catalog/{bp.id}")
        assert res_404.status_code == 404

    def test_warehouse_ui_elements_rendered(self, db_session):
        client = TestClient(app)
        # Root redirects to /vendite
        res_root = client.get("/", follow_redirects=False)
        assert res_root.status_code == 307
        assert res_root.headers["location"] == "/vendite"

        # Magazzino renders catalog UI and swapped tabs
        res = client.get("/magazzino")
        assert res.status_code == 200
        assert "Mostra solo articoli disponibili" in res.text
        assert "modal-edit-article" in res.text
        assert "modal-delete-article" in res.text
        assert res.text.index('data-tab="import-article"') < res.text.index('data-tab="import-ddt"')



class TestDeleteWithLegacyEmbeddingMeta:
    def test_delete_blueprint_removes_embedding_meta_rows(self, db_session):
        from sqlalchemy import text
        from src.services import catalog_service

        db_session.execute(text(
            'CREATE TABLE IF NOT EXISTS blueprint_embedding_meta ('
            'blueprint_id VARCHAR(36) NOT NULL PRIMARY KEY, text_hash VARCHAR(64) NOT NULL, '
            'model_name VARCHAR(100) NOT NULL, embedded_at DATETIME NOT NULL, '
            'FOREIGN KEY(blueprint_id) REFERENCES article_blueprints (id))'
        ))
        brand = _make_brand(db_session)
        bp = _make_blueprint(db_session, brand.id, article_name='Con Meta')
        db_session.execute(text(
            'INSERT INTO blueprint_embedding_meta VALUES (:i, :h, :m, :t)'
        ), {'i': bp.id, 'h': 'x', 'm': 'm', 't': '2026-01-01 00:00:00'})
        db_session.commit()

        res = catalog_service.delete_catalog_item(db_session, bp.id)
        assert res.deleted_blueprint is True
        left = db_session.execute(text(
            'SELECT COUNT(*) FROM blueprint_embedding_meta WHERE blueprint_id = :i'
        ), {'i': bp.id}).scalar()
        assert left == 0


class TestScanBarcodeEndpoint:
    def test_scan_barcode_endpoint_success(self, db_session):
        from fastapi.testclient import TestClient
        from src.main import app
        from src.services.barcode_service import create_ean13_test_image
        client = TestClient(app)
        test_code = "8000000000019"
        png_bytes = create_ean13_test_image(test_code)

        res = client.post("/api/v1/catalog/scan-barcode", content=png_bytes, headers={"Content-Type": "image/png"})
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert data["barcode"] == test_code
        assert data["format"] == "EAN-13"

    def test_scan_barcode_endpoint_no_barcode(self, db_session):
        from fastapi.testclient import TestClient
        from src.main import app
        from PIL import Image
        import io
        client = TestClient(app)
        # Blank white image with no barcode
        img = Image.new("RGB", (200, 100), color=(255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")

        res = client.post("/api/v1/catalog/scan-barcode", content=buf.getvalue(), headers={"Content-Type": "image/png"})
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "not_found"

    def test_scan_barcode_endpoint_empty_file(self, db_session):
        from fastapi.testclient import TestClient
        from src.main import app
        client = TestClient(app)
        res = client.post("/api/v1/catalog/scan-barcode", content=b"", headers={"Content-Type": "image/png"})
        assert res.status_code == 400


class TestUpdateBlueprintQuantityAndCategory:
    def test_update_blueprint_quantity_increase_and_decrease(self, db_session):
        from datetime import date
        from src.services import catalog_service
        from src.schemas.catalog import CatalogUpdateRequest
        from src.models.wms import Article, Batch, Supplier, ArticleStatus

        brand = _make_brand(db_session)
        cat = _make_category(db_session)
        bp = _make_blueprint(db_session, brand.id, category_id=cat.id, article_name="Zaino Travel")

        # Initial stock: 1 article
        sup = Supplier(company_name="Test Supplier")
        db_session.add(sup)
        db_session.flush()
        batch = Batch(supplier_id=sup.id, delivery_note_number="DDT-001", document_date=date.today())
        db_session.add(batch)
        db_session.flush()

        art = Article(
            article_blueprint_id=bp.id,
            batch_id=batch.id,
            supplier_code="ZN-01",
            status=ArticleStatus.AVAILABLE,
            colors=["nero"]
        )
        db_session.add(art)
        db_session.commit()

        # Increase stock to 3
        req_inc = CatalogUpdateRequest(quantity=3, color="nero")
        res_inc = catalog_service.update_blueprint(db_session, bp.id, req_inc)
        assert res_inc.status == "success"
        assert "quantity" in res_inc.updated_fields

        avail_count = db_session.query(Article).filter(
            Article.article_blueprint_id == bp.id,
            Article.status == ArticleStatus.AVAILABLE
        ).count()
        assert avail_count == 3

        # Decrease stock to 1
        req_dec = CatalogUpdateRequest(quantity=1, color="nero")
        res_dec = catalog_service.update_blueprint(db_session, bp.id, req_dec)
        assert res_dec.status == "success"

        avail_count_dec = db_session.query(Article).filter(
            Article.article_blueprint_id == bp.id,
            Article.status == ArticleStatus.AVAILABLE
        ).count()
        assert avail_count_dec == 1

        # Decrease stock to 0
        req_zero = CatalogUpdateRequest(quantity=0, color="nero")
        catalog_service.update_blueprint(db_session, bp.id, req_zero)
        avail_count_zero = db_session.query(Article).filter(
            Article.article_blueprint_id == bp.id,
            Article.status == ArticleStatus.AVAILABLE
        ).count()
        assert avail_count_zero == 0

    def test_update_blueprint_category_and_subcategory(self, db_session):
        from src.services import catalog_service
        from src.schemas.catalog import CatalogUpdateRequest
        from src.models.pim import Category

        brand = _make_brand(db_session)
        macro_cat = Category(name="Valigeria", description="Valigeria")
        db_session.add(macro_cat)
        db_session.flush()

        sub_cat = Category(name="Trolley", description="Trolley da viaggio", parent_id=macro_cat.id)
        db_session.add(sub_cat)

        other_macro = Category(name="Borse", description="Borse moda")
        db_session.add(other_macro)
        db_session.commit()

        bp = _make_blueprint(db_session, brand.id, category_id=None, article_name="Trolley Rigido")

        # Set to subcategory under Valigeria
        req1 = CatalogUpdateRequest(category_name="Valigeria", sub_category_name="Trolley")
        res1 = catalog_service.update_blueprint(db_session, bp.id, req1)
        assert "category" in res1.updated_fields
        db_session.refresh(bp)
        assert bp.category_id == sub_cat.id

        # Set to macro-category Borse
        req2 = CatalogUpdateRequest(category_name="Borse", sub_category_name="")
        res2 = catalog_service.update_blueprint(db_session, bp.id, req2)
        assert "category" in res2.updated_fields
        db_session.refresh(bp)
        assert bp.category_id == other_macro.id

    def test_warehouse_ui_quantity_and_category_elements_rendered(self, db_session):
        client = TestClient(app)
        res = client.get("/magazzino")
        assert res.status_code == 200
        # Desktop edit modal elements
        assert "edit-article-qty" in res.text
        assert "edit-article-macro-cat" in res.text
        assert "edit-article-sub-cat" in res.text
        # Mobile edit panel elements
        assert "mob-art-qty" in res.text
        assert "mob-art-macro-cat" in res.text
        assert "mob-art-sub-cat" in res.text

