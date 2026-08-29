# Tide

Tide automates versioning and branch movement for git repositories. It determines
versions by reading git tag history rather than by writing to a project's config
file, and it treats monorepos as the default case rather than a special case.

## Language

### Core

**Project**:
A versioned unit within a repository, identified by a directory containing a
`pyproject.toml`. A repository may contain many.
_Avoid_: Package, module, sub-project

**Branching Mode**:
The set of rules Tide follows for how versions advance and how branches move.
A repository selects exactly one; it is not selectable per Project.
_Avoid_: Strategy, workflow, model

**Release ID**:
The maturity phase a version belongs to: alpha, beta, rc, or stable.

### Gitflow Mode

**Gitflow Mode**:
The Branching Mode in which a fixed ladder of long-lived branches, one per
Release ID, carries changes from most experimental to stable.

**Promotion**:
Advancing every branch in the ladder one rung toward stable, converting each
branch's Release ID as it goes.
_Avoid_: Release, cut, ship

**Hotfix**:
A change originating on a more stable branch, which Tide merges downward into
every more experimental branch.
_Avoid_: Backport, patch, cherry-pick

**Promotion Marker**:
A record attached to the commit a Promotion started from, used to decide whether
a Project's next version is a minor bump.

### Semantic Commit Mode

**Semantic Commit Mode**:
The Branching Mode in which a Project's next version is derived from the
conventional-commit types of the commits that touched it. Trunk-based: it has no
Promotion and no Hotfix.
_Avoid_: Conventional commit mode, semver mode, trunk mode

**Trunk**:
The single branch all development lands on, and the only branch from which a
Project's minor and major version lines are opened.
_Avoid_: Main, master, mainline, default branch

**Version Line**:
All versions of one Project sharing a major and minor number. Opened on the
Trunk, which mints into it until Divergence transfers ownership to its
Release Branch.
_Avoid_: Series, track, stream, release train

**Release Branch**:
A long-lived branch owning one Project's Version Line. It accepts only
patch-level changes, and only to the Project that owns it.
_Avoid_: Maintenance branch, support branch, stable branch

**Divergence**:
The state of a Release Branch that has received a commit of its own, and so is no
longer an ancestor of the Trunk. It transfers ownership of the Version Line to
that branch, after which the Trunk may no longer mint into the line.
_Avoid_: Fork, split, drift
