"""Smoke tests which verify tide is installed and runnable."""

import pytest


@pytest.mark.smoke
def test_dummy_smoke():
    assert True
