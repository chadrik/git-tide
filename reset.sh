#!/bin/bash
set -e

git checkout master
git reset --hard origin/master
git tag -d $(git tag -l)
git fetch
git branch -D beta
