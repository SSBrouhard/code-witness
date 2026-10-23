"""Decoy upstream conftest: fails every test if it is ever loaded for a characterization suite."""
import pytest


@pytest.fixture(autouse=True)
def decoy_conftest_loaded():
    raise RuntimeError("decoy upstream conftest.py was loaded")
