from __future__ import absolute_import, print_function, annotations
import click
import time
import os
import subprocess
import re
from pathlib import Path

from .core import (
    is_url,
    get_next_version,
    get_current_version,
    get_modified_projects,
    get_projects,
    load_config,
    promote as _promote,
    Config,
    Runtime,
    Backend,
    GitlabBackend,
    TestGitlabBackend,
    GitlabRuntime,
    TestGitlabRuntime,
    LocalRuntime,
    ENVVAR_PREFIX,
    HOTFIX_MESSAGE,
)
from .gitutils import git, checkout

CONFIG: Config
CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


def set_config(config: Config) -> Config:
    """
    Set the global configuration object.
    """
    global CONFIG
    CONFIG = config
    return config


def get_backend(url: str | None = None) -> Backend:
    """
    Return a Backend corresponding to where the current python process is pushing/pulling.
    """
    if os.environ.get("GITLAB_CI", "false") == "true" or (url and "gitlab" in url):
        return GitlabBackend(CONFIG)
    else:
        return TestGitlabBackend(CONFIG)


def get_runtime() -> Runtime:
    """
    Return a Runtime corresponding to where the current python process is *running*
    """
    if os.environ.get("GITLAB_CI", "false") == "true":
        return GitlabRuntime(CONFIG)
    # gitlab-ci-local and our unittests set this to false as an inidicator that
    # we are testing gitlab, but not IN gitlab.
    elif os.environ.get("GITLAB_CI") == "false":
        return TestGitlabRuntime(CONFIG)
    else:
        return LocalRuntime(CONFIG)


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

    if not base_rev:
        base_rev = runtime.get_base_rev()

    projects = get_modified_projects(base_rev, verbose=CONFIG.verbose)
    if projects:
        for project_folder, project_name in projects:
            # Auto-tag
            tag = get_next_version(
                CONFIG, remote, branch, project_name, project_folder, as_tag=True
            )

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
    upstream_branch = CONFIG.get_upstream_branch(branch)
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
    _promote(CONFIG, backend, runtime)


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
        runtime = get_runtime()
        projects_ = get_modified_projects(
            runtime.get_base_rev(), verbose=CONFIG.verbose
        )
    else:
        projects_ = get_projects()

    for project_dir, project_name in projects_:
        if project_name is None:
            project_name = "[unset]"
        click.echo(f"{project_name} = {project_dir}")


@cli.command
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
    "--remote",
    default="origin",
    show_default=True,
    help="The git remote to use to when determining the next version.",
)
@click.option("--next", "-n", is_flag=True, default=False)
@click.option("--as-tag", "-t", is_flag=True, default=False)
def version(
    path: str, branch: str | None, remote: str, next: bool, as_tag: bool
) -> None:
    """
    Get the project version
    """
    projects = dict(get_projects())
    folder = Path(path)
    try:
        project_name = projects[folder]
    except KeyError:
        raise click.ClickException(
            f"There is not a project at path={folder}. "
            "Ensure there is a pyproject.toml file with a [tool.tide] section "
            "and a `project` entry"
        )

    if next:
        if branch is None:
            runtime = get_runtime()
            branch = runtime.current_branch()

        click.echo(
            get_next_version(CONFIG, remote, branch, project_name, folder, as_tag)
        )
    else:
        click.echo(get_current_version(CONFIG, project_name, folder, as_tag))


def main() -> None:
    import shutil

    return cli(
        auto_envvar_prefix=ENVVAR_PREFIX,
        max_content_width=shutil.get_terminal_size().columns,
    )
