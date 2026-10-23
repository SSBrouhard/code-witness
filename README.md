# code-witness

This repository is code-witness. The command is `debt-compiler`.

`debt-compiler` is a local Python slice compiler: tree-sitter graph, Git archaeology, contract candidates, and executable characterization. The deterministic extractor has no LLM, API key, cloud service, or rewrite engine. An optional Jev evidence review is separate and requires its own API key.

The license is MIT. Copyright (c) 2026 Stephen Brouhard. The full text is in [LICENSE](LICENSE).

## Run

Python 3.10+ is required. From this repository root:

```sh
pip install -e .
debt-compiler extract sample/legacy --out /tmp/debt-pack
python -m unittest discover -s /tmp/debt-pack/tests -v
```

To require behavioral verification, use `debt-compiler extract sample/legacy
--out /tmp/debt-pack --require-runtime`. The JSON summary and manifest expose
`verification.status`, its reason and case counts, plus `exit_code`. The library
accepts the same requirement as `extract(paths, out, require_runtime=True)`.

For an isolated install, first run `python3 -m venv .venv` and `source .venv/bin/activate`.

Run the development checks:

```sh
python -m unittest discover -s tests -v
```

GitHub Actions runs this suite on Python 3.10 and 3.14 using Linux runners.

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
- `manifest.json`: format/tool versions, extraction time, relative source locators, Git HEAD and dirty state, SHA-256 source/fixture fingerprints, interpreter and extractor identity, verification outcome and exit classification/code. This version produces a single-root pack; mixed-repository slices are rejected.

## Runtime convention and limits

Static extraction works for any selected Python files. Runtime characterization requires a selected `batch.py`, a sibling `../fixtures/cases.json`, and a callable `run(case, sleep=...)`. Fixtures are a nonempty JSON list of unique `{"name": "case-name", "input": {...}}` objects. A sibling `../modern/batch.py` is optional. Without fixtures or a recognizable top-level synchronous `run(case, sleep=...)`, extraction still writes every static artifact, dual-run says NOT RUN, generated characterization skips with NOT RUN, and verification is INCONCLUSIVE with exit 0 by default. Missing conventions are detected without importing the target. Multiple runnable slices must be extracted separately.

Extraction success and verification are separate:

| Verification | Meaning | Default exit | With `--require-runtime` |
| --- | --- | --- | --- |
| `PASS` | All supplied legacy/modern fixture comparisons pass; each detected invariant has passing applicable evidence for both targets | 0 | 0 |
| `FAIL` | Observed mismatch or failed candidate check | 1 | 1 |
| `BLOCKED` | Harness execution failed, source/fixture contents changed during extraction, or required runtime targets/fixtures are absent | 2 | 2 |
| `INCONCLUSIVE` | Static-only or legacy-only evidence, incomplete coverage, or a candidate lacks applicable fixtures | 0 | 2 |

Requiring runtime promotes missing conventions or a missing modern target to
BLOCKED. Static-only extraction remains useful and successful without that flag;
it never reports verification PASS. Generated tests explicitly skip missing
coverage. They inherit an extraction's runtime requirement, and their standalone
script also accepts `--require-runtime`. Harness failures preserve the static
artifacts and completed observations with a BLOCKED result, unless a completed
case already demonstrated a mismatch (FAIL). PASS is fixture-scoped evidence,
not equivalence proof.

**Only extract trusted code when this runtime convention is present.** The harness imports and executes the selected module automatically. A subprocess and five-second timeout isolate interpreter state; they are not a security sandbox. Each case gets a fresh process. Sleep injection records durations instead of waiting. Real timing, external services, arbitrary filesystem/network side effects, and process descendants are not verified. Direct sleeps outside the injection seam can time out.

The sample intentionally retains `sleep(50)`, retries, a bare except, manual cents conversion, and an overdraft flag. The modern target is equivalent on the ten fixtures and preserves the timing requests. A successful comparison does not approve removal of either workaround. Inputs in the sample are JSON objects with decimal strings and ordered credit/debit operations; production input validation is outside this MVP.

Machine checks cover normal returns only: nonnegative balance given a nonnegative initial balance and disabled overdraft; half-up applied amounts; event index order; and negative-amount rejection. Error returns and raised exceptions are characterized but excluded from invariant checks. Passing fixtures is not a universal proof. Git subjects are historical evidence, not authoritative explanations.

Exit codes: `0` successful extraction with no observed failure and no unmet runtime requirement, `1` demonstrated mismatch or failed invariant, `2` invalid input, parse failure, blocked execution, or insufficient required verification. Invalid inputs can fail before a pack is written. Existing named pack artifacts are overwritten on re-extraction; use a new output directory to retain a baseline. Source files are not edited.

## Portable replay and pack diff

All compiler-generated source locators are relative. Format v2 records the
repository/source root as `default_source_root`, relative to the pack directory,
with `source_roots[0].path` set to `.`. In a non-Git slice, the root is the invocation directory when it contains
all selected files, otherwise their common parent. The default works as long as
that relative layout remains intact. Application-returned strings are preserved
verbatim in observations, even if the application prints its own absolute paths.

SHA-256 fingerprints cover selected Python files, the discovered modern target,
and fixture JSON bytes. Git HEAD alone is not content identity: `git_dirty`
records working-tree changes (null without Git), and hashes identify captured
contents even with an unborn HEAD or uncommitted changes. Fingerprints are
checked again after execution; a changed or missing input blocks verification.
The environment records Python version/implementation/cache tag, OS/architecture,
parser dependency versions, and a digest of the extractor's Python source.
No interpreter executable path is stored. This is bounded provenance, not an
attestation or complete environment lock: imported target dependencies, external
resources, and transient changes between reads are not fingerprinted or isolated.
The runtime target is compiled from current source bytes to avoid stale `.pyc`
observations; imported helpers retain normal Python import behavior.

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
risk, invariant candidates and fixture checks, runtime coverage, case names,
fixture inputs, legacy and modern observations (returns, exception type/message,
stdout, stderr, requested sleeps), verdicts, and the three risk buckets. A change
is detected even when both targets still MATCH. It ignores extraction timestamps,
source-location defaults, environment and provenance metadata. Reordered JSON
keys, cases and invariant evidence do not count as changes; ordered application
results and sleep sequences retain their order. Exit 0 means identical-enough;
exit 1 means contract or captured behavior changed; exit 2 means a
missing/invalid/unsupported pack. Provenance-only changes do not imply behavioral
changes. Formats 1 and 2 can be compared and replayed; v1 lacks the new provenance
and verification guarantees. Unknown versions are rejected by diff. Packs without
a manifest must be re-extracted.

The non-sample result is in `packs/foreign-1`; see its `REAL_TARGET.md` for the
actual tags, NOT RUN status, and missed semantics. The unmodified MIT source and
pinned attribution are in `fixtures/foreign`. The rounding regression adds an
11th input only in a temporary test checkout: a mutant still passes the original
ten, then fails on `2.675`. The shipped sample stays at ten MATCH cases.

`DiffClassifier` in `harness.py` is only a protocol for `noise | semantic_break | invariant_violation`. No classifier or paid API is called. Joern and optional LLM drafting are deferred.

Parser API reference: [py-tree-sitter](https://github.com/tree-sitter/py-tree-sitter).

## License

The license is MIT. See [LICENSE](LICENSE).
