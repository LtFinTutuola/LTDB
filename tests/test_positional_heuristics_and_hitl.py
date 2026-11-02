import asyncio
from datetime import date
import pytest
from fastapi import BackgroundTasks, HTTPException

from src.models.pim import Brand, Category, ArticleBlueprint, BrandHeuristic, HeuristicMode
from src.models.wms import Article, Batch, Supplier, MovementReason
from src.models.staging import JobStatus, JobType
from src.repositories import staging_repo
from src.repositories.pim_repo import pim_repo, heuristic_repo
from src.schemas.data_ingestion import RevisionOperation, SingleItemIngestionRequest
from src.services.data_ingestion_service import (
    _resolve_items_with_photos,
    process_and_stage_single_item,
    process_and_stage_pdf,
    confirm_and_persist_staging,
)
from src.services.staging_revision_service import revise_staging_data
from src.utils import vendor_code_normalizer as vcn
from src.agents.article_blueprints_agent.nodes.model_equivalence_oracle import (
    CanonicalProductEntity,
    OracleVerdictOutput,
    SAME_MODEL,
    DIFFERENT_MODEL,
    INSUFFICIENT_DATA,
    deterministic_veto,
    evaluate_pair,
)
from src.agents.article_blueprints_agent.nodes.oracle_and_induction_node import oracle_and_induction_node
from src.agents.article_blueprints_agent.state import BlueprintsGraphState


# ===========================================================================
# 1. Test Normalizer Deterministico Posizionale (Slicing & Guardrails)
# ===========================================================================

class TestVendorCodeNormalizer:
    def test_character_by_character_slicing_ignores_typographic_boundary(self):
        # 6 lettere + 5 numeri: i primi 8 caratteri identificano il modello, gli ultimi 3 il colore
        res = vcn.induce_positional_mask(["ABCDEF12345", "ABCDEF12678"])
        assert res["status"] == vcn.OK
        rule = res["rule"]
        assert rule["type"] == "FIXED"
        assert rule["masked_positions"] == [8, 9, 10]
        assert vcn.apply_rule("ABCDEF12345", rule) == "ABCDEF12###"
        assert vcn.apply_rule("ABCDEF12999", rule) == "ABCDEF12###"

    def test_strict_minimal_masking_and_monotonic_mask_widening(self):
        # Osservazione iniziale: varia solo l'ultimo carattere -> maschera 1 sola posizione
        res1 = vcn.induce_positional_mask(["ABCDEF12345", "ABCDEF12346"])
        assert res1["status"] == vcn.OK
        rule1 = res1["rule"]
        assert rule1["masked_positions"] == [10]
        assert vcn.apply_rule("ABCDEF12345", rule1) == "ABCDEF1234#"

        # Arriva terza variante ABCDEF12567 -> allargamento monotono a 3 posizioni [8, 9, 10]
        res2 = vcn.induce_positional_mask(["ABCDEF12345", "ABCDEF12567"], existing_rule=rule1)
        assert res2["status"] == vcn.OK
        rule2 = res2["rule"]
        assert rule2["masked_positions"] == [8, 9, 10]
        assert vcn.apply_rule("ABCDEF12567", rule2) == "ABCDEF12###"

    def test_elastic_dual_anchor_variable_length_codes(self):
        res = vcn.induce_positional_mask(["BAG100-BK", "BAG100-RED"])
        assert res["status"] == vcn.OK
        rule = res["rule"]
        assert rule["type"] == "ELASTIC"
        assert rule["prefix"] == "BAG100-"
        assert rule["suffix"] == ""
        assert vcn.apply_rule("BAG100-BK", rule) == "BAG100-#"
        assert vcn.apply_rule("BAG100-GREEN", rule) == "BAG100-#"

    def test_discontinuous_anomaly_rejected(self):
        # Variano sia l'indice 2 sia l'indice 6 e il suffisso -> non contiguo
        res = vcn.induce_positional_mask(["AB1-2023-RED", "AB2-2024-BLK"])
        assert res["status"] == vcn.DISCONTINUOUS_ANOMALY
        assert res["rule"] is None

    def test_negative_constraint_prevents_over_masking(self):
        # Vincolo Q3: LINE12-001 e LINE45-001 sono modelli diversi e NON devono collassare
        res = vcn.induce_positional_mask(
            ["LINE12-001", "LINE45-001"],
            negative_pairs=[["LINE12-001", "LINE45-001"]],
        )
        assert res["status"] == vcn.NEGATIVE_CONSTRAINT_VIOLATION
        assert res["rule"] is None

    def test_insufficient_anchor_rejected(self):
        res = vcn.induce_positional_mask(["A123", "B456"])
        assert res["status"] == vcn.INSUFFICIENT_ANCHOR


# ===========================================================================
# 2. Test Model Equivalence Oracle (Veti Deterministici & Isolamento Codice)
# ===========================================================================

class TestModelEquivalenceOracle:
    def test_stage_2_1_anti_genericity_veto(self):
        generic = CanonicalProductEntity(name="Borsa", description="Borsa")
        rich = CanonicalProductEntity(
            name="Borsa Shopping Meridian",
            description="Borsa shopping doppio manico con zip",
            width_cm=32, height_cm=25, depth_cm=14,
        )
        assert deterministic_veto(generic, rich) == INSUFFICIENT_DATA

    def test_stage_2_2_dimensional_tolerance_veto_and_null_permissive(self):
        small = CanonicalProductEntity(
            name="Trolley Cabina", description="Trolley rigido 4 ruote bagaglio a mano",
            width_cm=40, height_cm=55, depth_cm=20,
        )
        large = CanonicalProductEntity(
            name="Trolley Grande", description="Trolley rigido 4 ruote da stiva",
            width_cm=50, height_cm=75, depth_cm=30,
        )
        assert deterministic_veto(small, large) == DIFFERENT_MODEL

        # Differenza entro tolleranza (1 cm) -> nessun veto
        within = CanonicalProductEntity(
            name="Trolley Cabina", description="Trolley rigido 4 ruote bagaglio a mano",
            width_cm=40, height_cm=56, depth_cm=20,
        )
        assert deterministic_veto(small, within) is None

        # Dimensioni assenti su uno dei due -> null-permissive (nessun veto)
        no_dims = CanonicalProductEntity(
            name="Trolley Cabina", description="Trolley rigido 4 ruote bagaglio a mano",
        )
        assert deterministic_veto(small, no_dims) is None

    def test_stage_2_3_taxonomy_and_material_family_veto(self):
        leather_bag = CanonicalProductEntity(
            name="Borsa Secchiello", description="Borsa secchiello con coulisse",
            category="borse", materials=["pelle"],
        )
        nylon_bag = CanonicalProductEntity(
            name="Borsa Secchiello", description="Borsa secchiello con coulisse",
            category="borse", materials=["nylon"],
        )
        assert deterministic_veto(leather_bag, nylon_bag) == DIFFERENT_MODEL

    def test_stage_3_llm_judge_never_receives_vendor_codes(self):
        captured_prompts = []

        class DummyClient:
            async def call(self, **kwargs):
                captured_prompts.append(kwargs.get("prompt", ""))
                return OracleVerdictOutput(
                    verdict="SAME_MODEL",
                    color_relation="DIFFERENT_COLOR",
                    reasoning="Stessa borsa, colori diversi",
                ).model_dump_json()

        a = CanonicalProductEntity(
            name="Borsa Tracolla Meridian",
            description="Borsa a tracolla con catena e chiusura magnetica",
            category="borse", materials=["pelle"], colors=["nero"],
        )
        b = CanonicalProductEntity(
            name="Borsa Tracolla Meridian",
            description="Borsa a tracolla con catena e chiusura magnetica",
            category="borse", materials=["pelle"], colors=["rosso"],
        )
        verdict = asyncio.run(evaluate_pair(DummyClient(), a, b))
        assert verdict.verdict == SAME_MODEL
        assert len(captured_prompts) == 1
        assert "HWVG" not in captured_prompts[0]


# ===========================================================================
# 3. Test Fast-Path L0/L1, Lazy Induction N=1 e Rimozione Hard-Block DDT
# ===========================================================================

class TestFastPathAndLazyInduction:
    def _seed_brand_and_category(self, db_session, name="BrandTest"):
        brand = Brand(name=name)
        cat = Category(name="Borse", description="Borse")
        reason = db_session.query(MovementReason).filter_by(code="CARICO_DDT").first()
        if not reason:
            reason = MovementReason(code="CARICO_DDT", sign=1)
            db_session.add(reason)
        db_session.add_all([brand, cat])
        db_session.commit()
        brand.categories.append(cat)
        db_session.commit()
        return brand, cat

    def test_single_item_n1_lazy_induction_creates_pending_cohort_passthrough(self, db_session, monkeypatch):
        brand, cat = self._seed_brand_and_category(db_session, "BrandLazyN1")

        class _SessionCtx:
            def __enter__(self):
                return db_session
            def __exit__(self, *args):
                pass

        monkeypatch.setattr("src.services.data_ingestion_service.SessionLocal", lambda: _SessionCtx())

        async def _mock_aexecute(self, input_data):
            bp = input_data["new_blueprints"][0]
            it = bp["cluster_items"][0]
            return {
                "items": [{
                    "item_id": it["item_id"],
                    "vendor_code": it["vendor_code"],
                    "normalized_vendor_code": it["normalized_vendor_code"],
                    "barcode": it.get("barcode") or "",
                    "quantity": it["quantity"],
                    "colors": it["colors"],
                    "article_blueprint_id": bp["id"],
                }],
                "blueprints": [{
                    "id": bp["id"],
                    "is_new": True,
                    "article_name": "Borsa Aria",
                    "description": "Borsa a mano",
                    "extended_description": "Borsa a mano in pelle",
                    "category": "Borse",
                    "sub_category": None,
                    "tags": ["borsa"],
                    "materials": ["pelle"],
                    "dimensions": None,
                }],
                "photo_proposals": [],
                "warnings": [],
                "heuristic_reconciliations": [],
            }

        monkeypatch.setattr("src.services.data_ingestion_service.ArticleBlueprintsAgent.aexecute", _mock_aexecute)

        req = SingleItemIngestionRequest(
            brand_id=str(brand.id),
            vendor_code="ARIA001BLK",
            article_name="Borsa Aria",
            quantity=2,
            colors=["nero"],
        )
        job_id = "job-n1-lazy"
        staging_repo.create_job(db_session, job_id=job_id, job_type=JobType.SINGLE_ITEM_IMPORT.value)
        asyncio.run(process_and_stage_single_item(job_id, req))

        job = staging_repo.get_job(db_session, job_id)
        assert job.status == JobStatus.COMPLETED.value
        assert job.data["items"][0]["normalized_vendor_code"] == "ARIA001BLK"

        asyncio.run(confirm_and_persist_staging(db_session, job_id, BackgroundTasks()))

        h = heuristic_repo.get_active_heuristic(db_session, str(brand.id))
        assert h is not None
        assert h.heuristic_mode == HeuristicMode.PENDING_COHORT
        assert h.heuristic_confirmed is False
        bp = pim_repo.get_blueprint_by_normalized_code(db_session, str(brand.id), "ARIA001BLK")
        assert bp is not None

    def test_ddt_import_does_not_block_when_brand_has_no_heuristics(self, db_session, monkeypatch, tmp_path):
        brand, _ = self._seed_brand_and_category(db_session, "BrandNoHeuristicDDT")
        pdf = tmp_path / "sample.pdf"
        pdf.write_bytes(b"%PDF-1.4 dummy")

        class _SessionCtx:
            def __enter__(self):
                return db_session
            def __exit__(self, *args):
                pass

        monkeypatch.setattr("src.services.data_ingestion_service.SessionLocal", lambda: _SessionCtx())
        monkeypatch.setattr(
            "src.services.data_ingestion_service.DataExtractionAgent.aexecute",
            lambda self, inp: asyncio.sleep(0, result={
                "items": [{"item_id": "it-1", "vendor_code": "NEW001", "quantity": 1, "colors": ["blu"]}],
                "warnings": [],
            }),
        )
        monkeypatch.setattr(
            "src.services.data_ingestion_service.ArticleBlueprintsAgent.aexecute",
            lambda self, inp: asyncio.sleep(0, result={
                "items": [{
                    "item_id": "it-1", "vendor_code": "NEW001", "normalized_vendor_code": "NEW001",
                    "barcode": "", "quantity": 1, "colors": ["blu"],
                    "article_blueprint_id": inp["new_blueprints"][0]["id"],
                }],
                "blueprints": [{
                    "id": inp["new_blueprints"][0]["id"], "is_new": True,
                    "article_name": "Borsa Blu", "description": "Borsa",
                    "extended_description": "Borsa", "category": "Borse", "sub_category": None,
                    "tags": ["borsa"], "materials": ["pelle"], "dimensions": None,
                }],
                "photo_proposals": [], "warnings": [], "heuristic_reconciliations": [],
            }),
        )

        job_id = "job-ddt-no-heur"
        staging_repo.create_job(db_session, job_id=job_id, job_type=JobType.DDT_IMPORT.value, file_path=str(pdf))
        asyncio.run(process_and_stage_pdf(job_id, str(pdf), str(brand.id)))
        job = staging_repo.get_job(db_session, job_id)
        assert job.status == JobStatus.COMPLETED.value

    def test_fast_path_l0_and_pending_cohort_different_color_diversion(self, db_session):
        brand, cat = self._seed_brand_and_category(db_session, "BrandFastPathL0")
        h = heuristic_repo.create_heuristic(
            db_session, brand_id=str(brand.id), pattern="",
            heuristic_mode=HeuristicMode.PENDING_COHORT, heuristic_confirmed=False,
        )
        bp = ArticleBlueprint(
            brand_id=brand.id, category_id=cat.id, normalized_vendor_code="MOD777",
            article_name="Zaino Urban", description="Zaino", extended_description="Zaino", tags=["zaino"],
        )
        sup = Supplier(company_name="Sup")
        db_session.add_all([bp, sup])
        db_session.commit()
        batch = Batch(supplier_id=sup.id, delivery_note_number="B1", document_date=date.today())
        db_session.add(batch)
        db_session.commit()
        art = Article(article_blueprint_id=bp.id, batch_id=batch.id, supplier_code="MOD777", colors=["nero"])
        db_session.add(art)
        db_session.commit()

        # 1) Stesso supplier_code e stesso colore -> Fast-Path L0 istantaneo
        r_items, ur_items, _, _, _ = _resolve_items_with_photos(
            db_session, str(brand.id),
            [{"item_id": "1", "vendor_code": "MOD777", "colors": ["nero"], "quantity": 1}],
            [h],
        )
        assert len(r_items) == 1 and len(ur_items) == 0

        # 2) Stesso supplier_code ma colore DIVERSO mentre in PENDING_COHORT -> inviato a Cold-Path per verifica Q2/Q4
        r_items2, ur_items2, _, _, _ = _resolve_items_with_photos(
            db_session, str(brand.id),
            [{"item_id": "2", "vendor_code": "MOD777", "colors": ["rosso"], "quantity": 1}],
            [h],
        )
        assert len(r_items2) == 0 and len(ur_items2) == 1


# ===========================================================================
# 4. Test Flusso End-to-End Induzione Q1-Q4 e Guardrail HITL
# ===========================================================================

class TestHeuristicInductionAndHITL:
    def _setup_catalog(self, db_session, brand_name="GuessHITL"):
        brand = Brand(name=brand_name)
        cat = Category(name="Borse", description="Borse")
        reason = db_session.query(MovementReason).filter_by(code="CARICO_DDT").first()
        if not reason:
            reason = MovementReason(code="CARICO_DDT", sign=1)
            db_session.add(reason)
        sup = Supplier(company_name=f"Sup-{brand_name}")
        db_session.add_all([brand, cat, sup])
        db_session.commit()
        brand.categories.append(cat)
        batch = Batch(supplier_id=sup.id, delivery_note_number="DDT-1", document_date=date.today())
        db_session.add(batch)
        db_session.commit()
        return brand, cat, batch

    def test_q1_against_existing_db_blueprint_triggers_hitl_confirm_and_retroactive_realign(
        self, db_session, monkeypatch
    ):
        brand, cat, batch = self._setup_catalog(db_session, "BrandHITLConfirm")
        # Blueprint già presente in DB (inserito al passo N=1 in passthrough ABCDEF12345)
        bp_db = ArticleBlueprint(
            brand_id=brand.id,
            category_id=cat.id,
            normalized_vendor_code="ABCDEF12345",
            article_name="Borsa Shopping Meridian",
            description="Borsa shopping doppio manico con zip",
            extended_description="Borsa shopping doppio manico con zip in pelle",
            tags=["borsa", "meridian"],
            materials=["pelle"],
        )
        db_session.add(bp_db)
        db_session.commit()
        art_db = Article(
            article_blueprint_id=bp_db.id,
            batch_id=batch.id,
            supplier_code="ABCDEF12345",
            colors=["nero"],
        )
        db_session.add(art_db)
        heuristic_repo.create_heuristic(
            db_session, brand_id=str(brand.id), pattern="",
            heuristic_mode=HeuristicMode.PENDING_COHORT, heuristic_confirmed=False,
        )

        # Mock dell'Oracolo: certifica SAME_MODEL + DIFFERENT_COLOR
        async def _mock_eval(client, a, b):
            return OracleVerdictOutput(
                verdict=SAME_MODEL, color_relation="DIFFERENT_COLOR", reasoning="Stesso modello"
            )

        monkeypatch.setattr(
            "src.agents.article_blueprints_agent.nodes.oracle_and_induction_node.evaluate_pair",
            _mock_eval,
        )
        monkeypatch.setattr(
            "src.agents.article_blueprints_agent.nodes.oracle_and_induction_node.embed_entity",
            lambda e, c=None: asyncio.sleep(0, result=None),
        )

        cohort = pim_repo.get_brand_cohort(db_session, str(brand.id))
        state = BlueprintsGraphState(
            brand_id=str(brand.id),
            brand_name=brand.name,
            brand_cohort=cohort,
            brand_heuristic={"mode": HeuristicMode.PENDING_COHORT, "rules": [], "negative_constraints": []},
            new_blueprints=[{
                "id": "staged-bp-new",
                "is_new": True,
                "article_name": "Borsa Shopping Meridian",
                "description": "Borsa shopping doppio manico con zip",
                "extended_description": "Borsa shopping doppio manico con zip in pelle",
                "category": {"id": str(cat.id), "description": cat.name},
                "sub_category": None,
                "tags": ["borsa"],
                "materials": ["pelle"],
                "dimensions": None,
                "cluster_items": [{
                    "item_id": "item-red-1",
                    "vendor_code": "ABCDEF12678",
                    "normalized_vendor_code": "ABCDEF12678",
                    "barcode": "1234567890123",
                    "quantity": 3,
                    "colors": ["rosso"],
                }],
            }],
        )
        out = asyncio.run(oracle_and_induction_node(state))
        recs = out["heuristic_reconciliations"]
        assert len(recs) == 1
        rec = recs[0]
        assert rec["requires_confirmation"] is True
        assert rec["status"] == "PENDING_USER_CONFIRMATION"
        assert rec["proposed_heuristic_update"]["new_mask_preview"] == "ABCDEF12###"

        # Costruiamo il job di staging con la proposta in stato PENDING_USER_CONFIRMATION
        job_id = "job-hitl-confirm"
        staging_repo.create_job(db_session, job_id=job_id, job_type=JobType.SINGLE_ITEM_IMPORT.value)
        staging_repo.update_job(
            db_session,
            job_id=job_id,
            status=JobStatus.COMPLETED.value,
            data={
                "items": [{
                    "item_id": "item-red-1",
                    "vendor_code": "ABCDEF12678",
                    "normalized_vendor_code": "ABCDEF12678",
                    "barcode": "1234567890123",
                    "quantity": 3,
                    "colors": ["rosso"],
                    "article_blueprint_id": "staged-bp-new",
                }],
                "blueprints": [{
                    "id": "staged-bp-new",
                    "is_new": True,
                    "article_name": "Borsa Shopping Meridian",
                    "description": "Borsa shopping doppio manico con zip",
                    "extended_description": "Borsa shopping doppio manico con zip in pelle",
                    "category": {"id": str(cat.id), "description": cat.name},
                    "sub_category": None,
                    "tags": ["borsa"],
                    "materials": ["pelle"],
                    "dimensions": None,
                }],
                "photo_proposals": [],
                "heuristic_reconciliations": recs,
                "warnings": [],
                "job_id": job_id,
                "brand_id": str(brand.id),
            },
        )

        # Tentativo di conferma con HITL pendente -> bloccato con 409
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(confirm_and_persist_staging(db_session, job_id, BackgroundTasks()))
        assert exc_info.value.status_code == 409

        # L'utente conferma che sono varianti cromatiche tramite l'operazione di revisione
        updated_data = revise_staging_data(
            db_session,
            job_id,
            [RevisionOperation(op="confirm_heuristic_reconciliation", reconciliation_id=rec["reconciliation_id"])],
        )
        assert updated_data["heuristic_reconciliations"][0]["status"] == "ACCEPTED"
        assert updated_data["items"][0]["article_blueprint_id"] == str(bp_db.id)
        assert updated_data["items"][0]["normalized_vendor_code"] == "ABCDEF12###"

        # Ora la conferma procede, riallinea retroattivamente bp_db e aggiorna l'euristica
        asyncio.run(confirm_and_persist_staging(db_session, job_id, BackgroundTasks()))

        db_session.refresh(bp_db)
        assert bp_db.normalized_vendor_code == "ABCDEF12###"
        assert len(bp_db.articles) == 4
        assert {a.supplier_code for a in bp_db.articles} == {"ABCDEF12345", "ABCDEF12678"}
        active_h = heuristic_repo.get_active_heuristic(db_session, str(brand.id))
        assert active_h.heuristic_mode == HeuristicMode.COLOR_MASKED
        assert active_h.heuristic_confirmed is True
        assert active_h.heuristic_metadata["rules"][0]["masked_positions"] == [8, 9, 10]

    def test_q1_hitl_rejection_keeps_separate_blueprint_and_stores_negative_constraint(
        self, db_session, monkeypatch
    ):
        brand, cat, batch = self._setup_catalog(db_session, "BrandHITLReject")
        bp_db = ArticleBlueprint(
            brand_id=brand.id,
            category_id=cat.id,
            normalized_vendor_code="ABCDEF12345",
            article_name="Borsa Shopping Meridian",
            description="Borsa shopping doppio manico",
            extended_description="Borsa shopping doppio manico",
            tags=["borsa"],
            materials=["pelle"],
        )
        db_session.add(bp_db)
        db_session.commit()
        db_session.add(Article(
            article_blueprint_id=bp_db.id, batch_id=batch.id,
            supplier_code="ABCDEF12345", colors=["nero"],
        ))
        heuristic_repo.create_heuristic(
            db_session, brand_id=str(brand.id), pattern="",
            heuristic_mode=HeuristicMode.PENDING_COHORT, heuristic_confirmed=False,
        )

        rec_id = "rec-reject-1"
        job_id = "job-hitl-reject"
        staging_repo.create_job(db_session, job_id=job_id, job_type=JobType.SINGLE_ITEM_IMPORT.value)
        staging_repo.update_job(
            db_session,
            job_id=job_id,
            status=JobStatus.COMPLETED.value,
            data={
                "items": [{
                    "item_id": "item-other-1",
                    "vendor_code": "ABCDEF12678",
                    "normalized_vendor_code": "ABCDEF12678",
                    "barcode": "9999999999999",
                    "quantity": 1,
                    "colors": ["rosso"],
                    "article_blueprint_id": "staged-bp-separate",
                }],
                "blueprints": [{
                    "id": "staged-bp-separate",
                    "is_new": True,
                    "article_name": "Borsa Meridian Mini",
                    "description": "Borsa mini",
                    "extended_description": "Borsa mini in pelle",
                    "category": {"id": str(cat.id), "description": cat.name},
                    "sub_category": None,
                    "tags": ["borsa"],
                    "materials": ["pelle"],
                    "dimensions": None,
                }],
                "photo_proposals": [],
                "heuristic_reconciliations": [{
                    "reconciliation_id": rec_id,
                    "status": "PENDING_USER_CONFIRMATION",
                    "requires_confirmation": True,
                    "kind": "MASK_UPDATE",
                    "staging_blueprint_ids": ["staged-bp-separate"],
                    "incoming_items": [{"item_id": "item-other-1", "vendor_code": "ABCDEF12678", "colors": ["rosso"]}],
                    "matched_db_blueprints": [{
                        "id": str(bp_db.id),
                        "article_name": bp_db.article_name,
                        "normalized_vendor_code": "ABCDEF12345",
                        "new_normalized_vendor_code": "ABCDEF12###",
                        "variants": [{"supplier_code": "ABCDEF12345", "colors": ["nero"]}],
                    }],
                    "rule_update": {"type": "FIXED", "signature": "xxxxxxxxxxx", "length": 11, "masked_positions": [8, 9, 10]},
                    "merge_target_blueprint_id": str(bp_db.id),
                }],
                "warnings": [],
                "job_id": job_id,
                "brand_id": str(brand.id),
            },
        )

        # L'utente rifiuta (sono articoli diversi!)
        revise_staging_data(
            db_session,
            job_id,
            [RevisionOperation(op="reject_heuristic_reconciliation", reconciliation_id=rec_id)],
        )
        asyncio.run(confirm_and_persist_staging(db_session, job_id, BackgroundTasks()))

        # Il blueprint in DB resta intatto con codice non mascherato e viene creato un secondo blueprint separato
        db_session.refresh(bp_db)
        assert bp_db.normalized_vendor_code == "ABCDEF12345"
        bp_second = pim_repo.get_blueprint_by_normalized_code(db_session, str(brand.id), "ABCDEF12678")
        assert bp_second is not None and bp_second.id != bp_db.id

        # Il vincolo negativo Q3 è registrato nell'euristica del brand
        active_h = heuristic_repo.get_active_heuristic(db_session, str(brand.id))
        assert ["ABCDEF12345", "ABCDEF12678"] in (active_h.heuristic_metadata or {}).get("negative_constraints", [])

    def test_in_batch_induction_with_empty_db_cohort_does_not_require_hitl(self, monkeypatch):
        async def _mock_eval(client, a, b):
            return OracleVerdictOutput(
                verdict=SAME_MODEL, color_relation="DIFFERENT_COLOR", reasoning="Stesso modello nel DDT"
            )

        monkeypatch.setattr(
            "src.agents.article_blueprints_agent.nodes.oracle_and_induction_node.evaluate_pair",
            _mock_eval,
        )

        state = BlueprintsGraphState(
            brand_id="brand-empty-db",
            brand_name="BrandInBatch",
            brand_cohort=[],  # Nessun blueprint pre-esistente in DB
            brand_heuristic={"mode": HeuristicMode.PENDING_COHORT, "rules": [], "negative_constraints": []},
            new_blueprints=[
                {
                    "id": "cl-1", "is_new": True,
                    "article_name": "Portafoglio Zip", "description": "Portafoglio grande con zip",
                    "cluster_items": [{"item_id": "i1", "vendor_code": "WALLET01BLK", "normalized_vendor_code": "WALLET01BLK", "colors": ["nero"], "quantity": 1}],
                },
                {
                    "id": "cl-2", "is_new": True,
                    "article_name": "Portafoglio Zip", "description": "Portafoglio grande con zip",
                    "cluster_items": [{"item_id": "i2", "vendor_code": "WALLET01RED", "normalized_vendor_code": "WALLET01RED", "colors": ["rosso"], "quantity": 1}],
                },
            ],
        )
        out = asyncio.run(oracle_and_induction_node(state))
        # I due cluster del DDT vengono fusi in uno solo con codice WALLET01### senza richiedere HITL
        assert len(out["new_blueprints"]) == 1
        merged_cluster = out["new_blueprints"][0]
        assert len(merged_cluster["cluster_items"]) == 2
        assert all(it["normalized_vendor_code"] == "WALLET01###" for it in merged_cluster["cluster_items"])
        recs = out["heuristic_reconciliations"]
        assert len(recs) == 1
        assert recs[0]["requires_confirmation"] is False
        assert recs[0]["status"] == "NOT_REQUIRED"

    def test_q2_color_agnostic_and_q4_unreliable_sku(self, monkeypatch):
        cohort = [{
            "id": "bp-existing-1",
            "normalized_vendor_code": "AGNOSTIC01",
            "article_name": "Cintura Reversibile",
            "description": "Cintura in pelle con fibbia",
            "dimensions": None,
            "tags": ["cintura"],
            "materials": ["pelle"],
            "category_name": "accessori",
            "embedding": None,
            "variants": [{"supplier_code": "AGNOSTIC01", "colors": ["nero"]}],
        }]

        # Caso Q2: stesso codice fornitore, stesso modello, colore diverso -> COLOR_AGNOSTIC + attach
        monkeypatch.setattr(
            "src.agents.article_blueprints_agent.nodes.oracle_and_induction_node.evaluate_pair",
            lambda client, a, b: asyncio.sleep(0, result=OracleVerdictOutput(
                verdict=SAME_MODEL, color_relation="DIFFERENT_COLOR", reasoning="Stesso modello"
            )),
        )
        monkeypatch.setattr(
            "src.agents.article_blueprints_agent.nodes.oracle_and_induction_node.embed_entity",
            lambda e, c=None: asyncio.sleep(0, result=None),
        )
        state_q2 = BlueprintsGraphState(
            brand_id="b-1", brand_name="B", brand_cohort=cohort,
            brand_heuristic={"mode": HeuristicMode.PENDING_COHORT, "rules": [], "negative_constraints": []},
            new_blueprints=[{
                "id": "cl-q2", "is_new": True,
                "article_name": "Cintura Reversibile", "description": "Cintura in pelle con fibbia",
                "cluster_items": [{"item_id": "i-q2", "vendor_code": "AGNOSTIC01", "normalized_vendor_code": "AGNOSTIC01", "colors": ["marrone"], "quantity": 1}],
            }],
        )
        out_q2 = asyncio.run(oracle_and_induction_node(state_q2))
        assert len(out_q2["new_blueprints"]) == 0
        assert out_q2["heuristic_reconciliations"][0]["mode_update"] == HeuristicMode.COLOR_AGNOSTIC

        # Caso Q4: stesso codice fornitore ma modelli DIVERSI -> UNRELIABLE_SKU + disambiguazione codice
        monkeypatch.setattr(
            "src.agents.article_blueprints_agent.nodes.oracle_and_induction_node.evaluate_pair",
            lambda client, a, b: asyncio.sleep(0, result=OracleVerdictOutput(
                verdict=DIFFERENT_MODEL, color_relation="UNKNOWN", reasoning="Articoli diversi"
            )),
        )
        state_q4 = BlueprintsGraphState(
            brand_id="b-1", brand_name="B", brand_cohort=cohort,
            brand_heuristic={"mode": HeuristicMode.PENDING_COHORT, "rules": [], "negative_constraints": []},
            new_blueprints=[{
                "id": "cl-q4", "is_new": True,
                "article_name": "Zaino Tecnico", "description": "Zaino porta pc",
                "cluster_items": [{"item_id": "i-q4", "vendor_code": "AGNOSTIC01", "normalized_vendor_code": "AGNOSTIC01", "colors": ["blu"], "quantity": 1}],
            }],
        )
        out_q4 = asyncio.run(oracle_and_induction_node(state_q4))
        assert len(out_q4["new_blueprints"]) == 1
        assert out_q4["new_blueprints"][0]["cluster_items"][0]["normalized_vendor_code"].startswith("AGNOSTIC01~")
        assert out_q4["heuristic_reconciliations"][0]["mode_update"] == HeuristicMode.UNRELIABLE_SKU
