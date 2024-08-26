from __future__ import absolute_import, print_function, annotations

import argparse
import glob

import nox
import os
import shutil


HERE = os.path.dirname(__file__)


@nox.session(reuse_venv=True)
def lint(session: nox.Session) -> None:
    """
    Lint the project's codebase.

    This installs and then runs pre-commit for all files. This is intended for manual usage ie:

    nox -s lint

    Args:
        session: The Nox session context.
    """
    session.install("pre-commit==3.6.2")
    session.run(
        "pre-commit",
        "run",
        "--all-files",
        "--show-diff-on-failure",
        "--hook-stage=manual",
    )


@nox.session(reuse_venv=True)
def ruff_lint(session: nox.Session) -> None:
    """
    Lint the project's codebase.

    Args:
        session: The Nox session context.
    """
    session.install("ruff==0.5.4")
    session.run("ruff", "check", "--fix", *session.posargs)


@nox.session(reuse_venv=True)
def ruff_format(session: nox.Session) -> None:
    """
    Format the project's codebase.

    This session installs necessary dependencies for code formatting and runs the formatter
    to check (and optionally correct) the code format according to the project's style guide.

    Args:
        session: The Nox session context.
    """
    session.install("ruff==0.5.4")
    session.run("ruff", "format", *session.posargs)


# @nox.session(reuse_venv=True)
# def yaml_lint(session: nox.Session) -> None:
#     """
#     Lint YAML files in the project.
#
#     This session installs dependencies necessary for YAML linting and runs a linter against the project's
#     YAML files to ensure they are well-formed and adhere to specified standards and best practices.
#
#     Args:
#         session (nox.Session): The Nox session being run, providing context and methods for session actions.
#     """
#     session.install("-r", "nox-tasks-requirements.txt")
#     posargs = session.posargs
#     if not posargs:
#         posargs = ["."]
#     session.run("yamllint", "-c", ".yamllint", "-f", "parsable", *posargs)


@nox.session(reuse_venv=True)
def type_hints(session: nox.Session) -> None:
    """
    Check type hints in the project's codebase.

    This session installs necessary dependencies for type checking and runs a static type checker
    to validate the type hints throughout the project's codebase, ensuring they are correct and consistent.

    Args:
        session: The Nox session context.
    """
    session.install("mypy==1.9.0")
    session.run("mypy", *session.posargs)


@nox.session(reuse_venv=True)
def unit_tests(session: nox.Session) -> None:
    """
    Run the project's unit tests.

    This session installs the necessary dependencies and runs the project's unit tests.
    It is focused on testing the functionality of individual units of code in isolation.

    Args:
        session: The Nox session context.
    """
    session.install("pytest==8.1.1")
    # [init] extra (i.e. gitlab) is not installed by default because it's a heavy dep
    # that slows down Gitlab jobs
    session.install("-e", ".[init]")

    # Default arguments for pytest
    default_args = ["-v", "-m", "unit"]

    # Combine default arguments with any additional args provided
    pytest_args = default_args + list(session.posargs)

    session.run("pytest", *pytest_args)


@nox.session(reuse_venv=True)
def smoke_tests(session: nox.Session) -> None:
    """
    Run the project's smoke tests.

    Args:
        session: The Nox session context.
    """
    session.install("pytest==8.1.1")
    session.install("-e", ".[init]")

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
        session: The Nox session context.

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
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--build-dir",
        default="public",
        help="Directory where the documentation should be built.",
    )
    parser.add_argument(
        "--serve", action="store_true", help="Serve the documentation after building."
    )

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


def tide(session: nox.Session, *args: str) -> None:
    # do not install the [init] extras
    if "CI" in os.environ:
        session.install("-e", ".")
    else:
        session.install(".")
    session.run("python", "-m", "tide", *(list(args) + session.posargs))


@nox.session(tags=["ci"], reuse_venv=True)
def autotag(session: nox.Session) -> None:
    """
    Automatically tag the current branch with a new version number and push the tag to
    the remote repository.

    Args:
        session: The Nox session context.
    """
    tide(session, "autotag")


@nox.session(tags=["ci"], reuse_venv=True)
def hotfix(session: nox.Session) -> None:
    """
    Handle automatic hotfix merging from a feature branch back to its upstream branch.

    Args:
        session: The Nox session context.
    """
    tide(session, "hotfix")


@nox.session(tags=["ci"], reuse_venv=True)
def promote(session: nox.Session) -> None:
    """
    Promote changes through the branch hierarchy from pre-release branches to the stable branch.

    Args:
        session: The Nox session context.
    """
    tide(session, "promote")


@nox.session
def build(session: nox.Session) -> None:
    os.removedirs("dist")
    session.run(
        "uvx", "--from", "build", "pyproject-build", "--installer", "uv", external=True
    )


@nox.session
def publish(session: nox.Session) -> None:
    whl = sorted(glob.glob("./dist/git_tide-*-py3-none-any.whl"))[0]
    print(whl)
    session.run("uvx", "twine", "upload", whl, external=True)
