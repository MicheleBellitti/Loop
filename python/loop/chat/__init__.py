"""The assistant that answers questions about this pipeline.

Assembled from maintained libraries rather than rewrites (decisions.md LIB-1),
with four thin modules of our own around them:

    llama.py   the model server through the openai SDK — llama.cpp serves it
    tools.py   what the model may do, as LangChain StructuredTools over our
               handlers: applications, statistics, the mail, the backfill
    agent.py   LangGraph's agent loop, translated to the five events the
               panel renders and the store persists
    store.py   conversations and messages, in Postgres, under the same RLS

The agent takes its model as a `BaseChatModel`, which is what lets the whole
loop run in a test against a scripted model with no server and no network.
"""

from .agent import (
    AgentEvent,
    ErrorEvent,
    FinalEvent,
    TokenEvent,
    ToolEndEvent,
    ToolStartEvent,
    chat_model,
    run_agent,
)
from .llama import LlamaClient, LlamaError
from .tools import Tool, ToolContext, ToolResult, default_tools, langchain_tools

__all__ = [
    "AgentEvent",
    "ErrorEvent",
    "FinalEvent",
    "LlamaClient",
    "LlamaError",
    "TokenEvent",
    "Tool",
    "ToolContext",
    "ToolEndEvent",
    "ToolResult",
    "ToolStartEvent",
    "chat_model",
    "default_tools",
    "langchain_tools",
    "run_agent",
]
