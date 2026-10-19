"""
Talkshop API — the assistant panel inside ShopSphere talks to this.

  POST   /api/talkshop/turn                  {session_id, text?, action?, page?}
         → text/event-stream of structured events (plan §9.4), ending with "done"
  GET    /api/talkshop/sessions/{id}         stage + transcript (rebuild the panel after a page change)
  DELETE /api/talkshop/sessions/{id}         start a fresh conversation

A turn carries EITHER typed text OR a button action:
  greet · select · ask_about · choose_size · choose_color · checkout · keep_shopping
  update_checkout · cancel_checkout · go_ahead      (go_ahead is the only way to pay)
POST + streamed response (read with fetch), since actions carry a JSON body.
"""
import json
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.auth.dependencies import CurrentUser, get_current_user
from backend.talkshop import orchestrator, state

# Open to every session: visitors search, get recommendations and fill a cart.
# Checkout needs a signed-in customer and paying a credit card (any issuer);
# both are enforced in the orchestrator and again by the shop services (CH-2).
router = APIRouter(tags=["talkshop"])


class TurnRequest(BaseModel):
    session_id: str = Field(..., min_length=4, max_length=80)
    text: Optional[str] = Field(None, max_length=1000)
    action: Optional[dict] = None
    image_base64: Optional[str] = Field(None, max_length=3_000_000)   # a pasted photo (data URL or base64)
    page: Optional[dict] = None   # e.g. {"type": "product", "product_id": "SSP001"} or {"type": "category", "department": "shoes"}


@router.post("/api/talkshop/turn")
async def talkshop_turn(req: TurnRequest, user: CurrentUser = Depends(get_current_user)):
    async def stream():
        async for ev in orchestrator.run_turn(user, req.session_id, text=req.text, action=req.action, page=req.page,
                                                  image_base64=req.image_base64):
            yield f"event: {ev['type']}\ndata: {json.dumps(ev, default=str)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/talkshop/sessions/{session_id}")
def get_session(session_id: str, user: CurrentUser = Depends(get_current_user)):
    s = state.peek(user.user_id, session_id)
    if not s:
        return {"session_id": session_id, "stage": state.Stage.GREETING.value, "step": "Search", "transcript": []}
    return {"session_id": session_id, "stage": s.stage.value, "step": state.STEPPER[s.stage],
            "transcript": s.transcript, "checkout_id": s.checkout_id}


@router.delete("/api/talkshop/sessions/{session_id}")
def reset_session(session_id: str, user: CurrentUser = Depends(get_current_user)):
    state.drop(user.user_id, session_id)
    return {"ok": True}
