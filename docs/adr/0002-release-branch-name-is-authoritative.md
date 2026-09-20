# The Release Branch name is authoritative for version resolution

When `autotag` runs on a Release Branch, tide resolves the current version by
parsing the major and minor out of the branch name and taking the highest tag on
*that* Version Line. It deliberately does not use `commitizen`'s
`ScmProvider.get_version()`, which returns the highest version **reachable from
HEAD** regardless of which line it belongs to.

## Considered Options

- **Keep `get_version()` and forbid merging the Trunk into a Release Branch.** The
  merge is the actual mistake, so block the mistake. Rejected as the sole defence:
  the guard only runs on merge requests, so a direct push or a locally-merged branch
  still corrupts versioning silently. We adopted the check as well, but not instead.
- **Keep `get_version()` and assert the result afterwards.** Rejected: it converts a
  silent wrong tag into a loud pipeline failure with no automatic remedy, leaving a
  developer to untangle a merge by hand.

## Consequences

The hand-rolled resolution looks like needless duplication of a `commitizen` API, and
will invite simplification back to `get_version()`. It is not duplication. Merging the
Trunk into `myproject/release-1.1` makes `myproject/1.2.0` reachable, so max-reachable
resolution would mint `myproject/1.2.1` from the maintenance branch — leapfrogging the
line the branch owns and colliding with the Trunk's own next patch, silently.

Constraining to the branch name makes correctness independent of topology hygiene,
which matters because merging the Trunk in is a natural thing for a developer to do
to pick up a CI fix or a root lockfile bump.
