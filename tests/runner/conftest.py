"""Fixtures for the runner's tests."""

# system imports
import time

# 3rd party imports
import pytest


@pytest.fixture
def deadline() -> float:
    """A `time.monotonic()` an hour away: a look that is given it always finishes."""
    return time.monotonic() + 3600
