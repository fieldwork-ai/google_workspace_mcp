"""Fixtures shared by every test."""

import sys

import pytest


@pytest.fixture(autouse=True)
def fresh_oauth_config():
    """Forget the memoised OAuth configuration after each test.

    ``get_oauth_config`` keeps a module-level singleton. A test that sets
    environment variables and reloads it gets its variables restored by
    ``monkeypatch``, but not the singleton, so a stateless config leaked into
    every later test and turned their local-file reads into hosted refusals.
    Clearing it lets the next read build it again from the restored environment,
    whichever teardown runs first. The module is looked up when the test ends
    because some tests import ``auth.oauth_config`` afresh, leaving any earlier
    reference to it orphaned.
    """
    yield
    module = sys.modules.get("auth.oauth_config")
    if module is not None:
        with module._oauth_config_lock:
            module._oauth_config = None
