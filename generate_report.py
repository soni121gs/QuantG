"""QuantG Full Application Report — PDF Generator (reportlab)"""

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm, cm
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT, TA_JUSTIFY
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak, KeepTogether
)
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor
from datetime import datetime
import os

# ── Color palette ──────────────────────────────────────────────────────────────
C_NAVY    = HexColor("#0D1B2A")
C_BLUE    = HexColor("#1565C0")
C_LIGHT   = HexColor("#1976D2")
C_ACCENT  = HexColor("#00ACC1")
C_GREEN   = HexColor("#2E7D32")
C_RED     = HexColor("#C62828")
C_AMBER   = HexColor("#F57F17")
C_GREY    = HexColor("#455A64")
C_LGREY   = HexColor("#ECEFF1")
C_WHITE   = colors.white
C_BLACK   = colors.black
C_DIVIDER = HexColor("#B0BEC5")

PAGE_W, PAGE_H = A4
MARGIN_L = 2.0 * cm
MARGIN_R = 2.0 * cm
MARGIN_T = 2.2 * cm
MARGIN_B = 2.0 * cm
INNER_W  = PAGE_W - MARGIN_L - MARGIN_R

# ── Styles ─────────────────────────────────────────────────────────────────────
def make_styles():
    base = getSampleStyleSheet()

    def s(name, parent="Normal", **kw):
        return ParagraphStyle(name, parent=base[parent], **kw)

    return {
        "cover_title": s("cover_title", "Title",
            fontSize=32, textColor=C_WHITE, alignment=TA_CENTER,
            leading=40, spaceAfter=6),
        "cover_sub": s("cover_sub",
            fontSize=14, textColor=HexColor("#90CAF9"), alignment=TA_CENTER,
            leading=20, spaceAfter=4),
        "cover_meta": s("cover_meta",
            fontSize=11, textColor=HexColor("#B0BEC5"), alignment=TA_CENTER,
            leading=16),
        "h1": s("h1",
            fontSize=18, textColor=C_BLUE, leading=24,
            spaceBefore=14, spaceAfter=6, fontName="Helvetica-Bold"),
        "h2": s("h2",
            fontSize=13, textColor=C_NAVY, leading=18,
            spaceBefore=10, spaceAfter=4, fontName="Helvetica-Bold"),
        "h3": s("h3",
            fontSize=11, textColor=C_GREY, leading=16,
            spaceBefore=8, spaceAfter=3, fontName="Helvetica-Bold"),
        "body": s("body",
            fontSize=9.5, textColor=C_BLACK, leading=15,
            spaceAfter=4, alignment=TA_JUSTIFY),
        "bullet": s("bullet",
            fontSize=9.5, textColor=C_BLACK, leading=14,
            leftIndent=14, spaceAfter=2),
        "code": s("code",
            fontSize=8.2, fontName="Courier", textColor=HexColor("#212121"),
            backColor=HexColor("#F5F5F5"), leading=13,
            leftIndent=10, rightIndent=10, spaceAfter=2),
        "caption": s("caption",
            fontSize=8, textColor=C_GREY, alignment=TA_CENTER,
            spaceAfter=6),
        "section_num": s("section_num",
            fontSize=9, textColor=C_ACCENT, fontName="Helvetica-Bold",
            spaceAfter=0),
    }

ST = make_styles()

# ── Header / Footer ────────────────────────────────────────────────────────────
class PageDecorator:
    def __call__(self, c: canvas.Canvas, doc):
        c.saveState()
        page_num = doc.page

        if page_num > 1:
            c.setFillColor(C_NAVY)
            c.rect(MARGIN_L, PAGE_H - 1.4*cm, INNER_W, 0.55*cm, fill=True, stroke=False)
            c.setFont("Helvetica-Bold", 7.5)
            c.setFillColor(C_WHITE)
            c.drawString(MARGIN_L + 4, PAGE_H - 1.0*cm, "QuantG — Full Application Analysis Report")
            c.setFont("Helvetica", 7.5)
            c.drawRightString(PAGE_W - MARGIN_R - 4, PAGE_H - 1.0*cm, "CONFIDENTIAL")

        c.setFillColor(C_DIVIDER)
        c.rect(MARGIN_L, MARGIN_B - 0.3*cm, INNER_W, 0.03*cm, fill=True, stroke=False)
        c.setFont("Helvetica", 7.5)
        c.setFillColor(C_GREY)
        c.drawString(MARGIN_L, MARGIN_B - 0.65*cm,
                     "quantgtrade.com  |  Generated: " + datetime.now().strftime("%Y-%m-%d"))
        c.drawRightString(PAGE_W - MARGIN_R, MARGIN_B - 0.65*cm, f"Page {page_num}")
        c.restoreState()

# ── Helpers ────────────────────────────────────────────────────────────────────
def hr(color=C_DIVIDER, thickness=0.5):
    return HRFlowable(width="100%", thickness=thickness, color=color, spaceAfter=4, spaceBefore=4)

def sp(h=6):
    return Spacer(1, h)

def p(text, style="body"):
    return Paragraph(text, ST[style])

def h1(num, text):
    return KeepTogether([
        sp(8),
        Paragraph(f"Section {num}", ST["section_num"]),
        Paragraph(text, ST["h1"]),
        hr(C_BLUE, 1.2),
        sp(2),
    ])

def h2(text):
    return Paragraph(text, ST["h2"])

def h3(text):
    return Paragraph(text, ST["h3"])

def bul(items):
    return [Paragraph(f"• {i}", ST["bullet"]) for i in items]

def code_block(lines):
    result = []
    for line in lines:
        safe = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        safe = safe.replace(" ", "&nbsp;")
        result.append(Paragraph(safe, ST["code"]))
    return result

def simple_table(headers, rows, col_widths=None, zebra=True):
    data = [headers] + rows
    if col_widths is None:
        n = len(headers)
        col_widths = [INNER_W / n] * n

    style = [
        ("BACKGROUND",   (0, 0), (-1, 0),   C_NAVY),
        ("TEXTCOLOR",    (0, 0), (-1, 0),   C_WHITE),
        ("FONTNAME",     (0, 0), (-1, 0),   "Helvetica-Bold"),
        ("FONTSIZE",     (0, 0), (-1, 0),   8.5),
        ("FONTSIZE",     (0, 1), (-1, -1),  8.2),
        ("FONTNAME",     (0, 1), (-1, -1),  "Helvetica"),
        ("TOPPADDING",   (0, 0), (-1, -1),  5),
        ("BOTTOMPADDING",(0, 0), (-1, -1),  5),
        ("LEFTPADDING",  (0, 0), (-1, -1),  6),
        ("RIGHTPADDING", (0, 0), (-1, -1),  6),
        ("GRID",         (0, 0), (-1, -1),  0.35, C_DIVIDER),
        ("VALIGN",       (0, 0), (-1, -1),  "TOP"),
        ("ROWBACKGROUNDS",(0, 1),(-1, -1),  [C_WHITE, C_LGREY] if zebra else [C_WHITE]),
    ]
    t = Table(data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle(style))
    return t

def badge_row(text, color, symbol):
    style = ParagraphStyle("badge", parent=ST["body"],
        textColor=color, fontName="Helvetica-Bold", fontSize=9.5)
    return Paragraph(f"{symbol}  {text}", style)

# ── Cover Page ─────────────────────────────────────────────────────────────────
def cover_page(elements):
    inner = [
        sp(50),
        Paragraph("QuantG", ST["cover_title"]),
        Paragraph("Full Application Analysis Report", ST["cover_sub"]),
        sp(8),
        HRFlowable(width="60%", thickness=1, color=C_ACCENT,
                   spaceAfter=10, hAlign="CENTER"),
        sp(4),
        Paragraph("NSE / BSE Options Algorithmic Trading Platform", ST["cover_meta"]),
        Paragraph("Broker: Upstox &nbsp;|&nbsp; Engine: Unified v13 &nbsp;|&nbsp; API: v12.0", ST["cover_meta"]),
        sp(20),
        Paragraph(f"Generated: {datetime.now().strftime('%B %d, %Y')}", ST["cover_meta"]),
        Paragraph("Domain: quantgtrade.com &nbsp;|&nbsp; VPS: 82.180.145.183", ST["cover_meta"]),
        sp(10),
        HRFlowable(width="40%", thickness=0.5, color=HexColor("#546E7A"),
                   spaceAfter=6, hAlign="CENTER"),
        Paragraph("CONFIDENTIAL — Internal Technical Documentation", ST["cover_meta"]),
    ]

    rows = [[item] for item in inner]
    tbl = Table(rows, colWidths=[INNER_W])
    tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), C_NAVY),
        ("TOPPADDING",    (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING",   (0, 0), (-1, -1), 20),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 20),
    ]))
    elements.append(tbl)
    elements.append(PageBreak())

# ── Main Build ─────────────────────────────────────────────────────────────────
def build():
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "QuantG_Application_Report.pdf")
    doc = SimpleDocTemplate(
        out_path,
        pagesize=A4,
        leftMargin=MARGIN_L, rightMargin=MARGIN_R,
        topMargin=MARGIN_T,  bottomMargin=MARGIN_B,
        title="QuantG Full Application Analysis Report",
        author="Claude Code — Anthropic",
        subject="NSE/BSE Algorithmic Trading Platform — Technical Analysis",
    )

    pd = PageDecorator()
    elements = []

    # ── Cover ──────────────────────────────────────────────────────────────────
    cover_page(elements)

    # ── Section 0: Executive Summary ──────────────────────────────────────────
    elements += [
        h1("0", "Executive Summary"),
        p("QuantG is a production-grade, multi-user algorithmic options trading platform for Indian markets "
          "(NSE F&O and BSE F&O), integrated exclusively with Upstox as the executing broker. "
          "It supports <b>paper trading</b> (simulated, virtual Rs.5,00,000 wallet per user) and "
          "<b>live trading</b> (real funds via Upstox API), with a core design principle: "
          "<b>identical risk checks apply to both modes</b> — mode selection occurs only at the execution boundary."),
        sp(6),
        p("The backend runs on FastAPI (Python 3.11) with MongoDB 6.0 as the primary data store. "
          "The frontend is React 19 with Tailwind CSS. Deployment is fully containerised via Docker Compose, "
          "served through Caddy 2.8 with automatic HTTPS, hosted on a Contabo VPS at quantgtrade.com."),
        sp(8),
        h2("Live Readiness Verdicts"),
        sp(4),
        badge_row("PAPER TRADING: READY", C_GREEN, "✔"),
        badge_row("LIVE TRADING (1-lot controlled test): READY — wiring complete as of 2026-06-07", C_GREEN, "✔"),
        badge_row("Unified Engine v13 deployed and active", C_GREEN, "✔"),
        badge_row("67 tests passing locally", C_GREEN, "✔"),
        badge_row("FULL LIVE (all strategies at scale): NOT YET — controlled 1-lot test required first", C_AMBER, "⚠"),
        badge_row("Container live flag requires explicit host env var (not just .env)", C_AMBER, "⚠"),
        sp(10),
    ]

    # ── Section 1: Tech Stack ──────────────────────────────────────────────────
    elements += [
        h1("1", "Technology Stack"),
        simple_table(
            ["Layer", "Technology", "Version"],
            [
                ["Backend Runtime",    "Python + FastAPI + Uvicorn (ASGI)",        "Python 3.11 / FastAPI 0.110.1"],
                ["Primary Database",   "MongoDB with Motor async driver",           "MongoDB 6.0 / Motor 3.3.1"],
                ["Runtime State DB",   "SQLite (option strategy ledger)",           "Built-in"],
                ["Frontend",           "React + React Router + Tailwind CSS",        "React 19 / Router 7.5.1"],
                ["UI Components",      "shadcn/ui + Radix UI + Recharts",            "Latest stable"],
                ["Broker",             "Upstox (OAuth 2.0 + REST API + WebSocket)", "v3 market data"],
                ["AI Model",           "Google Gemini 2.5 Flash",                   "gemini-2.5-flash, 20s timeout"],
                ["Reverse Proxy",      "Caddy 2.8 (auto HTTPS via Let's Encrypt)",  "2.8"],
                ["Containerisation",   "Docker Compose — 4-service stack",          "Latest"],
            ],
            col_widths=[4.5*cm, 7.5*cm, 5.5*cm],
        ),
        sp(8),
    ]

    # ── Section 2: Project Structure ───────────────────────────────────────────
    elements += [
        h1("2", "Project Structure"),
        h2("2.1  Top-Level Layout"),
    ] + code_block([
        "QuantG/",
        "  backend/",
        "    server.py               <- Main FastAPI app (4,248+ lines)",
        "    signal_manager.py       <- Signal sweep task (2-second tick)",
        "    strategy_runner.py      <- Strategy evaluation task (15-second tick)",
        "    execution_bridge.py     <- Broker dispatch (rate limiting, payload validation)",
        "    execution_router.py     <- Paper vs Live adapter routing",
        "    risk_controls.py        <- Position sizing and style caps",
        "    safe_exec.py            <- AST sandbox for user Python code",
        "    market_protection.py    <- Trend / fake-signal detection",
        "    core/                   <- Unified trading engine v13 (20+ modules)",
        "    brokers/                <- Upstox gateway + WebSocket streams",
        "    routes/                 <- auth.py, ops.py, ai.py",
        "    scripts/                <- Pre-market checklist, audit scripts",
        "    migration/              <- State migration utilities",
        "    tests/                  <- 39 test files (dev-only, not in Docker image)",
        "  frontend/",
        "    pages/                  <- 12 React pages",
        "    components/             <- 40+ shadcn/ui components",
        "    contexts/AuthContext.jsx",
        "  docker-compose.yml",
        "  Caddyfile",
    ]) + [
        sp(6),
        h2("2.2  Core Engine Modules (backend/core/)"),
        simple_table(
            ["File", "Responsibility"],
            [
                ["execution_router.py",       "Routes OrderIntents to PaperAdapter or UpstoxLiveAdapter"],
                ["live_entry_preflight.py",   "12-point hard gate before any live order fires"],
                ["live_safety_firewall.py",   "Definitive shield: arm state, token, reconciliation, kill switch"],
                ["readiness_checker.py",      "Pre-execution validation: market, broker, wallet state"],
                ["risk_manager.py",           "Account/session constraints; position size limits"],
                ["option_selector_v2.py",     "ATM/ITM1/OTM1 contract selection (quality score >= 70 live)"],
                ["portfolio_ledger.py",       "Single source of truth for positions (no fill = no position)"],
                ["paper_broker.py",           "PaperWallet: Rs.5,00,000 virtual; slippage + charges simulated"],
                ["market_clock.py",           "NSE F&O hours: 09:15-15:30 IST; holiday detection"],
                ["market_domains.py",         "NSE_FO vs BSE_FO domain boundaries and lot sizes"],
                ["reconciliation.py",         "Broker vs QuantG ledger reconciliation daemon"],
                ["backtest_engine.py",        "Historical backtesting with simulated fills"],
                ["performance_tracker.py",    "Strategy metrics: Sharpe, drawdown, win rate"],
                ["strategy_leaderboard.py",   "Capital allocation ranking (closed realised P&L only)"],
                ["event_store.py",            "Event sourcing for full audit trail"],
                ["instrument_resolver.py",    "Resolves tradable instruments with source tracking"],
                ["quote_service.py",          "LTP data with source attribution and staleness detection"],
                ["order_manager.py",          "Order lifecycle management and status transitions"],
                ["position_manager.py",       "Position tracking and updates"],
            ],
            col_widths=[5.5*cm, 12.0*cm],
        ),
        sp(8),
    ]

    # ── Section 3: Architecture ────────────────────────────────────────────────
    elements += [
        h1("3", "Backend Architecture"),
        h2("3.1  Execution Pipeline"),
        p("The core design principle: <b>mode-agnostic risk enforcement</b>. All risk checks run "
          "identically for paper and live. The only branching point is the final adapter selection "
          "inside ExecutionRouter."),
        sp(6),
    ] + code_block([
        "Strategy Signal",
        "    |",
        "    v",
        "signal_manager._dispatch_signal_via_unified_engine()",
        "    |",
        "    v",
        "live_entry_preflight()   <-- 12-point hard gate",
        "    |",
        "    v",
        "ExecutionRouter.route_intent(mode=paper|live)",
        "    |                              |",
        "    v                              v",
        "[Paper] PaperAdapter.execute()    [Live] UpstoxLiveAdapter.execute()",
        "    |                              |",
        " PaperWallet debit/credit       ExecutionDispatchPayload",
        " Simulated fill + charges        bridge_submit_order()",
        " portfolio_ledger update          _place_upstox_order()",
        "                                  Upstox REST API",
    ]) + [
        sp(8),
        h2("3.2  Background Tasks"),
        simple_table(
            ["Task", "Interval", "Responsibility"],
            [
                ["signal_manager",  "2 seconds",  "Sweep db.signals, apply platform checks, route to execution boundary"],
                ["strategy_runner", "15 seconds", "Evaluate all active strategies via safe_run_strategy(), dispatch signals"],
            ],
            col_widths=[4.0*cm, 3.0*cm, 10.5*cm],
        ),
        sp(6),
        p("Both tasks use <b>MongoDB TTL-based distributed locks</b> to prevent duplicate execution when "
          "more than one pod is running. Lock IDs: 'signal_manager' and 'strategy_runner'."),
        sp(8),
        h2("3.3  Broker Module (backend/brokers/)"),
        simple_table(
            ["File", "Purpose"],
            [
                ["upstox_gateway.py",          "Plain HTTPS API wrapper (not SDK-dependent); token refresh, order placement, portfolio sync"],
                ["upstox_market_data_v3.py",   "WebSocket feed for real-time market data (v3 format)"],
                ["upstox_portfolio_stream.py", "Real-time portfolio updates from Upstox (fills, order status)"],
                ["normalization.py",           "Normalizes Upstox API responses to standard QuantG schemas"],
            ],
            col_widths=[5.5*cm, 12.0*cm],
        ),
        sp(8),
    ]

    # ── Section 4: Safety Systems ──────────────────────────────────────────────
    elements += [
        h1("4", "Safety and Risk Systems"),
        h2("4.1  Kill Switch Architecture (3-Tier)"),
        simple_table(
            ["Level", "Scope", "Storage Location"],
            [
                ["Global Kill Switch",   "Blocks ALL trading for ALL users",  "db.risk_state._id='global_kill_switch'"],
                ["Strategy Kill Switch", "Halts a single specific strategy",   "db.strategies.halted = true"],
                ["Live Arm State",       "User-level armed/disarmed toggle",   "db.live_arm_state.armed + global_live_enabled"],
            ],
            col_widths=[4.5*cm, 5.5*cm, 7.5*cm],
        ),
        sp(8),
        h2("4.2  Live Entry Preflight — 12-Point Gate"),
        p("All 12 checks must pass before any live order is submitted to Upstox. "
          "A single failure aborts the order immediately."),
        sp(4),
        simple_table(
            ["#", "Check", "Failure Code"],
            [
                ["1",  "CORE_ENGINE_LIVE_ENABLED env flag = true",              "Hard block"],
                ["2",  "User live_arm_state.armed = true",                      "ARMED_FALSE"],
                ["3",  "User live_arm_state.global_live_enabled = true",        "GLOBAL_LIVE_DISABLED"],
                ["4",  "Global kill switch inactive",                           "KILL_SWITCH_ACTIVE"],
                ["5",  "Upstox access_token present and not a mock token",      "NO_TOKEN"],
                ["6",  "Market session open (NSE F&O: 09:15-15:30 IST weekdays)","MARKET_CLOSED"],
                ["7",  "Option instrument_key contains '|' (real contract)",    "INVALID_INSTRUMENT"],
                ["8",  "Instrument NOT paper-simulated (no PAPER_ prefix)",     "SIMULATED_INSTRUMENT"],
                ["9",  "Option LTP > 0 (fresh price available)",                "STALE_PRICE"],
                ["10", "Quality score >= 70 (live threshold)",                  "LOW_QUALITY"],
                ["11", "No duplicate active position for same instrument",       "DUPLICATE_POSITION"],
                ["12", "Idempotency key fresh (not used in last 10 min)",       "DUPLICATE_SIGNAL"],
            ],
            col_widths=[0.8*cm, 11.0*cm, 5.7*cm],
        ),
        sp(8),
        h2("4.3  Firewall Cascade"),
    ] + code_block([
        "RiskManager          <- account/session constraints, position caps, daily loss limit",
        "    |",
        "ReadinessChecker     <- market session, broker availability, paper wallet init",
        "    |",
        "LiveSafetyFirewall   <- arm state, token validity, reconciliation health",
        "    |",
        "ExecutionRouter      <- adapter selection (PaperAdapter vs UpstoxLiveAdapter)",
        "    |",
        "live_entry_preflight <- final 12-point gate before Upstox API call",
    ]) + [
        sp(8),
        h2("4.4  Position Reconciliation"),
    ] + bul([
        "Broker truth reconciled against QuantG ledger via reconciliation.py",
        "Mismatch detection blocks further live trading until resolved",
        "Orphaned positions trigger auto-creation of a manual recovery strategy",
        "Full audit trail stored in db.reconciliation_state",
        "Pre-market 16-point checklist includes reconciliation health verification",
    ]) + [
        sp(8),
        h2("4.5  User Code Sandbox (safe_exec.py)"),
    ] + bul([
        "AST-level validation before any user strategy code executes",
        "Blocked: import statements, dunder access (__xxx__), exec(), eval()",
        "No network access possible from within user strategy code",
        "Strategies must return a list of signal dicts — output is validated before queuing",
    ]) + [sp(8)]

    # ── Section 5: Database ────────────────────────────────────────────────────
    elements += [
        h1("5", "Database and State Management"),
        h2("5.1  MongoDB Collections"),
        simple_table(
            ["Collection", "Purpose"],
            [
                ["users",                 "User accounts, roles (owner/admin/user), approval status"],
                ["broker_keys",           "Upstox OAuth tokens — Fernet-encrypted (AES-128) at rest"],
                ["strategies",            "Strategy definitions: Python code, visual config, mode, status, halted flag"],
                ["orders",                "Full order history with lifecycle states"],
                ["positions",             "Portfolio positions — updated on every fill; avg_price, qty, unrealised P&L"],
                ["strategy_positions",    "Strategy-level position tracking (separate from portfolio positions)"],
                ["signals",               "Queued signals awaiting execution by signal_manager"],
                ["fills",                 "Execution records — authoritative source for position construction"],
                ["paper_wallets",         "Virtual Rs.5,00,000 per user; debits on BUY, credits on SELL"],
                ["live_arm_state",        "Per-user armed + global_live_enabled flags"],
                ["risk_state",            "Global kill switch; reconciliation status"],
                ["risk_reservations",     "Risk budget reservations (ACTIVE / NEEDS_REVIEW)"],
                ["risk_events",           "Audit trail of every risk decision"],
                ["instruments",           "Cached Upstox master instrument list (24-hour TTL)"],
                ["suspended_instruments", "Tracked suspended securities"],
                ["reconciliation_state",  "Broker vs QuantG ledger audit trail"],
                ["outbox_events",         "Event outbox for eventual consistency guarantees"],
                ["performance_records",   "Strategy performance snapshots"],
                ["backtests",             "Backtest results and metrics"],
            ],
            col_widths=[4.8*cm, 12.7*cm],
        ),
        sp(6),
        h2("5.2  SQLite — Option Strategy Runtime Ledger"),
        p("File: runtime_state.sqlite3 (Docker volume at /data/runtime_state.sqlite3). "
          "Provides fast in-process state for option strategy cooldown timers, active position tracking, "
          "and strategy state machine transitions: IDLE -> OPEN -> COOLDOWN -> DISABLED. "
          "Managed by option_state_ledger.py."),
        sp(8),
    ]

    # ── Section 6: API Layer ───────────────────────────────────────────────────
    elements += [
        h1("6", "API Layer"),
        h2("6.1  Authentication"),
        simple_table(
            ["Method", "Path", "Description"],
            [
                ["POST", "/api/auth/register", "Register user (first user auto-approved as owner)"],
                ["POST", "/api/auth/login",    "Login — returns JWT (HS256, 7-day lifetime)"],
                ["GET",  "/api/auth/me",       "Get current authenticated user"],
            ],
            col_widths=[1.8*cm, 5.5*cm, 10.2*cm],
        ),
        sp(6),
        h2("6.2  Strategies"),
        simple_table(
            ["Method", "Path", "Description"],
            [
                ["POST",   "/api/strategies",              "Create strategy"],
                ["GET",    "/api/strategies",              "List user strategies with performance metrics"],
                ["GET",    "/api/strategies/{id}",         "Get strategy details"],
                ["PUT",    "/api/strategies/{id}",         "Update strategy code or config"],
                ["DELETE", "/api/strategies/{id}",         "Delete strategy"],
                ["POST",   "/api/strategies/{id}/backtest","Run backtest against historical data"],
                ["POST",   "/api/strategies/{id}/run",     "Trigger immediate strategy evaluation"],
            ],
            col_widths=[1.8*cm, 5.5*cm, 10.2*cm],
        ),
        sp(6),
        h2("6.3  Orders and Positions"),
        simple_table(
            ["Method", "Path", "Description"],
            [
                ["POST",   "/api/orders",                  "Place order (paper or live)"],
                ["GET",    "/api/orders",                  "List orders (capped at 500)"],
                ["GET",    "/api/orders/{id}",             "Get order details"],
                ["POST",   "/api/orders/{id}/cancel",      "Cancel order"],
                ["GET",    "/api/positions",               "List open positions"],
                ["GET",    "/api/positions/{symbol}",      "Get position for a symbol"],
                ["POST",   "/api/positions/{symbol}/close","Close position"],
            ],
            col_widths=[1.8*cm, 5.5*cm, 10.2*cm],
        ),
        sp(6),
        h2("6.4  Operations and AI"),
        simple_table(
            ["Method", "Path", "Description"],
            [
                ["POST", "/api/ops/kill-switch/activate",   "Activate global kill switch"],
                ["POST", "/api/ops/kill-switch/deactivate", "Deactivate global kill switch"],
                ["GET",  "/api/ops/reconciliation-status",  "Get reconciliation snapshot"],
                ["POST", "/api/ops/reconciliation/resolve", "Manually resolve reconciliation break"],
                ["POST", "/api/ai/chat",                    "Chat with Gemini for strategy advice"],
                ["POST", "/api/ai/rate-strategy",           "AI confidence scoring on a strategy"],
                ["POST", "/api/ai/modify-strategy",         "AI-assisted code editing"],
                ["POST", "/api/agent/execute-tool",         "Read-only agent tool execution"],
                ["GET",  "/api/health",                     "Health check"],
                ["GET",  "/api/version",                    "App version"],
            ],
            col_widths=[1.8*cm, 5.7*cm, 10.0*cm],
        ),
        sp(8),
    ]

    # ── Section 7: Trading Logic ───────────────────────────────────────────────
    elements += [
        h1("7", "Trading Logic"),
        h2("7.1  Supported Markets and Instruments"),
        simple_table(
            ["Market", "Underlyings", "Lot Sizes", "Segments"],
            [
                ["NSE F&O",  "NIFTY, BANKNIFTY",              "NIFTY: 75, BANKNIFTY: 30", "NSE_FO"],
                ["BSE F&O",  "SENSEX",                         "SENSEX: 20",               "BSE_FO"],
                ["Equities", "RELIANCE, TCS, INFY, etc.",      "1 (CNC/MIS)",              "NSE_EQ, BSE_EQ"],
                ["BLOCKED",  "CRUDEOIL, CRUDEOILM, NATGAS",   "N/A",                      "MCX — not supported"],
            ],
            col_widths=[2.5*cm, 5.5*cm, 4.5*cm, 5.0*cm],
        ),
        sp(6),
        h2("7.2  Option Strike Selection (option_selector_v2.py)"),
    ] + bul([
        "Candidates evaluated: ATM, ITM1, OTM1, AUTO",
        "Quality score minimum: 70 for live mode, 50 for paper mode",
        "Quote staleness threshold: 30 seconds (QUANTG_OPTION_STALE_SEC)",
        "MCX/commodity instruments blocked at selection stage",
        "All instruments tagged with source: UPSTOX_MASTER vs PAPER_SIMULATED",
    ]) + [
        sp(6),
        h2("7.3  Paper Mode Execution"),
    ] + bul([
        "Virtual Rs.5,00,000 wallet per user (reset-able via /api/profile/reset-paper)",
        "Instant fill at quoted LTP + 0.035% fixed slippage",
        "Simulated charges: brokerage (min Rs.20, max 0.03%), STT (SELL only, 0.0625%), exchange fee, SEBI, stamp, GST",
        "Fill record created in db.fills; portfolio_ledger updated; position opened or closed",
        "Fully isolated from broker — no Upstox API calls",
    ]) + [
        sp(6),
        h2("7.4  Live Mode Execution"),
    ] + bul([
        "Live arm state + all 12 preflight checks must pass",
        "UpstoxLiveAdapter builds ExecutionDispatchPayload and calls bridge_submit_order()",
        "Upstox REST API returns order_id; status tracked asynchronously in db.orders",
        "Portfolio WebSocket stream delivers real-time fill confirmations",
        "Reconciliation daemon syncs broker truth back to QuantG ledger",
        "Upstox rate limits enforced: 10 req/s burst, 400 req/min sustained",
    ]) + [
        sp(6),
        h2("7.5  Risk Controls (applied identically to both modes)"),
        simple_table(
            ["Control", "Description"],
            [
                ["Kill switch",         "3-tier: global, strategy-level, live arm state"],
                ["Market session gate", "NSE F&O: 09:15-15:30 IST weekdays only"],
                ["Position size cap",   "Equity-based, adjusted by risk style (micro_scalp / momentum / breakout / balanced)"],
                ["Daily loss limit",    "Configurable per strategy (default Rs.5,000)"],
                ["Margin buffer",       "Max 85% of free margin to prevent over-leveraging"],
                ["Duplicate block",     "No second position for same instrument while one is open"],
                ["Cooldown tracking",   "Configurable per strategy (default 10 min post-exit)"],
                ["Trailing stop-loss",  "Adaptive trigger + step; auto-closes on adverse move"],
                ["Max trades per day",  "Configurable per strategy (default 5)"],
                ["Idempotency key",     "Prevents duplicate signal execution within 10-minute window"],
            ],
            col_widths=[4.5*cm, 13.0*cm],
        ),
        sp(8),
    ]

    # ── Section 8: Strategy Configuration ─────────────────────────────────────
    elements += [
        h1("8", "Strategy Configuration Schema"),
        h2("8.1  Visual Config Structure"),
    ] + code_block([
        "{",
        '  "options": {',
        '    "enabled": true,',
        '    "underlying": "NIFTY",',
        '    "strike_preference": "ATM|ITM1|OTM1|AUTO",',
        '    "expiry_rule": 0',
        "  },",
        '  "risk": {',
        '    "stop_loss_pct": 8.0,',
        '    "take_profit_pct": 20.0,',
        '    "trail_trigger_pct": 5.0,',
        '    "trail_step_pct": 2.0,',
        '    "trailing_sl_enabled": true,',
        '    "daily_loss_limit": 5000.0,',
        '    "required_capital": 50000.0,',
        '    "cooldown_minutes": 10,',
        '    "max_trades_day": 5',
        "  }",
        "}",
    ]) + [
        sp(6),
        h2("8.2  Pre-Seeded Strategies (16+)"),
    ] + bul([
        "Momentum: NIFTY ATM, BANKNIFTY ATM, EMA crossover",
        "Breakout: VWAP trend, opening range breakout, ATR volume",
        "Scalping: Quick scalper, band scalper, EMA scalper, micro trend",
        "Pullback: VWAP pullback, RSI reversal",
        "Volatility: Straddle strategies",
        "Commodities: CRUDEOILM EMA, RSI reversion (NOTE: commodities blocked at option selection)",
    ]) + [sp(8)]

    # ── Section 9: Frontend ────────────────────────────────────────────────────
    elements += [
        h1("9", "Frontend Architecture"),
        h2("9.1  Pages"),
        simple_table(
            ["Page", "Description"],
            [
                ["Dashboard.jsx",      "Main hub: KPIs, live positions, orders, market data summary"],
                ["Strategies.jsx",     "Strategy list, CRUD, performance metrics table"],
                ["PythonEditor.jsx",   "In-browser Python code editor with syntax highlighting"],
                ["VisualBuilder.jsx",  "Drag-and-drop strategy builder (no code required)"],
                ["Orders.jsx",         "Order history with status lifecycle, filter and search"],
                ["Positions.jsx",      "Active position management with P&L display"],
                ["ApiKeys.jsx",        "Upstox OAuth credential management"],
                ["Profile.jsx",        "User settings, paper wallet reset, password change"],
                ["Auth.jsx",           "Login and registration"],
                ["AIBot.jsx",          "Gemini 2.5-powered strategy assistant chatbot"],
                ["OpsConsole.jsx",     "Kill switch, reconciliation controls, audit event log"],
                ["MarketHub.jsx",      "Market data browser, watchlists, live quotes"],
            ],
            col_widths=[5.0*cm, 12.5*cm],
        ),
        sp(6),
        h2("9.2  Key Frontend Dependencies"),
        simple_table(
            ["Package", "Purpose"],
            [
                ["React 19.0.0",         "Component framework"],
                ["React Router 7.5.1",   "Client-side routing"],
                ["Tailwind CSS 3.4.17",  "Utility-first CSS framework"],
                ["shadcn/ui + Radix UI", "Accessible component primitives (40+ components)"],
                ["Recharts",             "Data visualisation — P&L charts, equity curves"],
                ["Axios",                "HTTP client for all API calls"],
                ["React Hook Form",      "Form state management"],
                ["AuthContext.jsx",      "JWT token management and user state across the app"],
            ],
            col_widths=[5.0*cm, 12.5*cm],
        ),
        sp(8),
    ]

    # ── Section 10: Auth & Security ────────────────────────────────────────────
    elements += [
        h1("10", "Authentication and Security"),
        h2("10.1  JWT Authentication"),
    ] + bul([
        "Algorithm: HS256, secret: JWT_SECRET (32-byte minimum recommended)",
        "Token lifetime: 7 days",
        "Scope: per-user isolation — strategies, orders, and positions are user-separated",
        "First registered user auto-approved as owner role",
    ]) + [
        sp(6),
        h2("10.2  Password Storage"),
    ] + bul([
        "Bcrypt hashing with cost factor 4 (NOTE: recommend increasing to 12 for production scale)",
        "Salted hash — plaintext never persisted",
        "Change password: POST /api/profile/change-password",
    ]) + [
        sp(6),
        h2("10.3  Upstox Token Encryption"),
    ] + bul([
        "Fernet symmetric encryption (AES-128-CBC + HMAC-SHA256)",
        "Key: CREDENTIAL_ENCRYPTION_KEY derived via SHA-256, stored in .env",
        "Tokens decrypted only at execution time, never written to logs",
    ]) + [
        sp(6),
        h2("10.4  Infrastructure Security"),
    ] + bul([
        "Docker backend container runs as non-root user",
        "Caddy handles TLS certificate lifecycle (Let's Encrypt, auto-renewal)",
        "CORS restricted to https://quantgtrade.com and https://www.quantgtrade.com only",
        "MongoDB not exposed externally — Docker internal network only",
        "AST sandbox prevents user code injection at strategy execution",
    ]) + [sp(8)]

    # ── Section 11: Infrastructure ─────────────────────────────────────────────
    elements += [
        h1("11", "Infrastructure and Deployment"),
        h2("11.1  Docker Compose Services"),
        simple_table(
            ["Service",   "Image",              "Exposed Port",       "Notes"],
            [
                ["mongo",    "mongo:6.0",          "27017 (internal)", "Volume: mongo-data:/data/db"],
                ["backend",  "Python 3.11/Uvicorn","8000 (internal)",  "Volume: backend-state:/data (SQLite ledger)"],
                ["frontend", "React/Nginx",         "80 (internal)",    "Serves built React bundle"],
                ["caddy",    "caddy:2.8",           "80, 443 (public)", "Auto HTTPS; routes /api/* to backend:8000"],
            ],
            col_widths=[2.5*cm, 4.0*cm, 3.5*cm, 7.5*cm],
        ),
        sp(6),
        h2("11.2  Production Environment"),
    ] + bul([
        "Domain: quantgtrade.com (and www.quantgtrade.com)",
        "VPS: Contabo, IP 82.180.145.183, user: root",
        "App path on VPS: /app/quantg/QuantG",
        "SSH key: ~/.ssh/codex_quantg_vps",
        "Caddyfile: quantgtrade.com proxies to frontend:80; /api/* proxies to backend:8000",
    ]) + [
        sp(6),
        h2("11.3  Key Environment Variables"),
        simple_table(
            ["Variable", "Value / Default", "Description"],
            [
                ["CORE_ENGINE_ENABLED",              "true",                   "Master switch for unified engine"],
                ["CORE_ENGINE_LIVE_ENABLED",          "true (.env) / false (compose default)", "Live flag — must set as host env var for container"],
                ["CORE_ENGINE_PAPER_ENABLED",         "true",                   "Paper trading enabled"],
                ["LEGACY_EXECUTION_WRITES_ENABLED",   "false",                  "Legacy execution path disabled"],
                ["SIGNAL_MANAGER_TICK_SECONDS",       "2",                      "Signal sweep frequency"],
                ["STRATEGY_RUNNER_TICK_SECONDS",      "15",                     "Strategy evaluation frequency"],
                ["SIGNAL_SPAM_WINDOW_SECONDS",        "300",                    "Duplicate signal suppression window (5 min)"],
                ["QUANTG_OPTION_STALE_SEC",           "30",                     "Quote staleness threshold (seconds)"],
                ["UPSTOX_INSTRUMENT_SYNC_TTL_SEC",    "86400",                  "Instrument cache TTL (24 hours)"],
                ["GEMINI_MODEL",                      "gemini-2.5-flash",       "AI model for scoring and chat"],
                ["GEMINI_TIMEOUT_SEC",                "20",                     "Timeout per Gemini API call"],
                ["OPTION_LEDGER_PATH",                "/data/runtime_state.sqlite3", "SQLite ledger path in container"],
            ],
            col_widths=[5.5*cm, 4.5*cm, 7.5*cm],
        ),
        sp(6),
        h2("11.4  Deploy Commands"),
    ] + code_block([
        "# Pull and rebuild backend on VPS",
        "cd /app/quantg/QuantG && git pull origin main",
        "docker-compose build backend && docker-compose up -d backend",
        "",
        "# Enable live trading in container (must override compose default=false)",
        "CORE_ENGINE_LIVE_ENABLED=true docker-compose up -d backend",
        "",
        "# Run pre-market checklist (16-point read-only audit)",
        "docker-compose exec backend python scripts/pre_market_open_checklist.py",
    ]) + [sp(8)]

    # ── Section 12: Testing ────────────────────────────────────────────────────
    elements += [
        h1("12", "Testing"),
        h2("12.1  Test Coverage — 39 Test Files, 67+ Tests"),
        simple_table(
            ["Test File", "Area", "Key Assertions"],
            [
                ["test_unified_trading_engine.py",       "Core engine integration",     "End-to-end paper and live signal dispatch"],
                ["test_paper_execution_architecture.py", "Paper adapter semantics",     "Wallet debits, fill records, position creation"],
                ["test_execution_bridge_upstox_only.py", "Upstox dispatch",             "Payload structure, rate limit, token injection"],
                ["test_live_safety_audit.py",            "Safety firewall (20 tests)",  "8 required + 7 additional safety invariants — all pass"],
                ["test_live_readiness.py",               "12-point preflight",          "Each check individually verified"],
                ["test_risk_controls.py",                "Position sizing",             "Style-adjusted sizes, daily loss, margin buffer"],
                ["test_option_selector_v2.py",           "Contract selection",          "Score thresholds, stale quotes, MCX block"],
                ["test_signal_manager.py",               "Signal validation",           "Shape validation, spam filter, idempotency"],
                ["test_paper_execution_lifecycle.py",    "Fill to position",            "status=PLACED, broker_order_id saved, mode=live"],
                ["test_position_integrity_healer.py",    "Reconciliation",              "Orphan detection, manual recovery creation"],
                ["test_market_timezone.py",              "IST / market hours",          "Holiday detection, session boundaries"],
                ["test_strategy_leaderboard.py",         "Performance ranking",         "Closed realised P&L only, Sharpe calculation"],
                ["test_dashboard_truthfulness.py",       "UI data accuracy",            "Dashboard KPIs match ledger truth"],
                ["test_trading_readiness.py",            "Pre-market checklist",        "16-point read-only audit results"],
            ],
            col_widths=[5.5*cm, 4.0*cm, 8.0*cm],
        ),
        sp(6),
        p("Tests are excluded from the Docker production image via .dockerignore. "
          "Run locally: <b>cd backend && pytest tests/ -v</b>"),
        sp(8),
    ]

    # ── Section 13: Scripts & Utilities ───────────────────────────────────────
    elements += [
        h1("13", "Scripts and Utilities"),
        h2("13.1  Pre-Market Checklist (16-Point Read-Only Audit)"),
        p("File: backend/scripts/pre_market_open_checklist.py — run each trading day before market open."),
        sp(4),
    ] + bul([
        "Upstox access_token present and non-mock",
        "Live arm state: armed + global_live_enabled = true",
        "Global kill switch inactive",
        "No unresolved reconciliation mismatch",
        "Paper wallet balance positive (> Rs.0)",
        "At least one strategy in ACTIVE status",
        "Strategy runner last ticked within 60 seconds",
        "Signal manager last ticked within 30 seconds",
        "Option ledger (SQLite) reachable",
        "MongoDB connection healthy",
        "No strategies in quarantine",
        "Instrument cache fresh (< 24 hours old)",
        "No pending NEEDS_REVIEW risk reservations",
        "Today is not a market holiday",
        "Backend /api/health returning 200",
        "CORE_ENGINE_LIVE_ENABLED flag confirmed set",
    ]) + [
        sp(6),
        h2("13.2  Other Utility Scripts"),
        simple_table(
            ["Script", "Purpose"],
            [
                ["strategy_doctor.py",           "Diagnose stuck signals, bad config, or missing fills"],
                ["audit_trading_state.py",        "Full audit of current trading state across all users"],
                ["production_readiness_audit.py", "Comprehensive system health check before going live"],
                ["reset_paper_state_safe.py",     "Safely reset all paper wallets and paper positions"],
            ],
            col_widths=[5.5*cm, 12.0*cm],
        ),
        sp(6),
        h2("13.3  Migration Scripts (backend/migration/)"),
        simple_table(
            ["Script", "Purpose"],
            [
                ["migrate_old_orders_to_core.py",    "Migrate legacy order documents to Unified Engine schema"],
                ["validate_core_state.py",            "Validate core state consistency post-migration"],
                ["rebuild_strategy_performance.py",   "Rebuild performance records from fill history"],
                ["repair_positions_without_fills.py", "Fix orphaned position documents with no backing fill"],
            ],
            col_widths=[5.5*cm, 12.0*cm],
        ),
        sp(8),
    ]

    # ── Section 14: Strengths & Concerns ──────────────────────────────────────
    elements += [
        h1("14", "Strengths and Areas for Attention"),
        h2("14.1  Architectural Strengths"),
    ] + bul([
        "<b>Unified pipeline with mode parity</b> — Identical risk checks for paper and live. "
        "Only the adapter differs. Eliminates divergence where paper works but live behaves differently.",
        "<b>12-point live preflight gate</b> — Accidental live firing is practically impossible without "
        "explicit armed state, real token, env flag, and open market session all simultaneously.",
        "<b>AST-level code sandbox</b> — User strategy code cannot import modules, use eval/exec, "
        "or access the network. Injection is blocked before execution.",
        "<b>Distributed locks</b> — MongoDB TTL-based locks prevent duplicate signal processing "
        "when multiple pods are running.",
        "<b>Full audit trail</b> — Event sourcing, risk events, and reconciliation logs provide "
        "complete traceability for every order and risk decision.",
        "<b>Source tracking</b> — All instruments tagged with origin (UPSTOX_MASTER / PAPER_SIMULATED) "
        "for debugging unexpected behaviour.",
        "<b>Self-healing reconciliation</b> — Orphaned positions trigger automatic manual recovery strategy creation.",
        "<b>Gemini 2.5 integration</b> — AI-assisted strategy scoring, code modification, and operator chat.",
    ]) + [
        sp(8),
        h2("14.2  Areas for Attention"),
        simple_table(
            ["Area", "Issue", "Recommended Action"],
            [
                ["Container live flag",
                 "CORE_ENGINE_LIVE_ENABLED defaults to false in docker-compose.yml regardless of .env. "
                 "Live will not fire without the host env var.",
                 "Document clearly in runbook. Consider defaulting to true and relying on arm state as primary gate."],
                ["Encryption key management",
                 "CREDENTIAL_ENCRYPTION_KEY is a static secret in .env. If .env is leaked, all Upstox tokens are exposed.",
                 "Rotate key periodically. Consider AWS Secrets Manager or HashiCorp Vault for production."],
                ["Intraday reconciliation",
                 "Reconciliation runs nightly. A position mismatch during market hours is not caught until next cycle.",
                 "Add hourly reconciliation loop for live users."],
                ["Strategy execution timeout",
                 "No hard timeout in safe_run_strategy(). Runaway user code can stall the strategy runner for that tick.",
                 "Add asyncio.wait_for() with a 5-second timeout around strategy code execution."],
                ["AI call rate limiting",
                 "No per-user cap on Gemini API calls via /api/ai/chat. High usage drives unexpected API costs.",
                 "Add per-user or per-minute rate limiting on AI endpoints."],
                ["Paper slippage model",
                 "Fixed 0.035% slippage does not model gap risk at open, illiquid strikes, or high-volatility regimes.",
                 "Consider dynamic slippage based on live bid-ask spread when WebSocket data is available."],
                ["Order history pagination",
                 "Some list queries cap at 500 orders. High-volume traders may see truncated order history.",
                 "Implement cursor-based pagination on /api/orders."],
                ["Bcrypt cost factor",
                 "Cost factor 4 is below the recommended 12 for production. Speeds up brute-force attacks.",
                 "Increase bcrypt cost factor to 12 before scaling the user base."],
            ],
            col_widths=[3.0*cm, 6.5*cm, 8.0*cm],
        ),
        sp(8),
    ]

    # ── Section 15: Production Readiness Summary ───────────────────────────────
    elements += [
        h1("15", "Production Readiness Summary"),
        sp(4),
        badge_row("PAPER TRADING: READY", C_GREEN, "✔"),
        badge_row("LIVE TRADING (1-lot controlled test): READY — all wiring complete as of 2026-06-07", C_GREEN, "✔"),
        badge_row("Unified Engine v13 active (LEGACY_EXECUTION_WRITES_ENABLED=false)", C_GREEN, "✔"),
        badge_row("67 tests passing locally across core engine, adapters, and safety gates", C_GREEN, "✔"),
        badge_row("12-point preflight gate verified in test_live_safety_audit.py (20 tests, all pass)", C_GREEN, "✔"),
        badge_row("16-point pre-market checklist script available", C_GREEN, "✔"),
        badge_row("Docker Compose 4-service stack with health checks on all containers", C_GREEN, "✔"),
        badge_row("HTTPS via Caddy with auto Let's Encrypt certificates at quantgtrade.com", C_GREEN, "✔"),
        sp(8),
        badge_row("FULL LIVE (all strategies at scale): NOT YET — 1-lot controlled test required first", C_AMBER, "⚠"),
        badge_row("Container live flag: must set host env var CORE_ENGINE_LIVE_ENABLED=true explicitly", C_AMBER, "⚠"),
        badge_row("Intraday reconciliation gap — mismatches only detected nightly", C_AMBER, "⚠"),
        badge_row("No hard timeout on user strategy code execution", C_AMBER, "⚠"),
        sp(10),
        h2("Conditions Required for Live Trading to Fire (ALL must be true simultaneously)"),
        sp(4),
    ] + code_block([
        "armed                 = true   (db.live_arm_state per user)",
        "global_live_enabled   = true   (db.live_arm_state per user)",
        "kill_switch           = inactive (db.risk_state)",
        "CORE_ENGINE_LIVE_ENABLED = true  (host environment variable, not just .env)",
        "Upstox token          = real, non-mock (broker_keys collection)",
        "Market session        = open   (NSE F&O: 09:15-15:30 IST, weekdays only)",
    ]) + [
        sp(10),
        PageBreak(),
        p("End of Report", "caption"),
        sp(4),
        p("This document was generated automatically by Claude Code from live codebase analysis on 2026-06-07. "
          "All file paths, configuration values, and architectural details are derived from the actual source "
          "code across d:/Quant/QuantG. For authoritative current state, always refer to the git repository "
          "and running containers.", "caption"),
    ]

    # ── Build PDF ──────────────────────────────────────────────────────────────
    doc.build(elements, onFirstPage=pd, onLaterPages=pd)
    print(f"PDF saved: {out_path}")
    return out_path


if __name__ == "__main__":
    build()
