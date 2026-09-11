import pytest
from src.models.pim import Brand, BrandHeuristic, Category
from src.services.data_ingestion_service import process_and_stage_single_item
from src.schemas.data_ingestion import SingleItemIngestionRequest
from src.repositories import staging_repo
from src.models.staging import JobStatus

class MockSessionLocal:
    def __init__(self, session):
        self.session = session
    def __enter__(self):
        return self.session
    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

def test_structural_heuristic_bypasses_color_extraction(db_session, monkeypatch):
    """
    Test that a structural heuristic (without color_code capture group) correctly
    validates the structure and avoids appending a heuristic_break warning,
    leaving the vendor code unmodified.
    """
    monkeypatch.setattr("src.services.data_ingestion_service.SessionLocal", lambda: MockSessionLocal(db_session))
    
    test_brand = Brand(name="StructuralBrand")
    db_session.add(test_brand)
    
    test_cat = Category(name="Borse", description="Borse")
    db_session.add(test_cat)
    db_session.commit()
    
    # A structural pattern without a color_code capture group
    test_heuristic = BrandHeuristic(brand_id=test_brand.id, pattern="^[A-Z]{4}[0-9]{7}$", explanation="No color")
    db_session.add(test_heuristic)
    db_session.commit()

    # Mock the ArticleBlueprintsAgent
    class MockBlueprintsAgent:
        async def aexecute(self, input_data):
            return {
                "items": input_data["new_blueprints"][0]["cluster_items"],
                "blueprints": [
                    {
                        "id": "mock-bp-id",
                        "is_new": True,
                        "article_name": "Mock Item",
                        "description": "Mock desc",
                        "category": "Borse",
                        "sub_category": "Tote",
                        "extended_description": "Ext desc",
                        "tags": [],
                        "materials": [],
                        "dimensions": None,
                    }
                ],
                "warnings": []
            }
    
    monkeypatch.setattr("src.services.data_ingestion_service.ArticleBlueprintsAgent", MockBlueprintsAgent)
    
    # Create a staging job for the test
    job_id = "test-structural-job"
    staging_repo.create_job(db_session, job_id=job_id, job_type="single_item_import")
    
    req = SingleItemIngestionRequest(
        brand_id=str(test_brand.id),
        vendor_code="ABCD1234567",
        article_name="Structural Bag",
        colors=[]
    )
    
    # Call the actual service method (we need to run the async method in a sync context for tests)
    import asyncio
    asyncio.run(process_and_stage_single_item(job_id, req))
    
    job = staging_repo.get_job(db_session, job_id)
    assert job is not None
    assert job.status == JobStatus.COMPLETED.value
    
    # Verify no heuristic_break warning was added
    assert "warnings" in job.data
    for w in job.data["warnings"]:
        assert "heuristic_break" not in w

    # Verify the normalized code is identical to raw code (since no color_code extraction)
    assert "items" in job.data
    assert len(job.data["items"]) == 1
    assert job.data["items"][0]["normalized_vendor_code"] == "ABCD1234567"
