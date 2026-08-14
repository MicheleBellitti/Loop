"""The assistant that answers questions about this pipeline.

Four modules, layered so the testable parts stay pure-ish:

    llama.py   the llama.cpp wire client — OpenAI chat completions, streamed
    tools.py   what the model may do: read applications, statistics, the mail
    agent.py   the loop: model → tool calls → model, until there is an answer
    store.py   conversations and messages, in Postgres, under the same RLS

The agent takes its model through a protocol and its tools as values, which is
what lets the whole loop run in a test against a scripted model with no server,
no database and no network.
"""

from .agent import (
    AgentEvent,
    ErrorEvent,
    FinalEvent,
    TokenEvent,
    ToolEndEvent,
    ToolStartEvent,
    run_agent,
)
from .llama import Completion, LlamaClient, LlamaError, TokenDelta, ToolCall
from .tools import Tool, ToolContext, ToolResult, default_tools

__all__ = [
    "AgentEvent",
    "Completion",
    "ErrorEvent",
    "FinalEvent",
    "LlamaClient",
    "LlamaError",
    "TokenDelta",
    "TokenEvent",
    "Tool",
    "ToolCall",
    "ToolContext",
    "ToolEndEvent",
    "ToolResult",
    "ToolStartEvent",
    "default_tools",
    "run_agent",
]
