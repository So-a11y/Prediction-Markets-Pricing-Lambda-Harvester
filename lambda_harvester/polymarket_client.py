"""
Polymarket API client — Gamma API (market metadata) + CLOB API (price history).

Yang (2026) Section 4.1: "I collect contract-level data from Polymarket via its
public Gamma API (market metadata) and CLOB API (hourly price histories)."

Gamma API: https://gamma-api.polymarket.com
CLOB API:  https://clob.polymarket.com
"""

import time
import datetime
import requests
import json
from typing import Optional

from lambda_harvester.config import (
    GAMMA_API_BASE,
    CLOB_API_BASE,
    PRICE_HISTORY_HOURS,
)


class PolymarketClient:
    """Client for Polymarket Gamma and CLOB APIs."""

    def __init__(self, request_delay: float = 0.25):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "lambda-harvester/1.0",
        })
        self.request_delay = request_delay

    def _get(self, url: str, params: dict = None, retries: int = 3) -> Optional[dict | list]:
        for attempt in range(retries):
            try:
                resp = self.session.get(url, params=params, timeout=15)
                resp.raise_for_status()
                time.sleep(self.request_delay)
                return resp.json()
            except requests.exceptions.HTTPError as e:
                if resp.status_code == 429:
                    wait = 2 ** attempt
                    print(f"  Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                elif attempt == retries - 1:
                    print(f"  HTTP error {resp.status_code} for {url}: {e}")
                    return None
            except Exception as e:
                if attempt == retries - 1:
                    print(f"  Request failed for {url}: {e}")
                    return None
                time.sleep(1)
        return None

    # ── Gamma API ──────────────────────────────────────────────────────────────

    def get_active_markets(self,
                           limit: int = 500,
                           offset: int = 0,
                           tag_slug: str = None) -> list[dict]:
        """Fetch active, unresolved Polymarket markets from Gamma API.

        Returns list of market dicts with fields:
            id, conditionId, question, outcomePrices, clobTokenIds,
            volume, startDate, endDate, tags, active, closed
        """
        params = {
            "active": "true",
            "closed": "false",
            "limit": limit,
            "offset": offset,
        }
        if tag_slug:
            params["tag_slug"] = tag_slug

        url = f"{GAMMA_API_BASE}/markets"
        data = self._get(url, params)
        if data is None:
            return []
        return data if isinstance(data, list) else data.get("markets", [])

    def get_all_active_markets(self, max_markets: int = 2000) -> list[dict]:
        """Paginate through all active markets."""
        all_markets = []
        limit = 500
        offset = 0
        while len(all_markets) < max_markets:
            batch = self.get_active_markets(limit=limit, offset=offset)
            if not batch:
                break
            all_markets.extend(batch)
            if len(batch) < limit:
                break
            offset += limit
        return all_markets[:max_markets]

    def parse_market_price(self, market: dict) -> Optional[float]:
        """Extract YES mid-price from market outcomePrices field."""
        raw = market.get("outcomePrices")
        if raw is None:
            return None
        try:
            prices = json.loads(raw) if isinstance(raw, str) else raw
            yes_price = float(prices[0])
            return yes_price
        except (ValueError, IndexError, TypeError):
            return None

    def parse_end_date(self, market: dict) -> Optional[datetime.datetime]:
        """Parse market end/resolution date."""
        raw = market.get("endDate") or market.get("end_date_iso")
        if not raw:
            return None
        try:
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            return datetime.datetime.fromisoformat(raw)
        except ValueError:
            return None

    def days_to_resolution(self, market: dict) -> Optional[float]:
        """Calculate days remaining until market resolution."""
        end_dt = self.parse_end_date(market)
        if end_dt is None:
            return None
        now = datetime.datetime.now(datetime.timezone.utc)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=datetime.timezone.utc)
        delta = end_dt - now
        return max(0.0, delta.total_seconds() / 86400)

    def parse_volume(self, market: dict) -> float:
        """Parse trading volume in USD."""
        try:
            return float(market.get("volume", 0) or 0)
        except (ValueError, TypeError):
            return 0.0

    def get_clob_token_ids(self, market: dict) -> list[str]:
        """Extract CLOB token IDs for YES (index 0) and NO (index 1) outcomes."""
        raw = market.get("clobTokenIds")
        if raw is None:
            return []
        try:
            tokens = json.loads(raw) if isinstance(raw, str) else raw
            return [str(t) for t in tokens]
        except (ValueError, TypeError):
            return []

    def categorize_market(self, market: dict) -> str:
        """Classify market into category using keyword matching.

        Based on Table 13 footnote (Yang 2026): keyword regex matching on titles.
        Categories with high structural λ: crypto, science/tech, other.
        Categories with near-zero λ (competed away): sports, politics.
        """
        question = (market.get("question") or "").lower()
        tags = [t.get("slug", "").lower() for t in (market.get("tags") or [])]
        tag_str = " ".join(tags)
        text = question + " " + tag_str

        sports_kw = ["nba", "nfl", "nhl", "mlb", "ufc", "fifa", "soccer", "basketball",
                     "football", "baseball", "tennis", "golf", "f1", "formula", "olympics",
                     "league", "super bowl", "world cup", "championship", "match", "game",
                     "team", "player", "score", "win", "beat"]
        politics_kw = ["election", "president", "congress", "senate", "vote", "democrat",
                       "republican", "governor", "minister", "parliament", "referendum",
                       "poll", "approval", "trump", "biden", "harris", "policy", "law", "bill"]
        crypto_kw = ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "sol",
                     "usdc", "defi", "nft", "blockchain", "token", "altcoin", "binance"]
        tech_kw = ["apple", "google", "microsoft", "meta", "amazon", "tesla", "nvidia",
                   "openai", "anthropic", "ai ", "spacex", "starship", "iphone", "gpt",
                   "ipo", "sec", "tech", "startup", "acquisition"]
        science_kw = ["science", "research", "study", "discovery", "drug", "fda", "clinical",
                      "vaccine", "trial", "nobel", "physics", "biology", "cancer", "space",
                      "nasa", "launch", "satellite", "mars", "moon"]

        for kw in sports_kw:
            if kw in text:
                return "sports"
        for kw in politics_kw:
            if kw in text:
                return "politics"
        for kw in crypto_kw:
            if kw in text:
                return "crypto"
        for kw in tech_kw:
            if kw in text:
                return "tech"
        for kw in science_kw:
            if kw in text:
                return "science"
        return "other"

    # ── CLOB API ───────────────────────────────────────────────────────────────

    def get_price_history(self,
                          token_id: str,
                          hours: int = PRICE_HISTORY_HOURS,
                          fidelity: int = 60) -> list[float]:
        """Fetch hourly price history for a CLOB token.

        Uses the CLOB prices-history endpoint. Returns YES prices in chronological order.

        Args:
            token_id: CLOB token ID for YES outcome
            hours:    Hours of history to fetch
            fidelity: Candle interval in minutes (60 = hourly)

        Returns:
            List of mid-prices in chronological order
        """
        end_ts = int(time.time())
        start_ts = end_ts - hours * 3600

        params = {
            "market": token_id,
            "startTs": start_ts,
            "endTs": end_ts,
            "fidelity": fidelity,
        }
        url = f"{CLOB_API_BASE}/prices-history"
        data = self._get(url, params)

        if data is None:
            return []

        history = data.get("history") if isinstance(data, dict) else data
        if not history:
            return []

        prices = []
        for candle in history:
            try:
                p = float(candle.get("p", candle.get("price", candle.get("c", 0))))
                if 0 < p < 1:
                    prices.append(p)
            except (ValueError, TypeError):
                continue

        return prices

    def get_market_mid_price(self, token_id: str) -> Optional[float]:
        """Fetch current best-bid/ask mid-price from CLOB order book."""
        url = f"{CLOB_API_BASE}/book"
        params = {"token_id": token_id}
        data = self._get(url, params)
        if data is None:
            return None
        try:
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            if bids and asks:
                best_bid = float(bids[0]["price"])
                best_ask = float(asks[0]["price"])
                return (best_bid + best_ask) / 2
        except (ValueError, TypeError, KeyError, IndexError):
            pass
        return None

    def get_price_history_with_timestamps(
        self,
        token_id: str,
        start_ts: int,
        end_ts: Optional[int] = None,
        fidelity: int = 60,
    ) -> list[tuple[int, float]]:
        """Fetch price history as (unix_timestamp, price) pairs for backtesting."""
        if end_ts is None:
            end_ts = int(time.time())
        params = {
            "market": token_id,
            "startTs": start_ts,
            "endTs": end_ts,
            "fidelity": fidelity,
        }
        data = self._get(f"{CLOB_API_BASE}/prices-history", params)
        if data is None:
            return []
        history = data.get("history") if isinstance(data, dict) else data
        if not history:
            return []
        result = []
        for candle in history:
            try:
                ts = int(candle.get("t", candle.get("ts", candle.get("time", 0))))
                p = float(candle.get("p", candle.get("price", candle.get("c", 0))))
                if ts > 0 and 0 < p < 1:
                    result.append((ts, p))
            except (ValueError, TypeError):
                continue
        result.sort(key=lambda x: x[0])
        return result

    # ── Resolved markets (for backtesting) ────────────────────────────────────

    def get_market_by_id(self, market_id: str) -> Optional[dict]:
        """Fetch a single market by its condition ID (works for active and resolved)."""
        data = self._get(f"{GAMMA_API_BASE}/markets", params={"id": market_id})
        if data is None:
            return None
        markets = data if isinstance(data, list) else data.get("markets", [])
        return markets[0] if markets else None

    def get_resolved_markets(self, limit: int = 500, offset: int = 0) -> list[dict]:
        """Fetch resolved (closed) markets from Gamma API."""
        params = {
            "active": "false",
            "closed": "true",
            "limit": limit,
            "offset": offset,
        }
        data = self._get(f"{GAMMA_API_BASE}/markets", params)
        if data is None:
            return []
        return data if isinstance(data, list) else data.get("markets", [])

    def get_all_resolved_markets(self, max_markets: int = 1000) -> list[dict]:
        """Paginate through resolved markets."""
        all_markets: list[dict] = []
        limit = 500
        offset = 0
        while len(all_markets) < max_markets:
            batch = self.get_resolved_markets(limit=limit, offset=offset)
            if not batch:
                break
            all_markets.extend(batch)
            if len(batch) < limit:
                break
            offset += limit
        return all_markets[:max_markets]

    def get_resolution_outcome(self, market: dict) -> Optional[int]:
        """Return 1 if YES won, 0 if NO won, None if indeterminate."""
        raw = market.get("outcomePrices")
        if raw is not None:
            try:
                prices = json.loads(raw) if isinstance(raw, str) else raw
                yes_price = float(prices[0])
                if yes_price >= 0.99:
                    return 1
                elif yes_price <= 0.01:
                    return 0
            except (ValueError, IndexError, TypeError):
                pass
        res = str(market.get("resolution") or "").strip().upper()
        if res == "YES":
            return 1
        if res == "NO":
            return 0
        return None
