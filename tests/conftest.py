"""Keep legacy decision tests on TypeSafe unless a test selects Mercury."""

import pytest


@pytest.fixture(autouse=True)
def pin_typesafe_decision_provider(monkeypatch):
    monkeypatch.setenv("DECISION_PROVIDER", "typesafe")
