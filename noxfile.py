from __future__ import absolute_import, print_function, annotations

import argparse
import nox
import os
import re
import requests
import shutil
import subprocess
import tomllib
from typing import List, Optional, Dict, Any


HOTFIX_MESSAGE = "auto-hotfix into {upstream_branch}: {message}"
HERE = os.path.dirname(__file__)


def load_gitflow_config() -> Dict[str, Any]:
    """
    Load and return the GitFlow configuration from the pyproject.toml file.

    Returns:
        A dictionary containing the configuration of GitFlow, including a first_tag for the repository
        and names and prereleases for each branch.
    """
    with open(f"{HERE}/pyproject.toml", "rb") as f:
        config = tomllib.load(f)
    return config.get("tool", {}).get("gitflow", {})


# Load the configuration
gitflow_config = load_gitflow_config()

# Initialize dictionaries and variables
BRANCHES = []
BRANCH_TO_PRERELEASE = {}
STABLE = "master"
RC = "staging"
BETA = "develop"

# Loop through branches once and extract all needed information
for branch in gitflow_config.get("branches", []):
    branch_name = branch["name"]
    prerelease = branch["prerelease"]

    # Append branch name to BRANCHES list
    BRANCHES.append(branch_name)

    # Update BRANCH_TO_PRERELEASE dictionary
    BRANCH_TO_PRERELEASE[branch_name] = None if prerelease == "None" else prerelease

    # Determine if the branch is STABLE, RC, or BETA
    if prerelease == "None":
        STABLE = branch_name
    elif prerelease == "rc":
        RC = branch_name
    elif prerelease == "beta":
        BETA = branch_name


def get_parser() -> argparse.ArgumentParser:
    """
    Create and return an argument parser for the script.

    Returns:
        An argparse.ArgumentParser instance with defined options for command-line usage.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--build-dir",
        default="public",
        help="Directory where the documentation should be built.",
    )
    parser.add_argument(
        "--serve", action="store_true", help="Serve the documentation after building."
    )
    parser.add_argument(
        "--annotation",
        default="automatic change detected",
        help="Message to pass for tag annotations.",
    )
    return parser


def git(*args, **kwargs) -> subprocess.CompletedProcess:
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
    args = [str(arg).strip() for arg in args]
    return subprocess.run(["git"] + args, text=True, check=True, **kwargs)


def current_branch() -> str:
    """
    Get the current git branch name.

    Returns:
        The name of the current branch.

    Raises:
        KeyError: If the 'CI_COMMIT_BRANCH' environment variable is not set and not in a git directory.
        AssertionError: If the current branch name is not obtainable or empty.
    """
    try:
        return os.environ["CI_COMMIT_BRANCH"]
    except KeyError:
        result = git("branch", "--show-current", stdout=subprocess.PIPE)
        branch = result.stdout.strip()
        assert branch
        return branch


def get_branches() -> List[str]:
    """
    Retrieve a list of all local branches in the git repository.

    Returns:
        A list of branch names.
    """
    result = git("branch", stdout=subprocess.PIPE)
    return [x.split()[-1] for x in result.stdout.splitlines()]


def checkout(remote: Optional[str], branch: str, create: bool = False) -> None:
    """
    Check out a specific branch, optionally creating it if it doesn't exist.

    Args:
        remote: The name of the remote repository.
        branch: The branch name to check out.
        create: Whether to create the branch if it does not exist (default is False).
    """
    args = ["checkout"]
    if create:
        args += ["-b"]
    args += [join(remote, branch)]
    if remote:
        args += ["--track"]
    git(*args)


def get_upstream_branch(session: nox.Session, branch: str) -> Optional[str]:
    """
    Determine the upstream branch for a given branch name based on configuration.

    Args:
        session: The nox.Session object representing the current session.
        branch: The name of the branch for which to find the upstream branch.

    Returns:
        The name of the upstream branch, or None if there is no upstream branch.

    Raises:
        ValueError: If the branch is not found in the configuration.
    """
    try:
        index = BRANCHES.index(branch)
    except ValueError:
        session.error(f"Invalid branch: {branch}")

    if index > 0:
        return BRANCHES[index - 1]
    else:
        return None


def create_gitlab_ci_variable(base_url: str, access_token: str, key: str, value: str):
    """
    Create a GitLab CI variable in the specified project.

    Args:
        base_url (str): The base URL to the GitLab project's API for variables.
        access_token (str): Access token for authentication with the GitLab API.
        key (str): The name of the variable to create.
        value (str): The value of the variable to set.

    Raises:
        HTTPError: An error occurred from the HTTP request if the response has an issue.
    """
    headers = {"PRIVATE-TOKEN": access_token}
    post_response = requests.post(
        base_url, headers=headers, json={"key": key, "value": value}
    )
    post_response.raise_for_status()


def update_gitlab_ci_variable(key: str, value: str):
    """
    Update a GitLab CI variable, or create it if it does not exist.

    Args:
        key (str): The name of the CI variable to update.
        value (str): The new value to assign to the CI variable.

    Raises:
        EnvironmentError: An error if the ACCESS_TOKEN environment variable is not set.
        HTTPError: An error occurred from the HTTP request if the response has an issue.
    """
    access_token = os.getenv("ACCESS_TOKEN")
    if not access_token:
        raise EnvironmentError("ACCESS_TOKEN environment variable is not set.")

    headers = {"PRIVATE-TOKEN": access_token}
    base_url = (
        f"{os.getenv('CI_API_V4_URL')}/projects/{os.getenv('CI_PROJECT_ID')}/variables"
    )

    put_response = requests.put(
        f"{base_url}/{key}", headers=headers, json={"value": value}
    )

    if put_response.status_code == 404:
        create_gitlab_ci_variable(base_url, access_token, key, value)

    put_response.raise_for_status()


def get_latest_commit(branch_name: str, remote: Optional[str]) -> str:
    """
    Fetch the latest commit hash from a specific branch.

    Args:
        branch_name (str): The name of the branch to fetch the latest commit from.
        remote (Optional[str]): The name of the remote repository. If None, the local repository is used.

    Returns:
        str: The latest commit hash of the specified branch.

    Raises:
        subprocess.CalledProcessError: If the git command fails.
    """
    if remote:
        git("fetch", "origin", branch_name)
    return git(
        "rev-parse", join(remote, branch_name), stdout=subprocess.PIPE
    ).stdout.strip()


def store_beta_cycle_start_commit() -> None:
    """
    Store the latest commit hash of the beta branch as a GitLab CI variable.

    This function retrieves the latest commit hash for the beta branch and updates or creates a
    GitLab CI variable to track the start of the current beta cycle.

    Raises:
        HTTPError: If the HTTP request to update or create the variable fails.
    """
    headers = {
        "PRIVATE-TOKEN": os.getenv("ACCESS_TOKEN"),
    }
    remote = get_remote()
    latest_commit = get_latest_commit(f"{BETA}", remote)

    if remote:
        base_url = f"{os.getenv('CI_API_V4_URL')}/projects/{os.getenv('CI_PROJECT_ID')}/variables"
        data = {"value": latest_commit}
        key = "BETA_CYCLE_START_COMMIT"

        put_response = requests.put(base_url + f"/{key}", headers=headers, data=data)

        if put_response.status_code == 404:
            data["key"] = key
            post_response = requests.post(base_url, headers=headers, data=data)
            post_response.raise_for_status()


def get_tag_for_branch(session: nox.Session, branch: str) -> str:
    """
    Determine the appropriate new tag for a given branch based on the latest changes.

    This function uses the Commitizen tool to determine the next tag name for a branch, potentially
    adjusting for pre-release tags and minor version increments.

    Args:
        session (nox.Session): The Nox session within which the command is run.
        branch (str): The name of the branch for which to generate the tag.

    Returns:
        str: The new tag to be created.

    Raises:
        RuntimeError: If the command does not generate an output or fails to determine the tag.
    """
    prerelease = BRANCH_TO_PRERELEASE[branch]

    increment = "patch"

    # Only apply minor increment beta
    if branch == BETA:
        last_default_commit = os.getenv("BETA_CYCLE_START_COMMIT")
        commit_before_sha = os.getenv("CI_COMMIT_BEFORE_SHA")
        if (last_default_commit and commit_before_sha) and (
            last_default_commit == commit_before_sha
        ):
            increment = "minor"

    args = [f"--increment={increment}"]
    if prerelease:
        args += ["--prerelease", prerelease]

    if increment == "minor":
        args += ["--increment-mode=exact"]

    output = session.run(
        *(["cz", "bump"] + list(args) + ["--dry-run", "--yes"]), silent=True
    )
    match = re.search("tag to create: (.*)", output)

    if not match:
        session.error(output)

    tag = match.group(1).strip()

    return tag


def join(remote: Optional[str], branch: str) -> str:
    """
    Construct a full branch path with remote prefix if specified.

    Args:
        remote (Optional[str]): The remote repository name, or None if local.
        branch (str): The branch name.

    Returns:
        str: The full path to the branch, prefixed by the remote name if specified.
    """
    if remote:
        return f"{remote}/{branch}"
    else:
        return branch


def get_remote() -> Optional[str]:
    """
    Configure and retrieve the name of a Git remote for use in CI environments.

    This function configures a Git remote named 'gitlab_origin' using environment variables
    that should be set in the GitLab CI/CD environment. It configures the git user credentials,
    splits the repository URL to format it with the access token, and adds the remote to the
    local git configuration.

    Returns:
        Optional[str]: The name of the configured remote ('gitlab_origin') if in CI mode, otherwise None.

    Raises:
        KeyError: If the 'CI_REPOSITORY_URL' is not set, indicating that the environment is not a CI environment.
        ValueError: If the 'ACCESS_TOKEN' is not set, necessary for accessing the GitLab repository.
    """
    try:
        url = os.environ["CI_REPOSITORY_URL"]
    except KeyError:
        # return None to indicate we're not in CI mode.  would be safer to make this explicit!
        return None

    remote = "gitlab_origin"
    try:
        access_token = os.environ["ACCESS_TOKEN"]
    except KeyError:
        print("You must setup a CI variable in the Gitlab process called ACCESS_TOKEN")
        print("See https://docs.gitlab.com/ee/ci/variables/#for-a-project")
        raise ValueError

    git("config", "user.email", "fake@email.com")
    git("config", "user.name", "ci-bot")
    url = url.split("@")[-1]
    git("remote", "add", remote, f"https://oauth2:{access_token}@{url}")
    return remote


@nox.session(reuse_venv=True)
def lint(session: nox.Session) -> None:
    """
    Lint the project's codebase.

    This session installs necessary dependencies for linting and then runs pre-commit
    for all the files. This is intended for manual usage ie:

    nox -s lint

    Args:
        session (nox.Session): The Nox session being run, providing context and methods for session actions.
    """
    session.install("-r", "nox-tasks-requirements.txt")
    session.run(
        "pre-commit",
        "run",
        "--all-files",
        "--show-diff-on-failure",
        "--hook-stage=manual",
        *session.posargs,
    )


@nox.session(reuse_venv=True)
def ruff_lint(session: nox.Session) -> None:
    """
    Lint the project's codebase.

    This session installs necessary dependencies for linting and then runs the linter to check for
    stylistic errors and coding standards compliance across the project's codebase.

    Args:
        session (nox.Session): The Nox session being run, providing context and methods for session actions.
    """
    session.install("-r", "nox-tasks-requirements.txt")
    session.run("ruff", "check", "--fix", *session.posargs)


@nox.session(reuse_venv=True)
def ruff_format(session: nox.Session) -> None:
    """
    Format the project's codebase.

    This session installs necessary dependencies for code formatting and runs the formatter
    to check (and optionally correct) the code format according to the project's style guide.

    Args:
        session (nox.Session): The Nox session being run, providing context and methods for session actions.
    """
    session.install("-r", "nox-tasks-requirements.txt")
    session.run("ruff", "format", *session.posargs)


@nox.session(reuse_venv=True)
def yaml_lint(session: nox.Session) -> None:
    """
    Lint YAML files in the project.

    This session installs dependencies necessary for YAML linting and runs a linter against the project's
    YAML files to ensure they are well-formed and adhere to specified standards and best practices.

    Args:
        session (nox.Session): The Nox session being run, providing context and methods for session actions.
    """
    session.install("-r", "nox-tasks-requirements.txt")
    posargs = session.posargs
    if not posargs:
        posargs = ["."]
    session.run("yamllint", "-c", ".yamllint", "-f", "parsable", *posargs)


@nox.session(reuse_venv=True)
def type_hints(session: nox.Session) -> None:
    """
    Check type hints in the project's codebase.

    This session installs necessary dependencies for type checking and runs a static type checker
    to validate the type hints throughout the project's codebase, ensuring they are correct and consistent.

    Args:
        session (nox.Session): The Nox session being run, providing context and methods for session actions.
    """
    session.install("-r", "nox-tasks-requirements.txt")
    session.run("mypy", *session.posargs)


@nox.session(reuse_venv=True)
def unit_tests(session: nox.Session) -> None:
    """
    Run the project's unit tests.

    This session installs the necessary dependencies and runs the project's unit tests.
    It is focused on testing the functionality of individual units of code in isolation.

    Args:
        session (nox.Session): The Nox session being run, providing context and methods for session actions.
    """
    session.install("-r", "nox-tasks-requirements.txt")
    # Default arguments for pytest
    default_args = ["-v"]

    # If no additional arguments are provided, add the "-m unit" option
    if not session.posargs:
        default_args.extend(["-m", "unit"])

    # Combine default arguments with any additional args provided
    pytest_args = default_args + list(session.posargs)

    session.run("pytest", *pytest_args)


@nox.session(reuse_venv=True)
def smoke_tests(session: nox.Session) -> None:
    """
    Run the project's smoke tests.

    This session installs the necessary dependencies and runs a subset of tests designed to quickly
    check the most important functions of the program, often as a prelude to more thorough testing.

    Args:
        session (nox.Session): The Nox session being run, providing context and methods for session actions.
    """
    session.install("-r", "nox-tasks-requirements.txt")
    # Default arguments for pytest, runs all tests marked with 'smoke'
    default_args = ["-v", "-m", "smoke"]

    # Combine default arguments with any additional args provided
    pytest_args = default_args + list(session.posargs)

    session.run("pytest", *pytest_args)


@nox.session(reuse_venv=True)
def docs(session: nox.Session) -> None:
    """
    Builds and optionally serves the project documentation using MkDocs.

    This session installs dependencies from `requirements.txt`, copies markdown files
    from the 'docs' directory to a specified `build_dir`, and builds the static site.
    It can also serve the site locally for development purposes.

    Args:
        session (nox.Session): The Nox session object, used to execute shell commands
            and manage the session environment.

    Command-line Arguments:
        --serve: If provided, the documentation will be served after building,
                 accessible via `localhost:8000`.
        --build-dir <path>: Optional. Specifies a custom directory to build the documentation.
                            If omitted, it defaults to '<repository_root>/public'.

    The session function handles:
    - Installation of Python packages specified in `requirements.txt`.
    - Creation of the documentation build directory if it does not exist.
    - Copying of all files from the original 'docs' directory to the build directory.
    - Building of the static site using MkDocs with the `mkdocs build` command.
    - Serving the site locally with `mkdocs serve` for development previews if `--serve` is used.

    Examples:
        nox -s docs -- --build-dir=./custom_build_dir
        nox -s docs -- --serve
    """
    parser = get_parser()
    args = parser.parse_args(session.posargs)

    session.install("-r", "requirements.txt")

    build_dir = args.build_dir or os.path.join(HERE, "public")

    # Original docs directory
    original_docs_dir = os.path.join(HERE, "docs")

    # Ensure the build directory exists
    if not os.path.exists(build_dir):
        os.makedirs(build_dir, exist_ok=True)

    shutil.copyfile(
        os.path.join(HERE, "README.md"), os.path.join(original_docs_dir, "README.md")
    )

    # Copy the entire contents of the docs directory to the build directory
    for item in os.listdir(original_docs_dir):
        src = os.path.join(original_docs_dir, item)
        dst = os.path.join(build_dir, item)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)

    # Build the documentation
    session.run("mkdocs", "build", "--clean", "--site-dir", build_dir)

    # Serve the documentation if requested
    if args.serve:
        session.run("mkdocs", "serve", "--dev-addr", "localhost:8000")


@nox.session(tags=["ci"], reuse_venv=True)
def autotag(session: nox.Session):
    """
    Automatically tag the current branch with a new version number and push the tag to the remote repository.

    Args:
        session (nox.Session): The Nox session context.

    This session uses command-line arguments to define the annotation for the tag and uses the Commitizen tool
    to determine the next tag based on conventional commits. It then tags the branch and pushes the tag to the remote.
    """
    parser = get_parser()
    # You need to parse the arguments passed to the Nox session
    args = parser.parse_args(session.posargs)

    annotation = args.annotation  # Now using argparse to get the increment type
    session.install("-r", "nox-tasks-requirements.txt")

    branch = current_branch()
    remote = get_remote()

    # Auto-tag
    tag = get_tag_for_branch(session, branch)

    session.log(f"Creating new tag: {tag} on branch: {branch}")
    git("tag", "-a", tag, "-m", annotation)

    if remote:
        session.log(f"Pushing {tag} to remote")
        git("push", "origin", tag)


@nox.session(tags=["ci"], reuse_venv=True)
def hotfix(session: nox.Session) -> None:
    """
    Handle automatic hotfix merging from a feature branch back to its upstream branch.

    Args:
        session (nox.Session): The Nox session context.

    This session checks if there is an upstream branch and performs an automatic merge from the
    current branch. If conflicts arise, the session logs the conflicts and fails. If successful,
    it pushes the changes to the remote.
    """
    remote = get_remote()
    branch = current_branch()
    upstream_branch = get_upstream_branch(session, branch)
    if not upstream_branch:
        session.log(f"No branch upstream from {branch}. Skipping auto-merge")
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

    checkout(remote, upstream_branch)

    msg = HOTFIX_MESSAGE.format(upstream_branch=upstream_branch, message=message)
    session.log(msg)

    try:
        git("merge", f"{branch}_temp", "-m", msg)
    except subprocess.CalledProcessError:
        session.warn("Conflicts:")
        git("diff", "--name-only", "--diff-filter=U")
        raise

    if remote:
        # this will trigger a full pipeline for upstream_branch, and potentially another auto-merge
        session.log(f"Pushing {upstream_branch} to {remote}")
        git(
            "push",
            remote,
            upstream_branch,
            "-o",
            f"ci.variable=GITFLOW_TAG_ANNOTATION={msg}",
        )
    else:
        session.posargs.append(f"--annotation={msg}")
        autotag(session)
        # local mode: cleanup
        git("branch", "-D", f"{branch}_temp")


@nox.session(tags=["ci"], reuse_venv=True)
def promote(session: nox.Session) -> None:
    """
    Promote changes through the branch hierarchy from beta to RC to stable.

    Args:
        session (nox.Session): The Nox session context.

    This session promotes changes upstream through defined branches, handling merge operations
    and triggering deployment/testing pipelines with new versions. Each branch promotion checks
    for the presence of the branch, merges, and pushes changes.
    """
    session.install("-r", "nox-tasks-requirements.txt")

    remote = get_remote()
    session.log(f"remote = {remote}")
    if remote:
        session.log(f"fetching remote = {remote}")
        git("fetch", remote)

    all_branches = get_branches()

    def promote_branch(branch: str, log_msg: str):
        session.log(log_msg)
        upstream_branch = get_upstream_branch(session, branch)
        # FIXME: raise an error by default if branch does not exist?  provide option to allow this only during bootstrapping/testing
        if upstream_branch and upstream_branch not in all_branches:
            session.warn(
                f"upstream_branch '{upstream_branch}' is not in all_branches: [{all_branches}]"
            )
            return

        checkout(remote, branch, create=branch not in all_branches)
        if upstream_branch:
            git("merge", join(remote, upstream_branch), "-m", f"{log_msg}")

        if remote:
            # Trigger test/tag jobs for these new versions, but skip auto-hotfix
            git(
                "push",
                "--atomic",
                remote,
                branch,
                "-o",
                "ci.variable=GITFLOW_SKIP_HOTFIX=true",
                "-o",
                f"ci.variable=GITFLOW_TAG_ANNOTATION={log_msg}",
            )
        else:
            if upstream_branch:
                session.posargs.append(f"--annotation={log_msg}")
                autotag(session)

    # Promotion time!
    promote_branch(
        STABLE,
        f"promoting {RC} to {STABLE}!",
    )
    promote_branch(
        RC,
        f"promoting {BETA} to {RC}!",
    )
    promote_branch(
        BETA,
        "starting new beta cycle.",
    )

    # we store the final commit from the beta branch. this way subsequent
    # jobs that run on the beta branch can compare the CI_COMMIT_BEFORE_SHA
    # variable with this one and if they are the same, then we know it's the first
    # change of a new cycle, and we need to increment our minor semver component.
    store_beta_cycle_start_commit()
