from __future__ import absolute_import, print_function, annotations

import base64
import fnmatch
import os
import pathlib
import re
import subprocess
import time
import json
import shutil

import click

try:
    import tomli as tomlllib  # noqa: F401
except ImportError:
    import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import urlparse, urlunparse
from abc import abstractmethod
from typing import Literal, overload

if False:
    import gitlab.v4.objects


TOOL_NAME = "monoflow"
ENVVAR_PREFIX = TOOL_NAME.upper()
BRANCH_ORDER = ["alpha", "beta", "rc", "stable"]
PROMOTION_BASE_MSG = "promotion base"
HOTFIX_MESSAGE = "auto-hotfix into {upstream_branch}: {message}"
HERE = os.path.dirname(__file__)
CONFIG: Config
GITLAB_REMOTE = "gitlab_origin"

cache = lru_cache(maxsize=None)


def is_url(s: str) -> bool:
    return "://" in s


def load_config(path=None, verbose: bool = False) -> Config:
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
        config = tomllib.load(f)

    try:
        settings = config["tool"][TOOL_NAME]
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
    for prerelease in BRANCH_ORDER:
        branch_name = branches.get(prerelease)
        if branch_name is None:
            if prerelease == "stable":
                branch_name = "master"
            else:
                continue

        # Append branch name to CONFIG.branches list
        config.branches.append(branch_name)

        # Update CONFIG.branch_to_prerelease dictionary
        config.branch_to_prerelease[branch_name] = (
            None if prerelease == "stable" else prerelease
        )
        setattr(config, prerelease, branch_name)
    return config


def variable_safe_encode(folder: str) -> str:
    import base64

    # strip equal signs because they are not compatible with env vars
    return base64.urlsafe_b64encode(folder.encode("utf-8")).decode("utf-8").strip("=")


def variable_safe_decode(encoded: str) -> str:
    b = encoded.encode("utf-8")
    # equal signs are used as padding
    padding = 3 - (len(b) % 3)
    return base64.urlsafe_b64decode(b + b"==" * padding).decode("utf-8")


def set_config(config: Config) -> Config:
    global CONFIG
    CONFIG = config
    return config


@dataclass
class Config:
    stable: str = "master"
    rc: str = "staging"  # FIXME: Make optional
    beta: str = "develop"  # FIXME: Make optional
    alpha: str | None = None  # FIXME: support alpha
    branches: list[str] = field(default_factory=list)
    branch_to_prerelease: dict[str, str] = field(default_factory=dict)
    verbose: bool = False


def get_backend() -> type[Backend]:
    if "CI" in os.environ:
        return GitlabBackend
    else:
        return TestGitlabBackend


class Backend:
    """
    Interact with a git backend / CI runtime
    """

    supports_push_options: bool

    @classmethod
    @abstractmethod
    def current_branch(cls) -> str:
        """
        Get the current git branch name.

        Returns:
            The name of the current branch.

        Raises:
            RuntimeError: If the current branch name is not obtainable
        """

    @classmethod
    @abstractmethod
    def get_base_rev(cls) -> str:
        pass

    @classmethod
    @abstractmethod
    def get_remote(cls) -> str:
        """
        Configure and retrieve the name of a Git remote for use in CI environments.

        This function configures a Git remote using environment variables
        that should be set in the GitLab CI/CD environment. It configures the git user credentials,
        splits the repository URL to format it with the access token, and adds the remote to the
        local git configuration.

        Returns:
            The name of the configured remote
        """

    @classmethod
    def init_local_repo(cls, remote_name: str):
        """
        Setup the local repository.

        Args:
            remote_name: name of the git remote, used to query the url
        """
        # initialize the local repo
        git("fetch", "--all")

        if CONFIG.verbose:
            git("branch", "-la")
            git("remote", "-v")

        if cls.supports_push_options:
            push_opts = ["-o", "ci.skip"]
        else:
            push_opts = []

        # setup branches.
        # loop from stable to pre-release branches, bc we set all prerelease branches to
        # the location of stable
        for branch in reversed(CONFIG.branches):
            if branch_exists(branch):
                if branch != CONFIG.stable:
                    click.echo(
                        f"{branch} already exists. This can potentially cause problems"
                    )
            else:
                git("branch", "-f", branch, CONFIG.stable)

            remote_branch = f"{remote_name}/{branch}"
            if branch_exists(remote_branch):
                git("branch", f"--set-upstream-to={remote_branch}", branch)
            else:
                git("push", "--set-upstream", remote_name, branch, *push_opts)

    @classmethod
    @abstractmethod
    def init_remote_repo(
        cls, remote_url: str, access_token: str, save_token: bool
    ) -> None:
        """
        Setup the remote repository.

        Args:
            remote_url: URL of the git remote
            access_token: token used to authenticate changes to the remote.
            save_token: whether to save `access_token` into the remote.
        """


class GitlabBackend(Backend):
    """Class to quarantine Gitlab-specific behavior

    Eventually this can serve as an abstraction to support other CI systems
    """

    PROMOTION_SCHEDULED_JOB_NAME = "Promote Gitflow Branches"

    supports_push_options = True

    @classmethod
    def _find_promote_job(
        cls, project: gitlab.v4.objects.Project
    ) -> gitlab.v4.objects.ProjectPipelineSchedule | None:
        schedules = project.pipelineschedules.list(get_all=True)
        for schedule in schedules:
            if schedule.description == cls.PROMOTION_SCHEDULED_JOB_NAME:
                return schedule
        return None

    @classmethod
    def current_branch(cls) -> str:
        try:
            return os.environ["CI_COMMIT_BRANCH"]
        except KeyError:
            raise RuntimeError

    @classmethod
    def get_base_rev(cls) -> str:
        return os.environ["CI_COMMIT_BEFORE_SHA"]

    @classmethod
    @cache
    def _setup_remote(cls, url: str) -> None:
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

    @classmethod
    def get_remote(cls) -> str:
        url = os.environ["CI_REPOSITORY_URL"]
        cls._setup_remote(url)
        return GITLAB_REMOTE

    @classmethod
    def init_remote_repo(
        cls, remote_url: str, access_token: str, save_token: bool
    ) -> None:
        try:
            import gitlab
        except ImportError:
            click.ClickException(
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

        gl = gitlab.Gitlab(
            url=base_url,
            private_token=access_token,
            retry_transient_errors=True,
        )
        try:
            project = gl.projects.get(project_and_ns)
        except gitlab.exceptions.GitlabGetError:
            raise click.ClickException(f"Could not find project '{project_and_ns}")

        if save_token:
            # FIXME: set to masked
            project.variables.create({"key": "ACCESS_TOKEN", "value": access_token})
            click.echo("Created ACCESS_TOKEN project variable")
        else:
            # FIXME: validate that ACCESS_TOKEN has been set at the project or group level
            pass

        if not cls._find_promote_job(project):
            # this must happen after the branch has been created in the remote and initial commit pushed
            schedule = project.pipelineschedules.create(
                {
                    "ref": CONFIG.rc,
                    "description": cls.PROMOTION_SCHEDULED_JOB_NAME,
                    "cron": "6 6 * * 4",
                    "active": False,
                }
            )
            schedule.variables.create({"key": "SCHEDULED_JOB_NAME", "value": "promote"})
            click.echo(
                f"Created '{cls.PROMOTION_SCHEDULED_JOB_NAME}' scheduled job, in non-active state"
            )


class TestGitlabBackend(GitlabBackend):
    supports_push_options = False

    @classmethod
    def get_remote(cls) -> str:
        """Override to do nothing"""
        return GITLAB_REMOTE

    @classmethod
    def init_remote_repo(
        cls, remote_name: str, access_token: str, save_token: bool
    ) -> None:
        """Override to do nothing"""
        pass


def cz(*args: str, folder: str | None = None):
    if CONFIG.verbose:
        click.echo(args)
    output = subprocess.check_output(
        ["cz"] + list(args),
        text=True,
        cwd=folder,
    )
    return output


@overload
def git(*args: str, quiet: bool = False, capture: Literal[True]) -> str:
    pass


@overload
def git(*args: str, quiet: bool = False) -> subprocess.CompletedProcess[str]:
    pass


def git(
    *args: str, quiet: bool = False, capture: bool = False
) -> subprocess.CompletedProcess | str:
    """
    Execute a git command with the specified arguments.

    Args:
        *args: Command line arguments for git.

    Returns:
        A subprocess.CompletedProcess instance, if capture is False, else stdout of the process.

    Raises:
        subprocess.CalledProcessError: If the git command fails.
    """
    cmd = ["git"] + [str(arg).strip() for arg in args]
    if CONFIG.verbose:
        click.echo(cmd)
    if capture:
        output = subprocess.run(
            cmd,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL if quiet else None,
        ).stdout.strip()
        click.echo(output)
        return output
    else:
        return subprocess.run(
            cmd,
            text=True,
            check=True,
            stdout=subprocess.DEVNULL if quiet else None,
            stderr=subprocess.DEVNULL if quiet else None,
        )


def get_tags(
    pattern: str | None = None, start_rev: str = "HEAD", end_rev: str | None = None
) -> list[str]:
    """
    Return all of the tags in the Git repository.

    Args:
        pattern: glob pattern for tag names
        start_rev: list tags reachable from this commit
        end_rev: list tags up until this commit
    """
    args = ["log", "--tags", "--format=%D", start_rev]
    if end_rev:
        args.extend(["--not", end_rev])

    lines = git(*args, capture=True).splitlines()
    tags = []
    for line in lines:
        parts = line.split(", ")
        for part in parts:
            if part.startswith("tag: "):
                tag = part[5:]
                if pattern is None or fnmatch.fnmatch(tag, pattern):
                    tags.append(tag)
    return tags


def get_branches() -> list[str]:
    """
    Retrieve a list of all local branches in the git repository.

    Returns:
        A list of branch names.
    """
    output = git("branch", capture=True)
    return [x.split()[-1] for x in output.splitlines()]


def checkout(remote: str | None, branch: str, create: bool = False) -> str:
    """
    Check out a specific branch, optionally creating it if it doesn't exist.

    Args:
        remote: The remote repository name
        branch: The branch name to check out.
        create: Whether to create the branch if it does not exist (default is False).
    """
    args = ["checkout"]

    if remote:
        if create and not branch_exists(branch):
            args += ["--track", join(remote, branch)]
        else:
            args = ["branch", f"--set-upstream-to={join(remote, branch)}"]
    else:
        if create and not branch_exists(branch):
            if CONFIG.verbose:
                click.echo(f"Creating branch {branch}")
            args += ["-b", branch]
        else:
            args += [branch]
    git(*args)
    return get_latest_commit(None, branch)


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


def current_rev() -> str:
    return git("rev-parse", "HEAD", capture=True)


def branch_exists(branch: str) -> bool:
    try:
        git("rev-parse", "--verify", branch, quiet=True)
    except subprocess.CalledProcessError:
        return False
    return True


def get_latest_commit(remote: str | None, branch_name: str) -> str:
    """
    Fetch the latest commit hash from a specific branch.

    Args:
        remote: The remote repository name
        branch_name: The name of the branch to fetch the latest commit from.

    Returns:
        str: The latest commit hash of the specified branch.

    Raises:
        subprocess.CalledProcessError: If the git command fails.
    """
    if remote:
        git("fetch", remote, branch_name)
    return git("rev-parse", join(remote, branch_name), capture=True)


def is_pending_bump(remote: str, branch: str, folder: str) -> bool:
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

    # FIXME: support rc?
    if branch != CONFIG.beta:
        return False

    # Find the closest promotion note to the current beta branch
    promotion_rev = get_promotion_marker(remote)
    if promotion_rev is None:
        click.echo("No promote marker found")
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
        # List any tags for this project folder between beta branch and the promotion note
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
        print(parts)
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


def get_tag_for_branch(remote: str, branch: str, folder: str) -> str:
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
    prerelease = CONFIG.branch_to_prerelease[branch]

    increment = "patch"

    # Only apply minor increment beta
    if is_pending_bump(remote, branch, folder):
        increment = "minor"

    args = [f"--increment={increment}"]
    if prerelease:
        args += ["--prerelease", prerelease]

    if increment == "minor":
        args += ["--increment-mode=exact"]

    # run this in the project directory so that the pyproject.toml is accessible.
    output = cz(*(["bump"] + list(args) + ["--dry-run", "--yes"]), folder=folder)
    match = re.search("tag to create: (.*)", output)

    if not match:
        raise click.ClickException(output)

    tag = match.group(1).strip()

    return tag


def join(remote: str | None, branch: str) -> str:
    """
    Construct a full branch path with remote prefix if specified.

    Args:
        remote: The remote repository name
        branch: The branch name.

    Returns:
        str: The full path to the branch, prefixed by the remote name if specified.
    """
    if remote:
        return f"{remote}/{branch}"
    else:
        return branch


def get_projects() -> list[str]:
    """Get the list of projects within the repo.

    A project is a folder with a pyproject.toml file with a `tool.commitizen` section.
    """
    # FIXME: make this more complete
    # FIXME: don't do this with a disk crawl, use the git manifest
    results = []
    for item in pathlib.Path(".").iterdir():
        if item.is_dir() and item.joinpath("pyproject.toml").is_file():
            results.append(item.name)
    return sorted(results)


def get_modified_projects(base_rev: str | None = None) -> list[str]:
    """Get the list of projects with changes files.

    A project is defined as a folder with a pyproject.toml file with a `tool.commitizen` section.

    Args:
        base_rev: The Git revision to compare against when identifying changed files
    """
    runtime = get_backend()
    if not base_rev:
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

    # FIXME: limit this to items in get_projects()
    return sorted(set(os.path.dirname(x) for x in all_files))


@click.group()
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
        "(configured using `git remote`) or the remote URL. "
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
    Initialize the current git repo and its associated Gitlab project for use with monoflow.

    This command must be run from a git repo, and the repo must have the Gitlab project setup
    as a remote, either by being cloned from it, or via `git remote add`.
    """
    runtime = get_backend()
    if is_url(remote):
        init_local = False
        remote_url = remote
    else:
        # FIXME: print a user friendly error if we're not in a git repo.
        # FIXME: handle remote not setup correctly
        remote_url = git("remote", "get-url", remote, capture=True)

    # FIXME: add pyproject.toml section?  check if it exists?  Hard to do automatically,
    #  because in a monorepo there could be many.
    # FIXME: create a stub gitlab-ci.yml file if it doesn't exist?
    if init_remote:
        runtime.init_remote_repo(remote_url, access_token, save_token)

    if init_local:
        runtime.init_local_repo(remote)


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
    Automatically tag the current branch with a new version number and push the tag to the remote repository.
    """
    runtime = get_backend()
    branch = runtime.current_branch()
    remote = runtime.get_remote()

    project_folders = get_modified_projects(base_rev)
    if project_folders:
        for project_folder in project_folders:
            # Auto-tag
            tag = get_tag_for_branch(remote, branch, project_folder)

            # NOTE: this delay is necessary to create stable sorting of tags
            # because git's time resolution is 1s (same as unix timestamp).
            # https://stackoverflow.com/questions/28237043/what-is-the-resolution-of-gits-commit-date-or-author-date-timestamps
            time.sleep(1.1)

            click.echo(f"Creating new tag: {tag} on branch: {branch} {time.time()}")
            git("tag", "-a", tag, "-m", annotation)

            # FIXME: we may want to push all tags at once
            if remote:
                click.echo(f"Pushing {tag} to remote")
                git("push", remote, tag)
    else:
        click.echo("No tags generated!", err=True)


@cli.command()
def hotfix() -> None:
    """
    Handle automatic hotfix merging from a feature branch back to its upstream branch.
    """
    runtime = get_backend()
    remote = runtime.get_remote()
    branch = runtime.current_branch()
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

    if remote:
        # Fetch the upstream branch
        git("fetch", remote, upstream_branch)

    base_rev = checkout(remote, upstream_branch, create=True)

    msg = HOTFIX_MESSAGE.format(upstream_branch=upstream_branch, message=message)
    click.echo(msg)

    try:
        git("merge", f"{branch}_temp", "-m", msg)
    except subprocess.CalledProcessError:
        click.echo("Conflicts:", err=True)
        git("diff", "--name-only", "--diff-filter=U")
        raise

    if remote:
        # this will trigger a full pipeline for upstream_branch, and potentially another auto-merge
        click.echo(f"Pushing {upstream_branch} to {remote}")
        if runtime.supports_push_options:
            push_opts = ["-o", f"ci.variable={ENVVAR_PREFIX}_AUTOTAG_ANNOTATION={msg}"]
        else:
            push_opts = []
        git("push", remote, upstream_branch, *push_opts)
    else:
        autotag.callback(msg, base_rev)
        # local mode: cleanup
        git("branch", "-D", f"{branch}_temp")


@cli.command()
def promote() -> None:
    """
    Promote changes through the branch hierarchy from beta to rc to stable.
    """
    runtime = get_backend()
    remote = runtime.get_remote()
    if CONFIG.verbose:
        click.echo(f"remote = {remote}")

    def promote_branch(branch: str, log_msg: str) -> None:
        """
        - Checkout the branch
        - Merge with the upstream branch, if it exists
        - Push, skipping hotfixes

        The branch is left checked out.
        """
        upstream_branch = get_upstream_branch(branch)
        # FIXME: raise an error by default if branch does not exist?  provide option to allow this only during bootstrapping/testing
        # if upstream_branch and upstream_branch not in all_branches:
        #     click.echo(
        #         f"upstream_branch '{upstream_branch}' is not in all_branches: [{all_branches}]",
        #         err=True,
        #     )
        #     return
        if remote:
            click.echo(f"fetching {remote}")
            git("fetch", remote, branch)

        base_rev = checkout(remote, branch, create=True)

        if upstream_branch:
            if remote:
                git("fetch", remote, upstream_branch)
            git("merge", join(remote, upstream_branch), "-m", f"{log_msg}")

        if remote:
            # Trigger test/tag jobs for these new versions, but skip auto-hotfix
            args = [
                "push",
                "--atomic",
                remote,
                branch,
            ]
            if runtime.supports_push_options:
                args.extend(
                    [
                        "-o",
                        f"ci.variable={ENVVAR_PREFIX}_SKIP_HOTFIX=true",
                        "-o",
                        f"ci.variable={ENVVAR_PREFIX}_AUTOTAG_ANNOTATION={log_msg}",
                    ]
                )
            git(*args)
            if not runtime.supports_push_options:
                if upstream_branch and base_rev != current_rev():
                    command = {
                        "annotation": log_msg,
                        "base_rev": base_rev,
                        "branch": branch,
                    }
                    click.echo(f"Trigger: {json.dumps(command)}")
            elif not remote:
                autotag.callback(log_msg, base_rev)

    # Promotion time!

    # It doesn't matter what the active branch is when promote is run: the next thing that we do
    # is check out CONFIG.stable.
    promote_branch(
        CONFIG.stable,
        f"promoting {CONFIG.rc} to {CONFIG.stable}!",
    )
    promote_branch(
        CONFIG.rc,
        f"promoting {CONFIG.beta} to {CONFIG.rc}!",
    )
    promote_branch(
        CONFIG.beta,
        "starting new beta cycle.",
    )

    # Note: we do not make a beta tag at this time. Instead, we wait for the first commit on the
    # CONFIG.beta branch to do so.
    set_promotion_marker(remote, CONFIG.beta)


if __name__ == "__main__":
    cli(
        auto_envvar_prefix=ENVVAR_PREFIX,
        max_content_width=shutil.get_terminal_size().columns,
    )
