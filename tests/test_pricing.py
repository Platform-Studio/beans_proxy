"""Tests for OpenRouter pricing normalization and cost recording."""

from __future__ import annotations

import logging
from decimal import Decimal

import httpx

from beans_proxy.app import _record_for_result
from beans_proxy.forwarder import ForwardResult
from beans_proxy.pricing import ModelPricing, PricingCatalog, _parse_model_prices


def test_parse_model_prices_indexes_id_and_canonical_slug():
    prices = _parse_model_prices(
        {
            "data": [
                {
                    "id": "openai/gpt-5.4",
                    "canonical_slug": "openai/gpt-5.4-20260305",
                    "pricing": {"prompt": "0.0000025", "completion": "0.000015"},
                    "description": "discarded",
                }
            ]
        }
    )

    assert prices["openai/gpt-5.4"] == ModelPricing(
        Decimal("0.0000025"), Decimal("0.000015")
    )
    assert prices["openai/gpt-5.4-20260305"] == prices["openai/gpt-5.4"]


def test_parse_model_prices_ignores_unavailable_and_invalid_prices():
    prices = _parse_model_prices(
        {
            "data": [
                {
                    "id": "openrouter/auto",
                    "pricing": {"prompt": "-1", "completion": "-1"},
                },
                {
                    "id": "missing/completion",
                    "pricing": {"prompt": "1"},
                },
            ]
        }
    )

    assert prices == {}


async def test_load_fetches_fixed_openrouter_url(monkeypatch):
    logged_messages: list[str] = []
    monkeypatch.setattr(
        logging.getLogger("uvicorn.error"),
        "info",
        lambda message, *args: logged_messages.append(message % args),
    )
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "model-a",
                        "canonical_slug": "model-a-2026",
                        "pricing": {"prompt": "0.1", "completion": "0.2"},
                    }
                ]
            },
        )

    catalog = PricingCatalog()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await catalog.load(client)

    assert requested_urls == ["https://openrouter.ai/api/v1/models"]
    assert catalog.get("model-a-2026") == ModelPricing(Decimal("0.1"), Decimal("0.2"))
    assert logged_messages == ["Pricing loaded for 1 models"]


def test_record_uses_response_model_and_calculates_decimal_prices():
    catalog = PricingCatalog()
    catalog._prices["openai/gpt-5.4-20260305"] = ModelPricing(
        Decimal("0.0000025"), Decimal("0.000015")
    )
    record = _record_for_result(
        ForwardResult(
            status_code=200,
            usage={"input_tokens": 12, "output_tokens": 34},
            model="openai/gpt-5.4-20260305",
        ),
        "start",
        "end",
        request_model="openai/gpt-5.4",
        pricing=catalog,
    )

    assert record["input_cost"] == 0.00003
    assert record["output_cost"] == 0.00051
    assert record["total_cost"] == 0.00054
    assert record["currency"] == "USD"
    assert "error" not in record


def test_record_falls_back_to_request_model_for_pricing():
    catalog = PricingCatalog()
    catalog._prices["model-a"] = ModelPricing(Decimal("0.1"), Decimal("0.2"))
    record = _record_for_result(
        ForwardResult(status_code=200, usage={"input_tokens": 2, "output_tokens": 3}),
        "start",
        "end",
        request_model="model-a",
        pricing=catalog,
    )

    assert record["total_cost"] == 0.8


def test_record_marks_missing_price_without_cost_fields():
    record = _record_for_result(
        ForwardResult(status_code=200, usage={"input_tokens": 1, "output_tokens": 2}),
        "start",
        "end",
        request_model="unknown/model",
        pricing=PricingCatalog(),
    )

    assert record["error"] == "unknown model/no price"
    assert "input_cost" not in record
    assert "output_cost" not in record
    assert "total_cost" not in record