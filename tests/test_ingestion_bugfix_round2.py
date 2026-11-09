"""
Regressione per i bug del 2° ciclo di test manuali della pipeline di ingestion
(BUG-A, BUG-B, BUG-C, BUG-D, BUG-E, PA-01).
"""
from datetime import date, datetime
from types import SimpleNamespace

from sqlalchemy.orm import Session

from src.models.pim import Brand, ArticleBlueprint, BrandHeuristic, HeuristicMode, CodeEmbedding
from src.models.wms import Supplier, Batch, Article
from src.services import staging_revision_service
from src.services.data_ingestion_service import _resolve_items_with_photos
from src.utils import vendor_code_normalizer as vcn


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _brand(db: Session, name: str) -> Brand:
    b = Brand(name=name)
    db.add(b)
    db.commit()
    return b


def _bp(db: Session, brand: Brand, norm: str, skeleton: str, name: str = "Art", heuristic=None) -> ArticleBlueprint:
    bp = ArticleBlueprint(
        brand_id=str(brand.id),
        heuristic_id=str(heuristic.id) if heuristic else None,
        normalized_vendor_code=norm,
        skeleton_vendor_code=skeleton,
        article_name=name,
        description="d",
        extended_description="e",
        tags=[],
    )
    db.add(bp)
    db.commit()
    return bp


def _batch(db: Session) -> Batch:
    sup = Supplier(company_name="dummy")
    db.add(sup)
    db.commit()
    bat = Batch(supplier_id=sup.id, delivery_note_number="1", document_date=date.today())
    db.add(bat)
    db.commit()
    return bat


def _art(db: Session, bp: ArticleBlueprint, batch: Batch, code: str, color: str) -> Article:
    a = Article(article_blueprint_id=bp.id, batch_id=batch.id, supplier_code=code, colors=[color], status="AVAILABLE")
    db.add(a)
    db.commit()
    return a


def _emb(db: Session, brand: Brand, h: BrandHeuristic, root_len: int, c_min: int, c_max: int, anchor: str) -> CodeEmbedding:
    e = CodeEmbedding(
        heuristic_id=str(h.id),
        brand_id=str(brand.id),
        total_length_min=root_len + c_min,
        total_length_max=root_len + c_max,
        root_length=root_len,
        color_length_min=c_min,
        color_length_max=c_max,
        separator_count=0,
        root_blocks_pattern="[ALPHA:1][NUM:2][ALPHA:4][NUM:1]",
        root_prefix_anchor=anchor,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(e)
    db.commit()
    return e


def _heuristic(db: Session, brand: Brand, mode: str, pattern: str = "", meta=None, confirmed: bool = True) -> BrandHeuristic:
    h = BrandHeuristic(
        brand_id=str(brand.id),
        pattern=pattern,
        explanation="t",
        heuristic_mode=mode,
        heuristic_confirmed=confirmed,
        heuristic_metadata=meta or {"rules": []},
    )
    db.add(h)
    db.commit()
    return h


def _masked_md_world(db: Session):
    """Mandarina Duck: blueprint mascherato P10QMTZ4# con colore a 3 caratteri."""
    brand = _brand(db, "Mandarina Duck")
    h = _heuristic(db, brand, HeuristicMode.COLOR_MASKED_COMPACT, meta={"rules": [], "root_length": 8})
    bp = _bp(db, brand, "P10QMTZ4#", "P10QMTZ4", "MD20 Backpack", h)
    _art(db, bp, _batch(db), "P10QMTZ4-551", "black")
    emb = _emb(db, brand, h, 8, 3, 3, "P10")
    db.refresh(bp)
    return brand, h, bp, emb


# ---------------------------------------------------------------------------
# BUG-A: regex legacy di euristiche PENDING_COHORT
# ---------------------------------------------------------------------------

def test_legacy_pending_regex_not_applied_to_compact_code():
    h = SimpleNamespace(
        heuristic_mode=HeuristicMode.PENDING_COHORT,
        heuristic_metadata=None,
        pattern=r"^(?P<model_code>[A-Z]+[0-9]+[A-Z]+)(?P<color_code>[0-9]+)$",
        color_segment_index=None,
    )
    assert vcn.normalize_code("P10QMTZ4551", h) == "P10QMTZ4551"


def test_confirmed_regex_still_applies():
    h = SimpleNamespace(
        heuristic_mode=HeuristicMode.COLOR_MASKED,
        heuristic_metadata=None,
        pattern=r"^(?P<model>\d+)@(?P<color_code>\w+)$",
        color_segment_index=None,
    )
    assert vcn.normalize_code("123-RED", h) == "123@###"


# ---------------------------------------------------------------------------
# BUG-B: nessun falso split retroattivo per blueprint non coperti dalla regola
# ---------------------------------------------------------------------------

def test_retroactive_no_false_split_for_uncovered_blueprints(db_session: Session):
    brand = _brand(db_session, "Mandarina Duck")
    h = _heuristic(db_session, brand, HeuristicMode.PENDING_COHORT, confirmed=False)
    batch = _batch(db_session)

    target = _bp(db_session, brand, "P10QMTZ4#", "P10QMTZ4", "MD20", h)
    _art(db_session, target, batch, "P10QMTZ4-551", "black")

    # Altro modello multi-variante già mascherato: non coperto da prefisso P10QMTZ4
    other = _bp(db_session, brand, "P10QMT71#", "P10QMT71", "Altro", h)
    _art(db_session, other, batch, "P10QMT71-09K", "black")
    _art(db_session, other, batch, "P10QMT71-B01", "blue")

    staging = {"brand_id": str(brand.id), "heuristic_reconciliations": [], "items": [], "blueprints": []}
    rule = {"type": "ELASTIC", "prefix": "P10QMTZ4", "suffix": "", "heuristic_mode": "COLOR_MASKED_COMPACT", "root_length": 8}

    staging_revision_service._generate_retroactive_db_reconciliations(db_session, staging, rule, str(target.id))

    splits = [r for r in staging["heuristic_reconciliations"] if r.get("kind") == "RETROACTIVE_SPLIT"]
    assert splits == []


# ---------------------------------------------------------------------------
# BUG-C: gate sulla lunghezza del colore nel Fast Path
# ---------------------------------------------------------------------------

def test_color_length_out_of_range_not_resolved_in_fast_path(db_session: Session):
    brand, h, bp, _ = _masked_md_world(db_session)
    items = [{"vendor_code": "P10QMTZ48122", "colors": ["starrynight"]}]
    resolved, unresolved, *_ = _resolve_items_with_photos(db_session, str(brand.id), items, [h])
    assert resolved == []
    assert len(unresolved) == 1


def test_color_length_in_range_resolves_in_fast_path(db_session: Session):
    brand, h, bp, _ = _masked_md_world(db_session)
    items = [{"vendor_code": "P10QMTZ4209", "colors": ["havana"]}]
    resolved, unresolved, *_ = _resolve_items_with_photos(db_session, str(brand.id), items, [h])
    assert len(resolved) == 1 and unresolved == []
    assert resolved[0]["article_blueprint_id"] == str(bp.id)


def test_exact_article_match_bypasses_length_gate(db_session: Session):
    brand, h, bp, _ = _masked_md_world(db_session)
    _art(db_session, bp, _batch(db_session), "P10QMTZ48122", "starrynight")
    items = [{"vendor_code": "P10QMTZ48122", "colors": ["starrynight"]}]
    resolved, unresolved, *_ = _resolve_items_with_photos(db_session, str(brand.id), items, [h])
    assert len(resolved) == 1 and unresolved == []


# ---------------------------------------------------------------------------
# BUG-D / BUG-E: guardrail anti-typo su blueprint mascherati
# ---------------------------------------------------------------------------

def test_typo_type_a_on_masked_blueprint(db_session: Session):
    brand, h, bp, emb = _masked_md_world(db_session)
    res = vcn.detect_typo_anomaly("P10QMTZ48122", [emb], [bp])
    assert res and res["anomaly_type"] == "TYPE_A_LENGTH_VARIANCE"


def test_typo_type_b_on_root(db_session: Session):
    brand, h, bp, emb = _masked_md_world(db_session)
    res = vcn.detect_typo_anomaly("P10QMTZA812", [emb], [bp])
    assert res and res["anomaly_type"] == "TYPE_B_PATTERN_VIOLATION"


def test_typo_type_b_on_full_variant_supplier_code(db_session: Session):
    brand = _brand(db_session, "The Bridge")
    bp = _bp(db_session, brand, "013025", "013025", "Tacco")
    _art(db_session, bp, _batch(db_session), "01302501-14", "cuoio")
    db_session.refresh(bp)
    res = vcn.detect_typo_anomaly("O130250114", [], [bp])
    assert res and res["anomaly_type"] == "TYPE_B_PATTERN_VIOLATION"


def test_typo_type_c_on_root(db_session: Session):
    brand, h, bp, emb = _masked_md_world(db_session)
    res = vcn.detect_typo_anomaly("P10MQTZ4-803", [emb], [bp])
    assert res and res["anomaly_type"] == "TYPE_C_TRANSPOSITION"


def test_typo_type_c_on_piquadro_root(db_session: Session):
    brand = _brand(db_session, "Piquadro")
    h = _heuristic(db_session, brand, HeuristicMode.COLOR_MASKED_DELIMITED)
    bp = _bp(db_session, brand, "PU1241B2R@#", "PU1241B2R", "Portafoglio", h)
    _art(db_session, bp, _batch(db_session), "PU1241B2R/N", "nero")
    db_session.refresh(bp)
    res = vcn.detect_typo_anomaly("PU2141B2R/N", [], [bp])
    assert res and res["anomaly_type"] == "TYPE_C_TRANSPOSITION"


def test_typo_no_false_positive_on_known_code(db_session: Session):
    brand, h, bp, emb = _masked_md_world(db_session)
    assert vcn.detect_typo_anomaly("P10QMTZ4209", [emb], [bp]) is None
    assert vcn.detect_typo_anomaly("P10QMTZ4-551", [emb], [bp]) is None


# ---------------------------------------------------------------------------
# PA-01: induzione su codice troncato vs codice delimitato
# ---------------------------------------------------------------------------

def test_induce_mask_rejects_truncated_code_vs_delimited():
    res = vcn.induce_positional_mask(["161357", "161357-1041-1CNU"])
    assert res["status"] != vcn.OK
    assert res["rule"] is None


def test_induce_mask_keeps_separator_agnostic_pair():
    res = vcn.induce_positional_mask(["P10QMTZ4-551", "P10QMTZ4551"])
    assert res["status"] == vcn.OK
    assert res["rule"]["type"] == "ELASTIC"
    assert res["rule"]["prefix"] == "P10QMTZ4"

