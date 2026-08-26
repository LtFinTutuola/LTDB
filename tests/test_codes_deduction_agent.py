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
            return '{"pattern_found": true, "analysis": "Prefix separated by dash", "components": [{"name": "model", "type": "alphanumeric"}], "examples": []}'
        return '{"regex_pattern": "^(?P<model_code>[A-Z0-9]+)-.*$", "explanation": "Extracted model"}'

    monkeypatch.setattr("src.agents.llm_client.LLMClient.call", mock_analysis)
    
    # Run the agent
    res = await agent.aexecute({"brand_name": "TestBrand", "vendor_codes": ["A123-RED", "B456-BLU"]})
    assert res["status"] == "success"
    assert "regex_pattern" in res
    assert res["regex_pattern"] == "^(?P<model_code>[A-Z0-9]+)-.*$"

@pytest.mark.asyncio
async def test_codes_deduction_agent_regex_validation_failure(monkeypatch, agent):
    call_count = 0
    
    async def mock_analysis(*args, **kwargs):
        nonlocal call_count
        stage = kwargs.get("pipeline_stage", "")
        if "Pattern Analysis" in stage:
            return '{"pattern_found": true, "analysis": "ok", "components": [], "examples": []}'
        if "Regex Synthesis" in stage:
            call_count += 1
            # Return invalid regex (unbalanced parenthesis)
            return '{"regex_pattern": "^(?P<model_code>[A-Z0-9]+$", "explanation": "Bad regex"}'
            
    monkeypatch.setattr("src.agents.llm_client.LLMClient.call", mock_analysis)
    
    # Run the agent
    res = await agent.aexecute({"brand_name": "TestBrand", "vendor_codes": ["A123-RED", "B456-BLU"]})
    assert res["status"] == "failed"
    # Agent should have retried up to 3 times
    assert call_count == 3
