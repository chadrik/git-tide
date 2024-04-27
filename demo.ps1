$ErrorActionPreference = 'Stop'

$DIR = Split-Path -Path $MyInvocation.MyCommand.Definition -Parent

pip install -r $DIR/requirements.txt


function Invoke-Cmtz {
    param(
        [Parameter(ValueFromRemainingArguments=$true)]
        [string[]]$Arguments
    )
    & cz $Arguments
}

function Get-Tag {
    param(
        [Parameter(ValueFromRemainingArguments=$true)]
        [string[]]$Arguments
    )
    $output = Invoke-Cmtz -Arguments @('bump') + $Arguments + @('--dry-run')
    $tag = $output | Select-String -Pattern 'tag to create: (.*)' | ForEach-Object { $_.Matches.Groups[1].Value }
    return $tag
}

git init

Copy-Item "$DIR\pyproject.toml" -Destination ".\"
Copy-Item "$DIR\noxfile.py" -Destination ".\"
Copy-Item "$DIR\requirements.txt" -Destination ".\"
git add pyproject.toml
git add noxfile.py
git add requirements.txt

New-Item -ItemType Directory -Path src -Force
New-Item -ItemType File -Path src\base.txt
git add src\base.txt
git commit -m "master: initial state"
git tag "1.0.0"

nox -s ci_release
git checkout develop
# skip autotag for master/staging, bc master is still at 1.0.0, and staging doesn't exist yet
nox -s ci_autotag -- --increment minor

# add a feature to develop
git checkout develop
New-Item -ItemType File -Path src\feat1.txt
git add src\feat1.txt
git commit -m "develop: add beta feature1"
nox -s ci_autotag

nox -s ci_release
# skip autotag for master, bc it's still at 1.0.0
git checkout staging
nox -s ci_autotag
git checkout develop
nox -s ci_autotag -- --increment minor

New-Item -ItemType File -Path src\feat2.txt
git add src\feat2.txt
git commit -m "develop: add beta feature2"
nox -s ci_autotag

# add hotfix to master
git checkout master
New-Item -ItemType File -Path src\fix.txt
git add src\fix.txt
git commit -m "master: add hotfix"
nox -s ci_autotag

# merge hotfix to staging
nox -s ci_automerge
nox -s ci_autotag

# merge hotfix to develop
nox -s ci_automerge
nox -s ci_autotag


# add a feature fix to staging
git checkout staging
Add-Content -Path src\feat1.txt -Value "more awesome"
git add src\feat1.txt
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

git log --graph --abbrev-commit --decorate --format=format:'%C(white)%s%C(reset) %C(dim white)- %C(auto)%d%C(reset)' --all
