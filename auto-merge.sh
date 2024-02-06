#!/bin/bash

# Required env vars:
# CI_REPOSITORY_URL: set by gitlab
# ACCESS_TOKEN: should be added to project variables
# AUTO_MERGE_BRANCH: can optionally be set by the job

set -e

source ./ci-common.sh

if [[ $TARGET_BRANCH == "master" ]]; then
  AUTO_MERGE_BRANCH="staging"
elif [[ $TARGET_BRANCH == "staging" ]]; then
  AUTO_MERGE_BRANCH="develop"
else
  exit 0
fi

# Auto-merge

# Record the current state
git checkout -B temp
# Fetch and checkout the upstream branch
git fetch gitlab_origin $AUTO_MERGE_BRANCH
git checkout --track gitlab_origin/$AUTO_MERGE_BRANCH

MSG="Auto-merge $CI_COMMIT_REF_NAME into $AUTO_MERGE_BRANCH"
echo $MSG

set +e
git merge temp -m "$MSG"
STATUS=$?
set -e

if [[ $STATUS != 0 ]]; then
  echo "Conflicts:"
  git diff --name-only --diff-filter=U
else
  # this will trigger a full pipeline for AUTO_MERGE_BRANCH, and potentially another auto-merge
  git push gitlab_origin $AUTO_MERGE_BRANCH
fi

