from __future__ import absolute_import, print_function, annotations

import os
import re
import subprocess
import nox

BRANCHES = ["develop", "staging", "master"]
BRANCH_TO_PRERELEASE = {
    "master": None,
    "staging": "rc",
    "develop": "beta",
}


def git(*args, **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git"] + list(args), text=True, check=True, **kwargs)


def current_branch() -> str:
    try:
        return os.environ["CI_COMMIT_BRANCH"]
    except KeyError:
        result = git("branch", "--show-current", stdout=subprocess.PIPE)
        branch = result.stdout.strip()
        assert branch
        return branch


def checkout(remote: str | None, branch: str) -> None:
    args = ["checkout"]
    if remote:
        args += ["--track"]
    args += [join(remote, branch)]
    git(*args)


def _get_tag(session: nox.Session, *args: str) -> str:
    output = session.run(*(["cz", "bump"] + list(args) + ["--dry-run"]), silent=True)
    return re.search('tag to create: (.*)', output).groups()[0]


def get_upstream_branch(branch) -> str | None:
    index = BRANCHES.index(branch)
    index -= 1
    if index > 0:
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
    return _get_tag(session, *args)


def join(remote: str | None, branch: str) -> str:
    if remote:
        return f"{remote}/{branch}"
    else:
        return branch


def _ci_setup_remote() -> str | None:
    try:
        url = os.environ["CI_REPOSITORY_URL"]
    except KeyError:
        return None

    branch = "gitlab_origin"
    try:
        access_token = os.environ["ACCESS_TOKEN"]
    except KeyError:
        print("You must setup a CI variable in the Gitlab process called ACCESS_TOKEN")
        print("See https://docs.gitlab.com/ee/ci/variables/#for-a-project")
        raise ValueError

    git("config", "user.email", "fake@email.com")
    git("config", "user.name", "ci-bot")
    url = url.split("@")[-1]
    git("remote", "add", branch, f"https://oauth2:{access_token}@{url}")
    return branch


@nox.session
def ci_autotag(session: nox.Session):
    session.install("-r", "requirements.txt")
    remote = _ci_setup_remote()
    branch = current_branch()

    # Auto-tag
    tag = get_tag_for_branch(session, branch)
    print(f"Creating new tag {tag}")
    git("tag", tag)
    if remote:
        git("push", remote, tag, "-o=ci.skip")


@nox.session
def ci_automerge(session: nox.Session):
    remote = _ci_setup_remote()
    branch = current_branch()
    upstream_branch = get_upstream_branch(branch)
    if not upstream_branch:
        return

    # Auto-merge
    # Record the current state
    git("checkout", "-B", f"{branch}_temp")

    if remote:
        # Fetch and checkout the upstream branch
        git("fetch", remote, upstream_branch)

    checkout(remote, upstream_branch)

    msg = f"Auto-merge {branch} into {upstream_branch}"
    print(msg)

    try:
        git("merge", f"{branch}_temp", "-m", msg)
    except subprocess.CalledProcessError:
        print("Conflicts:")
        git("diff", "--name-only", "--diff-filter=U")
        raise

    if remote:
        # this will trigger a full pipeline for upstream_branch, and potentially another auto-merge
        git("push", remote, upstream_branch)
    else:
        git("checkout", branch)
        git("branch", "-D", f"{branch}_temp")


@nox.session
def ci_release(session: nox.Session):

    def short_version(tag):
        return tag.rsplit('.', 1)[0]

    session.install("-r", "requirements.txt")

    remote = _ci_setup_remote()
    if remote:
        git("fetch", remote)

    # Release time!
    # merge staging to master
    print("Releasing staging to master!")
    checkout(remote, "master")
    git("merge", join(remote, "staging"), "-m", "Release staging to master")
    master_tag = get_tag_for_branch(session, "master")
    git("commit", "--allow-empty", "-m", f"New release! {short_version(master_tag)}")
    git("tag", master_tag)

    # start a new beta cycle
    print("Setting up new develop branch for beta development")
    checkout(remote, "develop")
    beta_tag = get_tag_for_branch(session, "develop")
    git("commit", "--allow-empty", "-m", f"Starting beta development for {short_version(beta_tag)}")
    git("tag", beta_tag)

    # develop becomes release candidate
    print("Converting develop branch into release candidate")
    checkout(remote, "staging")
    git("merge", join(remote, "develop"), "-m", "Release develop to staging")
    rc_tag = get_tag_for_branch(session, "staging")
    git("commit", "--allow-empty", "-m", f"Starting release candidate for {short_version(rc_tag)}")
    git("tag", rc_tag)

    if remote:
        # git push --atomic gitlab_origin master staging develop "$MASTER_TAG" "$RC_TAG" "$BETA_TAG" -o ci.skip
        # FIXME: We want to trigger test/deploy jobs for these new versions, but we want to skip auto-merge
        git("push", "--atomic", remote, "master", master_tag, "-o=ci.skip")
        git("push", "--atomic", remote, "staging", rc_tag, "-o=ci.skip")
        git("push", "--atomic", remote, "develop", beta_tag, "-o ci.skip")
