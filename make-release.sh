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
  # -f<from>-<to>
  cut -d "." -f1-2 <<< "$1"
}

pip install -r requirements.txt

git fetch gitlab_origin

echo "pipeline source: $CI_PIPELINE_SOURCE"

# Release time!
# merge staging to master
echo "Releasing staging to master!"
git checkout --track gitlab_origin/master
git merge gitlab_origin/staging -m "Release staging to master"
MASTER_TAG=$(get_tag --increment PATCH)
git commit --allow-empty -m "New release! $(short_tag $MASTER_TAG)"
git tag $MASTER_TAG

# develop becomes release candidate
echo "Converting develop branch into release candidate"
git checkout --track gitlab_origin/staging
git merge gitlab_origin/develop -m "Release develop to staging"
RC_TAG=$(get_tag --prerelease rc --increment PATCH)
git commit --allow-empty -m "Starting release candidate for $(short_tag $RC_TAG)"
git tag $RC_TAG

# start a new beta cycle
echo "Setting up new develop branch for beta development"
git checkout --track gitlab_origin/develop
BETA_TAG=$(get_tag --prerelease beta --increment MINOR --force-prerelease)
git commit --allow-empty -m "Starting beta development for $(short_tag $BETA_TAG)"
git tag $BETA_TAG

# git push --atomic gitlab_origin master staging develop "$MASTER_TAG" "$RC_TAG" "$BETA_TAG" -o ci.skip
git push --atomic gitlab_origin master "$MASTER_TAG" -o ci.skip
git push --atomic gitlab_origin staging "$RC_TAG" -o ci.skip
git push --atomic gitlab_origin develop "$BETA_TAG" -o ci.skip
