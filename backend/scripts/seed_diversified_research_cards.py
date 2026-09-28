"""Persist the five founder-approved diversified research hypotheses.

Research-only: writes the Hermes hypothesis ledger and never creates or wakes a
trading strategy, changes routing, or places an order.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.phase4_research import build_hypothesis_card, corpus_status, persist_hypothesis_cards  # noqa: E402


CARDS = [
    {
        "hypothesis": "Cross-sectional momentum in liquid Indian stocks produces positive market-neutral expectancy after turnover costs.",
        "who_pays": "Slow allocators and constrained short books allow relative winners to persist while losers underperform.",
        "universe": "Liquid NSE F&O stocks, sector-capped long top-decile versus short bottom-decile basket.",
        "horizon": "20-60 trading days with weekly and monthly rebalance variants.",
        "judge": "Purged walk-forward stock-return backtest with beta/sector neutralisation, turnover and borrow costs.",
        "data_needed": "Stock F&O bhavcopy, corporate actions, sector labels, liquidity, borrow/short constraints.",
        "kill_criteria": "Net OOS expectancy <= 0, unstable sign across years, or alpha disappears after sector/turnover controls.",
        "citations": [{"type": "corpus", "ref": "Momentum 30 India"}, {"type": "corpus", "ref": "Grinold-Kahn Fundamental Law"}],
    },
    {
        "hypothesis": "Large, correctly measured earnings surprises create post-earnings drift in liquid Indian F&O stocks after controlling for gap and sector moves.",
        "who_pays": "Investors underreact to earnings information and revise positions over several sessions.",
        "universe": "Liquid NSE F&O stocks with timestamped earnings, actual-versus-consensus EPS/revenue and usable daily prices.",
        "horizon": "Entry after the first post-event bar; 1, 3, 5 and 10-session holding variants.",
        "judge": "Event-time purged OOS study, surprise deciles, sector-neutral residual returns and realistic costs.",
        "data_needed": "Timestamped earnings, consensus estimates, EPS/revenue/margin surprises, stock and sector returns.",
        "kill_criteria": "No positive OOS expectancy after costs, timestamp leakage, or edge depends on fewer than five names.",
        "citations": [{"type": "corpus", "ref": "India PEAD Evidence"}, {"type": "corpus", "ref": "India PEAD Surprise Data Gap"}],
    },
    {
        "hypothesis": "A compressed-range breakout traded with defined-risk debit spreads captures volatility expansion without exposing the book to naked short gamma.",
        "who_pays": "Option sellers underprice abrupt post-compression directional movement, while defined-risk buyers cap failed-breakout loss.",
        "universe": "NIFTY, BANKNIFTY and SENSEX same-expiry call/put debit spreads with verified Upstox legs.",
        "horizon": "First 30-90 minutes; no expiry hold in the first validation.",
        "judge": "Intraday replay with quote-age, bid/ask, spread-width, slippage and volatility-regime stratification.",
        "data_needed": "1-minute underlying bars, intraday option bid/ask/LTP, IV, OI, instrument master and costs.",
        "kill_criteria": "OOS expectancy <= 3x modeled friction, false-breakout losses dominate, or quote data is incomplete.",
        "citations": [{"type": "corpus", "ref": "Defined Risk First"}, {"type": "corpus", "ref": "India Options Liquidity"}],
    },
    {
        "hypothesis": "Extreme near-versus-far implied-volatility richness mean-reverts through a delta-controlled calendar spread after event and liquidity exclusions.",
        "who_pays": "Short-dated event demand temporarily overprices one expiry relative to the adjacent maturity.",
        "universe": "NIFTY/BANKNIFTY/SENSEX same-strike same-type two-expiry calendars with verified legs.",
        "horizon": "1-5 sessions, forced exit before near-expiry gamma becomes dominant.",
        "judge": "IV-surface OOS replay with term-structure z-score, delta/vega exposure, event exclusions and all-in costs.",
        "data_needed": "Historical option chains by strike/expiry, IV, bid/ask, greeks, events and expiry settlement.",
        "kill_criteria": "No convergence after costs, unstable sign by expiry bucket, or missing intraday surface data.",
        "citations": [{"type": "corpus", "ref": "Calendar Spread Caveats"}, {"type": "corpus", "ref": "IV Surface Richness"}],
    },
    {
        "hypothesis": "Opening gaps followed by breadth and VWAP confirmation have distinct continuation and fade expectancy by gap-size and volatility regime.",
        "who_pays": "Overnight information is gradually incorporated, but extreme gaps can mean-revert when the opening auction exhausts demand.",
        "universe": "NIFTY, BANKNIFTY and liquid stock futures; separate continuation and fade variants.",
        "horizon": "Open through 15:00, with no overnight hold in the first test.",
        "judge": "Purged event study by gap bucket, opening range, breadth, VWAP and India VIX regime with opening slippage.",
        "data_needed": "Daily OHLC, 1-minute opening bars, breadth, VWAP, futures basis, events and realistic open fills.",
        "kill_criteria": "Continuation/fade sign flips across periods, edge disappears after open slippage, or event days dominate.",
        "citations": [{"type": "corpus", "ref": "India Day Night Option Returns"}, {"type": "corpus", "ref": "Regime First Options"}],
    },
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", default=os.environ.get("QUANTG_OWNER_USER_ID"))
    args = parser.parse_args()
    if not args.user_id:
        raise SystemExit("--user-id is required")
    corpus = corpus_status()
    cards = [build_hypothesis_card(card, corpus=corpus) for card in CARDS]
    import pymongo

    db = pymongo.MongoClient(os.environ.get("MONGO_URL", "mongodb://mongo:27017"), serverSelectionTimeoutMS=5000)[os.environ.get("DB_NAME", "quantg")]
    count = persist_hypothesis_cards(db, args.user_id, cards)
    print({"persisted": count, "strategy_registry_changes": 0, "live_changes": 0, "paper_wake": 0})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
