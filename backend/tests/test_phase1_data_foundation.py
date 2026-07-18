import gzip
import shutil
from pathlib import Path

from core.bhavcopy_store import BhavcopyStore
from core.earnings_calendar import events_for, store_events
from core.india_flows import parse_participant_oi_csv, store_participant_oi, get_participant_oi
from scripts.bhavcopy_ingest import parse_udiff_csv, write_day


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
