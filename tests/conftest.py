import pytest

from saferoute.config import Settings
from saferoute.notify import ConsoleNotifier
from saferoute.store import Store


@pytest.fixture
def cfg():
    return Settings(admin_key="test-admin", db_path=":memory:")


@pytest.fixture
def store():
    s = Store(":memory:")
    s.upsert_vehicle("UAX 123A", "bus", "Link SACCO")
    return s


@pytest.fixture
def notifier():
    return ConsoleNotifier()
