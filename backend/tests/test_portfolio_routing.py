from routes.ai import classify_playbook_by_query


def test_portfolio_questions_select_canonical_portfolio_truth():
    tools = classify_playbook_by_query("What is my whole portfolio exposure and Greek concentration?")
    assert "get_portfolio_snapshot" in tools


def test_scenario_questions_select_snapshot_and_scenario_tools():
    tools = classify_playbook_by_query("What if NIFTY falls 2% and IV rises 5 points?")
    assert "get_portfolio_snapshot" in tools
    assert "get_portfolio_scenario" in tools
