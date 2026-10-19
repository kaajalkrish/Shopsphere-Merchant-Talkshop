import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from backend.db.init_db import init_db, run as seed_db
from backend.routers import auth, merchants, products, chat, authorizations, payments, cart, compare, loyalty, shop, talkshop, member, vic
from backend.config.llm import resolve_llm
from backend.observability.tracing import init_tracing

from backend.db.config import DB_PATH


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(DB_PATH)
    seed_db()
    llm_info = resolve_llm()
    print(f"[LLM] Using {llm_info['provider']} / {llm_info['model']}")
    tracing_enabled, endpoint = init_tracing()
    print(f"[Observability] Tracing {'enabled -> ' + endpoint if tracing_enabled else 'disabled'}")
    yield


app = FastAPI(
    title="Agentic Commerce POC",
    description="Multi-agent commerce demo with DPAT payment authorization",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(merchants.router)
app.include_router(products.router)
app.include_router(chat.router)
app.include_router(authorizations.router)
app.include_router(payments.router)
app.include_router(cart.router)
app.include_router(compare.router)
app.include_router(loyalty.router)
app.include_router(shop.router)
app.include_router(talkshop.router)
app.include_router(member.router)
app.include_router(vic.router)

# CH-6: in a one-process demo the mock gateway / Simulated Secure Payment Provider is
# also reachable at /psp, so the customer's browser can open the provider's hosted page
# (http://localhost:8000/psp/hosted/…). Run it as its own service (MOCK_GATEWAY_URL) to
# give the provider its own origin.
from backend.gateway.app import app as _gateway_app  # noqa: E402
app.mount("/psp", _gateway_app)


@app.get("/")
def health():
    return {"status": "ok", "service": "Agentic Commerce POC"}


@app.post("/api/transcribe")
async def transcribe(audio: UploadFile = File(...)):
    """
    Voice input endpoint. Accepts an audio file (webm/wav) and returns
    the transcript using faster-whisper (local, no API key needed).
    Wired to the mic button in the Chat UI.
    Full implementation in Phase 2 — returns placeholder in Phase 1.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="faster-whisper not installed. Run: pip install faster-whisper"
        )

    audio_bytes = await audio.read()
    tmp_path = Path(tempfile.gettempdir()) / audio.filename
    tmp_path.write_bytes(audio_bytes)

    model = WhisperModel("base", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(str(tmp_path), beam_size=5)
    transcript = " ".join(seg.text.strip() for seg in segments)

    tmp_path.unlink(missing_ok=True)
    return {"text": transcript}
