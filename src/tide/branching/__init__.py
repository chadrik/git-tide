"""The branching models tide can be configured to follow.

A repository selects exactly one with `branching_mode` in its root `pyproject.toml`.
Each model owns the rules for how versions advance and how branches move; `core` holds
only what both models share.
"""

from .gitflow import GitflowModel
from .semantic import SemanticCommitModel

__all__ = ["GitflowModel", "SemanticCommitModel"]
