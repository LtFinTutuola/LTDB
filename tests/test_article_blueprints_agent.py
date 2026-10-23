import pytest
import asyncio
from src.agents.article_blueprints_agent.state import BlueprintsGraphState
from src.agents.article_blueprints_agent.nodes.synthesize_blueprints_node import synthesize_blueprints_node
from src.agents.article_blueprints_agent.nodes.enrich_blueprints_node import enrich_blueprints_node
from src.agents.article_blueprints_agent.nodes.format_output_node import format_output_node
from src.agents.article_blueprints_agent.nodes.web_search_blueprints_node import web_search_blueprints_node
from src.agents.article_blueprints_agent.agent import ArticleBlueprintsAgent
from unittest.mock import patch, AsyncMock

@pytest.fixture
def base_state():
    return BlueprintsGraphState(
        new_blueprints=[
            {
                "id": "bp-1",
                "is_new": True,
                "cluster_items": [
                    {"vendor_code": "A1", "article_name": "Test Art 1"},
                    {"vendor_code": "A2", "article_name": "Test Art 2"}
                ]
            }
        ],
        categories={"Cat": {"description": "desc", "sub_categories": {}}},
        brand_name="TestBrand"
    )

@pytest.mark.asyncio
async def test_web_search_blueprints_node_success(base_state):
    mock_raw = "<NAME>Official Name</NAME><DESCRIPTION>Official Desc</DESCRIPTION>"
    
    with patch("src.agents.article_blueprints_agent.nodes.web_search_blueprints_node.LLMClient") as MockClient:
        mock_inst = MockClient.return_value
        mock_inst.call_with_grounding = AsyncMock(return_value=(mock_raw, ["http://example.com"]))
        
        res = await web_search_blueprints_node(base_state)
        
        mock_inst.call_with_grounding.assert_called_once()
        
    bps = res["new_blueprints"]
    assert len(bps) == 1
    for item in bps[0]["cluster_items"]:
        assert item["article_name"] == "Official Name"
        assert item["article_description"] == "Official Desc"

@pytest.mark.asyncio
async def test_web_search_blueprints_node_fallback(base_state):
    # description is missing in base_state cluster_items, let's just assert fallback to vendor code
    with patch("src.agents.article_blueprints_agent.nodes.web_search_blueprints_node.LLMClient") as MockClient:
        mock_inst = MockClient.return_value
        mock_inst.call_with_grounding = AsyncMock(side_effect=Exception("Timeout"))
        
        res = await web_search_blueprints_node(base_state)
        
    bps = res["new_blueprints"]
    assert len(res["warnings"]) == 1
    for item in bps[0]["cluster_items"]:
        # fallback is description (or article_name) if exists, else f"{brand} {vendor_code}".strip()
        # vendor_code is A1, brand is TestBrand, article_name is "Test Art 1"
        # so fallback name is "Test Art 1"
        assert item["article_name"] == "Test Art 1"
        assert item["article_description"] == "Test Art 1"

@pytest.mark.asyncio
async def test_synthesize_blueprints_node(base_state, monkeypatch):
    async def mock_call(*args, **kwargs):
        return '{"article_name": "Synth Name", "description": "Synth Desc", "dimensions": {"width_cm": 10, "height_cm": 20, "depth_cm": 30}}'
    monkeypatch.setattr("src.agents.article_blueprints_agent.nodes.synthesize_blueprints_node.LLMClient.call", mock_call)
    
    res = await synthesize_blueprints_node(base_state)
    assert len(res["new_blueprints"]) == 1
    bp = res["new_blueprints"][0]
    assert bp["article_name"] == "Synth Name"
    assert bp["description"] == "Synth Desc"
    assert '"width_cm":10.0' in bp["dimensions"]

@pytest.mark.asyncio
async def test_enrich_blueprints_node(base_state, monkeypatch):
    base_state.new_blueprints[0]["article_name"] = "Synth Name"
    base_state.new_blueprints[0]["description"] = "Synth Desc"
    
    async def mock_call(*args, **kwargs):
        return '{"category": "Cat", "sub_category": "Sub", "extended_description": "Ext Desc", "tags": ["tag1"], "materials": ["mat1"]}'
    monkeypatch.setattr("src.agents.article_blueprints_agent.nodes.enrich_blueprints_node.LLMClient.call", mock_call)
    
    res = await enrich_blueprints_node(base_state)
    bp = res["new_blueprints"][0]
    assert bp["category"] == "Cat"
    assert bp["extended_description"] == "Ext Desc"

def test_format_output_node(base_state):
    base_state.new_blueprints[0]["article_name"] = "Name"
    base_state.new_blueprints[0]["description"] = "Desc"
    
    res = format_output_node(base_state)
    assert len(res["output_blueprints"]) == 1
    assert res["output_blueprints"][0]["article_name"] == "Name"
    
    assert len(res["output_items"]) == 2
    assert res["output_items"][0]["article_blueprint_id"] == "bp-1"
    assert res["output_items"][0]["vendor_code"] == "A1"

@pytest.mark.asyncio
async def test_agent_end_to_end(monkeypatch):
    async def mock_llm_call(*args, **kwargs):
        stage = kwargs.get("pipeline_stage", "")
        if "Synthesis" in stage:
            return '{"article_name": "Synth Name", "description": "Synth Desc", "dimensions": null}'
        else:
            return '{"category": "Cat", "sub_category": "Sub", "extended_description": "Ext Desc", "tags": [], "materials": []}'
            
    monkeypatch.setattr("src.agents.llm_client.LLMClient.call", mock_llm_call)
    
    agent = ArticleBlueprintsAgent()
    res = await agent.aexecute({
        "new_blueprints": [
            {"id": "bp-x", "is_new": True, "cluster_items": [{"vendor_code": "V1"}]}
        ],
        "categories": {}
    })
    
    assert len(res["items"]) == 1
    assert res["items"][0]["article_blueprint_id"] == "bp-x"
    assert len(res["blueprints"]) == 1
    assert res["blueprints"][0]["article_name"] == "Synth Name"

@pytest.mark.asyncio
async def test_synthesize_blueprints_node_uses_article_description(monkeypatch):
    captured_prompt = {}

    async def mock_call(*args, **kwargs):
        captured_prompt["prompt"] = kwargs.get("prompt", "")
        return '{"article_name": "Portafoglio The Bridge", "description": "Portafoglio compatto in pelle", "dimensions": null}'

    monkeypatch.setattr("src.agents.article_blueprints_agent.nodes.synthesize_blueprints_node.LLMClient.call", mock_call)

    state = BlueprintsGraphState(
        new_blueprints=[
            {
                "id": "bp-wallet",
                "is_new": True,
                "cluster_items": [
                    {
                        "vendor_code": "01423401",
                        "article_name": "The Bridge Story Donna",
                        "article_description": "Portafoglio da donna in pelle con patta e scomparti per carte",
                    }
                ],
            }
        ],
        categories={},
        brand_name="The Bridge",
    )

    res = await synthesize_blueprints_node(state)
    assert len(res["new_blueprints"]) == 1
    assert "Portafoglio da donna in pelle con patta e scomparti per carte" in captured_prompt["prompt"]
    assert res["new_blueprints"][0]["description"] == "Portafoglio compatto in pelle"

@pytest.mark.asyncio
async def test_web_search_to_synthesize_pipeline_preserves_description(monkeypatch):
    mock_raw = "<NAME>The Bridge Story Donna</NAME><DESCRIPTION>Portafoglio da donna in cuoio con chiusura a patta.</DESCRIPTION>"

    with patch("src.agents.article_blueprints_agent.nodes.web_search_blueprints_node.LLMClient") as MockClient:
        mock_inst = MockClient.return_value
        mock_inst.call_with_grounding = AsyncMock(return_value=(mock_raw, ["http://thebridge.it"]))

        initial_state = BlueprintsGraphState(
            new_blueprints=[
                {
                    "id": "bp-tb-1",
                    "is_new": True,
                    "cluster_items": [
                        {"vendor_code": "01423401", "article_name": "The Bridge 01423401"}
                    ],
                }
            ],
            categories={},
            brand_name="The Bridge",
        )

        search_res = await web_search_blueprints_node(initial_state)

    # Verify both article_description and description are populated on cluster items
    cluster_items = search_res["new_blueprints"][0]["cluster_items"]
    assert cluster_items[0]["article_description"] == "Portafoglio da donna in cuoio con chiusura a patta."
    assert cluster_items[0]["description"] == "Portafoglio da donna in cuoio con chiusura a patta."

    # Now pass output to synthesize_blueprints_node
    captured_prompt = {}

    async def mock_synth_call(*args, **kwargs):
        captured_prompt["prompt"] = kwargs.get("prompt", "")
        return '{"article_name": "The Bridge Story Donna", "description": "Portafoglio in cuoio con patta", "dimensions": null}'

    monkeypatch.setattr("src.agents.article_blueprints_agent.nodes.synthesize_blueprints_node.LLMClient.call", mock_synth_call)

    synth_state = BlueprintsGraphState(
        new_blueprints=search_res["new_blueprints"],
        categories={},
        brand_name="The Bridge",
    )
    synth_res = await synthesize_blueprints_node(synth_state)
    assert len(synth_res["new_blueprints"]) == 1
    assert "Portafoglio da donna in cuoio con chiusura a patta." in captured_prompt["prompt"]
    assert synth_res["new_blueprints"][0]["description"] == "Portafoglio in cuoio con patta"

