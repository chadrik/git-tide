# Validation is one function with two call sites

The Semantic Commit Mode rules — one Project per commit on a Release Branch, patch
increments only there, no Trunk merged in, and no minting into a Version Line the
Trunk has lost — are implemented once and called from two places: the `tide validate`
command, which runs advisory before a merge, and `autotag`, which runs the same
checks on what actually landed and refuses to mint a tag if they fail.

Pre-merge validation is a best guess. It inspects the source branch, but the commits
that reach the Trunk depend on a merge strategy tide deliberately does not detect.
`autotag` is therefore the authoritative gate, and it stays correct when the merge
request job is skipped, force-pushed past, or configured non-blocking.

## Consequences

tide never adjusts its exit code based on CI context, and never detects whether it is
running against a merge request, a merge commit, or a release. It exits non-zero on
failure, with a distinct code per failure scenario, and stops there. Whether that
failure blocks a pipeline is `allow_failure`'s job — and a job that fails under
`allow_failure: true` renders differently from one that passes, so suppressing the
exit code would destroy the only signal the developer gets.

Adding merge-request detection to soften pre-merge failures would look like a
usability improvement. It would remove that signal.
