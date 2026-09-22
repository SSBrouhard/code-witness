# Foreign target: existing HTTP retry helper

Used `fixtures/foreign/http.py`, an unmodified 54-line MIT module from
`eugeniughelbur/obsidian-second-brain`, originally
`scripts/research/lib/http.py`. The exact commit, origin URL, SHA-256, and
license are in `fixtures/foreign/SOURCE.txt` and `fixtures/foreign/LICENSE`
at the debt-compiler repository root. Copy and Git blob matched byte-for-byte.

The search covered `~/Documents`, `~/src`, and `~/code`. The latter two were
absent. The Python Git checkouts found in Documents had third-party origins;
no user-owned upstream repository was verified, so this uses the requested
foreign-module fallback. The source was already available locally; no network
fetch, dependency installation, import, or execution of this module occurred.

Reproduce from the debt-compiler repository root:

```sh
debt-compiler extract fixtures/foreign/http.py --out packs/foreign-1
python packs/foreign-1/tests/test_characterization.py --source-root . -v
```

## Actually tagged

Eight annotations: five `retry` tags marked **do-not-touch** at lines 13, 34,
40, 41, and 47; three `magic-number` tags marked **needs-human** at lines 23,
34, and 43. These catch the retry import/configuration references, 15-second
default timeout, retry count 3, and HTTP status-list constant 500. The graph
records functions, calls such as `Retry`, `HTTPAdapter`, session mounts, and
assignment dependencies. It does not resolve their behavior.

Retry tags are lexical hints, not five distinct retry mechanisms. The import
at line 13 is conservatively tagged because its name contains `retry`.
Constants 502, 503, and 504 share line 43: the one-file/line/tag deduplication
keeps only the first snippet, 500. No sleep, bare-except, rounding, or
feature-flag tags were emitted.

## NOT RUN

There is no `batch.py`/fixture/top-level `run(case, sleep=...)` convention.
The dual-run report says NOT RUN, generated characterization reports a skipped
test with NOT RUN, and `manifest.json` records `runtime-skipped`, exit 0.
No HTTP requests were made. `invariants.json` is empty because none of the
four sample-shaped invariant detectors applies. This is a static pack, not
runtime verification. The copied file is untracked, so blame is unavailable;
upstream provenance is recorded in SOURCE.txt rather than invented Git blame.

## What the heuristics missed

- Retry semantics: the allowed GET/HEAD methods, backoff, selected 5xx statuses,
  and `raise_on_status=False` are present in graph text but not extracted as
  contracts or validated.
- Requests and adapter calls are unresolved; the graph does not understand HTTP
  effects, timeout propagation, redirects, or retry behavior inside urllib3.
- `DEFAULT_TIMEOUT = 15` is flagged as a number, but the extractor cannot tell
  that this helper does not itself apply that timeout to requests.
- User-Agent/contact-email construction and HTTP/HTTPS adapter coverage are
  not recognized as invariants.
- The optional import's `except ImportError` is not a bare-except tag. No claim
  is made about whether that fallback is appropriate.
- Type annotations and function-default dependencies do not have dedicated
  graph edges. The graph is lexical, not a resolved data-flow proof.
