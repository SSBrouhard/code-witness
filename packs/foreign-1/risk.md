# Risk

## Touch

Documentation and generated contract-pack artifacts. No source transformation is performed.

## Do-not-touch

- `fixtures/foreign/http.py:13` (retry): Retry workaround may encode an external-system contract. Preserve until proven otherwise; attach Git context when present.
- `fixtures/foreign/http.py:34` (retry): Retry workaround may encode an external-system contract. Preserve until proven otherwise; attach Git context when present.
- `fixtures/foreign/http.py:40` (retry): Retry workaround may encode an external-system contract. Preserve until proven otherwise; attach Git context when present.
- `fixtures/foreign/http.py:41` (retry): Retry workaround may encode an external-system contract. Preserve until proven otherwise; attach Git context when present.
- `fixtures/foreign/http.py:47` (retry): Retry workaround may encode an external-system contract. Preserve until proven otherwise; attach Git context when present.

## Needs-human

All code changes require review. Fixture agreement is limited evidence, not equivalence proof. Candidate invariants require approval; missing history is not permission to remove workarounds.

- `fixtures/foreign/http.py:23` (magic-number): Unexplained numeric constant may encode a business or timing rule.
- `fixtures/foreign/http.py:34` (magic-number): Unexplained numeric constant may encode a business or timing rule.
- `fixtures/foreign/http.py:43` (magic-number): Unexplained numeric constant may encode a business or timing rule.
- No runtime characterization: No selected batch.py with sibling fixtures/cases.json
