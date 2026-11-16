import asyncio
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks, HTTPException

from src.agents.article_blueprints_agent.agent import ArticleBlueprintsAgent
from src.agents.article_blueprints_agent.nodes.web_search_blueprints_node import (
    _enrich_blueprint_cluster,
)
from src.models.pim import (
    ArticleBlueprint,
    ArticlePhoto,
    Brand,
    Category,
    HeuristicMode,
)
from src.models.staging import JobStatus, JobType
from src.models.wms import Article, Batch, MovementReason, Supplier
from src.repositories import staging_repo
from src.repositories.pim_repo import heuristic_repo
from src.schemas.data_ingestion import RevisionOperation
from src.services.data_ingestion_service import (
    _resolve_items_with_photos,
    confirm_and_persist_staging,
)
from src.services.staging_revision_service import revise_staging_data


def _seed_world(db_session, brand_name="TommyTest"):
    brand = Brand(name=brand_name)
    cat = Category(name=f"Cinture-{brand_name}", description="Cinture")
    sup = Supplier(company_name=f"Sup-{brand_name}")
    reason = db_session.query(MovementReason).filter_by(code="INGESTION").first()
    if not reason:
        reason = MovementReason(code="INGESTION", sign=1)
        db_session.add(reason)
    db_session.add_all([brand, cat, sup])
    db_session.commit()
    brand.categories.append(cat)
    batch = Batch(supplier_id=sup.id, delivery_note_number="DDT-BASE", document_date=date.today())
    db_session.add(batch)
    db_session.commit()
    return brand, cat, batch


# ===========================================================================
# Bug 2: Safeguard COLOR_AGNOSTIC + Two-phase Lookup + Per-Heuristic + Retroactive Fix
# ===========================================================================


def test_bug2_safeguard_triggers_on_same_code_different_or_double_color(db_session):
    brand, cat, batch = _seed_world(db_session, "TommyDoubleColor")
    h = heuristic_repo.create_heuristic(
        db_session,
        brand_id=str(brand.id),
        heuristic_mode=HeuristicMode.PENDING_COHORT,
        heuristic_confirmed=False,
    )
    bp = ArticleBlueprint(
        brand_id=brand.id,
        category_id=cat.id,
        heuristic_id=h.id,
        normalized_vendor_code="AM0AM14569DW6",
        skeleton_vendor_code="AM0AM14569DW6",
        article_name="Cintura Reversibile",
        description="Cintura",
        extended_description="Cintura pelle",
        tags=["cintura"],
    )
    db_session.add(bp)
    db_session.commit()

    art = Article(
        article_blueprint_id=bp.id,
        batch_id=batch.id,
        supplier_code="AM0AM14569DW6",
        colors=["black/blu"],
    )
    db_session.add(art)
    db_session.commit()

    # Exact full set ["black/blu"] vs ["black/blu"] -> no safeguard
    res_same, unres_same, _, warns_same, _ = _resolve_items_with_photos(
        db_session,
        str(brand.id),
        [{"item_id": "it-same", "vendor_code": "AM0AM14569DW6", "colors": ["black/blu"], "quantity": 1}],
        [h],
    )
    assert len(res_same) == 1 and len(unres_same) == 0
    assert "_safeguard_reconciliation" not in res_same[0]
    assert warns_same == []

    # Double color partial mismatch: ["blu"] vs existing ["black/blu"] -> MUST trigger safeguard!
    res_diff, unres_diff, _, warns_diff, _ = _resolve_items_with_photos(
        db_session,
        str(brand.id),
        [{"item_id": "it-diff", "vendor_code": "AM0AM14569DW6", "colors": ["blu"], "quantity": 1}],
        [h],
    )
    assert len(res_diff) == 1 and len(unres_diff) == 0
    rec = res_diff[0].get("_safeguard_reconciliation")
    assert rec is not None
    assert rec["kind"] == "COLOR_AGNOSTIC_SAFEGUARD"
    assert rec["status"] == "PENDING_USER_CONFIRMATION"
    assert rec["requires_confirmation"] is True
    assert rec["heuristic_id"] == str(h.id)
    assert rec["existing_colors"] == ["black/blu"]
    assert rec["incoming_colors"] == ["blu"]
    assert len(warns_diff) == 1


def test_bug2_two_phase_lookup_normalized_to_raw_variable_length(db_session):
    brand, cat, batch = _seed_world(db_session, "PiquadroTwoPhase")
    h = heuristic_repo.create_heuristic(
        db_session,
        brand_id=str(brand.id),
        heuristic_mode=HeuristicMode.PENDING_COHORT,
        heuristic_confirmed=False,
    )
    bp = ArticleBlueprint(
        brand_id=brand.id,
        category_id=cat.id,
        heuristic_id=h.id,
        normalized_vendor_code="PU1241B2R@BLU2",
        skeleton_vendor_code="PU1241B2RBLU2",
        article_name="Portafoglio",
        description="Portafoglio pelle",
        extended_description="Portafoglio pelle",
        tags=["portafoglio"],
    )
    db_session.add(bp)
    db_session.commit()

    art = Article(
        article_blueprint_id=bp.id,
        batch_id=batch.id,
        supplier_code="PU1241B2R//BLU2",
        colors=["blu"],
    )
    db_session.add(art)
    db_session.commit()

    # Different separator spelling collides on PU1241B2R@BLU2, phase 2 checks sterilized raw code
    res, unres, _, _, _ = _resolve_items_with_photos(
        db_session,
        str(brand.id),
        [{"item_id": "it-2p", "vendor_code": "PU1241B2R--BLU2", "colors": ["marrone"], "quantity": 1}],
        [h],
    )
    assert len(res) == 1 and len(unres) == 0
    rec = res[0].get("_safeguard_reconciliation")
    assert rec is not None
    assert rec["kind"] == "COLOR_AGNOSTIC_SAFEGUARD"
    assert rec["existing_colors"] == ["blu"]
    assert rec["incoming_colors"] == ["marrone"]


def test_bug2_per_heuristic_isolation_for_heterogeneous_brand(db_session):
    brand, cat, batch = _seed_world(db_session, "HeteroBrand")

    h_masked = heuristic_repo.create_heuristic(
        db_session,
        brand_id=str(brand.id),
        pattern="",
        heuristic_mode=HeuristicMode.COLOR_MASKED_COMPACT,
        heuristic_confirmed=True,
        heuristic_metadata={"rules": [{"type": "FIXED", "length": 13, "masked_positions": [10, 11, 12], "signature": "AAAAAAAAAA"}], "negative_constraints": []},
    )
    h_agnostic = heuristic_repo.create_heuristic(
        db_session,
        brand_id=str(brand.id),
        pattern="",
        heuristic_mode=HeuristicMode.COLOR_AGNOSTIC,
        heuristic_confirmed=True,
    )

    bp_masked = ArticleBlueprint(
        brand_id=brand.id,
        category_id=cat.id,
        heuristic_id=h_masked.id,
        normalized_vendor_code="AM0AM13576###",
        skeleton_vendor_code="AM0AM13576",
        article_name="Cintura Denton",
        description="Cintura",
        extended_description="Cintura",
        tags=["cintura"],
    )
    bp_agnostic = ArticleBlueprint(
        brand_id=brand.id,
        category_id=cat.id,
        heuristic_id=h_agnostic.id,
        normalized_vendor_code="AGNO999",
        skeleton_vendor_code="AGNO999",
        article_name="Cintura Agnostica",
        description="Cintura",
        extended_description="Cintura",
        tags=["cintura"],
    )
    db_session.add_all([bp_masked, bp_agnostic])
    db_session.commit()

    db_session.add_all([
        Article(article_blueprint_id=bp_masked.id, batch_id=batch.id, supplier_code="AM0AM13576DW6", colors=["dw6"]),
        Article(article_blueprint_id=bp_agnostic.id, batch_id=batch.id, supplier_code="AGNO999", colors=["nero"]),
    ])
    db_session.commit()

    # 1) bp_agnostic already uses h_agnostic (COLOR_AGNOSTIC) -> no safeguard on color mismatch
    res_a, _, _, _, _ = _resolve_items_with_photos(
        db_session,
        str(brand.id),
        [{"item_id": "it-a", "vendor_code": "AGNO999", "colors": ["blu"], "quantity": 1}],
        [h_masked, h_agnostic],
    )
    assert "_safeguard_reconciliation" not in res_a[0]

    # 2) bp_masked uses h_masked (NOT COLOR_AGNOSTIC) -> safeguard MUST trigger with heuristic_id = h_masked.id
    res_m, _, _, _, _ = _resolve_items_with_photos(
        db_session,
        str(brand.id),
        [{"item_id": "it-m", "vendor_code": "AM0AM13576DW6", "colors": ["black/blu"], "quantity": 1}],
        [h_masked, h_agnostic],
    )
    sg_rec = res_m[0].get("_safeguard_reconciliation")
    assert sg_rec is not None
    assert sg_rec["heuristic_id"] == str(h_masked.id)


def test_bug2_safeguard_confirm_promotes_only_target_heuristic_and_persists(db_session):
    brand, cat, batch = _seed_world(db_session, "ConfirmSafeguardBrand")
    h = heuristic_repo.create_heuristic(
        db_session,
        brand_id=str(brand.id),
        heuristic_mode=HeuristicMode.PENDING_COHORT,
        heuristic_confirmed=False,
    )
    bp = ArticleBlueprint(
        brand_id=brand.id,
        category_id=cat.id,
        heuristic_id=h.id,
        normalized_vendor_code="BELT100",
        skeleton_vendor_code="BELT100",
        article_name="Cintura Classic",
        description="Cintura",
        extended_description="Cintura pelle",
        tags=["cintura"],
    )
    db_session.add(bp)
    db_session.commit()

    db_session.add(Article(article_blueprint_id=bp.id, batch_id=batch.id, supplier_code="BELT100", colors=["nero"]))
    db_session.commit()

    job_id = "job-sg-confirm"
    rec_id = "rec-sg-confirm"
    staging_repo.create_job(db_session, job_id=job_id, job_type=JobType.SINGLE_ITEM_IMPORT.value)
    staging_repo.update_job(
        db_session,
        job_id=job_id,
        status=JobStatus.COMPLETED.value,
        data={
            "job_id": job_id,
            "brand_id": str(brand.id),
            "brand_name": brand.name,
            "items": [{
                "item_id": "it-1",
                "vendor_code": "BELT100",
                "normalized_vendor_code": "BELT100",
                "barcode": "8700000000011",
                "quantity": 1,
                "colors": ["marrone"],
                "article_blueprint_id": str(bp.id),
            }],
            "blueprints": [{
                "id": str(bp.id),
                "is_new": False,
                "article_name": bp.article_name,
                "description": bp.description,
                "extended_description": bp.extended_description,
                "category": {"id": str(cat.id), "description": cat.name},
                "tags": ["cintura"],
                "materials": [],
            }],
            "photo_proposals": [],
            "heuristic_reconciliations": [{
                "reconciliation_id": rec_id,
                "kind": "COLOR_AGNOSTIC_SAFEGUARD",
                "status": "PENDING_USER_CONFIRMATION",
                "requires_confirmation": True,
                "heuristic_id": str(h.id),
                "merge_target_blueprint_id": str(bp.id),
                "staging_blueprint_ids": [],
                "existing_colors": ["nero"],
                "incoming_colors": ["marrone"],
                "matched_db_blueprints": [{"id": str(bp.id), "variants": [{"supplier_code": "BELT100", "colors": ["nero"]}]}],
                "incoming_items": [{"item_id": "it-1", "vendor_code": "BELT100", "colors": ["marrone"]}],
                "mode_update": HeuristicMode.COLOR_AGNOSTIC,
            }],
        },
    )

    # Confirmation blocked before user decides HITL
    with pytest.raises(HTTPException) as exc:
        asyncio.run(confirm_and_persist_staging(db_session, job_id, BackgroundTasks()))
    assert exc.value.status_code == 409

    # User confirms: two distinct color variants on the same code
    revised = revise_staging_data(
        db_session,
        job_id,
        [RevisionOperation(op="confirm_heuristic_reconciliation", reconciliation_id=rec_id)],
    )
    assert revised["heuristic_reconciliations"][0]["status"] == "ACCEPTED"

    asyncio.run(confirm_and_persist_staging(db_session, job_id, BackgroundTasks()))

    db_session.refresh(h)
    assert h.heuristic_mode == HeuristicMode.COLOR_AGNOSTIC
    assert h.heuristic_confirmed is True
    all_arts = db_session.query(Article).filter_by(article_blueprint_id=bp.id).all()
    assert {tuple(a.colors) for a in all_arts} == {("nero",), ("marrone",)}


def test_bug2_safeguard_reject_with_corrected_color_fixes_warehouse_retroactively(db_session):
    brand, cat, batch = _seed_world(db_session, "TommyRetroColorFix")
    h = heuristic_repo.create_heuristic(
        db_session,
        brand_id=str(brand.id),
        heuristic_mode=HeuristicMode.PENDING_COHORT,
        heuristic_confirmed=False,
    )
    bp1 = ArticleBlueprint(
        brand_id=brand.id,
        category_id=cat.id,
        heuristic_id=h.id,
        normalized_vendor_code="AM0AM14569###",
        skeleton_vendor_code="AM0AM14569",
        article_name="Reversible Belt",
        description="Cintura",
        extended_description="Cintura",
        tags=["cintura"],
    )
    bp2 = ArticleBlueprint(
        brand_id=brand.id,
        category_id=cat.id,
        heuristic_id=h.id,
        normalized_vendor_code="AM0AM12549###",
        skeleton_vendor_code="AM0AM12549",
        article_name="Cintura 2",
        description="Cintura",
        extended_description="Cintura",
        tags=["cintura"],
    )
    db_session.add_all([bp1, bp2])
    db_session.commit()

    art1 = Article(article_blueprint_id=bp1.id, batch_id=batch.id, supplier_code="AM0AM14569DW6", colors=["dw6"])
    art2 = Article(article_blueprint_id=bp2.id, batch_id=batch.id, supplier_code="AM0AM12549DW6", colors=["dw6"])
    ph1 = ArticlePhoto(article_blueprint_id=bp1.id, canonical_color_name="dw6", photo_data=b"fakeimg")
    db_session.add_all([art1, art2, ph1])
    db_session.commit()

    job_id = "job-sg-reject"
    rec_id = "rec-sg-reject"
    staging_repo.create_job(db_session, job_id=job_id, job_type=JobType.SINGLE_ITEM_IMPORT.value)
    staging_repo.update_job(
        db_session,
        job_id=job_id,
        status=JobStatus.COMPLETED.value,
        data={
            "job_id": job_id,
            "brand_id": str(brand.id),
            "brand_name": brand.name,
            "items": [{
                "item_id": "it-dw6",
                "vendor_code": "AM0AM14569DW6",
                "normalized_vendor_code": "AM0AM14569###",
                "barcode": "8721441290844",
                "quantity": 1,
                "colors": ["black/blu"],
                "article_blueprint_id": str(bp1.id),
            }],
            "blueprints": [{
                "id": str(bp1.id),
                "is_new": False,
                "article_name": bp1.article_name,
                "description": bp1.description,
                "extended_description": bp1.extended_description,
                "category": {"id": str(cat.id), "description": cat.name},
                "tags": ["cintura"],
                "materials": [],
            }],
            "photo_proposals": [{
                "item_id": "it-dw6",
                "photo_url": "https://example.com/belt.jpg",
                "photo_source": "web_search",
                "canonical_color_candidate": "black/blu",
            }],
            "heuristic_reconciliations": [{
                "reconciliation_id": rec_id,
                "kind": "COLOR_AGNOSTIC_SAFEGUARD",
                "status": "PENDING_USER_CONFIRMATION",
                "requires_confirmation": True,
                "heuristic_id": str(h.id),
                "merge_target_blueprint_id": str(bp1.id),
                "staging_blueprint_ids": [],
                "existing_colors": ["dw6"],
                "incoming_colors": ["black/blu"],
                "matched_db_blueprints": [{"id": str(bp1.id), "variants": [{"supplier_code": "AM0AM14569DW6", "colors": ["dw6"]}]}],
                "incoming_items": [{"item_id": "it-dw6", "vendor_code": "AM0AM14569DW6", "colors": ["black/blu"]}],
                "mode_update": HeuristicMode.COLOR_AGNOSTIC,
            }],
        },
    )

    # Empty corrected_color must fail with 400
    with pytest.raises(HTTPException) as exc_empty:
        revise_staging_data(
            db_session,
            job_id,
            [RevisionOperation(op="reject_heuristic_reconciliation", reconciliation_id=rec_id, fields={"corrected_color": "  "})],
        )
    assert exc_empty.value.status_code == 400

    # Reject choosing "black/blu" as the correct color
    revised = revise_staging_data(
        db_session,
        job_id,
        [RevisionOperation(op="reject_heuristic_reconciliation", reconciliation_id=rec_id, fields={"corrected_color": "black/blu"})],
    )
    assert revised["heuristic_reconciliations"][0]["status"] == "REJECTED"
    assert revised["heuristic_reconciliations"][0]["mode_update"] is None
    assert revised["items"][0]["colors"] == ["black/blu"]
    assert revised["photo_proposals"][0]["photo_source"] == "db_existing"
    assert str(ph1.id) in revised["photo_proposals"][0]["photo_url"]

    asyncio.run(confirm_and_persist_staging(db_session, job_id, BackgroundTasks()))

    # Heuristic was NOT promoted to COLOR_AGNOSTIC!
    db_session.refresh(h)
    assert h.heuristic_mode == HeuristicMode.PENDING_COHORT
    assert h.heuristic_confirmed is False

    # Retroactive color fix applied across ALL articles of the brand that had ["dw6"]
    db_session.refresh(art1)
    db_session.refresh(art2)
    db_session.refresh(ph1)
    assert art1.colors == ["black/blu"]
    assert art2.colors == ["black/blu"]
    assert ph1.canonical_color_name == "black/blu"

    # New inbound article also saved with ["black/blu"]
    bp1_articles = db_session.query(Article).filter_by(article_blueprint_id=bp1.id).all()
    assert len(bp1_articles) == 2
    assert all(a.colors == ["black/blu"] for a in bp1_articles)


# ===========================================================================
# Bug 4: Stage 3 Grounding Retry (3 attempts, increasing temp) + Fail-Safe
# ===========================================================================


@pytest.mark.asyncio
async def test_bug4_stage3_retries_with_increasing_temp_and_stops_when_grounded():
    mock_client = MagicMock()
    mock_client.get_logs.return_value = []
    mock_client.call_with_grounding = AsyncMock(
        side_effect=[
            ("<NAME>First Ungrounded</NAME><DESCRIPTION>Desc 1</DESCRIPTION>", []),
            ("<NAME>Official Web Belt</NAME><DESCRIPTION>Web Desc</DESCRIPTION>", ["https://tommy.com/belt"]),
        ]
    )

    bp = {
        "id": "bp-retry-1",
        "is_new": True,
        "cluster_items": [{"vendor_code": "AM0AM13576DW6", "article_name": "denton adj ext"}],
    }
    enriched_bp, warnings = await _enrich_blueprint_cluster(bp, "Tommy Hilfiger", mock_client)

    assert mock_client.call_with_grounding.call_count == 2
    temps = [c.kwargs["temperature"] for c in mock_client.call_with_grounding.call_args_list]
    assert temps == [0.0, 0.3]
    assert enriched_bp["cluster_items"][0]["article_name"] == "Official Web Belt"
    assert not enriched_bp.get("web_search_unverified")
    assert warnings == []


@pytest.mark.asyncio
async def test_bug4_stage3_accepts_grounding_detected_via_client_logs():
    mock_client = MagicMock()
    mock_client.get_logs.return_value = [{"query_usage": 1, "grounding_detected": True}]
    mock_client.call_with_grounding = AsyncMock(
        return_value=("<NAME>Logged Search Belt</NAME><DESCRIPTION>Desc</DESCRIPTION>", [])
    )

    bp = {
        "id": "bp-log-ok",
        "is_new": True,
        "cluster_items": [{"vendor_code": "AM0AM13576DW6", "article_name": "denton"}],
    }
    enriched_bp, warnings = await _enrich_blueprint_cluster(bp, "Tommy Hilfiger", mock_client)

    assert mock_client.call_with_grounding.call_count == 1
    assert enriched_bp["cluster_items"][0]["article_name"] == "Logged Search Belt"
    assert warnings == []


@pytest.mark.asyncio
async def test_bug4_stage3_failsafe_uses_first_attempt_and_emits_warning():
    mock_client = MagicMock()
    mock_client.get_logs.return_value = [{"query_usage": 0, "grounding_detected": False}]
    mock_client.call_with_grounding = AsyncMock(
        side_effect=[
            ("<NAME>First Attempt Belt</NAME><DESCRIPTION>First Desc</DESCRIPTION>", []),
            ("<NAME>Second Attempt Creative</NAME><DESCRIPTION>Second Desc</DESCRIPTION>", []),
            ("<NAME>Third Attempt Hallucination</NAME><DESCRIPTION>Third Desc</DESCRIPTION>", []),
        ]
    )

    bp = {
        "id": "bp-failsafe",
        "is_new": True,
        "cluster_items": [{"vendor_code": "AM0AM13576DW6", "article_name": "denton adj ext"}],
    }
    enriched_bp, warnings = await _enrich_blueprint_cluster(bp, "Tommy Hilfiger", mock_client)

    assert mock_client.call_with_grounding.call_count == 3
    temps = [c.kwargs["temperature"] for c in mock_client.call_with_grounding.call_args_list]
    assert temps == [0.0, 0.3, 0.7]
    assert enriched_bp["cluster_items"][0]["article_name"] == "First Attempt Belt"
    assert enriched_bp["cluster_items"][0]["article_description"] == "First Desc"
    assert enriched_bp.get("web_search_unverified") is True
    assert enriched_bp["cluster_items"][0].get("web_search_unverified") is True
    assert len(warnings) == 1
    assert "non sono stati confermati da una ricerca sul web" in warnings[0]


@pytest.mark.asyncio
async def test_bug4_stage3_warning_and_flag_survive_full_blueprints_pipeline(monkeypatch):
    with patch("src.agents.article_blueprints_agent.nodes.web_search_blueprints_node.LLMClient") as MockWSClient:
        ws_inst = MockWSClient.return_value
        ws_inst.get_logs.return_value = [{"query_usage": 0, "grounding_detected": False}]
        ws_inst.call_with_grounding = AsyncMock(
            side_effect=[
                ("<NAME>Belt First Attempt</NAME><DESCRIPTION>Desc 1</DESCRIPTION>", []),
                ("<NAME>Belt Second</NAME><DESCRIPTION>Desc 2</DESCRIPTION>", []),
                ("<NAME>Belt Third</NAME><DESCRIPTION>Desc 3</DESCRIPTION>", []),
            ]
        )

        async def mock_llm_call(*args, **kwargs):
            stage = kwargs.get("pipeline_stage", "")
            if "Synthesis" in stage:
                return '{"article_name": "Belt First Attempt", "description": "Desc 1", "dimensions": null}'
            return '{"category": "Cinture", "sub_category": null, "extended_description": "Ext", "tags": ["cintura"], "materials": ["pelle"]}'

        monkeypatch.setattr("src.agents.llm_client.LLMClient.call", mock_llm_call)

        agent = ArticleBlueprintsAgent()
        res = await agent.aexecute({
            "new_blueprints": [{
                "id": "bp-e2e-warn",
                "is_new": True,
                "cluster_items": [{"item_id": "it-w", "vendor_code": "AM0AM13576DW6", "article_name": "denton adj ext"}],
            }],
            "categories": {"Cinture": {"description": "Cinture", "sub_categories": {}}},
            "brand_name": "Tommy Hilfiger",
        })

    assert len(res["warnings"]) == 1
    assert "non sono stati confermati da una ricerca sul web" in res["warnings"][0]
    assert len(res["blueprints"]) == 1
    assert res["blueprints"][0].get("web_search_unverified") is True
    assert "non sono stati confermati" in (res["blueprints"][0].get("warning_message") or "")


def test_ui_staging_js_contains_safeguard_prompt_and_warning_rendering():
    staging_js = Path("src/static/js/staging.js").read_text(encoding="utf-8")
    workspace_css = Path("src/static/css/workspace.css").read_text(encoding="utf-8")
    assert "COLOR_AGNOSTIC_SAFEGUARD" in staging_js
    assert "window.prompt" in staging_js
    assert "corrected_color" in staging_js
    assert "No, è un errore di importazione sul colore" in staging_js
    assert "Sì, sono due varianti cromatiche distinte" in staging_js
    assert "_buildWarningsBanner" not in staging_js
    assert "unverified-web-icon" in staging_js
    assert "unverified-web-tooltip" in staging_js
    assert "revisione manuale" in staging_js
    assert "btn-hitl-neutral" in staging_js
    assert ".btn-hitl-neutral" in workspace_css
    assert ".unverified-web-icon" in workspace_css
    assert ".unverified-web-tooltip" in workspace_css

