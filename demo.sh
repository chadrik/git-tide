#!/bin/bash
set -e

DIR="$( dirname -- "${BASH_SOURCE[0]}"; )";

pip install -r $DIR/requirements.txt

cmtz() {
  # PYTHONPATH=~/dev/commitizen python3.8 -m commitizen "$@"
  cz "$@"
}

get_tag() {
  echo $(cmtz bump "$@" --dry-run | grep tag | sed 's/tag to create: \(.*\)/\1/')
}

git init

cp "$DIR/pyproject.toml" ./
cp "$DIR/noxfile.py" ./
cp "$DIR/requirements.txt" ./
git add pyproject.toml
git add noxfile.py
git add requirements.txt

mkdir src || true
touch src/base.txt
git add src/base.txt
git commit -m "master: initial state"
git tag "1.0.0"

nox -s ci_release
git checkout develop
# skip autotag for master/staging, bc master is still at 1.0.0, and staging doesn't exist yet
nox -s ci_autotag -- --increment minor

# add a feature to develop
git checkout develop
touch src/feat1.txt
git add src/feat1.txt
git commit -m "develop: add beta feature1"
nox -s ci_autotag

nox -s ci_release
# skip autotag for master, bc it's still at 1.0.0
git checkout staging
nox -s ci_autotag
git checkout develop
nox -s ci_autotag -- --increment minor

touch src/feat2.txt
git add src/feat2.txt
git commit -m "develop: add beta feature2"
nox -s ci_autotag

# add a hotfix to master
git checkout master
touch src/fix.txt
git add src/fix.txt
git commit -m "master: add hotfix"
nox -s ci_autotag

# merge the hotfix to staging
nox -s ci_automerge
nox -s ci_autotag

# merge the hotfix to develop
nox -s ci_automerge
nox -s ci_autotag

# add a feature fix to staging
git checkout staging
echo "more awesome" >> src/feat1.txt
git add src/feat1.txt
git commit -m "staging: update beta feature"
nox -s ci_autotag

# merge the hotfix to develop
nox -s ci_automerge
nox -s ci_autotag

nox -s ci_release
git checkout master
nox -s ci_autotag
git checkout staging
nox -s ci_autotag
git checkout develop
nox -s ci_autotag -- --increment minor


git log --graph --abbrev-commit --decorate --format=format:'%C(white)%s%C(reset) %C(dim white)-%C(auto)%d%C(reset)' --all
# git log --graph --abbrev-commit --decorate --format=format:'%C(bold blue)%h%C(reset) - %C(bold green)(%ar)%C(reset) %C(white)%s%C(reset) %C(dim white)- %an%C(reset)%C(auto)%d%C(reset)' --all
