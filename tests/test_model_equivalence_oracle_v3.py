"""
Unit tests for Model Equivalence Oracle v3.0 (F1 & F2).
Verifica:
  - Validazione configurazione e invarianti R4
  - Morfologia del codice deterministica (IDENTICAL, VERY_HIGH, HIGH, NEUTRAL, CONFLICT)
  - Relazione colore deterministica (DIFFERENT, SAME, UNKNOWN)
  - Costruzione del contesto comparativo (cecità selettiva su codici, categorie, raw_description)
  - Motore di scoring deterministico e regole strutturali R1-R7
  - Valutazione asincrona a micro-giudici paralleli ed error guardrail (R7)
"""
import asyncio
import pytest
from src.agents.article_blueprints_agent.nodes.model_equivalence_oracle import (
    AMBIGUOUS,
    CODE_CONFLICT,
    CODE_HIGH,
    CODE_IDENTICAL,
    CODE_NEUTRAL,
    CODE_VERY_HIGH,
    DIFFERENT_MODEL,
    SAME_MODEL,
    DimensionEvaluationResult,
    build_article_comparison_context,
    calc_code_morphology,
    calc_color_relation,
    evaluate_equivalence_matrix,
    evaluate_pair,
    is_generic_article,
    load_oracle_config,
    validate_oracle_config,
)


def test_load_and_validate_config():
    cfg = load_oracle_config()
    assert cfg["high_threshold"] == 75
    assert cfg["low_threshold"] == 35
    assert cfg["negative_constraint_threshold"] == 20
    validate_oracle_config(cfg)

    # Invalida soglie
    bad_cfg = dict(cfg)
    bad_cfg["low_threshold"] = 80
    with pytest.raises(ValueError, match="Configurazione oracolo invalida"):
        validate_oracle_config(bad_cfg)

    # Invalida invariante R4 (auto-merge debole)
    bad_r4 = dict(cfg)
    bad_r4["dimensions"]["TYPOLOGY"]["MATCH"] = 25  # 50 + 25 + 3 + 6 = 84 >= 75
    with pytest.raises(ValueError, match="Violazione Invariante R4"):
        validate_oracle_config(bad_r4)


def test_code_morphology():
    cfg = load_oracle_config()

    # IDENTICAL
    assert calc_code_morphology("P10QMT7109K", "P10QMT7109K", cfg) == CODE_IDENTICAL

    # VERY_HIGH: 8 caratteri comuni su 11, residuo 3
    assert calc_code_morphology("P10QMT7109K", "P10QMT71803", cfg) == CODE_VERY_HIGH

    # HIGH: prefisso >= 60%, residuo variabile <= 6
    assert calc_code_morphology("ABCDEF12", "ABCDEF99", cfg) == CODE_VERY_HIGH
    # HIGH: residuo 5 (superiore a max_res_vh=4) e ratio >= 0.60
    assert calc_code_morphology("ABCDEFGH12345", "ABCDEFGH67890", cfg) == CODE_HIGH

    # NEUTRAL: prefisso 4 ma residuo troppo lungo
    assert calc_code_morphology("ABCD111111", "ABCD222222", cfg) == CODE_NEUTRAL

    # CONFLICT: lunghezze diverse o prefisso < 4
    assert calc_code_morphology("ABC", "ABCDEFG", cfg) == CODE_CONFLICT
    assert calc_code_morphology("XYZ123", "ABC123", cfg) == CODE_CONFLICT


def test_color_relation():
    assert calc_color_relation(["taupe"], ["sin"]) == "DIFFERENT"
    assert calc_color_relation(["nero"], ["nero", "rosso"]) == "SAME"
    assert calc_color_relation([], ["nero"]) == "UNKNOWN"
    assert calc_color_relation(None, ["rosso"]) == "UNKNOWN"


def test_build_article_comparison_context():
    item = {
        "vendor_code": "SECRET_VENDOR_CODE_123",
        "supplier_code": "SECRET_SUPPLIER_CODE_456",
        "category": "SECRET_CATEGORY_BORSE",
        "sub_category": "SECRET_SUB_ZAINI",
        "raw_description": "SECRET_RAW_INVOICE_LINE",
        "colors": ["blu", "nero"],
        "article_name": "Zaino MD20",
        "description": "Zaino pratico",
        "extended_description": "Zaino pratico in tessuto tecnico con spallacci jacquard.",
        "materials": ["tessuto tecnico", "jacquard"],
        "tags": ["zaino", "lavoro"],
        "dimensions": {"width_cm": 30, "height_cm": 40, "depth_cm": 15},
    }
    context = build_article_comparison_context(item)

    # I campi sensibili / non confermati devono essere tassativamente ASSENTI
    assert "SECRET_VENDOR_CODE_123" not in context
    assert "SECRET_SUPPLIER_CODE_456" not in context
    assert "SECRET_CATEGORY_BORSE" not in context
    assert "SECRET_SUB_ZAINI" not in context
    assert "SECRET_RAW_INVOICE_LINE" not in context
    assert "blu" not in context

    # I campi autorizzati devono essere PRESENTI
    assert "Zaino MD20" in context
    assert "tessuto tecnico" in context
    assert "larghezza=30.0cm" in context
    assert "altezza=40.0cm" in context


def test_is_generic_article():
    assert is_generic_article({"article_name": "Borsa", "description": ""}) is True
    assert is_generic_article({
        "article_name": "Zaino MD20 Mandarina Duck",
        "description": "Zaino in tessuto tecnico resistente",
    }) is False


def test_scoring_matrix_scenarios():
    cfg = load_oracle_config()

    # S1: Mandarina Duck Taupe vs Sin (Caso incidente risolto)
    dims_s1 = {
        "TYPOLOGY": DimensionEvaluationResult(dimension="TYPOLOGY", verdict="MATCH"),
        "LINE": DimensionEvaluationResult(dimension="LINE", verdict="MATCH"),
        "SIZE": DimensionEvaluationResult(dimension="SIZE", verdict="UNKNOWN"),
        "MATERIAL": DimensionEvaluationResult(dimension="MATERIAL", verdict="MATCH"),
        "CONSTRUCTION": DimensionEvaluationResult(dimension="CONSTRUCTION", verdict="MATCH"),
    }
    res_s1 = evaluate_equivalence_matrix(dims_s1, CODE_VERY_HIGH, cfg)
    assert res_s1["verdict"] == SAME_MODEL
    assert res_s1["final_score"] >= 75
    assert res_s1["write_negative_constraint"] is False

    # S2: Mandarina Sacca vs Zaino (Modelli diversi)
    dims_s2 = {
        "TYPOLOGY": DimensionEvaluationResult(dimension="TYPOLOGY", verdict="CONFLICT"),
        "LINE": DimensionEvaluationResult(dimension="LINE", verdict="MATCH"),
        "SIZE": DimensionEvaluationResult(dimension="SIZE", verdict="UNKNOWN"),
        "MATERIAL": DimensionEvaluationResult(dimension="MATERIAL", verdict="MATCH"),
        "CONSTRUCTION": DimensionEvaluationResult(dimension="CONSTRUCTION", verdict="CONFLICT"),
    }
    res_s2 = evaluate_equivalence_matrix(dims_s2, CODE_NEUTRAL, cfg)
    assert res_s2["verdict"] == DIFFERENT_MODEL
    assert res_s2["final_score"] <= 20
    assert res_s2["write_negative_constraint"] is True  # R5: score <= 20

    # S3: Trolley 67cm vs 77cm (Conflitto singolo su SIZE -> AMBIGUOUS)
    dims_s3 = {
        "TYPOLOGY": DimensionEvaluationResult(dimension="TYPOLOGY", verdict="MATCH"),
        "LINE": DimensionEvaluationResult(dimension="LINE", verdict="MATCH"),
        "SIZE": DimensionEvaluationResult(dimension="SIZE", verdict="CONFLICT"),
        "MATERIAL": DimensionEvaluationResult(dimension="MATERIAL", verdict="MATCH"),
        "CONSTRUCTION": DimensionEvaluationResult(dimension="CONSTRUCTION", verdict="MATCH"),
    }
    res_s3 = evaluate_equivalence_matrix(dims_s3, CODE_VERY_HIGH, cfg)
    assert res_s3["verdict"] == AMBIGUOUS
    assert res_s3["ambiguity_reason"] == "SINGLE_CONFLICT"
    assert res_s3["write_negative_constraint"] is False

    # R3: Se c'è solo 1 conflitto debole e punteggio basso, R3 forza AMBIGUOUS (mai DIFFERENT automatico)
    dims_r3 = {
        "TYPOLOGY": DimensionEvaluationResult(dimension="TYPOLOGY", verdict="UNKNOWN"),
        "LINE": DimensionEvaluationResult(dimension="LINE", verdict="CONFLICT"),
        "SIZE": DimensionEvaluationResult(dimension="SIZE", verdict="UNKNOWN"),
        "MATERIAL": DimensionEvaluationResult(dimension="MATERIAL", verdict="UNKNOWN"),
        "CONSTRUCTION": DimensionEvaluationResult(dimension="CONSTRUCTION", verdict="UNKNOWN"),
    }
    res_r3 = evaluate_equivalence_matrix(dims_r3, CODE_CONFLICT, cfg)
    assert res_r3["conflict_count"] == 1
    assert res_r3["verdict"] == AMBIGUOUS
    assert res_r3["write_negative_constraint"] is False

    # R6: Codice identico ma oracolo non raggiunge SAME -> R6 forza AMBIGUOUS (mai DIFFERENT)
    dims_r6 = {
        "TYPOLOGY": DimensionEvaluationResult(dimension="TYPOLOGY", verdict="CONFLICT"),
        "LINE": DimensionEvaluationResult(dimension="LINE", verdict="CONFLICT"),
        "SIZE": DimensionEvaluationResult(dimension="SIZE", verdict="UNKNOWN"),
        "MATERIAL": DimensionEvaluationResult(dimension="MATERIAL", verdict="UNKNOWN"),
        "CONSTRUCTION": DimensionEvaluationResult(dimension="CONSTRUCTION", verdict="UNKNOWN"),
    }
    res_r6 = evaluate_equivalence_matrix(dims_r6, CODE_IDENTICAL, cfg)
    assert res_r6["verdict"] == AMBIGUOUS
    assert res_r6["ambiguity_reason"] == "IDENTICAL_CODE_NOT_SAME"
    assert res_r6["write_negative_constraint"] is False

    # R7: Errore o input generico forza AMBIGUOUS
    res_r7_err = evaluate_equivalence_matrix(dims_s1, CODE_VERY_HIGH, cfg, has_error=True, error_reason="Timeout")
    assert res_r7_err["verdict"] == AMBIGUOUS
    assert res_r7_err["ambiguity_reason"] == "JUDGE_ERROR"
    assert res_r7_err["write_negative_constraint"] is False

    res_r7_gen = evaluate_equivalence_matrix(dims_s1, CODE_VERY_HIGH, cfg, is_generic=True)
    assert res_r7_gen["verdict"] == AMBIGUOUS
    assert res_r7_gen["ambiguity_reason"] == "GENERIC_INPUT"
    assert res_r7_gen["write_negative_constraint"] is False


@pytest.mark.asyncio
async def test_async_evaluate_pair_with_mock_client():
    class MockLLMClient:
        def __init__(self, fail_dim=None):
            self.fail_dim = fail_dim
            self.calls = []

        async def call(self, **kwargs):
            self.calls.append(kwargs)
            stage = kwargs.get("pipeline_stage", "")
            if self.fail_dim and f"Oracle:{self.fail_dim}" in stage:
                raise RuntimeError("Simulated API rate limit error")

            dim = stage.replace("Oracle:", "")
            return DimensionEvaluationResult(
                dimension=dim,
                extracted_value_x="val_x",
                extracted_value_y="val_y",
                verdict="MATCH",
                reasoning="Compatible test",
            ).model_dump_json()

    item_a = {
        "vendor_code": "P10QMT7109K",
        "article_name": "Mandarina Duck MD20 Zaino",
        "description": "Zaino taupe",
        "extended_description": "Zaino in tessuto tecnico Mitrix",
        "colors": ["taupe"],
    }
    item_b = {
        "vendor_code": "P10QMT71803",
        "article_name": "Zaino MD20",
        "description": "Zaino sin",
        "extended_description": "Zaino in tessuto tecnico cangiante",
        "colors": ["sin"],
    }

    client = MockLLMClient()
    result = await evaluate_pair(client, item_a, item_b)

    assert result.verdict == SAME_MODEL
    assert result.color_relation == "DIFFERENT"
    assert result.code_verdict == CODE_VERY_HIGH
    assert len(client.calls) == 5

    # Test error guardrail (R7)
    failing_client = MockLLMClient(fail_dim="SIZE")
    res_err = await evaluate_pair(failing_client, item_a, item_b)
    assert res_err.verdict == AMBIGUOUS
    assert res_err.ambiguity_reason == "JUDGE_ERROR"
    assert res_err.write_negative_constraint is False
