import gzip
import shutil
from pathlib import Path

from core.bhavcopy_store import BhavcopyStore
from core.earnings_calendar import events_for, normalize_event, store_events
from core.india_flows import parse_participant_oi_csv, store_participant_oi, get_participant_oi
from scripts.bhavcopy_ingest import parse_legacy_fo_csv, parse_udiff_csv, write_day
from scripts.earnings_calendar_fetch_nse import normalize_rows


def _scratch(name: str) -> Path:
    path = Path(__file__).resolve().parents[1] / f"_tmp_{name}"
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def test_bhavcopy_ingest_accepts_stock_fo_rows():
    tmp_path = _scratch("phase1_bhavcopy")
    raw = """TradDt,FinInstrmTp,TckrSymb,XpryDt,StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,SttlmPric,UndrlygPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,NewBrdLotQty
2025-01-09,STF,RELIANCE,2025-01-30,,,RELIANCE25JANFUT,2500,2520,2480,2510,2510,2510,100,5,1000,250
2025-01-09,STO,RELIANCE,2025-01-30,2500,CE,RELIANCE25JAN2500CE,100,120,90,110,110,2510,200,10,2000,250
2025-01-09,STO,RELIANCE,2025-01-30,2500,PE,RELIANCE25JAN2500PE,80,90,70,75,75,2510,150,-5,1500,250
2025-01-09,IDO,NIFTY,2025-01-30,24000,CE,NIFTY25JAN24000CE,10,12,8,9,9,24000,1,0,10,65
"""
    rows = parse_udiff_csv(raw, {"RELIANCE"}, {"STO", "STF"})
    assert {r["instr_type"] for r in rows} == {"STO", "STF"}
    write_day(__import__("datetime").date(2025, 1, 9), "BhavCopy_FO_", rows, store_dir=str(tmp_path))

    store = BhavcopyStore(root=str(tmp_path))
    assert store.underlying_daily("RELIANCE", "2025-01-09", "2025-01-09")[0]["close"] == 2510.0
    chain = store.option_chain("RELIANCE", "2025-01-09")
    assert chain["2025-01-30"][2500.0]["CE"]["close"] == "110"
    shutil.rmtree(tmp_path)


def test_bhavcopy_ingest_accepts_legacy_pre_2024_rows():
    raw = """INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP,
FUTIDX,NIFTY,25-Jan-2024,0,XX,21500,21600,21400,21550,21550,10,1,100,5,29-DEC-2023,
OPTIDX,NIFTY,25-Jan-2024,21500,CE,100,120,90,110,110,20,2,200,10,29-DEC-2023,
"""
    rows = parse_legacy_fo_csv(raw, {"NIFTY"}, {"IDF", "IDO"})
    assert [r["instr_type"] for r in rows] == ["IDF", "IDO"]
    assert rows[0]["date"] == "2023-12-29"
    assert rows[1]["expiry"] == "2024-01-25"
    assert rows[1]["option_type"] == "CE"


def test_earnings_calendar_store_and_lookup(monkeypatch):
    tmp_path = _scratch("phase1_earnings")
    monkeypatch.setattr("core.earnings_calendar.STORE_ROOT", str(tmp_path))
    store_events([
        {"symbol": "RELIANCE", "date": "09-01-2025", "source": "test"},
        {"symbol": "TCS", "date": "2025-01-10", "source": "test"},
    ])
    events = events_for("RELIANCE", "2025-01-01", "2025-01-31")
    assert len(events) == 1
    assert events[0]["date"] == "2025-01-09"
    shutil.rmtree(tmp_path)


def test_earnings_calendar_accepts_nse_board_meeting_fields():
    event = normalize_event({
        "SYMBOL": "RELIANCE",
        "COMPANY NAME": "Reliance Industries Limited",
        "MEETING DATE": "29-Jul-2025",
        "Purpose": "Financial Results",
    })
    assert event["symbol"] == "RELIANCE"
    assert event["date"] == "2025-07-29"
    assert event["headline"] == "Reliance Industries Limited"


def test_nse_earnings_fetch_filters_financial_results():
    rows = normalize_rows([
        {
            "SYMBOL": "RELIANCE",
            "Purpose": "Financial Results",
            "Details": "To consider unaudited financial results",
            "MEETING DATE": "29-Jul-2025",
        },
        {
            "SYMBOL": "RELIANCE",
            "Purpose": "Issue of Bonus Shares",
            "Details": "Board meeting",
            "MEETING DATE": "03-May-2024",
        },
    ])
    assert len(rows) == 1
    assert rows[0]["symbol"] == "RELIANCE"
    assert rows[0]["date"] == "29-Jul-2025"


def test_participant_oi_parse_and_store(monkeypatch):
    tmp_path = _scratch("phase1_participant_oi")
    monkeypatch.setattr("core.india_flows.PARTICIPANT_OI_ROOT", str(tmp_path))
    raw = """Client Type,Future Index Long,Future Index Short,Option Index Call Long,Option Index Put Short
FII,1,2,3,4
Client,5,6,7,8
"""
    rows = parse_participant_oi_csv(raw)
    assert rows[0]["participant"] == "FII"
    assert rows[0]["future_index_long"] == 1.0
    assert store_participant_oi("2026-07-17", rows) == 2
    payload = get_participant_oi("2026-07-17")
    assert payload["rows"][1]["participant"] == "CLIENT"
    shutil.rmtree(tmp_path)


def test_participant_oi_parse_skips_nse_title_preamble():
    raw = '''"Participant wise Open Interest (no. of contracts) in Equity Derivatives as on Jul 05,2024",,,,,,,,,,,,,,
Client Type,Future Index Long,Future Index Short,Future Stock Long,Future Stock Short,Option Index Call Long
FII,1,2,3,4,5
'''
    rows = parse_participant_oi_csv(raw)
    assert len(rows) == 1
    assert rows[0]["participant"] == "FII"
    assert rows[0]["option_index_call_long"] == 5.0
