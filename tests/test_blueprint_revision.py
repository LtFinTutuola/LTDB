"""
test_blueprint_revision.py
--------------------------
Comprehensive unit and integration tests for the Visual Blueprint Split/Merge
and Vendor Code Heuristic Revision System:
- BrandHeuristicRepository new methods (update_heuristic, find_matching_heuristic)
- Contrastive Sandbox validation (Must-Split & Must-Merge enforcement)
- Blueprint revision context retrieval (grouping physical articles by supplier_code + colors + ean)
- Rule deduction & natural-language correction loop (zero regex exposure to frontend)
- Warehouse-wide retroactive diffing & "Before/After" Staging plan preparation + inline editing
- 5-step ordered atomic DB commit (splits, merges, ArticlePhoto preservation, global normalized_vendor_code alignment, brand_heuristics update)
"""
import re
from unittest.mock import AsyncMock, patch
import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.models.pim import Brand, BrandHeuristic, BrandCategory, Category, ArticleBlueprint, ArticlePhoto
from src.models.wms import Supplier, Batch, Article, ArticleStatus
from src.repositories.pim_repo import BrandHeuristicRepository
from src.agents.codes_deduction_agent.nodes.validation_node import _check_contrastive_constraints
from src.services.blueprint_revision_service import (
    _build_grounded_code_breakdowns,
    _generate_deterministic_grounded_explanation,
    _generate_user_friendly_rule_explanation,
)


client = TestClient(app)


def _seed_brand_and_warehouse(db_session):
    """
    Create a Brand ('GuessTest'), Categories, Supplier, Batch, and a set of
    Blueprints + Articles simulating:
    1) bp_merged: Falsely merged blueprint containing two distinct models:
       - Large bag: HWVG870123BLA (nero, 2 pcs), HWVG870123RED (rosso, 1 pc)
       - Small wallet: HWVG870999BLA (nero, 1 pc)
    2) bp_extra_split: Another blueprint in warehouse with 2 distinct models merged:
       - HWVG870555BLA (nero) and HWVG870666BLA (nero)
    3) bp_dup_1 & bp_dup_2: Falsely separated duplicate blueprints of the same model:
       - bp_dup_1: HWVG870777BLA (nero) with old wrong normalized code 'HWVG870777BLA'
       - bp_dup_2: HWVG870777WHI (bianco) with old wrong normalized code 'HWVG870777WHI'
    4) bp_aligned: Already single-model blueprint whose normalized_vendor_code needs alignment:
       - HWVG870888GRN (verde) with old normalized code 'OLD_NORM_888'
    """
    brand = Brand(id="brand-guess-1", name="GuessTest")
    db_session.add(brand)

    heuristic = BrandHeuristic(
        id="heur-guess-1",
        brand_id=brand.id,
        pattern=r"^HWVG87(?P<color_code>[A-Z0-9]+)$",
        explanation="Vecchia regola errata che considerava tutto suffisso colore.",
    )
    db_session.add(heuristic)

    cat = Category(id="cat-borse", name="Borse", description="Borse da donna", parent_id=None)
    sub_cat = Category(id="subcat-tracolla", name="Tracolla", description="Borse a tracolla", parent_id=cat.id)
    db_session.add_all([cat, sub_cat])
    db_session.flush()

    db_session.add(BrandCategory(brand_id=brand.id, category_id=cat.id))

    from datetime import date
    supplier = Supplier(id="sup-1", company_name="Guess Italia SRL", vat_number="IT99999999999")
    db_session.add(supplier)
    db_session.flush()

    batch = Batch(id="batch-1", supplier_id=supplier.id, delivery_note_number="DDT-100", document_date=date(2026, 10, 26))
    db_session.add(batch)

    # 1. Target blueprint to split (bp_merged)
    bp_merged = ArticleBlueprint(
        id="bp-merged-1",
        brand_id=brand.id,
        category_id=sub_cat.id,
        normalized_vendor_code="HWVG87#",
        article_name="Borsa GuessTest",
        description="Borsa elegante con tracolla",
        extended_description="Borsa in ecopelle con logo frontale",
        materials=["ecopelle"],
        tags=["borsa", "tracolla"],
    )
    # 2. Additional warehouse blueprint that also has 2 models merged
    bp_extra_split = ArticleBlueprint(
        id="bp-extra-split-1",
        brand_id=brand.id,
        category_id=sub_cat.id,
        normalized_vendor_code="HWVG87_EXTRA#",
        article_name="Zaino GuessTest",
        description="Zaino casual",
        extended_description="Zaino giornaliero",
        materials=["ecopelle"],
        tags=["zaino"],
    )
    # 3. Duplicate blueprints to merge (bp_dup_1 & bp_dup_2)
    bp_dup_1 = ArticleBlueprint(
        id="bp-dup-1",
        brand_id=brand.id,
        category_id=sub_cat.id,
        normalized_vendor_code="HWVG870777BLA",
        article_name="Shopper GuessTest Nero",
        description="Shopper capiente",
        extended_description="Shopper in ecopelle",
        materials=["ecopelle"],
        tags=["shopper"],
    )
    bp_dup_2 = ArticleBlueprint(
        id="bp-dup-2",
        brand_id=brand.id,
        category_id=sub_cat.id,
        normalized_vendor_code="HWVG870777WHI",
        article_name="Shopper GuessTest Bianco",
        description="Shopper capiente",
        extended_description="Shopper in pelle",
        materials=["pelle"],
        tags=["elegante"],
    )
    # 4. Blueprint needing normalized_vendor_code alignment
    bp_aligned = ArticleBlueprint(
        id="bp-aligned-1",
        brand_id=brand.id,
        category_id=sub_cat.id,
        normalized_vendor_code="OLD_NORM_888",
        article_name="Bauletto GuessTest",
        description="Bauletto classico",
        extended_description="Bauletto con manici",
        materials=["ecopelle"],
        tags=["bauletto"],
    )
    db_session.add_all([bp_merged, bp_extra_split, bp_dup_1, bp_dup_2, bp_aligned])
    db_session.flush()

    # Photos on bp_merged and bp_dup_1 / bp_dup_2
    p_merged_nero = ArticlePhoto(
        id="photo-merged-nero",
        article_blueprint_id=bp_merged.id,
        canonical_color_name="nero",
        photo_data=b"fake_nero_bytes",
    )
    p_merged_rosso = ArticlePhoto(
        id="photo-merged-rosso",
        article_blueprint_id=bp_merged.id,
        canonical_color_name="rosso",
        photo_data=b"fake_rosso_bytes",
    )
    p_dup_nero = ArticlePhoto(
        id="photo-dup-nero",
        article_blueprint_id=bp_dup_1.id,
        canonical_color_name="nero",
        photo_data=b"fake_dup_nero",
    )
    p_dup_bianco = ArticlePhoto(
        id="photo-dup-bianco",
        article_blueprint_id=bp_dup_2.id,
        canonical_color_name="bianco",
        photo_data=b"fake_dup_bianco",
    )
    # Also add a conflicting 'nero' photo on bp_dup_2 to verify deduplication on merge
    p_dup_2_conflict_nero = ArticlePhoto(
        id="photo-dup-2-conflict-nero",
        article_blueprint_id=bp_dup_2.id,
        canonical_color_name="nero",
        photo_data=b"fake_dup_conflict",
    )
    db_session.add_all([p_merged_nero, p_merged_rosso, p_dup_nero, p_dup_bianco, p_dup_2_conflict_nero])

    # Physical Articles for bp_merged
    a1 = Article(
        id="art-123-bla-1",
        article_blueprint_id=bp_merged.id,
        batch_id=batch.id,
        supplier_code="HWVG870123BLA",
        ean="8000000001231",
        colors=["nero"],
        status=ArticleStatus.AVAILABLE,
    )
    a2 = Article(
        id="art-123-bla-2",
        article_blueprint_id=bp_merged.id,
        batch_id=batch.id,
        supplier_code="HWVG870123BLA",
        ean="8000000001231",
        colors=["nero"],
        status=ArticleStatus.AVAILABLE,
    )
    a3 = Article(
        id="art-123-red-1",
        article_blueprint_id=bp_merged.id,
        batch_id=batch.id,
        supplier_code="HWVG870123RED",
        ean="8000000001232",
        colors=["rosso"],
        status=ArticleStatus.AVAILABLE,
    )
    a4 = Article(
        id="art-999-bla-1",
        article_blueprint_id=bp_merged.id,
        batch_id=batch.id,
        supplier_code="HWVG870999BLA",
        ean="8000000009991",
        colors=["nero"],
        status=ArticleStatus.AVAILABLE,
    )

    # Physical Articles for bp_extra_split
    a_extra_1 = Article(
        id="art-555-bla",
        article_blueprint_id=bp_extra_split.id,
        batch_id=batch.id,
        supplier_code="HWVG870555BLA",
        colors=["nero"],
        status=ArticleStatus.AVAILABLE,
    )
    a_extra_2 = Article(
        id="art-666-bla",
        article_blueprint_id=bp_extra_split.id,
        batch_id=batch.id,
        supplier_code="HWVG870666BLA",
        colors=["nero"],
        status=ArticleStatus.AVAILABLE,
    )

    # Physical Articles for bp_dup_1 & bp_dup_2
    a_dup_1 = Article(
        id="art-777-bla",
        article_blueprint_id=bp_dup_1.id,
        batch_id=batch.id,
        supplier_code="HWVG870777BLA",
        colors=["nero"],
        status=ArticleStatus.AVAILABLE,
    )
    a_dup_2 = Article(
        id="art-777-whi",
        article_blueprint_id=bp_dup_2.id,
        batch_id=batch.id,
        supplier_code="HWVG870777WHI",
        colors=["bianco"],
        status=ArticleStatus.AVAILABLE,
    )

    # Physical Article for bp_aligned
    a_aligned = Article(
        id="art-888-grn",
        article_blueprint_id=bp_aligned.id,
        batch_id=batch.id,
        supplier_code="HWVG870888GRN",
        colors=["verde"],
        status=ArticleStatus.AVAILABLE,
    )

    db_session.add_all([a1, a2, a3, a4, a_extra_1, a_extra_2, a_dup_1, a_dup_2, a_aligned])
    db_session.commit()


class TestContrastiveSandboxValidation:
    """Unit tests for deterministic Must-Split and Must-Merge validation in validation_node."""

    def test_contrastive_validation_rejects_must_split_collision(self):
        # Over-greedy regex masks both model digits and color suffix into the same root
        norm_map = {
            "HWVG870123BLA": "HWVG87#",
            "HWVG870123RED": "HWVG87#",
            "HWVG870999BLA": "HWVG87#",
        }
        must_split = [["HWVG870123BLA", "HWVG870123RED"], ["HWVG870999BLA"]]
        must_merge = [["HWVG870123BLA", "HWVG870123RED"]]

        errors = _check_contrastive_constraints(norm_map, must_split, must_merge)
        assert len(errors) >= 1
        assert "Vincolo Must-Split violato" in errors[0]

    def test_contrastive_validation_rejects_must_merge_divergence(self):
        # Regex only masks the last 1 char instead of 3-char color code
        norm_map = {
            "HWVG870123BLA": "HWVG870123BL#",
            "HWVG870123RED": "HWVG870123RE#",
            "HWVG870999BLA": "HWVG870999BL#",
        }
        must_split = [["HWVG870123BLA", "HWVG870123RED"], ["HWVG870999BLA"]]
        must_merge = [["HWVG870123BLA", "HWVG870123RED"]]

        errors = _check_contrastive_constraints(norm_map, must_split, must_merge)
        assert any("Vincolo Must-Merge violato" in e for e in errors)

    def test_contrastive_validation_passes_valid_regex(self):
        norm_map = {
            "HWVG870123BLA": "HWVG870123#",
            "HWVG870123RED": "HWVG870123#",
            "HWVG870999BLA": "HWVG870999#",
        }
        must_split = [["HWVG870123BLA", "HWVG870123RED"], ["HWVG870999BLA"]]
        must_merge = [["HWVG870123BLA", "HWVG870123RED"]]

        errors = _check_contrastive_constraints(norm_map, must_split, must_merge)
        assert errors == []


class TestBrandHeuristicRepoExtensions:
    """Tests for update_heuristic and find_matching_heuristic on BrandHeuristicRepository."""

    def test_find_and_update_heuristic(self, db_session):
        _seed_brand_and_warehouse(db_session)
        repo = BrandHeuristicRepository()

        matched = repo.find_matching_heuristic(db_session, "brand-guess-1", ["HWVG870123BLA"])
        assert matched is not None
        assert matched.id == "heur-guess-1"

        updated = repo.update_heuristic(
            db_session,
            heuristic_id=matched.id,
            pattern=r"^[A-Z0-9]{10}(?P<color_code>[A-Z]{3})$",
            explanation="I primi 10 caratteri identificano il modello, gli ultimi 3 il colore.",
        )
        assert updated.pattern == r"^[A-Z0-9]{10}(?P<color_code>[A-Z]{3})$"
        assert "primi 10 caratteri" in updated.explanation


class TestBlueprintRevisionEndToEnd:
    """Integration tests for the full Blueprint Revision & Retroactive Reconciliation pipeline."""

    def test_get_blueprint_revision_context_groups_distinct_models_same_color(self, db_session):
        _seed_brand_and_warehouse(db_session)

        res = client.get("/api/v1/brands/blueprint-revision/bp-merged-1/context")
        assert res.status_code == 200
        data = res.json()

        assert data["blueprint_id"] == "bp-merged-1"
        assert data["brand_name"] == "GuessTest"
        # 3 distinct (supplier_code, colors, ean) groups:
        # HWVG870123BLA (qty 2, nero), HWVG870123RED (qty 1, rosso), HWVG870999BLA (qty 1, nero)
        groups = {g["supplier_code"]: g for g in data["article_groups"]}
        assert len(groups) == 3
        assert groups["HWVG870123BLA"]["quantity"] == 2
        assert groups["HWVG870123BLA"]["photo_id"] == "photo-merged-nero"
        assert groups["HWVG870999BLA"]["quantity"] == 1
        assert groups["HWVG870999BLA"]["colors"] == ["nero"]

    def test_full_deduce_correct_stage_edit_and_atomic_commit_flow(self, db_session):
        _seed_brand_and_warehouse(db_session)

        mock_deduction_first = {
            "regex": r"^[A-Z0-9]{10}(?P<color_code>[A-Z]{3})$",
            "explanation": "I primi 10 caratteri indicano il modello della borsa, mentre le ultime 3 lettere indicano il colore.",
            "examples": [
                {"raw": "HWVG870123BLA", "normalized": "HWVG870123#"},
                {"raw": "HWVG870999BLA", "normalized": "HWVG870999#"},
            ],
        }
        mock_deduction_corrected = {
            "regex": r"^HWVG87[0-9]{4}(?P<color_code>[A-Z]{3})$",
            "explanation": "Il prefisso HWVG87 seguito da 4 cifre identifica il modello; le ultime 3 lettere identificano la variante colore.",
            "examples": [
                {"raw": "HWVG870123BLA", "normalized": "HWVG870123#"},
                {"raw": "HWVG870999BLA", "normalized": "HWVG870999#"},
            ],
        }

        partitions_payload = [
            {
                "group_id": "group_original",
                "label": "Borsa GuessTest Grande",
                "is_original": True,
                "article_ids": ["art-123-bla-1", "art-123-bla-2", "art-123-red-1"],
                "supplier_codes": ["HWVG870123BLA", "HWVG870123RED"],
                "user_hint": "Borsa Grande",
            },
            {
                "group_id": "group_new_1",
                "label": "Portafoglio GuessTest Piccolo",
                "is_original": False,
                "article_ids": ["art-999-bla-1"],
                "supplier_codes": ["HWVG870999BLA"],
                "user_hint": "Portafoglio Piccolo",
            },
        ]

        with patch(
            "src.services.blueprint_revision_service.CodesDeductionAgent.aexecute",
            new_callable=AsyncMock,
            side_effect=[mock_deduction_first, mock_deduction_corrected],
        ) as mock_agent_run, patch(
            "src.services.blueprint_revision_service.LLMClient.call",
            new_callable=AsyncMock,
            side_effect=[
                "Nel codice HWVG870123BLA i primi 10 caratteri indicano la linea (HWVG870123) e gli ultimi 3 il colore (BLA).",
                "Nel codice HWVG870123BLA il prefisso HWVG87 seguito da 4 cifre identifica il modello e le ultime 3 lettere la variante colore.",
            ],
        ):
            # 1. Initial Rule Deduction from Visual Partitions
            res1 = client.post(
                "/api/v1/brands/blueprint-revision/deduce-rule",
                json={
                    "blueprint_id": "bp-merged-1",
                    "partitions": partitions_payload,
                },
            )
            assert res1.status_code == 200
            d1 = res1.json()
            job_id = d1["job_id"]
            assert "primi 10 caratteri" in d1["textual_explanation"]
            # Must never expose raw regex in response
            assert "regex" not in d1
            assert "?P<color_code>" not in str(d1)
            # Impact counts: 1 (target split) + 1 (bp_extra_split) = 2 splits; 1 merge (bp_dup_1 + bp_dup_2); 1 aligned (bp_aligned)
            assert d1["estimated_splits_count"] == 2
            assert d1["estimated_merges_count"] == 1
            assert d1["aligned_blueprints_count"] == 1

            # 2. User Correction Loop (recalculates regex and explanation)
            res2 = client.post(
                "/api/v1/brands/blueprint-revision/deduce-rule",
                json={
                    "blueprint_id": "bp-merged-1",
                    "job_id": job_id,
                    "partitions": partitions_payload,
                    "user_correction": "Specifica che il prefisso è HWVG87 più 4 cifre per il modello.",
                },
            )
            assert res2.status_code == 200
            d2 = res2.json()
            assert d2["job_id"] == job_id
            assert "prefisso HWVG87 seguito da 4 cifre" in d2["textual_explanation"]
            assert mock_agent_run.call_count == 2

        # 3. Prepare "Before & After" Staging Plan (with AI synthesis mocked)
        with patch(
            "src.services.blueprint_revision_service.ArticleBlueprintsAgent.aexecute",
            new_callable=AsyncMock,
            return_value={"blueprints": []},
        ), patch(
            "src.services.blueprint_revision_service.LLMClient.generate_embedding_sync",
            return_value=[0.1, 0.2, 0.3],
        ):
            res_stage = client.post(f"/api/v1/brands/blueprint-revision/{job_id}/prepare-staging")
            assert res_stage.status_code == 200
            stage_job = res_stage.json()

            # Verify regex is strictly stripped from sanitized staging job payload
            hs = stage_job["data"]["heuristic_state"]
            assert "new_regex" not in hs
            assert "old_regex" not in hs

            plan = stage_job["data"]["reconciliation_plan"]
            assert len(plan["target_split"]["after"]) == 2
            # All partitions in target_split["after"] are recreated from scratch as new blueprints
            assert all(abp["is_new"] is True for abp in plan["target_split"]["after"])
            assert all(abp["id"].startswith("new_bp_") for abp in plan["target_split"]["after"])
            # Staging Area is strictly scoped to the split blueprint (no auto-populated warehouse splits/merges)
            assert plan["warehouse_splits"] == []
            assert plan["warehouse_merges"] == []
            assert len(plan["aligned_blueprints"]) == 1

            # 4. Inline Edit in Staging Area before commit
            new_split_proposal_id = plan["target_split"]["after"][1]["id"]
            res_edit = client.put(
                f"/api/v1/brands/blueprint-revision/{job_id}/plan",
                json={
                    "target_blueprint_id": new_split_proposal_id,
                    "article_name": "Portafoglio GuessTest Mini Editato",
                    "tags": ["portafoglio", "mini"],
                },
            )
            assert res_edit.status_code == 200
            updated_after = res_edit.json()["data"]["reconciliation_plan"]["target_split"]["after"][1]
            assert updated_after["article_name"] == "Portafoglio GuessTest Mini Editato"
            assert updated_after["tags"] == ["portafoglio", "mini"]

            # 5. Execute Atomic Commit
            res_commit = client.post(f"/api/v1/brands/blueprint-revision/{job_id}/commit")
            assert res_commit.status_code == 200
            commit_data = res_commit.json()
            assert commit_data["status"] == "success"
            assert commit_data["splits_created"] == 2
            assert commit_data["merges_executed"] == 0
            # Remaining warehouse anomalies (bp-extra-split-1 and bp-dup-1/bp-dup-2) are signaled for manual review
            assert commit_data["remaining_anomalies"] == 2

        # Verify Database State after Atomic Commit:
        # A) Old fused bp-merged-1 was replaced and deleted; a brand-new blueprint exists for HWVG870123#
        bp_old = db_session.query(ArticleBlueprint).filter(ArticleBlueprint.id == "bp-merged-1").first()
        assert bp_old is None

        bp_first_new = (
            db_session.query(ArticleBlueprint)
            .filter(
                ArticleBlueprint.brand_id == "brand-guess-1",
                ArticleBlueprint.normalized_vendor_code == "HWVG870123#",
            )
            .first()
        )
        assert bp_first_new is not None
        assert bp_first_new.id != "bp-merged-1"
        orig_arts = db_session.query(Article).filter(Article.article_blueprint_id == bp_first_new.id).all()
        assert len(orig_arts) == 3
        first_photos = db_session.query(ArticlePhoto).filter(ArticlePhoto.article_blueprint_id == bp_first_new.id).all()
        assert {p.canonical_color_name for p in first_photos} == {"nero", "rosso"}

        # B) Second newly split blueprint exists with normalized code HWVG870999#, edited name, reassigned article, and cloned nero photo
        bp_second_new = (
            db_session.query(ArticleBlueprint)
            .filter(
                ArticleBlueprint.brand_id == "brand-guess-1",
                ArticleBlueprint.normalized_vendor_code == "HWVG870999#",
            )
            .first()
        )
        assert bp_second_new is not None
        assert bp_second_new.article_name == "Portafoglio GuessTest Mini Editato"
        new_arts = db_session.query(Article).filter(Article.article_blueprint_id == bp_second_new.id).all()
        assert len(new_arts) == 1
        assert new_arts[0].id == "art-999-bla-1"
        new_photos = db_session.query(ArticlePhoto).filter(ArticlePhoto.article_blueprint_id == bp_second_new.id).all()
        assert len(new_photos) == 1
        assert new_photos[0].canonical_color_name == "nero"

        # C) Untouched anomalous blueprints in warehouse (bp-extra-split-1 and bp-dup-1/bp-dup-2) remain intact until manual review
        assert db_session.query(ArticleBlueprint).filter(ArticleBlueprint.id == "bp-extra-split-1").first() is not None
        assert db_session.query(ArticleBlueprint).filter(ArticleBlueprint.id == "bp-dup-1").first() is not None
        assert db_session.query(ArticleBlueprint).filter(ArticleBlueprint.id == "bp-dup-2").first() is not None

        # D) Aligned blueprint (bp-aligned-1) updated its normalized_vendor_code to HWVG870888#
        bp_al = db_session.query(ArticleBlueprint).filter(ArticleBlueprint.id == "bp-aligned-1").first()
        assert bp_al is not None
        assert bp_al.normalized_vendor_code == "HWVG870888#"

        # E) BrandHeuristic record updated in brand_heuristics
        heur = db_session.query(BrandHeuristic).filter(BrandHeuristic.id == "heur-guess-1").first()
        assert heur is not None
        assert heur.pattern == r"^HWVG87[0-9]{4}(?P<color_code>[A-Z]{3})$"
        assert "prefisso HWVG87" in heur.explanation


class TestGroundedRuleExplanation:
    """Unit tests for grounded, user-friendly rule explanation generation."""

    def test_build_grounded_code_breakdowns_positions(self):
        compiled_regex = re.compile(r"^HWVG87[0-9]{4}(?P<color_code>[A-Z]{3})$")
        partitions = [
            {
                "group_id": "group_orig",
                "label": "Borsa Grande",
                "is_original": True,
                "supplier_codes": ["HWVG870123BLA"],
            },
            {
                "group_id": "group_new",
                "label": "Portafoglio Piccolo",
                "is_original": False,
                "supplier_codes": ["HWVG870999RED"],
            },
        ]
        breakdowns = _build_grounded_code_breakdowns(compiled_regex, partitions, [])
        assert len(breakdowns) == 2
        b1 = breakdowns[0]
        assert b1["code"] == "HWVG870123BLA"
        assert b1["model_root"] == "HWVG870123"
        assert b1["color_variant"] == "BLA"
        assert "ultimi 3 caratteri" in b1["position_info"]
        assert b1["partition_label"] == "Borsa Grande"

        b2 = breakdowns[1]
        assert b2["code"] == "HWVG870999RED"
        assert b2["model_root"] == "HWVG870999"
        assert b2["color_variant"] == "RED"

    def test_deterministic_grounded_explanation_content(self):
        breakdowns = [
            {
                "code": "HWVG870123BLA",
                "model_root": "HWVG870123",
                "color_variant": "BLA",
                "position_info": "gli ultimi 3 caratteri",
                "partition_label": "Borsa Grande",
                "is_original": True,
                "colors": ["nero"],
            },
            {
                "code": "HWVG870999BLA",
                "model_root": "HWVG870999",
                "color_variant": "BLA",
                "position_info": "gli ultimi 3 caratteri",
                "partition_label": "Portafoglio Piccolo",
                "is_original": False,
                "colors": ["nero"],
            },
        ]
        text = _generate_deterministic_grounded_explanation("Guess", breakdowns)
        assert "HWVG870123BLA" in text
        assert "HWVG870999BLA" in text
        assert "gli ultimi 3 caratteri" in text
        assert "regex" not in text.lower()
        assert "?p<color_code>" not in text.lower()

    @pytest.mark.asyncio
    async def test_user_friendly_rule_explanation_llm_invocation(self):
        breakdowns = [
            {
                "code": "HWVG870123BLA",
                "model_root": "HWVG870123",
                "color_variant": "BLA",
                "position_info": "gli ultimi 3 caratteri",
                "partition_label": "Borsa Grande",
                "is_original": True,
                "colors": ["nero"],
            }
        ]
        with patch("src.services.blueprint_revision_service.LLMClient.call", new_callable=AsyncMock) as mock_llm:
            mock_llm.return_value = "Nel codice HWVG870123BLA i primi 10 caratteri indicano la linea e gli ultimi 3 il colore nero."
            result = await _generate_user_friendly_rule_explanation(
                brand_name="Guess",
                code_breakdowns=breakdowns,
                base_explanation="",
                user_correction="I primi caratteri sono il modello",
            )
            assert result == "Nel codice HWVG870123BLA i primi 10 caratteri indicano la linea e gli ultimi 3 il colore nero."
            mock_llm.assert_called_once()
            call_kwargs = mock_llm.call_args[1]
            assert "commesso/operatore" in call_kwargs["system_prompt"]
            assert "NIENTE GERGO TECNICO" in call_kwargs["system_prompt"]
            assert "HWVG870123BLA" in call_kwargs["prompt"]
            assert "I primi caratteri sono il modello" in call_kwargs["prompt"]

    @pytest.mark.asyncio
    async def test_user_friendly_rule_explanation_fallback_on_error(self):
        breakdowns = [
            {
                "code": "HWVG870123BLA",
                "model_root": "HWVG870123",
                "color_variant": "BLA",
                "position_info": "gli ultimi 3 caratteri",
                "partition_label": "Borsa Grande",
                "is_original": True,
                "colors": ["nero"],
            }
        ]
        with patch("src.services.blueprint_revision_service.LLMClient.call", new_callable=AsyncMock, side_effect=RuntimeError("LLM API error")):
            result = await _generate_user_friendly_rule_explanation(
                brand_name="Guess",
                code_breakdowns=breakdowns,
                base_explanation="",
            )
            assert "HWVG870123BLA" in result
            assert "HWVG870123" in result


class TestBlueprintRevisionPhotoStagingAndPersistence:
    """Tests for photo upload, photo retry and photo persistence in the Blueprint Revision Staging workflow."""

    def test_photo_upload_and_persistence_on_commit(self, db_session):
        _seed_brand_and_warehouse(db_session)

        mock_deduction = {
            "regex": r"^[A-Z0-9]{10}(?P<color_code>[A-Z]{3})$",
            "explanation": "I primi 10 caratteri indicano il modello, gli ultimi 3 il colore.",
            "examples": [{"raw": "HWVG870123BLA", "normalized": "HWVG870123#"}],
        }
        partitions_payload = [
            {
                "group_id": "group_original",
                "label": "Borsa Grande",
                "is_original": True,
                "article_ids": ["art-123-bla-1", "art-123-bla-2", "art-123-red-1"],
                "supplier_codes": ["HWVG870123BLA", "HWVG870123RED"],
            },
            {
                "group_id": "group_new_1",
                "label": "Portafoglio Piccolo",
                "is_original": False,
                "article_ids": ["art-999-bla-1"],
                "supplier_codes": ["HWVG870999BLA"],
            },
        ]

        with patch("src.services.blueprint_revision_service.CodesDeductionAgent.aexecute", new_callable=AsyncMock, return_value=mock_deduction):
            res_deduce = client.post(
                "/api/v1/brands/blueprint-revision/deduce-rule",
                json={"blueprint_id": "bp-merged-1", "partitions": partitions_payload},
            )
            assert res_deduce.status_code == 200
            job_id = res_deduce.json()["job_id"]

        with patch("src.services.blueprint_revision_service.ArticleBlueprintsAgent.aexecute", new_callable=AsyncMock, return_value={"blueprints": []}), \
             patch("src.services.blueprint_revision_service.LLMClient.generate_embedding_sync", return_value=[0.1, 0.2, 0.3]):
            res_prep = client.post(f"/api/v1/brands/blueprint-revision/{job_id}/prepare-staging")
            assert res_prep.status_code == 200
            plan = res_prep.json()["data"]["reconciliation_plan"]

        new_split_bp_id = plan["target_split"]["after"][1]["id"]
        dummy_image_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xff\xdb"

        # 1. Test POST /photo-upload on new split blueprint
        res_upload = client.post(
            f"/api/v1/brands/blueprint-revision/{job_id}/photo-upload?target_blueprint_id={new_split_bp_id}&color=nero",
            content=dummy_image_bytes,
            headers={"Content-Type": "image/jpeg"},
        )
        assert res_upload.status_code == 200
        upload_data = res_upload.json()
        assert upload_data["status"] == "success"
        assert upload_data["target_blueprint_id"] == new_split_bp_id
        assert upload_data["photo_url"].startswith("data:image/jpeg;base64,")

        # Verify job data was updated in staging
        res_job = client.get(f"/api/v1/brands/blueprint-revision/{job_id}")
        assert res_job.status_code == 200
        card_after = res_job.json()["data"]["reconciliation_plan"]["target_split"]["after"][1]
        assert "staged_photos_by_color" in card_after
        assert card_after["staged_photos_by_color"]["nero"].startswith("data:image/jpeg;base64,")

        # 2. Test POST /photo-retry on original blueprint card with manual new_url
        with patch("httpx.AsyncClient.stream") as mock_stream:
            mock_res = AsyncMock()
            mock_res.status_code = 200
            mock_res.headers = {"content-type": "image/png"}
            mock_stream.return_value.__aenter__.return_value = mock_res

            orig_bp_id = plan["target_split"]["after"][0]["id"]
            res_retry = client.post(
                f"/api/v1/brands/blueprint-revision/{job_id}/photo-retry",
                json={
                    "target_blueprint_id": orig_bp_id,
                    "color": "rosso",
                    "new_url": "https://example.com/test_bag_red.png",
                },
            )
            assert res_retry.status_code == 200
            retry_data = res_retry.json()
            assert retry_data["status"] == "success"
            assert retry_data["photo_url"] == "https://example.com/test_bag_red.png"

        # 3. Commit plan and verify photo persistence
        with patch("src.services.blueprint_revision_service.photo_service.download_and_save_photo") as mock_dl_save, \
             patch("src.services.blueprint_revision_service.LLMClient.generate_embedding_sync", return_value=[0.1, 0.2, 0.3]):
            res_commit = client.post(f"/api/v1/brands/blueprint-revision/{job_id}/commit")
            assert res_commit.status_code == 200

            called_urls = [call[1]["photo_url"] for call in mock_dl_save.call_args_list]
            assert any(u.startswith("data:image/jpeg;base64,") for u in called_urls)
            assert "https://example.com/test_bag_red.png" in called_urls


class TestManualAnomalyCorrectionAndFromScratchRecreation:
    """Tests for warehouse anomalies detection and manual one-by-one blueprint correction without regex recalculation."""

    def test_detect_anomalies_and_fix_manually_one_by_one(self, db_session):
        _seed_brand_and_warehouse(db_session)

        # 1. Before any brand rule revision is executed by the operator, anomalies queue is empty (total_count == 0)
        #    Untouched brands (The Bridge, Gherardini, Alviero Martini, etc.) NEVER show up in anomalies!
        res_initial_anom = client.get("/api/v1/brands/blueprint-revision/anomalies")
        assert res_initial_anom.status_code == 200
        assert res_initial_anom.json()["total_count"] == 0

        # 2. Operator revises bp-merged-1 of brand-guess-1: regex is recalculated and Guess articles are reviewed
        mock_deduction = {
            "regex": r"^HWVG87[0-9]{4}(?P<color_code>[A-Z]{3})$",
            "explanation": "I primi 10 caratteri indicano il modello, gli ultimi 3 il colore.",
            "examples": [{"raw": "HWVG870123BLA", "normalized": "HWVG870123#"}],
        }
        with patch("src.services.blueprint_revision_service.CodesDeductionAgent.aexecute", new_callable=AsyncMock, return_value=mock_deduction), \
             patch("src.services.blueprint_revision_service.LLMClient.call", new_callable=AsyncMock, return_value="Spiegazione per Guess"), \
             patch("src.services.blueprint_revision_service.ArticleBlueprintsAgent.aexecute", new_callable=AsyncMock, return_value={"blueprints": []}), \
             patch("src.services.blueprint_revision_service.LLMClient.generate_embedding_sync", return_value=[0.1, 0.2, 0.3]):

            res_deduce = client.post(
                "/api/v1/brands/blueprint-revision/deduce-rule",
                json={
                    "blueprint_id": "bp-merged-1",
                    "partitions": [
                        {
                            "group_id": "group_original",
                            "label": "Borsa Grande",
                            "is_original": True,
                            "article_ids": ["art-123-bla-1", "art-123-bla-2", "art-123-red-1"],
                            "supplier_codes": ["HWVG870123BLA", "HWVG870123RED"],
                        },
                        {
                            "group_id": "group_new_1",
                            "label": "Portafoglio Piccolo",
                            "is_original": False,
                            "article_ids": ["art-999-bla-1"],
                            "supplier_codes": ["HWVG870999BLA"],
                        },
                    ],
                },
            )
            assert res_deduce.status_code == 200
            rev_job_id = res_deduce.json()["job_id"]

            res_prep = client.post(f"/api/v1/brands/blueprint-revision/{rev_job_id}/prepare-staging")
            assert res_prep.status_code == 200

            res_commit = client.post(f"/api/v1/brands/blueprint-revision/{rev_job_id}/commit")
            assert res_commit.status_code == 200
            assert res_commit.json()["remaining_anomalies"] == 2

        # 3. GET /anomalies now returns ONLY the 2 anomalies that emerged for the brand of reference (brand-guess-1):
        #    - bp-extra-split-1 (SPLIT_REQUIRED)
        #    - bp-dup-1 / bp-dup-2 (DUPLICATE_MODEL)
        res_anom = client.get("/api/v1/brands/blueprint-revision/anomalies")
        assert res_anom.status_code == 200
        anom_data = res_anom.json()
        assert anom_data["total_count"] == 2
        anomaly_by_bp = {item["blueprint_id"]: item for item in anom_data["anomalies"]}
        assert "bp-extra-split-1" in anomaly_by_bp
        assert anomaly_by_bp["bp-extra-split-1"]["anomaly_type"] == "SPLIT_REQUIRED"
        assert anomaly_by_bp["bp-extra-split-1"]["brand_id"] == "brand-guess-1"
        assert "bp-dup-1" in anomaly_by_bp
        assert anomaly_by_bp["bp-dup-1"]["anomaly_type"] == "DUPLICATE_MODEL"
        assert anomaly_by_bp["bp-dup-1"]["brand_id"] == "brand-guess-1"
        assert anomaly_by_bp["bp-dup-1"]["related_blueprint_ids"] == ["bp-dup-2"]

        # 2. Operator manually splits bp-extra-split-1 WITHOUT recalculating the brand heuristic
        with patch(
            "src.services.blueprint_revision_service.CodesDeductionAgent.aexecute",
            new_callable=AsyncMock,
        ) as mock_deduce_agent, patch(
            "src.services.blueprint_revision_service.ArticleBlueprintsAgent.aexecute",
            new_callable=AsyncMock,
            return_value={
                "blueprints": [
                    {
                        "vendor_code": "HWVG870555BLA",
                        "article_name": "Zaino GuessTest Grande Nuovo",
                        "description": "Descrizione generata da zero per Zaino Grande",
                        "tags": ["zaino", "grande"],
                        "materials": ["ecopelle"],
                    },
                    {
                        "vendor_code": "HWVG870666BLA",
                        "article_name": "Beauty Case GuessTest Nuovo",
                        "description": "Descrizione generata da zero per Beauty Case",
                        "tags": ["beauty", "piccolo"],
                        "materials": ["ecopelle"],
                    },
                ]
            },
        ), patch(
            "src.services.blueprint_revision_service.LLMClient.generate_embedding_sync",
            return_value=[0.2, 0.3, 0.4],
        ):
            res_manual_prep = client.post(
                "/api/v1/brands/blueprint-revision/prepare-manual-split",
                json={
                    "blueprint_id": "bp-extra-split-1",
                    "source_blueprint_ids": [],
                    "partitions": [
                        {
                            "group_id": "group_original",
                            "label": "Zaino Grande",
                            "is_original": True,
                            "article_ids": ["art-555-bla-1"],
                            "supplier_codes": ["HWVG870555BLA"],
                        },
                        {
                            "group_id": "group_new_1",
                            "label": "Beauty Case",
                            "is_original": False,
                            "article_ids": ["art-666-bla-1"],
                            "supplier_codes": ["HWVG870666BLA"],
                        },
                    ],
                },
            )
            assert res_manual_prep.status_code == 200
            # Verify heuristic deduction was NOT invoked
            mock_deduce_agent.assert_not_called()

            manual_job = res_manual_prep.json()
            manual_job_id = manual_job["job_id"]
            assert manual_job["data"]["skip_rule_update"] is True
            after_cards = manual_job["data"]["reconciliation_plan"]["target_split"]["after"]
            assert len(after_cards) == 2
            assert after_cards[0]["article_name"] == "Zaino GuessTest Grande Nuovo"
            assert after_cards[0]["description"] == "Descrizione generata da zero per Zaino Grande"
            assert after_cards[1]["article_name"] == "Beauty Case GuessTest Nuovo"

            # Commit manual split
            res_manual_commit = client.post(f"/api/v1/brands/blueprint-revision/{manual_job_id}/commit")
            assert res_manual_commit.status_code == 200
            assert res_manual_commit.json()["splits_created"] == 2

        # Verify bp-extra-split-1 was deleted and replaced by 2 brand-new blueprints
        assert db_session.query(ArticleBlueprint).filter(ArticleBlueprint.id == "bp-extra-split-1").first() is None
        bp_555 = (
            db_session.query(ArticleBlueprint)
            .filter(ArticleBlueprint.normalized_vendor_code == "HWVG870555#")
            .first()
        )
        bp_666 = (
            db_session.query(ArticleBlueprint)
            .filter(ArticleBlueprint.normalized_vendor_code == "HWVG870666#")
            .first()
        )
        assert bp_555 is not None
        assert bp_555.article_name == "Zaino GuessTest Grande Nuovo"
        assert bp_666 is not None
        assert bp_666.article_name == "Beauty Case GuessTest Nuovo"

        # 3. Operator manually resolves duplicate blueprints (bp-dup-1 + bp-dup-2)
        res_ctx_dup = client.get("/api/v1/brands/blueprint-revision/bp-dup-1/context?related_ids=bp-dup-2")
        assert res_ctx_dup.status_code == 200
        ctx_dup = res_ctx_dup.json()
        # Both articles from bp-dup-1 and bp-dup-2 are loaded in the same screen
        assert len(ctx_dup["article_groups"]) == 2

        with patch(
            "src.services.blueprint_revision_service.ArticleBlueprintsAgent.aexecute",
            new_callable=AsyncMock,
            return_value={"blueprints": []},
        ), patch(
            "src.services.blueprint_revision_service.LLMClient.generate_embedding_sync",
            return_value=[0.5, 0.5, 0.5],
        ):
            res_dup_prep = client.post(
                "/api/v1/brands/blueprint-revision/prepare-manual-split",
                json={
                    "blueprint_id": "bp-dup-1",
                    "source_blueprint_ids": ["bp-dup-2"],
                    "partitions": [
                        {
                            "group_id": "group_original",
                            "label": "Tracolla GuessTest Unificata",
                            "is_original": True,
                            "article_ids": ["art-777-bla-1", "art-777-whi-1"],
                            "supplier_codes": ["HWVG870777BLA", "HWVG870777WHI"],
                        }
                    ],
                },
            )
            assert res_dup_prep.status_code == 200
            dup_job_id = res_dup_prep.json()["job_id"]

            res_dup_commit = client.post(f"/api/v1/brands/blueprint-revision/{dup_job_id}/commit")
            assert res_dup_commit.status_code == 200
            # All anomalies for this brand are now resolved (queue is empty)
            assert res_dup_commit.json()["remaining_anomalies"] == 0

        # Verify both old duplicate blueprints were replaced by 1 new blueprint preserving both nero and bianco photos
        assert db_session.query(ArticleBlueprint).filter(ArticleBlueprint.id == "bp-dup-1").first() is None
        assert db_session.query(ArticleBlueprint).filter(ArticleBlueprint.id == "bp-dup-2").first() is None
        bp_777 = (
            db_session.query(ArticleBlueprint)
            .filter(ArticleBlueprint.normalized_vendor_code == "HWVG870777#")
            .first()
        )
        assert bp_777 is not None
        photos_777 = db_session.query(ArticlePhoto).filter(ArticlePhoto.article_blueprint_id == bp_777.id).all()
        assert {p.canonical_color_name for p in photos_777} == {"nero", "bianco"}


