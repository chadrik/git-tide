#!/bin/bash

# Required env vars:
# CI_REPOSITORY_URL: set by gitlab
# ACCESS_TOKEN: should be added to project variables
# AUTO_MERGE_BRANCH: can optionally be set by the job

set -e

source ./ci-common.sh

# Auto-merge
if [[ $AUTO_MERGE_BRANCH ]]; then

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
    # this will trigger auto-tag/merge for AUTO_MERGE_BRANCH
    git push gitlab_origin $AUTO_MERGE_BRANCH
  fi
fi
