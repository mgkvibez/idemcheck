"""One endpoint, two deliveries of the same key, one charge."""

from idemcheck.runtime import SQLiteStore, current_idempotency_key, idempotent

store = SQLiteStore("demo-idempotency.db")
charges = []


@idempotent(store=store)
def charge_card(amount, idempotency_key=None):
    charges.append(amount)
    return {"charged": amount, "key": current_idempotency_key()}


if __name__ == "__main__":
    print("first:  ", charge_card(2500, idempotency_key="order-41"))
    print("retry:  ", charge_card(2500, idempotency_key="order-41"))
    print("charges that reached the gateway:", charges)
