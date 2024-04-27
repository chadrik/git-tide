from __future__ import absolute_import, print_function, annotations

import argparse
import os
import re
import subprocess
import nox
from dataclasses import dataclass

AUTO_MERGE_MESSAGE = "Auto-merge into {upstream_branch}: {message}"
BRANCHES = ["develop", "staging", "master"]
BRANCH_TO_PRERELEASE = {
    "master": None,
    "staging": "rc",
    "develop": "beta",
}


@dataclass
class Branch:
    name: str
    pre_release: str | None
    downstream_branch: str | None
    release_message: str


# BRANCHES = [
#     Branch(
#         name="develop",
#         pre_release="beta",
#         downstream_branch="staging",
#         release_message="Starting beta development for {short_tag}",
#     ),
#     Branch(
#         name="staging",
#         pre_release="rc",
#         downstream_branch="master",
#         release_message="Starting release candidate for {short_tag}",
#     ),
#     Branch(
#         name="master",
#         pre_release=None,
#         downstream_branch=None,
#         release_message="New release! {short_tag}",
#     ),
# ]


def get_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--increment", type=str.lower, default="patch", choices=["patch", "minor", "major"],
                        help="Type of version increment to perform.")
    return parser


def git(*args, **kwargs):
    # Convert all args to string and strip possible carriage returns
    args = [str(arg).strip() for arg in args]
    return subprocess.run(["git"] + args, text=True, check=True, **kwargs)


def current_branch() -> str:
    try:
        return os.environ["CI_COMMIT_BRANCH"]
    except KeyError:
        result = git("branch", "--show-current", stdout=subprocess.PIPE)
        branch = result.stdout.strip()
        assert branch
        return branch


def get_branches() -> list[str]:
    result = git("branch", stdout=subprocess.PIPE)
    return [x.split()[-1] for x in result.stdout.splitlines()]


def checkout(remote: str | None, branch: str, create=False) -> None:
    args = ["checkout"]
    if create:
        args += ["-b"]
    if remote:
        args += ["--track"]
    args += [join(remote, branch)]
    git(*args)


def get_upstream_branch(session: nox.Session, branch: str) -> str | None:
    try:
        index = BRANCHES.index(branch)
    except ValueError:
        session.error(f"Invalid branch: {branch}")
    index -= 1
    if index >= 0:
        return BRANCHES[index]
    return None


def get_tag_for_branch(session, branch, increment="patch") -> str:
    prerelease = BRANCH_TO_PRERELEASE[branch]
    if prerelease:
        args = ["--prerelease", prerelease]
    else:
        args = []
    if increment == "patch":
        args += ["--increment=PATCH"]
    elif increment == "minor":
        # always use exact-mode for minor bumps
        args += ["--increment=MINOR", "--increment-mode=exact"]
    else:
        raise TypeError(increment)
    output = session.run(*(["cz", "bump"] + list(args) + ["--dry-run"]), silent=True)
    return re.search('tag to create: (.*)', output).groups()[0]


def join(remote: str | None, branch: str) -> str:
    if remote:
        return f"{remote}/{branch}"
    else:
        return branch


def get_remote() -> str | None:
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


@nox.session(tags=["ci"])
def ci_autotag(session: nox.Session):
    parser = get_parser()
    # You need to parse the arguments passed to the Nox session
    args = parser.parse_args(session.posargs)

    increment = args.increment  # Now using argparse to get the increment type

    session.install("-r", "requirements.txt")
    remote = get_remote()
    branch = current_branch()

    # Auto-tag
    tag = get_tag_for_branch(session, branch, increment=increment)
    session.log(f"Creating new tag {tag}")
    git("tag", tag)
    if remote:
        session.log(f"Pushing {tag} to {remote}")
        git("push", remote, tag, "-o=ci.skip")


@nox.session(tags=["ci"])
def ci_automerge(session: nox.Session):
    remote = get_remote()
    branch = current_branch()
    upstream_branch = get_upstream_branch(session, branch)
    if not upstream_branch:
        session.log(f"No branch upstream from {branch}. Skipping auto-merge")
        return

    # Auto-merge
    # Record the current state
    message = git("log", "--pretty=format: %s",  "-1", stdout=subprocess.PIPE).stdout.strip()
    # strip out previous Automerge formatting
    match = re.match(AUTO_MERGE_MESSAGE.format(upstream_branch="[^:]+", message="(.*)$"), message)
    if match:
        message = match.groups()[0]

    git("checkout", "-B", f"{branch}_temp")

    if remote:
        # Fetch the upstream branch
        git("fetch", remote, upstream_branch)

    checkout(remote, upstream_branch)

    msg = AUTO_MERGE_MESSAGE.format(upstream_branch=upstream_branch, message=message)
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
        git("push", remote, upstream_branch)
    else:
        # local mode: cleanup
        git("branch", "-D", f"{branch}_temp")


@nox.session(tags=["ci"])
def ci_release(session: nox.Session):

    def short_version(tag):
        return tag.rsplit('.', 1)[0]

    session.install("-r", "requirements.txt")

    remote = get_remote()
    if remote:
        git("fetch", remote)

    all_branches = get_branches()

    def cascade(branch: str, log_msg: str, release_msg: str):
        session.log(log_msg)
        upstream_branch = get_upstream_branch(session, branch)
        # FIXME: raise an error by default if branch does not exist?  provide option to allow this only during bootstrapping/testing
        if upstream_branch and upstream_branch not in all_branches:
            return

        checkout(remote, branch, create=branch not in all_branches)
        if upstream_branch:
            git("merge", join(remote, upstream_branch), "-m", f"Release {upstream_branch} to {branch}")
            increment = "patch"
        else:
            increment = "minor"

        # Get the tag for informational purposes only. Tagging will be triggered by push.
        tag = get_tag_for_branch(session, branch, increment=increment)
        git("commit", "--allow-empty", "-m", f"{release_msg} {short_version(tag)}")
        if remote:
            # Trigger test/deploy jobs for these new versions, but skip auto-merge
            git(
                "push", "--atomic", remote, branch,
                "-o", f"ci.variable=AUTOPILOT_INCREMENT={increment}",
                "-o", "ci.variable=AUTOPILOT_SKIP_AUTOMERGE=true",
            )

    # Release time!
    cascade("master",
            "Releasing staging to master!",
            "New release!")
    cascade("staging",
            "Converting develop branch into release candidate",
            "Starting release candidate for")
    cascade("develop",
            "Setting new develop branch for beta development",
            "Starting beta development for")
