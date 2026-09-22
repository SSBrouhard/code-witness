"""Old settlement batch. The timing hack is intentionally preserved."""
import time
from decimal import Decimal, ROUND_HALF_UP


def run(case, sleep=time.sleep):
    OVERDRAFT_ENABLED = case.get("overdraft_enabled", False)
    balance = Decimal(case["balance"])
    events = []
    retries = 0
    while True:
        try:
            if retries < case.get("transient_failures", 0):
                raise OSError("settlement service busy")
            break
        except:
            retries += 1
            if retries >= 3:
                return {"error": "settlement unavailable", "events": []}
            sleep(50)  # Do not remove: nobody remembers why settlement needs this.
    for index, operation in enumerate(case["operations"]):
        amount = Decimal(operation["amount"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        # Old manual cents conversion left in place.
        cents = int(amount * 100)
        amount = Decimal(cents) / 100
        if Decimal(operation["amount"]) < 0:
            events.append({"index": index, "status": "negative-rejected", "balance": str(balance)})
            continue
        if operation["kind"] == "credit":
            balance += amount
        elif operation["kind"] == "debit":
            if balance - amount < 0 and not OVERDRAFT_ENABLED:
                events.append({"index": index, "status": "overdraft-rejected", "balance": str(balance)})
                continue
            balance -= amount
        else:
            raise ValueError("unknown operation")
        events.append({"index": index, "status": "applied", "amount": format(amount, ".2f"), "balance": str(balance)})
    return {"balance": str(balance), "events": events}
