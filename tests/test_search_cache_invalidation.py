import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi import BackgroundTasks
from sqlalchemy.orm import Session

from src.models.pim import Brand, Category, ArticleBlueprint
from src.models.wms import Batch, Article, Supplier, ArticleStatus
from src.models.staging import StagingArea, JobStatus, JobType
from src.schemas.catalog import CatalogUpdateRequest
from src.core.cache import search_snapshot_cache
from src.services.search_service import invalidate_search_cache, execute_search
from src.services.catalog_service import delete_catalog_item, update_blueprint
from src.services.data_ingestion_service import confirm_and_persist_staging


def _setup_test_catalog(db: Session):
    brand = Brand(name="Mandarina Duck")
    cat = Category(name="Portafogli", description="Accessori di piccola pelletteria")
    db.add_all([brand, cat])
    db.flush()

    bp = ArticleBlueprint(
        brand_id=brand.id,
        category_id=cat.id,
        article_name="Mandarina Duck MD20",
        description="Portafoglio MD20 zip",
        extended_description="Portafoglio iconico Mandarina Duck",
        normalized_vendor_code="P10QMPN1651",
        tags=["mandarina", "portafoglio"],
        materials=["mitrix"],
    )
    db.add(bp)
    db.flush()

    supplier = Supplier(company_name="Supplier 1")
    db.add(supplier)
    db.flush()

    from datetime import date
    batch = Batch(supplier_id=supplier.id, delivery_note_number="DDT-001", document_date=date.today())
    db.add(batch)
    db.flush()

    art1 = Article(
        article_blueprint_id=bp.id,
        batch_id=batch.id,
        supplier_code="P10QMPN1651-NERO",
        ean="8032803701234",
        colors=["nero"],
        status=ArticleStatus.AVAILABLE,
    )
    art2 = Article(
        article_blueprint_id=bp.id,
        batch_id=batch.id,
        supplier_code="P10QMPN1651-BLU",
        ean="8032803701235",
        colors=["blu"],
        status=ArticleStatus.AVAILABLE,
    )
    db.add_all([art1, art2])
    db.commit()
    return brand, cat, bp, art1, art2


class TestSearchCacheInvalidation:

    def test_direct_cache_invalidation(self):
        search_snapshot_cache.clear()
        search_snapshot_cache.set("mandarina::False", {"candidates": [{"id": "abc"}]})
        assert len(search_snapshot_cache) == 1

        invalidate_search_cache()
        assert len(search_snapshot_cache) == 0

    def test_delete_catalog_item_clears_search_cache(self, db_session):
        brand, cat, bp, art1, art2 = _setup_test_catalog(db_session)

        # Populate cache
        search_snapshot_cache.set("mandarina::False", {"candidates": [{"id": str(bp.id)}]})
        assert len(search_snapshot_cache) == 1

        # Delete variant 'nero'
        result = delete_catalog_item(db_session, str(bp.id), color="nero")
        assert result.deleted_articles_count == 1
        assert len(search_snapshot_cache) == 0

    def test_update_blueprint_clears_search_cache(self, db_session):
        brand, cat, bp, art1, art2 = _setup_test_catalog(db_session)

        # Populate cache
        search_snapshot_cache.set("mandarina::False", {"candidates": [{"id": str(bp.id)}]})
        assert len(search_snapshot_cache) == 1

        # Update blueprint
        req = CatalogUpdateRequest(description="Nuova descrizione aggiornata")
        update_blueprint(db_session, str(bp.id), req)
        assert len(search_snapshot_cache) == 0

    @pytest.mark.asyncio
    async def test_confirm_ingestion_clears_search_cache(self, db_session):
        brand, cat, bp, art1, art2 = _setup_test_catalog(db_session)

        # Create a staging job
        job_data = {
            "brand_id": str(brand.id),
            "items": [
                {
                    "vendor_code": "P10QMPN8651",
                    "article_name": "Mandarina Duck MD20 Rosso",
                    "product_short_description": "Portafoglio Rosso",
                    "product_extended_description": "Descrizione estesa rosso",
                    "colors": ["rosso"],
                    "quantity": 2,
                    "price": 75.0,
                    "brand": {"name": "Mandarina Duck", "id": str(brand.id)},
                    "category": {"name": "Portafogli", "id": str(cat.id), "description": "Portafogli e piccola pelletteria"},
                    "sub_category": None,
                    "tags": ["mandarina", "rosso"],
                    "materials": ["mitrix"],
                    "dimensions": None,
                    "blueprint_group_id": "P10QMPN8651",
                }
            ],
            "blueprints": [
                {
                    "id": "P10QMPN8651",
                    "article_name": "Mandarina Duck MD20 Rosso",
                    "description": "Portafoglio Rosso",
                    "extended_description": "Descrizione estesa rosso",
                    "tags": ["mandarina", "rosso"],
                    "materials": ["mitrix"],
                    "dimensions": None,
                    "is_new": True,
                }
            ],
            "photo_proposals": [],
        }

        job = StagingArea(
            job_type=JobType.DDT_IMPORT.value,
            status=JobStatus.COMPLETED.value,
            file_path="test_ingestion.xlsx",
            data=job_data,
        )
        db_session.add(job)
        db_session.commit()

        # Populate cache
        search_snapshot_cache.set("mandarina::False", {"candidates": [{"id": str(bp.id)}]})
        assert len(search_snapshot_cache) == 1

        bg = BackgroundTasks()
        await confirm_and_persist_staging(db_session, str(job.id), bg)
        assert len(search_snapshot_cache) == 0

    @pytest.mark.asyncio
    async def test_search_reflects_immediate_db_changes_without_stale_cache(self, db_session):
        brand, cat, bp, art1, art2 = _setup_test_catalog(db_session)

        # Mock LLM Agent to return exact plan for "Mandarina"
        from src.agents.warehouse_search_agent.state import SearchQueryPlan
        mock_plan = SearchQueryPlan(
            brand="Mandarina Duck",
            category=None,
            color=None,
            keywords=["portafoglio"],
        )

        with patch("src.agents.warehouse_search_agent.nodes.query_plan_node.LLMClient") as MockPlanLLM:
            p_client = MagicMock()
            p_client.call = AsyncMock(return_value=mock_plan.model_dump_json())
            MockPlanLLM.return_value = p_client

            # Step 1: Initial search
            res1 = await execute_search(db_session, "Mandarina", in_stock_only=False)
            assert res1["total_items"] == 2  # 2 variants (nero, blu)
            assert len(search_snapshot_cache) == 1

            # Step 2: Delete variant 'blu'
            delete_catalog_item(db_session, str(bp.id), color="blu")
            # Cache is now invalidated!
            assert len(search_snapshot_cache) == 0

            # Step 3: Next search runs fresh against DB
            res2 = await execute_search(db_session, "Mandarina", in_stock_only=False)
            assert res2["total_items"] == 1
            assert res2["results"][0]["colors"] == ["nero"]
