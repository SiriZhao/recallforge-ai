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

## Extraction and evidence foundations

P0 branch CI run 39135039382: backend and frontend both succeeded on GitHub.

Fixed DOCX paragraph/table interleaving and included native table text in PPTX
generation input. Preserved DOCX OMML text with a representation warning;
captured PPT titles/table geometry, PDF layout extraction and bookmark titles.
Uninterpreted images now produce explicit page warnings. Repeated heading text
is excluded from chapter events; processing windows are explicitly labeled as
processing windows, not semantic chapter discoveries.

Added evidence spans with document/block/page/slide identities, hashes, extraction
warnings and confidence. Citation resolution checks document/name/page/block,
quoted fragments and text positions against extracted source text. This confirms
reference resolution, not semantic entailment. Full backend: 130 passed, 1 skipped.

Not yet verified: broad multi-column PDF recovery, chart/diagram interpretation,
semantic concept normalization and relation correctness on real courses.

## Teaching quality foundations

Connected a model-driven per-unit critic with explicit claim basis and resolvable
citations. A defective unit receives at most one targeted repair and is reviewed
again. Rejected repair keeps the original unit as NEEDS_REPAIR and generation as
partial. Successful unit/critic/repair stages are independently checkpointed.
Added bounded fan-in synthesis: intermediate summaries are cached, oversized
requests are rejected explicitly, and no source text is silently truncated.
Coverage manifest checks concept name/alias presence (not factual correctness).

Full backend: 133 passed, 1 skipped. Focused final regression: 13 passed. New tests
exercise actual critic -> repair -> critic orchestration, bounded repair failure,
and synthesis batching/context failure.

Added ownership guards so an expired/superseded worker cannot overwrite a new
worker's parsed data, checkpoint or final result. This is tested against the
SQLite lease recovery path. No CSS/layout files were modified.

Real-course evaluation now takes priority once the user supplies the authorized
local folder and gold answers. No API key has been requested in chat or read into
logs. No paid requests have been sent. User requires a separate budget approval
before even connection/smoke calls. Missing/unreliable gold answers must remain
UNVERIFIED; model-written answers cannot become ground truth. A/B arms must use
the same explicit DeepSeek model version, source subset, goals and total budget.
