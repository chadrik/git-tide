"""Core tide logic: configuration, runtimes, backends, and branching models."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time

import click

try:
    import tomli as tomllib  # noqa: F401
except ImportError:
    import tomllib  # type: ignore[no-redef]
from abc import abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Mapping, NamedTuple, cast
from urllib.parse import urlparse, urlunparse

from .gitutils import (
    GitRepo,
    branch_exists,
    git,
    join,
)

if TYPE_CHECKING:
    import commitizen.cmd
    import commitizen.config
    import commitizen.providers
    import commitizen.version_schemes
    import gitlab.v4.objects


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


def _patched_run(cmd: str, env: Mapping[str, str] | None = None) -> commitizen.cmd.Command:
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
    """Replace `commitizen.cmd.run` with a version that runs no shell.

    `commitizen.cmd.run` sets shell=True. The shell reads the user's profile, so the
    same command can behave differently for different users.
    """
    if os.environ.get("TIDE_PATCH_CZ_RUN", "0").lower() not in ["1", "true"]:
        return

    import commitizen.cmd

    if commitizen.cmd.run is _patched_run:
        return
    commitizen.cmd.run = _patched_run


class ReleaseID(str, Enum):
    """A semver pre-release phase, plus 'stable' for a version that is not a pre-release."""

    alpha = "alpha"
    beta = "beta"
    rc = "rc"
    stable = "stable"

    def prerelease_suffix(self) -> str | None:
        return {
            "alpha": "a",
            "beta": "b",
            "rc": "rc",
            "stable": None,
        }[self.name]


class BranchingMode(str, Enum):
    """The set of rules tide follows for how versions advance and branches move.

    A repository selects one mode. A project cannot select a mode of its own.
    """

    gitflow = "gitflow"
    semantic_commit = "semantic_commit"


def is_url(s: str) -> bool:
    """Return whether the string looks like a URL."""
    return "://" in s


def load_config(path: str | None = None, verbose: bool = False) -> Config:
    """Load the tide configuration from a pyproject.toml file.

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
        raise click.ClickException(f"'tool.{TOOL_NAME}.branches' section missing: {path}")

    config = Config(verbose=verbose)

    mode = settings.get("branching_mode", BranchingMode.gitflow.value)
    try:
        config.branching_mode = BranchingMode(mode)
    except ValueError:
        valid = ", ".join(repr(m.value) for m in BranchingMode)
        raise click.ClickException(
            f"Invalid 'tool.{TOOL_NAME}.branching_mode' {mode!r}: must be one of {valid}: {path}"
        )

    for key in ("tag_format", "release_branch_format"):
        try:
            setattr(config, key, settings[key])
        except KeyError:
            pass

    # Record each configured branch, from most experimental to stable.
    for release_id in ReleaseID:
        branch_name = branches.get(release_id.value)
        if branch_name is None:
            continue

        config.branches.append(branch_name)
        config.branch_to_release_id[branch_name] = release_id
        setattr(config, release_id.value, branch_name)

    if config.branching_mode is BranchingMode.semantic_commit:
        _validate_semantic_commit_config(config, branches, path)
    return config


def _validate_semantic_commit_config(config: Config, branches: dict, path: str) -> None:
    """Reject configuration that has no meaning in semantic_commit mode.

    Args:
        config: the partially built configuration
        branches: the raw `tool.tide.branches` table
        path: path to the config file, used in error messages

    Raises:
        ClickException: if the configuration names a pre-release branch, or if
            `release_branch_format` cannot be parsed back into a version line.
    """
    prerelease = [
        release_id.value
        for release_id in ReleaseID
        if release_id is not ReleaseID.stable and branches.get(release_id.value)
    ]
    if prerelease:
        raise click.ClickException(
            f"'tool.{TOOL_NAME}.branches.{prerelease[0]}' is not supported in "
            f"{BranchingMode.semantic_commit.value!r} branching mode: this mode is "
            f"trunk-based, so only 'branches.stable' (the trunk) may be set: {path}"
        )

    for placeholder in ("$major", "$minor"):
        if placeholder not in config.release_branch_format:
            raise click.ClickException(
                f"'tool.{TOOL_NAME}.release_branch_format' must contain {placeholder!r} "
                f"so that release branches can be parsed back into a version line, "
                f"got {config.release_branch_format!r}: {path}"
            )


@dataclass
class Config:
    """The tide configuration, as loaded from `tool.tide` in pyproject.toml."""

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
    branching_mode: BranchingMode = BranchingMode.gitflow
    # tide manages monorepos by default. Both formats contain $project, so that two
    # projects never share a tag namespace or a branch namespace.
    tag_format: str = "$project/$version"
    release_branch_format: str = "$project/release-$major.$minor"

    def get_upstream_branch(self, branch: str) -> str | None:
        """Return the upstream branch of `branch`.

        Args:
            branch: The name of the branch to find the upstream branch of.

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
        """Return the most experimental branch.

        This branch holds the earliest pre-release phase in the configuration.
        """
        if self.branches[0] == self.stable:
            return None
        else:
            return self.branches[0]


class Runtime:
    """Interact with a git repo that is local to the current process."""

    def __init__(self, config: Config):
        self.config = config

    @abstractmethod
    def current_branch(self) -> str:
        """Return the name of the current git branch.

        Returns:
            The name of the current branch.

        Raises:
            RuntimeError: if tide cannot determine the current branch.
        """

    @abstractmethod
    def get_base_rev(self) -> str:
        """Return the git revision of the repository before the current pipeline.

        This revision precedes the changes that started the current pipeline. tide
        compares it against HEAD. The files that changed between the two revisions
        decide which project tags to increment.
        """

    @abstractmethod
    def get_remote(self) -> str:
        """Configure a git remote and return its name.

        An implementation reads the environment variables that its CI system sets. It
        then sets the git user credentials, adds the access token to the repository
        URL, and registers the remote with the local git configuration.

        Returns:
            The name of the configured remote
        """


class Backend:
    """Interact with a remote git backend."""

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
        """Configure the local repository.

        Args:
            remote_name: name of the git remote, used to query the url
        """
        git("fetch", remote_name, quiet=self.config.verbose)

        if self.config.verbose:
            git("branch", "-la")
            git("remote", "-v")

        # Create the branches. Loop from stable to the pre-release branches, because
        # every branch starts at the position of stable.
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
        self,
        remote_url: str,
        access_token: str,
        save_token: bool,
        model: BranchingModel,
    ) -> None:
        """Configure the remote repository.

        Args:
            remote_url: URL of the git remote
            access_token: token used to authenticate changes to the remote.
            save_token: whether to save `access_token` into the remote.
            model: the branching model this repository follows. It decides which
                branches the remote protects and whether a promotion schedule exists.
        """


class BranchingModel:
    """The rules a repository follows for how versions advance and branches move.

    A repository selects one branching mode. The model for that mode makes every
    decision that differs between modes. A model omits the operations that its mode
    does not support, so no caller has to test the mode.
    """

    def __init__(self, config: Config):
        self.config = config

    @abstractmethod
    def release_id(self, branch: str) -> ReleaseID:
        """Return the ReleaseID of versions minted on `branch`.

        Args:
            branch: a branch that tide mints versions on.

        Raises:
            ClickException: if versions are never minted on `branch`.
        """

    @abstractmethod
    def next_version(
        self,
        branch: str,
        project_name: str,
        remote: str | None = None,
        as_tag: bool = False,
        dry_run: bool = True,
        fetch: bool = True,
    ) -> str | None:
        """Return the next version for `project_name` on `branch`.

        Args:
            branch: The name of the branch for which to generate the tag.
            project_name: The name of the project, used to find the commitizen
                configuration and the matching tags.
            remote: The remote repository name.
            as_tag: Whether to format the version based on tool.tide.tag_format.
            dry_run: if True, make no changes to the repository.
            fetch: whether to fetch from the remote.

        Returns:
            The next version or tag, or None if no version should be minted.
        """

    @abstractmethod
    def autotag(
        self,
        runtime: Runtime,
        backend: Backend,
        annotation: str,
        base_rev: str | None = None,
        projects: tuple[str, ...] = (),
        dry_run: bool = False,
        fetch: bool = True,
    ) -> None:
        """Tag the current branch with a new version for each modified project."""

    def validate(
        self,
        target_branch: str,
        commits: list[Commit] | None = None,
        remote: str | None = None,
    ) -> None:
        """Check the commits leading to HEAD against the rules of this mode.

        Args:
            target_branch: the branch the current changes are destined for.
            commits: the commits to check, or None to let the model choose.
            remote: the git remote, used to resolve branches with no local ref.

        Raises:
            ValidationError: if a rule is broken.
        """

    @abstractmethod
    def protected_branch_patterns(self) -> list[str]:
        """Return the branch names or wildcards that the remote must protect.

        Protection does two things. It sets `CI_COMMIT_REF_PROTECTED`, which the tide
        jobs test before they run. It also stops a developer from pushing to a branch
        that tide maintains.

        A mode that creates branches after `init` runs must protect them with a
        wildcard, because their names do not exist yet.
        """

    @abstractmethod
    def uses_promotion_schedule(self) -> bool:
        """Whether the remote needs a scheduled job to drive `tide promote`."""

    def hotfix(self, runtime: Runtime, backend: Backend) -> None:
        """Merge hotfixes from a branch back to its upstream branch."""
        raise self._unsupported("hotfix")

    def promote(self, runtime: Runtime, backend: Backend) -> None:
        """Promote changes through the branch hierarchy."""
        raise self._unsupported("promote")

    def _unsupported(self, command: str) -> click.ClickException:
        return click.ClickException(
            f"'{TOOL_NAME} {command}' is not supported in "
            f"{self.config.branching_mode.value!r} branching mode"
        )

    def _create_tag(self, tag: str, annotation: str, branch: str, dry_run: bool) -> None:
        """Create an annotated tag on HEAD."""
        # NOTE: git records a time with a resolution of one second, the same as a unix
        # timestamp. The delay gives each tag a distinct time, so that tags always sort
        # in the order that tide creates them.
        # https://stackoverflow.com/questions/28237043/what-is-the-resolution-of-gits-commit-date-or-author-date-timestamps
        time.sleep(1.1)
        click.echo(
            f"Creating new tag '{tag}' on branch {branch}" + (" (dry_run=True)" if dry_run else "")
        )
        if not dry_run:
            git("tag", "-a", tag, "-m", annotation)


class GitlabRuntime(Runtime):
    """Interact with a git repo from within a Gitlab CI job."""

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
    """Gitlab-specific behavior."""

    PROMOTION_SCHEDULED_JOB_NAME = "Promote Gitflow Branches"

    @cache
    def _conn(self, base_url: str, access_token: str) -> gitlab.Gitlab:
        """Return a cached gitlab connection object."""
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
        """Return the scheduled job that starts promotion, or None if it does not exist."""
        schedules = project.pipelineschedules.list(get_all=True)
        for schedule in schedules:
            if schedule.description == self.PROMOTION_SCHEDULED_JOB_NAME:
                return schedule
        return None

    def init_remote_repo(
        self,
        remote_url: str,
        access_token: str,
        save_token: bool,
        model: BranchingModel,
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
        # A remote url may carry credentials, e.g. 'https://oauth2:$TOKEN@gitlab.com/...',
        # which is the form that Gitlab CI puts in CI_REPOSITORY_URL. These credentials
        # authenticate git, not the REST API, which uses `access_token` instead.
        # Credentials left in the base url add a second auth header to every API call.
        netloc = url.hostname or ""
        if url.port:
            netloc = f"{netloc}:{url.port}"
        base_url = urlunparse(url._replace(netloc=netloc, path=""))
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
                click.echo("ACCESS_TOKEN project variable already exists. Skipping", err=True)
        else:
            # FIXME: validate that ACCESS_TOKEN has been set at the project or group level
            pass

        for branch in model.protected_branch_patterns():
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
        click.echo("Configured the protected branches", err=True)

        default_branch = self.config.most_experimental_branch() or self.config.stable
        gl.projects.update(project.id, {"default_branch": default_branch})

        if model.uses_promotion_schedule() and not self._find_promote_job(project):
            # The remote must already have the branch and the first commit.
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
    """Interact with a local git repo, outside of CI."""

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
        # FIXME: make this configurable. It is a user preference, so it does not
        #  belong in pyproject.toml.
        return "origin"


class TestGitlabRuntime(GitlabRuntime):
    """`GitlabRuntime` variant used by the test suite."""

    @cache
    def _setup_remote(self, url: str) -> None:
        # Overridden to keep the oauth token out of the remote url.
        git("config", "user.email", os.environ["GITLAB_USER_EMAIL"])
        git("config", "user.name", os.environ["GITLAB_USER_NAME"])


class TestGitlabBackend(GitlabBackend):
    """`GitlabBackend` variant used by the test suite."""

    @cache
    def _conn(self, base_url: str, access_token: str) -> gitlab.Gitlab:
        # Overridden to return a mock Gitlab connection object.
        import unittest.mock

        return cast("gitlab.Gitlab", unittest.mock.MagicMock())

    def push(
        self, *args: str, variables: dict[str, str] | None = None, skip_ci: bool = False
    ) -> None:
        # Overridden to write the variables to a json file. A local git repo does not
        # support push options.
        if variables:
            json_file = os.path.join(os.environ["CI_REPOSITORY_URL"], "push-opts.json")
            click.echo(f"Writing local output to {json_file}", err=True)
            if os.path.exists(json_file):
                os.remove(json_file)

            with open(json_file, "w") as f:
                json.dump(variables, f)

        git("push", *args)


class CommitizenContext(NamedTuple):
    """Context for commitizen operations."""

    config: commitizen.config.BaseConfig
    provider: commitizen.providers.ScmProvider
    scheme: commitizen.version_schemes.VersionProtocol


def _init_commitizen_context(config: Config, project_name: str) -> CommitizenContext:
    """Initialize commitizen configuration, provider, and scheme.

    Args:
        config: The tide configuration
        project_name: The name of the project

    Returns:
        A CommitizenContext that holds the config, the provider, and the scheme
    """
    from commitizen.config.base_config import BaseConfig
    from commitizen.defaults import Settings
    from commitizen.providers import ScmProvider
    from commitizen.version_schemes import get_version_scheme

    _patch_cz_run()

    cz_config = BaseConfig()
    cz_config.update(
        Settings(
            name="cz_conventional_commits",
            tag_format=config.tag_format.replace("$project", project_name),
            version_scheme="pep440",
            version_provider="scm",
            major_version_zero=False,
        )
    )
    provider = ScmProvider(cz_config)
    scheme = get_version_scheme(cz_config)
    return CommitizenContext(cz_config, provider, scheme)


def get_current_version(config: Config, project_name: str, as_tag: bool = False) -> str:
    """Return the current version.

    Args:
        project_name: The name of the project, used to find the commitizen
            configuration and the matching tags
        as_tag: Whether to format the version based on tool.tide.tag_format

    Returns:
        The current version or tag
    """
    from commitizen import bump

    cz_ctx = _init_commitizen_context(config, project_name)
    current_version = cz_ctx.provider.get_version()

    tag_version = bump.normalize_tag(
        current_version,
        tag_format=cz_ctx.config.settings["tag_format"] if as_tag else "$version",
        scheme=cz_ctx.scheme,
    )

    return tag_version


def get_version_at_ref(
    config: Config,
    project_name: str,
    ref: str,
    as_tag: bool = False,
    release_id: ReleaseID | None = None,
) -> str:
    """Return the version at a specific git ref by looking up existing tags.

    Args:
        config: The tide configuration
        project_name: The name of the project, used to find the matching tags
        ref: The git ref (commit SHA, branch name, tag, etc.) to query
        as_tag: Whether to format the version based on tool.tide.tag_format
        release_id: Optional release ID to filter tags by release phase

    Returns:
        The version or tag at the specified ref

    Raises:
        click.ClickException: If a matching tag cannot be found at the given ref.
    """
    try:
        tag_list = git("tag", "--points-at", ref, capture=True, quiet=True).splitlines()
    except subprocess.CalledProcessError:
        raise click.ClickException(f"Invalid git ref {ref!r}")
    if not tag_list:
        raise click.ClickException(f"No tags found at git ref {ref!r}")

    cz_ctx = _init_commitizen_context(config, project_name)
    matcher = cz_ctx.provider._tag_format_matcher()

    version_tag_map = {version: tag for tag in tag_list if (version := matcher(tag))}
    if not version_tag_map:
        raise click.ClickException(
            f"No version tags found for project {project_name!r} at git ref {ref!r}"
        )

    if release_id is not None:
        phase_marker = release_id.prerelease_suffix()
        if phase_marker is None:
            phase_versions = [v for v in version_tag_map if v.pre is None]
        else:
            phase_versions = [
                v for v in version_tag_map if v.pre is not None and v.pre[0] == phase_marker
            ]

        # `version_tag_map` holds only the tags of this project, so exactly one version
        # must match the given release phase.
        if len(phase_versions) != 1:
            if phase_versions:
                adjective = "Multiple"
                suffix = f": {', '.join(version_tag_map[v] for v in phase_versions)}"
            else:
                adjective = "No"
                suffix = ""
            raise click.ClickException(
                f"{adjective} tags found for project {project_name!r} and release phase "
                f"{release_id.value!r} at git ref {ref!r}{suffix}"
            )

        matching_version = phase_versions[0]
    else:
        # Without a release phase, take the highest version.
        matching_version = sorted(version_tag_map)[-1]

    if as_tag:
        return version_tag_map[matching_version]
    else:
        return str(matching_version)


def get_project_name(pyproject: Path) -> str | None:
    """Return the name of the project at the given path.

    A project is a folder with a pyproject.toml file that has a `[project].name`
    value or a `[tool.tide].project` value.

    A project can exclude itself by setting `[tool.tide].managed_project = false`
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


@lru_cache(maxsize=None)
def get_projects() -> tuple[tuple[Path, str], ...]:
    """Return every project within the repo.

    A project is a folder with a pyproject.toml file that has a `[project].name`
    value or a `[tool.tide].project` value.

    A project can exclude itself by setting `[tool.tide].managed_project = false`
    """
    results = []
    repo = GitRepo(".")
    for path in repo.file_matches(include=("**/pyproject.toml",)):
        pyproject = Path(path)
        if pyproject.parent != Path("."):
            _assert_no_branching_mode(pyproject)
        project_name = get_project_name(pyproject)
        if project_name is not None:
            results.append((pyproject.parent, project_name))
    return tuple(sorted(results))


def _assert_no_branching_mode(pyproject: Path) -> None:
    """Reject `branching_mode` set anywhere but the root config.

    A repository has one branching mode. If a project could override the mode, two
    projects could disagree about the meaning of the branches that they share.
    """
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)
    if "branching_mode" in data.get("tool", {}).get(TOOL_NAME, {}):
        raise click.ClickException(
            f"'tool.{TOOL_NAME}.branching_mode' may only be set in the root "
            f"pyproject.toml, not in a project: {pyproject}"
        )


def get_modified_projects(base_rev: str, verbose: bool = False) -> list[tuple[Path, str]]:
    """Return every project that has a changed file.

    A project is a folder with a pyproject.toml file that has a `[project].name` value
    or a `[tool.tide].project` value.

    Args:
        base_rev: The Git revision to compare against when identifying changed files
    """
    # FIXME: do not include deleted files
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
    """Return a mapping of project directory to the files that belong to it.

    Each returned file path is relative to its project directory.
    """
    from collections import defaultdict

    if project_dirs is None:
        project_dirs = dict(get_projects()).keys()
    # Sort the deepest directory first, so that a file joins the most specific project.
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
    """Return a (project path, project name) tuple for each project that owns one of `files`."""
    project_map = dict(get_projects())
    results = group_files_by_projects(files, project_dirs=project_map)
    return [(project_dir, project_map[project_dir]) for project_dir in sorted(results)]


class Commit(NamedTuple):
    """A commit, its parents, and the files it touched.

    A merge commit reports no files, so tide never attributes one to a project.
    """

    rev: str
    parents: list[str]
    message: str
    files: list[Path]

    @property
    def is_merge(self) -> bool:
        """Whether this commit merges another branch."""
        return len(self.parents) > 1


def reachable_tags(rev: str = "HEAD") -> list[str]:
    """Return every tag reachable from `rev`."""
    return [
        tag
        for tag in git("tag", "--merged", rev, capture=True, quiet=True).splitlines()
        if tag.strip()
    ]


def project_versions(
    config: Config, project_name: str, rev: str = "HEAD"
) -> dict[commitizen.version_schemes.VersionProtocol, str]:
    """Return a mapping of version to tag for every tag of `project_name` reachable from `rev`."""
    cz_ctx = _init_commitizen_context(config, project_name)
    matcher = cz_ctx.provider._tag_format_matcher()
    return {version: tag for tag in reachable_tags(rev) if (version := matcher(tag))}


def get_commits(start: str | None, end: str = "HEAD") -> list[Commit]:
    """Return the commits in `start..end`, newest first, with the files each touched.

    Args:
        start: exclusive lower bound, or None to walk the entire history.
        end: inclusive upper bound.
    """
    rev_range = f"{start}..{end}" if start else end
    try:
        output = git(
            "log",
            "--format=%x1e%H%x1f%P%x1f%B%x1f",
            "--name-only",
            rev_range,
            capture=True,
            quiet=True,
        )
    except subprocess.CalledProcessError:
        return []

    commits = []
    for record in output.split("\x1e"):
        if not record.strip():
            continue
        parts = record.split("\x1f", 3)
        if len(parts) < 3:
            continue
        rev, parents, message = parts[:3]
        # A merge commit lists no files, so its record ends on the trailing separator.
        # Python counts \x1f as whitespace. The strip() in git() therefore removes the
        # separator from the last record, and the files segment is absent, not empty.
        files = parts[3] if len(parts) == 4 else ""
        commits.append(
            Commit(
                rev=rev.strip(),
                parents=parents.split(),
                message=message.strip(),
                files=[Path(line) for line in files.splitlines() if line.strip()],
            )
        )
    return commits


def resolve_ref(branch: str, remote: str | None = None) -> str | None:
    """Return a ref for `branch`, preferring the remote-tracking ref.

    A CI runner usually checks out a detached HEAD and has no local branches. The
    remote-tracking ref then holds the true state of the branch.
    """
    candidates = [join(remote, branch)] if remote else []
    candidates.append(branch)
    for candidate in candidates:
        if branch_exists(candidate):
            return candidate
    return None


def fetch_ref(branch: str, remote: str | None = None) -> str | None:
    """Return a ref for `branch`, fetching it from `remote` if it is not present.

    A CI runner usually holds only the revision that started the pipeline. tide must
    fetch any other branch before it can resolve the branch.

    Returns:
        A usable ref, or None if `branch` does not exist locally or on the remote.
    """
    ref = resolve_ref(branch, remote)
    if ref is not None or remote is None:
        return ref
    try:
        git(
            "fetch",
            remote,
            f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}",
            quiet=True,
        )
    except subprocess.CalledProcessError:
        return None
    return resolve_ref(branch, remote)
