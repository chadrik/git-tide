from __future__ import absolute_import, print_function, annotations

import os
import shlex
import subprocess
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
from typing import TYPE_CHECKING, Iterable, Mapping, cast

from .gitutils import (
    git,
    checkout_remote_branch,
    get_tags,
    branch_exists,
    join,
    current_rev,
    GitRepo,
)

if TYPE_CHECKING:
    import gitlab.v4.objects
    import commitizen.providers
    import commitizen.cmd
    import commitizen.config


TOOL_NAME = "tide"
ENVVAR_PREFIX = TOOL_NAME.upper()
PROMOTION_BASE_MSG = "promotion base"
HERE = os.path.dirname(__file__)
GITLAB_REMOTE = "origin"

# FIXME: add these to config file
HOTFIX_MESSAGE = "auto-hotfix into {upstream_branch}: {message}"
PROMOTION_CYCLE_START_MESSAGE = "starting new {release_id} cycle."
PROMOTION_MESSAGE = "promoting {upstream_branch} to {branch}!"

cache = lru_cache(maxsize=None)


def _patched_run(
    cmd: str, env: Mapping[str, str] | None = None
) -> commitizen.cmd.Command:
    import commitizen.cmd

    process = subprocess.Popen(
        shlex.split(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE,
        env=env,
    )
    stdout, stderr = process.communicate()
    return_code = process.returncode
    return commitizen.cmd.Command(
        commitizen.cmd._try_decode(stdout),
        commitizen.cmd._try_decode(stderr),
        stdout,
        stderr,
        return_code,
    )


def _patch_cz_run() -> None:
    """
    commitizen.cmd.run uses shell=True, which can introduce inconsistency
    based on user profiles, etc.
    """
    if os.environ.get("TIDE_PATCH_CZ_RUN", "0").lower() not in ["1", "true"]:
        return

    import commitizen.cmd

    if commitizen.cmd.run is _patched_run:
        return
    commitizen.cmd.run = _patched_run


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

    try:
        config.tag_format = settings["tag_format"]
    except KeyError:
        pass

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


@dataclass
class Config:
    # mapping from id to branch name
    stable: str = "master"
    rc: str | None = None
    beta: str | None = None
    alpha: str | None = None

    # branches in order from most-experimental to stable
    branches: list[str] = field(default_factory=list)
    # branch name to pre-release name (alpha, beta, rc). None for stable.
    branch_to_release_id: dict[str, ReleaseID] = field(default_factory=dict)
    verbose: bool = False
    tag_format: str = "$version"

    def get_upstream_branch(self, branch: str) -> str | None:
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
            index = self.branches.index(branch)
        except ValueError:
            raise click.ClickException(f"Invalid branch: {branch}")

        if index > 0:
            return self.branches[index - 1]
        else:
            return None

    def most_experimental_branch(self) -> str | None:
        """
        Return the most experimental branch.

        This branch corresponds to the earliest pre-release specified by the config.
        """
        if self.branches[0] == self.stable:
            return None
        else:
            return self.branches[0]


class Runtime:
    """
    Interact with a git repo that is local to the current process
    """

    def __init__(self, config: Config):
        self.config = config

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

    def __init__(self, config: Config):
        self.config = config

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
        git("fetch", remote_name, quiet=self.config.verbose)

        if self.config.verbose:
            git("branch", "-la")
            git("remote", "-v")

        # setup branches.
        # loop from stable to pre-release branches, bc we set all release_id branches to
        # the location of stable
        for branch in reversed(self.config.branches):
            if branch_exists(branch):
                if branch != self.config.stable:
                    click.echo(
                        f"{branch} already exists. This can potentially cause problems",
                        err=True,
                    )
            else:
                git("branch", "-f", branch, self.config.stable)

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
        git("remote", "set-url", GITLAB_REMOTE, f"https://oauth2:{access_token}@{url}")

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
                click.echo("Created ACCESS_TOKEN project variable", err=True)
            else:
                click.echo(
                    "ACCESS_TOKEN project variable already exists. Skipping", err=True
                )
        else:
            # FIXME: validate that ACCESS_TOKEN has been set at the project or group level
            pass

        for branch in self.config.branches:
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
        click.echo("Setup protected branches", err=True)

        default_branch = self.config.most_experimental_branch() or self.config.stable
        gl.projects.update(project.id, {"default_branch": default_branch})

        if not self._find_promote_job(project):
            # this must happen after the branch has been created in the remote and initial
            # commit pushed
            schedule = project.pipelineschedules.create(
                {
                    "ref": self.config.stable,
                    "description": self.PROMOTION_SCHEDULED_JOB_NAME,
                    "cron": "6 6 * * 4",
                    "active": False,
                }
            )
            schedule.variables.create({"key": "SCHEDULED_JOB_NAME", "value": "promote"})
            click.echo(
                f"Created '{self.PROMOTION_SCHEDULED_JOB_NAME}' scheduled job, in non-active state",
                err=True,
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
            click.echo(f"Writing local output to {json_file}", err=True)
            if os.path.exists(json_file):
                os.remove(json_file)

            with open(json_file, "w") as f:
                json.dump(variables, f)

        git("push", *args)


def cz(*args: str, folder: str | Path | None = None) -> str:
    """
    Run commitizen in a subprocess.
    """
    output = subprocess.check_output(
        ["cz"] + list(args),
        text=True,
        cwd=folder,
    )
    return output.strip()


def is_pending_bump(
    config: Config,
    provider: commitizen.providers.ScmProvider,
    branch: str,
    remote: str | None = None,
    add_missing_promote_marker: bool = False,
) -> bool:
    """
    Return whether the given branch and folder combination are awaiting a minor bump.

    Args:
        remote: The remote repository name
        branch: one of the registered gitflow branches
        add_missing_promote_marker: if this is called prior to the first promotion
          of branches, setting this to True will cause a promotion marker to
          be set so that subsequent calls will not cause a minor version bump.

    Returns:
        whether it is pending or not
    """
    if branch != config.most_experimental_branch():
        return False

    promotion_rev = get_promotion_marker(remote)
    if promotion_rev is None:
        if config.verbose:
            click.echo("No promote marker found", err=True)
        if add_missing_promote_marker:
            if remote is None:
                raise ValueError(
                    "Must provide remote when setting add_missing_promote_marker=True"
                )
            set_promotion_marker(remote, current_rev())
        return True
    else:
        if config.verbose:
            click.echo(f"Found promotion base rev: {promotion_rev}", err=True)

    matcher = provider._tag_format_matcher()
    # List any tags for this project folder between this branch and the promotion note
    all_tags = get_tags(end_rev=promotion_rev)
    tags = [t for t in all_tags if matcher(t)]
    return not tags


def get_promotion_marker(remote: str | None = None) -> str | None:
    """
    Get the hash for the most recent promotion commit.

    Args:
        remote: The remote repository name
    """
    git("fetch", remote if remote else "--all", "refs/notes/*:refs/notes/*", quiet=True)
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

    If it is true for a given branch, then get_next_tag() will return a minor increment.
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


def _get_cz_config(tag_format: str, project_name: str) -> commitizen.config.BaseConfig:
    from commitizen.config.base_config import BaseConfig
    from commitizen.defaults import Settings

    cz_config = BaseConfig()
    cz_config.update(
        Settings(
            name="cz_conventional_commits",
            tag_format=tag_format.replace("$project", project_name),
            version_scheme="pep440",
            version_provider="scm",
            major_version_zero=False,
        )
    )
    return cz_config


def get_current_version(config: Config, project_name: str, as_tag: bool = False) -> str:
    """
    Return the current version.

    Args:
        project_name: The name of the project, used to look up the commitizen
            configuration, and find matching tags
        as_tag: Whether to format the version based on tool.tide.tag_format

    Returns:
        The current version or tag
    """
    from commitizen.providers import ScmProvider
    from commitizen.version_schemes import get_version_scheme
    from commitizen import bump

    _patch_cz_run()

    cz_config = _get_cz_config(config.tag_format, project_name)
    provider = ScmProvider(cz_config)
    scheme = get_version_scheme(cz_config)
    current_version = provider.get_version()

    tag_version = bump.normalize_tag(
        current_version,
        tag_format=cz_config.settings["tag_format"] if as_tag else "$version",
        scheme=scheme,
    )

    return tag_version


def get_next_version(
    config: Config,
    branch: str,
    project_name: str,
    remote: str | None = None,
    as_tag: bool = False,
    dry_run: bool = True,
) -> str | None:
    """
    Return the next version for a given branch based on the latest changes.

    Args:
        remote: The remote repository name
        branch: The name of the branch for which to generate the tag.
        project_name: The name of the project, used to look up the commitizen
            configuration, and find matching tags
        as_tag: Whether to format the version based on tool.tide.tag_format
        dry_run: if False, simply return the version or tag.  If True,
            also add missing promote markers, and return None if a branch has
            not yet received its first seed promotion.
    Returns:
        The next version or tag to be created
    """
    from commitizen.providers import ScmProvider
    from commitizen.version_schemes import get_version_scheme, Increment
    from commitizen import bump

    _patch_cz_run()

    try:
        release_id = config.branch_to_release_id[branch]
    except KeyError:
        raise click.ClickException(
            f"{branch} is not a valid release branch.  "
            f"Must be one of {', '.join(config.branches)}"
        )

    cz_config = _get_cz_config(config.tag_format, project_name)

    provider = ScmProvider(cz_config)
    scheme = get_version_scheme(cz_config)
    current_version = scheme(provider.get_version())

    if release_id != ReleaseID.stable:
        prerelease = release_id.value
        if (
            not dry_run
            and not current_version.prerelease
            and branch != config.most_experimental_branch()
        ):
            # tag can be None if a branch has not yet received its first seed promotion.
            # for example: prior to beta being promoted to rc, there will not be any
            # rc tags, and we don't want to generate one until the first rc release
            # comes into existence.
            return None
    else:
        prerelease = None

    # Find the closest promotion note to the current branch
    pending_bump = is_pending_bump(
        config,
        provider,
        branch,
        remote,
        add_missing_promote_marker=not dry_run,
    )

    # Only apply minor increment the most experimental branch
    if pending_bump:
        increment: Increment = "MINOR"
        exact_increment = True
    else:
        increment = "PATCH"
        exact_increment = False

    new_version = current_version.bump(
        increment,
        prerelease=prerelease,
        exact_increment=exact_increment,
    )

    return bump.normalize_tag(
        new_version,
        tag_format=cz_config.settings["tag_format"] if as_tag else "$version",
        scheme=scheme,
    )


def get_project_name(pyproject: Path) -> str | None:
    """
    Return the name of the project at the given path.

    A project is a folder with a pyproject.toml file with a `[project].name`
    value or a `[tool.tide].project` value.

    A project can opt-out by setting `[tool.tide].managed_project = false`
    """
    if not pyproject.suffix == ".toml" and pyproject.is_dir():
        pyproject = pyproject.joinpath("pyproject.toml")

    name = None
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)
        try:
            name = data["project"]["name"]
        except KeyError:
            try:
                name = data["tool"][TOOL_NAME]["project"]
            except KeyError:
                return None

        try:
            if not data["tool"][TOOL_NAME]["managed_project"]:
                return None
        except KeyError:
            pass
    return name


def get_projects() -> list[tuple[Path, str]]:
    """Get the list of projects within the repo.

    A project is a folder with a pyproject.toml file with a `[project].name`
    value or a `[tool.tide].project` value.

    A project can opt-out by setting `[tool.tide].managed_project = false`
    """
    results = []
    repo = GitRepo(".")
    for path in repo.file_matches(include=("**/pyproject.toml",)):
        pyproject = Path(path)
        project_name = get_project_name(pyproject)
        if project_name is not None:
            results.append((pyproject.parent, project_name))
    return sorted(results)


def get_modified_projects(
    base_rev: str, verbose: bool = False
) -> list[tuple[Path, str]]:
    """Get the list of projects with changes files.

    A project is defined as a folder with a pyproject.toml file with a `tool.tide` section.

    Args:
        base_rev: The Git revision to compare against when identifying changed files
    """
    # Compare the current commit with the branch you want to merge with:
    # FIXME: do not included deleted files
    output = git("diff-tree", "--name-only", "-r", base_rev, "HEAD", capture=True)
    all_files = output.splitlines()
    if verbose:
        if all_files:
            click.echo(f"Modified files between {base_rev} and HEAD:")
            for path in all_files:
                click.echo(f" {path}")
        else:
            click.echo(f"No modified files between {base_rev} and HEAD", err=True)
    return get_projects_from_files([Path(x) for x in all_files])


def group_files_by_projects(
    files: Iterable[Path], project_dirs: Iterable[Path] | None = None
) -> dict[Path, list[Path]]:
    """Given an iterable of files and project directories, return a mapping from
    project directories to files made relative to those directories.
    """
    from collections import defaultdict

    if project_dirs is None:
        project_dirs = dict(get_projects()).keys()
    # find the deepest project that the file belongs to
    project_dirs = list(reversed(sorted(project_dirs)))

    results: dict[Path, list[Path]] = defaultdict(list)
    for changed_file in files:
        parents = changed_file.parents
        for project_dir in project_dirs:
            if project_dir in parents:
                results[project_dir].append(changed_file.relative_to(project_dir))
                break
    return dict(results)


def get_projects_from_files(files: Iterable[Path]) -> list[tuple[Path, str]]:
    """Given an iterable of files, return a list of (project path, package name) tuples."""
    projects: list[tuple[Path, str]] = get_projects()
    project_map = dict(projects)
    results = group_files_by_projects(files, project_dirs=project_map)
    return [(project_dir, project_map[project_dir]) for project_dir in sorted(results)]


def promote(config: Config, backend: Backend, runtime: Runtime) -> None:
    """
    Promote changes through the branch hierarchy.

    e.g. from alpha -> beta -> rc -> stable.
    """
    remote = runtime.get_remote()
    if config.verbose:
        click.echo(f"remote = {remote}")

    local_output = []

    def promote_branch(branch: str, log_msg_template: str) -> None:
        """
        - Checkout the branch
        - Merge with the upstream branch, if it exists
        - Push, skipping hotfixes

        The branch is left checked out.
        """
        upstream_branch = config.get_upstream_branch(branch)
        release_id = config.branch_to_release_id[branch]
        log_msg = log_msg_template.format(
            branch=branch, upstream_branch=upstream_branch, release_id=release_id.value
        )

        click.echo(f"Fetching {remote}/{branch}")
        git("fetch", remote, branch)

        base_rev = checkout_remote_branch(remote, branch)

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
    for branch in reversed(config.branches):
        # It doesn't matter what the active branch is when promote is run: the next thing
        # that we do is checkout CONFIG.stable.
        if branch == config.most_experimental_branch():
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
    experimental_branch = config.most_experimental_branch()
    if experimental_branch:
        set_promotion_marker(remote, experimental_branch)
