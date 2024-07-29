from __future__ import annotations

import contextlib
import dataclasses
import fnmatch
import json
import os
import pprint
import textwrap
import tempfile
import time
import re

import gitlab
import gitlab.const
import gitlab.v4.objects
import pytest
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from urllib.parse import urlparse, urlunparse
from pathlib import Path
from collections import defaultdict
from _pytest.monkeypatch import MonkeyPatch
from unittest.mock import MagicMock, patch, call
from typing import Generator

import click

from monoflow import (
    checkout,
    current_branch,
    get_branches,
    get_latest_commit,
    current_rev,
    Gitlab,
    get_upstream_branch,
    git,
    join,
    load_config,
    set_config,
    Config,
    PROMOTION_PENDING_VAR,
    LOCAL_REMOTE,
)

REMOTE_MODE = os.environ.get("REMOTE_MODE", "false").lower() in ("true", "1")

if REMOTE_MODE:
    try:
        ACCESS_TOKEN = os.environ["ACCESS_TOKEN"]
    except KeyError:
        print("Must set ACCESS_TOKEN env var before running nox")
        raise
else:
    ACCESS_TOKEN = None

# set this to reuse an existing Gitlab remote during testing
FORCE_GITLAB_REMOTE = os.environ.get("FORCE_GITLAB_REMOTE")
KEEP_OLD_GITLAB_PROJECTS = os.environ.get("KEEP_OLD_GITLAB_PROJECTS", 3)
GITLAB_API_URL = "gitlab.com"

HERE = os.path.dirname(__file__)
PROJECTS = ["projectA", "projectB"]
# Git timestamps are limited to unix timestamp resolution: 1s
DELAY = 1.1
VERBOSE = False


def all_tags(pattern: str | None = None) -> list[str]:
    """
    Return all of the tags in the Git repository.

    Args:
        pattern: glob pattern for tag names
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

    Args:
        pattern: glob pattern for tag names

    Returns:
        The most recent tag in the repository. Returns None if no tags are found or an error occurs.
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
def gitlab_project():
    if REMOTE_MODE:
        # private token or personal token authentication (self-hosted GitLab instance)
        if FORCE_GITLAB_REMOTE:
            # reuse
            url = urlparse(FORCE_GITLAB_REMOTE)
            gl = gitlab.Gitlab(
                url=urlunparse(url._replace(path="")),
                private_token=ACCESS_TOKEN,
                retry_transient_errors=True,
            )
            # remove leading "/"
            project = gl.projects.get(url.path[1:])
        else:
            gl = gitlab.Gitlab(
                url=f"https://{GITLAB_API_URL}",
                private_token=ACCESS_TOKEN,
                retry_transient_errors=True,
            )
            project = gl.projects.create(
                {
                    "name": f"semver-demo-{uuid.uuid4()}",
                    "visibility_level": gitlab.const.Visibility.PRIVATE,
                }
            )
    else:
        project = None

    yield project

    if REMOTE_MODE and not FORCE_GITLAB_REMOTE:
        # cleanup old projects
        projects = gl.projects.list(
            owned=True, visibility=gitlab.const.Visibility.PRIVATE
        )
        # these are sorted from newest to oldest
        projects = [
            project for project in projects if project.name.startswith("semver-demo-")
        ]
        if len(projects) > KEEP_OLD_GITLAB_PROJECTS:
            for project in projects[KEEP_OLD_GITLAB_PROJECTS:]:
                print(f"Cleaning up Gitlab project {project.name}")
                project.delete()


@dataclasses.dataclass
class GitlabData:
    project: gitlab.v4.objects.Project
    schedule: gitlab.v4.objects.ProjectPipelineSchedule

    @property
    def remote(self) -> str:
        return self.project.http_url_to_repo


@dataclasses.dataclass
class LocalData:
    tempdir: tempfile.TemporaryDirectory

    @property
    def remote(self) -> str:
        return self.tempdir.name


@pytest.fixture
def setup_git_repo(
    tmpdir, gitlab_project, monkeypatch
) -> Generator[tuple[str, Config], None, None]:
    """
    Pytest fixture that sets up a temporary Git repository with an initial commit of project files.

    Args:
        tmpdir: Temporary directory provided by pytest.
        monkeypatch: Pytest's monkeypatch fixture to update environment variables.

    Yields:
        The path of the temporary directory with initialized Git repository.

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
        ".gitlab-ci.yml",
        "requirements.txt",
        "src/monoflow.py",
    ]

    for file in files_to_copy:
        dest = Path(tmpdir_str).joinpath(file)
        os.makedirs(dest.parent, exist_ok=True)
        shutil.copy(Path(parent_directory).joinpath(file), dest)

    for folder in PROJECTS:
        shutil.copytree(
            os.path.join(parent_directory, folder), os.path.join(tmpdir_str, folder)
        )

    config = set_config(
        load_config(os.path.join(tmpdir_str, "pyproject.toml"), verbose=True)
    )

    # Initialize the git repository
    git("init", "-b", config.stable)

    # Add all files and commit them
    message = "initial state"
    git("add", ".")
    git("commit", "-m", message, stdout=subprocess.PIPE).stdout.strip()
    rev = current_rev()
    git("checkout", "--detach", rev)

    monkeypatch.delenv("CI_REPOSITORY_URL", raising=False)

    if gitlab_project:
        if FORCE_GITLAB_REMOTE:
            # cleanup
            for schedule in gitlab_project.pipelineschedules.list(get_all=True):
                schedule.delete()
            # FIXME: this doesn't always seem to take effect
            for var in gitlab_project.variables.list(get_all=True):
                var.delete()
            assert gitlab_project.variables.list(get_all=True) == []

            # allow force push
            p_branch = gitlab_project.protectedbranches.get(config.beta)
            p_branch.allow_force_push = True
            p_branch.save()

        gitlab_project.variables.create({"key": "ACCESS_TOKEN", "value": ACCESS_TOKEN})
        remote_data = GitlabData(gitlab_project, None)
        push_opts = ["-o", "ci.skip"]
    else:
        monkeypatch.setenv("ACCESS_TOKEN", "some-value")
        local_remote = tempfile.TemporaryDirectory()
        os.chdir(local_remote.name)
        git("init", "--bare")
        os.chdir(tmpdir_str)
        remote_data = LocalData(local_remote)
        push_opts = []

    git("remote", "add", "origin", remote_data.remote)

    if FORCE_GITLAB_REMOTE:
        # cleanup remote tags
        git("fetch", "--tags")
        tags = all_tags()
        if tags:
            git("tag", "-d", *tags)
            git("push", "origin", "--delete", *tags)
        git("prune")

    git("fetch", "--all")
    git("branch", "-la")
    git("remote", "-v")

    # push branches
    for branch in config.branches:
        git("branch", "-f", branch, rev)
        git("checkout", branch)
        git("push", "-f", "--set-upstream", "origin", branch, *push_opts)

    # make initial tags
    for project in PROJECTS:
        git("tag", "-a", f"{project}-1.0.0", "-m", message)
        time.sleep(DELAY)

    print_git_graph()

    git("push", "-f", "--all", *push_opts)
    git("push", "-f", "--tags", *push_opts)

    if isinstance(remote_data, GitlabData):
        # this must happen after the branch has been created in the remote and initial commit pushed
        schedule = gitlab_project.pipelineschedules.create(
            {
                "ref": config.rc,
                "description": "Promote",
                "cron": "6 6 * * 4",
                "active": False,
            }
        )
        schedule.variables.create({"key": "SCHEDULED_JOB_NAME", "value": "promote"})
        remote_data.schedule = schedule

    # Yield the directory path to allow tests to run in this environment
    yield tmpdir_str, config, remote_data

    if isinstance(remote_data, LocalData):
        remote_data.tempdir.cleanup()


def create_file_and_commit(
    branch, message: str, folder: str | None = None, filename: str | None = None
) -> None:
    """
    Create a file and commit it to the repository.

    Args:
        message: The commit message.
        filename: The name of the file to be created. If not provided, a UUID will be generated.

    Returns:
        None
    """
    git("checkout", branch)

    if not filename:
        filename = str(uuid.uuid4())
    if folder:
        path = os.path.join(folder, filename)
    else:
        path = filename

    Path(path).touch()
    git("add", ".")
    git("commit", "-m", message)
    git("push", "-f")


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


def get_tags_with_annotations() -> list[str]:
    """
    Retrieve a chronological list of tags from the Git repository, formatted with their annotations.

    Note that chronological order is not reliable due to the fact that jobs run in
    parallel in Gitlab.

    Returns:
        A list of strings containing tags and their annotations.
    """
    # Get all tags, sorted by creation date
    tags = all_tags()

    tag_order = []
    for tag in tags:
        annotation = git(
            "tag", "-n", "--format=%(subject)", tag, stdout=subprocess.PIPE
        ).stdout.strip()
        if not annotation:
            print(f"No annotation message found for tag {tag}")
            continue

        # Append to the tag order list
        tag_order.append(f"{tag:<24} {annotation}")

    return tag_order


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


def print_git_graph():
    """
    Prints the git graph in color, for debugging purposes
    """
    return git(
        "log",
        "--graph",
        "--abbrev-commit",
        "--oneline",
        "--all",
        "--decorate",
    )


def verify_git_graph(expected_graph: str) -> None:
    """
    Verify the current state of the git graph matches an expected graph.

    Args:
        expected_graph (str): The expected output of the git log graph command.

    This function fetches the current git graph, compares it to the expected graph, and asserts equality.
    Differences trigger an assertion error, aiding in debugging git histories in tests.
    """
    expected = textwrap.dedent(expected_graph).strip()
    result = git_graph()
    if VERBOSE:
        print(result)
    assert result == expected, f"Expected graph:\n{expected}\nActual graph:\n{result}"


def find_pipeline_job(
    gitlab_project: gitlab.v4.objects.Project,
    job_name_pattern: re.Pattern,
    rev: str | None = None,
    source: str | None = None,
    updated_after: datetime | None = None,
) -> gitlab.v4.objects.ProjectJob:
    """
    Return a Gitlab job matching the given search parameters.
    """
    tries = 20
    while tries:
        print(
            f"Searching for active pipeline ({source=}, {rev=}) with {job_name_pattern=}"
        )
        # FIXME: it appears that this can deadlock here.
        pipelines = gitlab_project.pipelines.list(
            get_all=True, source=source, updated_after=updated_after
        )
        non_match = defaultdict(list)
        for pipeline in pipelines:
            if pipeline.status in ["success", "failed", "skipped"]:
                non_match["status"].append((pipeline.id, pipeline.status))
                continue
            if rev and pipeline.sha != rev:
                non_match["rev"].append((pipeline.id, pipeline.rev))
                continue

            jobs = pipeline.jobs.list(get_all=True)
            for job in jobs:
                if job_name_pattern.match(job.name):
                    return job
            non_match["job_name"].append((pipeline.id, [job.name for job in jobs]))
        print("Misses")
        pprint.pprint(dict(non_match))
        time.sleep(5.0)
        tries -= 1
    raise RuntimeError(
        f"Failed to find {source} job matching {job_name_pattern} for {rev}"
    )


# def retry(func: Callable, tries=5, msg="Failed") -> None:
#     while tries:
#         try:
#             return func()
#         except requests.exceptions.ConnectionError:
#             pass
#         time.sleep(1.0)
#         tries -= 1
#     raise RuntimeError(msg)


def wait_for_job(
    gitlab_project: gitlab.v4.objects.Project, job: gitlab.v4.objects.ProjectJob
):
    """
    Wait for the given job to complete
    """
    tries = 40
    while tries:
        print(f"{job.name} status: {job.status}")
        if job.status == "success":
            return
        elif job.status in ["failed", "skipped"]:
            job.pprint()
            raise RuntimeError(f"Job {job.status}")
        time.sleep(5.0)
        job = gitlab_project.jobs.get(job.id)
        tries -= 1
    job.pprint()
    raise RuntimeError("Job failed to complete")


def run_autotag(
    branch: str,
    remote_data: GitlabData | LocalData,
    annotation: str | None = None,
    base_rev: str | None = None,
    wait: bool = True,
) -> gitlab.v4.objects.ProjectJob | None:
    """
    Trigger the 'autotag' command to automatically generate a new git tag.

    Args:
        annotation (str): A custom annotation message for the git tag. Defaults to "automatic change detected".

    This function runs a command that automates the process of tagging the current commit in the git repository.
    """
    if annotation is None:
        # get from the commit message.  this mimics the behavior of gitlab-ci.yml
        annotation = git(
            "log", "--pretty=format:%s", "-n1", stdout=subprocess.PIPE
        ).stdout.strip()

    print(f"Running autotag: {annotation=}, {base_rev=}")
    if isinstance(remote_data, GitlabData):
        job = find_pipeline_job(
            remote_data.project,
            re.compile(rf"^auto-tag-{branch}$"),
            source="push",
            updated_after=datetime.fromisoformat(remote_data.project.updated_at),
        )
        if wait:
            wait_for_job(remote_data.project, job)
        print("autotag job done")
        return job

    time.sleep(DELAY)
    args = [
        sys.executable,
        "-m",
        "monoflow",
        "autotag",
        f"--annotation={annotation}",
    ]
    if base_rev:
        args.extend(["--base-rev", base_rev])
    subprocess.run(args, check=True)


def run_promote(
    beta: str, remote_data: GitlabData | LocalData, monkeypatch: MonkeyPatch
) -> list[dict[str, str]] | None:
    """
    Promote changes in a git repository to simulate a promotion process handled typically by CI/CD.

    Args:
        monkeypatch (MonkeyPatch): The pytest monkeypatch fixture to mock environment variables.

    This function sets the latest commit from the BETA branch as the start of a new cycle,
    checks out the specified branch, and triggers the 'promote' command.
    """
    if isinstance(remote_data, GitlabData):
        try:
            remote_data.schedule.play()
        except gitlab.exceptions.GitlabPipelinePlayError as err:
            # schedule id can become stale?
            print(err)
            schedules = remote_data.project.pipelineschedules.list(get_all=True)
            for mayabe_schedule in schedules:
                if mayabe_schedule.description == "Promote":
                    mayabe_schedule.play()
                    break
            else:
                raise RuntimeError("Could not find Promote schedule")
        job = find_pipeline_job(
            remote_data.project,
            re.compile("^promote$"),
            source="schedule",
            updated_after=datetime.fromisoformat(remote_data.project.updated_at),
        )
        wait_for_job(remote_data.project, job)
        print("promote job done")
        return None

    time.sleep(DELAY)
    result = subprocess.run(
        [sys.executable, "-m", "monoflow", "promote"], text=True, stdout=subprocess.PIPE
    )
    print(result.stdout)
    tag_args = []
    for line in result.stdout.splitlines():
        if line.startswith("Trigger: "):
            tag_args.append(json.loads(line.split(": ", 1)[1]))

    for project in PROJECTS:
        configure_environment(
            PROMOTION_PENDING_VAR.format(beta, project), "true", monkeypatch
        )
    return tag_args


def run_hotfix(branch: str, remote_data: GitlabData | LocalData) -> None:
    """
    Trigger the 'hotfix' command to handle hotfix operations without affecting minor version increments.
    """
    if isinstance(remote_data, GitlabData):
        job = find_pipeline_job(
            remote_data.project,
            re.compile(f"^hotfix-{branch}-to-.*$"),
            source="push",
            updated_after=datetime.fromisoformat(remote_data.project.updated_at),
        )
        wait_for_job(remote_data.project, job)
        return

    time.sleep(DELAY)
    subprocess.run([sys.executable, "-m", "monoflow", "hotfix"])


@contextlib.contextmanager
def pipeline(
    branch: str,
    title: str,
    remote_data: GitlabData | LocalData,
    monkeypatch,
    base_rev=None,
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
    git("checkout", branch)
    git("pull")

    print()
    print("USER")
    print_git_graph()

    cwd = os.getcwd()

    if isinstance(remote_data, LocalData):
        latest_commit = current_rev()

        if not base_rev:
            try:
                base_rev = git(
                    "rev-parse",
                    "HEAD^",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                ).stdout.strip()
            except subprocess.CalledProcessError:
                base_rev = "0000000000000000000000000000000000000000"

        configure_environment("CI_PIPELINE_SOURCE", "push", monkeypatch)
        configure_environment("CI_COMMIT_SHA", latest_commit, monkeypatch)
        configure_environment("CI_COMMIT_BEFORE_SHA", base_rev, monkeypatch)
        configure_environment("CI_COMMIT_BRANCH", branch, monkeypatch)

        tmpdir = tempfile.TemporaryDirectory()
        print("created temporary directory", tmpdir.name)
        os.chdir(tmpdir.name)
        git("init", "-b", branch)
        # FIXME: in Gitlab the "origin" is configured, and the only branch that exists is the remote trigger branch
        git("remote", "add", LOCAL_REMOTE, remote_data.remote)
        git("fetch", "--quiet", LOCAL_REMOTE, latest_commit)
        # in Gitlab all tags exist
        git("fetch", "--quiet", LOCAL_REMOTE, "--tags")
        git("checkout", "--detach", latest_commit)

        print()
        print("RUNNER")
        print_git_graph()

        yield

        tmpdir.cleanup()

    else:
        yield

    # change to local repo
    os.chdir(cwd)

    # sync with remote
    git("fetch", "--tags")
    git("fetch")
    git("checkout", branch)


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
            call("branch", f"--set-upstream-to={remote}/{branch}"),
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
            call("rev-parse", "--verify", branch, stderr=subprocess.PIPE),
            call("branch", f"--set-upstream-to={remote}/{branch}"),
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
            call("rev-parse", "--verify", branch, stderr=subprocess.PIPE),
            call("checkout", branch),
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
    with patch.dict("os.environ", {}, clear=True):
        assert Gitlab.get_remote() == LOCAL_REMOTE


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


def _run_autotag_jobs(tag_args: list[dict], remote_data, monkeypatch):
    """
    Execute one or more autotag pipelines.

    Autotag jobs may run in parallel on multiple pipelines.
    """
    pprint.pprint(tag_args)
    jobs = []
    for tag_arg in tag_args:
        with pipeline(
            tag_arg["branch"],
            "Post-promotion autotag",
            remote_data,
            monkeypatch,
        ):
            jobs.append(
                run_autotag(
                    tag_arg["branch"],
                    remote_data,
                    annotation=tag_arg["annotation"],
                    base_rev=tag_arg["base_rev"],
                    wait=False,
                )
            )
    if isinstance(remote_data, GitlabData):
        print("Waiting for jobs: {}".format([job.name for job in jobs]))
        for job in jobs:
            wait_for_job(remote_data.project, job)
        git("fetch", "--tags")
        git("fetch")


@pytest.mark.unit
def test_dev_cycle(setup_git_repo, monkeypatch) -> None:
    """
    Test the development cycle by mimicking typical operations in a CICD environment.

    This uses a 3-repo system to emulate Gitlab CI.

    1. local: the user's repo, and its state is used to test assertions
    2. runner: the temporary local clone used to run CI scripts.
    3. remote: represents the central Gitlab repo.  the "runner" pushes to this repo.

    Args:
        setup_git_repo: The pytest fixture for setting up a temporary Git repository.
        monkeypatch: The pytest monkeypatch fixture for setting environment variables.
    """
    local_repo, config, remote_data = setup_git_repo
    assert os.path.isdir(local_repo)
    assert latest_tag("projectA-*") == "projectA-1.0.0"

    # The trick here is to try and mimic what the
    # order of operations would be in a typical
    # development cycle where we have a mix of
    # things done by developers and others
    # done by our CICD.

    print_git_graph()
    print()

    # (ProjectA) Add feature 1 to BETA branch
    msg = f"{config.beta}: (projectA) add feature 1"
    create_file_and_commit(config.beta, msg, folder="projectA")
    with pipeline(
        config.beta,
        "(ProjectA) Add feature 1 to BETA branch",
        remote_data,
        monkeypatch,
    ):
        # this pipeline runs in response to a merged merge request
        run_autotag(config.beta, remote_data)
        configure_environment(
            PROMOTION_PENDING_VAR.format(config.beta, "projectA"), "false", monkeypatch
        )

    assert latest_tag("projectA-*") == "projectA-1.1.0b0"

    # Promote (promote always runs on rc, according to our .gitlab-ci.yml)
    with pipeline(
        config.rc,
        "Promote",
        remote_data,
        monkeypatch,
    ):
        # this pipeline runs manually or on a schedule
        tag_args = run_promote(config.beta, remote_data, monkeypatch)

    if tag_args is None:
        tag_args = [
            {
                "annotation": "promoting develop to staging!",
                "base_rev": None,
                "branch": "staging",
            }
        ]

    _run_autotag_jobs(tag_args, remote_data, monkeypatch)

    expected = rf"""
    * {config.beta}: (projectA) add feature 1 -  (HEAD -> {config.rc}, tag: projectA-1.1.0rc0, tag: projectA-1.1.0b0, origin/{config.rc}, origin/{config.beta}, {config.beta})
    * initial state -  (tag: projectB-1.0.0, tag: projectA-1.0.0, origin/{config.stable}, {config.stable})"""
    verify_git_graph(expected)

    assert latest_tag("projectA-*") == "projectA-1.1.0rc0"

    # (ProjectA) Add beta feature 2 to BETA branch
    msg = f"{config.beta}: (projectA) add beta feature 2"
    create_file_and_commit(config.beta, msg, folder="projectA")
    with pipeline(
        config.beta,
        "(ProjectA) Add beta feature 2 to BETA branch",
        remote_data,
        monkeypatch,
    ):
        # this pipeline runs in response to a merged merge request
        run_autotag(config.beta, remote_data)
        configure_environment(
            PROMOTION_PENDING_VAR.format(config.beta, "projectA"), "false", monkeypatch
        )

    expected = rf"""
    * {config.beta}: (projectA) add beta feature 2 -  (HEAD -> {config.beta}, tag: projectA-1.2.0b0, origin/{config.beta})
    * {config.beta}: (projectA) add feature 1 -  (tag: projectA-1.1.0rc0, tag: projectA-1.1.0b0, origin/{config.rc}, {config.rc})
    * initial state -  (tag: projectB-1.0.0, tag: projectA-1.0.0, origin/{config.stable}, {config.stable})"""
    verify_git_graph(expected)

    assert latest_tag("projectA-*") == "projectA-1.2.0b0"

    # (ProjectA) Add hotfix to STABLE branch
    msg = f"{config.stable}: (projectA) add hotfix"
    create_file_and_commit(config.stable, msg, folder="projectA")
    with pipeline(
        config.stable,
        "(ProjectA) Add hotfix to STABLE branch",
        remote_data,
        monkeypatch,
    ):
        # this pipeline runs in response to a merged merge request
        run_autotag(config.stable, remote_data)
        run_hotfix(config.stable, remote_data)

    assert latest_tag("projectA-*") == "projectA-1.0.1"

    annotation = f"auto-hotfix into {config.rc}: {config.stable}: (projectA) add hotfix"
    with pipeline(
        config.rc,
        "(ProjectA) Cascade hotfix to RC",
        remote_data,
        monkeypatch,
    ):
        # this pipeline runs in response to a push from the pipeline above
        run_autotag(config.rc, remote_data, annotation=annotation)
        run_hotfix(config.rc, remote_data)

    assert latest_tag("projectA-*") == "projectA-1.1.0rc1"

    annotation = (
        f"auto-hotfix into {config.beta}: {config.stable}: (projectA) add hotfix"
    )
    with pipeline(
        config.beta,
        "(ProjectA) Cascade hotfix to BETA",
        remote_data,
        monkeypatch,
    ):
        # this pipeline runs in response to a push from the pipeline above
        run_autotag(config.beta, remote_data, annotation=annotation)

    assert latest_tag("projectA-*") == "projectA-1.2.0b1"

    # (ProjectB) Add feature 1 to BETA branch
    msg = f"{config.beta}: (projectB) add feature 1"
    create_file_and_commit(config.beta, msg, folder="projectB")
    with pipeline(
        config.beta,
        "(ProjectB) Add feature 1 to BETA branch",
        remote_data,
        monkeypatch,
    ):
        # this pipeline runs in response to a merged merge request
        run_autotag(config.beta, remote_data)
        configure_environment(
            PROMOTION_PENDING_VAR.format(config.beta, "projectB"), "false", monkeypatch
        )

    assert latest_tag("projectB-*") == "projectB-1.1.0b0"

    # (ProjectA) Add hotfix 2 to RC branch
    msg = f"{config.rc}: (projectA) add hotfix 2"
    create_file_and_commit(config.rc, msg, folder="projectA")
    with pipeline(
        config.rc,
        "(ProjectA) Add hotfix 2 to RC branch",
        remote_data,
        monkeypatch,
    ):
        # this pipeline runs in response to a merged merge request
        run_autotag(config.rc, remote_data)
        run_hotfix(config.rc, remote_data)

    assert latest_tag("projectA-*") == "projectA-1.1.0rc2"

    annotation = f"auto-hotfix into {config.beta}: {config.rc}: (projectA) add hotfix 2"
    with pipeline(
        config.beta,
        "(ProjectA) Cascade hotfix to BETA",
        remote_data,
        monkeypatch,
    ):
        # this pipeline runs in response to a push from the pipeline above
        run_autotag(config.beta, remote_data, annotation=annotation)

    assert latest_tag("projectA-*") == "projectA-1.2.0b2"

    expected = rf"""
    *   auto-hotfix into {config.beta}: {config.rc}: (projectA) add hotfix 2 -  (HEAD -> {config.beta}, tag: projectA-1.2.0b2, origin/{config.beta})
    |\  
    | * {config.rc}: (projectA) add hotfix 2 -  (tag: projectA-1.1.0rc2, origin/{config.rc}, {config.rc})
    * | {config.beta}: (projectB) add feature 1 -  (tag: projectB-1.1.0b0)
    * | auto-hotfix into {config.beta}: master: (projectA) add hotfix -  (tag: projectA-1.2.0b1)
    |\| 
    | *   auto-hotfix into {config.rc}: master: (projectA) add hotfix -  (tag: projectA-1.1.0rc1)
    | |\  
    | | * {config.stable}: (projectA) add hotfix -  (tag: projectA-1.0.1, origin/{config.stable}, {config.stable})
    * | | {config.beta}: (projectA) add beta feature 2 -  (tag: projectA-1.2.0b0)
    |/ /  
    * / {config.beta}: (projectA) add feature 1 -  (tag: projectA-1.1.0rc0, tag: projectA-1.1.0b0)
    |/  
    * initial state -  (tag: projectB-1.0.0, tag: projectA-1.0.0)
    """
    verify_git_graph(expected)

    # there are no changes for projectB, so the tag should remain the same
    assert latest_tag("projectB-*") == "projectB-1.1.0b0"

    # Promote (promote always runs on rc, according to our .gitlab-ci.yml)
    with pipeline(
        config.rc,
        "Promote",
        remote_data,
        monkeypatch,
    ):
        tag_args = run_promote(config.beta, remote_data, monkeypatch)

    if tag_args is None:
        tag_args = [
            {
                "annotation": "promoting staging to master!",
                "base_rev": None,
                "branch": "master",
            },
            {
                "annotation": "promoting develop to staging!",
                "base_rev": None,
                "branch": "staging",
            },
        ]

    _run_autotag_jobs(tag_args, remote_data, monkeypatch)

    expected = rf"""
    *   auto-hotfix into {config.beta}: {config.rc}: (projectA) add hotfix 2 -  (HEAD -> {config.rc}, tag: projectB-1.1.0rc0, tag: projectA-1.2.0rc0, tag: projectA-1.2.0b2, origin/{config.rc}, origin/{config.beta}, {config.beta})
    |\  
    | * {config.rc}: (projectA) add hotfix 2 -  (tag: projectA-1.1.0rc2, tag: projectA-1.1.0, origin/{config.stable}, {config.stable})
    * | {config.beta}: (projectB) add feature 1 -  (tag: projectB-1.1.0b0)
    * | auto-hotfix into {config.beta}: {config.stable}: (projectA) add hotfix -  (tag: projectA-1.2.0b1)
    |\| 
    | *   auto-hotfix into {config.rc}: {config.stable}: (projectA) add hotfix -  (tag: projectA-1.1.0rc1)
    | |\  
    | | * {config.stable}: (projectA) add hotfix -  (tag: projectA-1.0.1)
    * | | {config.beta}: (projectA) add beta feature 2 -  (tag: projectA-1.2.0b0)
    |/ /  
    * / {config.beta}: (projectA) add feature 1 -  (tag: projectA-1.1.0rc0, tag: projectA-1.1.0b0)
    |/  
    * initial state -  (tag: projectB-1.0.0, tag: projectA-1.0.0)"""
    verify_git_graph(expected)

    # Promote (promote always runs on rc, according to our .gitlab-ci.yml)
    with pipeline(
        config.rc,
        "Promote",
        remote_data,
        monkeypatch,
    ):
        tag_args = run_promote(config.beta, remote_data, monkeypatch)

    if tag_args is None:
        tag_args = [
            {
                "annotation": "promoting staging to master!",
                "base_rev": None,
                "branch": "master",
            }
        ]

    _run_autotag_jobs(tag_args, remote_data, monkeypatch)

    expected = rf"""
    *   auto-hotfix into {config.beta}: {config.rc}: (projectA) add hotfix 2 -  (HEAD -> {config.stable}, tag: projectB-1.1.0rc0, tag: projectB-1.1.0, tag: projectA-1.2.0rc0, tag: projectA-1.2.0b2, tag: projectA-1.2.0, origin/{config.rc}, origin/{config.stable}, origin/{config.beta}, {config.rc}, {config.beta})
    |\  
    | * {config.rc}: (projectA) add hotfix 2 -  (tag: projectA-1.1.0rc2, tag: projectA-1.1.0)
    * | {config.beta}: (projectB) add feature 1 -  (tag: projectB-1.1.0b0)
    * | auto-hotfix into {config.beta}: {config.stable}: (projectA) add hotfix -  (tag: projectA-1.2.0b1)
    |\| 
    | *   auto-hotfix into {config.rc}: {config.stable}: (projectA) add hotfix -  (tag: projectA-1.1.0rc1)
    | |\  
    | | * {config.stable}: (projectA) add hotfix -  (tag: projectA-1.0.1)
    * | | {config.beta}: (projectA) add beta feature 2 -  (tag: projectA-1.2.0b0)
    |/ /  
    * / {config.beta}: (projectA) add feature 1 -  (tag: projectA-1.1.0rc0, tag: projectA-1.1.0b0)
    |/  
    * initial state -  (tag: projectB-1.0.0, tag: projectA-1.0.0)"""
    verify_git_graph(expected)

    if VERBOSE:
        print(get_tags_with_annotations())

    expected_tags = rf"""
projectB-1.1.0           promoting {config.rc} to {config.stable}!
projectA-1.2.0           promoting {config.rc} to {config.stable}!
projectB-1.1.0rc0        promoting {config.beta} to {config.rc}!
projectA-1.2.0rc0        promoting {config.beta} to {config.rc}!
projectA-1.1.0           promoting {config.rc} to {config.stable}!
projectA-1.2.0b2         auto-hotfix into {config.beta}: {config.rc}: (projectA) add hotfix 2
projectA-1.1.0rc2        {config.rc}: (projectA) add hotfix 2
projectB-1.1.0b0         {config.beta}: (projectB) add feature 1
projectA-1.2.0b1         auto-hotfix into {config.beta}: {config.stable}: (projectA) add hotfix
projectA-1.1.0rc1        auto-hotfix into {config.rc}: {config.stable}: (projectA) add hotfix
projectA-1.0.1           {config.stable}: (projectA) add hotfix
projectA-1.2.0b0         {config.beta}: (projectA) add beta feature 2
projectA-1.1.0rc0        promoting {config.beta} to {config.rc}!
projectA-1.1.0b0         {config.beta}: (projectA) add feature 1
projectB-1.0.0           initial state
projectA-1.0.0           initial state"""

    # order is not reliable due to Gitlab jobs running in parallel
    assert set(get_tags_with_annotations()) == set(
        textwrap.dedent(expected_tags).strip().splitlines()
    )
