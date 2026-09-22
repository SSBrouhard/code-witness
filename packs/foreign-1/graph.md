# Tree-sitter graph

Lexical call sites and assignment read/write dependencies. No runtime dispatch, alias, control-flow, or whole-program proof. RHS identifiers include attribute names; edges are conservative syntactic facts. Joern is not used in v0.

- **function** `fixtures/foreign/http.py:20` in `<module>.get_contact_email@20`: `()`
- **assignment** `fixtures/foreign/http.py:23` in `<module>`: `DEFAULT_TIMEOUT = 15`
- **assignment** `fixtures/foreign/http.py:24` in `<module>`: `USER_AGENT_BASE = "obsidian-second-brain/1.0"`
- **function** `fixtures/foreign/http.py:27` in `<module>.polite_user_agent@27`: `(client_name: str, contact_email: str | None)`
- **function** `fixtures/foreign/http.py:34` in `<module>.get_session@34`: `(retries: int = 3, backoff: float = 1.0)`
- **assignment** `fixtures/foreign/http.py:39` in `<module>.get_session@34`: `sess = requests.Session()`
- **call** `fixtures/foreign/http.py:39` in `<module>.get_session@34`: `requests.Session`
- **assignment** `fixtures/foreign/http.py:40` in `<module>.get_session@34`: `retry = Retry(         total=retries,         backoff_factor=backoff,         status_forcelist=(500, 502, 503, 504),         allowed_methods=("GET", "HEAD"),         raise_on_status=False,     )`
- **call** `fixtures/foreign/http.py:40` in `<module>.get_session@34`: `Retry`
- **assignment** `fixtures/foreign/http.py:47` in `<module>.get_session@34`: `adapter = HTTPAdapter(max_retries=retry)`
- **call** `fixtures/foreign/http.py:47` in `<module>.get_session@34`: `HTTPAdapter`
- **call** `fixtures/foreign/http.py:48` in `<module>.get_session@34`: `sess.mount`
- **call** `fixtures/foreign/http.py:49` in `<module>.get_session@34`: `sess.mount`
- **call** `fixtures/foreign/http.py:50` in `<module>.get_session@34`: `sess.headers.update`
- **call** `fixtures/foreign/http.py:50` in `<module>.get_session@34`: `polite_user_agent`
- **call** `fixtures/foreign/http.py:50` in `<module>.get_session@34`: `get_contact_email`
- **assignment** `fixtures/foreign/http.py:54` in `<module>`: `__all__ = ["get_session", "polite_user_agent", "DEFAULT_TIMEOUT", "USER_AGENT_BASE"]`

## Edges

- `fixtures/foreign/http.py:<module>.get_session@34:Session` -> syntactic data dependency -> `sess` (site `fixtures/foreign/http.py#5`)
- `fixtures/foreign/http.py:<module>.get_session@34:requests` -> syntactic data dependency -> `sess` (site `fixtures/foreign/http.py#5`)
- `fixtures/foreign/http.py:<module>.get_session@34` -> calls (unresolved) -> `requests.Session` (site `fixtures/foreign/http.py#6`)
- `fixtures/foreign/http.py:<module>.get_session@34:Retry` -> syntactic data dependency -> `retry` (site `fixtures/foreign/http.py#7`)
- `fixtures/foreign/http.py:<module>.get_session@34:allowed_methods` -> syntactic data dependency -> `retry` (site `fixtures/foreign/http.py#7`)
- `fixtures/foreign/http.py:<module>.get_session@34:backoff` -> syntactic data dependency -> `retry` (site `fixtures/foreign/http.py#7`)
- `fixtures/foreign/http.py:<module>.get_session@34:backoff_factor` -> syntactic data dependency -> `retry` (site `fixtures/foreign/http.py#7`)
- `fixtures/foreign/http.py:<module>.get_session@34:raise_on_status` -> syntactic data dependency -> `retry` (site `fixtures/foreign/http.py#7`)
- `fixtures/foreign/http.py:<module>.get_session@34:retries` -> syntactic data dependency -> `retry` (site `fixtures/foreign/http.py#7`)
- `fixtures/foreign/http.py:<module>.get_session@34:status_forcelist` -> syntactic data dependency -> `retry` (site `fixtures/foreign/http.py#7`)
- `fixtures/foreign/http.py:<module>.get_session@34:total` -> syntactic data dependency -> `retry` (site `fixtures/foreign/http.py#7`)
- `fixtures/foreign/http.py:<module>.get_session@34` -> calls (unresolved) -> `Retry` (site `fixtures/foreign/http.py#8`)
- `fixtures/foreign/http.py:<module>.get_session@34:HTTPAdapter` -> syntactic data dependency -> `adapter` (site `fixtures/foreign/http.py#9`)
- `fixtures/foreign/http.py:<module>.get_session@34:max_retries` -> syntactic data dependency -> `adapter` (site `fixtures/foreign/http.py#9`)
- `fixtures/foreign/http.py:<module>.get_session@34:retry` -> syntactic data dependency -> `adapter` (site `fixtures/foreign/http.py#9`)
- `fixtures/foreign/http.py:<module>.get_session@34` -> calls (unresolved) -> `HTTPAdapter` (site `fixtures/foreign/http.py#10`)
- `fixtures/foreign/http.py:<module>.get_session@34` -> calls (unresolved) -> `sess.mount` (site `fixtures/foreign/http.py#11`)
- `fixtures/foreign/http.py:<module>.get_session@34` -> calls (unresolved) -> `sess.mount` (site `fixtures/foreign/http.py#12`)
- `fixtures/foreign/http.py:<module>.get_session@34` -> calls (unresolved) -> `sess.headers.update` (site `fixtures/foreign/http.py#13`)
- `fixtures/foreign/http.py:<module>.get_session@34` -> calls (unresolved) -> `polite_user_agent` (site `fixtures/foreign/http.py#14`)
- `fixtures/foreign/http.py:<module>.get_session@34` -> calls (unresolved) -> `get_contact_email` (site `fixtures/foreign/http.py#15`)
