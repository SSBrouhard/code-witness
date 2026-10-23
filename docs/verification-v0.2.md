# Evidence reliability verification

Evidence date: October 22, 2026 (America/New_York).
Starting revision: `8281d0653d4c286de56e417871eb1fb806e55870`.
Task: [PORT-101](https://linear.app/stephen-portfolio/issue/PORT-101/harden-code-witness-evidence-reliability-for-v02).

Acceptance: Code Witness detects changes to captured behavior across packs,
distinguishes verification from missing evidence, and binds findings to
identifiable source contents, with automated regression tests.

## Baseline findings

The original 19 tests passed on CPython 3.13.16. The preliminary findings were
accurate: diff omitted observations; static-only extraction intentionally returned
zero with NOT RUN; the manifest lacked content hashes and interpreter identity;
no GitHub Actions workflow existed; and the HTTP-helper pack had no runtime
coverage. Static extraction success was not itself a bug.

The original diff implementation was also tested against two compatibility
packs whose manifests declared format 1. Their stored return values were
explicitly read back as `behavior A` and `behavior B`; both legacy/modern pairs
reported MATCH. The unchanged baseline diff returned no change, while the new
diff reported the changed case. Only the copies' manifest format field was
adapted, not the original comparison code or observations.

An initial quick reproduction was invalidated on readback: stale bytecode made
both stored values A. This exposed a second evidence defect. The new regression
seeds `.pyc` files and preserves source timestamps across same-size edits; target
loading now compiles current source bytes. JSON boolean/number distinctions are
also enforced throughout extraction, comparison and replay.

## Executed checks

On CPython 3.13.16, `python -m unittest discover -s tests -v` passes all 33
tests. These include the original suite and regressions for observation/input
changes, required-runtime outcomes, source/fixture hashes, dirty Git contents,
source mutation during extraction, missing stored replay coverage, v1 comparison,
partial execution with a previously observed mismatch, and portable replay after
both source and pack relocation. Jev tests use mocked
transport and confirm deterministic artifacts remain unchanged.

Separate installed-CLI checks were run:

| Check | Observed result |
| --- | --- |
| Sample extraction with `--require-runtime` | PASS, 10 cases, exit 0 |
| Generated sample characterization via unittest discovery | Pass, exit 0 |
| A/B packs with both pairs MATCH | Diff detects changed observations, exit 1 |
| HTTP-helper static extraction | INCONCLUSIVE, 0 runtime cases, exit 0 |
| HTTP-helper extraction with `--require-runtime` | BLOCKED, exit 2 |
| Existing format-v1 HTTP pack vs new static pack | No contract change, exit 0 |
| `git diff --check` | Pass |

GitHub Actions runs the existing unittest command on Linux with Python 3.10 and
3.14. Hosted results are recorded on the draft PR. This repository configures no
separate lint or type-check command.

One focused independent review reproduced a missing manifest on static-source
mutation, skipped earlier mismatches after a later worker failure, and a
machine-path leak in blocked diagnostics for non-Git slices. Regression tests
failed for each before the fixes, then passed. Generated replay also explicitly
marks unexercised candidate coverage INCONCLUSIVE. The review did not delegate
implementation or launch additional reviewers.

## Limits and next milestone

The non-sample HTTP helper was statically extracted only. It has no supported
runtime entry point or fixture convention, imports third-party networking
dependencies, and no appropriately isolated runtime validation was established.
It was never imported or executed. No claim of non-sample runtime coverage is
made. The next narrow milestone is a reviewed, deterministic fixture set for one
trusted real module, with an explicitly approved execution environment.

PASS means agreement on supplied fixtures and applicable candidate checks,
not general equivalence or universal invariant proof. Provenance covers selected
sources, the discovered modern target and fixture bytes; it does not capture
the complete dependency closure, external resources, or transient changes between
reads. Imported helpers retain normal Python cache behavior. The subprocess and
timeout are not a security sandbox. No Jev service request, deployment, merge,
security integration or remediation platform was performed.

The bytecode and JSON-type fixes are in-scope evidence-integrity repairs. There
were no platform or execution-authority scope deviations.
