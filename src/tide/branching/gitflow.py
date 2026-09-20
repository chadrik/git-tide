"""The gitflow branching model: a ladder of branches advanced by promotion."""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import TYPE_CHECKING

import click

from tide.core import (
    ENVVAR_PREFIX,
    HOTFIX_MESSAGE,
    PROMOTION_BASE_MSG,
    PROMOTION_CYCLE_START_MESSAGE,
    PROMOTION_MESSAGE,
    Backend,
    BranchingModel,
    Config,
    ReleaseID,
    Runtime,
    TestGitlabBackend,
    _init_commitizen_context,
    get_modified_projects,
    get_projects,
    project_versions,
)
from tide.gitutils import (
    checkout_remote_branch,
    current_rev,
    get_tags,
    git,
    join,
    print_git_graph,
)

if TYPE_CHECKING:
    import commitizen.providers
    import commitizen.version_schemes


def is_pending_bump(
    config: Config,
    provider: commitizen.providers.ScmProvider,
    branch: str,
    remote: str | None = None,
    add_missing_promote_marker: bool = False,
    fetch: bool = True,
) -> bool:
    """Return whether `branch` awaits a minor version bump.

    Args:
        config: The tide configuration
        provider: the commitizen provider, used to match tags against the tag format
        branch: one of the registered gitflow branches
        remote: The remote repository name
        add_missing_promote_marker: set this to True before the first promotion of
          branches. The function then writes a promotion marker, so that a later
          call does not request a second minor version bump.
        fetch: whether to fetch promotion marker notes from the remote. Set this
          to False when a previous step already fetched the notes.

    Returns:
        True if `branch` awaits a minor version bump.
    """
    exp_branch = config.most_experimental_branch()
    if branch != exp_branch:
        return False

    promotion_rev = get_promotion_marker(remote, fetch=fetch)

    if promotion_rev is None:
        if config.verbose:
            click.echo("No promote marker found", err=True)
        if add_missing_promote_marker:
            if remote is None:
                raise ValueError("Must provide remote when setting add_missing_promote_marker=True")
            set_promotion_marker(remote, current_rev("HEAD~1"), fetch=fetch)
        return True
    else:
        if config.verbose:
            click.echo(f"Found promotion base rev: {promotion_rev}", err=True)

    suffix = config.branch_to_release_id[exp_branch].prerelease_suffix()

    def prerelease_match(ver: commitizen.version_schemes.VersionProtocol) -> bool:
        if suffix is None:
            # This should happen only when the repository has a stable branch and no
            # pre-release branch.
            return ver.prerelease is None
        else:
            return ver.prerelease is not None and ver.prerelease.startswith(suffix)

    matcher = provider._tag_format_matcher()
    # List the tags of this project between this branch and the promotion note.
    all_tags = get_tags(end_rev=promotion_rev)
    for tag in all_tags:
        ver = matcher(tag)
        # A matching tag for this branch means that the promotion already happened.
        if ver is not None and prerelease_match(ver):
            return False
    # No tag exists after the promotion, so the bump is still pending.
    return True


def get_promotion_marker(remote: str | None = None, fetch: bool = True) -> str | None:
    """Return the hash of the most recent promotion commit.

    Args:
        remote: The remote repository name
        fetch: whether to fetch the notes that hold promotion markers from the remote.
          Set this to False when a previous step already fetched the notes.
    """
    if fetch:
        git("fetch", remote if remote else "--all", "+refs/notes/*:refs/notes/*", quiet=True)

    start_rev = "HEAD"

    # Search the history for the promotion marker, 100 commits at a time.
    while True:
        # TODO: the final call to `git log` always fails at the start of the history.
        #  Skip it by checking whether the previous call returned fewer than 100 commits.
        try:
            output = git(
                "log",
                "--first-parent",
                "--format=%H %N",
                "-n100",
                start_rev,
                capture=True,
            )
        except subprocess.CalledProcessError:
            # End of history or invalid ref
            return None

        if not output:
            return None

        lines = output.splitlines()
        last_rev: str | None = None

        for line in lines:
            line = line.strip()
            if not line:
                continue

            parts = line.split(maxsplit=1)
            last_rev = parts[0]
            if len(parts) == 1:
                continue

            if parts[1] == PROMOTION_BASE_MSG:
                return last_rev

        if not last_rev:
            return None

        start_rev = f"{last_rev}^"


def set_promotion_marker(remote: str, branch: str, fetch: bool = True) -> None:
    """Add a promotion marker note to `branch`, and push the note to `remote`.

    `is_pending_bump` searches the history for this marker. A branch that has a marker
    and no later tag awaits a minor version bump. `promote` writes a new marker at the
    end of each promotion cycle, which starts the next minor version.

    Args:
        remote: The remote repository name
        branch: the branch or revision to attach the marker to
        fetch: whether to fetch existing notes from the remote before adding the
          marker. Set this to False when a previous step already fetched the notes.
    """
    if fetch:
        git("fetch", remote, "+refs/notes/*:refs/notes/*")
    # FIXME: force the note, because the same commit can be the promotion base more
    #  than once. Consider skipping the note instead.
    git("notes", "add", "--force", "-m", PROMOTION_BASE_MSG, branch)
    git("push", remote, "refs/notes/*")


def promote(config: Config, backend: Backend, runtime: Runtime) -> None:
    """Promote changes through the branch hierarchy.

    e.g. from alpha -> beta -> rc -> stable.
    """
    remote = runtime.get_remote()
    if config.verbose:
        click.echo(f"remote = {remote}")

    local_output = []

    def promote_branch(branch: str, log_msg_template: str) -> None:
        """Promote a branch to its upstream branch.

        1. Check out the branch.
        2. Merge the upstream branch, if it exists.
        3. Push the branch, and skip the hotfix job.

        The function leaves the branch checked out.
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

        # Start the test job and the tag job for these new versions, but skip the
        # auto-hotfix. --atomic makes git push every ref or no ref at all, like a
        # database transaction. This may not be necessary here.
        click.echo("Pushing changes")
        backend.push("--atomic", remote, branch, variables=variables)

        # FIXME: switch to using push-opts.json
        if isinstance(backend, TestGitlabBackend) and upstream_branch and base_rev != current_rev():
            push_info = {
                "annotation": log_msg,
                "base_rev": base_rev,
                "branch": branch,
            }
            local_output.append(push_info)
            click.echo(f"Trigger: {json.dumps(push_info)}")

    # Promote each branch, from stable to the most experimental branch.
    for branch in reversed(config.branches):
        # The active branch does not matter here, because `promote_branch` checks out
        # each branch before it merges.
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

    # tide does not tag the cycle-start branch now. The first commit on that branch
    # creates the tag.
    experimental_branch = config.most_experimental_branch()
    if experimental_branch:
        set_promotion_marker(remote, experimental_branch)


class GitflowModel(BranchingModel):
    """Versions advance along a fixed ladder of branches, one per ReleaseID.

    A promotion marker drives a minor bump, not a commit message. A change cascades
    down the ladder as a hotfix, and up the ladder as a promotion.
    """

    def release_id(self, branch: str) -> ReleaseID:
        try:
            return self.config.branch_to_release_id[branch]
        except KeyError:
            raise click.ClickException(
                f"{branch} is not a valid release branch.  "
                f"Must be one of {', '.join(self.config.branches)}"
            )

    def protected_branch_patterns(self) -> list[str]:
        """Protect the ladder, which is every branch that this mode moves.

        The configuration fixes the ladder. Unlike semantic_commit mode, this mode
        creates no branch after `init` runs, so it needs no wildcard.
        """
        return list(self.config.branches)

    def uses_promotion_schedule(self) -> bool:
        """A scheduled job starts the promotion, because no push starts it."""
        return True

    def next_version(
        self,
        branch: str,
        project_name: str,
        remote: str | None = None,
        as_tag: bool = False,
        dry_run: bool = True,
        fetch: bool = True,
    ) -> str | None:
        from commitizen import bump
        from commitizen.version_schemes import Increment

        release_id = self.release_id(branch)

        cz_ctx = _init_commitizen_context(self.config, project_name)
        current_version = cz_ctx.scheme(cz_ctx.provider.get_version())

        if release_id != ReleaseID.stable:
            prerelease: str | None = release_id.value
            if (
                not dry_run
                and not current_version.prerelease
                and branch != self.config.most_experimental_branch()
            ):
                # Mint no version until the branch receives its first promotion. For
                # example, tide creates no rc tag before the first promotion of beta
                # to rc.
                return None
        else:
            prerelease = None

        # A project with no tag starts at 0.1.0. tide never applies a patch increment
        # to the 0.0.0 that commitizen reports when it finds no tag.
        if not project_versions(self.config, project_name):
            pending_bump = True
        else:
            # Find the closest promotion note to the current branch
            pending_bump = is_pending_bump(
                self.config,
                cz_ctx.provider,
                branch,
                remote,
                add_missing_promote_marker=not dry_run,
                fetch=fetch,
            )

        # Only the most experimental branch takes a minor increment.
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
            tag_format=cz_ctx.config.settings["tag_format"] if as_tag else "$version",
            scheme=cz_ctx.scheme,
        )

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
        branch = runtime.current_branch()
        remote = runtime.get_remote()

        if not base_rev:
            base_rev = runtime.get_base_rev()

        if projects:
            path_mapping = {name: path for path, name in get_projects()}
            projects_and_paths = [(path_mapping[name], name) for name in sorted(projects)]
        else:
            projects_and_paths = get_modified_projects(base_rev, verbose=self.config.verbose)

        if not projects_and_paths:
            click.echo("No projects were modified and no tags generated!", err=True)
            return

        for _, project_name in projects_and_paths:
            # Auto-tag
            tag = self.next_version(
                branch,
                project_name=project_name,
                remote=remote,
                as_tag=True,
                dry_run=False,
                fetch=fetch,
            )
            # next_version returns None until the branch receives its first promotion.
            if tag is None:
                continue

            self._create_tag(tag, annotation, branch, dry_run)

            # FIXME: push all of the tags at once.
            click.echo(f"Pushing '{tag}' to {remote}" + (" (dry_run=True)" if dry_run else ""))
            if not dry_run:
                backend.push(remote, tag)

    def hotfix(self, runtime: Runtime, backend: Backend) -> None:
        branch = runtime.current_branch()
        remote = runtime.get_remote()
        upstream_branch = self.config.get_upstream_branch(branch)
        if not upstream_branch:
            click.echo(f"No branch upstream from {branch}. Skipping auto-merge")
            return

        # Record the message of the most recent commit.
        message = git("log", "--pretty=format: %s", "-1", capture=True)
        # Remove the formatting that a previous auto-hotfix added.
        match = re.match(HOTFIX_MESSAGE.format(upstream_branch="[^:]+", message="(.*)$"), message)
        if match:
            message = match.groups()[0]

        tmp_branch = f"{branch}_temp"
        git("checkout", "-B", tmp_branch)
        start_rev = current_rev()
        click.echo(f"Branch {branch} at {start_rev}")

        try:
            # Fetch the upstream branch
            git("fetch", remote, upstream_branch)

            rev = checkout_remote_branch(remote, upstream_branch)

            click.echo(f"Branch {upstream_branch} at {rev}", err=True)

            msg = HOTFIX_MESSAGE.format(upstream_branch=upstream_branch, message=message)
            click.echo(msg, err=True)

            try:
                git("merge", f"{branch}_temp", "-m", msg)
            except subprocess.CalledProcessError:
                click.echo("Conflicts:", err=True)
                git("diff", "--name-only", "--diff-filter=U")
                raise click.ClickException("Encountered conflicts during merge")

            # This push starts a full pipeline for upstream_branch, and possibly
            # another auto-merge.
            click.echo(f"Pushing {upstream_branch} to {remote}", err=True)
            variables = {
                f"{ENVVAR_PREFIX}_AUTOTAG_ANNOTATION": msg,
            }
            try:
                backend.push(remote, upstream_branch, variables=variables)
            except subprocess.CalledProcessError as err:
                click.echo(err, err=True)
                git("remote", "-v")
                print_git_graph(max_count=50)
                raise click.ClickException("Failed to push changes")
        finally:
            # Restore the original branch.
            git("checkout", start_rev, quiet=True)
            git("branch", "--delete", tmp_branch, quiet=True)

    def promote(self, runtime: Runtime, backend: Backend) -> None:
        promote(self.config, backend, runtime)
