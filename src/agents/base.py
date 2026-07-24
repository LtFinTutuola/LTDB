"""
src/agents/base.py
------------------
Foundational building blocks for the agent layer:
  - AgentException: custom exception for agent-level failures.
  - BaseAgent: abstract contract that all agents must implement.
"""
from abc import ABC, abstractmethod
from typing import Optional


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

    @abstractmethod
    async def aexecute(self, input_data: dict) -> dict:
        """
        Run the agent asynchronously.

        Args:
            input_data: A dictionary containing all runtime parameters
                        required by the concrete agent implementation.

        Returns:
            A dictionary containing the agent's output.

        Raises:
            AgentException: On any fatal, unrecoverable error.
        """
        ...
