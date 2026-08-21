"""GPU pricing and SOL price fetching for micro-grants."""

import json
import logging
import os
import re
import time

import aiohttp

logger = logging.getLogger('DiscordBot')

# Approximate cloud GPU rates (USD/hr) — maintained by the price refresh loop
# (refresh_grant_prices) from live market listings; these are the fallback
# defaults until the first successful fetch.
GPU_RATES = {
    'H100_80GB': 2.50,
    'H200': 3.50,
    'B200': 5.00,
}

# 10% buffer for platform fees
FEE_MULTIPLIER = 1.1

# Supported grant GPU types. The refresh agent updates exactly these keys —
# keeping the set stable means the assessor, cost calc, and payment flow never
# see an unadvertised GPU type.
GRANT_GPU_TYPES = ('H100_80GB', 'H200', 'B200')

# RunPod pricing-page product names (substring match on JSON-LD names).
RUNPOD_GPU_MAP = {
    'H100_80GB': 'H100 SXM',
    'H200': 'H200',
    'B200': 'B200',
}

# Sanity bounds for fetched rates — guards against the source changing format
# and shipping garbage into the reviewer prompt / grant amounts.
MIN_RATE_USD = 0.10
MAX_RATE_USD = 20.00

# Source page for GPU market rates (overridable for testing/fallback).
GRANTS_PRICE_URL = 'https://www.runpod.io/pricing'
_HTTP_UA = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/126.0 Safari/537.36'
)

# Cache SOL price for 60 seconds
_sol_price_cache = {'price': None, 'timestamp': 0}
SOL_CACHE_TTL = 60


def max_grant_usd() -> float:
    """Current grant cap: 50hrs of H100 at today's rate, incl. fee buffer.

    Follows the live H100 rate so the cap stays aligned with the market.
    """
    return round(50 * GPU_RATES['H100_80GB'] * FEE_MULTIPLIER, 2)


def _walk_ld_json(node, out):
    """Recursively collect JSON-LD nodes into ``out`` (handles @graph)."""
    if isinstance(node, dict):
        node_type = node.get('@type')
        if node_type == 'Product' or (isinstance(node_type, list) and 'Product' in node_type):
            out.append(node)
        for value in node.values():
            _walk_ld_json(value, out)
    elif isinstance(node, list):
        for value in node:
            _walk_ld_json(value, out)


def _parse_runpod_pricing_html(html: str, tier: str = 'community') -> dict:
    """Extract per-GPU hourly rates from the RunPod pricing page JSON-LD.

    Returns {grant_gpu_type: rate_usd} for every supported type.

    Raises:
        ValueError: unknown tier, unparseable page, missing/out-of-range rates.
    """
    if tier not in ('community', 'secure'):
        raise ValueError(f"Unknown price tier: {tier!r}. Must be 'community' or 'secure'")
    price_field = 'lowPrice' if tier == 'community' else 'highPrice'

    products = []
    for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            payload = json.loads(block)
        except json.JSONDecodeError:
            continue
        _walk_ld_json(payload, products)
    if not products:
        raise ValueError('RunPod pricing page contained no JSON-LD Product entries')

    rates = {}
    missing = []
    for gpu_key, listing_name in RUNPOD_GPU_MAP.items():
        match = None
        for product in products:
            if listing_name in (product.get('name') or ''):
                match = product
                break
        if match is None:
            missing.append(listing_name)
            continue
        offers = match.get('offers') or {}
        raw = offers.get(price_field)
        try:
            rate = float(raw)
        except (TypeError, ValueError):
            missing.append(f"{listing_name} ({price_field}={raw!r})")
            continue
        if not (MIN_RATE_USD <= rate <= MAX_RATE_USD):
            raise ValueError(
                f"RunPod rate for {listing_name} ({price_field}) out of bounds: ${rate}"
            )
        rates[gpu_key] = round(rate, 2)

    if missing:
        raise ValueError(f"RunPod pricing page missing rates for: {', '.join(missing)}")
    return rates


def apply_gpu_rates(new_rates: dict) -> dict:
    """Update GPU_RATES in place from a fetched market snapshot.

    Returns {gpu_type: (old_rate, new_rate)} for changed rates.
    Unknown keys are ignored — the supported set is fixed.
    """
    changes = {}
    for key, rate in new_rates.items():
        if key not in GPU_RATES:
            continue
        rounded = round(float(rate), 2)
        old = GPU_RATES[key]
        if abs(old - rounded) > 1e-9:
            changes[key] = (old, rounded)
            GPU_RATES[key] = rounded
    return changes


async def fetch_runpod_gpu_rates(tier: str = 'community') -> dict:
    """Fetch current RunPod hourly rates for the grant GPU types."""
    url = os.getenv('GRANTS_GPU_PRICE_URL', GRANTS_PRICE_URL)
    headers = {'User-Agent': _HTTP_UA}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            resp.raise_for_status()
            html = await resp.text()
    return _parse_runpod_pricing_html(html, tier=tier)


async def refresh_grant_prices() -> dict | None:
    """Refresh GPU market rates and warm the SOL price cache.

    Applies new rates to GPU_RATES on success (every downstream read follows).
    On any failure the current rates are kept and None is returned.

    Returns a summary dict on success: {tier, rates, changes, sol_price_usd}.
    """
    tier = os.getenv('GRANTS_GPU_PRICE_TIER', 'community').strip().lower()
    try:
        rates = await fetch_runpod_gpu_rates(tier=tier)
    except Exception as e:
        logger.warning(f"Grant price refresh: GPU rate fetch failed, keeping current rates: {e}")
        return None

    changes = apply_gpu_rates(rates)

    sol_price = None
    try:
        sol_price = await get_sol_price_usd()
    except Exception as e:
        logger.warning(f"Grant price refresh: SOL price fetch failed: {e}")

    summary = {
        'tier': tier,
        'rates': {key: GPU_RATES[key] for key in GRANT_GPU_TYPES},
        'changes': {key: {'from': old, 'to': new} for key, (old, new) in changes.items()},
        'sol_price_usd': sol_price,
    }
    logger.info(f"Grant price refresh: {json.dumps(summary)}")
    return summary


def calculate_grant_cost(gpu_type: str, hours: float) -> float:
    """Calculate total grant cost in USD including fee buffer."""
    rate = GPU_RATES.get(gpu_type)
    if not rate:
        raise ValueError(f"Unknown GPU type: {gpu_type}. Valid: {list(GPU_RATES.keys())}")
    return round(hours * rate * FEE_MULTIPLIER, 2)


async def get_sol_price_usd() -> float:
    """Fetch current SOL/USD price from CoinGecko (60s cache)."""
    now = time.time()
    if _sol_price_cache['price'] and (now - _sol_price_cache['timestamp']) < SOL_CACHE_TTL:
        return _sol_price_cache['price']

    url = 'https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd'
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            data = await resp.json()
            price = data['solana']['usd']

    _sol_price_cache['price'] = price
    _sol_price_cache['timestamp'] = now
    logger.info(f"Fetched SOL price: ${price}")
    return price


def usd_to_sol(usd_amount: float, sol_price: float) -> float:
    """Convert USD to SOL amount."""
    return round(usd_amount / sol_price, 6)
