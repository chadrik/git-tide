from __future__ import absolute_import, print_function, annotations

import base64
import os
import pathlib
import re
import subprocess
import time
import json

import click
import requests

try:
    import tomli as tomlllib  # noqa: F401
except ImportError:
    import tomllib
from dataclasses import dataclass, field
from functools import cache


ENVVAR_PREFIX = "MONOFLOW"
PROMOTION_PENDING_VAR = "MONOFLOW_MINOR_BUMP_PENDING_{}_{}"
HOTFIX_MESSAGE = "auto-hotfix into {upstream_branch}: {message}"
HERE = os.path.dirname(__file__)
CONFIG: Config

LOCAL_REMOTE = "local_origin"
GITLAB_REMOTE = "gitlab_origin"


def load_config(path=None, verbose: bool = False) -> Config:
    """
    Load and return the GitFlow configuration from the pyproject.toml file.

    Returns:
        A dictionary containing the configuration of GitFlow, including a first_tag for the repository
        and names and prereleases for each branch.
    """
    if path is None:
        path = os.path.join(os.getcwd(), "pyproject.toml")
    with open(path, "rb") as f:
        config = tomllib.load(f)
    settings = config.get("tool", {}).get("gitflow", {})

    config = Config(verbose=verbose)
    # Loop through branches once and extract all needed information
    for branch in settings.get("branches", []):
        branch_name = branch["name"]
        prerelease = branch["prerelease"]

        # Append branch name to CONFIG.branches list
        config.branches.append(branch_name)

        # Update CONFIG.branch_to_prerelease dictionary
        config.branch_to_prerelease[branch_name] = (
            None if prerelease == "None" else prerelease
        )

        # Determine if the branch is CONFIG.stable, CONFIG.rc, or CONFIG.beta
        if prerelease == "None":
            config.stable = branch_name
        elif prerelease == "rc":
            config.rc = branch_name
        elif prerelease == "beta":
            config.beta = branch_name
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
    rc: str = "staging"
    beta: str = "develop"
    branches: list[str] = field(default_factory=list)
    branch_to_prerelease: dict[str, str] = field(default_factory=dict)
    verbose: bool = False


class Gitlab:
    """Class to quarantine Gitlab-specific behavior

    Eventually this can serve as an abstraction to support other CI systems
    """

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
    def is_pending_bump(cls, branch: str, folder: str) -> bool:
        """
        Return whether the given branch and folder combination are awaiting a minor bump.

        Args:
            branch: one of the registered gitflow branches
            folder: folder within the repo that defines commitizen tag rules

        Returns:
            whether it is pending or not
        """
        # FIXME: handle slashes in the folder name.  base64 encoding?
        key = PROMOTION_PENDING_VAR.format(branch, folder)
        try:
            state = os.environ[key]
            return state == "true"
        except KeyError:
            # if the variable has not been set we assume this repo is brand new
            return True

    @classmethod
    def set_pending_bump_state(cls, branch: str, folder: str, state: bool) -> None:
        """
        Store a state for whether the given project on the given branch needs to have a minor bump.

        If it is true for a given branch, then get_tag_for_branch() will return a minor increment.
        After this, autotag() will set the pending state to False until.

        This pending state is reset to True after each promotion event.

        Args:
            branch: one of the registered gitflow branches
            folder: folder within the repo that defines commitizen tag rules
            state: whether it is pending or not

        Raises:
            HTTPError: If the HTTP request to update or create the variable fails.
        """
        remote = Gitlab.get_remote()

        if remote == GITLAB_REMOTE:
            headers = {
                "PRIVATE-TOKEN": os.environ["ACCESS_TOKEN"],
            }
            base_url = f"{os.environ['CI_API_V4_URL']}/projects/{os.environ['CI_PROJECT_ID']}/variables"
            data = {"value": "true" if state else "false"}
            key = PROMOTION_PENDING_VAR.format(branch, folder)

            put_response = requests.put(
                base_url + f"/{key}", headers=headers, data=data
            )

            if put_response.status_code == 404:
                data["key"] = key
                post_response = requests.post(base_url, headers=headers, data=data)
                post_response.raise_for_status()

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

        git("config", "user.email", "fake@email.com")
        git("config", "user.name", "ci-bot")
        url = url.split("@")[-1]
        git("remote", "add", GITLAB_REMOTE, f"https://oauth2:{access_token}@{url}")

    @classmethod
    def get_remote(cls) -> str | None:
        """
        Configure and retrieve the name of a Git remote for use in CI environments.

        This function configures a Git remote named 'gitlab_origin' using environment variables
        that should be set in the GitLab CI/CD environment. It configures the git user credentials,
        splits the repository URL to format it with the access token, and adds the remote to the
        local git configuration.

        Returns:
            The name of the configured remote ('gitlab_origin') if in CI mode, otherwise None.

        Raises:
            ClickException: If the 'ACCESS_TOKEN' is not set, necessary for accessing the GitLab repository.
        """
        try:
            url = os.environ["CI_REPOSITORY_URL"]
        except KeyError:
            return LOCAL_REMOTE

        cls._setup_remote(url)
        return GITLAB_REMOTE


def git(*args: str, **kwargs) -> subprocess.CompletedProcess:
    """
    Execute a git command with the specified arguments.

    Args:
        *args: Command line arguments for git.
        **kwargs: Additional keyword arguments to pass to subprocess.run.

    Returns:
        A subprocess.CompletedProcess instance containing the command's output and status.

    Raises:
        subprocess.CalledProcessError: If the git command fails.
    """
    args = ["git"] + [str(arg).strip() for arg in args]
    if CONFIG.verbose:
        click.echo(args)
    return subprocess.run(args, text=True, check=True, **kwargs)


def current_branch() -> str:
    """
    Get the current git branch name.

    Returns:
        The name of the current branch.

    Raises:
        RuntimeError: If the current branch name is not obtainable
    """
    try:
        return Gitlab.current_branch()
    except RuntimeError:
        result = git("branch", "--show-current", stdout=subprocess.PIPE)
        branch = result.stdout.strip()
        if not branch:
            raise RuntimeError
        return branch


def get_branches() -> list[str]:
    """
    Retrieve a list of all local branches in the git repository.

    Returns:
        A list of branch names.
    """
    result = git("branch", stdout=subprocess.PIPE)
    return [x.split()[-1] for x in result.stdout.splitlines()]


def checkout(remote: str | None, branch: str, create: bool = False) -> str:
    """
    Check out a specific branch, optionally creating it if it doesn't exist.

    Args:
        remote: The name of the remote repository.
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
    return git("rev-parse", "HEAD", stdout=subprocess.PIPE).stdout.strip()


def branch_exists(branch: str) -> bool:
    try:
        git("rev-parse", "--verify", branch, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError:
        return False
    return True


def get_latest_commit(remote: str | None, branch_name: str) -> str:
    """
    Fetch the latest commit hash from a specific branch.

    Args:
        branch_name (str): The name of the branch to fetch the latest commit from.
        remote (str | None): The name of the remote repository. If None, the local repository is used.

    Returns:
        str: The latest commit hash of the specified branch.

    Raises:
        subprocess.CalledProcessError: If the git command fails.
    """
    if remote:
        git("fetch", remote, branch_name)
    return git(
        "rev-parse", join(remote, branch_name), stdout=subprocess.PIPE
    ).stdout.strip()


def get_tag_for_branch(branch: str, folder: str) -> tuple[str, str]:
    """
    Determine the appropriate new tag for a given branch based on the latest changes.

    This function uses the Commitizen tool to determine the next tag name for a branch, potentially
    adjusting for pre-release tags and minor version increments.

    Args:
        branch: The name of the branch for which to generate the tag.
        folder: The folder within the repo that controls the tag.

    Returns:
        The new tag to be created, and its increment type

    Raises:
        RuntimeError: If the command does not generate an output or fails to determine the tag.
    """

    prerelease = CONFIG.branch_to_prerelease[branch]

    increment = "patch"

    # Only apply minor increment beta
    if branch == CONFIG.beta and Gitlab.is_pending_bump(CONFIG.beta, folder):
        increment = "minor"

    args = [f"--increment={increment}"]
    if prerelease:
        args += ["--prerelease", prerelease]

    if increment == "minor":
        args += ["--increment-mode=exact"]

    # run this in the project directory so that the pyproject.toml is accessible.
    output = subprocess.check_output(
        ["cz", "bump"] + list(args) + ["--dry-run", "--yes"],
        text=True,
        cwd=folder,
    )
    match = re.search("tag to create: (.*)", output)

    if not match:
        raise click.ClickException(output)

    tag = match.group(1).strip()

    return tag, increment


def join(remote: str | None, branch: str) -> str:
    """
    Construct a full branch path with remote prefix if specified.

    Args:
        remote: The remote repository name, or None if local.
        branch: The branch name.

    Returns:
        str: The full path to the branch, prefixed by the remote name if specified.
    """
    if remote:
        return f"{remote}/{branch}"
    else:
        return branch


def get_projects() -> list[str]:
    """Get the list of projects.

    A project is defined as a folder with a pyproject.toml file with a `tool.commitizen` section.
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
    """
    if not base_rev:
        base_rev = Gitlab.get_base_rev()

    # Compare the current commit with the branch you want to merge with:
    # FIXME: do not included deleted files
    result = git(
        "diff-tree", "--name-only", "-r", base_rev, "HEAD", stdout=subprocess.PIPE
    )
    all_files = result.stdout.strip().splitlines()
    if CONFIG.verbose:
        if all_files:
            click.echo(f"Modified files between {base_rev} and HEAD:")
            for path in all_files:
                click.echo(f" {path}")
        else:
            click.echo(f"No modified files between {base_rev} and HEAD")

    # FIXME: limit this to items in get_projects()
    return sorted(set(os.path.dirname(x) for x in all_files))


@click.group()
@click.option("--config", "-c", "config_path", type=str)
@click.option("--verbose", "-v", is_flag=True, default=False)
def cli(config_path: str, verbose: bool):
    set_config(load_config(config_path, verbose))


@cli.command()
@click.option(
    "--annotation",
    default="automatic change detected",
    help="Message to pass for tag annotations.",
)
@click.option(
    "--base-rev",
)
def autotag(annotation, base_rev):
    """
    Automatically tag the current branch with a new version number and push the tag to the remote repository.

    Determines the next tag, then tags the branch and pushes the tag to the remote.
    """
    branch = current_branch()
    remote = Gitlab.get_remote()

    project_folders = get_modified_projects(base_rev)
    if project_folders:
        for project_folder in project_folders:
            # Auto-tag
            tag, increment = get_tag_for_branch(branch, project_folder)

            # NOTE: this delay is necessary to create stable sorting of tags
            # because git's time resolution is 1s (same as unix timestamp).
            # https://stackoverflow.com/questions/28237043/what-is-the-resolution-of-gits-commit-date-or-author-date-timestamps
            time.sleep(1.1)

            click.echo(f"Creating new tag: {tag} on branch: {branch} {time.time()}")
            git("tag", "-a", tag, "-m", annotation)

            if increment == "minor" and branch == CONFIG.beta:
                # FIXME: we may want to roll this back if push fails
                Gitlab.set_pending_bump_state(branch, project_folder, False)

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

    This session checks if there is an upstream branch and performs an automatic merge from the
    current branch. If conflicts arise, the session logs the conflicts and fails. If successful,
    it pushes the changes to the remote.
    """
    remote = Gitlab.get_remote()
    branch = current_branch()
    upstream_branch = get_upstream_branch(branch)
    if not upstream_branch:
        click.echo(f"No branch upstream from {branch}. Skipping auto-merge")
        return

    # Auto-merge
    # Record the current state
    message = git(
        "log", "--pretty=format: %s", "-1", stdout=subprocess.PIPE
    ).stdout.strip()
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
        if remote == GITLAB_REMOTE:
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
    Promote changes through the branch hierarchy from beta to CONFIG.rc to stable.

    This session promotes changes upstream through defined branches, handling merge operations
    and triggering deployment/testing pipelines with new versions. Each branch promotion checks
    for the presence of the branch, merges, and pushes changes.
    """
    remote = Gitlab.get_remote()
    if CONFIG.verbose:
        click.echo(f"remote = {remote}")

    # FIXME: this is returning sha's instead of branch names in CI
    # all_branches = get_branches()

    def promote_branch(branch: str, log_msg: str):
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
            if remote == GITLAB_REMOTE:
                args.extend(
                    [
                        "-o",
                        f"ci.variable={ENVVAR_PREFIX}_SKIP_HOTFIX=true",
                        "-o",
                        f"ci.variable={ENVVAR_PREFIX}_AUTOTAG_ANNOTATION={log_msg}",
                    ]
                )
            git(*args)
            if remote == LOCAL_REMOTE:
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
    # FIXME: actually maybe it does matter, because missing branches will be created at the current location
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
    for project_name in get_projects():
        Gitlab.set_pending_bump_state(CONFIG.beta, project_name, True)


if __name__ == "__main__":
    cli(auto_envvar_prefix=ENVVAR_PREFIX)
