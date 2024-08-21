from __future__ import absolute_import, print_function, annotations

import os
import re
import subprocess
import time
import json

import click

try:
    import tomli as tomllib  # noqa: F401
except ImportError:
    import tomllib  # type: ignore[no-redef]
from pathlib import Path
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import urlparse, urlunparse
from abc import abstractmethod
from enum import Enum
from typing import TYPE_CHECKING, cast

from .gitutils import git, get_tags, branch_exists, checkout, current_rev, join, GitRepo

if TYPE_CHECKING:
    import gitlab.v4.objects


TOOL_NAME = "tide"
ENVVAR_PREFIX = TOOL_NAME.upper()
PROMOTION_BASE_MSG = "promotion base"
HERE = os.path.dirname(__file__)
CONFIG: Config
GITLAB_REMOTE = "gitlab_origin"
CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}

# FIXME: add these to config file
HOTFIX_MESSAGE = "auto-hotfix into {upstream_branch}: {message}"
PROMOTION_CYCLE_START_MESSAGE = "starting new {release_id} cycle."
PROMOTION_MESSAGE = "promoting {upstream_branch} to {branch}!"

cache = lru_cache(maxsize=None)


class ReleaseID(str, Enum):
    """Represents semver pre-releases, plus 'stable' (i.e. non-pre-release)"""

    alpha = "alpha"
    beta = "beta"
    rc = "rc"
    stable = "stable"


def is_url(s: str) -> bool:
    """
    Return whether the string looks like a URL.
    """
    return "://" in s


def load_config(path: str | None = None, verbose: bool = False) -> Config:
    """
    Load and return the GitFlow configuration from the pyproject.toml file.

    Returns:
        A configuration object
    """
    if path is None:
        path = os.path.join(os.getcwd(), "pyproject.toml")
    if not os.path.isfile(path):
        raise click.ClickException("No pyproject.toml found")

    with open(path, "rb") as f:
        data = tomllib.load(f)

    try:
        settings = data["tool"][TOOL_NAME]
    except KeyError:
        raise click.ClickException(f"'tool.{TOOL_NAME}' section missing: {path}")

    try:
        branches = settings["branches"]
    except KeyError:
        raise click.ClickException(
            f"'tool.{TOOL_NAME}.branches' section missing: {path}"
        )

    config = Config(verbose=verbose)
    # Loop through branches once and extract all needed information
    for release_id in ReleaseID:
        branch_name = branches.get(release_id.value)
        if branch_name is None:
            continue

        # Append branch name to CONFIG.branches list
        config.branches.append(branch_name)

        # Update CONFIG.branch_to_release_id dictionary
        config.branch_to_release_id[branch_name] = release_id
        setattr(config, release_id.value, branch_name)
    return config


def set_config(config: Config) -> Config:
    """
    Set the global configuration object.
    """
    global CONFIG
    CONFIG = config
    return config


@dataclass
class Config:
    stable: str = "master"
    rc: str | None = None
    beta: str | None = None
    alpha: str | None = None
    # branches in order from most-experimental to stable
    branches: list[str] = field(default_factory=list)
    # branch name to pre-release name (alpha, beta, rc). None for stable.
    branch_to_release_id: dict[str, ReleaseID] = field(default_factory=dict)
    verbose: bool = False

    def most_experimental_branch(self) -> str | None:
        """
        Return the most experimental branch.

        This branch corresponds to the earliest pre-release specified by the config.
        """
        if self.branches[0] == self.stable:
            return None
        else:
            return self.branches[0]


def get_backend(url: str | None = None) -> Backend:
    """
    Return a Backend corresponding to where the current python process is pushing/pulling.
    """
    if os.environ.get("GITLAB_CI", "false") == "true" or (url and "gitlab" in url):
        return GitlabBackend()
    else:
        return TestGitlabBackend()


def get_runtime() -> Runtime:
    """
    Return a Runtime corresponding to where the current python process is *running*
    """
    print(os.environ.get("GITLAB_CI"))
    if os.environ.get("GITLAB_CI", "false") == "true":
        return GitlabRuntime()
    # gitlab-ci-local and our unittests set this to false as an inidicator that
    # we are testing gitlab, but not IN gitlab.
    elif os.environ.get("GITLAB_CI") == "false":
        return TestGitlabRuntime()
    else:
        return LocalRuntime()


class Runtime:
    """
    Interact with a git repo that is local to the current process
    """

    @abstractmethod
    def current_branch(self) -> str:
        """
        Get the current git branch name.

        Returns:
            The name of the current branch.

        Raises:
            RuntimeError: If the current branch name is not obtainable
        """

    @abstractmethod
    def get_base_rev(self) -> str:
        """
        Get the git revision that represents the state of the repo before the changes
        that triggered the current pipeline

        The files changed after this revision will be used to determine which
        project tags to increment.
        """

    @abstractmethod
    def get_remote(self) -> str:
        """
        Configure and retrieve the name of a Git remote for use in CI environments.

        This function configures a Git remote using environment variables
        that should be set in the GitLab CI/CD environment. It configures the git user credentials,
        splits the repository URL to format it with the access token, and adds the remote to the
        local git configuration.

        Returns:
            The name of the configured remote
        """


class Backend:
    """
    Interact with a remote git backend.
    """

    def push(
        self, *args: str, variables: dict[str, str] | None = None, skip_ci: bool = False
    ) -> None:
        opts = []
        if skip_ci:
            opts.extend(["-o", "ci.skip"])
        if variables:
            for key, value in variables.items():
                opts.extend(
                    [
                        "-o",
                        f"ci.variable={key}={value}",
                    ]
                )
        git("push", *args, *opts)

    def init_local_repo(self, remote_name: str) -> None:
        """
        Setup the local repository.

        Args:
            remote_name: name of the git remote, used to query the url
        """
        # initialize the local repo
        git("fetch", remote_name, quiet=CONFIG.verbose)

        if CONFIG.verbose:
            git("branch", "-la")
            git("remote", "-v")

        # setup branches.
        # loop from stable to pre-release branches, bc we set all release_id branches to
        # the location of stable
        for branch in reversed(CONFIG.branches):
            if branch_exists(branch):
                if branch != CONFIG.stable:
                    click.echo(
                        f"{branch} already exists. This can potentially cause problems",
                        err=True,
                    )
            else:
                git("branch", "-f", branch, CONFIG.stable)

            remote_branch = f"{remote_name}/{branch}"
            if branch_exists(remote_branch):
                git("branch", f"--set-upstream-to={remote_branch}", branch)
            else:
                self.push("--set-upstream", remote_name, branch, skip_ci=True)

    @abstractmethod
    def init_remote_repo(
        self, remote_url: str, access_token: str, save_token: bool
    ) -> None:
        """
        Setup the remote repository.

        Args:
            remote_url: URL of the git remote
            access_token: token used to authenticate changes to the remote.
            save_token: whether to save `access_token` into the remote.
        """


class GitlabRuntime(Runtime):
    def current_branch(self) -> str:
        try:
            return os.environ["CI_COMMIT_BRANCH"]
        except KeyError:
            raise RuntimeError

    def get_base_rev(self) -> str:
        return os.environ["CI_COMMIT_BEFORE_SHA"]

    @cache
    def _setup_remote(self, url: str) -> None:
        try:
            access_token = os.environ["ACCESS_TOKEN"]
        except KeyError:
            raise click.ClickException(
                "You must setup a CI variable in the Gitlab process called ACCESS_TOKEN\n"
                "See https://docs.gitlab.com/ee/ci/variables/#for-a-project"
            )
        git("config", "user.email", os.environ["GITLAB_USER_EMAIL"])
        git("config", "user.name", os.environ["GITLAB_USER_NAME"])
        url = url.split("@")[-1]
        git("remote", "add", GITLAB_REMOTE, f"https://oauth2:{access_token}@{url}")

    def get_remote(self) -> str:
        url = os.environ["CI_REPOSITORY_URL"]
        self._setup_remote(url)
        return GITLAB_REMOTE


class GitlabBackend(Backend):
    """Gitlab-specific behavior"""

    PROMOTION_SCHEDULED_JOB_NAME = "Promote Gitflow Branches"

    @cache
    def _conn(self, base_url: str, access_token: str) -> gitlab.Gitlab:
        """
        Get a cached gitlab connection object.
        """
        try:
            import gitlab
        except ImportError:
            raise click.ClickException(
                f"To use the init command you must run: pip install {TOOL_NAME}[init]"
            )

        return gitlab.Gitlab(
            url=base_url,
            private_token=access_token,
            retry_transient_errors=True,
        )

    def _find_promote_job(
        self, project: gitlab.v4.objects.Project
    ) -> gitlab.v4.objects.ProjectPipelineSchedule | None:
        """
        Find the scheduled job that is used to trigger promotion.
        """
        schedules = project.pipelineschedules.list(get_all=True)
        for schedule in schedules:
            if schedule.description == self.PROMOTION_SCHEDULED_JOB_NAME:
                return schedule
        return None

    def init_remote_repo(
        self, remote_url: str, access_token: str, save_token: bool
    ) -> None:
        try:
            import gitlab.const
            import gitlab.exceptions
        except ImportError:
            raise click.ClickException(
                f"To use the init command you must run: pip install {TOOL_NAME}[init]"
            )

        if remote_url.endswith(".git"):
            remote_url = remote_url[:-4]

        # separate 'https://gitlab.com/groupname/projectname' into
        # 'https://gitlab.com' and 'groupname/projectname'
        url = urlparse(remote_url)
        base_url = urlunparse(url._replace(path=""))
        # remove leading "/"
        project_and_ns = url.path[1:]

        gl = self._conn(base_url, access_token)
        try:
            project = gl.projects.get(project_and_ns)
        except gitlab.exceptions.GitlabGetError:
            raise click.ClickException(f"Could not find project '{project_and_ns}")

        if save_token:
            try:
                project.variables.get("ACCESS_TOKEN")
            except gitlab.exceptions.GitlabGetError:
                project.variables.create(
                    {
                        "key": "ACCESS_TOKEN",
                        "value": access_token,
                        "protected": True,
                        "masked": True,
                    }
                )
                click.echo("Created ACCESS_TOKEN project variable")
            else:
                click.echo("ACCESS_TOKEN project variable already exists. Skipping")
        else:
            # FIXME: validate that ACCESS_TOKEN has been set at the project or group level
            pass

        for branch in CONFIG.branches:
            try:
                p_branch = project.protectedbranches.get(branch)
            except gitlab.exceptions.GitlabGetError:
                project.protectedbranches.create(
                    {
                        "name": branch,
                        "merge_access_level": gitlab.const.AccessLevel.DEVELOPER,
                        "push_access_level": gitlab.const.AccessLevel.MAINTAINER,
                        "allow_force_push": True,
                    }
                )
            else:
                p_branch.allow_force_push = True
                p_branch.save()
        click.echo("Setup protected branches")

        default_branch = CONFIG.most_experimental_branch() or CONFIG.stable
        gl.projects.update(project.id, {"default_branch": default_branch})

        if not self._find_promote_job(project):
            # this must happen after the branch has been created in the remote and initial
            # commit pushed
            schedule = project.pipelineschedules.create(
                {
                    "ref": CONFIG.stable,
                    "description": self.PROMOTION_SCHEDULED_JOB_NAME,
                    "cron": "6 6 * * 4",
                    "active": False,
                }
            )
            schedule.variables.create({"key": "SCHEDULED_JOB_NAME", "value": "promote"})
            click.echo(
                f"Created '{self.PROMOTION_SCHEDULED_JOB_NAME}' scheduled job, in non-active state"
            )


class LocalRuntime(Runtime):
    """
    Used for processes running in local git repos, not in CI.
    """

    def current_branch(self) -> str:
        branch = git("branch", "--show-current", capture=True)
        if not branch:
            raise RuntimeError
        return branch

    def get_base_rev(self) -> str:
        try:
            return git("rev-parse", "HEAD^", capture=True)
        except subprocess.CalledProcessError:
            return "0000000000000000000000000000000000000000"

    def get_remote(self) -> str:
        # FIXME: this should probably be configurable somehow, but it's a user pref
        #  so it shouldn't live in pyproject.toml
        return "origin"


class TestGitlabRuntime(GitlabRuntime):
    @cache
    def _setup_remote(self, url: str) -> None:
        # overridden to prevent adding the oath token to the remote url
        git("config", "user.email", os.environ["GITLAB_USER_EMAIL"])
        git("config", "user.name", os.environ["GITLAB_USER_NAME"])


class TestGitlabBackend(GitlabBackend):
    @cache
    def _conn(self, base_url: str, access_token: str) -> gitlab.Gitlab:
        # overridden to return a mocked Gitlab connection object.
        import unittest.mock

        return cast("gitlab.Gitlab", unittest.mock.MagicMock())

    def push(
        self, *args: str, variables: dict[str, str] | None = None, skip_ci: bool = False
    ) -> None:
        # overridden to write variables to a json object rather than use push options
        # which are not supported by local git repos.
        if variables:
            json_file = os.path.join(os.environ["CI_REPOSITORY_URL"], "push-opts.json")
            click.echo(f"Writing local output to {json_file}")
            if os.path.exists(json_file):
                os.remove(json_file)

            with open(json_file, "w") as f:
                json.dump(variables, f)

        git("push", *args)


def cz(*args: str, folder: str | Path | None = None) -> str:
    """
    Run commitizen in a subprocess.
    """
    if CONFIG.verbose:
        click.echo(f"running {args} in {folder}")
    output = subprocess.check_output(
        ["cz"] + list(args),
        text=True,
        cwd=folder,
    )
    return output


def get_upstream_branch(branch: str) -> str | None:
    """
    Determine the upstream branch for a given branch name based on configuration.

    Args:
        branch: The name of the branch for which to find the upstream branch.

    Returns:
        The name of the upstream branch, or None if there is no upstream branch.

    Raises:
        ClickException: If the branch is not found in the configuration.
    """
    try:
        index = CONFIG.branches.index(branch)
    except ValueError:
        raise click.ClickException(f"Invalid branch: {branch}")

    if index > 0:
        return CONFIG.branches[index - 1]
    else:
        return None


def is_pending_bump(remote: str, branch: str, folder: Path) -> bool:
    """
    Return whether the given branch and folder combination are awaiting a minor bump.

    Args:
        remote: The remote repository name
        branch: one of the registered gitflow branches
        folder: folder within the repo that defines commitizen tag rules

    Returns:
        whether it is pending or not
    """
    from commitizen.config import read_cfg
    from commitizen.providers import ScmProvider

    if branch != CONFIG.most_experimental_branch():
        return False

    # Find the closest promotion note to the current branch
    promotion_rev = get_promotion_marker(remote)
    if promotion_rev is None:
        if CONFIG.verbose:
            click.echo("No promote marker found", err=True)
        return True

    cwd = os.getcwd()
    os.chdir(folder)
    try:
        # Use the matching logic from commitizen, which will read the commitizen
        # config file for us (note: it supports more than just pyproject.toml).
        provider = ScmProvider(read_cfg())
        matcher = provider._tag_format_matcher()
        if CONFIG.verbose:
            click.echo(f"Found promotion base rev: {promotion_rev}")
        # List any tags for this project folder between this branch and the promotion note
        all_tags = get_tags(end_rev=promotion_rev)
        tags = [t for t in all_tags if matcher(t)]
        return not tags
    finally:
        os.chdir(cwd)


def get_promotion_marker(remote: str) -> str | None:
    """
    Get the hash for the most recent promotion commit.

    Args:
        remote: The remote repository name
    """
    git("fetch", remote, "refs/notes/*:refs/notes/*")
    output = git("log", "--format=%H %N", "-n20", capture=True)
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ", 1)
        if len(parts) == 1:
            continue
        if parts[1] == PROMOTION_BASE_MSG:
            return parts[0]
    return None


def set_promotion_marker(remote: str, branch: str) -> None:
    """
    Store a state for whether the given project on the given branch needs to have a minor bump.

    If it is true for a given branch, then get_tag_for_branch() will return a minor increment.
    After this, autotag() will set the pending state to False until.

    This pending state is reset to True after each promotion event.

    Args:
        remote: The remote repository name
        branch: one of the registered gitflow branches
    """
    git("fetch", remote, "refs/notes/*:refs/notes/*")
    # FIXME: forcing here, because the same commit can be the promotion base more than once.  should we skip?
    git("notes", "add", "--force", "-m", PROMOTION_BASE_MSG, branch)
    git("push", remote, "refs/notes/*")


def get_tag_for_branch(remote: str, branch: str, folder: Path) -> str:
    """
    Determine the appropriate new tag for a given branch based on the latest changes.

    This function uses the Commitizen tool to determine the next tag name for a branch, potentially
    adjusting for pre-release tags and minor version increments.

    Args:
        remote: The remote repository name
        branch: The name of the branch for which to generate the tag.
        folder: The folder within the repo that controls the tag.

    Returns:
        The new tag to be created

    Raises:
        RuntimeError: If the command does not generate an output or fails to determine the tag.
    """
    try:
        release_id = CONFIG.branch_to_release_id[branch]
    except KeyError:
        raise click.ClickException(
            f"{branch} is not a valid release branch.  "
            f"Must be one of {', '.join(CONFIG.branches)}"
        )

    increment = "patch"

    # Only apply minor increment the most experimental branch
    if is_pending_bump(remote, branch, folder):
        increment = "minor"

    args = [f"--increment={increment}"]
    if release_id != ReleaseID.stable:
        args += ["--prerelease", release_id.value]

    if increment == "minor":
        args += ["--increment-mode=exact"]

    # run this in the project directory so that the pyproject.toml is accessible.
    output = cz(*(["bump"] + list(args) + ["--dry-run", "--yes"]), folder=folder)
    match = re.search("tag to create: (.*)", output)

    if not match:
        raise click.ClickException(output)

    tag = match.group(1).strip()

    return tag


def get_projects() -> list[tuple[Path, str | None]]:
    """Get the list of projects within the repo.

    A project is a folder with a pyproject.toml file with a `tool.commitizen` section.
    """
    results = []
    repo = GitRepo(".")
    for path in repo.file_matches(include=("**/pyproject.toml",)):
        with open(path, "rb") as f:
            data = tomllib.load(f)
            try:
                data["tool"]["commitizen"]
            except KeyError:
                pass
            else:
                try:
                    name = data["project"]["name"]
                except KeyError:
                    name = None
                results.append((Path(path).parent, name))
    return sorted(results)


def get_modified_projects(base_rev: str | None = None) -> list[tuple[Path, str | None]]:
    """Get the list of projects with changes files.

    A project is defined as a folder with a pyproject.toml file with a `tool.commitizen` section.

    Args:
        base_rev: The Git revision to compare against when identifying changed files
    """
    if not base_rev:
        runtime = get_runtime()
        base_rev = runtime.get_base_rev()

    # Compare the current commit with the branch you want to merge with:
    # FIXME: do not included deleted files
    output = git("diff-tree", "--name-only", "-r", base_rev, "HEAD", capture=True)
    all_files = output.splitlines()
    if CONFIG.verbose:
        if all_files:
            click.echo(f"Modified files between {base_rev} and HEAD:")
            for path in all_files:
                click.echo(f" {path}")
        else:
            click.echo(f"No modified files between {base_rev} and HEAD", err=True)

    # find the deepest project that the file belongs to
    projects = dict(get_projects())
    project_dirs = list(reversed(projects))

    results = set()
    for changed_file in all_files:
        for project_dir in project_dirs:
            if project_dir in Path(changed_file).parents:
                results.add(project_dir)

    return [(project_dir, projects[project_dir]) for project_dir in sorted(results)]


@click.group(context_settings=CONTEXT_SETTINGS)
@click.option("--config", "-c", "config_path", metavar="CONFIG", type=str)
@click.option("--verbose", "-v", is_flag=True, default=False)
def cli(config_path: str, verbose: bool) -> None:
    set_config(load_config(config_path, verbose))


@cli.command()
@click.option(
    "--access-token",
    required=True,
    metavar="TOKEN",
    help="Security token used to authenticate with the remote",
)
@click.option(
    "--remote",
    default="origin",
    metavar="REMOTE",
    show_default=True,
    help=(
        "The name of the git remote in the current git repo "
        "(e.g. configured using `git remote`) or the remote URL. "
        "Providing a URL implies --no-local"
    ),
)
@click.option(
    "--save-token/--no-save-token",
    default=True,
    help="Whether to save the access token into the remote as a reusable "
    "variable. If this is disabled, you must configure the ACCESS_TOKEN "
    "manually.",
)
@click.option(
    "--init-local/--no-local",
    default=True,
    help="Whether to initialize the local git repo",
)
@click.option(
    "--init-remote/--no-remote",
    default=True,
    help="Whether to initialize the remote git repo (i.e. Gitlab)",
)
def init(
    access_token: str,
    remote: str,
    save_token: bool,
    init_local: bool,
    init_remote: bool,
) -> None:
    """
    Initialize the current git repo and its associated Gitlab project for use with tide.

    This command must be run from a git repo, and the repo must have the Gitlab project setup
    as a remote, either by being cloned from it, or via `git remote add`.
    """
    import tempfile

    if is_url(remote):
        init_local = False
        remote_url = remote
    else:
        # FIXME: print a user friendly error if we're not in a git repo.
        # FIXME: handle remote not setup correctly
        remote_url = git("remote", "get-url", remote, capture=True)

    backend = get_backend(remote_url)

    if init_local:
        backend.init_local_repo(remote)
    else:
        # we may still need to create branches in the remote, so create a dummy clone
        with tempfile.TemporaryDirectory() as tmpdir:
            git("clone", f"--branch={CONFIG.stable}", remote_url, tmpdir)
            pwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                backend.init_local_repo("origin")
            finally:
                os.chdir(pwd)

    # FIXME: add pyproject.toml section?  check if it exists?  Hard to do automatically,
    #  because in a monorepo there could be many.
    # FIXME: create a stub gitlab-ci.yml file if it doesn't exist?
    if init_remote:
        backend.init_remote_repo(remote_url, access_token, save_token)


@cli.command()
@click.option(
    "--annotation",
    default="automatic change detected",
    show_default=True,
    help="Message to pass for tag annotations.",
)
@click.option(
    "--base-rev",
    metavar="SHA",
    help="The Git revision to compare against when identifying changed files.",
)
def autotag(annotation: str, base_rev: str | None) -> None:
    """
    Tag the current branch with a new version number and push the tag to the remote repository.
    """
    runtime = get_runtime()
    branch = runtime.current_branch()
    remote = runtime.get_remote()

    backend = get_backend()

    projects = get_modified_projects(base_rev)
    if projects:
        for project_folder, _ in projects:
            # Auto-tag
            tag = get_tag_for_branch(remote, branch, project_folder)

            # NOTE: this delay is necessary to create stable sorting of tags
            # because git's time resolution is 1s (same as unix timestamp).
            # https://stackoverflow.com/questions/28237043/what-is-the-resolution-of-gits-commit-date-or-author-date-timestamps
            time.sleep(1.1)

            click.echo(f"Creating new tag: {tag} on branch: {branch} {time.time()}")
            git("tag", "-a", tag, "-m", annotation)

            # FIXME: we may want to push all tags at once
            click.echo(f"Pushing {tag} to remote")
            backend.push(remote, tag)
    else:
        click.echo("No tags generated!", err=True)


@cli.command()
def hotfix() -> None:
    """
    Merge hotfixes from a feature branch back to upstream branches.
    """
    runtime = get_runtime()
    branch = runtime.current_branch()
    remote = runtime.get_remote()
    upstream_branch = get_upstream_branch(branch)
    if not upstream_branch:
        click.echo(f"No branch upstream from {branch}. Skipping auto-merge")
        return

    # Auto-merge
    # Record the current state
    message = git("log", "--pretty=format: %s", "-1", capture=True)
    # strip out previous Automerge formatting
    match = re.match(
        HOTFIX_MESSAGE.format(upstream_branch="[^:]+", message="(.*)$"), message
    )
    if match:
        message = match.groups()[0]

    git("checkout", "-B", f"{branch}_temp")

    # Fetch the upstream branch
    git("fetch", remote, upstream_branch)

    checkout(remote, upstream_branch, create=True)

    msg = HOTFIX_MESSAGE.format(upstream_branch=upstream_branch, message=message)
    click.echo(msg)

    try:
        git("merge", f"{branch}_temp", "-m", msg)
    except subprocess.CalledProcessError:
        click.echo("Conflicts:", err=True)
        git("diff", "--name-only", "--diff-filter=U")
        raise

    # this will trigger a full pipeline for upstream_branch, and potentially another auto-merge
    click.echo(f"Pushing {upstream_branch} to {remote}")
    variables = {
        f"{ENVVAR_PREFIX}_AUTOTAG_ANNOTATION": msg,
    }
    get_backend().push(remote, upstream_branch, variables=variables)


@cli.command()
def promote() -> None:
    """
    Promote changes through the branch hierarchy.

    e.g. from alpha -> beta -> rc -> stable.
    """
    backend = get_backend()
    runtime = get_runtime()
    remote = runtime.get_remote()
    if CONFIG.verbose:
        click.echo(f"remote = {remote}")

    local_output = []

    def promote_branch(branch: str, log_msg_template: str) -> None:
        """
        - Checkout the branch
        - Merge with the upstream branch, if it exists
        - Push, skipping hotfixes

        The branch is left checked out.
        """
        upstream_branch = get_upstream_branch(branch)
        release_id = CONFIG.branch_to_release_id[branch]
        log_msg = log_msg_template.format(
            branch=branch, upstream_branch=upstream_branch, release_id=release_id.value
        )

        click.echo(f"Fetching {remote}/{branch}")
        git("fetch", remote, branch)

        base_rev = checkout(remote, branch, create=True)

        if upstream_branch:
            git("fetch", remote, upstream_branch)
            click.echo(f"Merging with upstream branch {remote}/{upstream_branch}")
            git("merge", join(remote, upstream_branch), "-m", f"{log_msg}")

        variables = {
            f"{ENVVAR_PREFIX}_SKIP_HOTFIX": "true",
            f"{ENVVAR_PREFIX}_AUTOTAG_ANNOTATION": log_msg,
            f"{ENVVAR_PREFIX}_AUTOTAG_BASE_REV": base_rev,
        }

        # Trigger test/tag jobs for these new versions, but skip auto-hotfix
        # --atomic means if there's a failure to push any of the refs, the entire
        # operation will fail (like a databases).  May not be strictly necessary here.
        click.echo("Pushing changes")
        backend.push("--atomic", remote, branch, variables=variables)

        # FIXME: switch to using push-opts.json
        if (
            isinstance(backend, TestGitlabBackend)
            and upstream_branch
            and base_rev != current_rev()
        ):
            push_info = {
                "annotation": log_msg,
                "base_rev": base_rev,
                "branch": branch,
            }
            local_output.append(push_info)
            click.echo(f"Trigger: {json.dumps(push_info)}")

    # Promotion time!

    # loop from stable to most experimental
    for branch in reversed(CONFIG.branches):
        # It doesn't matter what the active branch is when promote is run: the next thing
        # that we do is checkout CONFIG.stable.
        if branch == CONFIG.most_experimental_branch():
            msg = PROMOTION_CYCLE_START_MESSAGE
        else:
            msg = PROMOTION_MESSAGE

        promote_branch(branch, msg)

    if local_output:
        json_file = os.path.join(os.environ["CI_REPOSITORY_URL"], "push-data.json")
        click.echo(f"Writing local output to {json_file}")
        with open(json_file, "w") as f:
            json.dump(local_output, f)

    # Note: we do not make a tag on our cycle-start branch at this time. Instead, we wait
    # for the first commit on the branch to do so.
    experimental_branch = CONFIG.most_experimental_branch()
    if experimental_branch:
        set_promotion_marker(remote, experimental_branch)


@cli.command
@click.option("--modified", "-m", is_flag=True, default=False)
# FIXME: add output format
# FIXME: add option to write to file
def projects(modified: bool) -> None:
    """
    List project paths within the repo.

    A project is a folder with a pyproject.toml file with a `tool.commitizen` section.
    """

    if modified:
        projects_ = get_modified_projects()
    else:
        projects_ = get_projects()

    for project_dir, project_name in projects_:
        if project_name is None:
            project_name = "[unset]"
        click.echo(f"{project_name} = {project_dir}")


@cli.command(name="next-version")
@click.option(
    "--path",
    default=".",
    type=click.Path(exists=True, file_okay=False),
    show_default=True,
    help="Folder within the repository. A pyproject.toml should reside in the "
    "specified directory",
)
@click.option(
    "--branch",
    default=None,
    help=(
        "The release branch to use to determine the next version. "
        "Defaults to the current branch."
    ),
)
@click.option(
    "--origin",
    default="origin",
    show_default=True,
    help="The git remote to use to when determining the next version.",
)
def next_tag(path: str, branch: str | None, remote: str) -> None:
    """
    Get the next tag.
    """
    if branch is None:
        runtime = get_runtime()
        branch = runtime.current_branch()
    click.echo(get_tag_for_branch(remote, branch, Path(path)))


def main() -> None:
    import shutil

    return cli(
        auto_envvar_prefix=ENVVAR_PREFIX,
        max_content_width=shutil.get_terminal_size().columns,
    )
