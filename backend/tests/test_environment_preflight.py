"""Environment preflights for deploy/integration checks.

These tests skip when local infrastructure is missing. That keeps low-spec
laptops from reporting setup gaps as QuantG application failures.
"""
from __future__ import annotations

import subprocess

import pytest
import requests


def test_docker_daemon_preflight():
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        pytest.skip("Docker daemon not running. Start Docker Desktop or run on VPS.")
    if result.returncode != 0:
        pytest.skip("Docker daemon not running. Start Docker Desktop or run on VPS.")


def test_backend_preflight():
    try:
        result = requests.get("http://127.0.0.1:8000/api/", timeout=3)
    except requests.RequestException:
        pytest.skip("Backend not running. Start backend before paper trade test.")
    if result.status_code >= 500:
        pytest.fail(f"Backend health returned {result.status_code}: {result.text[:300]}")
