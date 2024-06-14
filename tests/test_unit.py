import argparse
import os
import pytest
import shutil
import subprocess
import sys
import tomllib
import uuid
from pathlib import Path
from _pytest.monkeypatch import MonkeyPatch
from unittest.mock import Mock, mock_open, MagicMock, patch
from typing import Generator, Optional


project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.append(project_root)
from noxfile import (  # noqa: E402
    BETA,
    RC,
    STABLE,
    checkout,
    create_gitlab_ci_variable,
    current_branch,
    get_branches,
    get_latest_commit,
    get_parser,
    get_remote,
    get_tag_for_branch,
    get_upstream_branch,
    git,
    join,
    load_gitflow_config,
    update_gitlab_ci_variable,
)


@pytest.fixture
def setup_git_repo(tmpdir, monkeypatch) -> Generator[str, None, None]:
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
    os.chdir(tmpdir_str)

    # Initialize the git repository
    git("init", "-b", STABLE)

    parent_directory = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    files_to_copy = [
        "noxfile.py",
        ".gitignore",
        "pyproject.toml",
        "requirements.txt",
        "nox-tasks-requirements.txt",
    ]
    for file in files_to_copy:
        shutil.copy(os.path.join(parent_directory, file), tmpdir_str)

    # Add all files and commit them
    message = f"{STABLE}: initial state"
    git("add", ".")
    git("commit", "-m", message, stdout=subprocess.PIPE).stdout.strip()
    initial_commit = git("rev-parse", "HEAD", stdout=subprocess.PIPE).stdout.strip()

    monkeypatch.delenv("CI_REPOSITORY_URL", raising=False)
    monkeypatch.setenv("CI_COMMIT_BEFORE_SHA", initial_commit)
    monkeypatch.setenv("BETA_CYCLE_START_COMMIT", initial_commit)
    monkeypatch.setenv("ACCESS_TOKEN", "some-value")

    git("tag", "-a", "1.0.0", "-m", message)

    # Yield the directory path to allow tests to run in this environment
    yield tmpdir_str

    # After the test, forcefully delete the .git folder
    git_dir = os.path.join(tmpdir_str, ".git")
    if os.path.exists(git_dir):
        shutil.rmtree(git_dir, ignore_errors=True)


def create_file_and_commit(message: str, filename: Optional[str] = None) -> None:
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

    Path(filename).touch()
    git("add", ".")
    git("commit", "-m", message)


def branch_exists(branch_name) -> bool:
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
    tags = (
        git("tag", "--sort=-creatordate", stdout=subprocess.PIPE)
        .stdout.strip()
        .splitlines()
    )

    tag_order = ["\n\ntag_order:"]
    for tag in tags:
        annotation = git("tag", "-n", tag, stdout=subprocess.PIPE).stdout.strip()
        if not annotation:
            print(f"No annotation message found for tag {tag}")
            continue

        # Append to the tag order list
        tag_order.append(annotation)

    return "\n".join(tag_order)


def verify_git_graph(expected_graph: str) -> None:
    """
    Verify the current state of the git graph matches an expected graph.

    Args:
        expected_graph (str): The expected output of the git log graph command.

    This function fetches the current git graph, compares it to the expected graph, and asserts equality.
    Differences trigger an assertion error, aiding in debugging git histories in tests.
    """
    result = git(
        "log",
        "--graph",
        "--abbrev-commit",
        "--oneline",
        "--format=format:%C(white)%s%C(reset) %C(dim white)- %C(auto)%d%C(reset)",
        "--all",
        "--decorate",
        stdout=subprocess.PIPE,
    ).stdout.strip()

    tag_order = chronological_tag_history()
    result += tag_order
    assert (
        result == expected_graph.strip()
    ), f"Expected graph:\n{expected_graph}\nActual graph:\n{result}"


def nox_autotag(annotation: str) -> None:
    """
    Trigger the 'autotag' Nox session to automatically generate a new git tag.

    Args:
        annotation (str): A custom annotation message for the git tag. Defaults to "automatic change detected".

    This function runs a Nox session that automates the process of tagging the current commit in the git repository.
    """
    subprocess.run(
        [
            "nox",
            "-s",
            "autotag",
            "-v",
            "--force-color",
            "--",
            "--annotation",
            annotation,
        ]
    )


def nox_promote(monkeypatch: MonkeyPatch) -> None:
    """
    Promote changes in a git repository to simulate a promotion process handled typically by CI/CD.

    Args:
        monkeypatch (MonkeyPatch): The pytest monkeypatch fixture to mock environment variables.

    This function sets the latest commit from the BETA branch as the start of a new cycle,
    checks out the specified branch, and triggers the 'promote' Nox session.
    """
    # we use monkey patch to simulate setting the interaction that happens
    # with the remote repository for automating the increment of our minor
    # versions on the first commit after a promotion
    latest_commit = get_latest_commit(BETA, "")
    configure_environment("BETA_CYCLE_START_COMMIT", latest_commit, monkeypatch)
    subprocess.run(["nox", "-s", "promote", "-v", "--force-color"])


def nox_hotfix(monkeypatch: MonkeyPatch) -> None:
    """
    Trigger the 'hotfix' Nox session to handle hotfix operations without affecting minor version increments.

    Args:
        monkeypatch (MonkeyPatch): The pytest monkeypatch fixture to mock environment variables.

    This function resets the CI_COMMIT_BEFORE_SHA environment variable to prevent accidental minor version increments.
    """
    # we don't want to accidentally have a hotfix increment the minor version,
    # so we need to ensure the CI_COMMIT_BEFORE_SHA is reset
    configure_environment(
        "CI_COMMIT_BEFORE_SHA", "0000000000000000000000000000000000000000", monkeypatch
    )
    subprocess.run(["nox", "-s", "hotfix", "-v", "--force-color"])


def commit_and_configure_environment(branch: str, annotation: str, monkeypatch) -> None:
    """
    Checkout a branch, create a commit, and configure the environment.

    Args:
        branch (str): The branch to check out.
        annotation (str): The commit annotation message.
        monkeypatch: The pytest monkeypatch fixture for setting environment variables.

    Returns:
        None
    """
    checkout(None, branch, create=not branch_exists(branch))
    latest_commit = git("rev-parse", "HEAD", stdout=subprocess.PIPE).stdout.strip()
    configure_environment("CI_COMMIT_BEFORE_SHA", latest_commit, monkeypatch)
    create_file_and_commit(annotation)


def latest_tag() -> str:
    """
    Retrieves the most recently created tag in the Git repository.

    This function runs the `git describe --tags --abbrev=0` command to get the most recent tag in the current branch.

    Returns:
        str: The most recent tag in the repository. Returns None if no tags are found or an error occurs.
    """
    return git(
        "describe", "--tags", "--abbrev=0", stdout=subprocess.PIPE
    ).stdout.strip()


@pytest.mark.unit
@patch("noxfile.os.getenv")
@patch("noxfile.nox.Session", autospec=True)  # Mock the entire Nox Session class
def test_get_tag_for_beta_with_minor_increment(
    mock_session: MagicMock, mock_getenv: MagicMock
):
    # Setup
    mock_session = (
        mock_session.return_value
    )  # Obtain a mock instance from the mocked class
    branch = BETA
    mock_getenv.side_effect = lambda var_name: {
        "BETA_CYCLE_START_COMMIT": "12345",
        "CI_COMMIT_BEFORE_SHA": "12345",
    }.get(var_name)
    expected_output = "tag to create: 1.2.0-beta-prerelease"
    mock_session.run.return_value = expected_output

    # Action
    tag = get_tag_for_branch(mock_session, branch)

    # Assert
    assert (
        tag == "1.2.0-beta-prerelease"
    ), f"Expected tag to be '1.2.0-beta-prerelease', got '{tag}'"
    mock_session.run.assert_called_once()


@pytest.mark.unit
@patch("noxfile.os.getenv")
@patch("noxfile.nox.Session", autospec=True)
@pytest.mark.parametrize(
    "branch,expected_tag",
    [
        (STABLE, "1.1.1"),
        (BETA, "1.2.0b2"),
        (RC, "1.1.0rc2"),
        # Add other branches and their expected tags as needed
    ],
)
def test_get_tag_for_branch(
    mock_session: MagicMock, mock_getenv: MagicMock, branch: str, expected_tag: str
):
    # Setup
    mock_session = mock_session.return_value
    mock_getenv.return_value = None  # Adjust based on what each branch might need
    mock_session.run.return_value = f"tag to create: {expected_tag}"

    # Action
    tag = get_tag_for_branch(mock_session, branch)

    # Assert
    assert tag == expected_tag, f"Expected tag to be '{expected_tag}', got '{tag}'"
    mock_session.run.assert_called_once()


@pytest.mark.unit
@patch("noxfile.os.getenv")
@patch("noxfile.nox.Session", autospec=True)  # Mock the entire Nox Session class
def test_error_on_no_tag_output(mock_session: MagicMock, mock_getenv: MagicMock):
    # Setup
    mock_session = mock_session.return_value
    branch = STABLE
    mock_getenv.return_value = None
    mock_session.run.return_value = "Unexpected output"

    # mock session.error raising an error when called
    def error_side_effect(message):
        raise RuntimeError(message)

    mock_session.error.side_effect = error_side_effect

    # Action & Assert
    with pytest.raises(RuntimeError) as exc_info:
        get_tag_for_branch(mock_session, branch)
    assert "Unexpected output" in str(
        exc_info.value
    ), "Should raise an error with the output that caused the issue"
    mock_session.run.assert_called_once()


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
def test_get_upstream_branch_with_valid_branch():
    session = MagicMock()
    branch = STABLE
    expected_upstream = RC

    upstream = get_upstream_branch(session, branch)

    assert upstream == expected_upstream
    session.error.assert_not_called()


@pytest.mark.unit
def test_get_upstream_branch_with_first_branch():
    session = MagicMock()
    branch = BETA
    expected_upstream = None

    upstream = get_upstream_branch(session, branch)

    assert upstream == expected_upstream
    session.error.assert_not_called()


@pytest.mark.unit
def test_get_upstream_branch_with_invalid_branch():
    session = MagicMock()
    invalid_branch = "abc123"
    session.error.side_effect = ValueError(f"Invalid branch: {invalid_branch}")

    with patch("noxfile.BRANCHES", []):
        with pytest.raises(ValueError) as excinfo:
            get_upstream_branch(session, invalid_branch)

        assert str(excinfo.value) == f"Invalid branch: {invalid_branch}"
        session.error.assert_called_once_with(f"Invalid branch: {invalid_branch}")


@pytest.mark.unit
def test_get_upstream_branch_with_empty_branches():
    session = MagicMock()
    session.error.side_effect = ValueError(f"Invalid branch: {BETA}")

    with patch("noxfile.BRANCHES", []):
        with pytest.raises(ValueError) as excinfo:
            get_upstream_branch(session, BETA)

        assert str(excinfo.value) == f"Invalid branch: {BETA}"
        session.error.assert_called_once_with(f"Invalid branch: {BETA}")


@pytest.mark.unit
def test_checkout_existing_branch():
    remote = "origin"
    branch = "main"

    with patch("noxfile.git") as mock_git:
        checkout(remote, branch)

    mock_git.assert_called_once_with("checkout", f"{remote}/{branch}", "--track")


@pytest.mark.unit
def test_checkout_new_branch():
    remote = "origin"
    branch = "feature/123"
    create = True

    with patch("noxfile.git") as mock_git:
        checkout(remote, branch, create)

    mock_git.assert_called_once_with("checkout", "-b", f"{remote}/{branch}", "--track")


@pytest.mark.unit
def test_checkout_local_branch():
    remote = None
    branch = "bugfix/456"

    with patch("noxfile.git") as mock_git:
        checkout(remote, branch)

    mock_git.assert_called_once_with("checkout", branch)


@pytest.mark.unit
def test_checkout_local_new_branch():
    remote = None
    branch = "feature/789"
    create = True

    with patch("noxfile.git") as mock_git:
        checkout(remote, branch, create)

    mock_git.assert_called_once_with("checkout", "-b", branch)


@pytest.mark.unit
def test_checkout_git_command_failure():
    remote = "origin"
    branch = "main"

    with patch("noxfile.git") as mock_git:
        mock_git.side_effect = subprocess.CalledProcessError(1, "git")
        with pytest.raises(subprocess.CalledProcessError):
            checkout(remote, branch)


@pytest.mark.unit
def test_get_branches():
    expected_branches = ["main", "feature/123", "bugfix/456"]
    mocked_stdout = "\n".join([f"  {branch}" for branch in expected_branches])

    with patch("noxfile.git") as mock_git:
        mock_git.return_value.stdout = mocked_stdout
        branches = get_branches()

    assert branches == expected_branches
    mock_git.assert_called_once_with("branch", stdout=subprocess.PIPE)


@pytest.mark.unit
def test_get_branches_empty():
    mocked_stdout = ""

    with patch("noxfile.git") as mock_git:
        mock_git.return_value.stdout = mocked_stdout
        branches = get_branches()

    assert branches == []
    mock_git.assert_called_once_with("branch", stdout=subprocess.PIPE)


@pytest.mark.unit
def test_get_branches_git_command_failure():
    with patch("noxfile.git") as mock_git:
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
        with patch("noxfile.git") as mock_git:
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

    with patch("noxfile.git") as mock_git:
        mock_git.return_value.stdout = expected_commit_hash + "\n"
        commit_hash = get_latest_commit(branch_name, remote)

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

    with patch("noxfile.git") as mock_git:
        mock_git.return_value.stdout = expected_commit_hash + "\n"
        commit_hash = get_latest_commit(branch_name, remote)

    assert commit_hash == expected_commit_hash
    mock_git.assert_called_once_with("rev-parse", branch_name, stdout=subprocess.PIPE)


@pytest.mark.unit
def test_get_latest_commit_git_command_failure():
    branch_name = "main"
    remote = "origin"

    with patch("noxfile.git") as mock_git:
        mock_git.side_effect = subprocess.CalledProcessError(1, "git")
        with pytest.raises(subprocess.CalledProcessError):
            get_latest_commit(branch_name, remote)


@pytest.mark.unit
def test_create_gitlab_ci_variable_success():
    # Test the function under successful conditions
    with patch("requests.post") as mock_post:
        mock_post.return_value = Mock(status_code=200)
        mock_post.return_value.raise_for_status = Mock()

        try:
            create_gitlab_ci_variable(
                "https://gitlab.example.com/api", "access-token", "NEW_VAR", "value123"
            )
            mock_post.assert_called_once_with(
                "https://gitlab.example.com/api",
                headers={"PRIVATE-TOKEN": "access-token"},
                json={"key": "NEW_VAR", "value": "value123"},
            )
        except Exception as e:
            pytest.fail(f"Unexpected exception raised: {e}")


@pytest.mark.unit
def test_create_gitlab_ci_variable_http_error():
    # Test the function's response to HTTP errors
    with patch("requests.post") as mock_post:
        mock_post.return_value = Mock(status_code=400)
        mock_post.return_value.raise_for_status.side_effect = Exception("HTTP Error")

        with pytest.raises(Exception, match="HTTP Error"):
            create_gitlab_ci_variable(
                "https://gitlab.example.com/api", "access-token", "NEW_VAR", "value123"
            )


@pytest.mark.unit
def test_update_gitlab_ci_variable_success():
    with patch("requests.put") as mock_put, patch.dict(
        "os.environ",
        {
            "ACCESS_TOKEN": "fake_token",
            "CI_API_V4_URL": "https://gitlab.example.com/api",
            "CI_PROJECT_ID": "123",
        },
    ):
        mock_put.return_value = Mock(status_code=200)
        update_gitlab_ci_variable("TEST_KEY", "new_value")
        mock_put.assert_called_once_with(
            "https://gitlab.example.com/api/projects/123/variables/TEST_KEY",
            headers={"PRIVATE-TOKEN": "fake_token"},
            json={"value": "new_value"},
        )


@pytest.mark.unit
def test_update_gitlab_ci_variable_not_found():
    with patch("requests.put") as mock_put, patch(
        "noxfile.create_gitlab_ci_variable"
    ) as mock_create, patch.dict(
        "os.environ",
        {
            "ACCESS_TOKEN": "fake_token",
            "CI_API_V4_URL": "https://gitlab.example.com/api",
            "CI_PROJECT_ID": "123",
        },
    ):
        mock_put.return_value = Mock(status_code=404)
        update_gitlab_ci_variable("TEST_KEY", "new_value")
        mock_create.assert_called_once_with(
            "https://gitlab.example.com/api/projects/123/variables",
            "fake_token",
            "TEST_KEY",
            "new_value",
        )


@pytest.mark.unit
def test_update_gitlab_ci_variable_http_error():
    with patch.dict("os.environ", {"ACCESS_TOKEN": "fake_token"}):
        with patch("requests.put") as mock_put:
            mock_put.return_value = Mock(
                status_code=500,
                raise_for_status=Mock(side_effect=Exception("HTTP Error")),
            )
            with pytest.raises(Exception) as e:
                update_gitlab_ci_variable("TEST_KEY", "new_value")
            assert "HTTP Error" in str(e.value)


@pytest.mark.unit
def test_get_remote_in_ci_environment():
    url = "https://gitlab-ci-token:[MASKED]@gitlab.example.com/someproject/"

    with patch.dict("os.environ", {"CI_REPOSITORY_URL": url, "ACCESS_TOKEN": "abc123"}):
        with patch("noxfile.git") as mock_git:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_git.return_value = mock_result

            from noxfile import get_remote

            assert get_remote() == "gitlab_origin"


@pytest.mark.unit
def test_get_remote_in_local_environment():
    with patch.dict("os.environ", {}, clear=True):  #
        assert get_remote() is None


@pytest.mark.unit
def test_get_current_branch_in_ci_environment():
    with patch.dict("os.environ", {"CI_COMMIT_BRANCH": "beta"}):
        assert current_branch() == "beta"


@pytest.mark.unit
def test_get_current_branch_in_local_environment():
    with patch.dict("os.environ", {}, clear=True):
        with patch("noxfile.git") as mock_git:
            mock_result = MagicMock()
            mock_result.stdout.strip.return_value = "main"
            mock_git.return_value = mock_result

            from noxfile import current_branch

            branch = current_branch()
            assert branch == "main"


@pytest.mark.unit
def test_get_parser():
    parser = get_parser()

    # Check if the returned object is an instance of argparse.ArgumentParser
    assert isinstance(parser, argparse.ArgumentParser)

    # Check if the parser has the expected arguments with default values and help messages
    args = parser.parse_args(
        []
    )  # Simulate calling the parser without any command-line arguments
    assert hasattr(args, "annotation")
    assert args.annotation == "automatic change detected"
    assert hasattr(args, "build_dir")
    assert args.build_dir is "public"  # Default should be None unless specified
    assert hasattr(args, "serve")
    assert not args.serve  # Default should be False unless specified

    # Collect help messages for each option to ensure they are set correctly
    help_messages = {action.dest: action.help for action in parser._actions}
    expected_help_messages = {
        "annotation": "Message to pass for tag annotations.",
        "build_dir": "Directory where the documentation should be built.",
        "serve": "Serve the documentation after building.",
    }

    # Validate help messages
    for key, expected in expected_help_messages.items():
        assert help_messages[key] == expected


@pytest.mark.unit
def test_get_parser_with_argument():
    parser = get_parser()
    # Testing with multiple command line arguments
    args = parser.parse_args(
        [
            "--annotation",
            "custom annotation message",
            "--build-dir",
            "custom/dir",
            "--serve",
        ]
    )

    # Check if the custom arguments are parsed correctly
    assert args.annotation == "custom annotation message"
    assert args.build_dir == "custom/dir"
    assert args.serve is True


@pytest.mark.unit
def test_load_gitflow_config():
    # Mocked content of the pyproject.toml file
    mocked_toml_content = b"""
    [tool.gitflow]
    first_tag = "1.0.0"
    [tool.gitflow.branches]
    master = "main"
    develop = "beta"
    [tool.gitflow.prereleases]
    beta = "beta"
    rc = "rc"
    """

    # Expected result
    expected_config = {
        "first_tag": "1.0.0",
        "branches": {"master": "main", "develop": "beta"},
        "prereleases": {"beta": "beta", "rc": "rc"},
    }

    # Mocking the open function and the tomllib.load function
    with patch("builtins.open", mock_open(read_data=mocked_toml_content)), patch(
        "tomllib.load", return_value=tomllib.loads(mocked_toml_content.decode("utf-8"))
    ):
        config = load_gitflow_config()
        assert config == expected_config


expected_graph = rf"""
*   auto-hotfix into {BETA}: {RC}: add hotfix 2 -  (HEAD -> {BETA}, tag: 1.2.0rc0, tag: 1.2.0b2, {RC})
|\  
| * {RC}: add hotfix 2 -  (tag: 1.1.0rc2, tag: 1.1.0, {STABLE})
* | auto-hotfix into {BETA}: {STABLE}: add hotfix -  (tag: 1.2.0b1)
|\| 
| *   auto-hotfix into {RC}: {STABLE}: add hotfix -  (tag: 1.1.0rc1)
| |\  
| | * {STABLE}: add hotfix -  (tag: 1.0.1)
* | | {BETA}: add beta feature 2 -  (tag: 1.2.0b0)
|/ /  
* / {BETA}: add feature 1 -  (tag: 1.1.0rc0, tag: 1.1.0b0)
|/  
* {STABLE}: initial state -  (tag: 1.0.0)

tag_order:
1.2.0rc0        promoting {BETA} to {RC}!
1.1.0           promoting {RC} to {STABLE}!
1.2.0b2         auto-hotfix into {BETA}: {RC}: add hotfix 2
1.1.0rc2        {RC}: add hotfix 2
1.2.0b1         auto-hotfix into {BETA}: {STABLE}: add hotfix
1.1.0rc1        auto-hotfix into {RC}: {STABLE}: add hotfix
1.0.1           {STABLE}: add hotfix
1.2.0b0         {BETA}: add beta feature 2
1.1.0rc0        promoting {BETA} to {RC}!
1.1.0b0         {BETA}: add feature 1
1.0.0           {STABLE}: initial state"""


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
    tmpdir = setup_git_repo
    assert os.path.isdir(tmpdir)
    assert latest_tag() == "1.0.0"

    # The trick here is to try and mimic what the
    # order of operations would be in a typical
    # development cycle where we have a mix of
    # things done by developers and others
    # done by our CICD.

    # Add feature 1 to BETA branch
    annotation = f"{BETA}: add feature 1"
    commit_and_configure_environment(BETA, annotation, monkeypatch)
    nox_autotag(annotation)
    assert latest_tag() == "1.1.0b0"

    # Promote BETA to RC
    nox_promote(monkeypatch)
    assert latest_tag() == "1.1.0rc0"

    # Add beta feature 2 to BETA branch
    annnotation = f"{BETA}: add beta feature 2"
    commit_and_configure_environment(BETA, annnotation, monkeypatch)
    nox_autotag(annnotation)
    assert latest_tag() == "1.2.0b0"

    # Add hotfix to STABLE branch
    annotation = f"{STABLE}: add hotfix"
    commit_and_configure_environment(STABLE, annotation, monkeypatch)
    nox_autotag(annotation)
    assert latest_tag() == "1.0.1"

    # Apply hotfix to STABLE
    nox_hotfix(monkeypatch)
    assert latest_tag() == "1.1.0rc1"

    # Apply hotfix to RC
    nox_hotfix(monkeypatch)
    assert latest_tag() == "1.2.0b1"

    # Add hotfix 2 to RC branch
    annotation = f"{RC}: add hotfix 2"
    commit_and_configure_environment(RC, annotation, monkeypatch)
    nox_autotag(annotation)
    assert latest_tag() == "1.1.0rc2"

    # Apply hotfix 2 to BETA
    nox_hotfix(monkeypatch)
    assert latest_tag() == "1.2.0b2"

    # Promote RC to STABLE
    nox_promote(monkeypatch)
    assert latest_tag() == "1.2.0rc0"

    # Verify the git graph against expected structure
    verify_git_graph(expected_graph)
