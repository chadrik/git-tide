
# Gitflow + Semantic Versioning Autopilot Demo

This repo demonstrates a three-branch gitflow structure with automatic tagging and merging.

## Rules

Here is an overview of the rules:

- Merge requests are made against the `develop` branch (this is the default)
- The `develop` branch is manually merged to `staging` and then to `master` on a schedule (weekly, fortnightly, etc)
- Commits merged to `develop` auto-generate a tag with a beta suffix: e.g. `1.0.0b1`
- Commits merged to `staging` auto-generate a tag with a release candidate suffix: e.g. `1.0.0rc1`
- Hotfixes added to `master` automatically merge to `staging`
- Hotfixes added to `staging` automatically merge to `develop`

## Setting up a repo

In order to work, the Gitlab repo needs to be properly configured:

- Create a [Project Access Token](https://docs.gitlab.com/ee/user/project/settings/project_access_tokens.html)
- Add a [CI variable](https://docs.gitlab.com/ee/ci/variables/#for-a-project) named `ACCESS_TOKEN` with the token value
- Add `develop` and `staging` as [protect branches](https://docs.gitlab.com/ee/user/project/protected_branches.html#add-protection-to-existing-branches)
