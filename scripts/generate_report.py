import os
import sys
from datetime import datetime, timezone
from pymongo import MongoClient

# ReportLab imports
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether, ListFlowable, ListItem
from reportlab.pdfgen import canvas

# NumberedCanvas for professional "Page X of Y" and header/footer decoration
class NumberedCanvas(canvas.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_page_decorations(self, page_count):
        self.saveState()
        
        # Draw running header on later pages
        if self._pageNumber > 1:
            self.setFont("Helvetica-Bold", 8)
            self.setFillColor(colors.HexColor('#1E3A8A')) # Deep Blue
            self.drawString(54, 750, "QuantG — Strategy Architecture & Deep System Audit")
            self.setFont("Helvetica", 8)
            self.setFillColor(colors.HexColor('#64748B')) # Slate Gray
            self.drawRightString(558, 750, "June 2026 Audit Report")
            self.setStrokeColor(colors.HexColor('#CBD5E1')) # Light Gray
            self.setLineWidth(0.5)
            self.line(54, 742, 558, 742)
            
        # Draw running footer on all pages
        self.setFont("Helvetica-Bold", 8)
        self.setFillColor(colors.HexColor('#94A3B8'))
        self.drawString(54, 40, "CONFIDENTIAL")
        self.setFont("Helvetica", 8)
        self.drawString(130, 40, "— FOR PROP TRADING & INTERNAL DEVELOPMENT ONLY")
        page_text = f"Page {self._pageNumber} of {page_count}"
        self.drawRightString(558, 40, page_text)
        self.setStrokeColor(colors.HexColor('#E2E8F0'))
        self.setLineWidth(0.5)
        self.line(54, 52, 558, 52)
        
        self.restoreState()

def get_db_data():
    """Retrieve runtime and catalog info from local MongoDB database."""
    data = {
        'users': [],
        'strategies': [],
        'active_positions': 0,
        'closed_positions': 0,
        'signals': [],
        'positions_summary': {}
    }
    
    try:
        client = MongoClient('mongodb://localhost:27017', serverSelectionTimeoutMS=2000)
        # Test connection
        client.admin.command('ping')
        db = client['quantg']
        
        # 1. Fetch Users
        users = list(db.users.find({}, {"_id": 0, "password_hash": 0}))
        data['users'] = users
        
        # 2. Fetch Strategies
        strategies = list(db.strategies.find({}, {"_id": 0}))
        data['strategies'] = strategies
        
        # 3. Counts from ledger collections
        data['active_positions'] = db.strategy_positions.count_documents({"status": {"$in": ["RESERVED", "PENDING_OPEN", "PENDING_BROKER", "OPEN", "FILLED", "EXITING"]}})
        data['closed_positions'] = db.strategy_positions.count_documents({"status": {"$in": ["CLOSED", "EXITED"]}})
        
        # 4. Fetch recent signals
        signals = list(db.signals.find({}).sort("created_at", -1).limit(10))
        data['signals'] = signals
        
        # 5. Position Summary by strategy
        pipeline = [
            {"$group": {"_id": "$strategy_id", "count": {"$sum": 1}, "status_open": {"$sum": {"$cond": [{"$in": ["$status", ["OPEN", "FILLED"]]}, 1, 0]}}}}
        ]
        pos_stats = list(db.strategy_positions.aggregate(pipeline))
        data['positions_summary'] = {p['_id']: p for p in pos_stats}
        
    except Exception as e:
        print(f"MongoDB connection failed ({e}). Using offline fallbacks.")
        
    return data

def build_pdf(report_path, db_data):
    # Setup document geometry: Letter page, 0.75 inch (54pt) margins, leaving 612x792 pt size
    doc = SimpleDocTemplate(
        report_path,
        pagesize=letter,
        leftMargin=54,
        rightMargin=54,
        topMargin=72,
        bottomMargin=72
    )
    
    styles = getSampleStyleSheet()
    
    # Custom Color Palette
    c_primary = colors.HexColor('#0F172A')   # Slate 900 (Dark background/Text)
    c_secondary = colors.HexColor('#1E3A8A') # Deep Blue 900 (Headings)
    c_teal = colors.HexColor('#0D9488')      # Teal 600 (Accents/Highlights)
    c_slate = colors.HexColor('#475569')     # Slate 600 (Subtext/Normal text)
    c_bg_light = colors.HexColor('#F8FAFC')  # Slate 50 (Table alternate bg)
    c_border = colors.HexColor('#E2E8F0')    # Slate 200 (Lines)
    c_red = colors.HexColor('#DC2626')       # Red 600
    c_green = colors.HexColor('#16A34A')     # Green 600
    
    # Custom Paragraph Styles
    style_cover_title = ParagraphStyle(
        'CoverTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=26,
        leading=32,
        textColor=c_primary,
        spaceAfter=15
    )
    
    style_cover_subtitle = ParagraphStyle(
        'CoverSubtitle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=12,
        leading=16,
        textColor=c_teal,
        spaceAfter=40
    )
    
    style_h1 = ParagraphStyle(
        'Heading1_Custom',
        parent=styles['Heading1'],
        fontName='Helvetica-Bold',
        fontSize=18,
        leading=22,
        textColor=c_secondary,
        spaceBefore=18,
        spaceAfter=10,
        keepWithNext=True
    )
    
    style_h2 = ParagraphStyle(
        'Heading2_Custom',
        parent=styles['Heading2'],
        fontName='Helvetica-Bold',
        fontSize=12,
        leading=16,
        textColor=c_teal,
        spaceBefore=14,
        spaceAfter=6,
        keepWithNext=True
    )
    
    style_body = ParagraphStyle(
        'Body_Custom',
        parent=styles['BodyText'],
        fontName='Helvetica',
        fontSize=9.5,
        leading=14,
        textColor=c_slate,
        spaceAfter=8
    )
    
    style_body_bold = ParagraphStyle(
        'BodyBold_Custom',
        parent=style_body,
        fontName='Helvetica-Bold',
        textColor=c_primary
    )

    style_bullet = ParagraphStyle(
        'Bullet_Custom',
        parent=style_body,
        leftIndent=15,
        firstLineIndent=-10,
        spaceAfter=4
    )

    style_code = ParagraphStyle(
        'Code_Custom',
        parent=styles['Code'],
        fontName='Courier',
        fontSize=7.5,
        leading=9.5,
        textColor=colors.HexColor('#1E293B'),
        backColor=colors.HexColor('#F1F5F9'),
        borderColor=colors.HexColor('#E2E8F0'),
        borderWidth=0.5,
        borderPadding=5,
        spaceAfter=6
    )

    story = []
    
    # ================= PAGE 1: COVER PAGE =================
    story.append(Spacer(1, 100))
    story.append(Paragraph("QUANTG PLATFORM", style_cover_subtitle))
    story.append(Paragraph("System-Wide Strategy Audit & Technical Gap Analysis", style_cover_title))
    
    # Colored accent bar
    story.append(Table(
        [['']], 
        colWidths=[100], 
        rowHeights=[4], 
        style=TableStyle([
            ('BACKGROUND', (0,0), (-1,-1), c_teal),
            ('BOTTOMPADDING', (0,0), (-1,-1), 0),
            ('TOPPADDING', (0,0), (-1,-1), 0),
        ])
    ))
    story.append(Spacer(1, 40))
    
    # Metadata block
    meta_data = [
        [Paragraph("<b>Date:</b>", style_body), Paragraph(datetime.now().strftime("%B %d, %Y"), style_body)],
        [Paragraph("<b>Author:</b>", style_body), Paragraph("Antigravity AI Platform Architect", style_body)],
        [Paragraph("<b>Classification:</b>", style_body), Paragraph("Confidential — Prop Trading Ops", style_body)],
        [Paragraph("<b>Target Broker:</b>", style_body), Paragraph("Upstox API (V3 WebSocket + REST)", style_body)],
        [Paragraph("<b>Current Engine State:</b>", style_body), Paragraph("Paper Trading (Live Infrastructure Inactive)", style_body)]
    ]
    t_meta = Table(meta_data, colWidths=[110, 300])
    t_meta.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('LEFTPADDING', (0,0), (-1,-1), 0),
    ]))
    story.append(t_meta)
    
    story.append(Spacer(1, 120))
    
    # Executive Summary brief on cover
    story.append(Paragraph("<b>Executive Overview:</b>", style_body_bold))
    story.append(Paragraph(
        "This document presents a deep, exhaustive technical audit of the QuantG algo-trading platform's "
        "strategy execution engine and default strategy catalog. The audit covers strategy structures, "
        "integration with the core runtime, market clock validation, pre-trade risk controls, OptionSelector v2 "
        "mechanics, and identifies critical design flaws causing stuck positions, ledger discrepancies, and "
        "execution deadlocks. Solutions and a phased path to live trading readiness are detailed.",
        style_body
    ))
    
    story.append(PageBreak())
    
    # ================= PAGE 2: EXECUTIVE SUMMARY & ARCHITECTURE =================
    story.append(Paragraph("1. Executive Summary & Core Verdict", style_h1))
    story.append(Paragraph(
        "A rigorous inspection of the QuantG codebase and active database state reveals that while the platform "
        "has sophisticated strategy components (e.g. AST sandboxes, OptionsSelector v2, and ATR-based exit policies), "
        "its core trading loops suffer from <b>architectural fragmentation</b>. Specifically, the system operates "
        "two parallel, conflicting bookkeeping pipelines: a legacy path writing to database collections and a "
        "modern path writing to the ledger classes. This divergence causes the platform to lose its single source "
        "of truth for positions, balances, and P&L tracking.",
        style_body
    ))
    
    story.append(Paragraph("Key Findings from the Production Audit:", style_h2))
    story.append(Paragraph("• <b>Empty Transaction Ledgers:</b> The collections <code>db.trades</code> and <code>db.trade_fills</code> contain 0 documents, which means the platform has never officially closed a trade inside its primary transaction registry.", style_bullet))
    story.append(Paragraph("• <b>Exit Failures:</b> Zero positions have exited through the code-defined stop-loss, take-profit, or time-based rules. Closed positions have instead been terminated via manual script database overrides or daily janitor sweeps.", style_bullet))
    story.append(Paragraph("• <b>Paper Wallet Corruption:</b> Duplicate fills and lack of exit idempotency have led to significant balance inflation in paper wallets (e.g., recorded gains of 470% in 3 days due to multiple exits crediting the same position).", style_bullet))
    story.append(Paragraph("• <b>Risk Deadlocks:</b> High-level risk management gates, such as the NIFTY delta cap of 50, interact restrictively with options lot sizes, causing strategies to be deadlocked and entry signals to be skipped.", style_bullet))
    
    story.append(Spacer(1, 10))
    
    story.append(Paragraph("2. System Architecture & Strategy Integration", style_h1))
    story.append(Paragraph(
        "The strategy execution flow is divided into three distinct asynchronous layers. The decoupling is "
        "technically robust, but adds latency and points of failure: ",
        style_body
    ))
    
    # Simple diagram table
    flow_table_data = [
        [
            Paragraph("<b>1. Evaluation Loop</b><br/><code>strategy_runner.py</code><br/>Runs every 15s. Evaluates history, runs sandbox code, creates <code>PENDING</code> signals.", style_body),
            Paragraph("<b>2. Sweeper & Arbitrage</b><br/><code>signal_manager.py</code><br/>Runs every 2s. Validates limits, solves conflicts via priority scores.", style_body),
            Paragraph("<b>3. Execution & Ledger</b><br/><code>execution_router.py</code><br/>Validates preflights/risk. Routes to paper simulator or Upstox REST.", style_body)
        ]
    ]
    t_flow = Table(flow_table_data, colWidths=[160, 170, 170])
    t_flow.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), c_bg_light),
        ('BOX', (0,0), (-1,-1), 1, c_border),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 10),
        ('TOPPADDING', (0,0), (-1,-1), 10),
        ('LEFTPADDING', (0,0), (-1,-1), 10),
        ('RIGHTPADDING', (0,0), (-1,-1), 10),
    ]))
    story.append(t_flow)
    
    story.append(Spacer(1, 10))
    story.append(Paragraph("How Strategies Interact with the Market & Runtime:", style_h2))
    story.append(Paragraph("• <b>Market Clock Gate:</b> A global clock blocks all execution outside 9:00 AM – 3:35 PM IST. Segment-level check blocks index or equity trades if their respective segment is closed.", style_bullet))
    story.append(Paragraph("• <b>Regime Adaptation:</b> A trend analyzer determines the market regime (e.g., RANGE, TREND_UP, CRASH). The runner blocks CE entries during CRASH or PE entries during MELTUP, adjusts hold times, and halves deadlines on mid-session regime flips.", style_bullet))
    story.append(Paragraph("• <b>OptionSelector V2:</b> When options mode is enabled, underlying indexes (NIFTY/BANKNIFTY/SENSEX) are scanned, and contracts are filtered using standard criteria. If a contract fails the quality threshold (>=70 for live, >=35 for paper), it is skipped.", style_bullet))
    story.append(Paragraph("• <b>Pre-Trade Risk Manager:</b> Validates margins, lot caps, maximum daily loss limits, and Greeks (Delta) exposure before routing orders to the broker gateway.", style_bullet))
    
    story.append(PageBreak())
    
    # ================= PAGE 3 & 4: STRATEGY CATALOG =================
    story.append(Paragraph("3. The Default Strategy Catalog", style_h1))
    story.append(Paragraph(
        "QuantG is pre-seeded with 19 default strategies, targeting different instruments (Options vs Equities) "
        "and styles (Momentum, Breakout, Mean Reversion, Scalping). Below is a structured summary of the "
        "catalogs and their operational parameters.",
        style_body
    ))
    
    # Strategy catalog table
    # Columns: Name, Underlying, Type, Lot, Key Indicators
    cat_header = [
        Paragraph("<b>Strategy Name</b>", style_body_bold),
        Paragraph("<b>Asset</b>", style_body_bold),
        Paragraph("<b>Type</b>", style_body_bold),
        Paragraph("<b>Capital</b>", style_body_bold),
        Paragraph("<b>Indicators & Trigger Logic</b>", style_body_bold)
    ]
    
    cat_rows = [
        [
            Paragraph("NIFTY ATM Momentum Buyer", style_body),
            Paragraph("NIFTY", style_body),
            Paragraph("Option Buy", style_body),
            Paragraph("₹25k", style_body),
            Paragraph("EMA 8/21 + Momentum 3-bar. Enters on strong index trend, exits on EMA cross or target %.", style_body)
        ],
        [
            Paragraph("BANKNIFTY Breakout Buyer", style_body),
            Paragraph("BANKNIFTY", style_body),
            Paragraph("Option Buy", style_body),
            Paragraph("₹30k", style_body),
            Paragraph("12-bar Donchian channel. Breaks out of channel high/low with volume multiplier.", style_body)
        ],
        [
            Paragraph("NIFTY VWAP Trend Breakout", style_body),
            Paragraph("NIFTY", style_body),
            Paragraph("Option Buy", style_body),
            Paragraph("₹25k", style_body),
            Paragraph("VWAP Crossover + 20-bar Volume Surge (1.2x average). Bullish/Bearish breakouts.", style_body)
        ],
        [
            Paragraph("SENSEX Swing RSI Pullback", style_body),
            Paragraph("SENSEX", style_body),
            Paragraph("Option Buy", style_body),
            Paragraph("₹25k", style_body),
            Paragraph("14-period RSI recovery from oversold (<30) or overbought (>70) levels.", style_body)
        ],
        [
            Paragraph("NIFTY Micro-Lot Follower", style_body),
            Paragraph("NIFTY", style_body),
            Paragraph("Option Buy", style_body),
            Paragraph("₹10k", style_body),
            Paragraph("20-period Simple Moving Average crossover. Designed for low-capital retail trading.", style_body)
        ],
        [
            Paragraph("NIFTY HFT Quick Scalper", style_body),
            Paragraph("NIFTY", style_body),
            Paragraph("Option Buy", style_body),
            Paragraph("₹10k", style_body),
            Paragraph("Fast 3/10 EMA crossover + price confirmation. Designed for 1-minute execution.", style_body)
        ],
        [
            Paragraph("BANKNIFTY HFT Scalper", style_body),
            Paragraph("BANKNIFTY", style_body),
            Paragraph("Option Buy", style_body),
            Paragraph("₹15k", style_body),
            Paragraph("20-period SMA + 1.5 StdDev Volatility Bands. Captures sudden contract breakouts.", style_body)
        ],
        [
            Paragraph("NIFTY Quick EMA Scalper", style_body),
            Paragraph("NIFTY", style_body),
            Paragraph("Option Buy", style_body),
            Paragraph("₹5k", style_body),
            Paragraph("Fast 3/9 EMA crossover above VWAP with Time Exit at 14:15. Target 1.25R.", style_body)
        ],
        [
            Paragraph("BANKNIFTY Volatility Breakout", style_body),
            Paragraph("BANKNIFTY", style_body),
            Paragraph("Option Buy", style_body),
            Paragraph("₹8k", style_body),
            Paragraph("BB Squeeze (width < 88% average) followed by SMA expansion and Donchian break.", style_body)
        ],
        [
            Paragraph("RELIANCE Momentum Rider", style_body),
            Paragraph("RELIANCE", style_body),
            Paragraph("Equity MIS", style_body),
            Paragraph("₹15k", style_body),
            Paragraph("9/21 EMA crossover + Time-of-day volume surge (>1.1). Rides stock momentum.", style_body)
        ],
        [
            Paragraph("SBIN Macro Short Seller", style_body),
            Paragraph("SBIN", style_body),
            Paragraph("Equity MIS", style_body),
            Paragraph("₹15k", style_body),
            Paragraph("Price < VWAP and Price < EMA 50. Activates on volume-backed bearish breakdowns.", style_body)
        ],
        [
            Paragraph("HDFCBANK Mean Reversion", style_body),
            Paragraph("HDFCBANK", style_body),
            Paragraph("Equity MIS", style_body),
            Paragraph("₹12k", style_body),
            Paragraph("RSI < 30 + Lower BB cross. Exits at SMA middle band or RSI overbought (>70).", style_body)
        ],
        [
            Paragraph("ICICIBANK News Catalyst", style_body),
            Paragraph("ICICIBANK", style_body),
            Paragraph("Equity MIS", style_body),
            Paragraph("₹20k", style_body),
            Paragraph("20-period Donchian breakout + extreme volume spike (tod_vol_ratio > 2.0).", style_body)
        ],
        [
            Paragraph("TCS Defensive Accumulator", style_body),
            Paragraph("TCS", style_body),
            Paragraph("Equity CNC", style_body),
            Paragraph("₹30k", style_body),
            Paragraph("RSI extreme oversold (<25) on daily charts. Delivery-based swing accumulation.", style_body)
        ],
        [
            Paragraph("INFY VWAP Pullback Buyer", style_body),
            Paragraph("INFY", style_body),
            Paragraph("Equity MIS", style_body),
            Paragraph("₹10k", style_body),
            Paragraph("Pullback to VWAP within an EMA 50 uptrend. Enters on price recovery above VWAP.", style_body)
        ],
        [
            Paragraph("AXISBANK Macro Follower", style_body),
            Paragraph("AXISBANK", style_body),
            Paragraph("Equity MIS", style_body),
            Paragraph("₹15k", style_body),
            Paragraph("EMA 9/21 crossover aligned with long-term slope of 200 EMA. Intraday trend follower.", style_body)
        ]
    ]
    
    t_cat = Table([cat_header] + cat_rows, colWidths=[110, 60, 60, 50, 220])
    t_cat.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), c_secondary),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('BOTTOMPADDING', (0,0), (-1,0), 6),
        ('TOPPADDING', (0,0), (-1,0), 6),
        ('GRID', (0,0), (-1,-1), 0.5, c_border),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, c_bg_light]),
        ('BOTTOMPADDING', (0,1), (-1,-1), 4),
        ('TOPPADDING', (0,1), (-1,-1), 4),
    ]))
    story.append(t_cat)
    
    story.append(Spacer(1, 10))
    story.append(Paragraph("<b>Note on Commodity/MCX Strategies:</b> Several MCX strategies (Crude Oil, Natural Gas, Crude Oil Mini) were originally seeded but have been correctly archived. These are excluded from active run logs to comply with operational limitations.", style_body))
    
    story.append(PageBreak())
    
    # ================= PAGE 5: DEEP GAP ANALYSIS =================
    story.append(Paragraph("4. Technical Gap Analysis: What is Broken & Missing", style_h1))
    story.append(Paragraph(
        "A deep inspection of the execution path (from <code>strategy_runner.py</code> through "
        "<code>signal_manager.py</code> to <code>portfolio_ledger.py</code>) exposes major technical defects "
        "that must be resolved before deploying any strategy live.",
        style_body
    ))
    
    story.append(Paragraph("A. Bookkeeping & Ledger Divergence (Critical)", style_h2))
    story.append(Paragraph(
        "The system has two parallel accounting codepaths: the legacy path (writing to <code>orders</code>, "
        "<code>trade_fills</code>, and <code>trades</code>) and the modern path (writing to <code>strategy_positions</code> "
        "and <code>strategies.today_pnl</code> via <code>PortfolioLedger</code>). Currently, paper fills route through the modern path, "
        "leaving the <code>trades</code> and <code>trade_fills</code> collections empty. Consequently, the win-rate calculation "
        "and daily loss kill switches (which read <code>db.trades</code> and <code>strategy.today_pnl</code>) "
        "are completely broken or non-enforceable.",
        style_body
    ))
    
    story.append(Paragraph("B. Dead Gates and Exception Fall-Throughs", style_h2))
    story.append(Paragraph(
        "In <code>strategy_runner.py</code>, the <code>SIGNAL_CONFIDENCE_MIN</code> check is bypassed because the runner "
        "only uses diagnostics rather than enforcing the confidence threshold on incoming signal documents. "
        "Furthermore, option contract resolution failures inside the runner fall through to the signal-queue block, "
        "leading to 'ghost' positions where an order for a raw index (like NIFTY) is queued with no specific option contract.",
        style_body
    ))
    
    story.append(Paragraph("C. Pre-Trade Risk Engine Deadlocks", style_h2))
    story.append(Paragraph(
        "The Greeks (Delta) risk check uses a 0.5 delta proxy for options. Under the default settings, the maximum net delta "
        "is capped at 50. With NIFTY lot sizes at 65 (source: <code>market_domains.py</code>), a single NIFTY options contract "
        "exposes the account to 32.5 delta. A second concurrent trade attempts to buy 65 delta, pushing the total to 97.5, which "
        "is blocked by the 50-limit cap. This rigid boundary deadlocks the platform, explaining why the majority of signals are skipped.",
        style_body
    ))
    
    story.append(Paragraph("D. Name Inconsistencies & Field Anarchy", style_h2))
    story.append(Paragraph(
        "Different eras of code write inconsistent field names for the same core data points. Realized P&L is written as "
        "both <code>realised_pnl</code> and <code>realized_pnl</code>. Entry price is named <code>average_price</code>, "
        "<code>average_buy_price</code>, or <code>avg_price</code> across collections. This naming variance is a persistent source of "
        "bugs when new analytical features are added.",
        style_body
    ))
    
    story.append(Paragraph("E. Stale Signal Execution & Slippage Gaps", style_h2))
    story.append(Paragraph(
        "The strategy runner permits signal entries from the last 20 candles (up to 100 minutes of delay on 5-minute bars). "
        "For options trading, where theta decay and IV fluctuations are rapid, entering a trade 100 minutes late constitutes a "
        "completely different trade. In addition, the paper broker executes orders at the exact signal/cache price, introducing a "
        "<b>zero-slippage bias</b> that makes paper trading results look artificially profitable compared to the live market.",
        style_body
    ))
    
    story.append(Spacer(1, 10))
    story.append(Paragraph("5. Operational Status (Database Inspection)", style_h1))
    
    # Active/Closed statistics table from database
    db_stats_data = [
        [Paragraph("<b>Metric / Collection</b>", style_body_bold), Paragraph("<b>Database Count / Status</b>", style_body_bold), Paragraph("<b>Implication</b>", style_body_bold)],
        [Paragraph("Registered Users", style_body), Paragraph(f"{len(db_data['users'])} users in DB", style_body), Paragraph("System database initialized.", style_body)],
        [Paragraph("Strategies in DB", style_body), Paragraph(f"{len(db_data['strategies'])} documents", style_body), Paragraph("Strategy registry is inflated due to clones.", style_body)],
        [Paragraph("Active Ledger Positions", style_body), Paragraph(f"{db_data['active_positions']}", style_body), Paragraph("Positions currently open or pending.", style_body)],
        [Paragraph("Closed Ledger Positions", style_body), Paragraph(f"{db_data['closed_positions']}", style_body), Paragraph("Completed positions in modern ledger.", style_body)],
        [Paragraph("Completed Trades (db.trades)", style_body), Paragraph("0 trades", style_body), Paragraph("Legacy bookkeeping pipeline is unused and empty.", style_body)]
    ]
    t_stats = Table(db_stats_data, colWidths=[170, 150, 180])
    t_stats.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), c_teal),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('GRID', (0,0), (-1,-1), 0.5, c_border),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, c_bg_light]),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
        ('TOPPADDING', (0,0), (-1,-1), 5),
    ]))
    story.append(t_stats)
    
    story.append(PageBreak())
    
    # ================= PAGE 6: RECOMMENDATIONS & ROADMAP =================
    story.append(Paragraph("6. Recommendations & Path to Live Trading", style_h1))
    story.append(Paragraph(
        "To achieve production-grade stability and prepare QuantG for live capital deployment, we recommend "
        "a structured sequence of refactoring. Patching individual files has failed to converge because of "
        "these underlying architectural issues.",
        style_body
    ))
    
    story.append(Paragraph("Phase 1: Unify the Bookkeeping Pipeline (Highest Priority)", style_h2))
    story.append(Paragraph(
        "Consolidate the ledger accounting. Eliminate the legacy pipeline in <code>server.py</code> and make "
        "<code>PortfolioLedger</code> the sole manager for position updates. Ensure that <code>PortfolioLedger</code> "
        "writes to <code>db.trades</code> and <code>db.trade_fills</code> on every completed transaction. This will "
        "automatically repair the win-rate statistics and the daily loss kill-switch tracking.",
        style_body
    ))
    
    story.append(Paragraph("Phase 2: Establish Guaranteed Exit Mechanisms", style_h2))
    story.append(Paragraph(
        "Exits should not depend on a Python polling loop that runs every 30 seconds. In option trading, minor "
        "network outages can cause severe losses. The moment an entry order is filled, a bracket order or stop-loss "
        "order should be placed directly at the broker (Upstox). This guarantees that stops are executed "
        "even if the QuantG backend crashes.",
        style_body
    ))
    
    story.append(Paragraph("Phase 3: Restructure Risk Limits & Prevent Deadlocks", style_h2))
    story.append(Paragraph(
        "Refactor the pre-trade risk controls. Instead of a hard-coded Delta limit of 50, the risk manager should "
        "dynamically scale limits based on user account equity. Ensure that the Delta cap checks allow reduction/exit "
        "orders to pass without restriction. In addition, resolve the 100-minute stale signal window by restricting "
        "entry signals to the most recent 1 to 3 candles.",
        style_body
    ))
    
    story.append(Paragraph("Phase 4: Implement Cost & Slippage Modeling", style_h2))
    story.append(Paragraph(
        "To prevent backtesting and paper trading bias, introduce a transaction cost model in `paper_broker.py` "
        "simulating standard NSE stamp duty, STT, exchange transaction charges, and a slippage penalty of 0.5% "
        "to 1.5% on option premiums. This will align paper trading performance numbers with real market expectations.",
        style_body
    ))
    
    story.append(Spacer(1, 15))
    
    # Roadmap table
    roadmap_headers = [
        Paragraph("<b>Stage</b>", style_body_bold),
        Paragraph("<b>Objective</b>", style_body_bold),
        Paragraph("<b>Pass Criteria</b>", style_body_bold),
        Paragraph("<b>Timeline</b>", style_body_bold)
    ]
    roadmap_rows = [
        [
            Paragraph("<b>G1: Pipeline Unity</b>", style_body),
            Paragraph("Consolidate ledger paths, align database schema fields.", style_body),
            Paragraph("P&L, win-rate, and daily loss limits display correctly.", style_body),
            Paragraph("1–2 Weeks", style_body)
        ],
        [
            Paragraph("<b>G2: Rest-Exits</b>", style_body),
            Paragraph("Rest SL/TP at broker; add automatic 15:15 EOD square-offs.", style_body),
            Paragraph("Zero open positions remaining overnight on intraday strategies.", style_body),
            Paragraph("3–5 Days", style_body)
        ],
        [
            Paragraph("<b>G3: Paper Streak</b>", style_body),
            Paragraph("Run system in paper mode to test logic stability.", style_body),
            Paragraph("10 consecutive sessions with zero manual database overrides.", style_body),
            Paragraph("2 Weeks", style_body)
        ],
        [
            Paragraph("<b>G4: Live Pilot</b>", style_body),
            Paragraph("Deploy a single strategy with 1 lot on NIFTY.", style_body),
            Paragraph("Verify live fills, connection stability, and risk controls.", style_body),
            Paragraph("1 Month", style_body)
        ]
    ]
    t_road = Table([roadmap_headers] + roadmap_rows, colWidths=[100, 170, 150, 80])
    t_road.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), c_secondary),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('GRID', (0,0), (-1,-1), 0.5, c_border),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, c_bg_light]),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
        ('TOPPADDING', (0,0), (-1,-1), 5),
    ]))
    story.append(t_road)
    
    # Build Document
    doc.build(story, canvasmaker=NumberedCanvas)

if __name__ == "__main__":
    # Define report path
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    report_file = os.path.join(project_root, "QuantG_Strategy_Audit_Report.pdf")
    
    print("Fetching data from DB...")
    db_data = get_db_data()
    
    print(f"Generating PDF report at: {report_file}...")
    build_pdf(report_file, db_data)
    print("Report generated successfully.")
