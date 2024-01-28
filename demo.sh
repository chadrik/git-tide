#!/bin/bash
set -e

# create if doesn't exist
git checkout beta || git checkout -b beta

# add a feature to beta
mkdir src || true
touch src/feat.txt
git add src/feat.txt
git commit -m "add feature"
cz bump --prerelease beta --increment MINOR

# add a hotfix to master
git checkout master
mkdir src || true
touch src/fix.txt
git add src/fix.txt
git commit -m "add fix"
cz bump --increment PATCH

# merge the hotfix to beta
git checkout beta
git merge master --strategy-option ours -m "auto-merge master into beta"
cz bump --prerelease beta --increment PATCH

# add a feature fix to beta
git checkout beta
echo "more awesome" >> src/feat.txt
git add src/feat.txt
git commit -m "update feature"
cz bump --prerelease beta --increment PATCH

# Release time!
# merge beta to master
git checkout master
git merge beta --strategy-option theirs -m "release beta into master"
cz bump --increment PATCH
