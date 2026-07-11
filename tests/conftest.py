import pytest

import ballast
from ballast._clock import FakeClock


@pytest.fixture(autouse=True)
def isolated_runtime():
    """Each test gets a fresh runtime; no breaker/queue state leaks between tests."""
    ballast.reset()
    yield
    ballast.reset()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
