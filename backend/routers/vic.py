"""
VIC-inspired simulator API (Part 2) — LOCAL SIMULATION, not a Visa API.
No endpoint here exists at Visa; nothing is sent to Visa or Chase.

Agent-payment enrollment (Chase Visa credit-card fixtures)
  GET  /api/vic/status
  POST /api/vic/enrollment/start          {card_account_id}             → simulated passkey challenge
  POST /api/vic/enrollment/complete       {card_account_id, challenge_id, nonce, user_verified}
  POST /api/vic/enrollment/{suspend|resume|revoke}   {card_account_id}

Payment instructions (one exact checkout)
  POST /api/vic/instructions              {checkout_id, consent_mode}   → instruction + passkey challenge
  POST /api/vic/instructions/{id}/authenticate  {challenge_id, nonce, user_verified}
  POST /api/vic/instructions/{id}/cancel
  GET  /api/vic/instructions/{id}         status, lifecycle events, commerce signals (safe view)

Demo controls
  POST /api/vic/demo/scenario             {outcome: decline | timeout | duplicate | none}
"""
from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.auth.dependencies import CurrentUser, get_current_user
from backend.db.schema import CardAccount, SimScenario, VicLifecycleEvent
from backend.db.session_utils import get_session
from backend.shop.eligibility import require_shopper
from backend.vic import enrollment, instructions, signals
from backend.vic.common import VicError, short

router = APIRouter(prefix="/api/vic", tags=["vic-simulator"], dependencies=[Depends(require_shopper)])

DISCLAIMER = ("Visa Intelligent Commerce-inspired LOCAL SIMULATION. Not connected to Visa or Chase; "
              "agent tokens are not Visa Token Service tokens.")


def _error(exc: VicError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": {"code": exc.code, "message": exc.message,
                                                                    **exc.detail}})


class CardRef(BaseModel):
    card_account_id: str = Field(..., max_length=50)


class PasskeyAssertion(BaseModel):
    challenge_id: str = Field(..., max_length=80)
    nonce: str = Field("", max_length=120)
    user_verified: bool = False


class EnrollComplete(CardRef, PasskeyAssertion):
    pass


class NewInstruction(BaseModel):
    checkout_id: str = Field(..., max_length=50)
    consent_mode: Literal["button", "typed"] = "button"           # CH-8: explicit approvals only


class Scenario(BaseModel):
    outcome: Literal["decline", "timeout", "duplicate", "none"]


def status_view(db, user_id: str) -> dict:
    cards = (db.query(CardAccount).filter_by(user_id=user_id, account_type="credit_card", network="Visa",
                                              status="active", agent_pay_capable=True).all())   # CH-9
    return {"simulated": True, "disclaimer": DISCLAIMER, "cards": [enrollment.view(db, user_id, a) for a in cards]}


@router.get("/status")
def vic_status(user: CurrentUser = Depends(get_current_user)):
    with get_session() as db:
        return status_view(db, user.user_id)


@router.post("/enrollment/start")
def enrollment_start(req: CardRef, user: CurrentUser = Depends(get_current_user)):
    with get_session() as db:
        try:
            tok, challenge = enrollment.start(db, user.user_id, req.card_account_id)
        except VicError as exc:
            return _error(exc)
        return {"status": tok.status, "agent_token": short(tok.agent_token_ref), "passkey": challenge,
                "disclaimer": DISCLAIMER}


@router.post("/enrollment/complete")
def enrollment_complete(req: EnrollComplete, user: CurrentUser = Depends(get_current_user)):
    with get_session() as db:
        try:
            enrollment.complete(db, user.user_id, req.card_account_id, req.challenge_id, req.nonce, req.user_verified)
        except VicError as exc:
            return _error(exc)
        return status_view(db, user.user_id)


@router.post("/enrollment/{action}")
def enrollment_action(action: Literal["suspend", "resume", "revoke"], req: CardRef,
                      user: CurrentUser = Depends(get_current_user)):
    with get_session() as db:
        try:
            enrollment.set_status(db, user.user_id, req.card_account_id, action)
        except VicError as exc:
            return _error(exc)
        return status_view(db, user.user_id)


@router.post("/instructions")
def create_instruction(req: NewInstruction, user: CurrentUser = Depends(get_current_user)):
    with get_session() as db:
        try:
            instr, challenge = instructions.create(db, user.user_id, req.checkout_id, req.consent_mode)
        except VicError as exc:
            return _error(exc)
        return {"instruction": instructions.view(instr), "passkey": challenge, "disclaimer": DISCLAIMER}


@router.post("/instructions/{instruction_id}/authenticate")
def authenticate_instruction(instruction_id: str, req: PasskeyAssertion, user: CurrentUser = Depends(get_current_user)):
    with get_session() as db:
        try:
            instr = instructions.authenticate(db, user.user_id, instruction_id, req.challenge_id, req.nonce,
                                              req.user_verified)
        except VicError as exc:
            return _error(exc)
        return {"instruction": instructions.view(instr)}


@router.post("/instructions/{instruction_id}/cancel")
def cancel_instruction(instruction_id: str, user: CurrentUser = Depends(get_current_user)):
    with get_session() as db:
        try:
            instr = instructions.cancel(db, user.user_id, instruction_id)
        except VicError as exc:
            return _error(exc)
        return {"instruction": instructions.view(instr)}


@router.get("/instructions/{instruction_id}")
def get_instruction(instruction_id: str, user: CurrentUser = Depends(get_current_user)):
    with get_session() as db:
        try:
            instr = instructions.get(db, user.user_id, instruction_id)
        except VicError as exc:
            return _error(exc)
        events = db.query(VicLifecycleEvent).filter_by(instruction_id=instruction_id).order_by(VicLifecycleEvent.id)
        return {"instruction": instructions.view(instr),
                "lifecycle": [{"state": e.state, "detail": e.detail,
                               "at": e.created_at.isoformat() if e.created_at else None} for e in events],
                "signals": [{"outcome": s.outcome, "reason": s.reason, "order_id": s.order_id,
                             "gateway_ref": short(s.gateway_ref), "amount": s.amount}
                            for s in signals.for_instruction(db, instruction_id)],
                "disclaimer": DISCLAIMER}


@router.post("/demo/scenario")
def set_scenario(req: Scenario, user: CurrentUser = Depends(get_current_user)):
    """Demo control: how the NEXT gateway authorization for this member ends."""
    with get_session() as db:
        db.query(SimScenario).filter_by(user_id=user.user_id).delete()
        if req.outcome != "none":
            db.add(SimScenario(user_id=user.user_id, outcome=req.outcome))
        db.commit()
    return {"next_outcome": None if req.outcome == "none" else req.outcome, "simulated": True}
