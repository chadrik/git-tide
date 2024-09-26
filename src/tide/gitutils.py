from __future__ import absolute_import, print_function, annotations

import fnmatch
import subprocess
import re
import os.path
import sys

from functools import lru_cache
from typing import overload, Iterable, Match, Pattern, Iterator
from typing_extensions import Literal

cache = lru_cache(maxsize=None)

GLOB_TO_REGEX = [
    # I'm pretty sure that "**/*.py" should match a python file at the root, e.g. "setup.py"
    # this first replacement ensures that this works in pre-commit
    ("**/", ".*"),
    ("**", ".*"),
    ("*", "[^/]*"),
    (".", "[.]"),
    ("?", "."),
    ("{", "("),
    ("}", ")"),
    (",", "|"),
]
GLOB_TO_REGEX_MAP = dict(GLOB_TO_REGEX)
GLOB_TO_REGEX_REG = re.compile(
    r"({})".format("|".join(re.escape(f) for f, r in GLOB_TO_REGEX))
)

_verbose: bool = False


def set_git_verbose(enabled: bool = True) -> None:
    global _verbose
    _verbose = enabled


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
    if _verbose:
        print(cmd, file=sys.stderr)
    if capture:
        output = subprocess.run(
            cmd,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL if quiet else None,
        ).stdout.strip()
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


def checkout_remote_branch(remote: str, branch: str) -> str:
    """
    Check out a specific remote branch, creating it if it doesn't exist.

    Args:
        remote: The remote repository name
        branch: The branch name to check out.
    """
    if branch_exists(branch):
        git("branch", "--delete", branch)
    git("checkout", "--track", join(remote, branch))
    return get_latest_commit(None, branch)


def current_rev() -> str:
    """
    Get the SHA for the current git revision.
    """
    return git("rev-parse", "HEAD", capture=True)


def print_git_graph(max_count: int | None = None) -> None:
    """
    Prints the git graph in color, for debugging purposes
    """
    args = [
        "log",
        "--graph",
        "--abbrev-commit",
        "--oneline",
        "--all",
        "--decorate",
    ]
    if max_count:
        args.append(f"-n={max_count}")
    git(*args)


def branch_exists(branch: str) -> bool:
    """
    Return whether the given branch exists.

    Args:
        branch: branch name
    """
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


def glob_to_regex(pattern: str) -> str:
    """
    Convert a glob to a regular expression.

    Unlike fnmatch.translate, this handles ** syntax, which match across multiple
    directories, and ensures that * globs only match within one level.

    It currently does not work with paths that contain complex characters that
    need to be escaped.
    """

    def replace(match: Match) -> str:
        pat = match.groups()[0]
        return GLOB_TO_REGEX_MAP[pat]

    return GLOB_TO_REGEX_REG.sub(replace, pattern)


def regexes_to_regex(patterns: Iterable[str]) -> Pattern | None:
    """
    Convert a tuple of regex strings to a single regex Pattern
    """
    patterns = ["^" + p for p in patterns]
    if len(patterns) > 1:
        reg = "|\n".join("    " + p for p in patterns)
        pattern = "(?x)(\n{}\n)$".format(reg)
    elif len(patterns) == 1:
        pattern = patterns[0] + "$"
    else:
        return None

    try:
        return re.compile(pattern)
    except re.error:
        print(pattern, file=sys.stderr)
        raise


@cache
def globs_to_regex(patterns: tuple[str, ...]) -> Pattern | None:
    """
    Convert a tuple of glob patterns to a single regex Pattern.
    """
    return regexes_to_regex(glob_to_regex(p) for p in patterns)


def filter_paths_regex(
    paths: Iterable[str],
    include: Pattern | None = None,
    exclude: Pattern | None = None,
) -> Iterator[str]:
    """
    Args:
        paths: iterable of paths to filter
        include: regex include pattern
        exclude: regex exclude pattern
    """
    for path in paths:
        if (not include or include.search(path)) and (
            not exclude or not exclude.search(path)
        ):
            yield path


def filter_paths(
    paths: Iterable[str],
    include: tuple[str, ...] | None = None,
    exclude: tuple[str, ...] | None = None,
) -> list[str]:
    """
    Args:
        paths: iterable of paths to filter
        include: tuple of glob include patterns
        exclude: tuple glob include patterns
    """
    return list(
        filter_paths_regex(
            paths,
            globs_to_regex(include) if include else None,
            globs_to_regex(exclude) if exclude else None,
        )
    )


class GitRepo:
    """
    Query and filter files from git and cache results
    """

    def __init__(self, root: str):
        self.root: str = root

    @cache
    def files(self) -> list[str]:
        """
        Return all of the files in the git repo, at the current commit.
        """
        output = subprocess.check_output(["git", "ls-files"], cwd=self.root)
        return [x for x in output.decode().split("\n") if x]

    @cache
    def file_matches(
        self, include: tuple[str, ...] | None, exclude: tuple[str, ...] | None = None
    ) -> list[str]:
        """
        Return file paths in the git repo at the current commit.

        Args:
            include: tuple of glob include patterns
            exclude: tuple glob include patterns
        """
        return list(filter_paths(self.files(), include, exclude))

    @cache
    def folders(self) -> list[str]:
        """
        Return all of the folders in the git repo, at the current commit.
        """
        results = set()
        for path in self.files():
            while True:
                path = os.path.dirname(path)
                if not path:
                    break
                results.add(path)
        return sorted(results)

    @cache
    def folder_matches(
        self, include: tuple[str, ...] | None, exclude: tuple[str, ...] | None = None
    ) -> list[str]:
        """
        Return folder paths in the git repo at the current commit.

        Args:
            include: tuple of glob include patterns
            exclude: tuple glob include patterns
        """
        return list(filter_paths(self.folders(), include, exclude))
