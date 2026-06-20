import os
import sys
import time
import re
from datetime import datetime, timezone, timedelta
import requests
from dotenv import load_dotenv

# Load environment variables (fallback to local .env.hermes if exists)
load_dotenv(".env.hermes")

# Configurations
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
QUANTG_BACKEND_URL = os.getenv("QUANTG_BACKEND_URL", "http://backend:8000/api")
QUANTG_OPERATOR_EMAIL = os.getenv("QUANTG_OPERATOR_EMAIL", "demo@quantdesk.io")
QUANTG_OPERATOR_PASSWORD = os.getenv("QUANTG_OPERATOR_PASSWORD", "demo1234")

# Rate limit watchdog alerts: alert once per hour per type
last_alert_sent = {}
ALERT_COOLDOWN_SECONDS = 3600

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
            print("[TELEGRAM] Message sent successfully")
            return True
        else:
            print(f"[TELEGRAM] Send failed with status code {r.status_code}: {r.text}")
            return False
    except Exception as e:
        print(f"[TELEGRAM] Exception sending alert: {e}")
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
    if not TELEGRAM_BOT_TOKEN:
        return 0
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        r = requests.get(url, params={"offset": -1, "timeout": 0}, timeout=5)
        if r.status_code == 200:
            data = r.json()
            results = data.get("result", [])
            if results:
                return results[-1]["update_id"] + 1
    except Exception as e:
        print(f"[TELEGRAM] Error initializing offset: {e}")
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
    if not TELEGRAM_BOT_TOKEN:
        return offset
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"offset": offset, "timeout": 2}
    try:
        r = requests.get(url, params=params, timeout=7)
        if r.status_code == 200:
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
        print(f"[TELEGRAM] Error polling updates: {e}")
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
    last_watchdog_run = 0
    last_premarket_date = None
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
            now_utc = datetime.now(timezone.utc)
            ist = now_utc + timedelta(hours=5, minutes=30)
            today_str = ist.strftime("%Y-%m-%d")
            
            # 1. Watchdog: run every 3 minutes
            now_ts = time.time()
            if now_ts - last_watchdog_run >= 180:
                run_watchdog()
                last_watchdog_run = now_ts
                
            # 2. Pre-market Check: 09:00 IST on weekdays
            if ist.weekday() < 5:  # Monday to Friday
                if ist.hour == 9 and ist.minute == 0 and last_premarket_date != today_str:
                    run_premarket_check(today_str)
                    last_premarket_date = today_str
                    
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
            
        time.sleep(1)


if __name__ == "__main__":
    run_loop()
