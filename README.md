
# Gitflow + Semantic Versioning Autopilot Demo

This repo demonstrates a three-branch [gitflow](https://www.atlassian.com/git/tutorials/comparing-workflows/gitflow-workflow) structure with automatic tagging and merging.

It uses [`commitizen`](https://github.com/commitizen-tools/commitizen) to create semver tags.

## Rules

Here is an overview of the rules:

- New features should start as merge requests made against the `develop` branch (this is the default branch)
- Commits that are merged to `develop` auto-generate a tag with a beta suffix: e.g. `1.0.0b1`
- Commits that are merged to `staging` auto-generate a tag with a release candidate suffix: e.g. `1.0.0rc1`
- Hotfixes that are added to `master` automatically merge to `staging`
- Hotfixes that are added to `staging` automatically merge to `develop`
- Promoting the release candidate to a new official release can be done two ways:
  - Every pipeline on the `staging` branch has a `make-release` job that can be manually run
  - A scheduled pipeline can be setup to do the same
- Either way, when the `make-release` job runs it will do 3 things:
  - Move the `master` branch to the tip of `staging` and create a new release, stripping off the `rc` suffix
  - Move the `staging` branch to the tip of `develop` and create a new release, converting `b` to `rc`
  - Restart `develop`, incrementing the minor version, e.g. from `1.1.0b4` to `1.2.0b0`. 

## Setting up a repo

In order to work, the Gitlab repo needs to be properly configured:

- Create a [Project Access Token](https://docs.gitlab.com/ee/user/project/settings/project_access_tokens.html)
- Add a [CI variable](https://docs.gitlab.com/ee/ci/variables/#for-a-project) named `ACCESS_TOKEN` with the token value
- Add `develop` and `staging` as [protected branches](https://docs.gitlab.com/ee/user/project/protected_branches.html#add-protection-to-existing-branches) and set "Allowed to push and merge" to "Developers + Maintainers"
- Under "Protected branches", edit `master` and set "Allowed to push and merge" to "Developers + Maintainers"
- Create a scheduled pipeline and create a variable `SCHEDULED_JOB_NAME=make-release`.  You can leave "Activated" unchecked to trigger the job manually.
