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

if [[ -d .git ]]; then
  git checkout master
  git reset --hard demo
else
  git init

  cp "$DIR/pyproject.toml" ./
  cp "$DIR/noxfile.py" ./
  cp "$DIR/requirements.txt" ./
  git add pyproject.toml
  git add noxfile.py
  git add requirements.txt
fi

mkdir src || true
touch src/base.txt
git add src/base.txt
git commit -m "master: initial state"
git tag "1.0.0"

# create develop
git checkout -b develop
cmtz version -p
git commit --allow-empty -m "develop: Starting beta development for 1.1"
git tag $(get_tag --prerelease beta --increment MINOR --increment-mode=exact)

# add a feature to develop
touch src/feat1.txt
git add src/feat1.txt
git commit -m "develop: add beta feature1"
git tag $(get_tag --prerelease beta)

# create staging
git checkout -b staging
cmtz version -p
git commit --allow-empty -m "staging: Starting release candidate for 1.1"
git tag $(get_tag --prerelease rc --increment PATCH)

# add another feature to develop
git checkout develop
git commit --allow-empty -m "develop: Starting beta development for 1.2"
git tag $(get_tag --prerelease beta --increment MINOR --increment-mode=exact)

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

git log --graph --abbrev-commit --decorate --format=format:'%C(white)%s%C(reset) %C(dim white)-%C(auto)%d%C(reset)' --all
# git log --graph --abbrev-commit --decorate --format=format:'%C(bold blue)%h%C(reset) - %C(bold green)(%ar)%C(reset) %C(white)%s%C(reset) %C(dim white)- %an%C(reset)%C(auto)%d%C(reset)' --all
