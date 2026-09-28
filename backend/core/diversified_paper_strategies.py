"""Executable, paper-only diversified strategy signal code.

The returned snippets are intentionally sandbox-compatible: no imports, network,
or database access. Execution, contract selection, risk, exits, and paper fills
remain owned by strategy_runner and signal_manager.
"""
from __future__ import annotations


PEAD_CODE = '''def run(data):
    if len(data) < 25:
        return []
    d = data[-1]
    closes = [float(x.get('close') or 0) for x in data]
    vols = [float(x.get('volume') or 0) for x in data]
    if not closes[-1] or not vols[-1]:
        return []
    # Earnings proxy: an opening gap followed by a close above the event-day close.
    prev = float(data[-2].get('close') or 0)
    op = float(d.get('open') or d.get('close') or 0)
    baseline = sum(closes[-21:-1]) / 20.0
    avg_vol = sum(vols[-21:-1]) / 20.0
    gap = (op - prev) / prev if prev else 0.0
    follow = (closes[-1] - op) / op if op else 0.0
    if gap < 0.015 or follow < 0.003 or closes[-1] <= baseline or vols[-1] < avg_vol * 1.25:
        return []
    return [{'date': d['date'], 'action': 'BUY', 'confidence': 72.0,
             'setup_type': 'pead_positive_gap_followthrough',
             'entry_reason': 'positive event-gap proxy with volume-confirmed continuation',
             'signal_version': 'pead-paper-v1'}]
'''

CROSS_SECTIONAL_MOMENTUM_CODE = '''def run(data):
    if len(data) < 40:
        return []
    d = data[-1]
    c = [float(x.get('close') or 0) for x in data]
    if not c[-1] or not c[-21] or not c[-40]:
        return []
    r20 = c[-1] / c[-21] - 1.0
    r40 = c[-1] / c[-40] - 1.0
    path = sum(abs(c[i] - c[i-1]) for i in range(len(c)-10, len(c))) or 1e-9
    efficiency = abs(c[-1] - c[-11]) / path
    if r20 < 0.02 or r40 < 0.03 or efficiency < 0.45:
        return []
    return [{'date': d['date'], 'action': 'BUY', 'direction': 'CE',
             'confidence': 70.0, 'setup_type': 'cross_sectional_momentum_proxy',
             'entry_reason': 'top-quintile momentum proxy: 20/40-bar return and efficiency passed',
             'signal_version': 'xsmom-paper-v1'}]
'''

VOL_BREAKOUT_DEBIT_CODE = '''def run(data):
    if len(data) < 25:
        return []
    d = data[-1]
    c = [float(x.get('close') or 0) for x in data]
    h = [float(x.get('high') or x.get('close') or 0) for x in data]
    l = [float(x.get('low') or x.get('close') or 0) for x in data]
    if not c[-1]:
        return []
    atr = sum(h[i] - l[i] for i in range(-14, 0)) / 14.0
    prior_hi = max(h[-21:-1])
    prior_lo = min(l[-21:-1])
    if c[-1] > prior_hi and c[-1] - prior_hi > atr * 0.15:
        direction = 'CE'
    elif c[-1] < prior_lo and prior_lo - c[-1] > atr * 0.15:
        direction = 'PE'
    else:
        return []
    return [{'date': d['date'], 'action': 'BUY' if direction == 'CE' else 'SELL',
             'direction': direction, 'confidence': 75.0,
             'setup_type': 'atr_volatility_breakout_debit_spread',
             'entry_reason': '20-bar range breakout exceeded ATR noise band',
             'signal_version': 'volbreak-paper-v1'}]
'''

IV_TERM_STRUCTURE_CODE = '''def run(data):
    if len(data) < 35:
        return []
    d = data[-1]
    c = [float(x.get('close') or 0) for x in data]
    if not c[-1] or not c[-6] or not c[-21]:
        return []
    short_move = abs(c[-1] / c[-6] - 1.0)
    long_move = abs(c[-1] / c[-21] - 1.0)
    # Price compression is the observable proxy; the option selector supplies
    # the current chain and spread geometry at execution time.
    if short_move > 0.012 or long_move > 0.04:
        return []
    return [{'date': d['date'], 'action': 'BUY', 'direction': 'CE',
             'confidence': 62.0, 'setup_type': 'iv_term_structure_calendar_proxy',
             'entry_reason': 'compressed realized move; execute defined-risk calendar proxy',
             'signal_version': 'ivterm-paper-v1'}]
'''

OVERNIGHT_GAP_CODE = '''def run(data):
    if len(data) < 22:
        return []
    d = data[-1]
    clock = str(d.get('date', ''))[11:16]
    if clock and clock < '14:45':
        return []
    c = [float(x.get('close') or 0) for x in data]
    if not c[-1] or not c[-2]:
        return []
    gap = (c[-1] - c[-2]) / c[-2]
    vol = sum(abs(c[i] / c[i-1] - 1.0) for i in range(len(c)-10, len(c))) / 10.0
    if abs(gap) < 0.006 or abs(gap) < vol * 1.25:
        return []
    # Carry the gap direction overnight with a defined-risk debit spread.
    direction = 'CE' if gap > 0 else 'PE'
    return [{'date': d['date'], 'action': 'BUY' if gap > 0 else 'SELL',
             'direction': direction, 'confidence': 68.0,
             'setup_type': 'overnight_gap_continuation',
             'entry_reason': 'late-session gap impulse exceeded recent noise; overnight debit',
             'signal_version': 'gap-paper-v1'}]
'''


STRATEGIES = [
    {"id": "paper-pead-reliance", "name": "Paper PEAD Stock Follow-Through",
     "symbol": "RELIANCE", "exchange": "NSE", "code": PEAD_CODE,
     "options": {"enabled": False}, "risk": {"max_hold_days": 3, "max_trades_day": 1}},
    {"id": "paper-cross-sectional-nifty", "name": "Paper Cross-Sectional Momentum",
     "symbol": "NIFTY", "exchange": "NFO", "code": CROSS_SECTIONAL_MOMENTUM_CODE,
     "options": {"enabled": True, "underlying": "NIFTY", "structure": "single_leg", "strike_mode": "ITM_BUY", "itm_offset_pct": 0.02},
     "risk": {"target_pct": 45.0, "stoploss_pct": 25.0, "max_hold_days": 3, "max_trades_day": 1}},
    {"id": "paper-vol-breakout-nifty", "name": "Paper Volatility Breakout Debit Spread",
     "symbol": "NIFTY", "exchange": "NFO", "code": VOL_BREAKOUT_DEBIT_CODE,
     "options": {"enabled": True, "underlying": "NIFTY", "structure": "debit_spread", "strike_mode": "OTM_BUY", "spread_width": 10, "wing_width": 10, "min_dte_days": 2, "max_dte_days": 10},
     "risk": {"target_pct": 60.0, "stoploss_pct": 45.0, "max_hold_days": 2, "max_trades_day": 1}},
    {"id": "paper-iv-term-nifty", "name": "Paper IV Term-Structure Calendar Proxy",
     "symbol": "NIFTY", "exchange": "NFO", "code": IV_TERM_STRUCTURE_CODE,
     "options": {"enabled": True, "underlying": "NIFTY", "structure": "debit_spread", "strike_mode": "ATM_BUY", "spread_width": 10, "wing_width": 10, "min_dte_days": 7, "max_dte_days": 30},
     "risk": {"target_pct": 35.0, "stoploss_pct": 35.0, "max_hold_days": 7, "max_trades_day": 1}},
    {"id": "paper-overnight-gap-nifty", "name": "Paper Overnight Gap Debit Spread",
     "symbol": "NIFTY", "exchange": "NFO", "code": OVERNIGHT_GAP_CODE,
     "options": {"enabled": True, "underlying": "NIFTY", "structure": "debit_spread", "strike_mode": "OTM_BUY", "spread_width": 10, "wing_width": 10, "min_dte_days": 2, "max_dte_days": 10},
     "risk": {"target_pct": 50.0, "stoploss_pct": 50.0, "max_hold_days": 2, "max_trades_day": 1}},
]
