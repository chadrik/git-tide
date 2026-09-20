# Divergence transfers Version Line ownership away from the Trunk

In Semantic Commit Mode, a Release Branch owns exactly one Project's Version Line.
The moment that branch receives a commit of its own and is no longer an ancestor
of the Trunk — Divergence — ownership of the line transfers to it, and `autotag`
hard-errors if a change on the Trunk would mint another version on that line.

## Considered Options

- **Auto-bump the Trunk to the next minor.** A `fix:` landing on the Trunk after
  `release-1.2` diverged would mint `1.3.0` instead of failing. Rejected: it mints
  a minor version that no `feat:` justifies, which corrupts the changelog, and it
  hides the fact that the developer's fix is not reaching the 1.2 line at all.
- **Forbid Divergence entirely.** Release Branches would only ever fast-forward.
  Rejected: shipping a patch for an older Version Line is the entire reason Release
  Branches exist.
- **Check for a tag collision instead of Divergence.** Rejected: a commit on a
  Release Branch that touches no Project diverges the branch without minting a tag,
  so the Trunk would mint into a line it no longer owns and the failure would
  surface later, on the maintenance branch, blaming the wrong developer.

## Consequences

The developer who diverges a Release Branch is not the one who sees the error; the
next person to land a `fix:` on the Trunk is. We accepted this rather than
pre-emptively blocking divergence, because diverging a line the Trunk still owns is
legitimate — it is how you ship "1.2.2 plus one fix" without dragging in unrelated
Trunk churn. The cost is carried entirely by the error message, which must name the
branch that took ownership and offer both remedies: retarget the change to that
Release Branch, or upgrade the commit to `feat:` to open a new Version Line.

A future maintainer will encounter this error and read it as a bug. It is not.
