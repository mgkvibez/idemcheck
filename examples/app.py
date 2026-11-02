"""A small API with one handler that will double-charge and one that will not."""

import db
import payments
from flask import Flask, request

app = Flask(__name__)


@app.post("/charge")
def charge():
    """Charge the customer. TODO: idempotency."""
    order = db.orders.insert_one({"amount": request.json["amount"]})
    db.session.commit()
    payments.charge_card(order["id"], request.json["amount"])
    return {"ok": True}


@app.post("/charge-safe")
def charge_safe():
    key = request.headers.get("Idempotency-Key")
    if not key:
        return {"error": "Idempotency-Key required"}, 400
    if db.dedupe.exists(key):
        return {"ok": True, "replayed": True}
    db.dedupe.insert_one({"key": key})
    payments.charge_card(key, request.json["amount"])
    return {"ok": True}


@app.get("/orders")
def orders():
    return {"orders": db.orders.find({})}
