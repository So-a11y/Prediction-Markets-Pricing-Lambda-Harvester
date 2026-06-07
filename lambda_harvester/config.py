"""
Empirical parameters calibrated from Yang (2026)
"Pricing Prediction Markets: Incomplete Markets, Selection Rules, and Risk Premia"
University of Illinois Urbana-Champaign, Working Paper April 2026

Yang, Y. (2026). Pricing Prediction Markets: Incomplete Markets, Selection Rules, and Risk Premia. Working Paper
"""

# ── Global λ estimates (Table 18, Yang 2026) ──────────────────────────────────
LAMBDA_POLYMARKET = 0.176       # MLE, N=2,460 (SE=0.027, p=7.1e-11)
LAMBDA_POLYMARKET_FULL = 0.166  # N=13,738 (SE=0.011)

# ── Category-level λ (Table 13, Yang 2026) ────────────────────────────────────
LAMBDA_BY_CATEGORY = {
    "sports":       0.070,   # insignificant — competed away
    "politics":     0.054,   # insignificant
    "crypto":       0.253,   # p=0.001
    "science":      0.268,   # p=0.001
    "tech":         0.268,   # p=0.001
    "other":        0.282,   # p<0.001
}

# ── Duration-stratified λ (Table 10, Yang 2026) ───────────────────────────────
LAMBDA_BY_DURATION = {
    "2_6h":   0.078,
    "6_24h": -0.138,   # mostly labeling artifact per CI spec
    "1_3d":   0.121,
    "3_7d":   0.227,
    "7d_plus": 0.444,  # largest structural wedge
}

# ── Volume tiers (Table 14, Yang 2026) ────────────────────────────────────────
# Very-high volume (>$10K) shows λ̂ = -0.031 (insignificant) — wedge competed away
VOLUME_TIER_LAMBDA = {
    "low":       (0, 500,    0.354),
    "medium":    (500, 2000, 0.285),
    "high":      (2000, 10000, 0.316),
    "very_high": (10000, float("inf"), -0.031),  # no edge
}

# ── Time-decay parameters (Table 6, Yang 2026) ────────────────────────────────
# λ(τ) = β₀ + γ₁τ + γ₂τ²  where τ ∈ [0,1] is fraction of lifetime elapsed
LAMBDA_DECAY_BETA0 = 0.170    # baseline at opening
LAMBDA_DECAY_GAMMA1 = -0.232  # linear decay
LAMBDA_DECAY_GAMMA2 = 0.151   # quadratic term (slows near resolution)

# ── Execution filters (plan document + Yang 2026 evidence) ────────────────────
MIN_DAYS_TO_RESOLUTION = 7          # λ̂ ≈ 0.444 for >7d vs ~0.12 for 1-3d
PRICE_LOWER_BOUND = 0.05            # avoid boundary artifacts (logit noise)
PRICE_UPPER_BOUND = 0.50            # longshot focus; FLB strongest below 0.20 but widened for coverage
MAX_VOLUME_USD = 25_000             # raised from 10K; Table 14 shows λ≈0 above 10K on average but that pools with >$1M events
MIN_VOLUME_USD = 100                # minimum liquidity floor
MIN_PRICE_HISTORY_HOURS = 10       # minimum hourly obs for EIV estimate
PRICE_HISTORY_HOURS = 48            # lookback window for EIV calculation

# Priority categories (high structural λ per Table 13)
HIGH_EDGE_CATEGORIES = {"crypto", "science", "tech", "other"}
LOW_EDGE_CATEGORIES = {"sports", "politics"}

# ── Scoring thresholds ────────────────────────────────────────────────────────
MIN_LAMBDA_SIGNAL = 0.15    # minimum λ to flag (below global avg = noise)
MAX_EIV_THRESHOLD = 0.30    # "quiet market" — low information arrival rate
MIN_TRADE_SCORE = 0.5       # λ/EIV minimum to generate alert

# ── Execution quality filter ──────────────────────────────────────────────────
# A spread > 10% means you give up >10 cents per dollar just entering the trade.
# Ghost markets (spread ~1.0) have Gamma mid-prices that are completely stale.
MAX_BID_ASK_SPREAD = 0.25

# ── API endpoints ──────────────────────────────────────────────────────────────
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"
