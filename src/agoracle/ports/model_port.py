"""
Model Port — interface for calling any AI model.

Each call is stateless: no shared conversation history between calls.
This is the architectural guarantee of role isolation.
"""

from __future__ import annotations

from typing import AsyncIterator, Protocol

from agoracle.domain.types import ModelResponse, RoleCall


class ModelPort(Protocol):
    """
    Port for making model API calls.

    Contract:
      - Each call() is an independent HTTP request
      - No conversation state maintained between calls
      - No context shared between different RoleCalls
    """

    async def call(self, role_call: RoleCall) -> ModelResponse:
        """Make a non-streaming model call. Returns complete response."""
        ...

    async def call_stream(self, role_call: RoleCall) -> AsyncIterator[str]:
        """Make a streaming model call. Yields text chunks."""
        ...

    def supports_model(self, model_id: str) -> bool:
        """Check if this adapter can handle the given model_id."""
        ...
