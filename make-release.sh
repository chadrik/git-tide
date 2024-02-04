#!/bin/bash

set -e

git config user.email "fake@email.com"
git config user.name "ci-bot"
URL=$(cut -d "@" -f2- <<< "$CI_REPOSITORY_URL")
git remote add gitlab_origin "https://oauth2:$ACCESS_TOKEN@$URL"

get_tag() {
  echo $(cz bump "$@" --dry-run | grep tag | sed 's/tag to create: \(.*\)/\1/')
}

pip install -r requirements.txt

git fetch gitlab_origin

# Release time!
# merge staging to master
echo "Releasing develop to master!"
git checkout master
git merge staging -m "Release develop to master"
TAG=$(get_tag --increment PATCH)
git commit --allow-empty -m "New release! $(cut -d "." -f2- <<< "$TAG")"
git tag $TAG
git push gitlab_origin "$TAG" -o ci.skip

# develop becomes release candidate
echo "Converting develop branch into release candidate"
git checkout staging
git merge develop -m "Release develop to master"
#git reset --hard develop
TAG=$(get_tag --prerelease rc --increment PATCH)
git commit --allow-empty -m "Starting release candidate for $(cut -d "." -f2- <<< "$TAG")"
git tag $TAG
git push gitlab_origin "$TAG" -o ci.skip

# start a new beta cycle
echo "Setting up new develop branch for beta development"
git checkout develop
TAG=$(get_tag --prerelease beta --increment MINOR --force-prerelease)
git commit --allow-empty -m "Starting beta development for $(cut -d "." -f2- <<< "$TAG")"
git tag $TAG
git push gitlab_origin "$TAG" -o ci.skip
