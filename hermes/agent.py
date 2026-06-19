import os
import sys
import time
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

def run_loop():
    last_watchdog_run = 0
    last_premarket_date = None
    last_eod_date = None
    
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
                    
                # 3. EOD Report: 15:35 IST on weekdays
                if ist.hour == 15 and ist.minute == 35 and last_eod_date != today_str:
                    run_eod_report(today_str)
                    last_eod_date = today_str
                    
        except Exception as e:
            print(f"[AGENT] Exception in main loop: {e}")
            
        time.sleep(10)

if __name__ == "__main__":
    run_loop()
