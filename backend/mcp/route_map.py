"""
The delivery route map on the ShopSphere tracking card: a small illustrated map (SVG) with the route
from the fulfilment center to the shopper's city, its stops, and the parcel's position for the
current shipment step. It is drawn here and embedded in the card as a data URI (cards render in a
sandbox that may not load images from other sites).

Shipping in this demo is simulated: the stops are generic (no real addresses or carrier hubs), and
the destination shows only the city and state, never the street address.
"""
import base64
import math
from html import escape
from typing import Optional

W, H = 600, 230
# The route: one cubic Bezier curve across the map.
_P = ((60, 170), (190, 25), (390, 215), (535, 70))
# Stops along the route, as a share of its length, with where each label goes (dx, dy, anchor).
STOPS = (("ShopSphere fulfilment center", 0.0, (-8, 38, "start")),
         ("Carrier hub", 0.45, (0, -16, "middle")),
         ("Local delivery station", 0.78, (0, 27, "middle")))
# Where the parcel is (share of the route) for each shipment step.
_POSITION = {"confirmed": 0.0, "payment_completed": 0.0, "processing": 0.0, "packed": 0.0,
             "shipped": 0.3, "out_for_delivery": 0.88, "delivered": 1.0}
# How much of the route counts as travelled (drawn solid) for each step.
_TRAVELLED = {**_POSITION, "shipped": 0.3, "out_for_delivery": 0.88}


def _bezier(t: float) -> tuple[float, float]:
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = _P
    u = 1 - t
    return (u ** 3 * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t ** 3 * x3,
            u ** 3 * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t ** 3 * y3)


def _arc_table(n: int = 240) -> list[tuple[float, float, float]]:
    """(share of length, x, y) along the curve, so stops sit at true distances along the route."""
    pts = [_bezier(i / n) for i in range(n + 1)]
    dist = [0.0]
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        dist.append(dist[-1] + math.hypot(bx - ax, by - ay))
    return [(d / dist[-1], x, y) for d, (x, y) in zip(dist, pts)]


_TABLE = _arc_table()


def point_at(share: float) -> tuple[float, float]:
    share = max(0.0, min(1.0, share))
    for (s0, x0, y0), (s1, x1, y1) in zip(_TABLE, _TABLE[1:]):
        if s1 >= share:
            f = 0 if s1 == s0 else (share - s0) / (s1 - s0)
            return x0 + (x1 - x0) * f, y0 + (y1 - y0) * f
    return _TABLE[-1][1], _TABLE[-1][2]


def _label(x: float, y: float, text: str, anchor: str = "middle", size: int = 12, weight: int = 600,
           fill: str = "#1f2937") -> str:
    return (f'<text x="{x:.0f}" y="{y:.0f}" text-anchor="{anchor}" font-size="{size}" font-weight="{weight}" '
            f'fill="{fill}" stroke="#ffffff" stroke-width="4" stroke-linejoin="round" paint-order="stroke">'
            f'{escape(text)}</text>')


def _scenery() -> str:
    """Land, water, a park and a grid of streets, so it reads as a map."""
    roads = "".join(f'<line x1="0" y1="{y}" x2="{W}" y2="{y}"/>' for y in (48, 122, 196))
    roads += "".join(f'<line x1="{x}" y1="0" x2="{x}" y2="{H}"/>' for x in (110, 245, 370, 480))
    return (f'<rect width="{W}" height="{H}" rx="14" fill="#f1eee8"/>'
            f'<path d="M0 0 H150 C120 30 70 40 0 70 Z" fill="#cfe3f4"/>'                       # water
            f'<path d="M{W} {H} V150 C560 170 520 200 470 {H} Z" fill="#cfe3f4"/>'
            f'<rect x="262" y="138" width="92" height="46" rx="8" fill="#d6ebcd"/>'               # park
            f'<rect x="388" y="12" width="76" height="30" rx="8" fill="#d6ebcd"/>'
            f'<g stroke="#ffffff" stroke-width="7">{roads}</g>'
            f'<path d="M0 205 L{W} 18" stroke="#f6dc9b" stroke-width="9"/>')                     # highway


def _truck(x: float, y: float) -> str:
    return (f'<g transform="translate({x:.1f} {y:.1f})">'
            f'<circle r="19" fill="#ffffff" stroke="#2563eb" stroke-width="3"/>'
            f'<rect x="-11" y="-7" width="13" height="10" rx="1.5" fill="#2563eb"/>'
            f'<path d="M3 -4 H8 L11 0 V3 H3 Z" fill="#1d4ed8"/>'
            f'<circle cx="-6" cy="5" r="2.6" fill="#111827"/><circle cx="7" cy="5" r="2.6" fill="#111827"/>'
            f'</g>')


def _pin(x: float, y: float, delivered: bool) -> str:
    colour = "#16a34a" if delivered else "#e11d48"
    inner = ('<path d="M-4 -24 L-1 -21 L5 -28" fill="none" stroke="#ffffff" stroke-width="2.4" '
             'stroke-linecap="round" stroke-linejoin="round"/>' if delivered
             else '<circle cx="0" cy="-25" r="4.5" fill="#ffffff"/>')
    return (f'<g transform="translate({x:.1f} {y:.1f})">'
            f'<path d="M0 0 C-4 -9 -13 -15 -13 -25 A13 13 0 0 1 13 -25 C13 -15 4 -9 0 0 Z" fill="{colour}" '
            f'stroke="#ffffff" stroke-width="2"/>{inner}</g>')


def route_map_svg(status: Optional[str], city: Optional[str], state: Optional[str]) -> str:
    status = status if status in _POSITION else "processing"
    delivered = status == "delivered"
    travelled = _TRAVELLED[status]
    curve = "M{} {} C{} {} {} {} {} {}".format(*[c for p in _P for c in p])
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
             f'font-family="Segoe UI, Helvetica, Arial, sans-serif">', _scenery(),
             f'<path d="{curve}" fill="none" stroke="#ffffff" stroke-width="11" stroke-linecap="round"/>',
             f'<path d="{curve}" fill="none" stroke="#94a3b8" stroke-width="4" stroke-dasharray="9 8" '
             f'stroke-linecap="round"/>']
    if travelled > 0:
        parts.append(f'<path d="{curve}" fill="none" stroke="#2563eb" stroke-width="5" stroke-linecap="round" '
                     f'pathLength="100" stroke-dasharray="{travelled * 100:.1f} 200"/>')
    for name, share, (dx, dy, anchor) in STOPS:
        x, y = point_at(share)
        reached = travelled >= share and (share > 0 or status != "confirmed")
        ring = "#2563eb" if reached else "#94a3b8"
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="8" fill="#ffffff" stroke="{ring}" stroke-width="3"/>')
        if reached:
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{ring}"/>')
        parts.append(_label(x + dx, y + dy, name, anchor, size=11, weight=600 if reached else 500,
                            fill="#1f2937" if reached else "#64748b"))
    dx_, dy_ = point_at(1.0)
    parts.append(_pin(dx_, dy_, delivered))
    where = ", ".join(x for x in (city, state) if x) or "Your address"
    parts.append(_label(dx_ - 18, dy_ - 22, where, "end", size=13, weight=700))      # left of the pin, clear of the route
    if not delivered:
        tx, ty = point_at(_POSITION[status])
        parts.append(_truck(tx, ty))
    parts.append(_label(W - 12, H - 10, "Simulated route", "end", size=10, weight=500, fill="#64748b"))
    parts.append("</svg>")
    return "".join(parts)


def route_map(status: Optional[str], city: Optional[str], state: Optional[str]) -> str:
    """The map as a data URI for an Image on the card."""
    svg = route_map_svg(status, city, state)
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")
