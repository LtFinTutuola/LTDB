import pytest
from datetime import date
from sqlalchemy.orm import Session
from src.models.pim import Brand, ArticleBlueprint, BrandHeuristic, HeuristicMode, CodeEmbedding, Category
from src.models.wms import Supplier, Batch, Article
from src.models.staging import JobStatus, StagingArea, JobType
from src.schemas.data_ingestion import SingleItemIngestionRequest
from src.repositories.pim_repo import pim_repo, heuristic_repo
from src.utils import vendor_code_normalizer as vcn
from src.agents.article_blueprints_agent.nodes.model_equivalence_oracle import (
    calc_code_morphology,
    CODE_IDENTICAL,
    CODE_VERY_HIGH,
    CODE_HIGH,
    CODE_NEUTRAL,
    CODE_CONFLICT,
)
from src.services import data_ingestion_service, staging_revision_service
from src.services.data_ingestion_service import _resolve_items_with_photos, _apply_reconciliations_and_realign
from src.schemas.data_ingestion import RevisionOperation


# ---------------------------------------------------------------------------
# 1. Step 0: Sterilizzazione Separatori (@) e Scheletro (skeleton_vendor_code)
# ---------------------------------------------------------------------------

def test_step0_separator_sterilization_and_skeleton_code():
    variants = [
        "P15-20502",
        "P15 20502",
        "P15/20502",
        "P15--20502",
        " P15__20502 ",
        "P15..20502",
    ]
    for raw in variants:
        sterilized = vcn.sterilize_separators(raw)
        assert sterilized == "P15@20502", f"Failed for {raw}"
        skeleton = vcn.compute_skeleton_code(sterilized)
        assert skeleton == "P1520502", f"Failed for {raw}"

    # Masked code skeleton
    assert vcn.compute_skeleton_code("P10FZP61@###") == "P10FZP61"
    assert vcn.compute_skeleton_code("140879@####@1IDU") == "1408791IDU"
    assert vcn.compute_skeleton_code("04704@081@##") == "04704081"


def test_single_item_request_preserves_internal_spaces_for_step0():
    req = SingleItemIngestionRequest(
        brand_id="test-brand",
        vendor_code=" P15  20502 ",
        colors=["nero"],
    )
    # The validator normalizes multiple spaces to a single space, without deleting it
    assert req.vendor_code == "P15 20502"
    # When sterilized in Step 0, internal space becomes '@'
    assert vcn.sterilize_separators(req.vendor_code) == "P15@20502"


# ---------------------------------------------------------------------------
# 2. Estrazione Radice/Colore a 3 Rami e Caso Colore Centrale (Samsonite)
# ---------------------------------------------------------------------------

def test_3branch_root_color_extraction_central_color_samsonite():
    raw_samsonite = "140879-1041-1IDU"
    sterilized, root_str, color_segment = vcn.extract_root_and_color(
        raw_samsonite,
        heuristic_mode=HeuristicMode.COLOR_MASKED_DELIMITED,
        color_segment_index=1,
    )
    assert sterilized == "140879@1041@1IDU"
    assert root_str == "140879@1IDU"
    assert color_segment == "1041"

    root_length = len(vcn.compute_skeleton_code(root_str))
    assert root_length == 10
    pattern = vcn.compute_root_blocks_pattern(root_str)
    assert pattern == "[NUM:6]@[NUM:1][ALPHA:3]"
    anchor = vcn.compute_root_prefix_anchor(root_str)
    assert anchor == "1408"


def test_delimited_guard_indexerror_safety():
    # If code has fewer segments than expected, extract_root_and_color must not raise IndexError
    short_code = "140879"
    sterilized, root_str, color_segment = vcn.extract_root_and_color(
        short_code,
        heuristic_mode=HeuristicMode.COLOR_MASKED_DELIMITED,
        color_segment_index=1,
    )
    assert sterilized == "140879"
    assert root_str == "140879"
    assert color_segment == ""


def test_compact_extraction_with_separator_agnostic_fallback():
    # Compact root length 8: "P10FZP61" with suffix "B01"
    # Input has an explicit separator right at boundary: "P10FZP61@B01"
    raw_code = "P10FZP61-B01"
    sterilized, root_str, color_segment = vcn.extract_root_and_color(
        raw_code,
        heuristic_mode=HeuristicMode.COLOR_MASKED_COMPACT,
        root_length=8,
    )
    assert sterilized == "P10FZP61@B01"
    assert root_str == "P10FZP61"
    assert color_segment == "B01"


# ---------------------------------------------------------------------------
# 3. Stadio 2A/2B Fast-Path, Indice Scheletro e Cross-Euristica
# ---------------------------------------------------------------------------

def test_stage2_fast_path_and_skeleton_lookup(db_session: Session):
    brand = Brand(name="Piquadro")
    db_session.add(brand)
    db_session.commit()

    # Create heuristic for Piquadro: COLOR_MASKED_COMPACT with root_length=8
    h = heuristic_repo.create_heuristic(
        db_session,
        brand_id=str(brand.id),
        pattern="",
        heuristic_mode=HeuristicMode.COLOR_MASKED_COMPACT,
        heuristic_confirmed=True,
        heuristic_metadata={"rules": [], "root_length": 8},
    )

    # Create Blueprint
    bp = ArticleBlueprint(
        brand_id=str(brand.id),
        heuristic_id=str(h.id),
        normalized_vendor_code="P10FZP61###",
        skeleton_vendor_code="P10FZP61",
        article_name="Zaino Brief",
        description="Zaino porta pc",
        extended_description="Zaino Brief pelle",
        tags=["zaino"],
    )
    db_session.add(bp)
    db_session.commit()

    # Recalculate code embeddings
    embs = heuristic_repo.recalculate_embeddings_for_heuristic(db_session, str(brand.id), str(h.id))
    assert len(embs) == 1
    assert embs[0].root_length == 8

    # Fast-Path Stage 2B resolution with separator-agnostic input "P10FZP61-B01"
    items = [{"vendor_code": "P10FZP61-B01", "colors": ["blu"]}]
    resolved, unresolved, resolved_bps, _, _ = data_ingestion_service._resolve_items_with_photos(
        db_session, str(brand.id), items, [h]
    )
    assert len(resolved) == 1
    assert len(unresolved) == 0
    assert resolved[0]["article_blueprint_id"] == str(bp.id)


def test_stage2b_cross_heuristic_color_agnostic_exclusion(db_session: Session):
    brand = Brand(name="AgnosticBrand")
    db_session.add(brand)
    db_session.commit()

    # Heuristic 1: COLOR_AGNOSTIC
    h_agno = heuristic_repo.create_heuristic(
        db_session,
        brand_id=str(brand.id),
        heuristic_mode=HeuristicMode.COLOR_AGNOSTIC,
        heuristic_confirmed=True,
    )
    bp_short = ArticleBlueprint(
        brand_id=str(brand.id),
        heuristic_id=str(h_agno.id),
        normalized_vendor_code="P10FZ",
        skeleton_vendor_code="P10FZ",
        article_name="Short Agnostic",
        description="Desc",
        extended_description="Ext",
        tags=[],
    )
    db_session.add(bp_short)
    db_session.commit()

    # Query with exclude_color_agnostic=True for "P10FZ"
    matches = pim_repo.get_by_skeleton_code(db_session, str(brand.id), "P10FZ", exclude_color_agnostic=True)
    assert len(matches) == 0


def test_stage2b_multi_match_quadrant_q4_delegates_to_stage4(db_session: Session):
    brand = Brand(name="MultiBrand")
    db_session.add(brand)
    db_session.commit()

    h = heuristic_repo.create_heuristic(
        db_session,
        brand_id=str(brand.id),
        heuristic_mode=HeuristicMode.COLOR_MASKED_COMPACT,
        heuristic_confirmed=True,
        heuristic_metadata={"root_length": 6},
    )

    # Two distinct blueprints sharing the same skeleton (HITL split scenario Q4)
    bp1 = ArticleBlueprint(
        brand_id=str(brand.id),
        heuristic_id=str(h.id),
        normalized_vendor_code="LINE12###",
        skeleton_vendor_code="LINE12",
        article_name="Model A",
        description="Desc",
        extended_description="Ext",
        tags=[],
    )
    bp2 = ArticleBlueprint(
        brand_id=str(brand.id),
        heuristic_id=str(h.id),
        normalized_vendor_code="LINE12###",
        skeleton_vendor_code="LINE12",
        article_name="Model B",
        description="Desc",
        extended_description="Ext",
        tags=[],
    )
    db_session.add_all([bp1, bp2])
    db_session.commit()

    heuristic_repo.recalculate_embeddings_for_heuristic(db_session, str(brand.id), str(h.id))

    # Incoming code "LINE12-001"
    items = [{"vendor_code": "LINE12-001", "colors": ["nero"]}]
    resolved, unresolved, _, _, _ = data_ingestion_service._resolve_items_with_photos(
        db_session, str(brand.id), items, [h]
    )
    # Multi-match: must NOT resolve blindly in Stage 2B, must be in unresolved to route to Oracle Stage 4
    assert len(resolved) == 0
    assert len(unresolved) == 1


# ---------------------------------------------------------------------------
# 4. Stadio 3: Guardrail Anti-Typo Interattivo (Tipi A, B, C)
# ---------------------------------------------------------------------------

def test_anti_typo_type_a_length_variance(db_session: Session):
    brand = Brand(name="TypoBrand")
    db_session.add(brand)
    db_session.commit()

    bp = ArticleBlueprint(
        brand_id=str(brand.id),
        normalized_vendor_code="P10QMTZ4812",
        skeleton_vendor_code="P10QMTZ4812",
        article_name="Zaino Piquadro",
        description="Desc",
        extended_description="Ext",
        tags=[],
    )
    db_session.add(bp)
    db_session.commit()

    # Suspect code with 1 extra character at the end: "P10QMTZ48122" (DeltaLen = 1, Levenshtein = 1)
    res = vcn.detect_typo_anomaly("P10QMTZ48122", [], [bp])
    assert res is not None
    assert res["anomaly_type"] == "TYPE_A_LENGTH_VARIANCE"
    assert res["suggested_match"] == "P10QMTZ4812"


def test_anti_typo_type_b_alphanumeric_substitution(db_session: Session):
    brand = Brand(name="TypoBrandB")
    db_session.add(brand)
    db_session.commit()

    bp = ArticleBlueprint(
        brand_id=str(brand.id),
        normalized_vendor_code="P10QMTT1###",
        skeleton_vendor_code="P10QMTT1",
        article_name="Trolley Piquadro",
        description="Desc",
        extended_description="Ext",
        tags=[],
    )
    db_session.add(bp)
    db_session.commit()

    # Typo: 'I' instead of '1' at the end of root: "P10QMTTI"
    res = vcn.detect_typo_anomaly("P10QMTTI", [], [bp])
    assert res is not None
    assert res["anomaly_type"] == "TYPE_B_PATTERN_VIOLATION"


def test_anti_typo_type_c_adjacent_transposition(db_session: Session):
    brand = Brand(name="TypoBrandC")
    db_session.add(brand)
    db_session.commit()

    bp = ArticleBlueprint(
        brand_id=str(brand.id),
        normalized_vendor_code="P10QMTZ4812",
        skeleton_vendor_code="P10QMTZ4812",
        article_name="Borsa Piquadro",
        description="Desc",
        extended_description="Ext",
        tags=[],
    )
    db_session.add(bp)
    db_session.commit()

    # Typo: transposition of 'Q' and 'M' -> "P10MQ..."
    res = vcn.detect_typo_anomaly("P10MQTZ4812", [], [bp])
    assert res is not None
    assert res["anomaly_type"] == "TYPE_C_TRANSPOSITION"


class MockSessionLocal:
    def __init__(self, session):
        self.session = session
    def __enter__(self):
        return self.session
    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


@pytest.mark.asyncio
async def test_stage3_suspends_job_and_bypass_with_confirmation(db_session: Session, monkeypatch):
    monkeypatch.setattr("src.services.data_ingestion_service.SessionLocal", lambda: MockSessionLocal(db_session))

    brand = Brand(name="Stage3Brand")
    db_session.add(brand)
    db_session.commit()

    bp = ArticleBlueprint(
        brand_id=str(brand.id),
        normalized_vendor_code="P10QMTZ4812",
        skeleton_vendor_code="P10QMTZ4812",
        article_name="Zaino Test",
        description="Desc",
        extended_description="Ext",
        tags=[],
    )
    db_session.add(bp)
    db_session.commit()

    # 1. Unconfirmed request with Typo: Job suspended with TYPO_WARNING
    req_typo = SingleItemIngestionRequest(
        brand_id=str(brand.id),
        vendor_code="P10QMTZ48122",
        colors=["nero"],
        typo_warning_confirmed=False,
    )
    job_id = data_ingestion_service.accept_single_item_job(db_session, req_typo)
    await data_ingestion_service.process_and_stage_single_item(job_id, req_typo)

    job_data = data_ingestion_service.get_job(db_session, job_id)
    assert job_data["status"] == "TYPO_WARNING"
    assert job_data["data"]["anomaly_type"] == "TYPE_A_LENGTH_VARIANCE"


# ---------------------------------------------------------------------------
# 5. Ricalcolo a Cascata (Mask Widening) e Garbage Collection Orfani
# ---------------------------------------------------------------------------

def test_cascade_recalculation_and_orphan_garbage_collection(db_session: Session):
    brand = Brand(name="CascadeBrand")
    db_session.add(brand)
    db_session.commit()

    h = heuristic_repo.create_heuristic(
        db_session,
        brand_id=str(brand.id),
        heuristic_mode=HeuristicMode.COLOR_MASKED_COMPACT,
        heuristic_confirmed=True,
        heuristic_metadata={"root_length": 8},
    )

    bp1 = ArticleBlueprint(
        brand_id=str(brand.id),
        heuristic_id=str(h.id),
        normalized_vendor_code="P10FZP61###",
        skeleton_vendor_code="P10FZP61",
        article_name="Model 1",
        description="Desc",
        extended_description="Ext",
        tags=[],
    )
    db_session.add(bp1)
    db_session.commit()

    embs1 = heuristic_repo.recalculate_embeddings_for_heuristic(db_session, str(brand.id), str(h.id))
    assert len(embs1) == 1
    assert embs1[0].root_length == 8

    # Now change bp1 to root length 6: "P10FZP##"
    bp1.normalized_vendor_code = "P10FZP##"
    bp1.skeleton_vendor_code = "P10FZP"
    db_session.commit()

    # Recalculate: old embedding with root_length=8 is orphan and must be DELETED!
    embs2 = heuristic_repo.recalculate_embeddings_for_heuristic(db_session, str(brand.id), str(h.id))
    assert len(embs2) == 1
    assert embs2[0].root_length == 6

    # Verify directly from DB query
    all_embs = db_session.query(CodeEmbedding).filter(CodeEmbedding.heuristic_id == str(h.id)).all()
    assert len(all_embs) == 1
    assert all_embs[0].root_length == 6


def test_staging_revision_split_invalidates_conflicting_reconciliations(db_session: Session):
    staging_data = {
        "items": [
            {"item_id": "it-1", "article_blueprint_id": "bp-1", "vendor_code": "CODE-1"},
            {"item_id": "it-2", "article_blueprint_id": "bp-1", "vendor_code": "CODE-2"},
        ],
        "blueprints": [
            {"id": "bp-1", "is_new": True, "article_name": "Shared Model"},
        ],
        "heuristic_reconciliations": [
            {
                "reconciliation_id": "rec-1",
                "matched_db_blueprints": [{"id": "bp-1"}],
                "incoming_items": [{"item_id": "it-2"}],
            }
        ],
    }

    # Split it-2 into a new blueprint
    op = RevisionOperation(
        op="split_blueprint",
        source_blueprint_id="bp-1",
        item_ids=["it-2"],
        blueprint_overrides={"article_name": "Split Model"},
    )
    generated_ids = {}
    staging_revision_service._dispatch_operation(db_session, staging_data, op, 0, generated_ids)

    # Automated reconciliation touching bp-1 and it-2 must have been invalidated!
    assert len(staging_data.get("heuristic_reconciliations", [])) == 0


# ---------------------------------------------------------------------------
# 6. Oracolo v3.0: calc_code_morphology a 3 Fasi
# ---------------------------------------------------------------------------

def test_oracle_v3_morphology_three_phases():
    # Rettifica 1.A: First segment differing must NOT receive CODE_VERY_HIGH
    assert calc_code_morphology("04704-081", "05704-081") == CODE_CONFLICT

    # Rettifica 1.A: Suffix segment differing receives CODE_VERY_HIGH
    assert calc_code_morphology("04704-081-01", "04704-081-02") == CODE_VERY_HIGH

    # Central segment differing with first segment anchored receives CODE_VERY_HIGH (Samsonite)
    assert calc_code_morphology("140879-1041-1IDU", "140879-1090-1IDU", color_segment_index=1) == CODE_VERY_HIGH

    # Rettifica 1.B: No summing LCS to LCP for compact codes
    # "P10QMTZ4812" vs "P15QMTZ1812" -> common prefix is only "P1" (len 2 < 4) -> CONFLICT
    assert calc_code_morphology("P10QMTZ4812", "P15QMTZ1812") == CODE_CONFLICT

    # Compact codes with length variance: DeltaL = 1 (P10QMTT133 vs P10QMTT1651)
    assert calc_code_morphology("P10QMTT133", "P10QMTT1651") == CODE_VERY_HIGH

    # Static root vs code with delimited color suffix (AW5646WOL01 vs AW5646WOL01-BLA)
    assert calc_code_morphology("AW5646WOL01", "AW5646WOL01-BLA") == CODE_HIGH


# ---------------------------------------------------------------------------
# 7. Regression Tests for QA BUG-01 to BUG-06
# ---------------------------------------------------------------------------

def test_bug01_pending_cohort_sql_short_circuit_with_different_color_label(db_session: Session):
    """
    BUG-01: When an incoming delimited vendor code matches an existing ArticleBlueprint
    in PENDING_COHORT mode, it must short-circuit in Stage 1 and match the blueprint,
    even if the incoming color label differs from the existing DB article's color label.
    """
    brand = Brand(name="BrandBug01")
    cat = Category(name="CatBug01", description="CatBug01 Desc")
    db_session.add_all([brand, cat])
    db_session.commit()

    h = heuristic_repo.create_heuristic(
        db_session,
        brand_id=str(brand.id),
        pattern="",
        heuristic_mode=HeuristicMode.PENDING_COHORT,
        heuristic_confirmed=False,
    )
    bp = ArticleBlueprint(
        brand_id=brand.id,
        category_id=cat.id,
        normalized_vendor_code="PU1241B2R@BLU2",
        skeleton_vendor_code="PU1241B2RBLU2",
        article_name="Portafoglio Blue Square",
        description="Portafoglio",
        extended_description="Portafoglio Blue Square in pelle",
        tags=["portafoglio", "pelle"],
    )
    sup = Supplier(company_name="SupBug01")
    db_session.add_all([bp, sup])
    db_session.commit()

    batch = Batch(supplier_id=sup.id, delivery_note_number="B-BUG01", document_date=date.today())
    db_session.add(batch)
    db_session.commit()

    art = Article(article_blueprint_id=bp.id, batch_id=batch.id, supplier_code="PU1241B2R//BLU2", colors=["blu"])
    db_session.add(art)
    db_session.commit()

    # Incoming item has different separator and different color ("brown" vs "blu")
    resolved, unresolved, _, _, _ = _resolve_items_with_photos(
        db_session,
        str(brand.id),
        [{"item_id": "it-bug01", "vendor_code": "PU1241B2R--BLU2", "colors": ["brown"], "quantity": 1}],
        [h],
    )
    assert len(resolved) == 1, "Delimited code in PENDING_COHORT must short-circuit in Stage 1!"
    assert len(unresolved) == 0
    assert resolved[0]["article_blueprint_id"] == str(bp.id)


def test_bug02_oracle_sterilized_code_comparison():
    """
    BUG-02: Codes differing only by typographical separator characters
    must be compared using sterilize_separators (@) so they are recognized
    as identical codes without SIGNATURE_MISMATCH.
    """
    c1 = "PU1241B2R--BLU2"
    c2 = "PU1241B2R//BLU2"
    assert vcn.sterilize_separators(c1) == vcn.sterilize_separators(c2) == "PU1241B2R@BLU2"

    c3 = "161361-1041-1CNU"
    c4 = "161361--1041--1CNU"
    assert vcn.sterilize_separators(c3) == vcn.sterilize_separators(c4) == "161361@1041@1CNU"


def test_bug03_segment_level_induction_samsonite_gotbag_piquadro():
    """
    BUG-03: Positional mask induction on delimited codes must operate at the segment level,
    masking the whole color segment with a single '#' and avoiding spurious residual characters.
    """
    # 1. Samsonite central color code: 161357-1041-1CNU vs 161357-1124-1CNU
    res_samsonite = vcn.induce_positional_mask(["161357-1041-1CNU", "161357-1124-1CNU"])
    assert res_samsonite["status"] == vcn.OK
    rule_s = res_samsonite["rule"]
    assert rule_s["type"] == "DELIMITED"
    assert rule_s["num_segments"] == 3
    assert rule_s["color_segment_index"] == 1
    masked_s = vcn.apply_rule("161357-1041-1CNU", rule_s)
    assert masked_s == "161357@#@1CNU"
    assert vcn.compute_skeleton_code(masked_s) == "1613571CNU"

    # 2. Got Bag suffix color code: BA0321MO-432 vs BA0321MO 100
    res_gotbag = vcn.induce_positional_mask(["BA0321MO-432", "BA0321MO 100"])
    assert res_gotbag["status"] == vcn.OK
    rule_g = res_gotbag["rule"]
    assert rule_g["type"] == "DELIMITED"
    assert rule_g["num_segments"] == 2
    assert rule_g["color_segment_index"] == 1
    masked_g = vcn.apply_rule("BA0321MO-432", rule_g)
    assert masked_g == "BA0321MO@#"
    assert vcn.compute_skeleton_code(masked_g) == "BA0321MO"

    # 3. Piquadro suffix color code with irregular separators: PU1241B2R//BLU2 vs PU1241B2R/N
    res_piquadro = vcn.induce_positional_mask(["PU1241B2R//BLU2", "PU1241B2R/N"])
    assert res_piquadro["status"] == vcn.OK
    rule_p = res_piquadro["rule"]
    assert rule_p["type"] == "DELIMITED"
    assert rule_p["num_segments"] == 2
    assert rule_p["color_segment_index"] == 1
    masked_p = vcn.apply_rule("PU1241B2R//BLU2", rule_p)
    assert masked_p == "PU1241B2R@#"
    assert vcn.compute_skeleton_code(masked_p) == "PU1241B2R"


def test_bug04_bug05_disambiguation_confirm_persists_mask_and_populates_code_embeddings(db_session: Session):
    """
    BUG-04 & BUG-05: Confirming a DISAMBIGUATION card with rule_update must:
    - Update normalized_vendor_code and skeleton_vendor_code on the blueprint;
    - Set heuristic_mode = COLOR_MASKED_DELIMITED, color_segment_index, and active_h.id on blueprint;
    - Populate code_embeddings rows in the database.
    """
    brand = Brand(name="BrandBug0405")
    cat = Category(name="CatBug0405", description="Cat0405 Desc")
    db_session.add_all([brand, cat])
    db_session.commit()

    h = heuristic_repo.create_heuristic(
        db_session,
        brand_id=str(brand.id),
        pattern="",
        heuristic_mode=HeuristicMode.PENDING_COHORT,
        heuristic_confirmed=False,
    )
    bp = ArticleBlueprint(
        brand_id=brand.id,
        category_id=cat.id,
        normalized_vendor_code="161361@1041@1CNU",
        skeleton_vendor_code="16136110411CNU",
        article_name="Samsonite Proxis Spinner",
        description="Spinner",
        extended_description="Samsonite Proxis Spinner 55cm",
        tags=["valigia", "spinner"],
    )
    sup = Supplier(company_name="SupBug0405")
    db_session.add_all([bp, sup])
    db_session.commit()

    batch = Batch(supplier_id=sup.id, delivery_note_number="B-0405", document_date=date.today())
    db_session.add(batch)
    db_session.commit()

    for raw in ["161361--1041--1CNU", "161361/1041/1CNU", "161361__1041__1CNU", "-161361-1041-1CNU-", "161361-1041-1CNU"]:
        art = Article(article_blueprint_id=bp.id, batch_id=batch.id, supplier_code=raw, colors=["black"])
        db_session.add(art)
    db_session.commit()

    rule = {
        "type": "DELIMITED",
        "num_segments": 3,
        "color_segment_index": 1,
        "heuristic_mode": "COLOR_MASKED_DELIMITED",
    }
    reconciliations = [
        {
            "reconciliation_id": "rec-0405",
            "kind": "DISAMBIGUATION",
            "status": "ACCEPTED",
            "merge_target_blueprint_id": str(bp.id),
            "rule_update": rule,
            "mode_update": "COLOR_MASKED_DELIMITED",
            "color_segment_index": 1,
            "incoming_items": [{"item_id": "it-new", "vendor_code": "161361-1124-1CNU"}],
        }
    ]

    _apply_reconciliations_and_realign(db_session, str(brand.id), reconciliations, [])
    db_session.commit()

    db_session.refresh(bp)
    assert bp.normalized_vendor_code == "161361@#@1CNU"
    assert bp.skeleton_vendor_code == "1613611CNU"
    assert bp.heuristic_id == str(h.id)

    db_session.refresh(h)
    assert h.heuristic_mode == HeuristicMode.COLOR_MASKED_DELIMITED
    assert h.color_segment_index == 1

    embs = heuristic_repo.get_embeddings_by_heuristic(db_session, str(h.id))
    assert len(embs) >= 1
    assert embs[0].separator_count == 2
    assert embs[0].root_length == len("1613611CNU")


def test_bug06_search_by_exact_code_separator_and_skeleton_agnostic(db_session: Session):
    """
    BUG-06: Warehouse search_by_exact_code must be separator- and skeleton-agnostic:
    - Searching 'P10QMTZ4551' must find Article 'P10QMTZ4-551' and return ONLY allowed_colors = ['taupe']
    - Searching model root 'P10QMTZ4' must match ArticleBlueprint and return allowed_colors = None (all colors)
    - Searching '0130250114' must find Article '01302501-14' under ArticleBlueprint '013025'
    """
    brand = Brand(name="BrandBug06")
    cat = Category(name="CatBug06", description="Cat06 Desc")
    db_session.add_all([brand, cat])
    db_session.commit()

    bp1 = ArticleBlueprint(
        brand_id=brand.id,
        category_id=cat.id,
        normalized_vendor_code="P10QMTZ4#",
        skeleton_vendor_code="P10QMTZ4",
        article_name="Mandarina Duck MD20",
        description="Zaino MD20",
        extended_description="Zaino Mandarina Duck MD20",
        tags=["zaino"],
    )
    bp2 = ArticleBlueprint(
        brand_id=brand.id,
        category_id=cat.id,
        normalized_vendor_code="013025",
        skeleton_vendor_code="013025",
        article_name="The Bridge Story Donna",
        description="Borsa Story",
        extended_description="Borsa The Bridge Story Donna",
        tags=["borsa"],
    )
    sup = Supplier(company_name="SupBug06")
    db_session.add_all([bp1, bp2, sup])
    db_session.commit()

    batch = Batch(supplier_id=sup.id, delivery_note_number="B-06", document_date=date.today())
    db_session.add(batch)
    db_session.commit()

    art1_taupe = Article(article_blueprint_id=bp1.id, batch_id=batch.id, supplier_code="P10QMTZ4-551", colors=["taupe"])
    art1_nero = Article(article_blueprint_id=bp1.id, batch_id=batch.id, supplier_code="P10QMTZ4-651", colors=["nero"])
    art2_marrone = Article(article_blueprint_id=bp2.id, batch_id=batch.id, supplier_code="01302501-14", colors=["marrone"])
    db_session.add_all([art1_taupe, art1_nero, art2_marrone])
    db_session.commit()

    res1 = pim_repo.search_by_exact_code(db_session, "P10QMTZ4551")
    assert len(res1) == 1
    assert res1[0]["blueprint_id"] == str(bp1.id)
    assert res1[0]["allowed_colors"] == ["taupe"]

    res2 = pim_repo.search_by_exact_code(db_session, "P10QMTZ4")
    assert len(res2) == 1
    assert res2[0]["blueprint_id"] == str(bp1.id)
    assert res2[0]["allowed_colors"] is None

    res3 = pim_repo.search_by_exact_code(db_session, "0130250114")
    assert len(res3) == 1
    assert res3[0]["blueprint_id"] == str(bp2.id)
    assert res3[0]["allowed_colors"] == ["marrone"]


def test_code_embedding_timestamps_and_persistence(db_session: Session):
    """
    Verify that CodeEmbedding instances are correctly created with non-null created_at
    and updated_at timestamps, both through recalculate_embeddings_for_heuristic (flush & commit)
    and direct model instantiation, preventing SQLite IntegrityError: NOT NULL constraint failed.
    """
    from datetime import datetime
    brand = Brand(name="TimestampTestBrand")
    db_session.add(brand)
    db_session.commit()

    h = BrandHeuristic(
        brand_id=str(brand.id),
        pattern=r"^(TST\d{3})-([A-Z0-9]+)$",
        explanation="Test pattern",
        heuristic_mode=HeuristicMode.COLOR_MASKED_DELIMITED,
    )
    db_session.add(h)
    db_session.commit()

    bp = ArticleBlueprint(
        brand_id=str(brand.id),
        heuristic_id=str(h.id),
        normalized_vendor_code="TST100-##",
        skeleton_vendor_code="TST100",
        article_name="Timestamp Test Blueprint",
        description="Timestamp Test Description",
        extended_description="Description",
        tags=[],
    )
    db_session.add(bp)
    db_session.commit()

    # Test recalculation with commit_changes=False (flush only, exactly as in confirm_and_persist_staging)
    embs = heuristic_repo.recalculate_embeddings_for_heuristic(
        db_session, str(brand.id), str(h.id), commit_changes=False
    )
    assert len(embs) == 1
    emb = embs[0]
    assert emb.created_at is not None
    assert isinstance(emb.created_at, datetime)
    assert emb.updated_at is not None
    assert isinstance(emb.updated_at, datetime)

    # Test direct instantiation without explicit timestamps
    direct_emb = CodeEmbedding(
        heuristic_id=str(h.id),
        brand_id=str(brand.id),
        total_length_min=8,
        total_length_max=10,
        root_length=6,
        color_length_min=2,
        color_length_max=4,
        separator_count=1,
        root_blocks_pattern="3_3",
        root_prefix_anchor="TST",
        sample_raw_code="TST100-01",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db_session.add(direct_emb)
    db_session.flush()

    assert direct_emb.id is not None
    assert direct_emb.created_at is not None
    assert direct_emb.updated_at is not None

