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
            self.setFillColor(colors.HexColor('#0891B2'))  # Teal — verification, not alarm
            self.drawString(54, 750, "QuantG — Live Trading Readiness Verification")
            self.setFont("Helvetica", 8)
            self.setFillColor(colors.HexColor('#64748B'))  # Slate Gray
            self.drawRightString(558, 750, "Blocker Re-Audit vs. Current Code")
            self.setStrokeColor(colors.HexColor('#CBD5E1'))  # Light Gray
            self.setLineWidth(0.5)
            self.line(54, 742, 558, 742)

        # Draw running footer on all pages
        self.setFont("Helvetica-Bold", 8)
        self.setFillColor(colors.HexColor('#0891B2'))
        self.drawString(54, 40, "READINESS VERIFICATION")
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor('#64748B'))
        self.drawString(190, 40, "— code-verified against the live-trading path")
        page_text = f"Page {self._pageNumber} of {page_count}"
        self.drawRightString(558, 40, page_text)
        self.setStrokeColor(colors.HexColor('#E2E8F0'))
        self.setLineWidth(0.5)
        self.line(54, 52, 558, 52)

        self.restoreState()


# Known production snapshot (VPS quantg DB, captured 2026-06-16) used as a
# fallback so the report stays truthful even when this script is run from a
# machine that cannot reach the production Mongo. Override the live values by
# setting MONGO_URL to a reachable instance (e.g. via an SSH tunnel to the VPS).
FALLBACK_SNAPSHOT = {
    "trade_fills": 77,
    "trades": 38,
    "orders": 82,
    "strategies_total": 96,
    "strategies_live": 0,
    "closed_positions": 38,
    "live_fills": 0,
    "paper_fills": 77,
    "source": "fallback snapshot (VPS, 2026-06-16)",
}


def get_db_data():
    """Retrieve runtime/catalog evidence from MongoDB.

    Connection target is configurable via the MONGO_URL env var so the report
    can be generated against the real production DB (run on the VPS or through
    an SSH tunnel). Falls back to the captured 2026-06-16 snapshot when no DB
    is reachable, so the rendered figures are never fabricated.
    """
    data = dict(FALLBACK_SNAPSHOT)
    mongo_url = os.getenv("MONGO_URL")
    if not mongo_url:
        # No explicit DB target — use the verified production snapshot rather
        # than silently querying whatever local/dev Mongo happens to be running
        # (which would render misleading figures). Run on the VPS or via an SSH
        # tunnel with MONGO_URL set to refresh against production.
        print("MONGO_URL not set — using verified production snapshot (2026-06-16).")
        return data

    try:
        client = MongoClient(mongo_url, serverSelectionTimeoutMS=2500)
        client.admin.command('ping')
        db = client['quantg']

        data.update({
            "trade_fills": db.trade_fills.count_documents({}),
            "trades": db.trades.count_documents({}),
            "orders": db.orders.count_documents({}),
            "strategies_total": db.strategies.count_documents({}),
            "strategies_live": db.strategies.count_documents({"status": "live"}),
            "closed_positions": db.strategy_positions.count_documents({"status": "CLOSED"}),
            "live_fills": db.trade_fills.count_documents({"mode": "live"}),
            "paper_fills": db.trade_fills.count_documents({"mode": "paper"}),
            "source": f"live query ({mongo_url})",
        })
        print(f"Connected to {mongo_url} — using live figures.")
    except Exception as e:
        print(f"MongoDB unreachable at {mongo_url} ({e}). Using captured VPS snapshot.")

    return data


def build_pdf(report_path, db_data):
    # Setup document geometry: Letter page, 0.75 inch (54pt) margins
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
    c_primary = colors.HexColor('#0F172A')   # Slate 900
    c_secondary = colors.HexColor('#0891B2')  # Teal (verification focus)
    c_teal = colors.HexColor('#0E7490')      # Darker teal for subheads
    c_slate = colors.HexColor('#334155')     # Slate 700
    c_bg_light = colors.HexColor('#F8FAFC')  # Light Slate Alternate
    c_border = colors.HexColor('#E2E8F0')    # Border Slate
    c_red = colors.HexColor('#DC2626')       # Alert Red
    c_amber = colors.HexColor('#D97706')     # Amber (residual / tuning)
    c_green = colors.HexColor('#16A34A')     # Green 600 (resolved)

    # Custom Paragraph Styles
    style_cover_title = ParagraphStyle(
        'CoverTitle', parent=styles['Normal'], fontName='Helvetica-Bold',
        fontSize=24, leading=30, textColor=c_primary, spaceAfter=15)

    style_cover_subtitle = ParagraphStyle(
        'CoverSubtitle', parent=styles['Normal'], fontName='Helvetica-Bold',
        fontSize=11, leading=14, textColor=c_secondary, spaceAfter=30)

    style_h1 = ParagraphStyle(
        'Heading1_Custom', parent=styles['Heading1'], fontName='Helvetica-Bold',
        fontSize=16, leading=20, textColor=c_secondary, spaceBefore=18,
        spaceAfter=8, keepWithNext=True)

    style_h2 = ParagraphStyle(
        'Heading2_Custom', parent=styles['Heading2'], fontName='Helvetica-Bold',
        fontSize=11, leading=15, textColor=c_teal, spaceBefore=12,
        spaceAfter=5, keepWithNext=True)

    style_body = ParagraphStyle(
        'Body_Custom', parent=styles['BodyText'], fontName='Helvetica',
        fontSize=9.5, leading=14, textColor=c_slate, spaceAfter=8)

    style_body_bold = ParagraphStyle(
        'BodyBold_Custom', parent=style_body, fontName='Helvetica-Bold',
        textColor=c_primary)

    style_bullet = ParagraphStyle(
        'Bullet_Custom', parent=style_body, leftIndent=15,
        firstLineIndent=-10, spaceAfter=4)

    story = []

    # ================= PAGE 1: COVER PAGE =================
    story.append(Spacer(1, 100))
    story.append(Paragraph("QUANTG PLATFORM LIVE READINESS VERIFICATION", style_cover_subtitle))
    story.append(Paragraph("Blocker Re-Audit: Prior Findings Mapped Against the Current Codebase", style_cover_title))

    # Colored accent bar (Teal — verification)
    story.append(Table(
        [['']], colWidths=[100], rowHeights=[4],
        style=TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), c_secondary),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
            ('TOPPADDING', (0, 0), (-1, -1), 0),
        ])
    ))
    story.append(Spacer(1, 30))

    # Metadata block
    meta_data = [
        [Paragraph("<b>Date:</b>", style_body), Paragraph(datetime.now().strftime("%B %d, %Y"), style_body)],
        [Paragraph("<b>Scope:</b>", style_body), Paragraph("Re-audit of the prior \"NOT READY\" findings against current code + production DB", style_body)],
        [Paragraph("<b>Target Broker:</b>", style_body), Paragraph("Upstox Live V3 Gateway (Nifty/BankNifty Options)", style_body)],
        [Paragraph("<b>Verdict:</b>", style_body), Paragraph("<font color='#16A34A'><b>ALL PRIOR BLOCKERS RESOLVED IN CODE</b></font> — remaining gate is empirical, not structural", style_body)],
        [Paragraph("<b>Remaining Gate:</b>", style_body), Paragraph("Live path is code-complete + unit-tested but never exercised; needs a supervised 1-lot pilot + track record", style_body)],
    ]
    t_meta = Table(meta_data, colWidths=[110, 290])
    t_meta.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
    ]))
    story.append(t_meta)

    story.append(Spacer(1, 80))

    story.append(Paragraph("<b>Why this re-audit exists:</b>", style_body_bold))
    story.append(Paragraph(
        "An earlier audit verdicted the platform \"NOT READY FOR LIVE MONEY\" on the basis of a fixed blocker "
        "checklist (empty trade ledger, exits only via polling, static delta deadlock, stale-signal entries, "
        "no reconciler, no cost model). Each of those items has since been addressed in code — primarily by the "
        "audit-fix campaign (Stages A–D, 2026-06-12) and the live-readiness fixes in commit <code>b38e5c0</code> "
        "(TASK-021, 2026-06-15). This document re-checks every prior claim against the live code path and the "
        "production database so the readiness picture is accurate.",
        style_body
    ))
    story.append(Paragraph(
        "<b>Corrected verdict:</b> The structural blockers are <b>closed</b>. The honest reason live trading is "
        "not yet \"on\" is that the live execution path has <b>never been exercised with real capital</b> "
        "(<code>CORE_ENGINE_LIVE_ENABLED=false</code> by design) and there is <b>no live track record yet</b>. "
        "The next step is a small, supervised live pilot — a founder decision — not another round of bug fixing.",
        style_body
    ))

    story.append(PageBreak())

    # ================= PAGE 2: EVIDENCE + COMPARATIVE MATRIX =================
    story.append(Paragraph("1. Verification Evidence (Production DB)", style_h1))
    story.append(Paragraph(
        f"The prior audit's headline claim was a blank account ledger. Direct counts from the production "
        f"<code>quantg</code> database refute this. Source: <i>{db_data.get('source')}</i>.",
        style_body
    ))

    ev_header = [
        Paragraph("<b>Collection / Metric</b>", style_body_bold),
        Paragraph("<b>Prior Audit Claim</b>", style_body_bold),
        Paragraph("<b>Actual (Production)</b>", style_body_bold),
    ]
    ev_rows = [
        [Paragraph("<code>trade_fills</code>", style_body),
         Paragraph("“remains empty”", style_body),
         Paragraph(f"<font color='#16A34A'><b>{db_data['trade_fills']} fills</b></font> ({db_data['paper_fills']} paper / {db_data['live_fills']} live)", style_body)],
        [Paragraph("<code>trades</code>", style_body),
         Paragraph("“remains empty”", style_body),
         Paragraph(f"<font color='#16A34A'><b>{db_data['trades']} trades</b></font>", style_body)],
        [Paragraph("<code>orders</code>", style_body),
         Paragraph("—", style_body),
         Paragraph(f"{db_data['orders']} orders", style_body)],
        [Paragraph("<code>strategy_positions</code> (closed)", style_body),
         Paragraph("—", style_body),
         Paragraph(f"{db_data['closed_positions']} closed positions with realized P&amp;L", style_body)],
        [Paragraph("<code>strategies</code>", style_body),
         Paragraph("—", style_body),
         Paragraph(f"{db_data['strategies_total']} total, <b>{db_data['strategies_live']} armed</b> right now", style_body)],
    ]
    t_ev = Table([ev_header] + ev_rows, colWidths=[150, 130, 220])
    t_ev.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), c_secondary),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('GRID', (0, 0), (-1, -1), 0.5, c_border),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, c_bg_light]),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
    ]))
    story.append(t_ev)
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "The ledger is populated. Live fills route through the same <code>_book_live_fill_from_order</code> → "
        "<code>PortfolioLedger.process_fill</code> path (server.py) that already writes these paper fills, with "
        "unit coverage in <code>tests/test_audit_fixes.py</code>. <code>live_fills = 0</code> only because the "
        "live engine has never been switched on.",
        style_body
    ))

    story.append(PageBreak())

    # ================= PAGE 3: BLOCKER-BY-BLOCKER STATUS =================
    story.append(Paragraph("2. Prior Blockers — Current Status", style_h1))
    story.append(Paragraph(
        "Each defect from the original audit, its current state in code, and any residual risk that remains.",
        style_body
    ))

    comp_header = [
        Paragraph("<b>Prior Blocker</b>", style_body_bold),
        Paragraph("<b>Current State in Code</b>", style_body_bold),
        Paragraph("<b>Residual Risk</b>", style_body_bold),
    ]

    comp_rows = [
        [
            Paragraph("<b>Zero-ledger blindness</b><br/>(no trade audit trail)", style_body),
            Paragraph("<font color='#16A34A'><b>RESOLVED.</b></font> Both paper and live fills route through <code>PortfolioLedger.process_fill</code> → <code>trade_fills</code>/<code>trades</code>. Verified: 77 fills / 38 trades in prod.", style_body),
            Paragraph("Low. Live path shares the verified code but is unexercised.", style_body),
        ],
        [
            Paragraph("<b>Exits only via polling</b><br/>(no broker-side stop)", style_body),
            Paragraph("<font color='#16A34A'><b>RESOLVED.</b></font> <code>_place_resting_sl_order</code> submits an SL order to Upstox right after the entry fill (server.py ~16268); <code>place_gtt_order</code> exists in the gateway. The 5s poll is now a backstop, not the only line of defense.", style_body),
            Paragraph("<font color='#D97706'>Medium.</font> Resting-SL placement has unit tests but zero live executions — must be watched on the first live fills.", style_body),
        ],
        [
            Paragraph("<b>Static delta cap (50) deadlock</b>", style_body),
            Paragraph("<font color='#16A34A'><b>RESOLVED.</b></font> Delta exposure cap is config-driven (<code>RISK.DELTA_CAP</code>, default 500) and the check in <code>risk_manager.py</code> is no longer a hard static 50.", style_body),
            Paragraph("Low. Tune <code>DELTA_CAP</code> to live account size before scaling strategies.", style_body),
        ],
        [
            Paragraph("<b>Stale-signal entries</b><br/>(20-bar-old setups)", style_body),
            Paragraph("<font color='#16A34A'><b>RESOLVED.</b></font> <code>strategy_runner.py</code> rejects signals older than <code>LIVE_CANDLE_MAX_AGE</code> (default 1200s = 20 min).", style_body),
            Paragraph("<font color='#D97706'>Low–Med.</font> 20 min is looser than the 10-min guideline for weekly options; consider tightening per-strategy.", style_body),
        ],
        [
            Paragraph("<b>No open-position reconciler</b>", style_body),
            Paragraph("<font color='#16A34A'><b>RESOLVED.</b></font> <code>position_reconciler.py</code> compares broker vs <code>strategy_positions</code> and blocks on mismatch (live-mode only).", style_body),
            Paragraph("<font color='#D97706'>Medium.</font> Reconciler logic is untested against a real Upstox account — validate during the pilot.", style_body),
        ],
        [
            Paragraph("<b>No slippage / cost model</b><br/>(0% slippage)", style_body),
            Paragraph("<font color='#16A34A'><b>RESOLVED.</b></font> <code>execution_router.py</code> applies slippage (5 bps), partial fills, stale-quote rejection, and the full F&amp;O cost stack (brokerage, STT, exch txn, SEBI, stamp, GST).", style_body),
            Paragraph("<font color='#D97706'>Medium.</font> 5 bps flat is optimistic for weekly-options market orders — raise for conservative paper realism.", style_body),
        ],
        [
            Paragraph("<b>LTP feed fallback / P&amp;L freeze</b>", style_body),
            Paragraph("<font color='#16A34A'><b>MITIGATED.</b></font> A WS drop still freezes displayed P&amp;L, but the position is now protected by the broker-side resting SL rather than riding naked.", style_body),
            Paragraph("<font color='#D97706'>Low–Med.</font> Add a feed-staleness alert so a frozen P&amp;L is visibly flagged, not silent.", style_body),
        ],
    ]

    t_comp = Table([comp_header] + comp_rows, colWidths=[130, 240, 130])
    t_comp.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), c_secondary),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('GRID', (0, 0), (-1, -1), 0.5, c_border),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, c_bg_light]),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
    ]))
    story.append(t_comp)

    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "<b>Net:</b> all six structural blockers are resolved or mitigated in code. The residuals are about "
        "<i>exercising and calibrating</i> the live path — not building it.",
        style_body
    ))

    story.append(PageBreak())

    # ================= PAGE 4: TRUE REMAINING GATE =================
    story.append(Paragraph("3. The True Remaining Gate to Live", style_h1))
    story.append(Paragraph(
        "Because the structural work is done, what stands between QuantG and live capital is empirical and "
        "operational, not architectural:",
        style_body
    ))

    gate_header = [
        Paragraph("<b>Remaining Item</b>", style_body_bold),
        Paragraph("<b>What's Needed</b>", style_body_bold),
        Paragraph("<b>Type</b>", style_body_bold),
    ]
    gate_rows = [
        [
            Paragraph("<b>1. Exercise the live path</b>", style_body),
            Paragraph("Founder flips <code>CORE_ENGINE_LIVE_ENABLED=true</code> and runs a supervised 1-lot pilot. Confirm: a resting SL appears in the Upstox terminal on entry, live fills write to <code>trade_fills</code>, and the reconciler stays clean.", style_body),
            Paragraph("<font color='#D97706'><b>Empirical</b></font>", style_body),
        ],
        [
            Paragraph("<b>2. Build a track record</b>", style_body),
            Paragraph("Accumulate verified live P&amp;L toward the Phase-1 targets (3–5%/mo, Sharpe &gt; 1.5, max DD &lt; 20%, win rate &gt; 55%). This is the real fundraising asset.", style_body),
            Paragraph("<font color='#D97706'><b>Time / Data</b></font>", style_body),
        ],
        [
            Paragraph("<b>3. Calibrate slippage</b>", style_body),
            Paragraph("Raise <code>PAPER_SLIPPAGE_PCT</code> above 5 bps for weekly options so paper metrics don't overstate net returns vs. live.", style_body),
            Paragraph("<font color='#0D9488'><b>Tuning</b></font>", style_body),
        ],
        [
            Paragraph("<b>4. Tighten stale-signal window</b>", style_body),
            Paragraph("Consider lowering <code>STRATEGY_LIVE_CANDLE_MAX_AGE_SEC</code> from 1200s toward ~600s for fast option strategies.", style_body),
            Paragraph("<font color='#0D9488'><b>Tuning</b></font>", style_body),
        ],
        [
            Paragraph("<b>5. Feed-staleness alert</b>", style_body),
            Paragraph("Surface a visible warning when the WS feed is stale so a frozen P&amp;L is never mistaken for a flat position.", style_body),
            Paragraph("<font color='#0D9488'><b>Observability</b></font>", style_body),
        ],
    ]
    t_gate = Table([gate_header] + gate_rows, colWidths=[140, 290, 70])
    t_gate.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), c_secondary),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('GRID', (0, 0), (-1, -1), 0.5, c_border),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, c_bg_light]),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
    ]))
    story.append(t_gate)

    story.append(Spacer(1, 12))
    story.append(Paragraph("4. Recommended Live Pilot (Unchanged — Still Sound)", style_h1))
    story.append(Paragraph(
        "When the founder chooses to go live, the conservative rollout from the original audit still applies:",
        style_body
    ))
    story.append(Paragraph("1. <b>Scale-down capital:</b> one lot on a single index strategy (e.g. NIFTY ATM momentum buyer). Restrict live capital to ~₹25,000.", style_bullet))
    story.append(Paragraph("2. <b>Manual supervision:</b> watch the terminal 9:15 AM–3:30 PM for the first 3 sessions; confirm resting SLs and live ledger writes on the first real fills.", style_bullet))
    story.append(Paragraph("3. <b>Aggressive circuit breakers:</b> tight daily loss limit (e.g. ₹1,000) and verify the global kill switch.", style_bullet))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        "<i>Note: enabling live trading is a founder decision and is deliberately not performed by AI agents.</i>",
        style_body
    ))

    # Build Document
    doc.build(story, canvasmaker=NumberedCanvas)


if __name__ == "__main__":
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    report_file = os.path.join(project_root, "QuantG_Live_Readiness_Analysis.pdf")

    print("Fetching data from DB...")
    db_data = get_db_data()

    print(f"Generating PDF report at: {report_file}...")
    build_pdf(report_file, db_data)
    print("Report generated successfully.")
