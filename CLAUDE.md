# Response style — applies from the FIRST reply of every session

Executive-level brevity. Non-negotiable, not a "when convenient" preference.

- Default: **1-3 sentences**. A status check gets one line.
- Lead with the answer. No preamble, no recap of my question, no
  restating what you just did in narrative form.
- No multi-section walls, no tables, no bullet lists unless I ask for a
  breakdown or the answer is genuinely a list of items.
- Findings: state the cause and the fix. Skip the investigation story.
- If it needs more depth, say so in one line and let me ask.
- End-of-turn summary: one sentence, usually zero.

Longer output is allowed ONLY when I explicitly ask ("deep dive",
"explain", "give me the breakdown", "full report").

# Project facts

- Cloud owns the pipeline (2026-07-28 handover). `gs://robotics-data/robotics.duckdb`
  is authoritative — **never run local `make ingest`** (forks state).
- Prod runtime identity is per-tool service accounts; owner/ADC is for deploys only.
- Status: `make ingest-render-status` (local vs cloud side by side).
- On-demand prod run: `make ingest-prod`.
- Full history + decisions: `docs/workbench.md` (newest entries at top).
