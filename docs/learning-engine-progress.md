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

## Exam evidence and persistent learning feedback (2026-10-18)

Implemented conservative cross-file concept/alias merging with stable meaning IDs;
different definitions remain distinct with conflict warnings. Added shared subject
profiles for probability, programming, botany and organic chemistry. Added observed
exam extraction, cross-page continuation, exact-text deduplication, answer-sheet
exclusion and an evidence-labeled exam blueprint. Legacy report projections now
retain question/card IDs, knowledge IDs, rubric and source refs.

The existing Questions/Flashcards/KnowledgeMap/ResultsPage consume these fields
without CSS or layout changes. Workspace-owned SQLite attempts, exam sessions,
mastery and schedules persist real answers; nonce checks prevent duplicate updates.
Objective choice scoring is relative to the reference, not proof of its correctness.
Subjective grading remains explicitly pending self review; keyboard self ratings
are learner reports. Recall exposure never implies mastery. Recommendations use
observed ratings, prerequisite order, remaining days, target and daily time.

New arithmetic verification uses a bounded AST interpreter, never eval/exec. Schema
repair is limited to reported leaf paths so it cannot delete course concepts.
Typed artifact-root and unit-plan contracts address observed real-model failures.
Synthesis failure preserves model-authored units as explicit partial results.

Validation: full backend 147 passed / 1 skipped; frontend 19 passed; lint,
typecheck and build passed. New tests cover workspace isolation, answer restore,
nonce idempotency, additive schema initialization, source deduplication, numeric
safety, claim-preserving correction, and actual UI-to-API bindings. CSS build
artifact remains index-DGYlaRpg.css. No main merge, tag change or release operation.

Real evaluation: project .env explicitly selected despite inherited key override.
Four local OCR probes completed. Confidence is a recognizer signal, not accuracy;
blank answer sheets and chemical diagrams require special handling. Initial real
probability/Python trials exposed truncation, invalid relation types and an invalid
artifact-root contract; these are retained as failures, not benchmark wins. A
botany trial produced populated structured artifacts with resolvable citations,
but critic/synthesis remained partial. No reliable human gold answers or blinded
human ratings are available, so factual correctness, 95% coverage and superiority
over a general chat workflow remain unverified. Private sources, prompts, responses,
OCR images and the durable spend ledger stay in ignored runtime/evaluation.

Follow-up from real failures: validate unit knowledge IDs against the course graph
and reject objective IDs; critic coverage is scoped to the actual assigned unit,
with prerequisite closure. Active-path quality gates use reference resolution,
coverage manifests, unit readiness and artifact validation rather than word count
or question quotas. A partial synthesis cannot get a high score by adding filler.

## Long-course coverage and targeted question recovery (2026-10-19)

Fixed same-page chapter boundaries, preserving preamble once while excluding TOC
interference. Continuation chunks retain page anchors. Oversized course modelling
now batches primary evidence without silent truncation, namespaces batch concept
IDs and explicitly leaves cross-batch dependency completeness unverified. Concept
identity uses source/name rather than model-definition wording when localized.

Teaching drafts, critics and repairs now receive scoped primary evidence, not only
model summaries. Rejected questions/cards get at most four single-item repairs per
bundle; each replacement is validated against the entire bank to prevent displacing
valid siblings or introducing practice/exam duplicates. Failed replacements remain
quarantined. Repair checkpoints are reusable and database write failures propagate.
Exam deduplication waits for full cross-page stems, preserves distinct continuations
and treats Python case/indentation as semantic. Question-type extraction is labeled
heuristic rather than human-verified.

Numeric responses are checked against bounded reference arithmetic. Same-day
repetitions cannot double the review interval. Time-to-exam changes relative review
priorities, tested with different selected items. Failed client requests reuse the
same nonce; editing a response clears stale grading in the existing component.
No style/layout changes.

Validation: full backend 154 passed / 1 skipped (external long-PDF opt-in test).
The opt-in private CLI and offline measurement command are reproducible; raw exam
content, responses, OCR images and the SQLite spend ledger remain ignored. Current
exploratory botany comparison uses identical source hashes and common per-arm caps,
but unequal actual calls/tokens. No human gold or blind scores exist. Engine output
is still partial, and no quality superiority is claimed. Conservative cumulative
paid-call upper bound before the next trial: USD 0.3359148, no uncertain requests.
The public price bound is cache-miss peak pricing, not an exact vendor invoice.

Continuity priorities: validate repaired real-model outputs, strengthen evidenced
course relations and formula conditions, add vetted independent answers before
efficacy claims, and check Windows packaging on the final frozen source snapshot.
Earlier PyInstaller smoke build succeeded but preceded this snapshot; Inno Setup
and optional WeasyPrint native libraries were unavailable. Main/release unchanged.

## Adaptive feedback and extraction safety follow-through (2026-10-19)

Added additive learning_misconceptions records linked to actual attempts and
knowledge IDs. Unknown causes stay uncertain; later correct answers update mastery
without deleting historical evidence. Retry nonces cannot duplicate this history.
Project deletion includes the additive records. Progress now returns ranked real
practice IDs and diagnostics; existing card text shows persisted self ratings/due
dates and existing question text shows assessment evidence/recommendations. Fixed
state leaking across generation jobs and stale grading after answer edits. Tests
assert retained DOM containers; CSS remains index-DGYlaRpg.css.

Text decoding no longer uses errors=ignore: BOM UTF-16 and GB18030 are decoded
strictly, unknown/corrupt input fails explicitly. Evidence carries OCR-unverified,
uninterpreted-visual and heuristic-confidence warnings. Diagram-dependent questions
cannot bypass quarantine merely by omitting requires_diagram=true.

Validation: full backend 162 passed / 1 skipped; frontend 22 passed; lint,
typecheck, build and diff check passed. A compatibility error from Array.at was
fixed without changing the TypeScript target; the synthetic CLI fixture was fixed
to provide required summary data, without weakening production assertions.

Real-model revision regression is kept apart from the original paired experiment,
with the same global spend ledger and private manifests. It generated 2 practice
questions, 2 different exam questions and 4 cards. All 18 reported source locators
resolved and all structured entities had citation fields. These are locator/field
checks, NOT entailment or answer correctness. Both teaching units still failed the
critic because source OCR/terminology and unsupported external explanation need
review. The output remains partial; no gates were relaxed. Cumulative conservative
upper bound: USD 0.3782982 across 73 requests; zero uncertain requests. This revision
regression is not a fresh budget-matched superiority experiment.

Remaining acceptance gaps: independently checked real-course gold answers and
human blind ratings; six-course efficacy measurement; diagram/chemical-structure
understanding; robust programming behavior verification; semantic dependency and
formula-condition completeness; exact current-source Windows installer/runtime
acceptance. No paid requests should be made without consulting the existing ledger.

## Formula/example association integrity

Course batches now namespace formula and example IDs as well as concept IDs;
concept references and entity knowledge_ids follow normalization. Source-less
formulas/examples retain their content/conditions with INSUFFICIENT basis and an
explicit quality failure. Dangling formula/example references are reported.
Source-resolved formula expressions now populate the existing study-unit export
fields instead of showing only condition strings.

Full backend: 164 passed / 1 skipped; frontend: 22 passed with lint/typecheck/build.
An additional read-only EXE checker compares compiled app code signatures and
frontend bytes with the current tree, and checks for env/database archive entries.
It correctly detected three stale engine modules in a successful-but-earlier
PyInstaller build. Build success alone is not final-source/runtime acceptance.
Final backend run with the provenance-checker regression: 165 passed / 1 skipped.

## Case-sensitive subject answers

Fixed a concrete programming/chemistry scoring defect: casefolded option text
could map print(a) to print(A), or Co to CO. Text options now require exact content
matches before permissive option-label parsing. Unicode string literal contents
are not folded. Question/card deduplication uses the same code-sensitive signature
as exam extraction, preserving Python case and indentation. These are deterministic
identity/scoring improvements, not a full Python or chemical-reaction verifier.
Full backend: 166 passed / 1 skipped; no additional paid requests.

## Frozen build and continuation evidence

Engine snapshot: a87ad37 (six local phase commits after 0291fc5). Main stays at
e500035; no public push, merge, release or tag change was performed in this run.
The final isolated PyInstaller build succeeded. Read-only EXE inspection verified
92 compiled application modules and four frontend assets against current source;
zero mismatches, missing required modules or env/database archive entries. EXE size:
220101654 bytes. Runtime and Inno Setup installer acceptance remain NOT_TESTED;
the compiler was absent from PATH/common install locations and optional WeasyPrint
native/ONNX quantization dependencies emitted warnings. No pixel-level UI baseline
comparison was performed; style source is unchanged and DOM-container tests pass.

Reproduce final archive verification from repository root:

```powershell
python scripts/verify-packaged-engine.py backend/runtime/packaging-check-casefinal-20261019/dist/ExamForgeAI.exe
```

Machine-readable tests are local-only at runtime/verification/backend-final.xml
and frontend-final.json under backend. The private continuation checkpoint includes
ledger/sample locations and exact remaining tasks. New paid calls must use the
existing ledger (USD 0.3782982 upper bound, 73 complete requests, zero uncertain).

Concrete next engine work: preserve the full structured critic when repairing
units (the current defect summary is length-limited); enforce question repair's
knowledge/objective scope invariants; project structured citations into legacy
display hints where absent; deepen safe programming/chemical-diagram validation;
and obtain independent real-course gold/anonymous human scores before reporting
efficacy. The six-course, 95% coverage/provenance and 15-point superiority targets
have NOT been established by the present engineering tests or exploratory runs.

## Structured repair scope and authorized remote delivery

Previously verified b43ed80 was pushed to codex/learning-engine-p0 after scanning
all reachable historical blobs, commit messages and original private-file hashes.
GitHub CI run 39234950719 passed backend/frontend. gh's saved login was invalid;
the existing Git credential manager was used in child-process memory only.

Production repair now receives the complete structured critic (including issues
beyond the former 1000-character cut-off), exact findings and unit scope. Located
minor wording defects use bounded patches; major defects stay unit-local. Valid
knowledge coverage, undisputed resolved references and verified sibling hashes
are checked before mandatory post-repair critique. Item repair preserves valid
knowledge IDs and its learning objective and is independently revalidated. Failed
repairs retain partial status, never READY by model assertion alone.

New adversarial tests cover omitted late critic defects, localized editing,
reference/concept deletion, sibling preservation and learning-target substitution.
Backend: 173 passed / 1 skipped. Frontend npm ci initially hit ECONNRESET; the
unchanged lockfile/prefer-offline retry succeeded, then 22 tests, lint, typecheck
and build passed. CI now explicitly includes frontend lint/typecheck.
No new paid calls; original USD 0.50 authorization and ledger remain in force.

## Scientific identity and verifiable quotations

Remote PR CI 39236449375 passed for f93d099. The next phase removes destructive
case/space/Unicode folding from scientific entity identity and concept definitions.
Code indentation, string literals, event symbols and chemical element case now
remain distinct. Exact duplicate definitions can still merge; prior ID aliases
survive repeat normalization. Existing stored reports/data are not rewritten.

SUPPORTED critic claims now need exact source excerpts, not only valid page
locators. Quote matching preserves whitespace; support traces record extraction
warnings and explicitly do not claim deterministic entailment. Critic judgments
remain fallible model reviews, not human gold. Validated structured references
are projected into existing question/card citation fields without UI changes.

Backend full run: 183 passed / 1 skipped (before the additional alias-idempotence
assertion, which is verified with the focused suite). The first non-elevated
focused run encountered Windows pytest temp directory permissions; using a fresh
repository-owned basetemp with authorized process permissions resolved setup.
No new paid calls. The USD 0.3782982 cumulative upper bound remains unchanged.

## Continuous learning evidence

Historical mistake records now expose their current follow-up state without
deleting the original error. Only a later answer to the same job, mode and item
can mark SUBSEQUENT_SUCCESS; it is explicitly not proof of a misconception cure.
Unscored follow-ups remain pending self-review. Literal rubric hints preserve
scientific case/spacing, and numeric grading cannot override an option grade.

The API regression submits seven sequential answers across two concepts,
verifies the next concept/question switches with performance, checks mastery
changes rather than merely HTTP success, and reopens database initialization
before reading preserved records. Full backend: 185 passed / 1 skipped.
PR CI 39245790223 passed the preceding scientific identity phase (55c8682).
No new paid calls, UI/layout/schema migration changes, main merge or release.
