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
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass
        def stream(self, method, url, **kwargs):
            class MockStreamContext:
                async def __aenter__(self):
                    return MockResponse()
                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass
            return MockStreamContext()
            
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", MockAsyncClient)
    
    req = PhotoRetryRequest(item_id="item1", new_url="https://via.placeholder.com/1")
    
    res = await retry_photo_search(db_session, job_id, req)
    
    assert res["photo_url"] == "https://via.placeholder.com/1"
    assert res["photo_source"] == "manual_override"

@pytest.mark.asyncio
async def test_url_guard_preserves_previous_url_on_rejection():
    """When the URL Guard rejects the new LLM candidate, the previous valid
    photo_url must be preserved in staging — not overwritten with None."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from src.services.photo_service import retry_photo_search
    from src.repositories import staging_repo
    from src.models.staging import JobStatus, JobType
    from src.schemas.data_ingestion import PhotoRetryRequest

    # Bootstrap an in-memory DB session via conftest fixture pattern
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from src.models.base import Base

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    job_id = "guard_preserve_job"
    previous_valid_url = "https://valid.cdn.com/product_image.jpg"
    data = {
        "brand_id": None,
        "items": [{"item_id": "item_x", "article_name": "Test Bag"}],
        "photo_proposals": [{
            "item_id": "item_x",
            "photo_url": previous_valid_url,
            "photo_source": "web_search",
            "canonical_color_candidate": "red",
            "retry_count": 1,
            "_photo_search_ctx": [
                {"role": "user", "content": "cerca"},
                {"role": "model", "content": previous_valid_url},
            ]
        }]
    }
    staging_repo.create_job(db, job_id, JobType.DDT_IMPORT.value)
    staging_repo.update_job(db, job_id, JobStatus.COMPLETED.value, data)

    # Guard will reject the new URL (404 response)
    mock_response_404 = MagicMock()
    mock_response_404.status_code = 404
    mock_response_404.headers = {}

    mock_llm_client = MagicMock()
    mock_llm_client.call_with_grounding = AsyncMock(
        return_value=("https://hallucinated.example.com/fake.jpg", [])
    )

    mock_httpx_client = AsyncMock()
    mock_httpx_client.__aenter__ = AsyncMock(return_value=mock_httpx_client)
    mock_httpx_client.__aexit__ = AsyncMock(return_value=False)
    class MockStreamContext:
        async def __aenter__(self):
            return mock_response_404
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_httpx_client.stream = MagicMock(return_value=MockStreamContext())

    mock_llm_client.call = AsyncMock(
        return_value="test query"
    )

    req = PhotoRetryRequest(item_id="item_x", user_feedback="wrong color")

    with patch("src.services.photo_service.LLMClient", return_value=mock_llm_client), \
         patch("src.agents.article_blueprints_agent.nodes.color_photo_search_node.httpx.AsyncClient", return_value=mock_httpx_client):
        res = await retry_photo_search(db, job_id, req)

    # The guard rejected the new URL: previous valid URL must be preserved
    assert res["photo_url"] == previous_valid_url, \
        f"Expected previous URL to be preserved, got: {res['photo_url']}"
    # retry_count must still increment
    assert res["retry_count"] == 2

    db.close()

@pytest.mark.asyncio
async def test_url_guard_rejects_hallucinated_url():
    """URL Guard must reject URLs that return non-image or error status codes."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from src.agents.article_blueprints_agent.nodes.color_photo_search_node import _validate_photo_url

    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.headers = {}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    class MockStreamContext:
        async def __aenter__(self):
            return mock_response
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_client.stream = MagicMock(return_value=MockStreamContext())

    with patch("src.agents.article_blueprints_agent.nodes.color_photo_search_node.httpx.AsyncClient", return_value=mock_client):
        result = await _validate_photo_url("https://img.giglio.com/images/prodotti/FAKEURL_1.jpg")
    assert result is False

@pytest.mark.asyncio
async def test_url_guard_accepts_valid_image_url():
    """URL Guard must accept URLs that return a valid image content-type."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from src.agents.article_blueprints_agent.nodes.color_photo_search_node import _validate_photo_url

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "image/jpeg"}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    class MockStreamContext:
        async def __aenter__(self):
            return mock_response
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_client.stream = MagicMock(return_value=MockStreamContext())

    with patch("src.agents.article_blueprints_agent.nodes.color_photo_search_node.httpx.AsyncClient", return_value=mock_client):
        result = await _validate_photo_url("https://assets.armani.com/real_product.jpg")
    assert result is True

@pytest.mark.asyncio
async def test_url_guard_rejects_null_response(monkeypatch):
    """URL Guard must return False if model responded with NULL (no grounding found)."""
    from src.agents.article_blueprints_agent.nodes.color_photo_search_node import _validate_photo_url

    result = await _validate_photo_url("NULL")
    assert result is False

    result2 = await _validate_photo_url(None)
    assert result2 is False
