# Release checklist

For a release that changes how agent runs start, stop, or clean up. The keeper owns merging, tagging, and installing.

1. **Real-agent check, on the exact commit.** From the checkout, run:

   ```bash
   .venv/bin/python scripts/agent_check.py --yes --ref <release commit>
   ```

   It must pass. Keep its `report.json` as the evidence; it names the commit, the agent versions, and each check. Without `--ref`, the check runs the working tree's `src`. That is fine during development, but it is not release evidence.
2. **Installed-app repeat, after installing.** The check above does not cover packaging, the window's lifecycle, or the shipped backend, so repeat it in `~/Applications/Hearth.app`:
   - **Stop:** start a Codex conversation and a Claude conversation, each running the project's check. Stop Codex. Claude's check keeps running, nothing of Codex's is left, and its reply has no note of processes left running.
   - **Quit:** send Codex the check again, then quit the app while it runs. Reopen the app. Codex's turn shows as stopped by quitting, and Claude's as stopped by quitting, or as done if its check had already finished. Nothing is left.

   Mark an execution change resolved only after this passes.
