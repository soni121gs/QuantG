"""Shared pytest fixtures — clears module-level state between tests."""
import pytest


_INTEGRATION_MODULES = {
    "backend_test.py",
    "test_agent_tools.py",
    "test_environment_preflight.py",
    "test_iteration2.py",
    "test_iteration3.py",
    "test_iteration4.py",
    "test_iteration5.py",
    "test_p4_gemini_model.py",
    "test_option_state_ledger.py",
    "test_paper_execution_lifecycle.py",
}


def pytest_collection_modifyitems(items):
    for item in items:
        if item.path.name in _INTEGRATION_MODULES:
            item.add_marker(pytest.mark.integration)


@pytest.fixture(autouse=True)
def clear_ltp_cache():
    """Clear the LTP cache before each test to prevent inter-test pollution."""
    try:
        import ltp_cache
        ltp_cache.clear_all()
    except Exception:
        pass
    yield
    try:
        import ltp_cache
        ltp_cache.clear_all()
    except Exception:
        pass
