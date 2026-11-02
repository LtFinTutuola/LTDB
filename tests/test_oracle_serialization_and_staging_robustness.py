"""
Test di unità e integrazione per la serializzazione delle dimensioni dell'Oracolo
e la robustezza dei background tasks di staging in caso di eccezioni.
"""
import uuid
import pytest
from unittest.mock import patch, MagicMock
from sqlalchemy.orm import Session

from src.models.staging import StagingArea, JobStatus, JobType
from src.repositories import staging_repo
from src.agents.article_blueprints_agent.nodes.model_equivalence_oracle import DimensionEvaluationResult
from src.agents.article_blueprints_agent.nodes.oracle_and_induction_node import _serialize_dimensions
from src.schemas.data_ingestion import SingleItemIngestionRequest
from src.services import data_ingestion_service


def test_serialize_dimensions_with_pydantic_models():
    """Verifica che _serialize_dimensions converta correttamente oggetti DimensionEvaluationResult in dict."""
    dim_res = DimensionEvaluationResult(
        dimension="TYPOLOGY",
        extracted_value_x="Zaino",
        extracted_value_y="Zaino",
        verdict="MATCH",
        reasoning="Entrambi zaini",
    )
    dims = {
        "TYPOLOGY": dim_res,
        "LINE": {"dimension": "LINE", "verdict": "MATCH", "reasoning": "MD20"},
        "SIZE": "UNKNOWN",
    }
    serialized = _serialize_dimensions(dims)
    assert isinstance(serialized, dict)
    assert serialized["TYPOLOGY"]["verdict"] == "MATCH"
    assert serialized["TYPOLOGY"]["extracted_value_x"] == "Zaino"
    assert serialized["LINE"]["verdict"] == "MATCH"
    assert serialized["SIZE"] == "UNKNOWN"


def test_serialize_dimensions_non_dict_fallback():
    """Verifica il comportamento di fallback su input non dizionario."""
    assert _serialize_dimensions(None) == {}
    assert _serialize_dimensions(["item"]) == {}


def test_update_job_with_serialized_oracle_dimensions(db_session: Session):
    """Verifica che i record con oracle.dimensions serializzati vengano salvati nel DB SQLite senza StatementError."""
    job_id = str(uuid.uuid4())
    job = staging_repo.create_job(db_session, job_id=job_id, job_type=JobType.SINGLE_ITEM_IMPORT.value)
    assert job.status == JobStatus.ACCEPTED.value

    dim_eval = DimensionEvaluationResult(
        dimension="MATERIAL",
        extracted_value_x="tessuto",
        extracted_value_y="tessuto",
        verdict="MATCH",
        reasoning="Compatibili",
    )

    data = {
        "heuristic_reconciliations": [
            {
                "reconciliation_id": str(uuid.uuid4()),
                "kind": "DISAMBIGUATION",
                "oracle": {
                    "final_score": 104.0,
                    "code_verdict": "VERY_HIGH",
                    "color_relation": "DIFFERENT",
                    "dimensions": _serialize_dimensions({"MATERIAL": dim_eval}),
                },
            }
        ]
    }

    # Deve completarsi senza sollevare TypeError o StatementError
    updated = staging_repo.update_job(db_session, job_id=job_id, status=JobStatus.COMPLETED.value, data=data)
    assert updated is not None
    assert updated.status == JobStatus.COMPLETED.value
    assert updated.data["heuristic_reconciliations"][0]["oracle"]["dimensions"]["MATERIAL"]["verdict"] == "MATCH"


def test_staging_repo_update_job_rollback_on_commit_error(db_session: Session):
    """Verifica che un errore durante il commit in update_job esegua rollback automatico."""
    job_id = str(uuid.uuid4())
    staging_repo.create_job(db_session, job_id=job_id, job_type=JobType.SINGLE_ITEM_IMPORT.value)

    with patch.object(db_session, "commit", side_effect=RuntimeError("Simulated DB lock or commit error")):
        with pytest.raises(RuntimeError, match="Simulated DB lock or commit error"):
            staging_repo.update_job(db_session, job_id=job_id, status=JobStatus.COMPLETED.value, data={})

    # Dopo l'eccezione, la sessione non deve essere in PendingRollbackError
    # e una nuova query deve funzionare normalmente
    check_job = staging_repo.get_job(db_session, job_id)
    assert check_job is not None
    assert check_job.status == JobStatus.ACCEPTED.value


@pytest.mark.asyncio
async def test_process_and_stage_single_item_handles_generic_exception(db_session: Session):
    """Verifica che una Exception generica durante single-item ingestion imposti JobStatus.ERROR."""
    job_id = str(uuid.uuid4())
    staging_repo.create_job(db_session, job_id=job_id, job_type=JobType.SINGLE_ITEM_IMPORT.value)

    from src.models.pim import Brand
    fake_brand = Brand(id="test-brand", name="TestBrand")
    db_session.add(fake_brand)
    db_session.commit()

    req = SingleItemIngestionRequest(
        brand_id="test-brand",
        vendor_code="CODE123",
        article_name="Test Product",
        quantity=1,
        colors=["black"],
    )

    with patch("src.services.data_ingestion_service.SessionLocal", return_value=db_session), \
         patch("src.services.data_ingestion_service._stage_extracted_items", side_effect=Exception("Database connection dropped")):
        with pytest.raises(Exception, match="Database connection dropped"):
            await data_ingestion_service.process_and_stage_single_item(job_id, req)

    # Il job deve essere stato aggiornato a JobStatus.ERROR
    job = staging_repo.get_job(db_session, job_id)
    assert job.status == JobStatus.ERROR.value
    assert "Database connection dropped" in job.data.get("error", "")


@pytest.mark.asyncio
async def test_process_and_stage_pdf_handles_generic_exception(db_session: Session):
    """Verifica che una Exception generica durante PDF ingestion imposti JobStatus.ERROR."""
    job_id = str(uuid.uuid4())
    staging_repo.create_job(db_session, job_id=job_id, job_type=JobType.DDT_IMPORT.value)

    with patch("src.services.data_ingestion_service.SessionLocal", return_value=db_session), \
         patch("src.services.data_ingestion_service.DataExtractionAgent") as mock_agent_cls:
        mock_agent_instance = MagicMock()
        mock_agent_instance.aexecute.side_effect = RuntimeError("PDF parser memory corruption")
        mock_agent_cls.return_value = mock_agent_instance

        # Mock brand resolution
        from src.models.pim import Brand
        fake_brand = Brand(id="brand-123", name="TestBrand")
        db_session.add(fake_brand)
        db_session.commit()

        with pytest.raises(RuntimeError, match="PDF parser memory corruption"):
            await data_ingestion_service.process_and_stage_pdf(job_id, "/tmp/fake.pdf", "brand-123")

    job = staging_repo.get_job(db_session, job_id)
    assert job.status == JobStatus.ERROR.value
    assert "PDF parser memory corruption" in job.data.get("error", "")
