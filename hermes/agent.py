import os
import sys
import time
import re
import json
from datetime import datetime, timezone, timedelta
import requests
from dotenv import load_dotenv

# Load environment variables (fallback to local .env.hermes if exists)
load_dotenv(".env.hermes")

# Configurations
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
QUANTG_BACKEND_URL = os.getenv("QUANTG_BACKEND_URL", "http://backend:8000/api")
QUANTG_OPERATOR_EMAIL = os.getenv("QUANTG_OPERATOR_EMAIL")
QUANTG_OPERATOR_PASSWORD = os.getenv("QUANTG_OPERATOR_PASSWORD")
HEALTH_PATH = os.getenv("HERMES_HEALTH_PATH", "/tmp/hermes_health.json")
_telegram_failures = 0
_error_log_at = {}

# Rate limit watchdog alerts: alert once per hour per type
last_alert_sent = {}
ALERT_COOLDOWN_SECONDS = 3600

# Behavioral alerts configurations (TASK-H024)
DROUGHT_CUTOFF_IST = os.getenv("DROUGHT_CUTOFF_IST", "12:00")
DRAWDOWN_ALERT_FRAC = float(os.getenv("DRAWDOWN_ALERT_FRAC", "0.8"))
LOSS_STREAK_N = int(os.getenv("LOSS_STREAK_N", "3"))

def _safe_error(exc):
    message = str(exc)
    if TELEGRAM_BOT_TOKEN:
        message = message.replace(TELEGRAM_BOT_TOKEN, "[REDACTED]")
    return message


def _log_error(key, message):
    now = time.time()
    if now - _error_log_at.get(key, 0) >= 60:
        print(message)
        _error_log_at[key] = now


def _write_health():
    payload = {
        "heartbeat_at": datetime.now(timezone.utc).isoformat(),
        "telegram_failures": _telegram_failures,
        "status": "ok" if _telegram_failures < 5 else "degraded",
    }
    temp = HEALTH_PATH + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(temp, HEALTH_PATH)


def _validate_config():
    required = {
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
        "QUANTG_OPERATOR_EMAIL": QUANTG_OPERATOR_EMAIL,
        "QUANTG_OPERATOR_PASSWORD": QUANTG_OPERATOR_PASSWORD,
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise RuntimeError("Missing required Hermes configuration: " + ", ".join(missing))


class QuantGClient:
    def __init__(self, base_url, email, password):
        self.base_url = base_url.rstrip('/')
        self.email = email
        self.password = password
        self.token = None

    def login(self):
        url = f"{self.base_url}/auth/login"
        payload = {"email": self.email, "password": self.password}
        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code == 200:
                self.token = r.json().get("access_token")
                print(f"[AUTH] Logged in successfully as {self.email}")
                return True
            else:
                print(f"[AUTH] Login failed with status code {r.status_code}: {r.text}")
                return False
        except Exception as e:
            print(f"[AUTH] Exception during login: {e}")
            return False

    def request(self, method, path, **kwargs):
        if not self.token:
            if not self.login():
                return None
        
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.token}"
        headers["Content-Type"] = "application/json"
        
        try:
            r = requests.request(method, url, headers=headers, timeout=15, **kwargs)
            if r.status_code in (401, 403):
                print("[AUTH] Token expired, attempting re-login...")
                if self.login():
                    headers["Authorization"] = f"Bearer {self.token}"
                    r = requests.request(method, url, headers=headers, timeout=15, **kwargs)
                else:
                    return None
            return r
        except Exception as e:
            print(f"[CLIENT] Request exception: {e}")
            return None

client = QuantGClient(QUANTG_BACKEND_URL, QUANTG_OPERATOR_EMAIL, QUANTG_OPERATOR_PASSWORD)

def send_telegram_alert(text):
    global _telegram_failures
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TELEGRAM] Missing bot token or chat ID, cannot send message.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            _telegram_failures = 0
            print("[TELEGRAM] Message sent successfully")
            return True
        else:
            _telegram_failures += 1
            _log_error("telegram_send_status", f"[TELEGRAM] Send failed with status code {r.status_code}")
            return False
    except Exception as e:
        _telegram_failures += 1
        _log_error("telegram_send", f"[TELEGRAM] Exception sending alert: {_safe_error(e)}")
        return False

def is_market_hours():
    """True if current time in IST is within weekdays 09:15 - 15:30."""
    now_utc = datetime.now(timezone.utc)
    ist = now_utc + timedelta(hours=5, minutes=30)
    if ist.weekday() >= 5:
        return False
    minutes_now = ist.hour * 60 + ist.minute
    return (9 * 60 + 15) <= minutes_now <= (15 * 60 + 30)

def should_rate_limit(alert_key):
    now = time.time()
    last_sent = last_alert_sent.get(alert_key, 0)
    if now - last_sent < ALERT_COOLDOWN_SECONDS:
        return True
    last_alert_sent[alert_key] = now
    return False

def run_watchdog():
    """Checks token health and feed stall status during market hours."""
    if not is_market_hours():
        return

    print("[WATCHDOG] Checking feed and session health...")
    r = client.request("GET", "/core/feed-status")
    if not r or r.status_code != 200:
        print("[WATCHDOG] Failed to retrieve feed status from backend.")
        return

    data = r.json()
    connected = data.get("connected", False)
    token_valid = data.get("token_valid", False)
    feed_stalled = data.get("feed_stalled", False)
    feed_stalled_reason = data.get("feed_stalled_reason", "No reason provided")

    if not token_valid:
        if not should_rate_limit("token_expired"):
            send_telegram_alert("⚠️ *QuantG Hermes Watchdog*:\nUpstox session token is *EXPIRED* or invalid. Action required: Re-authenticate through Broker Keys.")
    elif not connected:
        if not should_rate_limit("feed_disconnected"):
            send_telegram_alert("⚠️ *QuantG Hermes Watchdog*:\nUpstox live price feed is *DISCONNECTED*. Auto-trading may be halted.")
    elif feed_stalled:
        if not should_rate_limit("feed_stalled"):
            send_telegram_alert(f"⚠️ *QuantG Hermes Watchdog*:\nUpstox live price feed is *STALLED*.\nReason: `{feed_stalled_reason}`\nAuto-trading blocks active signals.")

def run_behavior_watch():
    """Checks for trading anomalies: trade drought, drawdown warnings, and loss streaks during market hours."""
    if not is_market_hours():
        return

    print("[BEHAVIOR_WATCH] Checking trading behavior and risk drawdowns...")
    
    # 1. Fetch current portfolio stats for P&L and live strategies
    r_portfolio = client.request("GET", "/portfolio")
    if not r_portfolio or r_portfolio.status_code != 200:
        print("[BEHAVIOR_WATCH] Failed to retrieve portfolio from backend.")
        return
    port_data = r_portfolio.json()
    total_pnl = float(port_data.get("total_pnl") or 0.0)
    live_strategies_count = int(port_data.get("live_strategies") or 0)

    # 2. Fetch current profile settings for daily loss limit
    r_profile = client.request("GET", "/profile")
    if not r_profile or r_profile.status_code != 200:
        print("[BEHAVIOR_WATCH] Failed to retrieve user settings from backend.")
        return
    profile_data = r_profile.json()
    max_daily_loss = float(profile_data.get("max_daily_loss") or 0.0)

    # 3. Fetch today's orders
    r_orders = client.request("GET", "/core/orders")
    if not r_orders or r_orders.status_code != 200:
        print("[BEHAVIOR_WATCH] Failed to retrieve orders from backend.")
        return
    orders = r_orders.json()

    now_utc = datetime.now(timezone.utc)
    ist = now_utc + timedelta(hours=5, minutes=30)
    ist_today_start = ist.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = ist_today_start - timedelta(hours=5, minutes=30)

    # Filter for orders filled today
    todays_filled_orders = []
    for o in orders:
        created_at_str = o.get("created_at")
        if not created_at_str:
            continue
        try:
            dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            if dt >= today_start_utc and o.get("status") in {"FILLED", "COMPLETE", "CLOSED"}:
                todays_filled_orders.append(o)
        except Exception:
            pass

    fills_count = len(todays_filled_orders)

    # A. Drawdown Alert
    if max_daily_loss > 0.0 and total_pnl <= -abs(max_daily_loss * DRAWDOWN_ALERT_FRAC):
        if not should_rate_limit("drawdown_warning"):
            pnl_formatted = f"Rs {total_pnl:,.2f}"
            limit_formatted = f"Rs {max_daily_loss:,.2f}"
            send_telegram_alert(
                f"⚠️ *QuantG Risk Alert: Drawdown Breach*\n"
                f"Today's total P&L is `{pnl_formatted}` which has breached "
                f"*{DRAWDOWN_ALERT_FRAC * 100:.0f}%* of your daily loss limit (`{limit_formatted}`)."
            )

    # B. No-Trade Drought Alert
    try:
        cutoff_hour, cutoff_min = map(int, DROUGHT_CUTOFF_IST.split(":"))
        cutoff_minutes = cutoff_hour * 60 + cutoff_min
        ist_minutes = ist.hour * 60 + ist.minute
        if ist_minutes >= cutoff_minutes and fills_count == 0 and live_strategies_count >= 1:
            # Check if feed is healthy
            r_feed = client.request("GET", "/core/feed-status")
            if r_feed and r_feed.status_code == 200:
                feed_data = r_feed.json()
                feed_ok = (
                    feed_data.get("connected", False)
                    and feed_data.get("token_valid", False)
                    and not feed_data.get("feed_stalled", False)
                )
                if feed_ok:
                    if not should_rate_limit("trade_drought"):
                        send_telegram_alert(
                            f"🔔 *QuantG Operator Alert: Trade Drought*\n"
                            f"There are *0 fills* recorded by `{DROUGHT_CUTOFF_IST}` IST today. "
                            f"The price feed is *HEALTHY* and *{live_strategies_count}* strategies are armed. "
                            f"Verify strategy logic and signal generation."
                        )
    except Exception as e:
        print(f"[BEHAVIOR_WATCH] Error evaluating no-trade drought: {e}")

    # C. Loss Streak Alert (Consecutive SL/Losing trades per strategy today)
    strategy_orders = {}
    for o in todays_filled_orders:
        strat_id = o.get("strategy_id")
        if not strat_id:
            continue
        strategy_orders.setdefault(strat_id, []).append(o)

    for strat_id, s_orders in strategy_orders.items():
        s_orders.sort(key=lambda x: x.get("created_at") or "")
        
        current_streak = 0
        for o in reversed(s_orders):
            is_exit = False
            pnl = 0.0
            if o.get("idempotency_key") and "exit" in str(o.get("idempotency_key")).lower():
                is_exit = True
                pnl = float(o.get("net_pnl") or o.get("realized_pnl") or 0.0)
            elif o.get("net_pnl") is not None and float(o.get("net_pnl")) != 0.0:
                is_exit = True
                pnl = float(o.get("net_pnl"))
            elif o.get("realized_pnl") is not None and float(o.get("realized_pnl")) != 0.0:
                is_exit = True
                pnl = float(o.get("realized_pnl"))
                
            if is_exit:
                if pnl < 0.0:
                    current_streak += 1
                else:
                    break
                    
        if current_streak >= LOSS_STREAK_N:
            alert_key = f"loss_streak_{strat_id}"
            if not should_rate_limit(alert_key):
                send_telegram_alert(
                    f"⚠️ *QuantG Risk Alert: Loss Streak Breach*\n"
                    f"Strategy *{strat_id}* has hit *{current_streak} consecutive losses* today."
                )

BRIEFING_QUERY = (
    "Morning briefing. Give me the THREE things that matter most for trading today, "
    "ranked by impact (risk/drawdown first, then trade-readiness, then info). For each: a one-line "
    "grounded finding citing the tool/source, and if action is warranted, propose ONE governed action. "
    "Use historical context and your recall memory to flag anything that is a repeat or a trend "
    "(e.g. 'drought 3rd day', 'this strategy lost 4 sessions running'). Check external context for "
    "today's event risk (expiry, RBI/Fed, results) and label it external/unverified. Be concise — "
    "this is a briefing, not a status dump."
)


def run_morning_briefing(date_str):
    """HSB-07: proactive, prioritized daily briefing (ranked top-3, not a status dump).

    Reuses the full Hermes brain (memory recall, historical context, scorecard, external
    grounding) via /agent/chat so the briefing is grounded and self-improving over time.
    """
    print(f"[BRIEFING] Compiling prioritized morning briefing for {date_str}...")
    reply = get_agent_chat_reply(BRIEFING_QUERY)
    msg = (
        f"🧭 *Hermes Morning Briefing*\n"
        f"Date: `{date_str}`\n\n"
        f"{reply}"
    )
    send_telegram_alert(msg)


def run_premarket_check(date_str):
    """Generates the pre-market readiness report."""
    print(f"[PREMARKET] Compiling readiness report for {date_str}...")
    r = client.request("GET", "/trading/live-readiness")
    if not r or r.status_code != 200:
        send_telegram_alert(f"🛑 *QuantG Hermes Alert*:\nFailed to compile pre-market readiness report for `{date_str}`. API request error.")
        return

    data = r.json()
    checks = data.get("checks", [])
    mode = data.get("current_mode", "PAPER")
    overall_ok = data.get("ok", False)

    status_emoji = "✅ READY" if overall_ok else "🛑 BLOCKED"
    
    checks_list = []
    for c in checks:
        if c.get("id") == "market_hours":
            continue
        icon = "✅" if c.get("ok") else "❌"
        label = c.get("label", c.get("id"))
        detail = c.get("detail")
        hint = c.get("hint")
        
        line = f"{icon} {label}"
        if not c.get("ok") and hint:
            line += f"\n   _(Hint: {hint})_"
        elif detail:
            line += f"\n   _({detail})_"
        checks_list.append(line)

    checks_formatted = "\n".join(checks_list)
    msg = f"🔔 *Hermes Pre-Market Readiness Report*\n" \
          f"Date: `{date_str}`\n" \
          f"Mode: `{mode}`\n\n" \
          f"*System Status checks*:\n" \
          f"{checks_formatted}\n\n" \
          f"Overall Status: *{status_emoji}*"
          
    send_telegram_alert(msg)

def run_eod_report(date_str):
    """Compiles daily trading statistics at market close."""
    print(f"[EOD] Compiling trading report for {date_str}...")
    
    # Retry loop to allow daily report collection job to finish on backend
    report_doc = None
    for attempt in range(5):
        r = client.request("GET", f"/reports/daily/{date_str}")
        if r and r.status_code == 200:
            doc = r.json()
            if doc.get("generated_at") is not None:
                report_doc = doc
                break
        print(f"[EOD] Report document not yet compiled. Retrying in 10s... (attempt {attempt+1}/5)")
        time.sleep(10)

    if not report_doc:
        send_telegram_alert(f"⚠️ *QuantG Hermes Alert*:\nEnd-of-Day report for `{date_str}` could not be retrieved or has not been compiled yet.")
        return

    realized = report_doc.get("total_realized_pnl", 0.0)
    unrealized = report_doc.get("total_unrealized_pnl", 0.0)
    total_pnl = realized + unrealized
    trades = report_doc.get("trades_taken", 0)
    fired = report_doc.get("signals_fired", 0)
    filtered = report_doc.get("signals_filtered", 0)
    regime = report_doc.get("market_regime", "UNKNOWN")
    best = report_doc.get("best_strategy")
    worst = report_doc.get("worst_strategy")
    strategies = report_doc.get("strategies", [])

    pnl_sign = "+" if total_pnl >= 0 else ""
    pnl_formatted = f"{pnl_sign}Rs {total_pnl:,.2f}"

    regime_desc = str(regime).upper()

    strat_list = []
    for s in strategies:
        s_pnl = s.get("pnl", 0.0)
        s_sign = "+" if s_pnl >= 0 else ""
        strat_list.append(f"• *{s.get('name')}*: {s_sign}Rs {s_pnl:,.2f} ({s.get('trade_count', 0)} trades)")
    
    strategies_formatted = "\n".join(strat_list) if strat_list else "No active strategy trades."

    msg = f"📊 *Hermes EOD Trading Report*\n" \
          f"Date: `{date_str}`\n" \
          f"Regime: *{regime_desc}*\n\n" \
          f"*Performance Summary*:\n" \
          f"• *Total Daily P&L*: `{pnl_formatted}`\n" \
          f"  _(Realized: Rs {realized:,.2f} / Unrealized: Rs {unrealized:,.2f})_\n" \
          f"• *Trades Filled*: `{trades}`\n" \
          f"• *Signals Processed*: `{fired}` _(Filtered/Blocked: {filtered})_\n\n" \
          f"*Per-Strategy Performance*:\n" \
          f"{strategies_formatted}"

    if best and best.get("pnl", 0) > 0:
        msg += f"\n\n⭐ *Best*: {best.get('name')} (+Rs {best.get('pnl'):,.2f})"
    if worst and worst.get("pnl", 0) < 0:
        msg += f"\n\n⚠️ *Worst*: {worst.get('name')} (Rs {worst.get('pnl'):,.2f})"

    send_telegram_alert(msg)


def init_telegram_offset():
    global _telegram_failures
    if not TELEGRAM_BOT_TOKEN:
        return 0
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        r = requests.get(url, params={"offset": -1, "timeout": 0}, timeout=5)
        if r.status_code == 200:
            _telegram_failures = 0
            data = r.json()
            results = data.get("result", [])
            if results:
                return results[-1]["update_id"] + 1
    except Exception as e:
        _telegram_failures += 1
        _log_error("telegram_init", f"[TELEGRAM] Error initializing offset: {_safe_error(e)}")
    return 0


def get_status_command_reply():
    now_utc = datetime.now(timezone.utc)
    ist = now_utc + timedelta(hours=5, minutes=30)
    today_str = ist.strftime("%Y-%m-%d")
    
    # 1. Fetch feed status
    feed_detail = ""
    r_feed = client.request("GET", "/core/feed-status")
    if r_feed and r_feed.status_code == 200:
        f_data = r_feed.json()
        f_conn = f_data.get("connected", False)
        f_tok = f_data.get("token_valid", False)
        f_stall = f_data.get("feed_stalled", False)
        f_stall_reason = f_data.get("feed_stalled_reason") or "No reason provided"
        
        feed_icon = "✅" if (f_conn and f_tok and not f_stall) else "❌"
        feed_st = "CONNECTED" if f_conn else "DISCONNECTED"
        if not f_tok:
            feed_st += " (TOKEN EXPIRED)"
        if f_stall:
            feed_st += f" (STALLED: {f_stall_reason})"
        feed_detail = f"{feed_icon} Upstox Feed: *{feed_st}*\n"
    else:
        feed_detail = "❌ Upstox Feed: *UNKNOWN (API Error)*\n"

    # 2. Fetch live readiness
    r_readiness = client.request("GET", "/trading/live-readiness")
    if not r_readiness or r_readiness.status_code != 200:
        return "🛑 *QuantG Hermes Alert*:\nFailed to compile status report. API request error."

    data = r_readiness.json()
    checks = data.get("checks", [])
    mode = data.get("current_mode", "PAPER")
    overall_ok = data.get("ok", False)

    status_emoji = "✅ READY" if overall_ok else "🛑 BLOCKED"
    
    checks_list = []
    for c in checks:
        if c.get("id") == "market_hours":
            continue
        icon = "✅" if c.get("ok") else "❌"
        label = c.get("label", c.get("id"))
        detail = c.get("detail")
        hint = c.get("hint")
        
        line = f"{icon} {label}"
        if not c.get("ok") and hint:
            line += f"\n   _(Hint: {hint})_"
        elif detail:
            line += f"\n   _({detail})_"
        checks_list.append(line)

    checks_formatted = "\n".join(checks_list)
    msg = f"🔔 *Hermes Status Report*\n" \
          f"Date: `{today_str}`\n" \
          f"Mode: `{mode}`\n\n" \
          f"{feed_detail}" \
          f"*System Status checks*:\n" \
          f"{checks_formatted}\n\n" \
          f"Overall Status: *{status_emoji}*"
    return msg


def get_pnl_command_reply():
    now_utc = datetime.now(timezone.utc)
    ist = now_utc + timedelta(hours=5, minutes=30)
    today_str = ist.strftime("%Y-%m-%d")
    
    r = client.request("GET", f"/reports/daily/{today_str}")
    if not r or r.status_code != 200:
        return f"⚠️ *QuantG Hermes Alert*:\nDaily P&L report for `{today_str}` could not be retrieved. API request error."
        
    report_doc = r.json()
    
    realized = report_doc.get("total_realized_pnl", 0.0)
    unrealized = report_doc.get("total_unrealized_pnl", 0.0)
    total_pnl = realized + unrealized
    trades = report_doc.get("trades_taken", 0)
    fired = report_doc.get("signals_fired", 0)
    filtered = report_doc.get("signals_filtered", 0)
    regime = report_doc.get("market_regime") or "UNKNOWN"
    best = report_doc.get("best_strategy")
    worst = report_doc.get("worst_strategy")
    strategies = report_doc.get("strategies", [])

    pnl_sign = "+" if total_pnl >= 0 else ""
    pnl_formatted = f"{pnl_sign}Rs {total_pnl:,.2f}"

    regime_desc = str(regime).upper()

    strat_list = []
    for s in strategies:
        s_pnl = s.get("realized_pnl") or s.get("pnl") or 0.0
        s_sign = "+" if s_pnl >= 0 else ""
        strat_list.append(f"• *{s.get('name')}*: {s_sign}Rs {s_pnl:,.2f} ({s.get('trade_count', 0)} trades)")
    
    strategies_formatted = "\n".join(strat_list) if strat_list else "No active strategy trades."

    msg = f"📊 *Hermes Daily P&L Report*\n" \
          f"Date: `{today_str}`\n" \
          f"Regime: *{regime_desc}*\n\n" \
          f"*Performance Summary*:\n" \
          f"• *Total Daily P&L*: `{pnl_formatted}`\n" \
          f"  _(Realized: Rs {realized:,.2f} / Unrealized: Rs {unrealized:,.2f})_\n" \
          f"• *Trades Filled*: `{trades}`\n" \
          f"• *Signals Processed*: `{fired}` _(Filtered/Blocked: {filtered})_\n\n" \
          f"*Per-Strategy Performance*:\n" \
          f"{strategies_formatted}"

    if best and best.get("pnl", 0) > 0:
        msg += f"\n\n⭐ *Best*: {best.get('name')} (+Rs {best.get('pnl'):,.2f})"
    if worst and worst.get("pnl", 0) < 0:
        msg += f"\n\n⚠️ *Worst*: {worst.get('name')} (Rs {worst.get('pnl'):,.2f})"

    return msg


def get_agent_chat_reply(text):
    payload = {
        "session_id": "telegram_session",
        "message": text
    }
    r = client.request("POST", "/agent/chat", json=payload)
    if not r or r.status_code != 200:
        return "🛑 *QuantG Hermes Alert*:\nFailed to get reply from Hermes agent. API request error."
        
    data = r.json()
    reply_text = data.get("content", "").strip()
    
    # Strip any PROPOSED_ACTION: {...} text if it is still present in reply_text
    reply_text = re.sub(r"PROPOSED_ACTION:\s*(\{.*\})", "", reply_text, flags=re.DOTALL).strip()
    
    # Check if a proposed action was returned (either in text or in pending_action)
    has_action = data.get("pending_action") is not None
    if has_action:
        reply_text += "\n\n*(Action proposed: Please approve in-app)*"
        
    # Append the sources footer
    tools = data.get("tools_used", [])
    tool_names = [t.get("name") for t in tools if t.get("status") == "ok"]
    # De-duplicate names keeping order
    seen = set()
    unique_tool_names = []
    for name in tool_names:
        if name not in seen:
            seen.add(name)
            unique_tool_names.append(name)
            
    sources_str = ", ".join(unique_tool_names) if unique_tool_names else "none"
    reply_text += f"\nsources: {sources_str}"
    
    return reply_text


def handle_incoming_message(message):
    text = message.get("text", "").strip()
    if not text:
        return
    
    print(f"[TELEGRAM] Processing message from authorized chat: {text}")
    
    try:
        if text.startswith("/status"):
            reply = get_status_command_reply()
            send_telegram_alert(reply)
        elif text.startswith("/pnl"):
            reply = get_pnl_command_reply()
            send_telegram_alert(reply)
        elif text.startswith("/brief"):
            now_utc = datetime.now(timezone.utc)
            ist = now_utc + timedelta(hours=5, minutes=30)
            run_morning_briefing(ist.strftime("%Y-%m-%d"))
        else:
            # Handle "/why" prefix by stripping it, or general text
            query_text = text
            if text.startswith("/why"):
                query_text = text[4:].strip()
                if not query_text:
                    send_telegram_alert("❓ *Hermes Analyst*:\nPlease provide a question after /why.")
                    return
            reply = get_agent_chat_reply(query_text)
            send_telegram_alert(reply)
    except Exception as e:
        print(f"[TELEGRAM] Error handling message '{text}': {e}")
        send_telegram_alert(f"⚠️ *QuantG Hermes Alert*:\nError processing command: `{e}`")


def poll_telegram_updates(offset):
    global _telegram_failures
    if not TELEGRAM_BOT_TOKEN:
        return offset
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"offset": offset, "timeout": 2}
    try:
        r = requests.get(url, params=params, timeout=7)
        if r.status_code == 200:
            _telegram_failures = 0
            data = r.json()
            updates = data.get("result", [])
            for update in updates:
                offset = max(offset, update["update_id"] + 1)
                
                message = update.get("message")
                if not message:
                    continue
                
                chat = message.get("chat")
                if not chat:
                    continue
                
                chat_id = str(chat.get("id"))
                if not TELEGRAM_CHAT_ID or chat_id.strip() != str(TELEGRAM_CHAT_ID).strip():
                    print(f"[TELEGRAM] Ignored message from unauthorized chat_id: {chat_id}")
                    continue
                
                handle_incoming_message(message)
    except Exception as e:
        _telegram_failures += 1
        _log_error("telegram_poll", f"[TELEGRAM] Error polling updates: {_safe_error(e)}")
    return offset


def run_weekly_ranking_report(date_str):
    """Compiles the weekly strategy ranking report."""
    print(f"[WEEKLY_RANK] Compiling strategy ranking report for {date_str}...")
    r = client.request("GET", "/ops/risk-scorecard")
    if not r or r.status_code != 200:
        send_telegram_alert(f"⚠️ *QuantG Hermes Alert*:\nFailed to compile weekly strategy ranking report for `{date_str}`. API request error.")
        return

    res_data = r.json()
    rows = res_data.get("rows", [])
    by_structure = res_data.get("by_structure", {})

    if not rows:
        send_telegram_alert(f"📊 *Hermes Weekly Strategy Ranking*\nDate: `{date_str}`\nNo strategy trade history found to rank.")
        return

    # 1. Format individual strategy rows
    lines = []
    # Rank top 5 strategies
    for idx, strat in enumerate(rows[:5]):
        name = strat.get("name", "Unknown")
        grade_val = strat.get("grade", "F")
        sharpe = strat.get("sharpe", 0.0)
        exp = strat.get("expectancy", 0.0)
        pnl = strat.get("total_pnl", 0.0)
        trades = strat.get("total_trades", 0)
        win_rate = strat.get("win_rate", 0.0) * 100
        
        medal = "🥇" if idx == 0 else "🥈" if idx == 1 else "🥉" if idx == 2 else "•"
        lines.append(
            f"{medal} *{name}* (Grade: *{grade_val}*)\n"
            f"  Sharpe: `{sharpe:.2f}` | Expectancy: `Rs {exp:.2f}`\n"
            f"  P&L: `Rs {pnl:,.2f}` | Trades: `{trades}` | Win: `{win_rate:.1f}%`"
        )
    strategies_formatted = "\n\n".join(lines)

    # 2. Format structure-level summary (credit spreads vs single leg buyer)
    summary_lines = []
    for structure, s_info in by_structure.items():
        s_pnl = s_info.get("total_pnl", 0.0)
        s_sign = "+" if s_pnl >= 0 else ""
        s_rate = s_info.get("win_rate", 0.0) * 100
        summary_lines.append(
            f"• *{structure}*: {s_sign}Rs {s_pnl:,.2f} ({s_info.get('total_trades', 0)} trades, Win: {s_rate:.1f}%)"
        )
    summary_formatted = "\n".join(summary_lines) if summary_lines else "No structured trade history."

    msg = f"📊 *Hermes Weekly Strategy Ranking*\n" \
          f"Date: `{date_str}`\n\n" \
          f"*Top Deployed Strategies*:\n" \
          f"{strategies_formatted}\n\n" \
          f"*Performance by Structure*:\n" \
          f"{summary_formatted}"
          
    send_telegram_alert(msg)


def run_loop():
    _validate_config()
    last_watchdog_run = 0
    last_premarket_date = None
    last_briefing_date = None
    last_weekly_rank_date = None
    last_eod_date = None
    telegram_offset = 0
    
    # Initialize telegram offset on startup to ignore past messages
    telegram_offset = init_telegram_offset()
    
    print("[AGENT] Hermes Sidecar Agent started successfully.")
    
    # Test connection and send startup notification
    startup_msg = "🚀 *Hermes Sidecar Agent* initialized and connected successfully on the VPS."
    if not send_telegram_alert(startup_msg):
        print("[WARNING] Failed to send Telegram startup notification. Check BOT_TOKEN and CHAT_ID.")
    
    while True:
        try:
            _write_health()
            now_utc = datetime.now(timezone.utc)
            ist = now_utc + timedelta(hours=5, minutes=30)
            today_str = ist.strftime("%Y-%m-%d")
            
            # 1. Watchdog: run every 3 minutes
            now_ts = time.time()
            if now_ts - last_watchdog_run >= 180:
                run_watchdog()
                run_behavior_watch()
                last_watchdog_run = now_ts
                
            # 2. Pre-market Check: 09:00 IST on weekdays
            if ist.weekday() < 5:  # Monday to Friday
                if ist.hour == 9 and ist.minute == 0 and last_premarket_date != today_str:
                    run_premarket_check(today_str)
                    last_premarket_date = today_str

                # HSB-07 Prioritized Morning Briefing: 09:05 IST weekdays (after readiness)
                if ist.hour == 9 and ist.minute == 5 and last_briefing_date != today_str:
                    run_morning_briefing(today_str)
                    last_briefing_date = today_str

                # Weekly Strategy Ranking: Friday at 09:00 IST (TASK-H015)
                # Note: 4 is Friday (0 is Mon, 1 is Tue, 2 is Wed, 3 is Thu, 4 is Fri)
                if ist.weekday() == 4 and ist.hour == 9 and ist.minute == 0 and last_weekly_rank_date != today_str:
                    run_weekly_ranking_report(today_str)
                    last_weekly_rank_date = today_str
                    
                # 3. EOD Report: 15:35 IST on weekdays
                if ist.hour == 15 and ist.minute == 35 and last_eod_date != today_str:
                    run_eod_report(today_str)
                    last_eod_date = today_str
            
            # 4. Poll Telegram Updates
            telegram_offset = poll_telegram_updates(telegram_offset)
            
        except Exception as e:
            print(f"[AGENT] Exception in main loop: {e}")
            
        time.sleep(min(60, max(1, 2 ** min(_telegram_failures, 6))))


if __name__ == "__main__":
    run_loop()
