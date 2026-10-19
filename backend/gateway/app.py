"""
Mock payment gateway / PSP — its own FastAPI app (SIMULATED).

Run as a separate local service (the merchant backend then makes a real
service-to-service HTTP call):
    COMMERCE_DB_PATH=backend/db/demo1.db python -m uvicorn backend.gateway.app:app --port 8100
and start the backend with MOCK_GATEWAY_URL=http://127.0.0.1:8100.

  GET  /health
  POST /v1/authorizations                 Idempotency-Key + X-Gateway-Key headers
  POST /v1/authorizations/{gateway_ref}/void
  /v1/payment_sessions… + /hosted/{id}    Simulated Secure Payment Provider (CH-6, backend/gateway/provider.py)

Simplification (demo): the simulated network and issuer read the same local
SQLite database as ShopSphere, standing in for their own systems.
"""
import hashlib
import json
import os
import secrets
from typing import Literal, Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from backend.db.schema import GatewayAuthorization, PaymentMethod
from backend.db.session_utils import get_session
from backend.gateway import issuer, network
from backend.vic.common import new_ref

GATEWAY_KEY = os.getenv("MOCK_GATEWAY_KEY", "shopsphere-demo-merchant-key")
ACTOR = "Mock gateway / PSP"

app = FastAPI(title="Mock payment gateway (SIMULATED)", version="1.0.0")

# CH-6: the Simulated Secure Payment Provider (hosted page + payment sessions) is part of this service
from backend.gateway.provider import router as provider_router  # noqa: E402
app.include_router(provider_router)


class Instrument(BaseModel):
    type: Literal["vic_credential", "card_on_file"]
    credential_ref: Optional[str] = Field(None, max_length=80)
    token_ref: Optional[str] = Field(None, max_length=100)


class AuthorizationRequest(BaseModel):
    merchant_id: str
    amount: float = Field(..., gt=0)
    currency: str = "USD"
    order_ref: str
    checkout_id: str
    customer_ref: str
    instrument: Instrument


def _check_key(key: Optional[str]) -> None:
    if not key or not secrets.compare_digest(key, GATEWAY_KEY):
        raise HTTPException(status_code=401, detail="Unknown merchant credentials")


@app.get("/health")
def health():
    return {"status": "ok", "service": "mock-gateway", "simulated": True}


@app.post("/v1/authorizations")
def authorize(req: AuthorizationRequest, idempotency_key: str = Header(..., alias="Idempotency-Key"),
              x_gateway_key: Optional[str] = Header(None), x_sim_scenario: Optional[str] = Header(None)):
    _check_key(x_gateway_key)
    body = req.model_dump()
    request_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    with get_session() as db:
        prior = db.query(GatewayAuthorization).filter_by(idempotency_key=idempotency_key).first()
        if prior:
            if prior.request_hash != request_hash:
                raise HTTPException(status_code=409, detail={"code": "IDEMPOTENCY_CONFLICT",
                                                             "message": "Same key, different request"})
            replay = json.loads(prior.response_json)
            replay["duplicate"] = True        # the original result; nothing authorized twice
            return replay

        gateway_ref = new_ref("gw")
        payment_method_id = None
        steps = [{"actor": ACTOR, "result": "pass", "simulated": True,
                  "detail": f"Merchant authenticated; idempotency key accepted; {req.currency} {req.amount:.2f}"}]
        out = {"gateway_ref": gateway_ref, "status": "declined", "auth_code": None, "decline_reason": None,
               "duplicate": False, "simulated": True, "steps": steps}

        if x_sim_scenario == "timeout":
            steps.append({"actor": ACTOR, "result": "timeout", "simulated": True,
                          "detail": "No answer from the network in time (demo timeout scenario)"})
            out.update(status="timeout", decline_reason="PAYMENT_TIMEOUT")
        else:
            try:
                route = network.visa_agent_credential if req.instrument.type == "vic_credential" else network.card_on_file
                card, acct, net_step = route(db, body)
                payment_method_id = card.payment_method_id
                steps.append({**net_step, "simulated": True})
                iss = issuer.authorize(db, card, acct, req.amount, x_sim_scenario)
                steps.append({"actor": iss["actor"], "result": iss["result"], "simulated": True,
                              "detail": iss["detail"]})
                if iss["result"] == "approved":
                    out.update(status="approved", auth_code=iss["auth_code"])
                else:
                    out.update(decline_reason=iss["reason"])
            except network.NetworkDecline as exc:
                steps.append({"actor": exc.actor, "result": "blocked", "simulated": True, "detail": exc.detail})
                out.update(decline_reason=exc.reason)

        db.add(GatewayAuthorization(gateway_ref=gateway_ref, idempotency_key=idempotency_key,
                                    request_hash=request_hash, merchant_id=req.merchant_id, amount=req.amount,
                                    currency=req.currency, instrument_type=req.instrument.type,
                                    status=out["status"], payment_method_id=payment_method_id,
                                    response_json=json.dumps(out)))
        db.commit()
        return out


@app.post("/v1/authorizations/{gateway_ref}/void")
def void(gateway_ref: str, x_gateway_key: Optional[str] = Header(None)):
    """Release an approved authorization that won't be captured (e.g. the order
    couldn't be created). The issuer hold is given back."""
    _check_key(x_gateway_key)
    with get_session() as db:
        row = db.query(GatewayAuthorization).filter_by(gateway_ref=gateway_ref).first()
        if not row:
            raise HTTPException(status_code=404, detail="Unknown authorization")
        if row.status == "approved":
            card = db.query(PaymentMethod).filter_by(payment_method_id=row.payment_method_id).first()                 if row.payment_method_id else None
            issuer.release(db, card, row.amount)
            row.status = "voided"
            db.commit()
        return {"gateway_ref": gateway_ref, "status": row.status, "simulated": True}
