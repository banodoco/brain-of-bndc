"""Tests for grant pricing: live GPU rate refresh + dynamic grant cap."""

import asyncio
import json

import pytest

from src.features.grants import pricing

# Snapshot of the RunPod pricing-page JSON-LD shape (name/lowPrice/highPrice),
# restricted to the entries relevant to the grant GPU map plus decoys.
_PRODUCTS = [
    {"@type": "Product", "name": "B300 GPU on Runpod", "offers": {"lowPrice": "6.94", "highPrice": "7.89"}},
    {"@type": "Product", "name": "H200 GPU on Runpod", "offers": {"lowPrice": "3.59", "highPrice": "4.59"}},
    {"@type": "Product", "name": "B200 GPU on Runpod", "offers": {"lowPrice": "5.98", "highPrice": "6.79"}},
    {"@type": "Product", "name": "H100 NVL GPU on Runpod", "offers": {"lowPrice": "2.59", "highPrice": "3.19"}},
    {"@type": "Product", "name": "H100 PCIe GPU on Runpod", "offers": {"lowPrice": "1.99", "highPrice": "2.89"}},
    {"@type": "Product", "name": "H100 SXM GPU on Runpod", "offers": {"lowPrice": "2.69", "highPrice": "3.29"}},
    {"@type": "Product", "name": "A100 SXM GPU on Runpod", "offers": {"lowPrice": "1.39", "highPrice": "1.59"}},
    {"@type": "Product", "name": "RTX 4090 GPU on Runpod", "offers": {"lowPrice": "0.34", "highPrice": "0.74"}},
]


def _pricing_html(products=None) -> str:
    graph = products if products is not None else _PRODUCTS
    payload = json.dumps({"@context": "https://schema.org", "@graph": graph})
    return f"<html><body><script type=\"application/ld+json\">{payload}</script></body></html>"


@pytest.fixture(autouse=True)
def _restore_gpu_rates():
    """Keep module-level GPU_RATES pristine across tests."""
    original = dict(pricing.GPU_RATES)
    yield
    pricing.GPU_RATES.clear()
    pricing.GPU_RATES.update(original)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_parse_runpod_community_tier():
    rates = pricing._parse_runpod_pricing_html(_pricing_html(), tier='community')
    assert rates == {'H100_80GB': 2.69, 'H200': 3.59, 'B200': 5.98}


def test_parse_runpod_secure_tier():
    rates = pricing._parse_runpod_pricing_html(_pricing_html(), tier='secure')
    assert rates == {'H100_80GB': 3.29, 'H200': 4.59, 'B200': 6.79}


def test_parse_unknown_tier_raises():
    with pytest.raises(ValueError):
        pricing._parse_runpod_pricing_html(_pricing_html(), tier='spot')


def test_parse_missing_gpu_raises():
    products = [p for p in _PRODUCTS if 'H200' not in p['name']]
    with pytest.raises(ValueError, match='H200'):
        pricing._parse_runpod_pricing_html(_pricing_html(products))


def test_parse_empty_page_raises():
    with pytest.raises(ValueError, match='no JSON-LD Product'):
        pricing._parse_runpod_pricing_html('<html>no data</html>')


def test_parse_out_of_bounds_rate_raises():
    products = []
    for p in _PRODUCTS:
        p = dict(p)
        if 'H100 SXM' in p['name']:
            p = {"@type": "Product", "name": p['name'], "offers": {"lowPrice": "999.00", "highPrice": "3.29"}}
        products.append(p)
    with pytest.raises(ValueError, match='out of bounds'):
        pricing._parse_runpod_pricing_html(_pricing_html(products))


# ---------------------------------------------------------------------------
# Applying rates
# ---------------------------------------------------------------------------

def test_apply_gpu_rates_updates_and_reports_changes():
    changes = pricing.apply_gpu_rates({'H100_80GB': 2.69, 'H200': 3.59, 'B200': 5.98})
    assert pricing.GPU_RATES == {'H100_80GB': 2.69, 'H200': 3.59, 'B200': 5.98}
    assert changes == {'H100_80GB': (2.50, 2.69), 'H200': (3.50, 3.59), 'B200': (5.00, 5.98)}


def test_apply_gpu_rates_no_change_reports_empty():
    changes = pricing.apply_gpu_rates({'H100_80GB': 2.50, 'H200': 3.50, 'B200': 5.00})
    assert changes == {}


def test_apply_gpu_rates_ignores_unknown_keys():
    changes = pricing.apply_gpu_rates({'H100_80GB': 2.69, 'RTX 4090': 0.34})
    assert changes == {'H100_80GB': (2.50, 2.69)}
    assert 'RTX 4090' not in pricing.GPU_RATES


# ---------------------------------------------------------------------------
# Dynamic downstream values
# ---------------------------------------------------------------------------

def test_max_grant_usd_follows_h100_rate():
    assert pricing.max_grant_usd() == pytest.approx(50 * 2.50 * 1.1, abs=0.01)
    pricing.apply_gpu_rates({'H100_80GB': 2.69})
    assert pricing.max_grant_usd() == pytest.approx(50 * 2.69 * 1.1, abs=0.01)


def test_calculate_grant_cost_uses_refreshed_rate():
    pricing.apply_gpu_rates({'H200': 3.59})
    assert pricing.calculate_grant_cost('H200', 20) == pytest.approx(20 * 3.59 * 1.1, abs=0.01)


# ---------------------------------------------------------------------------
# Refresh agent
# ---------------------------------------------------------------------------

def test_refresh_grant_prices_applies_rates(monkeypatch):
    async def fake_fetch(tier='community'):
        return {'H100_80GB': 2.69, 'H200': 3.59, 'B200': 5.98}

    async def fake_sol():
        return 148.25

    monkeypatch.setattr(pricing, 'fetch_runpod_gpu_rates', fake_fetch)
    monkeypatch.setattr(pricing, 'get_sol_price_usd', fake_sol)

    summary = asyncio.run(pricing.refresh_grant_prices())
    assert summary['tier'] == 'community'
    assert summary['rates']['H100_80GB'] == 2.69
    assert summary['sol_price_usd'] == 148.25
    assert pricing.GPU_RATES['H200'] == 3.59


def test_refresh_grant_prices_failure_keeps_rates(monkeypatch):
    async def fake_fetch(tier='community'):
        raise RuntimeError('boom')

    monkeypatch.setattr(pricing, 'fetch_runpod_gpu_rates', fake_fetch)
    result = asyncio.run(pricing.refresh_grant_prices())
    assert result is None
    assert pricing.GPU_RATES == {'H100_80GB': 2.50, 'H200': 3.50, 'B200': 5.00}


def test_refresh_grant_prices_keeps_rates_when_sol_fails(monkeypatch):
    async def fake_fetch(tier='community'):
        return {'H100_80GB': 2.69, 'H200': 3.59, 'B200': 5.98}

    async def fake_sol():
        raise RuntimeError('coingecko down')

    monkeypatch.setattr(pricing, 'fetch_runpod_gpu_rates', fake_fetch)
    monkeypatch.setattr(pricing, 'get_sol_price_usd', fake_sol)

    summary = asyncio.run(pricing.refresh_grant_prices())
    assert summary['rates']['H100_80GB'] == 2.69
    assert summary['sol_price_usd'] is None


# ---------------------------------------------------------------------------
# Assessor prompt follows live rates
# ---------------------------------------------------------------------------

def test_assessor_prompt_reflects_refreshed_rates():
    from src.features.grants import assessor

    pricing.apply_gpu_rates({'H100_80GB': 2.69, 'H200': 3.59, 'B200': 5.98})
    prompt = assessor._build_system_prompt(server_config=None, guild_id=None)
    assert '- H100_80GB: $2.69/hr' in prompt
    assert '- H200: $3.59/hr' in prompt
    assert '- B200: $5.98/hr' in prompt
    # cap = 50 * 2.69 * 1.1 = 147.95 -> shown as $148
    assert '$148' in prompt
