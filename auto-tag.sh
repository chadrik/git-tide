#!/bin/bash

# Required env vars:
# CI_REPOSITORY_URL: set by gitlab
# ACCESS_TOKEN: should be added to project variables
# TAG_ARGS: should be set by the job
# AUTO_MERGE_BRANCH: can optionally be set by the job

set -e

git config user.email "fake@email.com"
git config user.name "ci-bot"
URL=$(cut -d "@" -f2- <<< "$CI_REPOSITORY_URL")
git remote add gitlab_origin "https://oauth2:$ACCESS_TOKEN@$URL"

get_tag() {
  echo $(cz bump "$@" --dry-run | grep tag | sed 's/tag to create: \(.*\)/\1/')
}

pip install git+https://github.com/chadrik/commitizen@gitflow-test

# Auto-tag
TAG=$(get_tag $TAG_ARGS)
echo "Creating new tag $TAG"
git tag "$TAG"
git push gitlab_origin "$TAG" -o ci.skip

# Auto-merge
if [[ $AUTO_MERGE_BRANCH ]]; then

  git fetch gitlab_origin $AUTO_MERGE_BRANCH
  git checkout $AUTO_MERGE_BRANCH
  git reset --hard gitlab_origin/$AUTO_MERGE_BRANCH

  MSG="Auto-merge $CI_COMMIT_REF_NAME into $AUTO_MERGE_BRANCH"

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
