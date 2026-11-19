"""Shared by every test."""

import pytest

from proatc import atis, flightlog


@pytest.fixture(autouse=True)
def no_runways_from_another_test():
    """What one test's engine found of an airport's runways, or its plan's runways, is not another test's world:
    test worlds reuse the same idents (EGXX) with different taxiways."""
    atis.forget_runways()


@pytest.fixture(autouse=True)
def no_secrets_of_this_computer(monkeypatch):
    """The flight log hides the OpenRouter key and the SimBrief username wherever they appear. Tests do not read
    this computer's: a test of the hiding plants placeholders of its own (tests/test_journal.py)."""
    monkeypatch.setattr(flightlog, "known_secrets", lambda: [])
