#!/bin/bash

# Required env vars:
# CI_REPOSITORY_URL: set by gitlab
# ACCESS_TOKEN: should be added to project variables
# PRE_RELEASE_TYPE: should be set by the job
# AUTO_MERGE_BRANCH: can optionally be set by the job

set -e

source ./ci-common.sh

pip install -r requirements.txt

if [[ $TARGET_BRANCH == "develop" ]]; then
  PRE_RELEASE_TYPE="beta"
elif [[ $TARGET_BRANCH == "staging" ]]; then
  PRE_RELEASE_TYPE="rc"
else
  PRE_RELEASE_TYPE=""
fi

if [[ "$PRE_RELEASE_TYPE" ]]; then
  TAG_ARGS="--prerelease $PRE_RELEASE_TYPE --increment PATCH"
else
  TAG_ARGS="--increment PATCH"
fi

# Auto-tag
TAG=$(get_tag $TAG_ARGS)
echo "Creating new tag $TAG"
git tag "$TAG"
git push gitlab_origin "$TAG" -o ci.skip

# ./auto-merge.sh
