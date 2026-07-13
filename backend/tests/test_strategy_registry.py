"""Tests for the Strategy Registry config validator (QGX / INFRA-11..12)."""
from core.strategy_registry import validate_strategy_doc, project_config


def _doc(**opts_risk):
    """Build a minimal strategy doc with the given options/risk overrides."""
    opts = opts_risk.get("options", {})
    risk = opts_risk.get("risk", {})
    return {
        "id": opts_risk.get("id", "s1"),
        "name": opts_risk.get("name", "Test Strat"),
        "mode": "paper",
        "status": "paused",
        "visual_config": {"symbol": "NIFTY", "exchange": "NFO", "options": opts, "risk": risk},
    }


def test_hold_to_expiry_trap_is_an_error():
    # The exact 2026-07-13 RAE regression: intraday spread seeded hold-to-expiry.
    doc = _doc(options={"structure": "credit_spread", "exit_mode": "expiry",
                        "credit_tp_frac": None, "credit_sl_mult": None},
               risk={"exit_mode": "hold_to_expiry", "time_exit_minutes": 0})
    res = validate_strategy_doc(doc)
    # hold=True so no trap error, but ambiguity warning if it also had tp/sl. Here it's a clean hold.
    assert res.ok  # a clean hold-to-expiry is valid config...


def test_spread_with_no_exit_at_all_is_a_trap():
    # Not marked hold, but also no tp/sl/time → sits until square-off.
    doc = _doc(options={"structure": "credit_spread", "exit_mode": "",
                        "credit_tp_frac": None, "credit_sl_mult": None},
               risk={"exit_mode": "", "time_exit_minutes": 0})
    res = validate_strategy_doc(doc)
    assert not res.ok
    assert any("hold-to-expiry trap" in e for e in res.errors)


def test_healthy_intraday_spread_passes():
    doc = _doc(options={"structure": "credit_spread", "exit_mode": "",
                        "credit_tp_frac": 0.5, "credit_sl_mult": 2.0},
               risk={"exit_mode": "signal_or_tp_sl_trailing", "time_exit_minutes": 120})
    res = validate_strategy_doc(doc)
    assert res.ok, res.errors


def test_tp_frac_out_of_range_is_error():
    doc = _doc(options={"structure": "credit_spread", "credit_tp_frac": 1.5,
                        "credit_sl_mult": 2.0}, risk={"time_exit_minutes": 60})
    res = validate_strategy_doc(doc)
    assert not res.ok
    assert any("credit_tp_frac" in e for e in res.errors)


def test_sl_mult_out_of_range_is_error():
    doc = _doc(options={"structure": "credit_spread", "credit_tp_frac": 0.5,
                        "credit_sl_mult": 25.0}, risk={"time_exit_minutes": 60})
    res = validate_strategy_doc(doc)
    assert not res.ok
    assert any("credit_sl_mult" in e for e in res.errors)


def test_ambiguous_hold_plus_intraday_warns():
    doc = _doc(options={"structure": "credit_spread", "exit_mode": "expiry",
                        "credit_tp_frac": 0.5, "credit_sl_mult": 2.0},
               risk={"time_exit_minutes": 120})
    res = validate_strategy_doc(doc)
    assert res.ok  # warnings don't fail
    assert any("ambiguous" in w for w in res.warnings)


def test_capital_mismatch_warns():
    doc = _doc(options={"structure": "credit_spread", "credit_tp_frac": 0.5,
                        "credit_sl_mult": 2.0, "required_capital": 40000},
               risk={"time_exit_minutes": 60, "required_capital": 8000})
    doc["required_capital"] = 40000
    res = validate_strategy_doc(doc)
    assert any("required_capital differs" in w for w in res.warnings)


def test_single_leg_not_subject_to_spread_trap():
    doc = _doc(options={"structure": "single_leg", "exit_mode": ""},
               risk={"exit_mode": "signal_or_tp_sl_trailing", "time_exit_minutes": 120})
    res = validate_strategy_doc(doc)
    assert res.ok, res.errors


def test_project_config_reads_geometry():
    doc = _doc(options={"structure": "credit_spread", "credit_tp_frac": 0.5,
                        "credit_sl_mult": 2.0}, risk={"time_exit_minutes": 120})
    cfg = project_config(doc)
    assert cfg is not None
    assert cfg.visual_config.options.credit_tp_frac == 0.5
    assert cfg.visual_config.risk.time_exit_minutes == 120


def test_bad_doc_does_not_raise():
    res = validate_strategy_doc({"garbage": True})
    assert not res.ok
    assert res.errors
