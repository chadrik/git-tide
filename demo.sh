#!/bin/bash
set -e

# create develop
git checkout -b develop

# add a feature to develop
mkdir src || true
touch src/feat.txt
git add src/feat.txt
git commit -m "add beta feature"
cz bump --prerelease beta --increment MINOR

# add a hotfix to master
git checkout master
mkdir src || true
touch src/fix.txt
git add src/fix.txt
git commit -m "add hotfix"
cz bump --increment PATCH

# merge the hotfix to develop
git checkout develop
git merge master --strategy-option ours -m "auto-merge master into develop"
cz bump --prerelease beta --increment PATCH

# add a feature fix to develop
git checkout develop
echo "more awesome" >> src/feat.txt
git add src/feat.txt
git commit -m "update beta feature"
cz bump --prerelease beta --increment PATCH

# Release time!
# merge develop to master
git checkout master
git merge develop --strategy-option theirs -m "release develop into master"
cz bump --increment PATCH
