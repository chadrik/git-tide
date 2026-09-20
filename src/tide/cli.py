"""Command line interface for tide."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from .branching.gitflow import GitflowModel
from .branching.semantic import SemanticCommitModel
from .core import (
    ENVVAR_PREFIX,
    Backend,
    BranchingMode,
    BranchingModel,
    Config,
    GitlabBackend,
    GitlabRuntime,
    LocalRuntime,
    Runtime,
    TestGitlabBackend,
    TestGitlabRuntime,
    get_commits,
    get_current_version,
    get_modified_projects,
    get_project_name,
    get_projects,
    get_version_at_ref,
    is_url,
    load_config,
)
from .gitutils import (
    git,
    set_git_verbose,
)

CONFIG: Config
CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


def set_config(config: Config) -> Config:
    """Set the global configuration object."""
    global CONFIG
    CONFIG = config
    set_git_verbose(config.verbose)
    return config


def get_backend(url: str | None = None) -> Backend:
    """Return the Backend for the remote that this process pushes to and pulls from."""
    if os.environ.get("GITLAB_CI", "false") == "true" or (url and "gitlab" in url):
        return GitlabBackend(CONFIG)
    else:
        return TestGitlabBackend(CONFIG)


def get_branching_model() -> BranchingModel:
    """Return the BranchingModel for the branching mode that this repository selects."""
    if CONFIG.branching_mode is BranchingMode.semantic_commit:
        return SemanticCommitModel(CONFIG)
    else:
        return GitflowModel(CONFIG)


def get_runtime() -> Runtime:
    """Return the Runtime for the environment that this process runs in."""
    if os.environ.get("GITLAB_CI", "false") == "true":
        return GitlabRuntime(CONFIG)
    # gitlab-ci-local and the unit tests set this to "false". It means that the test
    # targets gitlab, but does not run inside gitlab.
    elif os.environ.get("GITLAB_CI") == "false":
        return TestGitlabRuntime(CONFIG)
    else:
        return LocalRuntime(CONFIG)


@click.group(context_settings=CONTEXT_SETTINGS)
@click.option("--config", "-c", "config_path", metavar="CONFIG", type=str)
@click.option("--verbose", "-v", is_flag=True, default=False)
@click.pass_context
def cli(ctx: click.Context, config_path: str, verbose: bool) -> None:
    args = sys.argv[1:]
    if not any(flag in args for flag in ctx.help_option_names):
        set_config(load_config(config_path, verbose))


@cli.command()
@click.option(
    "--access-token",
    required=True,
    metavar="TOKEN",
    help="The security token that authenticates tide with the remote",
)
@click.option(
    "--remote",
    default="origin",
    metavar="REMOTE",
    show_default=True,
    help=(
        "The name of a git remote in the current git repo "
        "(e.g. one configured with `git remote`), or the URL of the remote. "
        "A URL implies --no-local"
    ),
)
@click.option(
    "--save-token/--no-save-token",
    default=True,
    help="Whether to save the access token into the remote as a reusable "
    "variable. If you disable this, you must set the ACCESS_TOKEN "
    "variable yourself.",
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
    """Initialize the current git repo and its Gitlab project for use with tide.

    Run this command from a git repo. The repo must have the Gitlab project as a remote.
    Clone the repo from Gitlab, or add the remote with `git remote add`.
    """
    import tempfile

    if is_url(remote):
        init_local = False
        remote_url = remote
    else:
        # FIXME: print a clear error when the current directory is not a git repo.
        # FIXME: handle a remote that is not configured correctly.
        remote_url = git("remote", "get-url", remote, capture=True)

    backend = get_backend(remote_url)

    if init_local:
        backend.init_local_repo(remote)
    else:
        # The remote may still need branches, so clone it into a temporary directory.
        with tempfile.TemporaryDirectory() as tmpdir:
            git("clone", f"--branch={CONFIG.stable}", remote_url, tmpdir)
            pwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                backend.init_local_repo("origin")
            finally:
                os.chdir(pwd)

    # FIXME: add the `tool.tide` section to pyproject.toml, or check that it exists.
    #  tide cannot do this automatically, because a monorepo can contain many.
    # FIXME: create a stub gitlab-ci.yml file when none exists.
    if init_remote:
        backend.init_remote_repo(remote_url, access_token, save_token, get_branching_model())


@cli.command()
@click.option(
    "--annotation",
    default="automatic change detected",
    show_default=True,
    help="The message to store in the tag annotation.",
)
@click.option(
    "--base-rev",
    metavar="SHA",
    help="The Git revision to compare against when identifying changed files.",
)
@click.option(
    "--project",
    "-p",
    "projects",
    multiple=True,
    metavar="PROJECT",
    help="The name of a modified tide project to tag. "
    "If you do not set this, tide finds the projects that contain changed files. "
    "A project is a folder with a pyproject.toml file that has a "
    "`[project].name` value or a `[tool.tide].project` value.",
)
@click.option("--dry-run", is_flag=True, default=False)
@click.option(
    "--fetch/--no-fetch",
    default=True,
    help="Whether to fetch the promotion marker notes from the remote. Use "
    "--no-fetch when a previous step already fetched the notes, e.g. to stop "
    "parallel jobs from fetching at the same time.",
)
def autotag(
    annotation: str,
    base_rev: str | None,
    projects: list[str],
    dry_run: bool,
    fetch: bool,
) -> None:
    """Tag the current branch with a new version number.

    tide pushes the new tag to the remote repository.
    """
    get_branching_model().autotag(
        get_runtime(),
        get_backend(),
        annotation=annotation,
        base_rev=base_rev,
        projects=tuple(projects),
        dry_run=dry_run,
        fetch=fetch,
    )


@cli.command()
def hotfix() -> None:
    """Merge hotfixes from a feature branch back to upstream branches."""
    get_branching_model().hotfix(get_runtime(), get_backend())


@cli.command()
def promote() -> None:
    """Promote changes through the branch hierarchy.

    e.g. from alpha -> beta -> rc -> stable.
    """
    get_branching_model().promote(get_runtime(), get_backend())


@cli.command()
@click.option(
    "--target-branch",
    required=True,
    metavar="BRANCH",
    help="The branch these changes are destined for, e.g. the target of a merge request.",
)
def validate(target_branch: str) -> None:
    """Check the commits leading to HEAD against the rules of the branching mode.

    This command exits non-zero with a distinct code for each rule. Your CI
    configuration decides whether that failure blocks the pipeline. Run this job with
    `allow_failure: true` to make it advisory.
    """
    model = get_branching_model()
    commits = get_commits(_merge_base(target_branch))
    model.validate(target_branch, commits=commits)
    click.echo(f"{len(commits)} commit(s) validated against {target_branch}")


def _merge_base(target_branch: str) -> str | None:
    """Return the commit where HEAD diverged from `target_branch`.

    Returns:
        The merge base, or None if HEAD and `target_branch` share no history.
    """
    import subprocess

    from .core import fetch_ref

    ref = fetch_ref(target_branch, get_runtime().get_remote())
    if ref is None:
        raise click.ClickException(f"Could not resolve branch {target_branch!r}")
    try:
        return git("merge-base", ref, "HEAD", capture=True, quiet=True)
    except subprocess.CalledProcessError:
        return None


@cli.command
@click.option("--modified", "-m", is_flag=True, default=False)
# FIXME: add output format
# FIXME: add option to write to file
def projects(modified: bool) -> None:
    """List the project paths within the repo.

    A project is a folder with a pyproject.toml file that has a `[project].name` value
    or a `[tool.tide].project` value.
    """
    if modified:
        runtime = get_runtime()
        projects_ = get_modified_projects(runtime.get_base_rev(), verbose=CONFIG.verbose)
    else:
        projects_ = list(get_projects())

    for project_dir, project_name in projects_:
        if project_name is None:
            project_name = "[unset]"
        click.echo(f"{project_name} = {project_dir}")


@cli.command
@click.option("--as-tag", "-t", is_flag=True, default=False)
@click.option(
    "--at-ref",
    default=None,
    metavar="REF",
    help="The git ref (commit SHA, branch, or tag) to read the version from. "
    "tide reads only the tags that already point at this ref.",
)
@click.option(
    "--branch",
    default=None,
    metavar="BRANCH",
    help="The release branch that names the release phase to filter the tags by. "
    "Use this option only with --at-ref. "
    "Examples: develop (alpha), staging (rc), master (stable).",
)
@click.option(
    "--path",
    default=".",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    show_default=True,
    help="A folder within the repository. The folder must contain a pyproject.toml file",
)
def version(
    as_tag: bool,
    at_ref: str | None,
    branch: str | None,
    path: Path,
) -> None:
    """Print the current project version."""
    project_name = get_project_name(path)
    if project_name is None:
        raise click.ClickException(
            f"Could not determine the project name at {path.absolute()}. "
            "Ensure that the folder has a pyproject.toml file "
            "with project.name or tool.tide.project defined"
        )

    if branch and not at_ref:
        raise click.ClickException("--branch can only be used with --at-ref")

    if at_ref:
        release_id = None
        if branch:
            release_id = get_branching_model().release_id(branch)

        click.echo(
            get_version_at_ref(
                CONFIG,
                project_name=project_name,
                ref=at_ref,
                as_tag=as_tag,
                release_id=release_id,
            )
        )
    else:
        click.echo(get_current_version(CONFIG, project_name=project_name, as_tag=as_tag))


@cli.command
@click.option(
    "--branch",
    default=None,
    help=(
        "The release branch that determines the release phase of the version. "
        "Defaults to the current branch."
    ),
)
@click.option(
    "--remote",
    default="origin",
    show_default=True,
    help="The git remote to query when tide determines the next version.",
)
@click.option("--as-tag", "-t", is_flag=True, default=False)
@click.option(
    "--path",
    default=".",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    show_default=True,
    help="A folder within the repository. The folder must contain a pyproject.toml file",
)
def next_version(
    branch: str | None,
    remote: str,
    as_tag: bool,
    path: Path,
) -> None:
    """Print the next project version."""
    project_name = get_project_name(path)
    if project_name is None:
        raise click.ClickException(
            f"Could not determine the project name at {path.absolute()}. "
            "Ensure that the folder has a pyproject.toml file "
            "with project.name or tool.tide.project defined"
        )
    if branch is None:
        runtime = get_runtime()
        branch = runtime.current_branch()

    click.echo(
        get_branching_model().next_version(
            branch, project_name=project_name, remote=remote, as_tag=as_tag
        )
    )


def main() -> None:
    import shutil

    return cli(
        auto_envvar_prefix=ENVVAR_PREFIX,
        max_content_width=shutil.get_terminal_size().columns,
    )
