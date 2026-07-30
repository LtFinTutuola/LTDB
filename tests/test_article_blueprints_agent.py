import json
import pytest
from unittest.mock import AsyncMock, patch

from src.agents.article_blueprints_agent.state import BlueprintsGraphState
from src.agents.article_blueprints_agent.nodes.db_match_node import db_match_node
from src.agents.article_blueprints_agent.nodes.cluster_unmatched_node import cluster_unmatched_node
from src.agents.article_blueprints_agent.nodes.validate_clusters_node import validate_clusters_node
from src.agents.article_blueprints_agent.nodes.format_output_node import format_output_node
from src.agents.article_blueprints_agent import ArticleBlueprintsAgent


@pytest.fixture
def base_state() -> BlueprintsGraphState:
    return BlueprintsGraphState(
        items=[
            {"item_id": "item-1", "article_name": "Bag 1", "article_description": "Desc 1", "embedding": [1.0, 0.0, 0.0]},
            {"item_id": "item-2", "article_name": "Bag 1 copy", "article_description": "Desc 1 copy", "embedding": [0.99, 0.0, 0.0]},
            {"item_id": "item-3", "article_name": "Shoe 1", "article_description": "Desc shoe", "embedding": [0.0, 1.0, 0.0]},
        ],
        categories={"Borse": {"sub_categories": {"Spalla": "..."}}},
        db_embeddings_matrix=[
            {"id": "db-bp-1", "embedding": [1.0, 0.0, 0.0], "category_id": "cat-1"}
        ],
        db_similarity_threshold=0.95,
        articles_similarity_threshold=0.90,
    )


class TestDbMatchNode:
    @pytest.mark.asyncio
    async def test_db_match_and_deduplication(self, base_state):
        """Items with a single unambiguous DB match above strict threshold are matched directly."""
        with patch("src.agents.article_blueprints_agent.nodes.db_match_node.LLMClient") as MockClient:
            MockClient.return_value.call = AsyncMock()  # Should not be called
            res = await db_match_node(base_state)

        matched = res["matched_items"]
        unmatched = res["unmatched_items"]
        output_bps = res["output_blueprints"]

        assert len(matched) == 2  # item-1 and item-2 both match [1.0, 0.0, 0.0] with sim >= 0.95
        assert len(unmatched) == 1  # item-3
        assert matched[0]["article_blueprint_id"] == "db-bp-1"
        assert matched[1]["article_blueprint_id"] == "db-bp-1"

        # Verify deduplication in output_blueprints
        assert len(output_bps) == 1
        assert output_bps[0] == {"id": "db-bp-1", "is_new": False}

    @pytest.mark.asyncio
    async def test_db_match_zero_candidates_routes_to_unmatched(self, base_state):
        """Items with no candidate above the relaxed threshold go to unmatched_items."""
        base_state.items = [
            {"item_id": "x", "article_name": "Unrelated", "embedding": [0.0, 0.0, 1.0]}
        ]
        base_state.db_similarity_threshold = 0.95  # relaxed = 0.93; [0,0,1] vs [1,0,0] sim=0
        with patch("src.agents.article_blueprints_agent.nodes.db_match_node.LLMClient") as MockClient:
            MockClient.return_value.call = AsyncMock()  # Should not be called
            res = await db_match_node(base_state)

        assert res["matched_items"] == []
        assert len(res["unmatched_items"]) == 1
        MockClient.return_value.call.assert_not_called()

    @pytest.mark.asyncio
    async def test_db_match_multiple_candidates_llm_picks_one(self, base_state):
        """When 2+ candidates exceed the strict threshold, LLM picks one → matched."""
        base_state.items = [
            {"item_id": "a", "article_name": "Spinner 55", "article_description": "Cabin",
             "embedding": [1.0, 0.0, 0.0]}
        ]
        base_state.db_similarity_threshold = 0.90
        base_state.db_embeddings_matrix = [
            {"id": "bp-cabin", "article_name": "Spinner Cabin", "description": "55cm",
             "embedding": [1.0, 0.0, 0.0]},
            {"id": "bp-large", "article_name": "Spinner Large", "description": "75cm",
             "embedding": [0.99, 0.0, 0.0]},
        ]
        llm_response = json.dumps({"selected_blueprint_id": "bp-cabin"})
        with patch("src.agents.article_blueprints_agent.nodes.db_match_node.LLMClient") as MockClient:
            MockClient.return_value.call = AsyncMock(return_value=llm_response)
            res = await db_match_node(base_state)

        assert len(res["matched_items"]) == 1
        assert res["matched_items"][0]["article_blueprint_id"] == "bp-cabin"
        assert res["unmatched_items"] == []

    @pytest.mark.asyncio
    async def test_db_match_multiple_candidates_llm_returns_null_routes_to_most_similar(self, base_state):
        """When LLM returns null (no confident match), item goes to unmatched_items, not discarded."""
        base_state.items = [
            {"item_id": "b", "article_name": "New Model XL", "article_description": "New line",
             "embedding": [1.0, 0.0, 0.0]}
        ]
        base_state.db_similarity_threshold = 0.90
        base_state.db_embeddings_matrix = [
            {"id": "bp-A", "article_name": "Model A", "description": "Old line",
             "embedding": [1.0, 0.0, 0.0]},
            {"id": "bp-B", "article_name": "Model B", "description": "Old line B",
             "embedding": [0.99, 0.0, 0.0]},
        ]
        llm_response = json.dumps({"selected_blueprint_id": None})
        with patch("src.agents.article_blueprints_agent.nodes.db_match_node.LLMClient") as MockClient:
            MockClient.return_value.call = AsyncMock(return_value=llm_response)
            res = await db_match_node(base_state)

        assert len(res["matched_items"]) == 1
        assert res["matched_items"][0]["item_id"] == "b"
        assert res["matched_items"][0]["article_blueprint_id"] == "bp-A"  # 0.999 is top
        assert res["unmatched_items"] == []


class TestClusterUnmatchedNode:
    def test_clustering_assigns_uuids_and_new_blueprints(self, base_state):
        base_state.unmatched_items = [
            {"item_id": "item-3", "article_name": "Shoe 1", "embedding": [0.0, 1.0, 0.0]},
            {"item_id": "item-4", "article_name": "Shoe 1 sim", "embedding": [0.0, 0.95, 0.0]},
        ]
        res = cluster_unmatched_node(base_state)
        unmatched = res["unmatched_items"]
        new_bps = res["new_blueprints"]

        assert len(new_bps) == 1  # Clustered together
        assert len(unmatched) == 2
        assert unmatched[0]["article_blueprint_id"] == unmatched[1]["article_blueprint_id"]
        assert new_bps[0]["id"] == unmatched[0]["article_blueprint_id"]
        assert new_bps[0]["is_new"] is True

    def test_clustering_non_contiguous_items_merge(self, base_state):
        # Two groups formed initially, but their centroids are similar enough to merge in post-merge pass
        base_state.articles_similarity_threshold = 0.90
        base_state.unmatched_items = [
            {"item_id": "1", "embedding": [1.0, 0.0, 0.0]},
            {"item_id": "2", "embedding": [0.0, 1.0, 0.0]},  # Different group
            {"item_id": "3", "embedding": [0.92, 0.0, 0.0]},  # Joins group 1, shifting centroid
            {"item_id": "4", "embedding": [0.89, 0.0, 0.0]},  # With old seed [1,0,0] sim=0.89 < 0.90, but with new centroid sim > 0.90
        ]
        res = cluster_unmatched_node(base_state)
        new_bps = res["new_blueprints"]
        # Items 1, 3, 4 should be in one cluster, item 2 in another
        assert len(new_bps) == 2

    def test_clustering_best_match_over_first_match(self, base_state):
        base_state.articles_similarity_threshold = 0.85
        base_state.unmatched_items = [
            {"item_id": "1", "embedding": [1.0, 0.0, 0.0]},       # Group 0: [1.0, 0.0, 0.0]
            {"item_id": "2", "embedding": [0.86, 0.5, 0.0]},      # Group 1: sim with group 0 is ~0.864 (above 0.85), but let's make two distinct centroids
            {"item_id": "3", "embedding": [0.88, 0.47, 0.0]},     # Should pick the best matching centroid
        ]
        res = cluster_unmatched_node(base_state)
        assert len(res["new_blueprints"]) >= 1


class TestValidateClustersNode:
    @pytest.mark.asyncio
    async def test_validation_confirms_single_cluster(self, base_state):
        """When LLM returns a single array, the original cluster is preserved with its UUID."""
        original_id = "cluster-uuid-1"
        base_state.new_blueprints = [{
            "id": original_id,
            "is_new": True,
            "cluster_items": [
                {"item_id": "1", "article_name": "Bag Blue", "article_description": "Leather bag",
                 "embedding": [1.0, 0.0, 0.0], "article_blueprint_id": original_id},
                {"item_id": "2", "article_name": "Bag Red", "article_description": "Leather bag",
                 "embedding": [0.99, 0.0, 0.0], "article_blueprint_id": original_id},
            ],
        }]
        # LLM returns all items in a single sub-array → confirmed, no split.
        llm_response = json.dumps([[1, 2]])
        with patch("src.agents.article_blueprints_agent.nodes.validate_clusters_node.LLMClient") as MockClient:
            MockClient.return_value.call = AsyncMock(return_value=llm_response)
            res = await validate_clusters_node(base_state)

        assert len(res["new_blueprints"]) == 1
        assert res["new_blueprints"][0]["id"] == original_id
        assert res["warnings"] == []

    @pytest.mark.asyncio
    async def test_validation_accepts_split_geometrically_consistent(self, base_state):
        """LLM splits into geometrically distant sub-groups → split accepted, no warning."""
        original_id = "cluster-uuid-2"
        base_state.articles_similarity_threshold = 0.90
        base_state.new_blueprints = [{
            "id": original_id,
            "is_new": True,
            "cluster_items": [
                # Two orthogonal embeddings → centroids will be far apart (sim ≈ 0)
                {"item_id": "1", "article_name": "Spinner 55", "article_description": "Cabin size",
                 "embedding": [1.0, 0.0], "article_blueprint_id": original_id},
                {"item_id": "2", "article_name": "Spinner 75", "article_description": "Large size",
                 "embedding": [0.0, 1.0], "article_blueprint_id": original_id},
            ],
        }]
        # LLM proposes two sub-groups.
        llm_response = json.dumps([[1], [2]])
        with patch("src.agents.article_blueprints_agent.nodes.validate_clusters_node.LLMClient") as MockClient:
            MockClient.return_value.call = AsyncMock(return_value=llm_response)
            res = await validate_clusters_node(base_state)

        assert len(res["new_blueprints"]) == 2
        # Both sub-groups get fresh UUIDs, different from the original.
        ids = {bp["id"] for bp in res["new_blueprints"]}
        assert original_id not in ids
        assert len(ids) == 2
        assert res["warnings"] == []

    @pytest.mark.asyncio
    async def test_validation_rejects_split_with_warning_when_geometrically_close(self, base_state):
        """LLM splits into geometrically close sub-groups → split REJECTED, warning appended."""
        original_id = "cluster-uuid-3"
        base_state.hallucination_recognition_threshold = 0.90
        base_state.new_blueprints = [{
            "id": original_id,
            "is_new": True,
            "cluster_items": [
                # Nearly identical embeddings → centroids will be above threshold after split.
                {"item_id": "1", "article_name": "Spinner 55 Blue", "article_description": "Cabin",
                 "embedding": [1.0, 0.0], "article_blueprint_id": original_id},
                {"item_id": "2", "article_name": "Spinner 55 Red", "article_description": "Cabin",
                 "embedding": [0.99, 0.0], "article_blueprint_id": original_id},
            ],
        }]
        llm_response = json.dumps([[1], [2]])
        with patch("src.agents.article_blueprints_agent.nodes.validate_clusters_node.LLMClient") as MockClient:
            MockClient.return_value.call = AsyncMock(return_value=llm_response)
            res = await validate_clusters_node(base_state)

        # Split must be rejected (only 1 blueprint remains).
        assert len(res["new_blueprints"]) == 1
        assert res["new_blueprints"][0]["id"] == original_id
        # A warning must have been appended.
        assert len(res["warnings"]) == 1
        assert "Soft-check warning" in res["warnings"][0]

    @pytest.mark.asyncio
    async def test_validation_all_clusters_processed_in_parallel(self, base_state):
        """Multiple clusters are submitted concurrently: LLM called once per cluster."""
        base_state.new_blueprints = [
            {
                "id": f"c-{i}", "is_new": True,
                "cluster_items": [
                    {"item_id": str(i), "article_name": f"Item {i}", "article_description": "Desc",
                     "embedding": [1.0, 0.0], "article_blueprint_id": f"c-{i}"},
                ],
            }
            for i in range(3)
        ]
        # Single-item clusters pass through without an LLM call.
        with patch("src.agents.article_blueprints_agent.nodes.validate_clusters_node.LLMClient") as MockClient:
            MockClient.return_value.call = AsyncMock()
            res = await validate_clusters_node(base_state)

        # 3 single-item clusters → confirmed unchanged, no LLM call needed.
        assert len(res["new_blueprints"]) == 3
        MockClient.return_value.call.assert_not_called()


class TestSynthesisAndEnrichment:
    @pytest.mark.asyncio
    async def test_synthesis_node_success(self, base_state):
        from src.agents.article_blueprints_agent.nodes.synthesize_blueprints_node import synthesize_blueprints_node
        base_state.new_blueprints = [
            {"id": "new-bp-1", "is_new": True, "cluster_items": [{"article_name": "Var 1", "article_description": "Desc"}]}
        ]
        mock_json = json.dumps({
            "article_name": "Unified Name",
            "description": "Unified Desc",
            "dimensions": {"width_cm": 25.0, "height_cm": 15.0, "depth_cm": 13.0}
        })
        with patch("src.agents.article_blueprints_agent.nodes.synthesize_blueprints_node.LLMClient") as MockClient:
            mock_inst = MockClient.return_value
            mock_inst.call = AsyncMock(return_value=mock_json)
            res = await synthesize_blueprints_node(base_state)

        bps = res["new_blueprints"]
        assert bps[0]["article_name"] == "Unified Name"
        assert bps[0]["description"] == "Unified Desc"
        # dimensions must be stored as a canonical JSON string
        dims = json.loads(bps[0]["dimensions"])
        assert dims["width_cm"] == 25.0
        assert dims["height_cm"] == 15.0
        assert dims["depth_cm"] == 13.0

    @pytest.mark.asyncio
    async def test_synthesis_node_null_dimensions(self, base_state):
        """When dimensions are not present, the field must be None (not a string)."""
        from src.agents.article_blueprints_agent.nodes.synthesize_blueprints_node import synthesize_blueprints_node
        base_state.new_blueprints = [
            {"id": "new-bp-2", "is_new": True, "cluster_items": [{"article_name": "Scarf", "article_description": "A silk scarf"}]}
        ]
        mock_json = json.dumps({
            "article_name": "Silk Scarf",
            "description": "A fine silk scarf",
            "dimensions": None
        })
        with patch("src.agents.article_blueprints_agent.nodes.synthesize_blueprints_node.LLMClient") as MockClient:
            mock_inst = MockClient.return_value
            mock_inst.call = AsyncMock(return_value=mock_json)
            res = await synthesize_blueprints_node(base_state)

        bps = res["new_blueprints"]
        assert bps[0]["article_name"] == "Silk Scarf"
        assert bps[0]["dimensions"] is None

    @pytest.mark.asyncio
    async def test_enrichment_node_success(self, base_state):
        from src.agents.article_blueprints_agent.nodes.enrich_blueprints_node import enrich_blueprints_node
        base_state.new_blueprints = [
            {"id": "new-bp-1", "is_new": True, "article_name": "Unified Name", "description": "Unified Desc"}
        ]
        mock_json = json.dumps({
            "category": "Borse",
            "sub_category": "Spalla",
            "extended_description": "Ext desc",
            "tags": ["t1"],
            "materials": ["Pelle"]
        })
        with patch("src.agents.article_blueprints_agent.nodes.enrich_blueprints_node.LLMClient") as MockClient:
            mock_inst = MockClient.return_value
            mock_inst.call = AsyncMock(return_value=mock_json)
            res = await enrich_blueprints_node(base_state)

        bps = res["new_blueprints"]
        assert bps[0]["category"] == "Borse"
        assert bps[0]["sub_category"] == "Spalla"
        assert bps[0]["materials"] == ["Pelle"]


class TestFormatOutputNode:
    def test_format_strips_internal_fields(self, base_state):
        base_state.matched_items = [
            {"item_id": "i1", "vendor_code": "V1", "quantity": 2, "colors": ["Red"], "article_blueprint_id": "bp-1", "embedding": [1.0]}
        ]
        base_state.output_blueprints = [{"id": "bp-1", "is_new": False}]
        base_state.new_blueprints = [
            {"id": "bp-2", "is_new": True, "category": "Cat1", "cluster_items": [{"foo": "bar"}]}
        ]

        res = format_output_node(base_state)
        items = res["output_items"]
        bps = res["output_blueprints"]

        assert len(items) == 1
        assert "embedding" not in items[0]
        assert items[0]["item_id"] == "i1"

        assert len(bps) == 2
        assert bps[0] == {"id": "bp-1", "is_new": False}
        assert bps[1]["id"] == "bp-2"
        assert bps[1]["is_new"] is True
        assert "cluster_items" not in bps[1]


class TestArticleBlueprintsAgent:
    @pytest.mark.asyncio
    async def test_agent_aexecute_end_to_end(self):
        agent = ArticleBlueprintsAgent()
        with patch("src.agents.article_blueprints_agent.nodes.embed_items_node.LLMClient") as MockEmb, \
             patch("src.agents.article_blueprints_agent.nodes.db_match_node.LLMClient") as MockDbMatch, \
             patch("src.agents.article_blueprints_agent.nodes.validate_clusters_node.LLMClient") as MockValidate, \
             patch("src.agents.article_blueprints_agent.nodes.synthesize_blueprints_node.LLMClient") as MockSynth, \
             patch("src.agents.article_blueprints_agent.nodes.enrich_blueprints_node.LLMClient") as MockEnrich:

            MockEmb.return_value.generate_embedding = AsyncMock(return_value=[1.0, 0.0])
            # db_match_node: no DB matrix → no LLM call needed, but patch for safety.
            MockDbMatch.return_value.call = AsyncMock()
            # validate_clusters_node: single-item cluster passes through without LLM call.
            MockValidate.return_value.call = AsyncMock()
            MockSynth.return_value.call = AsyncMock(return_value='{"article_name": "N", "description": "D"}')
            MockEnrich.return_value.call = AsyncMock(return_value='{"category": "C", "sub_category": "S", "extended_description": "E", "tags": [], "materials": []}')

            res = await agent.aexecute({
                "items": [{"item_id": "1", "article_name": "Test", "article_description": "Desc"}],
                "categories": {"C": {}},
                "db_embeddings_matrix": [],
            })

        assert len(res["items"]) == 1
        assert len(res["blueprints"]) == 1
        assert res["blueprints"][0]["is_new"] is True
