#!/bin/bash

set -e

git config user.email "fake@email.com"
git config user.name "ci-bot"
URL=$(cut -d "@" -f2- <<< "$CI_REPOSITORY_URL")
git remote add gitlab_origin "https://oauth2:$ACCESS_TOKEN@$URL"

get_tag() {
  echo $(cz bump "$@" --dry-run | grep tag | sed 's/tag to create: \(.*\)/\1/')
}

short_tag() {
  cut -d "." -f2- <<< "$1"
}

pip install -r requirements.txt

git fetch gitlab_origin

# Release time!
# merge staging to master
echo "Releasing develop to master!"
git checkout --track gitlab_origin/master
git merge gitlab_origin/staging -m "Release develop to master"
MASTER_TAG=$(get_tag --increment PATCH)
git commit --allow-empty -m "New release! $(short_tag $MASTER_TAG)"
git tag $MASTER_TAG

# develop becomes release candidate
echo "Converting develop branch into release candidate"
git checkout --track gitlab_origin/staging
git merge gitlab_origin/develop -m "Release develop to master"
RC_TAG=$(get_tag --prerelease rc --increment PATCH)
git commit --allow-empty -m "Starting release candidate for $(short_tag $RC_TAG)"
git tag $RC_TAG

# start a new beta cycle
echo "Setting up new develop branch for beta development"
git checkout --track gitlab_origin/develop
BETA_TAG=$(get_tag --prerelease beta --increment MINOR --force-prerelease)
git commit --allow-empty -m "Starting beta development for $(short_tag $BETA_TAG)"
git tag $BETA_TAG

git push gitlab_origin "$MASTER_TAG" -o ci.skip
git push gitlab_origin "$RC_TAG" -o ci.skip
git push gitlab_origin "$BETA_TAG" -o ci.skip
