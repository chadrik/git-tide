
# Git Tide: Automated gitflow merge cycles with semantic versioning

`tide` simplifies [gitflow](https://www.atlassian.com/git/tutorials/comparing-workflows/gitflow-workflow) processes with automatic tagging and hot-fixing and promotion.

## Features

- Automatic cascading merging of hotfixes from stable branches into experimental ones, e.g. alpha, beta, rc (this is the "ebb tide")
- - Automated "promotion" of experimental branches forward to their next branch.  e.g. alpha to beta, beta to rc, and so on. (this is the "flood tide" or "flow")
- Uses [`commitizen`](https://github.com/commitizen-tools/commitizen) to create semver tags
- Avoids merge conflicts between branches by using tags to store and query the latest version


### Example flow

Here is an overview of the rules that `tide` applies during automation:

> NOTE: this document and repository use **develop**, **staging**, and **master** for it's gitflow branches 
> but this behavior is configurable in the **pyproject.toml** and **.gitlab-ci.yml** files.

- New features should start as merge requests made against the `develop` branch (this is the default branch)
- Commits that are merged to `develop` auto-generate a tag with a beta suffix: e.g. `1.0.0b1`
- Commits that are merged to `staging` auto-generate a tag with a release candidate suffix: e.g. `1.0.0rc1`
- Hotfixes that are added to `master` automatically merge to `staging`
- Hotfixes that are added to `staging` automatically merge to `develop`
- Promoting the release candidate to a new official release can be done two ways:
  - Every pipeline on the `staging` branch has a `promotion` job that can be manually run
  - A scheduled pipeline can be setup to do the same
- Either way, when the `promotion` job runs it will do 3 things:
  - Move the `master` branch to the tip of `staging` and create a new release, stripping off the `rc` suffix
  - Move the `staging` branch to the tip of `develop` and create a new release, converting `b` to `rc`
  - Restart `develop`, incrementing the minor version, e.g. from `1.1.0b4` to `1.2.0b0`. 

### Sample development cycle

Below is the Git commit graph demonstrating a sample development cycle using this project's branching strategy, 
including tags and hotfix propagation.

```plaintext
*   auto-hotfix into develop: staging: add hotfix 2 -  (HEAD -> develop, tag: 1.2.0rc0, tag: 1.2.0b2, staging)
|\  
| * staging: add hotfix 2 -  (tag: 1.1.0rc2, tag: 1.1.0, master)
* | auto-hotfix into develop: master: add hotfix -  (tag: 1.2.0b1)
|\| 
| *   auto-hotfix into staging: master: add hotfix -  (tag: 1.1.0rc1)
| |\  
| | * master: add hotfix -  (tag: 1.0.1)
* | | develop: add beta feature 2 -  (tag: 1.2.0b0)
|/ /  
* / develop: add feature 1 -  (tag: 1.1.0rc0, tag: 1.1.0b0)
|/  
* master: initial state -  (tag: 1.0.0)

chronological tag order:
1.2.0rc0        promoting develop to staging!
1.1.0           promoting staging to master!
1.2.0b2         auto-hotfix into develop: staging: add hotfix 2
1.1.0rc2        staging: add hotfix 2
1.2.0b1         auto-hotfix into develop: master: add hotfix
1.1.0rc1        auto-hotfix into staging: master: add hotfix
1.0.1           master: add hotfix
1.2.0b0         develop: add beta feature 2
1.1.0rc0        promoting develop to staging!
1.1.0b0         develop: add feature 1
1.0.0           master: initial state
```

## Setting up a repo

In order for `tide` to work its magic, the Gitlab repo needs to be properly configured:

- Add a `[tool].tide` section to your `pyproject.toml` file to define the gitflow branch names and prerelease version you wish to use.
    ```toml
    [tool.tide]
    branches.beta = "develop"
    branches.rc = "staging"
    branches.stable = "master"
    ```
- For each project within your repo that you want to manage tagged releases, add a `project` entry:
    ```toml
    [tool.tide]
    project = "project_name"
    ```
  If there is only one, then combine be sure to put all of your options under a single `[tool.tide]` section.
- Create a [Project Access Token](https://docs.gitlab.com/ee/user/project/settings/project_access_tokens.html) with the appropriate scope for being able to push tags, changes and create cicd variables.
- Run `tide init --access-token='YOUR_ACCESS_TOKEN'`
- Copy and modify the `.gitlab-ci.yml` file ensuring the branch variables match the ones in the `pyproject.toml` (TODO: automated generation of `.gitlab-ci.yml` in `tide init`)

## Development

### Setting up your local development environment

- clone the repository using either https or ssh depending on your preference and then run the following commands:
```bash
python -m venv venv
.\venv\Scripts\Activate
pip install -r requirements.txt
pre-commit install
```

### Local development info, tips, and tricks

`nox` is the primary interface for what we'll be doing day to day and under the hood.

Use `nox --list` to see the tasks that are available. 

### Running the unit tests

```bash
nox -s unit_tests
```

The primary test `tests/test_unit.py::test_dev_cycle` can be run in several modes:
- local: simulated using local repos
- remote: integration testing using real gitlab repos
- gitlab-ci-local: simulated using local repos, but using the `.gitlab-ci.yml` file for entry-point and env var control.  Requires installation of https://github.com/firecow/gitlab-ci-local

Here's how you run it in remote mode:
```bash
EXEC_MODE="remote" ACCESS_TOKEN="your-access-token-here" nox -s unit_tests -- tests/test_unit.py::test_dev_cycle -vv -s
```

Here's how you run it using `gitlab-ci-local`:
```bash
EXEC_MODE="gitlab-ci-local" nox -s unit_tests -- tests/test_unit.py::test_dev_cycle -vv -s
```

### Serving Documentation on GitLab Pages

Our project leverages GitLab Pages to host and serve the documentation generated by MkDocs. This setup ensures that all team members and stakeholders have access to the most current documentation relevant to the entire project.

#### How It Works

- **Unified Documentation:** We maintain a single, comprehensive documentation system that reflects the latest changes in our codebase.

- **Automated Builds:** The documentation is automatically built and updated whenever changes are made. This ensures that our documentation is always up-to-date with the latest codebase.

#### Benefits

- **Simplified Access:** Provides a single, consistent source of documentation for all stakeholders, simplifying access and reducing potential confusion.
- **Immediate Updates:** Changes to the documentation are immediately reflected upon updates, ensuring that the documentation always matches the current state of the project.

This streamlined approach to documentation allows our team to efficiently maintain and update project details, ensuring consistent and accessible information for everyone involved.
