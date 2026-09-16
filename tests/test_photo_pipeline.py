import pytest
from sqlalchemy.exc import IntegrityError
from src.models.pim import Brand, ArticleBlueprint, ArticlePhoto
from src.models.wms import Article, Batch, Supplier
from src.repositories.photo_repo import photo_repo
from src.services.data_ingestion_service import _resolve_items_with_photos
from src.schemas.data_ingestion import PhotoRetryRequest
from src.services.photo_service import retry_photo_search
from datetime import date
from src.repositories import staging_repo
from src.models.staging import JobStatus, JobType

def test_create_article_photo(db_session):
    brand = Brand(name="Test Brand")
    db_session.add(brand)
    db_session.commit()
    bp = ArticleBlueprint(brand_id=brand.id, article_name="Shirt", description="", extended_description="", tags=[])
    db_session.add(bp)
    db_session.commit()
    
    photo = photo_repo.create_photo(db_session, bp.id, "Blue", b"binarydata")
    
    assert photo.photo_data == b"binarydata"
    assert photo.canonical_color_name == "blue"

def test_unique_constraint_violation(db_session):
    brand = Brand(name="Test Brand")
    db_session.add(brand)
    db_session.commit()
    bp = ArticleBlueprint(brand_id=brand.id, article_name="Shirt", description="", extended_description="", tags=[])
    db_session.add(bp)
    db_session.commit()
    
    photo_repo.create_photo(db_session, bp.id, "red", b"1")
    
    photo2 = ArticlePhoto(article_blueprint_id=bp.id, canonical_color_name="red", photo_data=b"2")
    db_session.add(photo2)
    with pytest.raises(IntegrityError):
        db_session.commit()

def test_get_photo_by_blueprint_and_color_case_insensitive(db_session):
    brand = Brand(name="Test Brand")
    db_session.add(brand)
    db_session.commit()
    bp = ArticleBlueprint(brand_id=brand.id, article_name="Shirt", description="", extended_description="", tags=[])
    db_session.add(bp)
    db_session.commit()
    
    photo_repo.create_photo(db_session, bp.id, "GrEeN", b"data")
    
    photo = photo_repo.get_photo_by_blueprint_and_color(db_session, bp.id, "gReEn")
    assert photo is not None
    assert photo.canonical_color_name == "green"

class DummyHeuristic:
    def __init__(self, pattern):
        self.pattern = pattern

def test_caso_a_supplier_code_match(db_session):
    brand = Brand(name="Test Brand")
    db_session.add(brand)
    db_session.commit()
    bp = ArticleBlueprint(brand_id=brand.id, normalized_vendor_code="123##", article_name="Shirt", description="", extended_description="", tags=[])
    db_session.add(bp)
    db_session.commit()
    
    sup = Supplier(company_name="dummy")
    db_session.add(sup)
    db_session.commit()
    
    bat = Batch(supplier_id=sup.id, delivery_note_number="123", document_date=date.today())
    db_session.add(bat)
    db_session.commit()
    
    article = Article(article_blueprint_id=bp.id, batch_id=bat.id, supplier_code="123#RED", colors=["red"], status="AVAILABLE")
    db_session.add(article)
    db_session.commit()
    
    photo_repo.create_photo(db_session, bp.id, "red", b"data")
    
    extracted_items = [{"vendor_code": "123#RED"}]
    
    r_items, ur_items, r_bps, warns, po_items = _resolve_items_with_photos(
        db_session, brand.id, extracted_items, [DummyHeuristic(r"^(?P<model>\d+)#(?P<color_code>\w+)$")]
    )
    
    assert len(r_items) == 1
    assert "existing_photo_id" in r_items[0]
    assert len(po_items) == 0

def test_caso_a_supplier_code_no_match(db_session):
    brand = Brand(name="Test Brand")
    db_session.add(brand)
    db_session.commit()
    bp = ArticleBlueprint(brand_id=brand.id, normalized_vendor_code="123##", article_name="Shirt", description="", extended_description="", tags=[])
    db_session.add(bp)
    db_session.commit()
    
    extracted_items = [{"vendor_code": "123#BLUE"}]
    
    r_items, ur_items, r_bps, warns, po_items = _resolve_items_with_photos(
        db_session, brand.id, extracted_items, [DummyHeuristic(r"^(?P<model>\d+)#(?P<color_code>\w+)$")]
    )
    
    assert len(po_items) == 1
    assert po_items[0]["needs_photo_only"] is True

def test_caso_b_a_exact_color_match(db_session):
    brand = Brand(name="Test Brand")
    db_session.add(brand)
    db_session.commit()
    bp = ArticleBlueprint(brand_id=brand.id, normalized_vendor_code="456", article_name="Shirt", description="", extended_description="", tags=[])
    db_session.add(bp)
    db_session.commit()
    
    photo_repo.create_photo(db_session, bp.id, "navy", b"data")
    
    extracted_items = [{"vendor_code": "456", "colors": ["navy"]}]
    
    r_items, ur_items, r_bps, warns, po_items = _resolve_items_with_photos(
        db_session, brand.id, extracted_items, [DummyHeuristic(r"^456$")]
    )
    
    assert "existing_photo_id" in r_items[0]
    assert len(po_items) == 0

def test_caso_b_b_no_match_gallery(db_session):
    brand = Brand(name="Test Brand")
    db_session.add(brand)
    db_session.commit()
    bp = ArticleBlueprint(brand_id=brand.id, normalized_vendor_code="456", article_name="Shirt", description="", extended_description="", tags=[])
    db_session.add(bp)
    db_session.commit()
    
    extracted_items = [{"vendor_code": "456", "colors": ["cyan"]}]
    
    r_items, ur_items, r_bps, warns, po_items = _resolve_items_with_photos(
        db_session, brand.id, extracted_items, [DummyHeuristic(r"^456$")]
    )
    
    assert len(po_items) == 1
    assert po_items[0]["needs_photo_only"] is True

@pytest.mark.asyncio
async def test_retry_with_new_url_short_circuit(db_session, monkeypatch):
    job_id = "test_job"
    data = {
        "items": [{"item_id": "item1", "article_name": "T-Shirt"}],
        "photo_proposals": [{
            "item_id": "item1",
            "photo_url": "old",
            "retry_count": 0,
            "_photo_search_ctx": []
        }]
    }
    staging_repo.create_job(db_session, job_id, JobType.DDT_IMPORT.value)
    staging_repo.update_job(db_session, job_id, JobStatus.COMPLETED.value, data)
    
    # Mock httpx AsyncClient
    class MockResponse:
        status_code = 200
        headers = {"content-type": "image/jpeg"}
        
    class MockAsyncClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass
        async def head(self, url, **kwargs):
            return MockResponse()
            
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)
    
    req = PhotoRetryRequest(item_id="item1", model_ok=True, color_ok=True, new_url="https://via.placeholder.com/1")
    
    res = await retry_photo_search(db_session, job_id, req)
    
    assert res["photo_url"] == "https://via.placeholder.com/1"
    assert res["photo_source"] == "manual_override"
