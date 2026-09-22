"""Small rewrite target. Equivalent on fixtures, not approved for production."""
import time
from decimal import Decimal, ROUND_HALF_UP


def run(case, sleep=time.sleep):
    for attempt in range(3):
        if attempt >= case.get("transient_failures", 0):
            break
        if attempt == 2:
            return {"error": "settlement unavailable", "events": []}
        sleep(50)

    balance = Decimal(case["balance"])
    events = []
    for index, operation in enumerate(case["operations"]):
        raw = Decimal(operation["amount"])
        amount = raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        # Preserve the legacy Decimal exponent in string balances.
        amount = Decimal(int(amount * 100)) / 100
        status = "applied"
        if raw < 0:
            status = "negative-rejected"
        elif operation["kind"] == "credit":
            balance += amount
        elif operation["kind"] == "debit":
            if balance < amount and not case.get("overdraft_enabled", False):
                status = "overdraft-rejected"
            else:
                balance -= amount
        else:
            raise ValueError("unknown operation")
        event = {"index": index, "status": status, "balance": str(balance)}
        if status == "applied":
            event["amount"] = format(amount, ".2f")
        events.append(event)
    return {"balance": str(balance), "events": events}
