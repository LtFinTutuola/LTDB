import pytest
from src.agents.codes_deduction_agent.agent import CodesDeductionAgent
from src.agents.codes_deduction_agent.state import CodesDeductionGraphState

@pytest.fixture
def agent():
    return CodesDeductionAgent()

@pytest.mark.asyncio
async def test_codes_deduction_agent_success(monkeypatch, agent):
    # Mock pattern analysis
    async def mock_analysis(*args, **kwargs):
        stage = kwargs.get("pipeline_stage", "")
        if "Pattern Analysis" in stage:
            return '{"pattern_description": "Mask color after hyphen", "color_code_position": "after hyphen", "needs_masking": true}'
        if "Regex Synthesis" in stage:
            return '{"regex": "-(?P<color_code>[A-Z]+)$", "explanation": "Masked color"}'
            
    monkeypatch.setattr("src.agents.llm_client.LLMClient.call", mock_analysis)
    
    # Run the agent
    res = await agent.aexecute({
        "brand_name": "TestBrand", 
        "vendor_codes": ["A123-RED", "B456-BLU"],
        "context_items": [
            {"vendor_code": "A123-RED", "colors": ["RED"], "description": "Shoe red size 42"},
            {"vendor_code": "B456-BLU", "colors": ["BLU"], "description": "Hat blue"}
        ]
    })
    assert "regex" in res
    assert res["regex"] == "-(?P<color_code>[A-Z]+)$"

@pytest.mark.asyncio
async def test_codes_deduction_agent_regex_validation_failure(monkeypatch, agent):
    call_count = 0
    
    async def mock_analysis(*args, **kwargs):
        nonlocal call_count
        stage = kwargs.get("pipeline_stage", "")
        if "Pattern Analysis" in stage:
            return '{"pattern_description": "Mask color after hyphen", "color_code_position": "after hyphen", "needs_masking": true}'
        if "Regex Synthesis" in stage:
            call_count += 1
            # Return invalid regex (unbalanced parenthesis)
            return '{"regex": "-(?P<color_code>[A-Z]+$", "explanation": "Bad regex"}'
            
    monkeypatch.setattr("src.agents.llm_client.LLMClient.call", mock_analysis)
    
    from src.agents.base import AgentException
    with pytest.raises(AgentException):
        await agent.aexecute({"brand_name": "TestBrand", "vendor_codes": ["A123-RED", "B456-BLU"]})
    # Agent should have retried up to 3 times
    assert call_count == 3
