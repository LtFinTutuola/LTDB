"""
src/agents/base.py
------------------
Foundational building blocks for the agent layer:
  - AgentException: custom exception for agent-level failures.
  - BaseAgent: abstract contract that all agents must implement.
"""
from abc import ABC, abstractmethod
from typing import Optional
import time
from src.core.logger import get_logger

logger = get_logger()


class AgentException(Exception):
    """
    Raised when an agent encounters a fatal error during execution.

    Attributes:
        message: Human-readable description of the failure.
        output:  Optional partial result payload (e.g., items already extracted
                 before the failure occurred). Stored by the service layer into
                 the StagingArea.data column for diagnostic purposes.
    """

    def __init__(self, message: str, output: Optional[dict] = None) -> None:
        super().__init__(message)
        self.output = output


class BaseAgent(ABC):
    """
    Abstract base class for all LangGraph-backed agents.

    Every concrete agent must implement `aexecute`, which receives a
    generic input dictionary and returns a result dictionary. This keeps
    the interface agent-agnostic at the service level.
    """

    async def aexecute(self, input_data: dict) -> dict:
        """
        Run the agent asynchronously with automatic logging.
        """
        agent_name = self.__class__.__name__
        logger.log_agent(agent_name, "agent_started", "ok", input_data=input_data)
        start_time = time.time()
        
        try:
            result = await self._aexecute(input_data)
            elapsed_ms = int((time.time() - start_time) * 1000)
            logger.log_agent(agent_name, "agent_finished", "ok", output=result, latency_ms=elapsed_ms)
            return result
        except AgentException as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            logger.log_agent(agent_name, "agent_failed", "err", exc=e, output=e.output, latency_ms=elapsed_ms)
            raise e
        except Exception as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            logger.log_agent(agent_name, "agent_failed", "err", exc=e, latency_ms=elapsed_ms)
            raise AgentException(f"Unexpected error in {agent_name}: {e}") from e

    @abstractmethod
    async def _aexecute(self, input_data: dict) -> dict:
        """
        Actual agent implementation to be overridden by subclasses.
        """
        ...
