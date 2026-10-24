# Release checklist

For a release that changes how agent runs start, stop, or clean up.
The keeper owns merging, tagging, and installing.

1. **Real-agent check, on the exact commit.**
   From the checkout, run the following, with `HEAD` replaced by the release tag or commit ID if that is not what is checked out.
   The check runs that commit from its own clone, so the checkout and its uncommitted files stay untouched:

   ```bash
   .venv/bin/python scripts/agent_check.py --yes --ref HEAD
   ```

   It must pass.
   Keep its `report.json` as the evidence; it names the commit, the agent versions, and each check.
   Without `--ref`, the check runs the working tree's `src`.
   That is fine during development, but it is not release evidence.
2. **Installed-app repeat, after installing.**
   The check above does not cover packaging, the window's lifecycle, or the shipped backend, so repeat it in `~/Applications/Hearth.app`:
   - **Codex's command:** Codex is told not to run the project's `check` itself (ADR-0047), so ask it for the project's `turn_check`.
     If the project has none, add one to `~/.hearth/projects.json` for the repeat, as long as the full check but worded differently, such as the `check` followed by `-p 'test_*.py'`, as `agent_check` does.
     Start a fresh Codex conversation after adding it, since Codex is told only on a conversation's first turn.
     Afterwards, restore the `turn_check` the project had, or remove it if it had none.
   - **Stop:** start a Codex conversation running that command and a Claude conversation running the project's check.
     Stop Codex.
     Claude's check keeps running, nothing of Codex's is left, and its reply has no note of processes left running.
   - **Quit:** send a fresh Codex conversation that command, then quit the app while it runs.
     Reopen the app.
     Codex's turn shows as stopped by quitting, and Claude's as stopped by quitting, or as done if its check had already finished.
     Nothing is left.

   Mark an execution change resolved only after this passes.
