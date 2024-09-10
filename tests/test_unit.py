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
from unittest.mock import patch, call
from typing import Generator, Literal, overload

import click

from tide.gitutils import (
    checkout_remote_branch,
    get_branches,
    get_latest_commit,
    current_rev,
    git,
    join,
    print_git_graph,
)

from tide.core import (
    GitlabBackend,
    GitlabRuntime,
    get_modified_projects,
    load_config,
    Config,
    GITLAB_REMOTE,
    ENVVAR_PREFIX,
)
from tide.cli import set_config

EXEC_MODE = os.environ.get("EXEC_MODE", "local")
assert EXEC_MODE in ["local", "remote", "gitlab-ci-local"]
REMOTE_MODE = EXEC_MODE == "remote"

if REMOTE_MODE:
    try:
        ACCESS_TOKEN = os.environ["ACCESS_TOKEN"]
    except KeyError:
        print("Must set ACCESS_TOKEN env var before running nox")
        raise
else:
    ACCESS_TOKEN = "dummy-access-token"

# set this to reuse an existing Gitlab remote during testing
FORCE_GITLAB_REMOTE = os.environ.get("FORCE_GITLAB_REMOTE")
KEEP_OLD_GITLAB_PROJECTS = os.environ.get("KEEP_OLD_GITLAB_PROJECTS", 3)
GITLAB_API_URL = "gitlab.com"

HERE = os.path.dirname(__file__)
PROJECTS = ["projectA", "projectB"]
# Git timestamps are limited to unix timestamp resolution: 1s
DELAY = 1.1
VERBOSE = os.environ.get("VERBOSE", "false").lower() in ("true", "1")


def all_tags(pattern: str | None = None) -> list[str]:
    """
    Return all of the tags in the Git repository.

    Args:
        pattern: glob pattern for tag names
    """
    tags = git(
        "tag", "--sort=-creatordate", "--format=%(tag)  %(creatordate)", capture=True
    ).splitlines()
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
        return git("describe", "--tags", "--abbrev=0", capture=True)


@pytest.fixture
def config() -> Config:
    return set_config(
        load_config(os.path.join(HERE, "..", "pyproject.toml"), verbose=VERBOSE)
    )


@pytest.fixture
def gitlab_project():
    """
    Create a Gitlab project with a randomized name if REMOTE_MODE is enabled.

    Uses GITLAB_API_URL which defaults to gitlab.com.

    In the cleanup phase, this deletes old projects, but preserves at most KEEP_OLD_GITLAB_PROJECTS
    number of projects for debugging.
    """
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
            # FIXME: set the default branch here?
            project = gl.projects.create(
                {
                    "name": f"semver-demo-{uuid.uuid4()}",
                    "visibility_level": gitlab.const.Visibility.PRIVATE,
                    "ci_push_repository_for_job_token_allowed": True,
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

    @property
    def remote_url(self) -> str:
        """https git url"""
        return self.project.http_url_to_repo


@dataclasses.dataclass
class LocalData:
    tempdir: tempfile.TemporaryDirectory

    @property
    def remote_url(self) -> str:
        return self.tempdir.name


@pytest.fixture
def setup_git_repo(
    tmpdir, gitlab_project, request
) -> Generator[tuple[str, Config], None, None]:
    """
    Pytest fixture that sets up a temporary Git repository with an initial commit of project files.

    Args:
        tmpdir: Temporary directory provided by pytest.

    Yields:
        The path of the temporary directory with initialized Git repository.

    This setup includes copying essential project files, creating an initial commit, and simulating
    an autotag process. It cleans up by removing the .git directory after tests are done.
    """
    if "branches" in request.keywords:
        branches = request.keywords["branches"].args[0]
    else:
        branches = {"beta": "develop", "rc": "staging", "stable": "master"}

    tmp_path = Path(str(tmpdir))
    print(f"Git repo: {tmp_path}")
    os.chdir(tmp_path)

    parent_directory = Path(
        os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    )
    files_to_copy = [
        ".gitignore",
        ".gitlab-ci.yml",
        "requirements.txt",
        "src",
        "README.md",
    ]

    for relpath in files_to_copy:
        src = parent_directory.joinpath(relpath)
        dest = tmp_path.joinpath(relpath)
        if src.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, dest)
        elif src.is_dir():
            shutil.copytree(src, dest)
        else:
            raise TypeError(src)

    # Update the tide section of pyproject.toml
    lines = (
        parent_directory.joinpath("pyproject.toml")
        .read_text()
        .splitlines(keepends=False)
    )
    index = lines.index("[tool.tide]")
    pyproject = tmp_path.joinpath("pyproject.toml")
    lines = lines[: index + 1]
    lines.append("managed_project = false")
    lines.append('tag_format = "$project-$version"')
    for release_id, branch in branches.items():
        lines.append(f'branches.{release_id} = "{branch}"')
    new_text = "\n".join(lines)
    pyproject.write_text(new_text)

    for project in PROJECTS:
        dest = tmp_path.joinpath(project)
        dest.mkdir(parents=True, exist_ok=True)
        dest.joinpath("pyproject.toml").write_text(f"""\
[tool.tide]
project = "{project}"
""")

    config = set_config(load_config(str(pyproject), verbose=VERBOSE))

    # Initialize the git repository
    git("init", "-b", config.stable)

    # Add all files and commit them
    message = "initial state"
    git("add", ".")
    git("commit", "-m", message, capture=True)
    rev = current_rev()
    git("checkout", "--detach", rev)

    if gitlab_project:
        remote_data = GitlabData(gitlab_project)
        push_opts = ["-o", "ci.skip"]

        if FORCE_GITLAB_REMOTE:
            # cleanup
            for schedule in gitlab_project.pipelineschedules.list(get_all=True):
                schedule.delete()
            # FIXME: this doesn't always seem to take effect
            for var in gitlab_project.variables.list(get_all=True):
                var.delete()
            assert gitlab_project.variables.list(get_all=True) == []

            # allow force push
            for branch in config.branches:
                try:
                    p_branch = gitlab_project.protectedbranches.get(branch)
                except gitlab.exceptions.GitlabGetError:
                    pass
                else:
                    p_branch.allow_force_push = True
                    p_branch.save()

            push_opts.append("-f")

    else:
        # FIXME: move this to gitlab_project
        # this is a stand-in for the remote repository for local testing
        local_remote = tempfile.TemporaryDirectory()
        os.chdir(local_remote.name)
        git("init", "--bare", "-b", config.stable)
        os.chdir(tmp_path)
        remote_data = LocalData(local_remote)
        push_opts = []

    git("remote", "add", "origin", remote_data.remote_url)

    if FORCE_GITLAB_REMOTE:
        # cleanup remote tags
        git("fetch", "--tags")
        tags = all_tags()
        if tags:
            git("tag", "-d", *tags)
            git("push", "origin", "--delete", *tags)
        git("prune")

    _tide("init", "--access-token", ACCESS_TOKEN, "--remote=origin")

    # make initial tags
    for project in PROJECTS:
        git("tag", "-a", f"{project}-1.0.0", "-m", message)
        time.sleep(DELAY)

    if FORCE_GITLAB_REMOTE:
        for branch in config.branches:
            git("branch", "-f", branch, rev)

    print_git_graph()

    git("push", "-f", "--all", *push_opts)
    git("push", "-f", "--tags", *push_opts)

    # Yield the directory path to allow tests to run in this environment
    yield str(tmp_path), config, remote_data

    if isinstance(remote_data, LocalData):
        remote_data.tempdir.cleanup()


def commit_file_and_push(
    branch: str, message: str, folder: str | None = None, filename: str | None = None
) -> None:
    """
    Create a file and commit it to the repository.

    Args:
        message: The commit message.
        folder: Sub-folder within the project. The folder should contain a pyproject.toml.
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
        annotation = git("tag", "-n", "--format=%(subject)", tag, capture=True)
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
        capture=True,
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

    Args:
        gitlab_project: Project object from the gitlab python API
        job_name_pattern: look for jobs whose name matches this regex
        rev: look for jobs running on the given git revision
        source: look for jobs triggered by this source event
        updated_after: look for jobs created or modified after this time
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


def wait_for_job(
    gitlab_project: gitlab.v4.objects.Project, gitlab_job: gitlab.v4.objects.ProjectJob
):
    """
    Wait for the given job to complete

    Args:
        gitlab_project: Project object from the gitlab python API
        gitlab_job: ProjectJob object from the gitlab python API
    """
    tries = 40
    while tries:
        print(f"{gitlab_job.name} status: {gitlab_job.status}")
        if gitlab_job.status == "success":
            return
        elif gitlab_job.status in ["failed", "skipped"]:
            gitlab_job.pprint()
            raise RuntimeError(f"Job {gitlab_job.status}")
        time.sleep(5.0)
        gitlab_job = gitlab_project.jobs.get(gitlab_job.id)
        tries -= 1
    gitlab_job.pprint()
    raise RuntimeError("Job failed to complete")


@overload
def _tide(*args: str, capture: Literal[True]) -> str:
    pass


@overload
def _tide(*args: str) -> subprocess.CompletedProcess[str]:
    pass


def _tide(*args: str, capture: bool = False) -> str | subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable,
        "-m",
        "tide",
    ]
    if VERBOSE:
        cmd.append("--verbose")
    cmd.extend(args)
    if capture:
        output = subprocess.run(
            cmd, check=True, text=True, stdout=subprocess.PIPE
        ).stdout.strip()
        print(output)
        return output
    else:
        return subprocess.run(cmd, text=True, check=True)


def gitlab_ci_local(job_name: str, runner_env: dict[str, str]):
    options = []
    remote_path = runner_env["CI_REPOSITORY_URL"]
    options.extend(["--volume", f"{remote_path}:{remote_path}"])
    for varname, value in runner_env.items():
        options.extend(["--variable", f"{varname}={value}"])
    print(options)
    subprocess.run(["gitlab-ci-local", job_name] + options, check=True)


def run_autotag(
    runner_env,
    remote_data: GitlabData | LocalData,
    annotation: str | None = None,
    base_rev: str | None = None,
    wait: bool = True,
) -> gitlab.v4.objects.ProjectJob | None:
    """
    Trigger the 'autotag' command to automatically generate a new git tag.

    Args:
        runner_env: dict of env vars used to setup the runner env
        remote_data: Dataclass corresponding to the remote being used
        annotation: A custom annotation message for the git tag. Defaults to "automatic change detected".
        wait: block until the Gitlab job completes.

    This function runs a command that automates the process of tagging the current commit in the git repository.
    """
    if annotation is None:
        # get from the commit message.  this mimics the behavior of gitlab-ci.yml
        annotation = git("log", "--pretty=format:%s", "-n1", capture=True)

    print(f"Running autotag: {annotation=}, {base_rev=}")
    if isinstance(remote_data, GitlabData):
        job = find_pipeline_job(
            remote_data.project,
            re.compile(r"^auto-tag$"),
            source="push",
            updated_after=datetime.fromisoformat(remote_data.project.updated_at),
        )
        if wait:
            wait_for_job(remote_data.project, job)
        print("autotag job done")
        return job
    else:
        if EXEC_MODE == "gitlab-ci-local":
            # FIXME: use the push-opts file created by backend.push() to more accurately
            #  simulate the flow of variables between jobs
            gitlab_ci_local(
                "auto-tag",
                runner_env | {f"{ENVVAR_PREFIX}_AUTOTAG_ANNOTATION": annotation},
            )
        else:
            time.sleep(DELAY)
            args = [
                "autotag",
                f"--annotation={annotation}",
            ]
            if base_rev:
                args.extend(["--base-rev", base_rev])
            _tide(*args)


def run_promote(
    runner_env: dict[str, str],
    remote_data: GitlabData | LocalData,
    config: Config,
) -> list[dict[str, str]] | None:
    """
    Promote changes in a git repository to simulate a promotion process handled typically by CI/CD.

    Args:
        runner_env: dict of env vars used to setup the runner env
        remote_data: Dataclass corresponding to the remote being used

    This function sets the latest commit from the BETA branch as the start of a new cycle,
    checks out the specified branch, and triggers the 'promote' command.
    """
    if isinstance(remote_data, GitlabData):
        # schedule id can become stale?
        schedule = GitlabBackend(config)._find_promote_job(remote_data.project)
        if schedule:
            schedule.play()
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
    else:
        if EXEC_MODE == "gitlab-ci-local":
            gitlab_ci_local("promote", runner_env)
        else:
            time.sleep(DELAY)
            _tide("promote")

        json_file = os.path.join(remote_data.remote_url, "push-data.json")
        click.echo(f"Reading local output from {json_file}")
        if os.path.exists(json_file):
            with open(json_file) as f:
                tag_args = json.load(f)
            os.remove(json_file)
        else:
            tag_args = []
        return tag_args


def run_hotfix(
    runner_env, src_branch: str, dst_branch: str, remote_data: GitlabData | LocalData
) -> None:
    """
    Trigger the 'hotfix' command to handle hotfix operations without affecting minor version increments.

    Args:
        runner_env: dict of env vars used to setup the runner env
        src_branch: source of the hotfix
        dst_branch: destination for the hotfix
    """
    job_name = "hotfix"
    if isinstance(remote_data, GitlabData):
        job = find_pipeline_job(
            remote_data.project,
            re.compile(f"^{job_name}$"),
            source="push",
            updated_after=datetime.fromisoformat(remote_data.project.updated_at),
        )
        wait_for_job(remote_data.project, job)
    else:
        if EXEC_MODE == "gitlab-ci-local":
            gitlab_ci_local(job_name, runner_env)
        else:
            time.sleep(DELAY)
            _tide("hotfix")


def get_runner_env(
    config: Config,
    remote_url: str,
    latest_commit: str,
    base_rev: str | None,
    branch: str,
) -> dict[str, str]:
    if not base_rev:
        try:
            base_rev = git("rev-parse", "HEAD^", capture=True)
        except subprocess.CalledProcessError:
            base_rev = "0000000000000000000000000000000000000000"

    title = git("log", "--pretty=format:%s", "-n1", capture=True)
    return {
        "CI_REPOSITORY_URL": remote_url,
        "CI_PIPELINE_SOURCE": "push",
        "CI_COMMIT_SHA": latest_commit,
        "CI_COMMIT_BEFORE_SHA": base_rev,
        "CI_COMMIT_BRANCH": branch,
        "CI_COMMIT_TITLE": title,
        "ACCESS_TOKEN": ACCESS_TOKEN,
        "GITLAB_USER_EMAIL": "foo@bar.com",
        "GITLAB_USER_NAME": "fakeuser",
        "CI_COMMIT_REF_PROTECTED": "true",
        "CI_DEFAULT_BRANCH": config.most_experimental_branch() or config.stable,
        "GITLAB_CI": "false",
    }


def setup_runner_env(monkeypatch, env: dict[str, str]):
    for key, value in env.items():
        configure_environment(key, value, monkeypatch)


@contextlib.contextmanager
def pipeline(
    config: Config,
    tmp_path_factory,
    branch: str,
    description: str,
    remote_data: GitlabData | LocalData,
    monkeypatch,
    base_rev=None,
) -> Generator[dict[str, str], None]:
    """
    Simulate the beginning of a new CI pipeline.

    Checkout a branch and configure the environment.

    Args:
        branch: The branch to check out.
        description: Description of the pipeline for debugging purposes
        remote_data: Dataclass corresponding to the remote being used
        monkeypatch: The pytest monkeypatch fixture for setting environment variables.
        base_rev: The git revision that represents the commit before the commit for
            the Gitlab pipeline
    """
    print()
    print(f"Starting pipeline: {description}")
    git("checkout", branch)
    git("pull")

    print()
    print("USER")
    print_git_graph()

    cwd = os.getcwd()

    latest_commit = current_rev()

    runner_env = get_runner_env(
        config, remote_data.remote_url, latest_commit, base_rev, branch
    )

    if isinstance(remote_data, LocalData):
        setup_runner_env(monkeypatch, runner_env)
        tmpdir: Path = tmp_path_factory.mktemp(branch)
        print("created temporary directory", tmpdir)

        os.chdir(tmpdir)
        git("init", "-b", branch)
        # FIXME: in Gitlab the "origin" is configured, and the only branch that exists is the remote trigger branch
        git("remote", "add", GITLAB_REMOTE, remote_data.remote_url)
        git("fetch", "--quiet", GITLAB_REMOTE, latest_commit)
        # in Gitlab all tags exist
        git("fetch", "--quiet", GITLAB_REMOTE, "--tags")
        git("checkout", "--detach", latest_commit)

        print()
        print("RUNNER", tmpdir)
        print_git_graph()

    yield runner_env

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
def test_current_branch_in_ci_environment(config, monkeypatch):
    runner_env = get_runner_env(config, "fakeurl", "fakecommit", "basebaserev", "beta")
    setup_runner_env(monkeypatch, runner_env)
    assert GitlabRuntime(config).current_branch() == "beta"


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
def test_get_remote_in_ci_environment(config, monkeypatch) -> None:
    url = "https://gitlab-ci-token:[MASKED]@gitlab.example.com/someproject/"

    runner_env = get_runner_env(config, url, "fakecommit", "basebaserev", "beta")
    setup_runner_env(monkeypatch, runner_env)
    with patch("tide.core.git") as mock_git:
        mock_git.return_value = None

        assert GitlabRuntime(config).get_remote() == "origin"


@pytest.mark.unit
def test_get_current_branch_in_ci_environment(config, monkeypatch) -> None:
    runner_env = get_runner_env(config, "fakeurl", "fakecommit", "basebaserev", "beta")
    setup_runner_env(monkeypatch, runner_env)
    assert GitlabRuntime(config).current_branch() == "beta"


def run_promote_and_autotag_jobs(
    config: Config,
    tmp_path_factory,
    expected_tag_args: list[dict],
    remote_data,
    monkeypatch,
) -> None:
    """
    Execute one or more autotag pipelines.

    Autotag jobs may run in parallel on multiple pipelines.
    """
    # Promote (promote always runs on rc, according to our .gitlab-ci.yml)
    with pipeline(
        config,
        tmp_path_factory,
        config.rc or config.stable,
        "Promote",
        remote_data,
        monkeypatch,
    ) as env:
        tag_args = run_promote(env, remote_data, config)

    if REMOTE_MODE:
        assert tag_args is None
        tag_args = expected_tag_args
    else:
        assert len(tag_args) == len(expected_tag_args)

    print("Tag args")
    pprint.pprint(tag_args)

    jobs = []
    for tag_arg in tag_args:
        with pipeline(
            config,
            tmp_path_factory,
            tag_arg["branch"],
            "Post-promotion autotag",
            remote_data,
            monkeypatch,
            base_rev=tag_arg["base_rev"],
        ) as env:
            jobs.append(
                run_autotag(
                    env,
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
def test_dev_cycle(setup_git_repo, monkeypatch, tmp_path_factory) -> None:
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

    # -- commit and autotag
    # (ProjectA) Add feature 1 to BETA branch
    msg = f"{config.beta}: (projectA) add feature 1"
    commit_file_and_push(config.beta, msg, folder="projectA")
    with pipeline(
        config,
        tmp_path_factory,
        config.beta,
        "(ProjectA) Add feature 1 to BETA branch",
        remote_data,
        monkeypatch,
    ) as env:
        # this pipeline runs in response to a merged merge request
        run_autotag(env, remote_data)

    assert latest_tag("projectA-*") == "projectA-1.1.0b0"

    # -- promote
    tag_args = [
        {
            "annotation": "promoting develop to staging!",
            "base_rev": None,
            "branch": "staging",
        }
    ]

    run_promote_and_autotag_jobs(
        config, tmp_path_factory, tag_args, remote_data, monkeypatch
    )

    expected = rf"""
    * {config.beta}: (projectA) add feature 1 -  (HEAD -> {config.rc}, tag: projectA-1.1.0rc0, tag: projectA-1.1.0b0, origin/{config.rc}, origin/{config.beta}, {config.beta})
    * initial state -  (tag: projectB-1.0.0, tag: projectA-1.0.0, origin/{config.stable}, {config.stable})"""
    verify_git_graph(expected)

    assert latest_tag("projectA-*") == "projectA-1.1.0rc0"

    # -- commit and autotag
    # (ProjectA) Add beta feature 2 to BETA branch
    msg = f"{config.beta}: (projectA) add beta feature 2"
    commit_file_and_push(config.beta, msg, folder="projectA")
    with pipeline(
        config,
        tmp_path_factory,
        config.beta,
        "(ProjectA) Add beta feature 2 to BETA branch",
        remote_data,
        monkeypatch,
    ) as env:
        # this pipeline runs in response to a merged merge request
        run_autotag(env, remote_data)

    expected = rf"""
    * {config.beta}: (projectA) add beta feature 2 -  (HEAD -> {config.beta}, tag: projectA-1.2.0b0, origin/{config.beta})
    * {config.beta}: (projectA) add feature 1 -  (tag: projectA-1.1.0rc0, tag: projectA-1.1.0b0, origin/{config.rc}, {config.rc})
    * initial state -  (tag: projectB-1.0.0, tag: projectA-1.0.0, origin/{config.stable}, {config.stable})"""
    verify_git_graph(expected)

    assert latest_tag("projectA-*") == "projectA-1.2.0b0"

    # -- commit, autotag, and hotfix
    # (ProjectA) Add hotfix to STABLE branch
    msg = f"{config.stable}: (projectA) add hotfix"
    commit_file_and_push(config.stable, msg, folder="projectA")
    with pipeline(
        config,
        tmp_path_factory,
        config.stable,
        "(ProjectA) Add hotfix to STABLE branch",
        remote_data,
        monkeypatch,
    ) as env:
        # this pipeline runs in response to a merged merge request
        run_autotag(env, remote_data)
        run_hotfix(env, config.stable, config.rc, remote_data)

    assert latest_tag("projectA-*") == "projectA-1.0.1"

    annotation = f"auto-hotfix into {config.rc}: {config.stable}: (projectA) add hotfix"
    with pipeline(
        config,
        tmp_path_factory,
        config.rc,
        "(ProjectA) Cascade hotfix to RC",
        remote_data,
        monkeypatch,
    ) as env:
        # this pipeline runs in response to a push from the pipeline above
        run_autotag(env, remote_data, annotation=annotation)
        run_hotfix(env, config.rc, config.beta, remote_data)

    assert latest_tag("projectA-*") == "projectA-1.1.0rc1"

    annotation = (
        f"auto-hotfix into {config.beta}: {config.stable}: (projectA) add hotfix"
    )
    with pipeline(
        config,
        tmp_path_factory,
        config.beta,
        "(ProjectA) Cascade hotfix to BETA",
        remote_data,
        monkeypatch,
    ) as env:
        # this pipeline runs in response to a push from the pipeline above
        run_autotag(env, remote_data, annotation=annotation)

    assert latest_tag("projectA-*") == "projectA-1.2.0b1"

    # -- commit and autotag

    # (ProjectB) Add feature 1 to BETA branch
    msg = f"{config.beta}: (projectB) add feature 1"
    commit_file_and_push(config.beta, msg, folder="projectB")
    with pipeline(
        config,
        tmp_path_factory,
        config.beta,
        "(ProjectB) Add feature 1 to BETA branch",
        remote_data,
        monkeypatch,
    ) as env:
        # this pipeline runs in response to a merged merge request
        run_autotag(env, remote_data)

    assert latest_tag("projectB-*") == "projectB-1.1.0b0"

    # -- commit, autotag, and hotfix

    # (ProjectA) Add hotfix 2 to RC branch
    msg = f"{config.rc}: (projectA) add hotfix 2"
    commit_file_and_push(config.rc, msg, folder="projectA")
    with pipeline(
        config,
        tmp_path_factory,
        config.rc,
        "(ProjectA) Add hotfix 2 to RC branch",
        remote_data,
        monkeypatch,
    ) as env:
        # this pipeline runs in response to a merged merge request
        run_autotag(env, remote_data)
        run_hotfix(env, config.rc, config.beta, remote_data)

    assert latest_tag("projectA-*") == "projectA-1.1.0rc2"

    annotation = f"auto-hotfix into {config.beta}: {config.rc}: (projectA) add hotfix 2"
    with pipeline(
        config,
        tmp_path_factory,
        config.beta,
        "(ProjectA) Cascade hotfix to BETA",
        remote_data,
        monkeypatch,
    ) as env:
        # this pipeline runs in response to a push from the pipeline above
        run_autotag(env, remote_data, annotation=annotation)

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

    # -- promote

    tag_args = [
        {
            "annotation": "promoting staging to master!",
            "base_rev": None,
            "branch": config.stable,
        },
        {
            "annotation": "promoting develop to staging!",
            "base_rev": None,
            "branch": config.rc,
        },
    ]
    run_promote_and_autotag_jobs(
        config, tmp_path_factory, tag_args, remote_data, monkeypatch
    )

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

    # -- promote

    tag_args = [
        {
            "annotation": "promoting staging to master!",
            "base_rev": None,
            "branch": config.stable,
        }
    ]

    run_promote_and_autotag_jobs(
        config, tmp_path_factory, tag_args, remote_data, monkeypatch
    )

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

    # -- promote

    tag_args = []
    # no new tags are created when we run promote and there are no changes on RC or BETA
    run_promote_and_autotag_jobs(
        config, tmp_path_factory, tag_args, remote_data, monkeypatch
    )
    git("checkout", config.stable)
    # verify against the previous "expected" state
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


@pytest.mark.unit
@pytest.mark.branches({"stable": "main"})
def test_dev_cycle_one_branch(setup_git_repo, monkeypatch, tmp_path_factory) -> None:
    """
    Test the development cycle by mimicking typical operations in a CICD environment.
    """
    local_repo, config, remote_data = setup_git_repo
    assert os.path.isdir(local_repo)
    assert latest_tag("projectA-*") == "projectA-1.0.0"

    print_git_graph()
    print()

    # -- commit and autotag
    # (ProjectA) Add feature 1 to STABLE branch
    msg = f"{config.stable}: (projectA) add feature 1"
    commit_file_and_push(config.stable, msg, folder="projectA")
    with pipeline(
        config,
        tmp_path_factory,
        config.stable,
        "(ProjectA) Add feature 1 to STABLE branch",
        remote_data,
        monkeypatch,
    ) as env:
        # this pipeline runs in response to a merged merge request
        run_autotag(env, remote_data)

    assert latest_tag("projectA-*") == "projectA-1.0.1"

    # -- commit and autotag
    # (ProjectA) Add feature 2 to STABLE branch
    msg = f"{config.stable}: (projectB) add feature 1"
    commit_file_and_push(config.stable, msg, folder="projectB")
    with pipeline(
        config,
        tmp_path_factory,
        config.stable,
        "(projectB) Add feature 1 to STABLE branch",
        remote_data,
        monkeypatch,
    ) as env:
        # this pipeline runs in response to a merged merge request
        run_autotag(env, remote_data)

    assert latest_tag("projectB-*") == "projectB-1.0.1"

    # -- promote
    tag_args = []

    run_promote_and_autotag_jobs(
        config, tmp_path_factory, tag_args, remote_data, monkeypatch
    )

    assert latest_tag("projectA-*") == "projectA-1.0.1"
