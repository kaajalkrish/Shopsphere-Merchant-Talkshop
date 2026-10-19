"""
PaymentGatewayAdapter — how ShopSphere's payment service talks to its gateway.

  MOCK_GATEWAY_URL set   → real HTTP to the separate mock gateway service
  not set                → the same gateway app, in-process, through the same
                           httpx client (ASGI transport) — tests and one-process runs

Either way the request is a proper HTTP request with an Idempotency-Key and
the merchant's gateway key; card data never appears in it (only an opaque
VIC credential reference or the PSP card token).
"""
import os
from dataclasses import dataclass, field
from typing import Optional, Protocol

import httpx

TIMEOUT_SECONDS = float(os.getenv("MOCK_GATEWAY_TIMEOUT", "5"))


@dataclass
class GatewayResult:
    status: str                       # approved | declined | timeout | error
    gateway_ref: Optional[str] = None
    auth_code: Optional[str] = None
    decline_reason: Optional[str] = None
    duplicate: bool = False
    steps: list[dict] = field(default_factory=list)
    transport: str = ""


class PaymentGatewayAdapter(Protocol):
    async def authorize(self, request: dict, idempotency_key: str, scenario: Optional[str] = None) -> GatewayResult: ...
    async def void(self, gateway_ref: str) -> bool: ...


class MockGatewayAdapter:
    def __init__(self, base_url: Optional[str] = None, key: Optional[str] = None):
        self.base_url = base_url if base_url is not None else os.getenv("MOCK_GATEWAY_URL", "").strip()
        self.key = key or os.getenv("MOCK_GATEWAY_KEY", "shopsphere-demo-merchant-key")
        self.transport_label = (f"HTTP → {self.base_url} (separate local service)" if self.base_url
                                else "HTTP (in-process ASGI transport)")

    def _client(self) -> httpx.AsyncClient:
        if self.base_url:
            return httpx.AsyncClient(base_url=self.base_url, timeout=TIMEOUT_SECONDS)
        from backend.gateway.app import app
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mock-gateway.local",
                                 timeout=TIMEOUT_SECONDS)

    async def authorize(self, request: dict, idempotency_key: str, scenario: Optional[str] = None) -> GatewayResult:
        headers = {"Idempotency-Key": idempotency_key, "X-Gateway-Key": self.key}
        if scenario in ("decline", "timeout"):
            headers["X-Sim-Scenario"] = scenario
        try:
            async with self._client() as client:
                resp = await client.post("/v1/authorizations", json=request, headers=headers)
        except httpx.TimeoutException:
            return GatewayResult(status="timeout", decline_reason="PAYMENT_TIMEOUT", transport=self.transport_label,
                                 steps=[{"actor": "Mock gateway / PSP", "result": "timeout", "simulated": True,
                                         "detail": f"No response within {TIMEOUT_SECONDS:.0f}s"}])
        except httpx.HTTPError as exc:
            return GatewayResult(status="error", decline_reason="GATEWAY_UNAVAILABLE", transport=self.transport_label,
                                 steps=[{"actor": "Mock gateway / PSP", "result": "blocked", "simulated": True,
                                         "detail": f"Gateway unreachable ({type(exc).__name__})"}])
        if resp.status_code != 200:
            return GatewayResult(status="error", decline_reason="GATEWAY_ERROR", transport=self.transport_label,
                                 steps=[{"actor": "Mock gateway / PSP", "result": "blocked", "simulated": True,
                                         "detail": f"HTTP {resp.status_code}"}])
        body = resp.json()
        return GatewayResult(status=body["status"], gateway_ref=body.get("gateway_ref"), auth_code=body.get("auth_code"),
                             decline_reason=body.get("decline_reason"), duplicate=bool(body.get("duplicate")),
                             steps=body.get("steps") or [], transport=self.transport_label)

    # ── CH-6: Simulated Secure Payment Provider (server-to-server; the merchant key) ──
    def hosted_url(self, session_id: str) -> str:
        """Where the customer's browser goes: the provider's own page. Separate service →
        its own origin; one-process demo → mounted under /psp on the backend."""
        base = (os.getenv("PROVIDER_PUBLIC_URL") or self.base_url
                or os.getenv("PUBLIC_BACKEND_URL", "http://localhost:8000").rstrip("/") + "/psp")
        return f"{base.rstrip('/')}/hosted/{session_id}"

    async def _provider(self, method: str, path: str, body: Optional[dict] = None) -> tuple[int, dict]:
        try:
            async with self._client() as client:
                resp = await client.request(method, path, json=body, headers={"X-Gateway-Key": self.key})
        except httpx.TimeoutException:
            return 504, {"code": "PROVIDER_TIMEOUT"}
        except httpx.HTTPError as exc:
            return 502, {"code": "PROVIDER_UNAVAILABLE", "message": type(exc).__name__}
        data = resp.json() if resp.content else {}
        return resp.status_code, (data.get("detail") if resp.status_code >= 400 and isinstance(data.get("detail"), dict)
                                  else data)

    async def create_payment_session(self, body: dict) -> tuple[int, dict]:
        return await self._provider("POST", "/v1/payment_sessions", body)

    async def payment_session(self, session_id: str) -> tuple[int, dict]:
        return await self._provider("GET", f"/v1/payment_sessions/{session_id}")

    async def authorize_payment_session(self, session_id: str, body: dict) -> tuple[int, dict]:
        """PayIt's authorization of a tokenized + verified provider credential (idempotent on payment_ref)."""
        return await self._provider("POST", f"/v1/payment_sessions/{session_id}/authorize", body)

    async def void_payment_session(self, session_id: str) -> tuple[int, dict]:
        return await self._provider("POST", f"/v1/payment_sessions/{session_id}/void")

    async def void(self, gateway_ref: str) -> bool:
        try:
            async with self._client() as client:
                resp = await client.post(f"/v1/authorizations/{gateway_ref}/void", headers={"X-Gateway-Key": self.key})
            return resp.status_code == 200
        except httpx.HTTPError:
            return False


def get_adapter() -> PaymentGatewayAdapter:
    return MockGatewayAdapter()
