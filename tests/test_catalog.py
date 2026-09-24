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
                    tags=None, materials=None, embedding=None):
    bp = ArticleBlueprint(
        brand_id=brand_id,
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
