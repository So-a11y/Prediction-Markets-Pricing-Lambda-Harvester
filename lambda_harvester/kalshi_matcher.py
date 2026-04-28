"""
Cross-platform matcher: finds Kalshi equivalents for Polymarket opportunities.

Matching strategy:
  1. Load all active Kalshi markets with valid prices.
  2. For each Polymarket question, score every Kalshi title by word overlap.
  3. Return the best match above a minimum similarity threshold.

The match is intentionally fuzzy — Polymarket and Kalshi phrase events differently,
so we match on shared keywords (numbers, entities, dates) after stripping stop words.
"""

import re
from typing import Optional
from lambda_harvester.kalshi_client import KalshiClient

_STOP = {
    "the", "a", "an", "will", "be", "is", "are", "was", "were",
    "in", "on", "at", "to", "for", "of", "and", "or", "by", "that",
    "this", "it", "with", "from", "as", "have", "has", "had", "do",
    "does", "did", "not", "but", "if", "than", "then", "so", "yet",
    "over", "under", "before", "after", "about", "would", "could",
    "should", "may", "might", "shall", "can", "which", "who", "what",
    "when", "where", "how", "no", "yes",
}

_MIN_SCORE = 0.20  # minimum Jaccard similarity to consider a match valid


_ALWAYS_KEEP = {"btc", "eth", "sol", "xrp", "usd", "fed", "s&p", "gdp", "cpi"}

def _tokenize(text: str) -> set[str]:
    tokens = re.findall(r"[\w\$\%\.]+", text.lower())
    return {t for t in tokens if (t in _ALWAYS_KEEP) or (t not in _STOP and len(t) > 2)}


class KalshiMatcher:
    """Loads Kalshi markets and matches them against Polymarket questions."""

    def __init__(self, verbose: bool = True):
        self.client = KalshiClient()
        self.verbose = verbose
        self._index: list[tuple[dict, float, set[str]]] = []  # (market, price, tokens)
        self._load()

    def _load(self):
        if self.verbose:
            print("  Loading Kalshi markets for cross-platform matching...")
        markets = self.client.get_all_active_markets(max_markets=2000)
        for m in markets:
            price = self.client.parse_market_price(m)
            if price is None:
                continue
            days = self.client.days_to_resolution(m)
            if days is None or days < 1:
                continue
            tokens = _tokenize(m.get("title", "") + " " + m.get("question", ""))
            if tokens:
                self._index.append((m, price, tokens))
        if self.verbose:
            print(f"  Indexed {len(self._index)} Kalshi markets with valid prices.")

    def find_equivalent(self, polymarket_question: str) -> Optional[tuple[dict, float, float]]:
        """Return (kalshi_market, kalshi_yes_price, similarity_score) or None."""
        query = _tokenize(polymarket_question)
        if not query:
            return None

        best_market = None
        best_price = None
        best_score = 0.0

        for market, price, tokens in self._index:
            intersection = query & tokens
            union = query | tokens
            score = len(intersection) / len(union) if union else 0.0
            if score > best_score:
                best_score = score
                best_market = market
                best_price = price

        if best_score < _MIN_SCORE or best_market is None:
            return None

        return best_market, best_price, best_score

    def format_trade(self, market: dict, yes_price: float, score: float) -> str:
        ticker = market.get("ticker", "N/A")
        no_price = 1.0 - yes_price
        days = self.client.days_to_resolution(market) or 0
        title = market.get("title", "")[:65]
        return (
            f"  KALSHI EQUIVALENT (match={score:.0%}):\n"
            f"    Ticker : {ticker}\n"
            f"    Market : {title}\n"
            f"    YES    : ${yes_price:.3f}   NO : ${no_price:.3f}\n"
            f"    Action : Buy NO at ~${no_price:.2f}  ({days:.1f}d to resolution)\n"
        )
