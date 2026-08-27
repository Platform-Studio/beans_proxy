"""OpenRouter model pricing fetched and normalized at proxy startup."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx


OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
UNKNOWN_MODEL_ERROR = "unknown model/no price"


@dataclass(frozen=True)
class ModelPricing:
    input_price: Decimal
    output_price: Decimal


class PricingCatalog:
    """Small in-memory lookup of model IDs/slugs to token prices."""

    def __init__(self) -> None:
        self._prices: dict[str, ModelPricing] = {}
        self._model_count = 0

    async def load(self, client: httpx.AsyncClient | None = None) -> None:
        """Fetch and normalize the OpenRouter model list.

        A failed fetch is intentionally non-fatal. The caller can continue
        recording token usage, but calls will receive the no-price error.
        """
        own_client = client is None
        if own_client:
            client = httpx.AsyncClient(timeout=30.0)
        try:
            response = await client.get(OPENROUTER_MODELS_URL)
            response.raise_for_status()
            payload = response.json()
            self._prices, self._model_count = _parse_model_prices_with_count(payload)
            logging.getLogger("uvicorn.error").info(
                "Pricing loaded for %d models", self._model_count
            )
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            logging.getLogger("beans_proxy.pricing").warning(
                "could not load OpenRouter pricing: %s", exc
            )
            self._prices = {}
            self._model_count = 0
        finally:
            if own_client:
                await client.aclose()

    def get(self, model: str | None) -> ModelPricing | None:
        if not model:
            return None
        return self._prices.get(model)


def _parse_model_prices(payload: Any) -> dict[str, ModelPricing]:
    prices, _ = _parse_model_prices_with_count(payload)
    return prices


def _parse_model_prices_with_count(
    payload: Any,
) -> tuple[dict[str, ModelPricing], int]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("OpenRouter pricing response has no data array")

    prices: dict[str, ModelPricing] = {}
    model_count = 0
    for model in payload["data"]:
        if not isinstance(model, dict):
            continue
        pricing = model.get("pricing")
        if not isinstance(pricing, dict):
            continue
        parsed = _parse_pricing(pricing)
        if parsed is None:
            continue
        keys = (model.get("id"), model.get("canonical_slug"))
        valid_keys = [key for key in keys if isinstance(key, str) and key]
        if not valid_keys:
            continue
        model_count += 1
        for key in valid_keys:
            prices[key] = parsed
    return prices, model_count


def _parse_pricing(pricing: dict[str, Any]) -> ModelPricing | None:
    try:
        input_price = Decimal(str(pricing["prompt"]))
        output_price = Decimal(str(pricing["completion"]))
    except (KeyError, InvalidOperation, TypeError, ValueError):
        return None
    if input_price < 0 or output_price < 0:
        return None
    return ModelPricing(input_price, output_price)