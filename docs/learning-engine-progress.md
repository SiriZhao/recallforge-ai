# Learning engine upgrade: evidence log

## P0: production path repair

Baseline: main `e50003593b3f6448d0c82a0fa7e1abbc35480566`; clean worktree.
Work branch: `codex/learning-engine-p0`. No release/tag changes or CSS changes.

Confirmed defects:

- Saved learner fields/material roles were absent from the UI generation request.
- The active V4 implementation wrote chunk checkpoints but never loaded them;
  splitter-local IDs collided across chapters. Downstream stages were not saved.
- Optional artifacts remained in overview, leaving structured result tabs empty.
- Practice and mock exam tabs displayed the same collection.
- A partial LLM result was persisted as completed by the job worker.
- Interrupted running jobs were not resumable, and parsed files were saved only
  after all input files succeeded.
- GitHub CI run 35691584867 failed on Linux: simulated win32 attempted to use
  subprocess.STARTUPINFO which is unavailable on that host.

Implemented: additive learner request fields; role policies and source metadata;
content/config/prompt-version-addressed, validated stage checkpoints; partial-file
parse persistence; expiring worker leases and heartbeat; additive SQLite lease
columns; typed internal artifacts with invalid question/card quarantine; legacy
UI/export projections; separate practice/exam collections; corrected partial job
status; cross-platform Windows API test fixture.

Validation (2026-10-18):

- Baseline backend: 116 passed, 1 skipped.
- P0 full backend: 126 passed, 1 skipped (external long PDF test requires opt-in).
- Frontend: npm ci succeeded; 16 tests passed; lint/typecheck/build passed.
- Fake-provider recovery: first run has failed unit; second run calls only unit
  draft and synthesis; third run performs zero model calls (six cache hits).
- Changed material bytes invalidate job fingerprints; API/OCR key changes do not.
- Invalid answer is quarantined, never projected into formal practice, and its
  stage is not saved as completed. Repair can retry that stage.
- Different daily time/mastery inputs change allocated review/practice time.
- No paid model calls. Synthetic tests are engineering evidence, not efficacy
  evidence or a comparison against a general conversational model.

Remaining work: richer extraction and provenance verification, critic/repair,
budgeted synthesis, assessment and mastery persistence, subject validators and
fair blinded evaluation. Main CI remains unchanged until a reviewed merge; this
branch must pass CI independently. Main is not automatically merged.
