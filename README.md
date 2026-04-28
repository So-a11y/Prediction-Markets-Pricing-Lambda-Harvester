# λ-Harvesting Bot — Prediction Market Risk Premium Scanner

A quantitative scanner for [Polymarket](https://polymarket.com) and [Kalshi](https://kalshi.com) that identifies contracts where the market price systematically embeds a structural risk premium over the true physical probability — and ranks them by signal strength for **BUY NO** trading.

Built on the Wang Transform framework from Yang (2026).

---

## How It Works

Prediction market prices are not pure probability estimates. They embed a risk premium λ (lambda) that compensates liquidity providers for bearing unhedgeable binary risk. The Wang Transform makes this explicit:

```
p_mkt = Φ(Φ⁻¹(p*) + λ)
```

where `p*` is the physical (true) probability and `λ` is the pricing wedge.

**The strategy**: find markets where λ is large and the market is quiet (low EIV — Event Implied Volatility), meaning the overpricing is structural rather than driven by informed trading. Buy NO on those contracts.

**Primary ranking metric**: `Trade Score = λ / EIV`

Yang (2026) finds significant positive λ on Polymarket (λ̂ = 0.176, N=2,460) and Kalshi (λ̂ = 0.187, N=271,699), with the largest wedges in:
- Duration > 7 days (λ̂ = 0.444)
- Crypto / Science / Tech categories (λ̂ ≈ 0.25–0.28)
- Volume under $10K (wedge competed away above this)

---

## Installation

**Requirements**: Python 3.11+

```bash
git clone https://github.com/So-a11y/Prediction-Markets-Pricing-Lambda-Harvester.git
cd Prediction-Markets-Pricing-Lambda-Harvester
pip install -r requirements.txt
```

**Credentials** — create a `.env` file in the project root:

```env
# Kalshi (required only for Kalshi scanning)
# Option A — RSA key pair (recommended):
KALSHI_KEY_ID=your-key-id-from-dashboard
KALSHI_PRIVATE_KEY_PATH=C:\path\to\kalshi_private_key.pem

# Option B — email + password:
KALSHI_EMAIL=you@example.com
KALSHI_PASSWORD=yourpassword
```

Polymarket scanning requires no credentials (public API).

---

## Usage

### Live scan — find opportunities now

```bash
# Scan Polymarket (all categories)
python -m lambda_harvester.main

# Scan Polymarket, filter to high-edge categories only
python -m lambda_harvester.main --categories crypto,tech,science,other

# Scan Polymarket, show top 10, compact output
python -m lambda_harvester.main --top 10 --compact

# Scan Kalshi directly
python -m lambda_harvester.main --platform kalshi

# See a synthetic demo without any API calls
python -m lambda_harvester.main --demo
```

### Forward validation — track your signals

```bash
# Save today's opportunities to signals_log.json
python -m lambda_harvester.main --categories crypto,tech,science,other --save-signals

# Check which saved signals have resolved and print P&L
python -m lambda_harvester.main --check-outcomes
```

Run `--save-signals` regularly (daily or weekly). Each market is only recorded once — the original entry price is preserved. When markets resolve, `--check-outcomes` fetches the outcome and computes realized P&L.

### Backtest

```bash
# Backtest on 500 resolved Polymarket markets
python -m lambda_harvester.main --backtest --backtest-markets 500 --platform polymarket
```

> **Note**: Polymarket's CLOB API does not serve price history for resolved tokens, so the backtester uses the Wang prior for p* estimation rather than the EMA method. Use forward validation for the most accurate performance measurement.

---

## Execution Filters

| Filter | Value | Rationale |
|--------|-------|-----------|
| Price range | [0.05, 0.50] | Avoid boundary noise; FLB strongest in longshot range |
| Duration | > 7 days | λ̂ = 0.444 for long-duration contracts (Table 10) |
| Volume | $0 – $100K | Wedge competed away above ~$10K (Table 14) |
| EIV threshold | ≤ 0.30 | Low volatility = structural premium, not informed trading |
| Min λ | ≥ 0.15 | Below global Polymarket mean = noise |
| Min trade score | λ/EIV ≥ 0.5 | Minimum signal-to-noise ratio |

---

## Output Example

```
══════════════════════════════════════════════════════════════════════
  #1  🔴 STRONG  |  Trade Score (λ/EIV): 2.89
══════════════════════════════════════════════════════════════════════
  Market   : Will Bitcoin exceed $100,000 before May 31 2026?
  Category : CRYPTO        Days to resolution: 18.3d
  Volume   : $     2,450

  ┌─ Pricing ──────────────────────────────────────────────────────┐
  │  Market Price  (p_mkt) : 0.1500  (15.0%)                       │
  │  Physical p*           : 0.0800  (8.0%)   [ema]                │
  │  Overpricing           : +87.5%                                 │
  └────────────────────────────────────────────────────────────────┘

  ┌─ Wang Transform (λ) ───────────────────────────────────────────┐
  │  λ (contract)  : +0.2412                                        │
  │  λ (expected)  : +0.2530  [Yang 2026 benchmark]                 │
  │  λ (excess)    : -0.0118  [abnormal wedge]                      │
  │  EIV           :  0.0823  [n=42 hourly obs]                     │
  │  Trade Score   :  2.93    [λ/EIV — primary rank metric]         │
  └────────────────────────────────────────────────────────────────┘

  ► RECOMMENDATION: STRONG BUY NO — harvest risk premium
```

---

## Project Structure

```
lambda_harvester/
├── main.py              # CLI entry point
├── scanner.py           # Core Wang Transform scanner
├── math_engine.py       # λ, EIV, and Wang Transform calculations
├── polymarket_client.py # Polymarket Gamma + CLOB API client
├── kalshi_client.py     # Kalshi REST API client (RSA auth)
├── kalshi_matcher.py    # Cross-platform market matching
├── signal_tracker.py    # Forward validation: save signals, check outcomes
├── backtest.py          # Historical backtester
└── config.py            # Empirical parameters from Yang (2026)
```

---

## Reference

> Yang, Y. (2026). *Pricing Prediction Markets: Incomplete Markets, Selection Rules, and Risk Premia*. Working Paper, University of Illinois Urbana-Champaign.

---

## Disclaimer

This software is for research and educational purposes. Prediction market trading involves financial risk. Past model performance does not guarantee future results. Always review recommendations before placing trades.
