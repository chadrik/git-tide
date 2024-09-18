from __future__ import annotations

import os

import pytest

import subprocess
from pathlib import Path
from unittest.mock import patch, call

import click

from tide.gitutils import (
    checkout_remote_branch,
    get_branches,
    get_latest_commit,
    join,
)

from tide.core import (
    get_modified_projects,
    load_config,
    Config,
)
from tide.cli import set_config


HERE = os.path.dirname(__file__)
VERBOSE = os.environ.get("VERBOSE", "false").lower() in ("true", "1")


@pytest.fixture
def config() -> Config:
    return set_config(
        load_config(os.path.join(HERE, "..", "pyproject.toml"), verbose=VERBOSE)
    )


# @pytest.mark.unit
# @patch("noxfile.os.getenv")
# @patch("noxfile.nox.Session", autospec=True)  # Mock the entire Nox Session class
# def test_get_tag_for_beta_with_minor_increment(
#     mock_session: MagicMock, mock_getenv: MagicMock
# ):
#     # Setup
#     mock_session = (
#         mock_session.return_value
#     )  # Obtain a mock instance from the mocked class
#     branch = BETA
#     mock_getenv.side_effect = lambda var_name: {
#         "BETA_CYCLE_START_COMMIT": "12345",
#         "CI_COMMIT_BEFORE_SHA": "12345",
#     }.get(var_name)
#     expected_output = "tag to create: 1.2.0-beta-prerelease"
#     mock_session.run.return_value = expected_output
#
#     # Action
#     tag = get_next_tag(mock_session, branch)
#
#     # Assert
#     assert (
#         tag == "1.2.0-beta-prerelease"
#     ), f"Expected tag to be '1.2.0-beta-prerelease', got '{tag}'"
#     mock_session.run.assert_called_once()


#
# @pytest.mark.unit
# @patch("noxfile.os.getenv")
# @patch("noxfile.nox.Session", autospec=True)
# @pytest.mark.parametrize(
#     "branch,expected_tag",
#     [
#         (STABLE, "1.1.1"),
#         (BETA, "1.2.0b2"),
#         (RC, "1.1.0rc2"),
#         # Add other branches and their expected tags as needed
#     ],
# )
# def test_get_tag_for_branch(
#     mock_session: MagicMock, mock_getenv: MagicMock, branch: str, expected_tag: str
# ):
#     # Setup
#     mock_session = mock_session.return_value
#     mock_getenv.return_value = None  # Adjust based on what each branch might need
#     mock_session.run.return_value = f"tag to create: {expected_tag}"
#
#     # Action
#     tag = get_next_tag(mock_session, branch)
#
#     # Assert
#     assert tag == expected_tag, f"Expected tag to be '{expected_tag}', got '{tag}'"
#     mock_session.run.assert_called_once()


# @pytest.mark.unit
# @patch("noxfile.os.getenv")
# @patch("noxfile.nox.Session", autospec=True)  # Mock the entire Nox Session class
# def test_error_on_no_tag_output(mock_session: MagicMock, mock_getenv: MagicMock):
#     # Setup
#     mock_session = mock_session.return_value
#     branch = STABLE
#     mock_getenv.return_value = None
#     mock_session.run.return_value = "Unexpected output"
#
#     # mock session.error raising an error when called
#     def error_side_effect(message):
#         raise RuntimeError(message)
#
#     mock_session.error.side_effect = error_side_effect
#
#     # Action & Assert
#     with pytest.raises(RuntimeError) as exc_info:
#         get_next_tag(mock_session, branch)
#     assert "Unexpected output" in str(
#         exc_info.value
#     ), "Should raise an error with the output that caused the issue"
#     mock_session.run.assert_called_once()


@pytest.mark.unit
def test_get_modified_projects(config) -> None:
    # Note: we patch core.git and not gitutils.git, bc it's already imported
    with patch("tide.core.git") as mock_git, patch(
        "tide.core.get_projects"
    ) as mock_get_projects:
        mock_git.return_value = "\n".join(
            [
                "projectA/path.py",
            ]
        )
        mock_get_projects.return_value = [
            (Path("projectA"), "projectA"),
            (Path("projectB"), "projectB"),
        ]
        assert get_modified_projects(base_rev="fake") == [
            (Path("projectA"), "projectA")
        ]


@pytest.mark.unit
def test_join_with_remote() -> None:
    # Given a remote name and a branch
    remote = "origin"
    branch = "main"

    # When join is called
    result = join(remote, branch)

    # Then the result should include the remote name followed by the branch name
    expected_result = "origin/main"
    assert result == expected_result, f"Expected {expected_result}, got {result}"


@pytest.mark.unit
def test_join_without_remote() -> None:
    # Given no remote name and a branch
    remote = None
    branch = "main"

    # When join is called
    result = join(remote, branch)

    # Then the result should be the branch name only
    expected_result = "main"
    assert result == expected_result, f"Expected {expected_result}, got {result}"


@pytest.mark.unit
def test_get_upstream_branch_with_valid_branch(config) -> None:
    branch = config.stable
    expected_upstream = config.rc

    upstream = config.get_upstream_branch(branch)

    assert upstream == expected_upstream


@pytest.mark.unit
def test_get_upstream_branch_with_first_branch(config) -> None:
    branch = config.beta
    expected_upstream = None

    upstream = config.get_upstream_branch(branch)

    assert upstream == expected_upstream


@pytest.mark.unit
def test_get_upstream_branch_with_invalid_branch(config, monkeypatch) -> None:
    invalid_branch = "abc123"

    monkeypatch.setattr(config, "branches", [])
    with pytest.raises(click.ClickException) as excinfo:
        config.get_upstream_branch(invalid_branch)

    assert str(excinfo.value) == f"Invalid branch: {invalid_branch}"


@pytest.mark.unit
def test_get_upstream_branch_with_empty_branches(config, monkeypatch) -> None:
    monkeypatch.setattr(config, "branches", [])
    with pytest.raises(click.ClickException) as excinfo:
        config.get_upstream_branch(config.beta)

    assert str(excinfo.value) == f"Invalid branch: {config.beta}"


@pytest.mark.unit
def test_checkout_new_branch() -> None:
    remote = "origin"
    branch = "feature/123"

    with patch("tide.gitutils.git") as mock_git:
        checkout_remote_branch(remote, branch)

    mock_git.assert_has_calls(
        [
            call("rev-parse", "--verify", branch, quiet=True),
            call("branch", "--delete", branch),
            call("checkout", "--track", f"{remote}/{branch}"),
            call("rev-parse", branch, capture=True),
        ]
    )


@pytest.mark.unit
def test_checkout_git_command_failure() -> None:
    remote = "origin"
    branch = "main"

    with patch("tide.gitutils.git") as mock_git:
        mock_git.side_effect = subprocess.CalledProcessError(1, "git")
        with pytest.raises(subprocess.CalledProcessError):
            checkout_remote_branch(remote, branch)


@pytest.mark.unit
def test_get_branches() -> None:
    expected_branches = ["main", "feature/123", "bugfix/456"]
    mocked_stdout = "\n".join([f"  {branch}" for branch in expected_branches])

    with patch("tide.gitutils.git") as mock_git:
        mock_git.return_value = mocked_stdout
        branches = get_branches()

    assert branches == expected_branches
    mock_git.assert_called_once_with("branch", capture=True)


@pytest.mark.unit
def test_get_branches_empty() -> None:
    mocked_stdout = ""

    with patch("tide.gitutils.git") as mock_git:
        mock_git.return_value = mocked_stdout
        branches = get_branches()

    assert branches == []
    mock_git.assert_called_once_with("branch", capture=True)


@pytest.mark.unit
def test_get_branches_git_command_failure() -> None:
    with patch("tide.gitutils.git") as mock_git:
        mock_git.side_effect = subprocess.CalledProcessError(1, "git")
        with pytest.raises(subprocess.CalledProcessError):
            get_branches()


@pytest.mark.unit
def test_get_latest_commit_with_remote() -> None:
    branch_name = "main"
    remote = "origin"
    expected_commit_hash = "abcdef123456"

    with patch("tide.gitutils.git") as mock_git:
        mock_git.return_value = expected_commit_hash
        commit_hash = get_latest_commit(remote, branch_name)

    assert commit_hash == expected_commit_hash
    mock_git.assert_any_call("fetch", "origin", branch_name)
    mock_git.assert_called_with("rev-parse", f"{remote}/{branch_name}", capture=True)


@pytest.mark.unit
def test_get_latest_commit_without_remote() -> None:
    branch_name = "main"
    remote = None
    expected_commit_hash = "abcdef123456"

    with patch("tide.gitutils.git") as mock_git:
        mock_git.return_value = expected_commit_hash
        commit_hash = get_latest_commit(remote, branch_name)

    assert commit_hash == expected_commit_hash
    mock_git.assert_called_once_with("rev-parse", branch_name, capture=True)


@pytest.mark.unit
def test_get_latest_commit_git_command_failure() -> None:
    branch_name = "main"
    remote = "origin"

    with patch("tide.gitutils.git") as mock_git:
        mock_git.side_effect = subprocess.CalledProcessError(1, "git")
        with pytest.raises(subprocess.CalledProcessError):
            get_latest_commit(remote, branch_name)
