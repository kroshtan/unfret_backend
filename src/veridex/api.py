from typing import Any

import asyncpg
from fastapi import FastAPI
from langgraph.graph.state import CompiledStateGraph


class VeridexAPI(FastAPI):
    """Custom FastAPI application for the Unfret API."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the Unfret API application."""
        super().__init__(*args, **kwargs)
        self.graph: CompiledStateGraph | None = None  # set during lifespan; every skill (the deep tier)
        self.quick_graph: CompiledStateGraph | None = None  # set during lifespan; the x402 quick tier
        self.db_pool: asyncpg.Pool | None = None  # set during lifespan
        self.x402: Any = None  # veridex.x402.service.X402, created on first use
        self.credits: Any = None  # veridex.credits.CreditsDoor, created on first use
        self.fiat: Any = None  # a veridex.credits.fiat.FiatProvider (Polar) when configured; fiat is off while None
