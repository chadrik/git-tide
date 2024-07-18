from __future__ import annotations

import contextlib
import fnmatch
import os
import textwrap
import time

import pytest
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from _pytest.monkeypatch import MonkeyPatch
from unittest.mock import MagicMock, patch, call
from typing import Generator, Optional

import click

from monoflow import (
    checkout,
    current_branch,
    get_branches,
    get_latest_commit,
    Gitlab,
    get_upstream_branch,
    git,
    join,
    load_config,
    set_config,
    Config,
    PROMOTION_PENDING_VAR,
)

HERE = os.path.dirname(__file__)
PROJECTS = ["projectA", "projectB"]
# Git timestamps are limited to unix timestamp resolution: 1s
DELAY = 1.1
VERBOSE = False


def current_rev() -> str:
    return git("rev-parse", "HEAD", stdout=subprocess.PIPE).stdout.strip()


def all_tags(pattern: str | None = None) -> list[str]:
    """
    Return all of the tags in the Git repository.
    """
    tags = (
        git(
            "tag",
            "--sort=-creatordate",
            "--format=%(tag)  %(creatordate)",
            stdout=subprocess.PIPE,
        )
        .stdout.strip()
        .splitlines()
    )
    if VERBOSE:
        for tag in tags:
            print(tag)
    tags = [t.split("  ", 1)[0] for t in tags]
    if pattern:
        return fnmatch.filter(tags, pattern)
    else:
        return tags


def latest_tag(pattern: str | None = None) -> str:
    """
    Retrieves the most recently created tag in the Git repository.

    This function runs the `git describe --tags --abbrev=0` command to get the most recent tag in the current branch.

    Returns:
        str: The most recent tag in the repository. Returns None if no tags are found or an error occurs.
    """
    if pattern:
        return all_tags(pattern)[0]
    else:
        return git(
            "describe", "--tags", "--abbrev=0", stdout=subprocess.PIPE
        ).stdout.strip()


@pytest.fixture
def config() -> Config:
    return set_config(load_config(os.path.join(HERE, "..", "pyproject.toml")))


@pytest.fixture
def setup_git_repo(tmpdir, monkeypatch) -> Generator[tuple[str, Config], None, None]:
    """
    Pytest fixture that sets up a temporary Git repository with an initial commit of project files.

    Args:
        tmpdir: Temporary directory provided by pytest.
        monkeypatch: Pytest's monkeypatch fixture to update environment variables.

    Yields:
        str: The path of the temporary directory with initialized Git repository.

    This setup includes copying essential project files, creating an initial commit, and simulating
    an autotag process. It cleans up by removing the .git directory after tests are done.
    """
    tmpdir_str = str(tmpdir)
    print(f"Git repo: {tmpdir_str}")
    os.chdir(tmpdir_str)

    parent_directory = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    files_to_copy = [
        "noxfile.py",
        ".gitignore",
        "pyproject.toml",
    ]
    for file in files_to_copy:
        shutil.copy(os.path.join(parent_directory, file), tmpdir_str)

    for folder in PROJECTS:
        shutil.copytree(
            os.path.join(parent_directory, folder), os.path.join(tmpdir_str, folder)
        )

    config = set_config(load_config(os.path.join(tmpdir_str, "pyproject.toml")))

    # Initialize the git repository
    git("init", "-b", config.stable)

    # Add all files and commit them
    message = "initial state"
    git("add", ".")
    git("commit", "-m", message, stdout=subprocess.PIPE).stdout.strip()

    for branch in config.branches:
        checkout(None, branch, create=not branch_exists(branch))

    monkeypatch.delenv("CI_REPOSITORY_URL", raising=False)
    monkeypatch.setenv("ACCESS_TOKEN", "some-value")

    for project in PROJECTS:
        git("tag", "-a", f"{project}-1.0.0", "-m", message)
        time.sleep(DELAY)
        configure_environment(
            PROMOTION_PENDING_VAR.format(config.beta, project), "true", monkeypatch
        )

    # Yield the directory path to allow tests to run in this environment
    yield tmpdir_str, config


def create_file_and_commit(
    message: str, folder: Optional[str] = None, filename: Optional[str] = None
) -> None:
    """
    Create a file and commit it to the repository.

    Args:
        message (str): The commit message.
        filename (Optional[str]): The name of the file to be created. If not provided, a UUID will be generated.

    Returns:
        None
    """
    if not filename:
        filename = str(uuid.uuid4())
    if folder:
        path = os.path.join(folder, filename)
    else:
        path = filename

    Path(path).touch()
    git("add", ".")
    git("commit", "-m", message)


def branch_exists(branch_name: str) -> bool:
    """
    Check if a local git branch exists.

    Args:
        branch_name: The name of the branch to check.

    Returns:
        True if the branch exists locally, False otherwise.
    """
    try:
        # Using the existing 'git' function to list local branches
        result = git("branch", "--list", branch_name, stdout=subprocess.PIPE)
        # Check if the branch name is in the output of the command
        return branch_name.strip() in result.stdout.strip().split()
    except subprocess.CalledProcessError:
        # If the git command fails, assume the branch does not exist
        return False


def configure_environment(key: str, value: str, monkeypatch) -> None:
    """
    Configure an environment variable.

    Args:
        key (str): The environment variable key.
        value (str): The environment variable value.
        monkeypatch: The pytest monkeypatch fixture for setting environment variables.

    Returns:
        None
    """
    monkeypatch.setenv(key, value)
    monkeypatch.delenv("CI_REPOSITORY_URL", raising=False)


def chronological_tag_history() -> str:
    """
    Retrieve a chronological list of tags from the Git repository, formatted with their annotations.

    Returns:
        str: A formatted string containing a chronological list of tags with their annotations.
    """
    # Get all tags, sorted by creation date
    tags = all_tags()

    tag_order = ["tag_order:"]
    for tag in tags:
        annotation = git("tag", "-n", tag, stdout=subprocess.PIPE).stdout.strip()
        if not annotation:
            print(f"No annotation message found for tag {tag}")
            continue

        # control the formatting
        tag, msg = annotation.split(" ", 1)
        # Append to the tag order list
        tag_order.append(f"{tag:<24} {msg.strip()}")

    return "\n".join(tag_order).strip()


def format_branches(s: str, config: Config) -> str:
    return s.strip().format(BETA=config.beta, STABLE=config.stable, RC=config.rc)


def git_graph() -> str:
    return git(
        "log",
        "--graph",
        "--abbrev-commit",
        "--oneline",
        "--format=format:%C(white)%s%C(reset) %C(dim white)- %C(auto)%d%C(reset)",
        "--all",
        "--decorate",
        stdout=subprocess.PIPE,
    ).stdout.strip()


def verify_git_graph(config: Config, expected_graph: str) -> None:
    """
    Verify the current state of the git graph matches an expected graph.

    Args:
        expected_graph (str): The expected output of the git log graph command.

    This function fetches the current git graph, compares it to the expected graph, and asserts equality.
    Differences trigger an assertion error, aiding in debugging git histories in tests.
    """
    expected = textwrap.dedent(expected_graph)
    result = git_graph()
    if VERBOSE:
        print(format_branches(expected, config))
        print(result)
    assert result == format_branches(
        expected, config
    ), f"Expected graph:\n{expected}\nActual graph:\n{result}"


def run_autotag(annotation: str) -> None:
    """
    Trigger the 'autotag' command to automatically generate a new git tag.

    Args:
        annotation (str): A custom annotation message for the git tag. Defaults to "automatic change detected".

    This function runs a command that automates the process of tagging the current commit in the git repository.
    """
    time.sleep(DELAY)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "monoflow",
            "autotag",
            f"--annotation={annotation}",
        ],
        check=True,
    )


def run_promote(beta: str, monkeypatch: MonkeyPatch) -> None:
    """
    Promote changes in a git repository to simulate a promotion process handled typically by CI/CD.

    Args:
        monkeypatch (MonkeyPatch): The pytest monkeypatch fixture to mock environment variables.

    This function sets the latest commit from the BETA branch as the start of a new cycle,
    checks out the specified branch, and triggers the 'promote' command.
    """
    time.sleep(DELAY)
    subprocess.run([sys.executable, "-m", "monoflow", "promote"])
    for project in PROJECTS:
        configure_environment(
            PROMOTION_PENDING_VAR.format(beta, project), "true", monkeypatch
        )


def run_hotfix(monkeypatch: MonkeyPatch) -> None:
    """
    Trigger the 'hotfix' command to handle hotfix operations without affecting minor version increments.

    Args:
        monkeypatch (MonkeyPatch): The pytest monkeypatch fixture to mock environment variables.
    """
    time.sleep(DELAY)
    subprocess.run([sys.executable, "-m", "monoflow", "hotfix"])


@contextlib.contextmanager
def pipeline(
    branch: str,
    title: str,
    monkeypatch,
    is_merge_request=True,
) -> Generator[None, None]:
    """
    Simulate the begining of a new CI pipeline.

    Checkout a branch and configure the environment.

    Args:
        branch (str): The branch to check out.
        monkeypatch: The pytest monkeypatch fixture for setting environment variables.

    Returns:
        None
    """
    print()
    print(f"Starting pipeline: {title}")
    checkout(None, branch)

    print(git_graph())
    print()

    latest_commit = current_rev()
    if is_merge_request:
        configure_environment("CI_PIPELINE_SOURCE", "merge_request_event", monkeypatch)
        configure_environment(
            "CI_MERGE_REQUEST_DIFF_BASE_SHA", latest_commit, monkeypatch
        )
    else:
        configure_environment("CI_PIPELINE_SOURCE", "push", monkeypatch)
        configure_environment("CI_COMMIT_SHA", latest_commit, monkeypatch)
    yield


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
#     tag = get_tag_for_branch(mock_session, branch)
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
#     tag = get_tag_for_branch(mock_session, branch)
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
#         get_tag_for_branch(mock_session, branch)
#     assert "Unexpected output" in str(
#         exc_info.value
#     ), "Should raise an error with the output that caused the issue"
#     mock_session.run.assert_called_once()


@pytest.mark.unit
def test_join_with_remote():
    # Given a remote name and a branch
    remote = "origin"
    branch = "main"

    # When join is called
    result = join(remote, branch)

    # Then the result should include the remote name followed by the branch name
    expected_result = "origin/main"
    assert result == expected_result, f"Expected {expected_result}, got {result}"


@pytest.mark.unit
def test_join_without_remote():
    # Given no remote name and a branch
    remote = None
    branch = "main"

    # When join is called
    result = join(remote, branch)

    # Then the result should be the branch name only
    expected_result = "main"
    assert result == expected_result, f"Expected {expected_result}, got {result}"


@pytest.mark.unit
def test_get_upstream_branch_with_valid_branch(config):
    branch = config.stable
    expected_upstream = config.rc

    upstream = get_upstream_branch(branch)

    assert upstream == expected_upstream


@pytest.mark.unit
def test_get_upstream_branch_with_first_branch(config):
    branch = config.beta
    expected_upstream = None

    upstream = get_upstream_branch(branch)

    assert upstream == expected_upstream


@pytest.mark.unit
def test_get_upstream_branch_with_invalid_branch(config, monkeypatch):
    invalid_branch = "abc123"

    monkeypatch.setattr(config, "branches", [])
    with pytest.raises(click.ClickException) as excinfo:
        get_upstream_branch(invalid_branch)

    assert str(excinfo.value) == f"Invalid branch: {invalid_branch}"


@pytest.mark.unit
def test_get_upstream_branch_with_empty_branches(config, monkeypatch):
    monkeypatch.setattr(config, "branches", [])
    with pytest.raises(click.ClickException) as excinfo:
        get_upstream_branch(config.beta)

    assert str(excinfo.value) == f"Invalid branch: {config.beta}"


@pytest.mark.unit
def test_checkout_existing_branch():
    remote = "origin"
    branch = "main"

    with patch("monoflow.git") as mock_git:
        checkout(remote, branch)

    mock_git.assert_has_calls(
        [
            call("checkout", f"{remote}/{branch}", "--track"),
            call("rev-parse", branch, stdout=subprocess.PIPE),
        ]
    )


@pytest.mark.unit
def test_checkout_new_branch():
    remote = "origin"
    branch = "feature/123"
    create = True

    with patch("monoflow.git") as mock_git:
        checkout(remote, branch, create)

    mock_git.assert_has_calls(
        [
            call("checkout", "-b", f"{remote}/{branch}", "--track"),
            call("rev-parse", branch, stdout=subprocess.PIPE),
        ]
    )


@pytest.mark.unit
def test_checkout_local_branch():
    remote = None
    branch = "bugfix/456"

    with patch("monoflow.git") as mock_git:
        checkout(remote, branch)

    mock_git.assert_has_calls(
        [call("checkout", branch), call("rev-parse", branch, stdout=subprocess.PIPE)]
    )


@pytest.mark.unit
def test_checkout_local_new_branch():
    remote = None
    branch = "feature/789"
    create = True

    with patch("monoflow.git") as mock_git:
        checkout(remote, branch, create)

    mock_git.assert_has_calls(
        [
            call("checkout", "-b", branch),
            call("rev-parse", branch, stdout=subprocess.PIPE),
        ]
    )


@pytest.mark.unit
def test_checkout_git_command_failure():
    remote = "origin"
    branch = "main"

    with patch("monoflow.git") as mock_git:
        mock_git.side_effect = subprocess.CalledProcessError(1, "git")
        with pytest.raises(subprocess.CalledProcessError):
            checkout(remote, branch)


@pytest.mark.unit
def test_get_branches():
    expected_branches = ["main", "feature/123", "bugfix/456"]
    mocked_stdout = "\n".join([f"  {branch}" for branch in expected_branches])

    with patch("monoflow.git") as mock_git:
        mock_git.return_value.stdout = mocked_stdout
        branches = get_branches()

    assert branches == expected_branches
    mock_git.assert_called_once_with("branch", stdout=subprocess.PIPE)


@pytest.mark.unit
def test_get_branches_empty():
    mocked_stdout = ""

    with patch("monoflow.git") as mock_git:
        mock_git.return_value.stdout = mocked_stdout
        branches = get_branches()

    assert branches == []
    mock_git.assert_called_once_with("branch", stdout=subprocess.PIPE)


@pytest.mark.unit
def test_get_branches_git_command_failure():
    with patch("monoflow.git") as mock_git:
        mock_git.side_effect = subprocess.CalledProcessError(1, "git")
        with pytest.raises(subprocess.CalledProcessError):
            get_branches()


@pytest.mark.unit
def test_current_branch_in_ci_environment():
    with patch.dict("os.environ", {"CI_COMMIT_BRANCH": "beta"}):
        assert current_branch() == "beta"


@pytest.mark.unit
def test_current_branch_in_local_environment():
    with patch.dict("os.environ", {}, clear=True):
        with patch("monoflow.git") as mock_git:
            mock_result = MagicMock(spec=subprocess.CompletedProcess)
            mock_result.stdout = "beta"
            mock_git.return_value = mock_result
            branch = current_branch()
            mock_git.assert_called_once_with(
                "branch", "--show-current", stdout=subprocess.PIPE
            )
            assert branch == "beta"


@pytest.mark.unit
def test_get_latest_commit_with_remote():
    branch_name = "main"
    remote = "origin"
    expected_commit_hash = "abcdef123456"

    with patch("monoflow.git") as mock_git:
        mock_git.return_value.stdout = expected_commit_hash + "\n"
        commit_hash = get_latest_commit(remote, branch_name)

    assert commit_hash == expected_commit_hash
    mock_git.assert_any_call("fetch", "origin", branch_name)
    mock_git.assert_called_with(
        "rev-parse", f"{remote}/{branch_name}", stdout=subprocess.PIPE
    )


@pytest.mark.unit
def test_get_latest_commit_without_remote():
    branch_name = "main"
    remote = None
    expected_commit_hash = "abcdef123456"

    with patch("monoflow.git") as mock_git:
        mock_git.return_value.stdout = expected_commit_hash + "\n"
        commit_hash = get_latest_commit(remote, branch_name)

    assert commit_hash == expected_commit_hash
    mock_git.assert_called_once_with("rev-parse", branch_name, stdout=subprocess.PIPE)


@pytest.mark.unit
def test_get_latest_commit_git_command_failure():
    branch_name = "main"
    remote = "origin"

    with patch("monoflow.git") as mock_git:
        mock_git.side_effect = subprocess.CalledProcessError(1, "git")
        with pytest.raises(subprocess.CalledProcessError):
            get_latest_commit(remote, branch_name)


# @pytest.mark.unit
# def test_create_gitlab_ci_variable_success():
#     # Test the function under successful conditions
#     with patch("requests.post") as mock_post:
#         mock_post.return_value = Mock(status_code=200)
#         mock_post.return_value.raise_for_status = Mock()
#
#         try:
#             create_gitlab_ci_variable(
#                 "https://gitlab.example.com/api", "access-token", "NEW_VAR", "value123"
#             )
#             mock_post.assert_called_once_with(
#                 "https://gitlab.example.com/api",
#                 headers={"PRIVATE-TOKEN": "access-token"},
#                 json={"key": "NEW_VAR", "value": "value123"},
#             )
#         except Exception as e:
#             pytest.fail(f"Unexpected exception raised: {e}")


# @pytest.mark.unit
# def test_create_gitlab_ci_variable_http_error():
#     # Test the function's response to HTTP errors
#     with patch("requests.post") as mock_post:
#         mock_post.return_value = Mock(status_code=400)
#         mock_post.return_value.raise_for_status.side_effect = Exception("HTTP Error")
#
#         with pytest.raises(Exception, match="HTTP Error"):
#             create_gitlab_ci_variable(
#                 "https://gitlab.example.com/api", "access-token", "NEW_VAR", "value123"
#             )


# @pytest.mark.unit
# def test_update_gitlab_ci_variable_success():
#     with patch("requests.put") as mock_put, patch.dict(
#         "os.environ",
#         {
#             "ACCESS_TOKEN": "fake_token",
#             "CI_API_V4_URL": "https://gitlab.example.com/api",
#             "CI_PROJECT_ID": "123",
#         },
#     ):
#         mock_put.return_value = Mock(status_code=200)
#         update_gitlab_ci_variable("TEST_KEY", "new_value")
#         mock_put.assert_called_once_with(
#             "https://gitlab.example.com/api/projects/123/variables/TEST_KEY",
#             headers={"PRIVATE-TOKEN": "fake_token"},
#             json={"value": "new_value"},
#         )


# @pytest.mark.unit
# def test_update_gitlab_ci_variable_not_found():
#     with patch("requests.put") as mock_put, patch(
#         "monoflow.create_gitlab_ci_variable"
#     ) as mock_create, patch.dict(
#         "os.environ",
#         {
#             "ACCESS_TOKEN": "fake_token",
#             "CI_API_V4_URL": "https://gitlab.example.com/api",
#             "CI_PROJECT_ID": "123",
#         },
#     ):
#         mock_put.return_value = Mock(status_code=404)
#         update_gitlab_ci_variable("TEST_KEY", "new_value")
#         mock_create.assert_called_once_with(
#             "https://gitlab.example.com/api/projects/123/variables",
#             "fake_token",
#             "TEST_KEY",
#             "new_value",
#         )


# @pytest.mark.unit
# def test_update_gitlab_ci_variable_http_error():
#     with patch.dict("os.environ", {"ACCESS_TOKEN": "fake_token"}):
#         with patch("requests.put") as mock_put:
#             mock_put.return_value = Mock(
#                 status_code=500,
#                 raise_for_status=Mock(side_effect=Exception("HTTP Error")),
#             )
#             with pytest.raises(Exception) as e:
#                 update_gitlab_ci_variable("TEST_KEY", "new_value")
#             assert "HTTP Error" in str(e.value)


@pytest.mark.unit
def test_get_remote_in_ci_environment():
    url = "https://gitlab-ci-token:[MASKED]@gitlab.example.com/someproject/"

    with patch.dict("os.environ", {"CI_REPOSITORY_URL": url, "ACCESS_TOKEN": "abc123"}):
        with patch("monoflow.git") as mock_git:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_git.return_value = mock_result

            assert Gitlab.get_remote() == "gitlab_origin"


@pytest.mark.unit
def test_get_remote_in_local_environment():
    with patch.dict("os.environ", {}, clear=True):  #
        assert Gitlab.get_remote() is None


@pytest.mark.unit
def test_get_current_branch_in_ci_environment():
    with patch.dict("os.environ", {"CI_COMMIT_BRANCH": "beta"}):
        assert current_branch() == "beta"


@pytest.mark.unit
def test_get_current_branch_in_local_environment():
    with patch.dict("os.environ", {}, clear=True):
        with patch("monoflow.git") as mock_git:
            mock_result = MagicMock()
            mock_result.stdout.strip.return_value = "main"
            mock_git.return_value = mock_result

            from monoflow import current_branch

            branch = current_branch()
            assert branch == "main"


# @pytest.mark.unit
# def test_load_gitflow_config():
#     # Mocked content of the pyproject.toml file
#     mocked_toml_content = b"""
#     [tool.gitflow]
#     first_tag = "1.0.0"
#     [tool.gitflow.branches]
#     master = "main"
#     develop = "beta"
#     [tool.gitflow.prereleases]
#     beta = "beta"
#     rc = "rc"
#     """
#
#     # Expected result
#     expected_config = {
#         "first_tag": "1.0.0",
#         "branches": {"master": "main", "develop": "beta"},
#         "prereleases": {"beta": "beta", "rc": "rc"},
#     }
#
#     # FIXME: this test would be more meaningful if it actually used tomllib.
#     #  This is barely testing anything.
#     # Mocking the open function and the tomllib.load function
#     with patch("builtins.open", mock_open(read_data=mocked_toml_content)), patch(
#         "tomllib.load", return_value=tomllib.loads(mocked_toml_content.decode("utf-8"))
#     ):
#         config = load_config()
#         assert config == expected_config


expected_tags = r"""
tag_order:
projectB-1.1.0           promoting {RC} to {STABLE}!
projectA-1.2.0           promoting {RC} to {STABLE}!
projectB-1.1.0rc0        promoting {BETA} to {RC}!
projectA-1.2.0rc0        promoting {BETA} to {RC}!
projectA-1.1.0           promoting {RC} to {STABLE}!
projectA-1.2.0b2         auto-hotfix into {BETA}: {RC}: (projectA) add hotfix 2
projectA-1.1.0rc2        {RC}: (projectA) add hotfix 2
projectB-1.1.0b0         {BETA}: (projectB) add feature 1
projectA-1.2.0b1         auto-hotfix into {BETA}: {STABLE}: (projectA) add hotfix
projectA-1.1.0rc1        auto-hotfix into {RC}: {STABLE}: (projectA) add hotfix
projectA-1.0.1           {STABLE}: (projectA) add hotfix
projectA-1.2.0b0         {BETA}: (projectA) add beta feature 2
projectA-1.1.0rc0        promoting {BETA} to {RC}!
projectA-1.1.0b0         {BETA}: (projectA) add feature 1
projectB-1.0.0           initial state
projectA-1.0.0           initial state"""


@pytest.mark.unit
def test_dev_cycle(setup_git_repo, monkeypatch) -> None:
    """
    Test the development cycle by mimicking typical operations in a CICD environment.

    Args:
        setup_git_repo: The pytest fixture for setting up a temporary Git repository.
        monkeypatch: The pytest monkeypatch fixture for setting environment variables.

    Returns:
        None
    """
    tmpdir, config = setup_git_repo
    assert os.path.isdir(tmpdir)
    assert latest_tag("projectA-*") == "projectA-1.0.0"

    # The trick here is to try and mimic what the
    # order of operations would be in a typical
    # development cycle where we have a mix of
    # things done by developers and others
    # done by our CICD.

    print()
    print(git_graph())
    print()

    # (ProjectA) Add feature 1 to BETA branch
    annotation = f"{config.beta}: (projectA) add feature 1"
    with pipeline(config.beta, "(ProjectA) Add feature 1 to BETA branch", monkeypatch):
        create_file_and_commit(annotation, folder="projectA")
        run_autotag(annotation)
        assert latest_tag("projectA-*") == "projectA-1.1.0b0"
        configure_environment(
            PROMOTION_PENDING_VAR.format(config.beta, "projectA"), "false", monkeypatch
        )

    # Promote (promote always runs on rc, according to our .gitlab-ci.yml)
    with pipeline(config.rc, "Promote", monkeypatch, is_merge_request=False):
        run_promote(config.beta, monkeypatch)
        assert latest_tag("projectA-*") == "projectA-1.1.0rc0"

    expected = r"""
    * {BETA}: (projectA) add feature 1 -  (HEAD -> {BETA}, tag: projectA-1.1.0rc0, tag: projectA-1.1.0b0, {RC})
    * initial state -  (tag: projectB-1.0.0, tag: projectA-1.0.0, {STABLE})"""
    verify_git_graph(config, expected)

    # (ProjectA) Add beta feature 2 to BETA branch
    annotation = f"{config.beta}: (projectA) add beta feature 2"
    with pipeline(
        config.beta, "(ProjectA) Add beta feature 2 to BETA branch", monkeypatch
    ):
        create_file_and_commit(annotation, folder="projectA")
        run_autotag(annotation)
        assert latest_tag("projectA-*") == "projectA-1.2.0b0"
        configure_environment(
            PROMOTION_PENDING_VAR.format(config.beta, "projectA"), "false", monkeypatch
        )

    expected = r"""
    * {BETA}: (projectA) add beta feature 2 -  (HEAD -> {BETA}, tag: projectA-1.2.0b0)
    * {BETA}: (projectA) add feature 1 -  (tag: projectA-1.1.0rc0, tag: projectA-1.1.0b0, {RC})
    * initial state -  (tag: projectB-1.0.0, tag: projectA-1.0.0, {STABLE})"""
    verify_git_graph(config, expected)

    # (ProjectA) Add hotfix to STABLE branch
    annotation = f"{config.stable}: (projectA) add hotfix"
    with pipeline(config.stable, "(ProjectA) Add hotfix to STABLE branch", monkeypatch):
        create_file_and_commit(annotation, folder="projectA")
        run_autotag(annotation)
        assert latest_tag("projectA-*") == "projectA-1.0.1"

    # FIXME: this runs as the result of a tag push. what branch does it run on?
    # (ProjectA) Cascade hotfix to RC
    with pipeline(config.stable, "(ProjectA) Cascade hotfix to RC", monkeypatch):
        run_hotfix(monkeypatch)
        assert latest_tag("projectA-*") == "projectA-1.1.0rc1"

    # (ProjectA) Cascade hotfix to BETA
    with pipeline(config.rc, "(ProjectA) Cascade hotfix to BETA", monkeypatch):
        run_hotfix(monkeypatch)
        assert latest_tag("projectA-*") == "projectA-1.2.0b1"

    # (ProjectB) Add feature 1 to BETA branch
    annotation = f"{config.beta}: (projectB) add feature 1"
    with pipeline(config.beta, "(ProjectB) Add feature 1 to BETA branch", monkeypatch):
        create_file_and_commit(annotation, folder="projectB")
        run_autotag(annotation)
        assert latest_tag("projectB-*") == "projectB-1.1.0b0"
        configure_environment(
            PROMOTION_PENDING_VAR.format(config.beta, "projectB"), "false", monkeypatch
        )

    # (ProjectA) Add hotfix 2 to RC branch
    annotation = f"{config.rc}: (projectA) add hotfix 2"
    with pipeline(config.rc, "(ProjectA) Add hotfix 2 to RC branch", monkeypatch):
        create_file_and_commit(annotation, folder="projectA")
        run_autotag(annotation)
        assert latest_tag("projectA-*") == "projectA-1.1.0rc2"

    # (ProjectA) Cascade hotfix 2 to BETA
    with pipeline(config.rc, "(ProjectA) Cascade hotfix 2 to BETA", monkeypatch):
        run_hotfix(monkeypatch)
        assert latest_tag("projectA-*") == "projectA-1.2.0b2"

        # there are no changes for projectB, so the tag should remain the same
        assert latest_tag("projectB-*") == "projectB-1.1.0b0"

    expected = r"""
    *   auto-hotfix into {BETA}: {RC}: (projectA) add hotfix 2 -  (HEAD -> {BETA}, tag: projectA-1.2.0b2)
    |\  
    | * {RC}: (projectA) add hotfix 2 -  (tag: projectA-1.1.0rc2, {RC})
    * | {BETA}: (projectB) add feature 1 -  (tag: projectB-1.1.0b0)
    * | auto-hotfix into {BETA}: master: (projectA) add hotfix -  (tag: projectA-1.2.0b1)
    |\| 
    | *   auto-hotfix into {RC}: master: (projectA) add hotfix -  (tag: projectA-1.1.0rc1)
    | |\  
    | | * {STABLE}: (projectA) add hotfix -  (tag: projectA-1.0.1, {STABLE})
    * | | {BETA}: (projectA) add beta feature 2 -  (tag: projectA-1.2.0b0)
    |/ /  
    * / {BETA}: (projectA) add feature 1 -  (tag: projectA-1.1.0rc0, tag: projectA-1.1.0b0)
    |/  
    * initial state -  (tag: projectB-1.0.0, tag: projectA-1.0.0)
    """
    verify_git_graph(config, expected)

    # Promote (promote always runs on rc, according to our .gitlab-ci.yml)
    with pipeline(config.rc, "Promote", monkeypatch, is_merge_request=False):
        run_promote(config.beta, monkeypatch)
        assert latest_tag("projectA-*") == "projectA-1.2.0rc0"
        assert latest_tag("projectB-*") == "projectB-1.1.0rc0"

    expected = r"""
    *   auto-hotfix into {BETA}: {RC}: (projectA) add hotfix 2 -  (HEAD -> {BETA}, tag: projectB-1.1.0rc0, tag: projectA-1.2.0rc0, tag: projectA-1.2.0b2, {RC})
    |\  
    | * {RC}: (projectA) add hotfix 2 -  (tag: projectA-1.1.0rc2, tag: projectA-1.1.0, {STABLE})
    * | {BETA}: (projectB) add feature 1 -  (tag: projectB-1.1.0b0)
    * | auto-hotfix into {BETA}: {STABLE}: (projectA) add hotfix -  (tag: projectA-1.2.0b1)
    |\| 
    | *   auto-hotfix into {RC}: {STABLE}: (projectA) add hotfix -  (tag: projectA-1.1.0rc1)
    | |\  
    | | * {STABLE}: (projectA) add hotfix -  (tag: projectA-1.0.1)
    * | | {BETA}: (projectA) add beta feature 2 -  (tag: projectA-1.2.0b0)
    |/ /  
    * / {BETA}: (projectA) add feature 1 -  (tag: projectA-1.1.0rc0, tag: projectA-1.1.0b0)
    |/  
    * initial state -  (tag: projectB-1.0.0, tag: projectA-1.0.0)"""
    verify_git_graph(config, expected)

    # Promote (promote always runs on rc, according to our .gitlab-ci.yml)
    with pipeline(config.rc, "Promote", monkeypatch, is_merge_request=False):
        run_promote(config.beta, monkeypatch)
        assert latest_tag("projectA-*") == "projectA-1.2.0"
        assert latest_tag("projectB-*") == "projectB-1.1.0"

    expected = r"""
    *   auto-hotfix into {BETA}: {RC}: (projectA) add hotfix 2 -  (HEAD -> {BETA}, tag: projectB-1.1.0rc0, tag: projectB-1.1.0, tag: projectA-1.2.0rc0, tag: projectA-1.2.0b2, tag: projectA-1.2.0, {RC}, {STABLE})
    |\  
    | * {RC}: (projectA) add hotfix 2 -  (tag: projectA-1.1.0rc2, tag: projectA-1.1.0)
    * | {BETA}: (projectB) add feature 1 -  (tag: projectB-1.1.0b0)
    * | auto-hotfix into {BETA}: {STABLE}: (projectA) add hotfix -  (tag: projectA-1.2.0b1)
    |\| 
    | *   auto-hotfix into {RC}: {STABLE}: (projectA) add hotfix -  (tag: projectA-1.1.0rc1)
    | |\  
    | | * {STABLE}: (projectA) add hotfix -  (tag: projectA-1.0.1)
    * | | {BETA}: (projectA) add beta feature 2 -  (tag: projectA-1.2.0b0)
    |/ /  
    * / {BETA}: (projectA) add feature 1 -  (tag: projectA-1.1.0rc0, tag: projectA-1.1.0b0)
    |/  
    * initial state -  (tag: projectB-1.0.0, tag: projectA-1.0.0)"""
    verify_git_graph(config, expected)

    if VERBOSE:
        print(chronological_tag_history())
    assert chronological_tag_history() == format_branches(expected_tags, config)
