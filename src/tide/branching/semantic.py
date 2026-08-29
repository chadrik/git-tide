"""The semantic-commit branching model: trunk-based, with per-project release branches."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from enum import IntEnum
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, NamedTuple

import click

from tide.core import (
    GITLAB_REMOTE,
    TOOL_NAME,
    Backend,
    BranchingModel,
    Commit,
    Config,
    ReleaseID,
    Runtime,
    _init_commitizen_context,
    fetch_ref,
    get_commits,
    get_projects,
    group_files_by_projects,
    project_versions,
)
from tide.gitutils import git

if TYPE_CHECKING:
    import commitizen.version_schemes

# The first version of a project that has no tags. tide never applies a patch increment
# to the 0.0.0 that commitizen reports when it finds no matching tag.
INITIAL_VERSION = "0.1.0"


@lru_cache(maxsize=None)
def _default_project() -> str:
    """Return the name of the repository's only project.

    tide calls this when `release_branch_format` has no `$project` placeholder. That
    format is valid only when the repository contains one project.

    Raises:
        ClickException: if the repository contains more than one project.
    """
    projects = get_projects()
    if len(projects) != 1:
        raise click.ClickException(
            f"'tool.{TOOL_NAME}.release_branch_format' has no '$project' placeholder, so "
            f"release branches cannot be attributed to one of the {len(projects)} projects "
            f"in this repository. Add '$project' to release_branch_format."
        )
    return projects[0][1]


class NextVersion(NamedTuple):
    """The version a project is about to mint, and its tag."""

    version: commitizen.version_schemes.VersionProtocol
    tag: str

    @property
    def version_line(self) -> tuple[int, int]:
        """The (major, minor) version line this version belongs to."""
        return (self.version.major, self.version.minor)


_PLACEHOLDERS = re.compile(r"(\$project|\$major|\$minor)")


@dataclass(frozen=True)
class ReleaseBranch:
    """The project and version line for a release branch."""

    project: str
    major: int
    minor: int

    @property
    def version_line(self) -> tuple[int, int]:
        """The (major, minor) version line this branch owns."""
        return (self.major, self.minor)

    @staticmethod
    @lru_cache(maxsize=None)
    def _pattern(release_branch_format: str) -> re.Pattern:
        """Compile `release_branch_format` into a regex that parses a branch name."""
        groups = {
            "$project": r"(?P<project>.+)",
            "$major": r"(?P<major>\d+)",
            "$minor": r"(?P<minor>\d+)",
        }
        parts = [
            groups[token] if token in groups else re.escape(token)
            for token in _PLACEHOLDERS.split(release_branch_format)
        ]
        return re.compile("^{}$".format("".join(parts)))

    @classmethod
    def parse(cls, config: Config, branch: str) -> ReleaseBranch | None:
        """Parse a branch name into a ReleaseBranch.

        Args:
            config: The tide configuration
            branch: a branch name

        Returns:
            A ReleaseBranch, or None if `branch` does not match `release_branch_format`.
        """
        match = cls._pattern(config.release_branch_format).match(branch)
        if match is None:
            return None
        groups = match.groupdict()
        return cls(
            project=groups.get("project") or _default_project(),
            major=int(groups["major"]),
            minor=int(groups["minor"]),
        )

    @classmethod
    def instance(
        cls, config: Config, project_name: str, version_line: tuple[int, int]
    ) -> ReleaseBranch:
        """Construct a release branch.

        Args:
            config: The tide configuration
            project_name: The name of the project that owns the branch
            version_line: the (major, minor) version line
        """
        major, minor = version_line
        # Record the project only when the format contains it. A branch built here must
        # then equal the same branch that `parse` returns from its name.
        project = project_name if "$project" in config.release_branch_format else _default_project()
        return cls(project=project, major=major, minor=minor)

    def name(self, config: Config) -> str:
        """Return this branch's name under `release_branch_format`."""
        return (
            config.release_branch_format.replace("$project", self.project or "")
            .replace("$major", str(self.major))
            .replace("$minor", str(self.minor))
        )

    def get_ref(self, config: Config, remote: str | None = None) -> str | None:
        """Return a ref for this branch, fetching it from `remote` if it is not local.

        Returns:
            A usable ref, or None if the branch exists neither locally nor on `remote`.
        """
        return fetch_ref(self.name(config), remote)


class ValidationCode(IntEnum):
    """Process exit codes, one for each way that validation can fail.

    These codes are stable, because a CI configuration can test them. tide never
    changes an exit code to match the context that it runs in.
    """

    ok = 0
    multi_project_commit = 2
    wrong_project = 3
    non_patch_on_release_branch = 4
    trunk_merged_into_release_branch = 5
    trunk_line_diverged = 6
    no_tags_on_line = 7


class ValidationError(click.ClickException):
    """A broken branching rule, carrying the exit code for that rule."""

    def __init__(self, code: ValidationCode, message: str):
        super().__init__(message)
        self.code = code
        # click reads exit_code as a class attribute of the exception subclass, but tide
        # needs one code for each rule. This assignment makes both `tide validate` and
        # `tide autotag` exit with the correct code.
        self.exit_code = int(code)  # type: ignore[misc]


def latest_version(
    config: Config,
    project_name: str,
    rev: str = "HEAD",
    version_line: tuple[int, int] | None = None,
) -> commitizen.version_schemes.VersionProtocol | None:
    """Return the highest version of `project_name` reachable from `rev`.

    Args:
        config: The tide configuration
        project_name: The name of the project, used to find matching tags
        rev: the git ref to search backwards from
        version_line: when given, only consider versions on this (major, minor) version line.
            The branch name decides the line, not the git history. A merge of the trunk
            into a release branch makes a higher line reachable.

    Returns:
        The highest matching version, or None if there are no matching tags.
    """
    versions = list(project_versions(config, project_name, rev))
    if version_line is not None:
        versions = [v for v in versions if (v.major, v.minor) == version_line]
    return max(versions) if versions else None


def projects_touched_by_commit(commit: Commit, project_dirs: dict[Path, str]) -> list[str]:
    """Return the names of the projects a commit touched."""
    grouped = group_files_by_projects(commit.files, project_dirs=project_dirs)
    return sorted(project_dirs[project_dir] for project_dir in grouped)


def find_increment(commits: Iterable[Commit]) -> str | None:
    """Return the semver increment implied by a set of conventional commits.

    Returns:
        "MAJOR", "MINOR", "PATCH", or None when no commit implies a release.
    """
    from commitizen import defaults
    from commitizen.bump import find_increment as cz_find_increment
    from commitizen.git import GitCommit

    git_commits = []
    for commit in commits:
        title, _, body = commit.message.partition("\n")
        git_commits.append(GitCommit(rev=commit.rev, title=title, body=body))

    return cz_find_increment(
        git_commits,
        regex=defaults.bump_pattern,
        increments_map=defaults.bump_map,
    )


def is_ancestor(rev: str, of: str) -> bool:
    """Return whether `rev` is an ancestor of `of`."""
    try:
        git("merge-base", "--is-ancestor", rev, of, quiet=True)
    except subprocess.CalledProcessError:
        return False
    return True


class SemanticCommitModel(BranchingModel):
    """Versions advance from conventional-commit types, on a trunk plus release branches.

    The commits that touched a project since its last tag decide its next version. A
    release branch owns each version line. tide creates that branch when the line opens,
    and fast-forwards it while the trunk still owns the line.
    """

    def is_trunk(self, branch: str) -> bool:
        """Return whether `branch` is the trunk."""
        return branch == self.config.stable

    def validate_branch(self, branch: str) -> None:
        """Check that tide mints versions on `branch`.

        Raises:
            ClickException: if `branch` is neither the trunk nor a release branch.
        """
        if self.is_trunk(branch) or ReleaseBranch.parse(self.config, branch) is not None:
            return
        raise click.ClickException(
            f"{branch} is not a tide-managed branch. It must be the trunk "
            f"({self.config.stable}) or a release branch matching "
            f"{self.config.release_branch_format!r}"
        )

    def release_id(self, branch: str) -> ReleaseID:
        """Every version minted in this mode is a stable release.

        Raises:
            ClickException: if `branch` is neither the trunk nor a release branch.
        """
        self.validate_branch(branch)
        return ReleaseID.stable

    def protected_branch_patterns(self) -> list[str]:
        """Protect the trunk, plus a wildcard that covers every release branch.

        `autotag` creates a release branch long after `init` runs, so this method cannot
        list the names. The wildcard replaces every placeholder in
        `release_branch_format` with `*`. A release branch pipeline then sees
        `CI_COMMIT_REF_PROTECTED == "true"` and runs `auto-tag`.
        """
        return list(self.config.branches) + [
            _PLACEHOLDERS.sub("*", self.config.release_branch_format)
        ]

    def uses_promotion_schedule(self) -> bool:
        """This mode is trunk-based. It has no promotion, so it needs no schedule."""
        return False

    # -- project and commit attribution ------------------------------------------

    def _project_dir(self, project_name: str) -> Path:
        """Return the directory of `project_name`.

        Raises:
            ClickException: if there is no such project.
        """
        for path, name in get_projects():
            if name == project_name:
                return path
        raise click.ClickException(f"No project named {project_name!r} in this repository")

    def _commits_touching(
        self, commits: list[Commit], project_dir: Path, project_dirs: dict[Path, str]
    ) -> list[Commit]:
        """Return the subset of `commits` that touched files belonging to `project_dir`."""
        return [
            commit
            for commit in commits
            if project_dir in group_files_by_projects(commit.files, project_dirs=project_dirs)
        ]

    def _last_release_point(self) -> str | None:
        """Return the most recent tag reachable from HEAD, or None if there is none."""
        import subprocess

        try:
            return git("describe", "--tags", "--abbrev=0", capture=True, quiet=True) or None
        except subprocess.CalledProcessError:
            return None

    # -- version resolution ------------------------------------------------------

    def _next_version(self, branch: str, project_name: str) -> NextVersion | None:
        """Return the version `project_name` should mint on `branch`, or None.

        On a release branch, tide resolves the current version against the version line
        in the branch name, not against every version reachable from HEAD. A trunk merged
        into the branch then cannot advance the version past its own line.
        """
        from commitizen import bump

        cz_ctx = _init_commitizen_context(self.config, project_name)
        release_branch = ReleaseBranch.parse(self.config, branch)
        version_line = release_branch.version_line if release_branch is not None else None

        current = latest_version(self.config, project_name, version_line=version_line)
        if current is None:
            start_tag = None
        else:
            start_tag = project_versions(self.config, project_name)[current]

        project_dir = self._project_dir(project_name)
        commits = self._commits_touching(get_commits(start_tag), project_dir, dict(get_projects()))
        increment = find_increment(commits)
        if increment is None:
            return None

        if current is None:
            new_version = cz_ctx.scheme(INITIAL_VERSION)
        else:
            if version_line is not None:
                # A release branch mints only patches. `validate` rejects a non-patch
                # change. This limit keeps the output of `tide next-version` correct.
                increment = "PATCH"
            new_version = current.bump(increment, exact_increment=True)

        tag = bump.normalize_tag(
            new_version,
            tag_format=cz_ctx.config.settings["tag_format"],
            scheme=cz_ctx.scheme,
        )
        return NextVersion(version=new_version, tag=tag)

    def next_version(
        self,
        branch: str,
        project_name: str,
        remote: str | None = None,
        as_tag: bool = False,
        dry_run: bool = True,
        fetch: bool = True,
    ) -> str | None:
        self.validate_branch(branch)
        result = self._next_version(branch, project_name)
        if result is None:
            return None
        return result.tag if as_tag else str(result.version)

    # -- autotag -----------------------------------------------------------------

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
        self.validate_branch(branch)

        if base_rev:
            click.echo(
                "--base-rev is ignored in semantic_commit mode: versions are derived "
                "from the commits since each project's last tag.",
                err=True,
            )

        # This is the authoritative check. The `validate` job that runs before the merge
        # is advisory: CI can skip it, CI can allow it to fail, and a force push can
        # bypass it.
        self.validate(branch, remote=remote)

        release_branch = ReleaseBranch.parse(self.config, branch)
        if release_branch is not None:
            candidate_projects = [release_branch.project]
            if projects and set(projects) != set(candidate_projects):
                raise click.ClickException(
                    f"Release branch {branch} is bound to {candidate_projects[0]!r}, "
                    f"so it cannot tag {', '.join(sorted(projects))}"
                )
        elif projects:
            candidate_projects = sorted(projects)
        else:
            candidate_projects = [name for _, name in get_projects()]

        tagged = False
        for project_name in candidate_projects:
            next_version = self._next_version(branch, project_name)
            if next_version is None:
                continue

            release_branch = ReleaseBranch.instance(
                self.config, project_name, next_version.version_line
            )
            branch_name = release_branch.name(self.config)
            self._create_tag(next_version.tag, annotation, branch, dry_run)

            # The tag and its branch move together, or neither one moves. tide never
            # forces the push, so git enforces the fast-forward. A release branch that
            # diverged rejects the push, and the tag with it.
            refspec = f"HEAD:refs/heads/{branch_name}"
            click.echo(
                f"Pushing '{next_version.tag}' and '{branch_name}' to {remote}"
                + (" (dry_run=True)" if dry_run else "")
            )
            if not dry_run:
                backend.push("--atomic", remote, next_version.tag, refspec)
            tagged = True

        if not tagged:
            click.echo("No projects were modified and no tags generated!", err=True)

    # -- validation --------------------------------------------------------------

    def validate(
        self,
        target_branch: str,
        commits: list[Commit] | None = None,
        remote: str | None = None,
    ) -> None:
        """Check the commits leading to HEAD against the rules of this mode.

        Args:
            target_branch: the branch the current changes are destined for.
            commits: the commits to check. Defaults to every commit since the last
                release point, which is the set that `autotag` is about to tag.
            remote: the git remote, used to resolve release branches that have no
                local ref (the usual case on a CI runner).

        Raises:
            ValidationError: if a rule is broken. Its exit code names the rule.
        """
        remote = remote or GITLAB_REMOTE
        if commits is None:
            commits = get_commits(self._last_release_point())

        release_branch = ReleaseBranch.parse(self.config, target_branch)
        if release_branch is not None:
            self._validate_release_branch(target_branch, release_branch, commits, remote)
        elif self.is_trunk(target_branch):
            self._validate_trunk(target_branch, remote)
        else:
            self.validate_branch(target_branch)

    def _validate_release_branch(
        self,
        branch: str,
        release_branch: ReleaseBranch,
        commits: list[Commit],
        remote: str,
    ) -> None:
        """Apply the rules that only hold on a release branch."""
        project = release_branch.project
        project_dirs = dict(get_projects())

        if latest_version(self.config, project, version_line=release_branch.version_line) is None:
            raise ValidationError(
                ValidationCode.no_tags_on_line,
                f"'{branch}' owns the {release_branch.major}.{release_branch.minor} version "
                f"line of {project!r}, but no tag on that line is reachable from HEAD, so "
                f"there is no version to patch.",
            )

        trunk_ref = fetch_ref(self.config.stable, remote)
        for commit in commits:
            # A release branch belongs to one project. tide cannot attribute a commit
            # that touches several projects to one version line.
            touched = projects_touched_by_commit(commit, project_dirs)
            if len(touched) > 1:
                raise ValidationError(
                    ValidationCode.multi_project_commit,
                    f"Commit {commit.rev[:8]} touches {len(touched)} projects "
                    f"({', '.join(touched)}), but '{branch}' is bound to {project!r}. "
                    f"Split the change so each commit targets one project.",
                )
            if touched and touched[0] != project:
                raise ValidationError(
                    ValidationCode.wrong_project,
                    f"Commit {commit.rev[:8]} targets project {touched[0]!r}, but "
                    f"'{branch}' owns {project!r}.",
                )

            if commit.is_merge and trunk_ref is not None:
                for parent in commit.parents[1:]:
                    if is_ancestor(parent, trunk_ref):
                        raise ValidationError(
                            ValidationCode.trunk_merged_into_release_branch,
                            f"Commit {commit.rev[:8]} merges the trunk "
                            f"({self.config.stable}) into '{branch}', which risks polluting "
                            f"the release branch with future changes. Cherry-pick the "
                            f"change instead.",
                        )

        increment = find_increment(
            self._commits_touching(commits, self._project_dir(project), project_dirs)
        )
        if increment is not None and increment != "PATCH":
            raise ValidationError(
                ValidationCode.non_patch_on_release_branch,
                f"These commits imply a {increment} version bump, but '{branch}' owns "
                f"the {release_branch.major}.{release_branch.minor} version line of "
                f"{project!r} and may only add patches. Target the trunk "
                f"({self.config.stable}) to open a new version line.",
            )

    def _validate_trunk(self, branch: str, remote: str) -> None:
        """Reject a version on the trunk when a divergent release branch owns its line."""
        for _project_dir, project_name in get_projects():
            next_version = self._next_version(branch, project_name)
            if next_version is None:
                continue

            release_branch = ReleaseBranch.instance(
                self.config, project_name, next_version.version_line
            )
            ref = release_branch.get_ref(self.config, remote)
            if ref is None:
                # The line has not opened yet, so nothing owns it.
                continue
            if is_ancestor(ref, "HEAD"):
                # Still fast-forwardable: the trunk owns the line.
                continue

            raise ValidationError(
                ValidationCode.trunk_line_diverged,
                f"Cannot create {next_version.tag} on {branch}: ownership of the "
                f"{next_version.version.major}.{next_version.version.minor} version line of "
                f"{project_name!r} transferred to '{release_branch.name(self.config)}' "
                f"when that branch diverged from {branch}.\n"
                f"Either retarget this change at '{release_branch.name(self.config)}', "
                f"or upgrade the "
                f"commit message to 'feat:' to start a new minor version line.",
            )
