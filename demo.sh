#!/bin/bash
set -e

# create if doesn't exist
git checkout beta || git checkout -b beta

# add a MINOR change to beta
mkdir src || true
touch src/feat.txt
git add src/feat.txt
git commit -m "add feature"
cz bump --prerelease beta --increment MINOR

# add a PATCH change to master
git checkout master
mkdir src || true
touch src/fix.txt
git add src/fix.txt
git commit -m "add fix"
cz bump --increment PATCH

# merge beta
git merge beta --strategy-option theirs -m "merge beta into master"
cz bump --increment PATCH
