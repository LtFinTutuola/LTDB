"""
tests/test_photo_search_agent.py
--------------------------------
Unit and integration tests for the decoupled architecture between
`ArticleBlueprintsAgent` and `PhotoSearchAgent`:
  - `ArticleBlueprintsAgent` does not expose `photo_proposals` or `photo_only_items`
  - `PhotoSearchAgent` executes the 1-shot linear pipeline (Query Gen -> Grounded Search -> HTTP Guard)
    across ingestion, retry, catalog, and blueprint revision modes
  - `(Blueprint, color)` enforcement and separation of concerns in `_stage_extracted_items`
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.agents.article_blueprints_agent import ArticleBlueprintsAgent
from src.agents.article_blueprints_agent.state import BlueprintsGraphState
from src.agents.photo_search_agent import PhotoSearchAgent
from src.agents.photo_search_agent.nodes.grounded_search_node import extract_best_url
from src.models.pim import ArticleBlueprint, Brand, BrandHeuristic, Category, HeuristicMode
from src.repositories.photo_repo import photo_repo


class TestArticleBlueprintsAgentDecoupling:
    def test_state_and_output_do_not_expose_photo_fields(self):
        fields = set(BlueprintsGraphState.model_fields.keys())
        assert "photo_proposals" not in fields
        assert "photo_only_items" not in fields
        assert "brand_search_context" not in fields

    @pytest.mark.asyncio
    async def test_empty_new_blueprints_returns_without_photo_fields(self):
        agent = ArticleBlueprintsAgent()
        res = await agent.aexecute({
            "new_blueprints": [],
            "categories": {},
            "brand_name": "TestBrand",
            "resolved_blueprints": {},
        })
        assert res["items"] == []
        assert res["blueprints"] == []
        assert "photo_proposals" not in res
        assert "photo_only_items" not in res


class TestPhotoSearchAgentLinearExecution:
    def test_extract_best_url_prefers_direct_image_over_grounding_redirect(self):
        redirect = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc"
        direct = "https://cdn.brand.com/img/bag_nero.jpg"
        assert extract_best_url([redirect], f"Ecco la foto: {direct}") == direct
        assert extract_best_url([redirect, "https://other.site/page"], "nessun url") == "https://other.site/page"

    @pytest.mark.asyncio
    async def test_single_shot_ingestion_mode_prompts_and_contract(self):
        mock_client = MagicMock()
        mock_client.call = AsyncMock(return_value="Mandarina Duck P10QMT71 taupe packshot")
        mock_client.call_with_grounding = AsyncMock(
            return_value=("https://www.mandarinaduck.com/img/P10QMT71_09KK.jpg", [])
        )

        agent = PhotoSearchAgent(llm_client=mock_client)
        with patch("src.agents.photo_search_agent.nodes.validate_guard_node._validate_photo_url", return_value=True):
            out = await agent.aexecute({
                "items_with_blueprints": [
                    {
                        "item": {"item_id": "it-1", "vendor_code": "P10QMT7109KK", "colors": ["Taupe"]},
                        "blueprint": {"id": "bp-1", "article_name": "MD20 Tracolla", "description": "Borsa tracolla"},
                    }
                ],
                "brand_name": "Mandarina Duck",
            })

        assert len(out["photo_proposals"]) == 1
        prop = out["photo_proposals"][0]
        assert prop["item_id"] == "it-1"
        assert prop["photo_url"] == "https://www.mandarinaduck.com/img/P10QMT71_09KK.jpg"
        assert prop["photo_source"] == "web_search"
        assert prop["canonical_color_candidate"] == "taupe"
        assert prop["retry_count"] == 0
        assert prop["search_query"] == "Mandarina Duck P10QMT71 taupe packshot"
        assert len(prop["_photo_search_ctx"]) == 2
        assert prop["_photo_search_ctx"][0]["role"] == "user"
        assert prop["_photo_search_ctx"][1]["role"] == "model"

        # Single-shot: exactly 1 query gen call + 1 grounded call
        assert mock_client.call.call_count == 1
        assert mock_client.call.call_args.kwargs["temperature"] == 0.1
        assert mock_client.call.call_args.kwargs["pipeline_stage"] == "Stage 5 - Photo Query Gen"
        assert mock_client.call_with_grounding.call_count == 1
        assert mock_client.call_with_grounding.call_args.kwargs["temperature"] == 0.2
        assert mock_client.call_with_grounding.call_args.kwargs["max_output_tokens"] == 4096
        assert mock_client.call_with_grounding.call_args.kwargs["thinking_level"] == "LOW"
        assert mock_client.call_with_grounding.call_args.kwargs["pipeline_stage"] == "Stage 5 - Photo Search Grounded"

    @pytest.mark.asyncio
    async def test_single_shot_rejects_invalid_url_and_preserves_query(self):
        mock_client = MagicMock()
        mock_client.call = AsyncMock(return_value="Caleidos CA138C nero packshot")
        mock_client.call_with_grounding = AsyncMock(
            return_value=("https://example.com/broken.jpg", [])
        )

        agent = PhotoSearchAgent(llm_client=mock_client)
        with patch("src.agents.photo_search_agent.nodes.validate_guard_node._validate_photo_url", return_value=False):
            out = await agent.aexecute({
                "item_id": "it-cal",
                "brand_name": "Caleidos",
                "vendor_code": "CA138C",
                "color": "nero",
                "article_name": "Borsa Shopping",
                "mode": "catalog",
            })

        prop = out["proposal"]
        assert prop["photo_url"] is None
        assert prop["search_query"] == "Caleidos CA138C nero packshot"
        assert mock_client.call.call_args.kwargs["temperature"] == 0.3
        assert mock_client.call.call_args.kwargs["pipeline_stage"] == "Catalog Photo Search - Query Gen"
        assert mock_client.call_with_grounding.call_args.kwargs["pipeline_stage"] == "Catalog Photo Search - Grounded"

    @pytest.mark.asyncio
    async def test_single_shot_retry_mode_replays_ctx_and_uses_retry_temperature(self):
        mock_client = MagicMock()
        mock_client.call = AsyncMock(return_value="Mandarina Duck P10QMT71 taupe back packshot")
        mock_client.call_with_grounding = AsyncMock(
            return_value=("https://www.mandarinaduck.com/img/P10QMT71_09KK_v2.jpg", [])
        )

        prev_ctx = [
            {"role": "user", "content": "query originale"},
            {"role": "model", "content": "Mandarina Duck P10QMT71 taupe"},
        ]
        agent = PhotoSearchAgent(llm_client=mock_client)
        with patch("src.agents.photo_search_agent.nodes.validate_guard_node._validate_photo_url", return_value=True):
            out = await agent.aexecute({
                "item_id": "it-retry",
                "brand_name": "Mandarina Duck",
                "vendor_code": "P10QMT7109KK",
                "color": "taupe",
                "article_name": "MD20 Tracolla",
                "description": "Borsa tracolla",
                "user_feedback": "era il colore sbagliato, serve taupe chiarissimo",
                "previous_ctx": prev_ctx,
                "mode": "retry",
            })

        prop = out["proposal"]
        assert prop["photo_url"] == "https://www.mandarinaduck.com/img/P10QMT71_09KK_v2.jpg"
        assert prop["search_query"] == "Mandarina Duck P10QMT71 taupe back packshot"
        assert len(prop["_photo_search_ctx"]) == 4
        assert "era il colore sbagliato" in prop["_photo_search_ctx"][2]["content"]
        assert prop["_photo_search_ctx"][3] == {
            "role": "model",
            "content": "Mandarina Duck P10QMT71 taupe back packshot",
        }
        assert mock_client.call.call_args.kwargs["temperature"] == 0.4
        assert mock_client.call.call_args.kwargs["pipeline_stage"] == "Stage 5 - Photo Retry Query Gen"
        assert mock_client.call_with_grounding.call_args.kwargs["pipeline_stage"] == "Stage 5 - Photo Retry Grounded"

    @pytest.mark.asyncio
    async def test_single_shot_revision_mode_stage_and_contract(self):
        mock_client = MagicMock()
        mock_client.call = AsyncMock(return_value="Mandarina Duck EX001 nero packshot")
        mock_client.call_with_grounding = AsyncMock(
            return_value=("https://cdn.example.com/EX001_nero.jpg", [])
        )

        agent = PhotoSearchAgent(llm_client=mock_client)
        with patch("src.agents.photo_search_agent.nodes.validate_guard_node._validate_photo_url", return_value=True):
            out = await agent.aexecute({
                "item_id": "it-rev",
                "brand_name": "Mandarina Duck",
                "vendor_code": "EX001-BLK",
                "color": "nero",
                "article_name": "Zaino Existing",
                "mode": "revision",
            })

        prop = out["proposal"]
        assert prop["photo_url"] == "https://cdn.example.com/EX001_nero.jpg"
        assert mock_client.call.call_args.kwargs["temperature"] == 0.3
        assert mock_client.call.call_args.kwargs["pipeline_stage"] == "Blueprint Revision - Photo Search Query Gen"
        assert mock_client.call_with_grounding.call_args.kwargs["pipeline_stage"] == "Blueprint Revision - Photo Search Grounded"


class TestIngestionSeparationOfConcerns:
    @pytest.mark.asyncio
    async def test_stage_dispatches_photo_search_for_tuple_blueprint_color(self, db_session, monkeypatch):
        """
        Verify strict separation of concerns:
        - `ArticleBlueprintsAgent` receives only `new_blueprints` (no `photo_only_items`)
        - Oracle-attached items already having an `ArticlePhoto` for `(blueprint, color)` reuse DB photo
        - Existing or new `(blueprint, color)` items without DB photo are passed to `PhotoSearchAgent`
        - Items with empty `colors` are skipped (search always keyed on `(Blueprint, color)`)
        """
        from src.services.data_ingestion_service import _stage_extracted_items

        brand = Brand(name="Mandarina Duck")
        db_session.add(brand)
        cat = Category(name="Borse", description="Borse donna")
        db_session.add(cat)
        db_session.flush()

        heur = BrandHeuristic(
            brand_id=brand.id,
            pattern=r"^(?P<model_code>[A-Z0-9]+)-(?P<color_code>[A-Z]+)$",
            heuristic_mode=HeuristicMode.COLOR_MASKED,
            heuristic_confirmed=True,
        )
        db_session.add(heur)

        existing_bp = ArticleBlueprint(
            brand_id=brand.id,
            category_id=cat.id,
            normalized_vendor_code="EX001-###",
            article_name="Zaino Existing",
            description="Zaino",
            extended_description="Zaino esteso",
            tags=["zaino"],
            materials=["nylon"],
        )
        db_session.add(existing_bp)
        db_session.flush()

        # Existing DB photo ONLY for (existing_bp, "nero") — NOT for "blu" or "verde"
        photo_repo.create_photo(db_session, str(existing_bp.id), "nero", b"nero-img")
        db_session.commit()

        bp_agent_calls = []
        photo_agent_calls = []

        async def mock_bp_aexecute(self, input_data):
            bp_agent_calls.append(input_data)
            assert "photo_only_items" not in input_data
            assert "brand_search_context" not in input_data
            return {
                "items": [
                    {
                        "item_id": "it-oracle-nero",
                        "vendor_code": "NEW88-BLK",
                        "normalized_vendor_code": "NEW88-###",
                        "quantity": 1,
                        "colors": ["nero"],
                        "article_blueprint_id": str(existing_bp.id),
                    },
                    {
                        "item_id": "it-oracle-verde",
                        "vendor_code": "NEW88-GRN",
                        "normalized_vendor_code": "NEW88-###",
                        "quantity": 1,
                        "colors": ["verde"],
                        "article_blueprint_id": str(existing_bp.id),
                    },
                    {
                        "item_id": "it-new-red",
                        "vendor_code": "NEW99-RED",
                        "normalized_vendor_code": "NEW99-###",
                        "quantity": 1,
                        "colors": ["rosso"],
                        "article_blueprint_id": "bp-new-99",
                    },
                    {
                        "item_id": "it-new-nocolor",
                        "vendor_code": "NEW99-UNK",
                        "normalized_vendor_code": "NEW99-###",
                        "quantity": 1,
                        "colors": [],
                        "article_blueprint_id": "bp-new-99",
                    },
                ],
                "blueprints": [
                    {
                        "id": "bp-new-99",
                        "is_new": True,
                        "category": "Borse",
                        "sub_category": None,
                        "article_name": "Borsa Nuova",
                        "description": "Borsa",
                        "extended_description": "Borsa estesa",
                        "tags": ["borsa"],
                        "materials": ["pelle"],
                    }
                ],
                "warnings": [],
                "heuristic_reconciliations": [],
            }

        async def mock_photo_aexecute(self, input_data):
            photo_agent_calls.append(input_data)
            assert "brand_search_context" not in input_data
            proposals = []
            for entry in input_data["items_with_blueprints"]:
                it = entry["item"]
                col = (it.get("colors") or ["unknown"])[0].lower()
                proposals.append({
                    "item_id": it["item_id"],
                    "photo_url": f"https://cdn.example.com/{it['item_id']}_{col}.jpg",
                    "photo_source": "web_search",
                    "canonical_color_candidate": col,
                    "retry_count": 0,
                    "_photo_search_ctx": [],
                    "search_query": f"Mandarina Duck {it.get('vendor_code')} {col}",
                })
            return {"photo_proposals": proposals}

        monkeypatch.setattr("src.services.data_ingestion_service.ArticleBlueprintsAgent.aexecute", mock_bp_aexecute)
        monkeypatch.setattr("src.services.data_ingestion_service.PhotoSearchAgent.aexecute", mock_photo_aexecute)

        staged = await _stage_extracted_items(
            db=db_session,
            job_id="job-soc-test",
            brand_id=str(brand.id),
            brand_name=brand.name,
            categories={"Borse": {"description": "Borse donna", "sub_categories": {}}},
            heuristics=[heur],
            extracted_items=[
                {"item_id": "it-ex-nero", "vendor_code": "EX001-BLK", "quantity": 1, "colors": ["nero"]},
                {"item_id": "it-ex-blu", "vendor_code": "EX001-BLU", "quantity": 1, "colors": ["blu"]},
                {"item_id": "it-oracle-nero", "vendor_code": "NEW88-BLK", "quantity": 1, "colors": ["nero"]},
                {"item_id": "it-oracle-verde", "vendor_code": "NEW88-GRN", "quantity": 1, "colors": ["verde"]},
                {"item_id": "it-new-red", "vendor_code": "NEW99-RED", "quantity": 1, "colors": ["rosso"]},
                {"item_id": "it-new-nocolor", "vendor_code": "NEW99-UNK", "quantity": 1, "colors": []},
            ],
            extraction_warnings=[],
        )

        assert len(bp_agent_calls) == 1
        assert len(photo_agent_calls) == 1

        dispatched_ids = {entry["item"]["item_id"] for entry in photo_agent_calls[0]["items_with_blueprints"]}
        # `it-ex-nero` and `it-oracle-nero` reuse DB photo for (existing_bp, "nero");
        # `it-new-nocolor` is skipped (empty colors);
        # `it-ex-blu` (photo_only), `it-oracle-verde`, and `it-new-red` are searched!
        assert dispatched_ids == {"it-ex-blu", "it-oracle-verde", "it-new-red"}

        by_id = {p["item_id"]: p for p in staged["photo_proposals"]}
        assert by_id["it-ex-nero"]["photo_source"] == "db_existing"
        assert by_id["it-oracle-nero"]["photo_source"] == "db_existing"
        assert by_id["it-ex-blu"]["photo_url"] == "https://cdn.example.com/it-ex-blu_blu.jpg"
        assert by_id["it-oracle-verde"]["photo_url"] == "https://cdn.example.com/it-oracle-verde_verde.jpg"
        assert by_id["it-new-red"]["photo_url"] == "https://cdn.example.com/it-new-red_rosso.jpg"
        assert "it-new-nocolor" not in by_id


@pytest.mark.asyncio
async def test_grounded_search_retries_when_null_without_grounding_evidence():
    from src.agents.photo_search_agent.nodes.grounded_search_node import grounded_image_search_node
    from src.agents.photo_search_agent.state import PhotoSearchItemState

    mock_client = MagicMock()
    mock_client.get_logs.return_value = []
    mock_client.call_with_grounding = AsyncMock(
        side_effect=[
            ("NULL", []),
            ("https://www.ynot.it/media/STE008F7-BLUE.jpg", []),
        ]
    )

    state = PhotoSearchItemState(
        item_id="it-retry",
        brand_name="Ynot? Milano",
        vendor_code="STE008F7",
        color="deepblue",
        current_query="Ynot? Milano STE008F7 deep blue",
    )
    res = await grounded_image_search_node(state, client=mock_client)

    assert res["candidate_url"] == "https://www.ynot.it/media/STE008F7-BLUE.jpg"
    assert mock_client.call_with_grounding.call_count == 2
    assert mock_client.call_with_grounding.call_args_list[0].kwargs["temperature"] == 0.2
    assert mock_client.call_with_grounding.call_args_list[1].kwargs["temperature"] == 0.5
    assert "Google Search" in mock_client.call_with_grounding.call_args_list[1].kwargs["prompt"]


@pytest.mark.asyncio
async def test_grounded_search_stops_on_null_when_grounding_evidence_present():
    from src.agents.photo_search_agent.nodes.grounded_search_node import grounded_image_search_node
    from src.agents.photo_search_agent.state import PhotoSearchItemState

    mock_client = MagicMock()
    mock_client.get_logs.return_value = []
    mock_client.call_with_grounding = AsyncMock(
        return_value=("NULL", ["https://www.ynot.it/product/ste008f7"])
    )

    state = PhotoSearchItemState(
        item_id="it-grounded-null",
        brand_name="Ynot? Milano",
        vendor_code="STE008F7",
        color="deepblue",
        current_query="Ynot? Milano STE008F7 deep blue",
    )
    res = await grounded_image_search_node(state, client=mock_client)

    assert res["candidate_url"] is None
    assert mock_client.call_with_grounding.call_count == 1
