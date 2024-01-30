#!/bin/bash
set -e

cmtz() {
  PYTHONPATH=~/dev/commitizen python3.8 -m commitizen "$@"
}

git_tag() {
  local tag=''
  tag=$(cmtz bump "$@" --dry-run | grep tag | sed 's/tag to create: \(.*\)/\1/')
  git tag "$tag"
}

git checkout master
git reset --hard demo
mkdir src || true
touch src/base.txt
git add src/base.txt
git commit -m "master: initial state"
git tag "1.0.0"

# create develop
git checkout -b develop

# add a feature to develop
touch src/feat1.txt
git add src/feat1.txt
git commit -m "develop: add beta feature1"
git_tag --prerelease beta --increment MINOR --force-prerelease

# create staging
git checkout -b staging
cmtz version -p
git commit --allow-empty -m "staging: Starting release candidate"
git_tag --prerelease rc --increment PATCH

# add another feature to develop
git checkout develop
touch src/feat2.txt
git add src/feat2.txt
git commit -m "develop: add beta feature2"
git_tag --prerelease beta --increment MINOR --force-prerelease

# add a hotfix to master
git checkout master
touch src/fix.txt
git add src/fix.txt
git commit -m "master: add hotfix"
git_tag --increment PATCH

# merge the hotfix to staging
git checkout staging
git merge master -m "auto-merge hotfix from master into staging"
git_tag --prerelease rc --increment PATCH

# merge the hotfix to develop
git checkout develop
git merge staging -m "auto-merge hotfix from staging into develop"
git_tag --prerelease beta --increment PATCH

# add a feature fix to staging
git checkout staging
echo "more awesome" >> src/feat1.txt
git add src/feat1.txt
git commit -m "staging: update beta feature"
git_tag --prerelease rc --increment PATCH

# merge the hotfix to develop
git checkout develop
git merge staging -m "auto-merge rc hotfix from staging into develop"
git_tag --prerelease beta --increment PATCH

# Release time!
# merge staging to master
echo "Releasing develop to master!"
git checkout master
git merge staging -m "Release develop to master"
git commit --allow-empty -m "New release!"
git_tag --increment PATCH

## merge the hotfix to staging
#git checkout staging
#git merge master -m "auto-merge hotfix from master into staging"
#git_tag --prerelease rc --increment PATCH
#
## merge the hotfix to develop
#git checkout develop
#git merge staging -m "auto-merge hotfix from staging into develop"
#git_tag --prerelease beta --increment PATCH

## develop becomes release candidate
echo "Converting develop branch into release candidate"
git checkout staging
git reset --hard develop
#git merge develop -m "make new release candidate"
git commit --allow-empty -m "staging: Starting release candidate"
git_tag --prerelease rc --increment PATCH

# start a new beta cycle
#echo "Setting up new develop branch for beta development"
#git checkout develop
#git reset --hard master
##git merge staging -m "make new develop branch"
#git commit --allow-empty -m "develop: Starting branch"
#git_tag --prerelease beta --increment MINOR --force-prerelease
