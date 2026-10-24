"""G2-only pytest plugin. Supplies phase evidence; never changes a test outcome."""
import json
import os
from pathlib import Path
import sys

import pytest

from cw_b3_probe import KnockoutTrace

TRACE = None


def pytest_configure(config):
    global TRACE
    TRACE = KnockoutTrace(json.loads(os.environ["CW_B3_SCOPE"]))
    sys.settrace(TRACE.trace)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    item.user_properties.append(("cw_b3_nodeid", item.nodeid))
    TRACE.unit = item.nodeid
    try:
        yield
    finally:
        TRACE.unit = None
        TRACE.phase = "collection"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    TRACE.phase = "setup"
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    TRACE.phase = "item"
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    TRACE.phase = "teardown"
    yield


def pytest_sessionfinish(session, exitstatus):
    sys.settrace(None)
    Path(os.environ["CW_B3_EVENTS"]).write_text(json.dumps(TRACE.events))


def pytest_unconfigure(config):
    sys.settrace(None)
