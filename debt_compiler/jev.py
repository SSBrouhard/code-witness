"""Optional advisory review of extracted claims through TypeSafe Jev."""
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen


ENDPOINT = "https://api.typesafe.ai/v1/systemone"
VERDICTS = {"supported", "insufficient", "contradicted"}


def claims(pack):
    """Return reviewable claims already present in a contract pack."""
    pack = Path(pack)
    annotations = json.loads((pack / "annotations.json").read_text())
    invariants = json.loads((pack / "invariants.json").read_text())
    result = []
    for annotation in annotations:
        result.append({
            "kind": "annotation",
            "location": annotation["location"],
            "claim": annotation["reason"],
            "evidence": {"snippet": annotation["snippet"], "git": annotation["git"]},
        })
    for invariant in invariants:
        result.append({
            "kind": "invariant",
            "location": ", ".join(f"{item['file']}:{item['line']}" for item in invariant["evidence"]),
            "claim": invariant["english"],
            "evidence": {"source": invariant["evidence"], "fixture_checks": invariant.get("fixture_checks", [])},
        })
    return result


def review(pack, api_key=None, timeout=10):
    """Return Jev's advisory assessment without changing the contract pack."""
    api_key = api_key or os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        raise ValueError("Set TYPESAFE_API_KEY to run an advisory Jev review")
    records = claims(pack)
    questions = {
        f"claim_{index}": {
            "type": "choice",
            "instructions": {
                "question": "Does this claim's supplied evidence support the claim? Do not infer facts absent from the evidence.",
                "claim": record["claim"],
                "evidence": record["evidence"],
            },
            "criteria": {
                "supported": "The supplied evidence directly supports the claim.",
                "insufficient": "The evidence may be relevant but does not establish the claim.",
                "contradicted": "The supplied evidence conflicts with the claim.",
            },
        }
        for index, record in enumerate(records)
    }
    payload = json.dumps({"model": "jev-latest", "state": {"review_scope": "one claim per question; evidence is in that question"}, "questions": questions}).encode()
    request = Request(ENDPOINT, data=payload, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read())
    if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
        raise ValueError("Jev returned an invalid review response")
    answers = result["answers"]
    for index, record in enumerate(records):
        answer = answers.get(f"claim_{index}")
        if answer is None:
            record["review"] = {
                "verdict": "not-returned",
                "confidence": None,
                "probabilities": None,
            }
            continue
        if not isinstance(answer, dict):
            raise ValueError("Jev returned an invalid claim review")
        if answer.get("choice") not in VERDICTS:
            raise ValueError("Jev returned an unknown review verdict")
        record["review"] = {
            "verdict": answer["choice"],
            "confidence": answer.get("confidence"),
            "probabilities": answer.get("probabilities"),
        }
    return {
        "format_version": 1,
        "model": result.get("model", "unknown"),
        "claims": records,
        "limitations": [
            "Jev output is advisory and never changes deterministic flags, invariants, risk buckets, or exit codes.",
            "A supported verdict means the supplied pack evidence supports the claim, not that the claim is universally true.",
            "Effects outside the recorded contract pack remain unknown.",
        ],
    }
