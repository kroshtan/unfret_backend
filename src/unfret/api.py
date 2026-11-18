from typing import Any

import asyncpg
from fastapi import FastAPI
from langgraph.graph.state import CompiledStateGraph


class UnfretAPI(FastAPI):
    """Custom FastAPI application for the Unfret API."""

    def __init__(self, *args: Any, openapi_extensions: dict[str, Any] | None = None, **kwargs: Any) -> None:
        """
        Initialize the Unfret API application.

        :param openapi_extensions: Top-level ``x-…`` fields of the OpenAPI document.
        """
        super().__init__(*args, **kwargs)
        self.openapi_extensions = openapi_extensions or {}
        self.graph: CompiledStateGraph | None = None  # set during lifespan; every skill (the deep tier)
        self.quick_graph: CompiledStateGraph | None = None  # set during lifespan; the x402 quick tier
        self.db_pool: asyncpg.Pool | None = None  # set during lifespan
        self.x402: Any = None  # unfret.x402.service.X402, created on first use
        self.x402_base: Any = None  # unfret.x402.base.BaseX402, created on first use
        self.credits: Any = None  # unfret.credits.CreditsDoor, created on first use
        self.fiat: Any = None  # a unfret.credits.fiat.FiatProvider (Polar) when configured; fiat is off while None

    def openapi(self) -> dict[str, Any]:
        """
        The OpenAPI document, with the top-level extensions.

        :return: The document (built once).
        """
        if self.openapi_schema is None:
            super().openapi().update(self.openapi_extensions)
        return self.openapi_schema or {}
