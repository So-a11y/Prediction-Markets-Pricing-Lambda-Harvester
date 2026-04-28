"""
Kalshi API client — mirrors PolymarketClient's interface so the scanner and
backtester work unchanged with either platform.

Authentication — set these in your .env file:
    KALSHI_KEY_ID=your-key-id-from-dashboard
    KALSHI_PRIVATE_KEY_PATH=C:\\path\\to\\kalshi_private_key.pem

Key differences from Polymarket:
  - Prices in cents (0–100); divided by 100 here to produce probabilities.
  - Market identifier is a "ticker" string (e.g. "KXBTCD-25MAY-T100000").
  - Pagination is cursor-based, not offset-based.
  - Resolution outcome is a "result" field: "yes" | "no" | null.
"""

import base64
import os
import time
import datetime
from typing import Optional
from urllib.parse import urlparse

import requests
from requests.auth import AuthBase
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

_BASE = "https://api.elections.kalshi.com/trade-api/v2"


class _KalshiRSAAuth(AuthBase):
    """Attaches Kalshi RSA signature headers to every request."""

    def __init__(self, key_id: str, private_key):
        self.key_id = key_id
        self.private_key = private_key

    def __call__(self, r: requests.PreparedRequest) -> requests.PreparedRequest:
        ts = str(int(time.time() * 1000))
        path = urlparse(r.url).path
        message = (ts + r.method.upper() + path).encode()
        signature = self.private_key.sign(
            message,
            asym_padding.PKCS1v15(),
            hashes.SHA256(),
        )
        r.headers["KALSHI-ACCESS-KEY"] = self.key_id
        r.headers["KALSHI-ACCESS-TIMESTAMP"] = ts
        r.headers["KALSHI-ACCESS-SIGNATURE"] = base64.b64encode(signature).decode()
        return r


class KalshiClient:
    """Kalshi REST API client compatible with the PolymarketClient interface."""

    def __init__(self, request_delay: float = 0.3):
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self.request_delay = request_delay
        self.session.auth = self._build_auth()

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _build_auth(self):
        # Option A: RSA key pair
        key_id = os.getenv("KALSHI_KEY_ID", "").strip()
        key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "").strip()
        if key_id and key_path:
            with open(key_path, "rb") as f:
                private_key = serialization.load_pem_private_key(f.read(), password=None)
            return _KalshiRSAAuth(key_id, private_key)

        # Option B: email + password login
        email = os.getenv("KALSHI_EMAIL", "").strip()
        password = os.getenv("KALSHI_PASSWORD", "").strip()
        if email and password:
            resp = requests.post(
                f"{_BASE}/login",
                json={"email": email, "password": password},
                timeout=15,
            )
            resp.raise_for_status()
            token = resp.json().get("token")
            if not token:
                raise ValueError("Kalshi login succeeded but returned no token.")
            self.session.headers["Authorization"] = f"Token {token}"
            return None  # session header handles auth

        raise EnvironmentError(
            "\nKalshi credentials not found. Add ONE of these to your .env file:\n\n"
            "  Option A — email + password (simplest):\n"
            "    KALSHI_EMAIL=you@example.com\n"
            "    KALSHI_PASSWORD=yourpassword\n\n"
            "  Option B — RSA key pair:\n"
            "    KALSHI_KEY_ID=your-key-id\n"
            "    KALSHI_PRIVATE_KEY_PATH=C:\\path\\to\\kalshi_private_key.pem\n"
        )

    # ── HTTP ──────────────────────────────────────────────────────────────────

    def _get(self, url: str, params: dict = None, retries: int = 3) -> Optional[dict]:
        for attempt in range(retries):
            try:
                resp = self.session.get(url, params=params, timeout=15)
                resp.raise_for_status()
                time.sleep(self.request_delay)
                return resp.json()
            except requests.HTTPError:
                if resp.status_code == 429:
                    time.sleep(2 ** attempt)
                elif resp.status_code == 401:
                    print(f"  Auth failed (401): {resp.text}")
                    print("  Check that KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH in .env match the same key.")
                    return None
                elif attempt == retries - 1:
                    print(f"  HTTP {resp.status_code} for {url}: {resp.text}")
                    return None
            except Exception as e:
                if attempt == retries - 1:
                    print(f"  Request failed for {url}: {e}")
                    return None
                time.sleep(1)
        return None

    # ── Market normalization ───────────────────────────────────────────────────

    def _normalize(self, market: dict) -> dict:
        """Map Kalshi field names to the common names the scanner expects."""
        out = dict(market)
        out.setdefault("question", out.get("title", ""))
        out.setdefault("startDate", out.get("open_time", ""))
        return out

    # ── Active markets ─────────────────────────────────────────────────────────

    def _get_markets(self, status: Optional[str], limit: int, cursor: Optional[str]) -> tuple[list[dict], Optional[str]]:
        params: dict = {"limit": limit}
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        data = self._get(f"{_BASE}/markets", params)
        if data is None:
            return [], None
        markets = [
            self._normalize(m) for m in data.get("markets", [])
            if not m.get("mve_selected_legs")  # skip multivariate parlay markets
        ]
        return markets, data.get("cursor")

    def get_all_active_markets(self, max_markets: int = 1000) -> list[dict]:
        all_markets: list[dict] = []
        cursor = None
        page = 0
        max_pages = 100
        for _ in range(max_pages):
            if len(all_markets) >= max_markets:
                break
            batch, cursor = self._get_markets(None, 1000, cursor)
            page += 1
            if batch:
                all_markets.extend(batch)
            if not cursor:
                break
        return all_markets[:max_markets]

    # ── Price parsing ──────────────────────────────────────────────────────────

    def parse_market_price(self, market: dict) -> Optional[float]:
        """Return YES mid-price as probability in [0, 1]."""
        if market.get("mve_selected_legs"):
            return None

        def _mid(bid_key: str, ask_key: str) -> Optional[float]:
            b = market.get(bid_key)
            a = market.get(ask_key)
            if b is None or a is None:
                return None
            try:
                bid, ask = float(b), float(a)
                if ask > 0:
                    return (bid + ask) / 2.0
            except (ValueError, TypeError):
                pass
            return None

        # 1. YES bid/ask directly
        p = _mid("yes_bid_dollars", "yes_ask_dollars")
        if p is not None and p > 0:
            return p

        # 2. Infer YES from NO bid/ask  (YES = 1 - NO_mid)
        no_mid = _mid("no_bid_dollars", "no_ask_dollars")
        if no_mid is not None and 0 < no_mid < 1:
            yes_from_no = 1.0 - no_mid
            if yes_from_no > 0:
                return yes_from_no

        # 3. Last traded price
        for field in ("last_price_dollars", "previous_price_dollars", "previous_yes_ask_dollars"):
            raw = market.get(field)
            if raw is not None:
                try:
                    p = float(raw)
                    if 0 < p < 1:
                        return p
                except (ValueError, TypeError):
                    pass

        return None

    def parse_end_date(self, market: dict) -> Optional[datetime.datetime]:
        raw = market.get("close_time") or market.get("expiration_time")
        if not raw:
            return None
        try:
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            return datetime.datetime.fromisoformat(raw)
        except ValueError:
            return None

    def days_to_resolution(self, market: dict) -> Optional[float]:
        end_dt = self.parse_end_date(market)
        if end_dt is None:
            return None
        now = datetime.datetime.now(datetime.timezone.utc)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=datetime.timezone.utc)
        return max(0.0, (end_dt - now).total_seconds() / 86400)

    def parse_volume(self, market: dict) -> float:
        # volume_fp is contracts traded; each contract ≈ $1, so ≈ dollar volume
        for field in ("volume_fp", "dollar_volume", "volume"):
            val = market.get(field)
            if val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass
        return 0.0

    def get_clob_token_ids(self, market: dict) -> list[str]:
        ticker = market.get("ticker")
        return [ticker] if ticker else []

    def categorize_market(self, market: dict) -> str:
        cat = str(market.get("category") or "").lower()
        title = str(market.get("title") or market.get("question") or "").lower()
        text = title + " " + cat

        if any(k in text for k in ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "defi", "nft", "blockchain"]):
            return "crypto"
        if any(k in text for k in ["election", "president", "congress", "senate", "vote", "democrat", "republican", "parliament", "trump", "biden"]):
            return "politics"
        if any(k in text for k in ["nba", "nfl", "nhl", "mlb", "ufc", "soccer", "basketball", "football", "baseball", "tennis", "golf", "f1", "championship"]):
            return "sports"
        if any(k in text for k in ["apple", "google", "microsoft", "meta", "amazon", "tesla", "nvidia", "openai", "anthropic", "spacex", "ipo", "tech"]):
            return "tech"
        if any(k in text for k in ["science", "drug", "fda", "vaccine", "trial", "nobel", "cancer", "nasa", "mars", "space"]):
            return "science"
        return "other"

    # ── Price history ──────────────────────────────────────────────────────────

    def get_price_history(self, ticker: str, hours: int = 48, fidelity: int = 60) -> list[float]:
        start_ts = int(time.time()) - hours * 3600
        return [p for _, p in self.get_price_history_with_timestamps(ticker, start_ts=start_ts, fidelity=fidelity)]

    def get_price_history_with_timestamps(
        self,
        ticker: str,
        start_ts: int,
        end_ts: Optional[int] = None,
        fidelity: int = 60,
    ) -> list[tuple[int, float]]:
        if end_ts is None:
            end_ts = int(time.time())
        params = {"start_ts": start_ts, "end_ts": end_ts, "period_interval": fidelity}
        data = self._get(f"{_BASE}/markets/{ticker}/candlesticks", params)
        if data is None:
            return []
        candles = data.get("candlesticks", data.get("history", []))
        result = []
        for c in candles:
            try:
                ts = int(c.get("end_period_ts", c.get("start_ts", c.get("ts", c.get("t", 0)))))
                # close_price may be a bare number or nested {"price": N}
                close = c.get("close_price", c.get("close", c.get("p", c.get("c"))))
                if isinstance(close, dict):
                    close = close.get("price", 0)
                p = float(close)
                # Kalshi prices are 0–1 (dollars); if >1 assume cents and convert
                if p > 1:
                    p /= 100.0
                if ts > 0 and 0 < p < 1:
                    result.append((ts, p))
            except (ValueError, TypeError):
                continue
        result.sort(key=lambda x: x[0])
        return result

    # ── Resolved markets (backtesting) ────────────────────────────────────────

    def get_all_resolved_markets(self, max_markets: int = 500) -> list[dict]:
        all_markets: list[dict] = []
        cursor = None
        for _ in range(50):
            if len(all_markets) >= max_markets:
                break
            batch, cursor = self._get_markets("settled", 200, cursor)
            all_markets.extend(batch)
            if not cursor:
                break
        return all_markets[:max_markets]

    def get_resolution_outcome(self, market: dict) -> Optional[int]:
        result = str(market.get("result") or "").strip().lower()
        if result == "yes":
            return 1
        if result == "no":
            return 0
        return None
