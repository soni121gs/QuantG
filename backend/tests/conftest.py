"""Shared pytest fixtures — clears module-level state between tests."""
import pytest


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
