#!/bin/bash

set -e

get_tag() {
  echo $(cz bump "$@" --dry-run | grep tag | sed 's/tag to create: \(.*\)/\1/')
}

pip install git+https://github.com/chadrik/commitizen@gitflow-test

git config user.email "fake@email.com"
git config user.name "ci-bot"

echo "$CI_REPOSITORY_URL"

git remote add gitlab_origin https://oauth2:$ACCESS_TOKEN@gitlab.com/chadrik/semver-demo.git

git fetch gitlab_origin --tags
echo "TAGS"
git tag
echo ""


tag=$(get_tag $TAG_ARGS)
git tag "$tag"
git push gitlab_origin "$tag" -o ci.skip
