# Reviewer guide

Hearth sends this guide to the reviewer from the task's base commit and names one review context in the prompt.
The rules for every context come first; then apply the section for the named context.

## Every review

Check the change against the goal and, where they exist, `AGENTS.md` and the standards in `docs/standards/`, and cite a standard ID such as CLI-3 for each finding when one applies.
Approve only when the change meets the goal, is tested, and has no problem you would block a merge for.
Ask for changes to any edit the goal did not call for, especially one that weakens or skips a test (TEST-7).
A finding that matches no standard is still a finding; leave its `standard` empty.

## Initial implementation

The first review of the change: apply the rules above to the whole diff.

## Review after a fix

An earlier review asked for changes and the implementer has changed the branch since.
Review the whole diff from the base, not only the latest edits, with the initial implementation or docs-only checklist that fits it, since a fix can break something that was fine before.
Look for fixes that hide a problem instead of solving it: a weakened assertion, an added skip, a caught and ignored error, or a loosened limit (TEST-7, CODE-7).

## CI fix

CI failed on the pull request and the implementer changed the branch to fix it.
Review the whole diff as in the review after a fix; the CI fix should address the failure itself, often a difference between CI and the local environment (TEST-4, DEL-4).
Ask for changes when CI was made to pass by skipping or deleting tests, by editing the workflow or the check command, or by marking a test as expected to fail, unless the goal asks for that.

## Docs-only change

Every changed file is Markdown, so "is tested" does not apply; check the writing instead.
Apply the writing standards: one sentence per line (DOC-1), external tool behavior confirmed (DOC-3), figures computed from their source (DOC-4), and commands that run in zsh as written (DOC-6, DOC-7).
Ask for changes to any claim about the code that the current code contradicts, and to any private data or real local path (CODE-6).
A change to an ADR's decision needs a new ADR that supersedes it (DOC-2).
