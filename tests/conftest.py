from __future__ import annotations

import os.path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from tide.core import Config

HERE = os.path.dirname(__file__)
VERBOSE = os.environ.get("VERBOSE", "false").lower() in ("true", "1")


@pytest.fixture
def config() -> Config:
    import tide.core
    from tide.cli import set_config

    return set_config(
        tide.core.load_config(
            os.path.join(HERE, "..", "pyproject.toml"), verbose=VERBOSE
        )
    )
