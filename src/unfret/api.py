from typing import Any

import asyncpg
from fastapi import FastAPI
from langgraph.graph.state import CompiledStateGraph

from unfret.config import settings
from unfret.x402.ownership import OPENAPI_KEYS, ownership_proofs


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

    def ownership_proofs(self) -> list[str]:
        """
        The public origin signed by each ``payTo`` key the payment services hold (unfret.x402.ownership).

        :return: The proofs; none while x402 is off.
        """
        return ownership_proofs(settings.public_base_url, (self.x402, self.x402_base))

    def openapi(self) -> dict[str, Any]:
        """
        The OpenAPI document, with the top-level extensions and the ownership proofs x402scan reads.

        The extensions are the ``openapi_extensions`` given at construction (``x-agentcash-guidance``); the proofs go
        under ``x-agentcash-provenance`` and ``x-discovery``.

        :return: The document (built once; the proofs follow the payment services' keys).
        """
        schema = super().openapi()
        schema.update(self.openapi_extensions)
        proofs = self.ownership_proofs()
        for key in OPENAPI_KEYS:
            if proofs:
                schema[key] = {"ownershipProofs": proofs}
            else:
                schema.pop(key, None)
        return schema
