# debt-compiler

A local Python slice compiler: tree-sitter graph, Git archaeology, contract candidates, and executable characterization. The deterministic extractor has no LLM, API key, cloud service, or rewrite engine. An optional Jev evidence review is separate and requires its own API key.

## Run

Python 3.10+ is required. From this repository root:

```sh
pip install -e .
debt-compiler extract sample/legacy --out /tmp/debt-pack
python -m unittest discover -s /tmp/debt-pack/tests -v
```

For an isolated install, first run `python3 -m venv .venv` and `source .venv/bin/activate`.

Run the development checks:

```sh
python -m unittest discover -s tests -v
```

A directory or explicit Python file set works:

```sh
debt-compiler extract sample/legacy/batch.py --out /tmp/debt-pack
```

Library:

```python
from debt_compiler import extract
summary = extract(["sample/legacy/batch.py"], "/tmp/debt-pack")
```

## Optional Jev evidence review

After extraction, `jev-review` can ask TypeSafe Jev whether each existing annotation or invariant claim is supported by the evidence already in the pack:

```sh
TYPESAFE_API_KEY=... debt-compiler jev-review /tmp/debt-pack
```

It writes `/tmp/debt-pack/jev_review.json` by default. This is an advisory external API call, not part of extraction: it never changes flags, invariant status, risk buckets, exit codes, or generated tests. A `supported` verdict means the supplied pack evidence supports a claim, not that the claim is universally true. Effects outside the pack remain unknown.

## Contract pack

- `graph.md`: tree-sitter functions, calls, assignments, and syntactic data dependencies. Call targets remain unresolved; this is not control-flow analysis, alias analysis, or static proof.
- `annotations.json`: sacred-cow tags with repository-relative file/line, snippet, reason, and Git blame commit/subject plus reproducible `git blame`/`git show` commands to run at that repository root. Untracked lines and missing repositories are explicitly marked. Sleep and retry heuristics always say do-not-touch.
- `invariants.json`: evidence-backed **candidates**, English rules, named machine checks, and per-fixture results. The four narrow detectors recognize this sample's balance guard, rounding call, event enumeration, and negative guard. They are not a general invariant solver. Empty means no recognized candidate.
- `tests/test_characterization.py` and `tests/observations.json`: captured legacy observations and runnable regression assertions against both implementations. Source paths are relative to the recorded repository/source root. Replay from any working directory, or relocate the checkout with `--source-root` as described below. Tests compare against the stored legacy baseline, not regenerated expectations.
- `dual_run.md`: actual observations and legacy/modern comparison, including exceptions, stdout/stderr, and sleep requests.
- `risk.md`: touch / do-not-touch / needs-human.
- `manifest.json`: format/tool versions, extraction time, selected relative source files, source-root locations, Git HEAD (null for an unborn/non-Git repository), and exit classification/code. This version produces a single-root pack; mixed-repository slices are rejected.

## Runtime convention and limits

Static extraction works for any selected Python files. Runtime characterization requires a selected `batch.py`, a sibling `../fixtures/cases.json`, and a callable `run(case, sleep=...)`. Fixtures are a nonempty JSON list of unique `{"name": "case-name", "input": {...}}` objects. A sibling `../modern/batch.py` is optional. Without fixtures or a recognizable top-level synchronous `run(case, sleep=...)`, extraction still writes every static artifact, dual-run says NOT RUN, generated characterization skips with NOT RUN, and the manifest records `runtime-skipped` with exit 0. Missing conventions are detected without importing the target. Multiple runnable slices must be extracted separately.

**Only extract trusted code when this runtime convention is present.** The harness imports and executes the selected module automatically. A subprocess and five-second timeout isolate interpreter state; they are not a security sandbox. Each case gets a fresh process. Sleep injection records durations instead of waiting. Real timing, external services, arbitrary filesystem/network side effects, and process descendants are not verified. Direct sleeps outside the injection seam can time out.

The sample intentionally retains `sleep(50)`, retries, a bare except, manual cents conversion, and an overdraft flag. The modern target is equivalent on the ten fixtures and preserves the timing requests. A successful comparison does not approve removal of either workaround. Inputs in the sample are JSON objects with decimal strings and ordered credit/debit operations; production input validation is outside this MVP.

Machine checks cover normal returns only: nonnegative balance given a nonnegative initial balance and disabled overdraft; half-up applied amounts; event index order; and negative-amount rejection. Error returns and raised exceptions are characterized but excluded from invariant checks. Passing fixtures is not a universal proof. Git subjects are historical evidence, not authoritative explanations.

Exit codes: `0` pack written with no observed mismatch (may include explicit NOT RUN), `1` pack written with an output mismatch or failed invariant, `2` invalid input, parse failure, or harness failure. Existing named pack artifacts are overwritten on re-extraction; use a new output directory to retain a baseline. Source files are not edited.

## Portable replay and pack diff

All compiler-generated source locators are relative. `manifest.json` records the
invocation directory as `default_source_root`, relative to the pack directory;
`source_roots[0].path` locates the repository root relative to that invocation
directory. Together they preserve the original default without a machine-absolute
path. In a non-Git slice, the root is the invocation directory when it contains
all selected files, otherwise their common parent. The default works as long as
that relative layout remains intact. Application-returned strings are preserved
verbatim in observations, even if the application prints its own absolute paths.

To move a pack and its source independently, provide the new **repository/source
root**, not the legacy module directory (unless that was the non-Git source root):

```sh
python /tmp/debt-pack/tests/test_characterization.py --source-root "$PWD" -v
```

Without `--source-root`, the generated script and `unittest discover` both use
the manifest default, regardless of the current directory. The installed
`debt_compiler` package remains required. Tests replay stored legacy baselines;
they do not regenerate expectations.

```sh
debt-compiler extract sample/legacy --out /tmp/debt-old
debt-compiler extract sample/legacy --out /tmp/debt-new
debt-compiler diff /tmp/debt-old /tmp/debt-new
```

Diff compares annotation identities (`file:line + tag`) and their snippet/reason/
risk, invariant candidates and fixture checks, dual-run case names and
MATCH/MISMATCH/LEGACY ONLY/NOT RUN verdicts, and the contents of the three risk
buckets. It ignores manifest timestamps, source-location defaults (including
absolute defaults), and Git provenance. Reordered JSON keys/list evidence do not
count as changes. Exit 0 means identical-enough; exit 1 means any of these
contract categories changed; exit 2 means a missing/invalid/unsupported pack.
This is a contract diff, not a general output or code diff: changed return values
that still MATCH each other and leave the compared contracts unchanged are not
reported. Packs from the earlier format without a manifest must be re-extracted.

The non-sample result is in `packs/foreign-1`; see its `REAL_TARGET.md` for the
actual tags, NOT RUN status, and missed semantics. The unmodified MIT source and
pinned attribution are in `fixtures/foreign`. The rounding regression adds an
11th input only in a temporary test checkout: a mutant still passes the original
ten, then fails on `2.675`. The shipped sample stays at ten MATCH cases.

`DiffClassifier` in `harness.py` is only a protocol for `noise | semantic_break | invariant_violation`. No classifier or paid API is called. Joern and optional LLM drafting are deferred.

Parser API reference: [py-tree-sitter](https://github.com/tree-sitter/py-tree-sitter).
