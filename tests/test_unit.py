"""Unit tests for individual tide functions."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import call, patch

import click
import pytest

from tide.branching.gitflow import (
    get_promotion_marker,
)
from tide.core import (
    get_modified_projects,
)
from tide.gitutils import (
    checkout_remote_branch,
    get_branches,
    get_latest_commit,
    join,
)

HERE = os.path.dirname(__file__)
VERBOSE = os.environ.get("VERBOSE", "false").lower() in ("true", "1")

os.environ["TIDE_PATCH_CZ_RUN"] = "true"


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
    with patch("tide.core.git") as mock_git, patch("tide.core.get_projects") as mock_get_projects:
        mock_git.return_value = "\n".join(
            [
                "projectA/path.py",
            ]
        )
        mock_get_projects.return_value = [
            (Path("projectA"), "projectA"),
            (Path("projectB"), "projectB"),
        ]
        assert get_modified_projects(base_rev="fake") == [(Path("projectA"), "projectA")]


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


@pytest.mark.unit
def test_get_promotion_marker_fetches_notes() -> None:
    with patch("tide.branching.gitflow.git") as mock_git:
        mock_git.return_value = ""
        assert get_promotion_marker("origin") is None

    mock_git.assert_any_call("fetch", "origin", "+refs/notes/*:refs/notes/*", quiet=True)


@pytest.mark.unit
def test_get_promotion_marker_without_fetch() -> None:
    with patch("tide.branching.gitflow.git") as mock_git:
        mock_git.return_value = ""
        assert get_promotion_marker("origin", fetch=False) is None

    assert not [args for args, _ in mock_git.call_args_list if args[0] == "fetch"]


# ---------------------------------------------------------------------------
# semantic_commit mode
# ---------------------------------------------------------------------------


@pytest.fixture
def semantic_config():
    """A semantic_commit configuration using the default tag/branch formats."""
    from tide.cli import set_config
    from tide.core import BranchingMode, Config

    return set_config(
        Config(
            stable="main",
            branches=["main"],
            branching_mode=BranchingMode.semantic_commit,
        )
    )


@pytest.mark.unit
def test_release_branch_name(semantic_config) -> None:
    from tide.branching.semantic import ReleaseBranch

    assert ReleaseBranch("projectA", 1, 2).name(semantic_config) == "projectA/release-1.2"
    assert ReleaseBranch("projectA", 10, 0).name(semantic_config) == "projectA/release-10.0"


@pytest.mark.unit
def test_release_branch_parse(semantic_config) -> None:
    from tide.branching.semantic import ReleaseBranch

    assert ReleaseBranch.parse(semantic_config, "projectA/release-1.2") == ReleaseBranch(
        project="projectA", major=1, minor=2
    )
    # a project name may itself contain a slash
    assert ReleaseBranch.parse(semantic_config, "group/projectA/release-0.1") == ReleaseBranch(
        project="group/projectA", major=0, minor=1
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "branch",
    [
        "main",
        "projectA/release-1",  # no minor
        "projectA/release-1.2.3",  # a version, not a line
        "projectA/1.2.0",  # a tag, not a branch
        "release-1.2",  # no project
    ],
)
def test_release_branch_parse_rejects(semantic_config, branch) -> None:
    from tide.branching.semantic import ReleaseBranch

    assert ReleaseBranch.parse(semantic_config, branch) is None


@pytest.mark.unit
def test_release_branch_round_trips(semantic_config) -> None:
    from tide.branching.semantic import ReleaseBranch

    owning = ReleaseBranch.instance(semantic_config, "projectA", (3, 4))
    assert owning.name(semantic_config) == "projectA/release-3.4"
    # a branch built for a line compares equal to the same branch parsed back out
    assert ReleaseBranch.parse(semantic_config, owning.name(semantic_config)) == owning
    assert (owning.project, owning.version_line) == ("projectA", (3, 4))


def _commit(message: str, rev: str = "abc1234") -> object:
    from tide.core import Commit

    return Commit(rev=rev, parents=["def5678"], message=message, files=[])


@pytest.mark.unit
@pytest.mark.parametrize(
    "messages,expected",
    [
        (["fix: a crash"], "PATCH"),
        (["feat: a thing"], "MINOR"),
        (["feat!: a breaking thing"], "MAJOR"),
        (["fix: a crash\n\nBREAKING CHANGE: gone"], "MAJOR"),
        # the strongest signal in the set wins
        (["fix: a crash", "feat: a thing"], "MINOR"),
        (["feat: a thing", "fix: a crash"], "MINOR"),
        # nothing conventional implies no release at all
        (["chore: tidy up"], None),
        (["update the readme"], None),
        ([], None),
    ],
)
def test_find_increment(messages, expected) -> None:
    from tide.branching.semantic import find_increment

    assert find_increment([_commit(m) for m in messages]) == expected


@pytest.mark.unit
def test_validation_error_carries_exit_code() -> None:
    from tide.branching.semantic import ValidationCode, ValidationError

    err = ValidationError(ValidationCode.trunk_line_diverged, "nope")
    assert err.exit_code == int(ValidationCode.trunk_line_diverged)
    # every rule must be distinguishable by exit code, and none may look like success
    codes = [int(c) for c in ValidationCode if c is not ValidationCode.ok]
    assert len(codes) == len(set(codes))
    assert 0 not in codes


@pytest.mark.unit
def test_semantic_config_rejects_prerelease_branches(tmp_path) -> None:
    from tide.core import load_config

    pyproject = tmp_path.joinpath("pyproject.toml")
    pyproject.write_text(
        '[tool.tide]\nbranching_mode = "semantic_commit"\n'
        'branches.stable = "main"\nbranches.beta = "develop"\n'
    )
    with pytest.raises(click.ClickException) as excinfo:
        load_config(str(pyproject))
    assert "branches.beta" in str(excinfo.value)


@pytest.mark.unit
def test_semantic_config_requires_parseable_branch_format(tmp_path) -> None:
    from tide.core import load_config

    pyproject = tmp_path.joinpath("pyproject.toml")
    pyproject.write_text(
        '[tool.tide]\nbranching_mode = "semantic_commit"\nbranches.stable = "main"\n'
        'release_branch_format = "$project/release"\n'
    )
    with pytest.raises(click.ClickException) as excinfo:
        load_config(str(pyproject))
    assert "$major" in str(excinfo.value)


@pytest.mark.unit
def test_defaults_are_project_scoped(tmp_path) -> None:
    """Tide is monorepo-first: projects never share a namespace by default."""
    from tide.core import BranchingMode, load_config

    pyproject = tmp_path.joinpath("pyproject.toml")
    pyproject.write_text('[tool.tide]\nbranches.stable = "main"\n')
    config = load_config(str(pyproject))
    assert config.branching_mode is BranchingMode.gitflow
    assert config.tag_format == "$project/$version"
    assert config.release_branch_format == "$project/release-$major.$minor"


@pytest.mark.unit
def test_default_formats_isolate_projects(tmp_path) -> None:
    """The defaults are monorepo-first: two projects never share a name.

    The scenario fixtures pin an explicit tag_format so that both branching modes stay
    comparable, so this is the coverage that the shipped defaults behave as intended.
    """
    from tide.branching.semantic import ReleaseBranch
    from tide.core import _init_commitizen_context, load_config

    pyproject = tmp_path.joinpath("pyproject.toml")
    pyproject.write_text('[tool.tide]\nbranches.stable = "main"\n')
    config = load_config(str(pyproject))

    # a tag carries its project, so one project's version is not another's
    matcher = _init_commitizen_context(config, "projectA").provider._tag_format_matcher()
    assert str(matcher("projectA/1.1.0")) == "1.1.0"
    assert matcher("projectB/1.1.0") is None
    assert matcher("1.1.0") is None

    # and so does a release branch
    assert ReleaseBranch.instance(config, "projectA", (1, 1)).name(config) == "projectA/release-1.1"
    assert ReleaseBranch.instance(config, "projectB", (1, 1)).name(config) == "projectB/release-1.1"
    # which round-trips back to the project that owns it
    parsed = ReleaseBranch.parse(config, "projectA/release-1.1")
    assert parsed is not None and parsed.project == "projectA"


@pytest.mark.unit
def test_invalid_branching_mode(tmp_path) -> None:
    from tide.core import load_config

    pyproject = tmp_path.joinpath("pyproject.toml")
    pyproject.write_text('[tool.tide]\nbranching_mode = "trunk"\nbranches.stable = "main"\n')
    with pytest.raises(click.ClickException) as excinfo:
        load_config(str(pyproject))
    assert "branching_mode" in str(excinfo.value)


@pytest.fixture
def line_repo(tmp_path, monkeypatch):
    """A repo where a release branch has had the trunk merged into it.

    Layout: main carries projectA/1.1.0 then projectA/1.2.0. The release branch
    projectA/release-1.1 is cut at 1.1.0 and then merges main, which makes 1.2.0
    reachable from it.
    """
    from tide.cli import set_config
    from tide.core import BranchingMode, Config
    from tide.gitutils import git

    monkeypatch.chdir(tmp_path)
    git("init", "-b", "main", quiet=True)
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")

    Path("projectA").mkdir()
    Path("projectA/pyproject.toml").write_text('[tool.tide]\nproject = "projectA"\n')
    Path("projectA/a.txt").write_text("1")
    git("add", ".")
    git("commit", "-m", "feat: one", quiet=True)
    git("tag", "-a", "projectA/1.1.0", "-m", "x")

    git("branch", "projectA/release-1.1")

    Path("projectA/a.txt").write_text("2")
    git("add", ".")
    git("commit", "-m", "feat: two", quiet=True)
    git("tag", "-a", "projectA/1.2.0", "-m", "x")

    git("checkout", "projectA/release-1.1", quiet=True)
    git("merge", "main", "--no-ff", "-m", "merge main", quiet=True)

    return set_config(
        Config(
            stable="main",
            branches=["main"],
            branching_mode=BranchingMode.semantic_commit,
        )
    )


@pytest.mark.unit
def test_latest_version_unconstrained_leapfrogs_the_line(line_repo) -> None:
    """Max-reachable resolution is exactly the behavior ADR 0002 rejects."""
    from tide.branching.semantic import latest_version

    assert str(latest_version(line_repo, "projectA")) == "1.2.0"


@pytest.mark.unit
def test_latest_version_constrained_to_line(line_repo) -> None:
    """Constraining to the branch's own line survives the trunk being merged in."""
    from tide.branching.semantic import latest_version

    assert str(latest_version(line_repo, "projectA", version_line=(1, 1))) == "1.1.0"
    assert latest_version(line_repo, "projectA", version_line=(9, 9)) is None


@pytest.mark.unit
def test_next_version_on_release_branch_stays_on_its_line(line_repo) -> None:
    """The release branch name, not git topology, decides what may be minted."""
    from tide.branching.semantic import SemanticCommitModel
    from tide.gitutils import git

    Path("projectA/a.txt").write_text("3")
    git("add", ".")
    git("commit", "-m", "fix: three", quiet=True)

    model = SemanticCommitModel(line_repo)
    assert model.next_version("projectA/release-1.1", "projectA", as_tag=True) == "projectA/1.1.1"


@pytest.mark.unit
def test_is_ancestor_detects_divergence(line_repo) -> None:
    from tide.branching.semantic import is_ancestor

    # release-1.1 absorbed main, so main is an ancestor of it but not the reverse
    assert is_ancestor("main", "projectA/release-1.1")
    assert not is_ancestor("projectA/release-1.1", "main")


@pytest.mark.unit
@pytest.mark.parametrize("command", ["hotfix", "promote"])
def test_semantic_mode_does_not_support_gitflow_commands(semantic_config, command) -> None:
    """These are absent from the model rather than guarded at each call site."""
    from tide.branching.semantic import SemanticCommitModel

    model = SemanticCommitModel(semantic_config)
    with pytest.raises(click.ClickException) as excinfo:
        getattr(model, command)(None, None)
    assert command in str(excinfo.value)
    assert "semantic_commit" in str(excinfo.value)


@pytest.mark.unit
@pytest.mark.parametrize("command", ["hotfix", "promote"])
def test_gitflow_mode_supports_gitflow_commands(config, command) -> None:
    from tide.branching.gitflow import GitflowModel

    assert hasattr(GitflowModel(config), command)


@pytest.mark.unit
def test_semantic_mode_rejects_unknown_branches(semantic_config) -> None:
    from tide.branching.semantic import SemanticCommitModel
    from tide.core import ReleaseID

    model = SemanticCommitModel(semantic_config)
    assert model.release_id("main") is ReleaseID.stable
    assert model.release_id("projectA/release-1.2") is ReleaseID.stable
    with pytest.raises(click.ClickException):
        model.release_id("some-feature-branch")
