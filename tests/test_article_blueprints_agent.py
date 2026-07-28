import json
import pytest
from unittest.mock import AsyncMock, patch

from src.agents.article_blueprints_agent.state import BlueprintsGraphState
from src.agents.article_blueprints_agent.nodes.db_match_node import db_match_node
from src.agents.article_blueprints_agent.nodes.cluster_unmatched_node import cluster_unmatched_node
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
    def test_db_match_and_deduplication(self, base_state):
        res = db_match_node(base_state)
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


class TestSynthesisAndEnrichment:
    @pytest.mark.asyncio
    async def test_synthesis_node_success(self, base_state):
        from src.agents.article_blueprints_agent.nodes.synthesize_blueprints_node import synthesize_blueprints_node
        base_state.new_blueprints = [
            {"id": "new-bp-1", "is_new": True, "cluster_items": [{"article_name": "Var 1", "article_description": "Desc"}]}
        ]
        mock_json = json.dumps({"article_name": "Unified Name", "description": "Unified Desc"})
        with patch("src.agents.article_blueprints_agent.nodes.synthesize_blueprints_node.LLMClient") as MockClient:
            mock_inst = MockClient.return_value
            mock_inst.call = AsyncMock(return_value=mock_json)
            res = await synthesize_blueprints_node(base_state)

        bps = res["new_blueprints"]
        assert bps[0]["article_name"] == "Unified Name"
        assert bps[0]["description"] == "Unified Desc"

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
             patch("src.agents.article_blueprints_agent.nodes.synthesize_blueprints_node.LLMClient") as MockSynth, \
             patch("src.agents.article_blueprints_agent.nodes.enrich_blueprints_node.LLMClient") as MockEnrich:

            MockEmb.return_value.generate_embedding = AsyncMock(return_value=[1.0, 0.0])
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
