"""
diagram_lib.py — minimal builder for paired Excalidraw + SVG diagrams.

Build a Diagram by adding primitives (rect, text, arrow, line). Call
to_excalidraw() and to_svg() to get the two serializations.

Design notes:
  - IDs are deterministic from element index, so re-runs produce stable output.
  - Coordinates are top-left origin, same as both SVG and Excalidraw.
  - Colors use a small palette declared at module top.
  - Text auto-centers vertically/horizontally inside containing rectangles
    when added via rect(label=...).
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from typing import Any, Optional


# --- Palette ----------------------------------------------------------------

STROKE_DARK   = "#1e1e1e"
WHITE         = "#ffffff"
TRANSPARENT   = "transparent"

# Scope-coded fills (chosen to be Excalidraw-palette friendly)
PROCESS_FILL  = "#dbeafe"  # light blue
PROCESS_LINE  = "#2563eb"
REQUEST_FILL  = "#dcfce7"  # light green
REQUEST_LINE  = "#16a34a"
EXTERNAL_FILL = "#fed7aa"  # light orange
EXTERNAL_LINE = "#ea580c"
WARN_FILL     = "#fecaca"  # light red, for anti-patterns / traps
WARN_LINE     = "#dc2626"
ACCENT_FILL   = "#fef3c7"  # light yellow, for highlights
ACCENT_LINE   = "#ca8a04"

# Excalidraw font families:
#   1 = Virgil (hand-drawn), 2 = Helvetica, 3 = Cascadia (mono), 5 = Excalifont
FONT_SANS = 2
FONT_MONO = 3


# --- Element classes --------------------------------------------------------

@dataclass
class _Element:
    """Base class for diagram primitives. Subclasses serialize to both formats."""
    x: float
    y: float
    id: str = ""
    locked: bool = False


@dataclass
class Rect(_Element):
    w: float = 100
    h: float = 60
    stroke: str = STROKE_DARK
    fill: str = TRANSPARENT
    stroke_width: int = 2
    rounded: bool = True
    dashed: bool = False
    label: Optional[str] = None
    label_font_size: int = 16
    label_font: int = FONT_SANS
    bound_text_id: Optional[str] = None  # filled by Diagram.rect() if labeled


@dataclass
class Ellipse(_Element):
    w: float = 100
    h: float = 60
    stroke: str = STROKE_DARK
    fill: str = TRANSPARENT
    stroke_width: int = 2
    label: Optional[str] = None
    label_font_size: int = 16
    label_font: int = FONT_SANS
    bound_text_id: Optional[str] = None


@dataclass
class Diamond(_Element):
    w: float = 100
    h: float = 60
    stroke: str = STROKE_DARK
    fill: str = TRANSPARENT
    stroke_width: int = 2
    label: Optional[str] = None
    label_font_size: int = 16
    label_font: int = FONT_SANS
    bound_text_id: Optional[str] = None


@dataclass
class Text(_Element):
    text: str = ""
    font_size: int = 16
    font: int = FONT_SANS
    align: str = "left"          # "left" | "center" | "right"
    valign: str = "top"          # "top" | "middle" | "bottom"
    color: str = STROKE_DARK
    width: Optional[float] = None
    height: Optional[float] = None
    container_id: Optional[str] = None  # if set, this text is bound to a shape


@dataclass
class Arrow(_Element):
    x2: float = 0
    y2: float = 0
    stroke: str = STROKE_DARK
    stroke_width: int = 2
    dashed: bool = False
    start_arrowhead: Optional[str] = None    # None | "arrow"
    end_arrowhead: Optional[str] = "arrow"
    label: Optional[str] = None


@dataclass
class Line(_Element):
    x2: float = 0
    y2: float = 0
    stroke: str = STROKE_DARK
    stroke_width: int = 2
    dashed: bool = False


# --- Diagram ---------------------------------------------------------------

class Diagram:
    """A collection of elements with serializers for SVG and Excalidraw."""

    def __init__(self, width: int = 900, height: int = 600,
                 background: str = WHITE, title: str = ""):
        self.width = width
        self.height = height
        self.background = background
        self.title = title
        self.elements: list[_Element] = []
        self._counter = 0
        self._rng = random.Random(0xC0FFEE)  # deterministic

    # --- IDs ---------------------------------------------------------------

    def _next_id(self, prefix: str = "el") -> str:
        self._counter += 1
        return f"{prefix}{self._counter:03d}"

    # --- Adders ------------------------------------------------------------

    def rect(self, x, y, w, h, *, label=None, fill=TRANSPARENT,
             stroke=STROKE_DARK, stroke_width=2, rounded=True, dashed=False,
             label_font_size=16, label_font=FONT_SANS) -> Rect:
        rid = self._next_id("rect")
        r = Rect(x=x, y=y, w=w, h=h, id=rid, fill=fill, stroke=stroke,
                 stroke_width=stroke_width, rounded=rounded, dashed=dashed,
                 label=label, label_font_size=label_font_size,
                 label_font=label_font)
        if label:
            r.bound_text_id = self._next_id("text")
        self.elements.append(r)
        return r

    def ellipse(self, x, y, w, h, *, label=None, fill=TRANSPARENT,
                stroke=STROKE_DARK, stroke_width=2,
                label_font_size=16, label_font=FONT_SANS) -> Ellipse:
        eid = self._next_id("ell")
        e = Ellipse(x=x, y=y, w=w, h=h, id=eid, fill=fill, stroke=stroke,
                    stroke_width=stroke_width, label=label,
                    label_font_size=label_font_size, label_font=label_font)
        if label:
            e.bound_text_id = self._next_id("text")
        self.elements.append(e)
        return e

    def diamond(self, x, y, w, h, *, label=None, fill=TRANSPARENT,
                stroke=STROKE_DARK, stroke_width=2,
                label_font_size=16, label_font=FONT_SANS) -> Diamond:
        did = self._next_id("dia")
        d = Diamond(x=x, y=y, w=w, h=h, id=did, fill=fill, stroke=stroke,
                    stroke_width=stroke_width, label=label,
                    label_font_size=label_font_size, label_font=label_font)
        if label:
            d.bound_text_id = self._next_id("text")
        self.elements.append(d)
        return d

    def text(self, x, y, text, *, font_size=16, font=FONT_SANS,
             align="left", valign="top", color=STROKE_DARK,
             width=None, height=None) -> Text:
        tid = self._next_id("text")
        t = Text(x=x, y=y, text=text, id=tid, font_size=font_size,
                 font=font, align=align, valign=valign, color=color,
                 width=width, height=height)
        self.elements.append(t)
        return t

    def arrow(self, x, y, x2, y2, *, stroke=STROKE_DARK, stroke_width=2,
              dashed=False, start=None, end="arrow") -> Arrow:
        aid = self._next_id("arr")
        a = Arrow(x=x, y=y, x2=x2, y2=y2, id=aid, stroke=stroke,
                  stroke_width=stroke_width, dashed=dashed,
                  start_arrowhead=start, end_arrowhead=end)
        self.elements.append(a)
        return a

    def line(self, x, y, x2, y2, *, stroke=STROKE_DARK, stroke_width=2,
             dashed=False) -> Line:
        lid = self._next_id("line")
        ln = Line(x=x, y=y, x2=x2, y2=y2, id=lid, stroke=stroke,
                  stroke_width=stroke_width, dashed=dashed)
        self.elements.append(ln)
        return ln

    # --- Excalidraw serialization -----------------------------------------

    def _excalidraw_common(self, el: _Element) -> dict[str, Any]:
        """Fields common to all Excalidraw elements."""
        return {
            "id": el.id,
            "angle": 0,
            "strokeColor": getattr(el, "stroke", STROKE_DARK),
            "backgroundColor": getattr(el, "fill", TRANSPARENT),
            "fillStyle": "solid",
            "strokeWidth": getattr(el, "stroke_width", 2),
            "strokeStyle": "dashed" if getattr(el, "dashed", False) else "solid",
            "roughness": 1,
            "opacity": 100,
            "groupIds": [],
            "frameId": None,
            "roundness": {"type": 3} if getattr(el, "rounded", False) else None,
            "seed": self._rng.randint(1, 2_000_000_000),
            "version": 1,
            "versionNonce": self._rng.randint(1, 2_000_000_000),
            "isDeleted": False,
            "boundElements": None,
            "updated": 1700000000000,
            "link": None,
            "locked": el.locked,
        }

    def _shape_to_excalidraw(self, el, shape_type: str) -> list[dict]:
        out = []
        base = self._excalidraw_common(el)
        base.update({
            "type": shape_type,
            "x": el.x,
            "y": el.y,
            "width": el.w,
            "height": el.h,
        })

        # Bind label text if present.
        if el.label and el.bound_text_id:
            base["boundElements"] = [
                {"type": "text", "id": el.bound_text_id}
            ]
            text_el = {
                "id": el.bound_text_id,
                "type": "text",
                "x": el.x,
                "y": el.y,
                "width": el.w,
                "height": el.h,
                "angle": 0,
                "strokeColor": STROKE_DARK,
                "backgroundColor": TRANSPARENT,
                "fillStyle": "solid",
                "strokeWidth": 1,
                "strokeStyle": "solid",
                "roughness": 1,
                "opacity": 100,
                "groupIds": [],
                "frameId": None,
                "roundness": None,
                "seed": self._rng.randint(1, 2_000_000_000),
                "version": 1,
                "versionNonce": self._rng.randint(1, 2_000_000_000),
                "isDeleted": False,
                "boundElements": None,
                "updated": 1700000000000,
                "link": None,
                "locked": False,
                "fontSize": el.label_font_size,
                "fontFamily": el.label_font,
                "text": el.label,
                "textAlign": "center",
                "verticalAlign": "middle",
                "containerId": el.id,
                "originalText": el.label,
                "lineHeight": 1.25,
                "baseline": int(el.label_font_size * 0.9),
            }
            out.append(base)
            out.append(text_el)
        else:
            out.append(base)
        return out

    def _text_to_excalidraw(self, t: Text) -> dict:
        font_size = t.font_size
        # Approximate text dimensions; Excalidraw recomputes on load.
        approx_w = t.width if t.width is not None else max(50, len(t.text) * font_size * 0.55)
        approx_h = t.height if t.height is not None else font_size * 1.25
        base = self._excalidraw_common(t)
        base["strokeColor"] = t.color
        base["backgroundColor"] = TRANSPARENT
        base["roundness"] = None
        base.update({
            "type": "text",
            "x": t.x,
            "y": t.y,
            "width": approx_w,
            "height": approx_h,
            "fontSize": font_size,
            "fontFamily": t.font,
            "text": t.text,
            "textAlign": t.align,
            "verticalAlign": t.valign,
            "containerId": t.container_id,
            "originalText": t.text,
            "lineHeight": 1.25,
            "baseline": int(font_size * 0.9),
        })
        return base

    def _arrow_to_excalidraw(self, a: Arrow) -> dict:
        base = self._excalidraw_common(a)
        base["roundness"] = {"type": 2}
        dx = a.x2 - a.x
        dy = a.y2 - a.y
        base.update({
            "type": "arrow",
            "x": a.x,
            "y": a.y,
            "width": abs(dx),
            "height": abs(dy),
            "points": [[0, 0], [dx, dy]],
            "lastCommittedPoint": None,
            "startBinding": None,
            "endBinding": None,
            "startArrowhead": a.start_arrowhead,
            "endArrowhead": a.end_arrowhead,
        })
        return base

    def _line_to_excalidraw(self, ln: Line) -> dict:
        base = self._excalidraw_common(ln)
        base["roundness"] = {"type": 2}
        dx = ln.x2 - ln.x
        dy = ln.y2 - ln.y
        base.update({
            "type": "line",
            "x": ln.x,
            "y": ln.y,
            "width": abs(dx),
            "height": abs(dy),
            "points": [[0, 0], [dx, dy]],
            "lastCommittedPoint": None,
            "startBinding": None,
            "endBinding": None,
            "startArrowhead": None,
            "endArrowhead": None,
        })
        return base

    def to_excalidraw(self) -> dict:
        out_elements = []
        for el in self.elements:
            if isinstance(el, Rect):
                out_elements.extend(self._shape_to_excalidraw(el, "rectangle"))
            elif isinstance(el, Ellipse):
                out_elements.extend(self._shape_to_excalidraw(el, "ellipse"))
            elif isinstance(el, Diamond):
                out_elements.extend(self._shape_to_excalidraw(el, "diamond"))
            elif isinstance(el, Text):
                out_elements.append(self._text_to_excalidraw(el))
            elif isinstance(el, Arrow):
                out_elements.append(self._arrow_to_excalidraw(el))
            elif isinstance(el, Line):
                out_elements.append(self._line_to_excalidraw(el))

        return {
            "type": "excalidraw",
            "version": 2,
            "source": "diagram_lib",
            "elements": out_elements,
            "appState": {
                "gridSize": None,
                "viewBackgroundColor": self.background,
            },
            "files": {},
        }

    # --- SVG serialization -------------------------------------------------

    def to_svg(self) -> str:
        parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {self.width} {self.height}" '
            f'width="{self.width}" height="{self.height}" '
            f'font-family="Helvetica, Arial, sans-serif">',
            f'<rect width="100%" height="100%" fill="{self.background}"/>',
        ]

        # Arrowhead marker definition.
        parts.append(
            '<defs>'
            '<marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" '
            'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
            f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{STROKE_DARK}"/>'
            '</marker>'
            '</defs>'
        )

        if self.title:
            parts.append(
                f'<text x="{self.width // 2}" y="32" font-size="22" '
                f'font-weight="600" text-anchor="middle" fill="{STROKE_DARK}">'
                f'{_svg_escape(self.title)}</text>'
            )

        for el in self.elements:
            if isinstance(el, Rect):
                parts.append(self._rect_svg(el))
            elif isinstance(el, Ellipse):
                parts.append(self._ellipse_svg(el))
            elif isinstance(el, Diamond):
                parts.append(self._diamond_svg(el))
            elif isinstance(el, Text):
                parts.append(self._text_svg(el))
            elif isinstance(el, Arrow):
                parts.append(self._arrow_svg(el))
            elif isinstance(el, Line):
                parts.append(self._line_svg(el))

        parts.append('</svg>')
        return '\n'.join(parts)

    def _rect_svg(self, r: Rect) -> str:
        rx = 8 if r.rounded else 0
        dash = ' stroke-dasharray="6 4"' if r.dashed else ''
        s = (f'<rect x="{r.x}" y="{r.y}" width="{r.w}" height="{r.h}" '
             f'rx="{rx}" ry="{rx}" fill="{r.fill}" stroke="{r.stroke}" '
             f'stroke-width="{r.stroke_width}"{dash}/>')
        if r.label:
            cx = r.x + r.w / 2
            cy = r.y + r.h / 2
            family = "Cascadia Code, Consolas, monospace" if r.label_font == FONT_MONO else "Helvetica, Arial, sans-serif"
            s += (f'<text x="{cx}" y="{cy}" font-size="{r.label_font_size}" '
                  f'font-family="{family}" '
                  f'text-anchor="middle" dominant-baseline="middle" '
                  f'fill="{STROKE_DARK}">{_svg_escape_multiline(r.label, cx, cy, r.label_font_size)}</text>')
        return s

    def _ellipse_svg(self, e: Ellipse) -> str:
        cx = e.x + e.w / 2
        cy = e.y + e.h / 2
        s = (f'<ellipse cx="{cx}" cy="{cy}" rx="{e.w/2}" ry="{e.h/2}" '
             f'fill="{e.fill}" stroke="{e.stroke}" '
             f'stroke-width="{e.stroke_width}"/>')
        if e.label:
            family = "Cascadia Code, Consolas, monospace" if e.label_font == FONT_MONO else "Helvetica, Arial, sans-serif"
            s += (f'<text x="{cx}" y="{cy}" font-size="{e.label_font_size}" '
                  f'font-family="{family}" '
                  f'text-anchor="middle" dominant-baseline="middle" '
                  f'fill="{STROKE_DARK}">{_svg_escape_multiline(e.label, cx, cy, e.label_font_size)}</text>')
        return s

    def _diamond_svg(self, d: Diamond) -> str:
        cx = d.x + d.w / 2
        cy = d.y + d.h / 2
        pts = f"{cx},{d.y} {d.x + d.w},{cy} {cx},{d.y + d.h} {d.x},{cy}"
        s = (f'<polygon points="{pts}" fill="{d.fill}" '
             f'stroke="{d.stroke}" stroke-width="{d.stroke_width}"/>')
        if d.label:
            family = "Cascadia Code, Consolas, monospace" if d.label_font == FONT_MONO else "Helvetica, Arial, sans-serif"
            s += (f'<text x="{cx}" y="{cy}" font-size="{d.label_font_size}" '
                  f'font-family="{family}" '
                  f'text-anchor="middle" dominant-baseline="middle" '
                  f'fill="{STROKE_DARK}">{_svg_escape_multiline(d.label, cx, cy, d.label_font_size)}</text>')
        return s

    def _text_svg(self, t: Text) -> str:
        # Map alignment to SVG attributes.
        anchor = {"left": "start", "center": "middle", "right": "end"}[t.align]
        baseline = {"top": "hanging", "middle": "middle", "bottom": "alphabetic"}[t.valign]
        family = "Cascadia Code, Consolas, monospace" if t.font == FONT_MONO else "Helvetica, Arial, sans-serif"

        # Compute anchor x based on alignment.
        if t.align == "center":
            anchor_x = t.x + (t.width or 0) / 2 if t.width else t.x
        elif t.align == "right":
            anchor_x = t.x + (t.width or 0) if t.width else t.x
        else:
            anchor_x = t.x

        lines = t.text.split('\n')
        if len(lines) == 1:
            return (f'<text x="{anchor_x}" y="{t.y}" font-size="{t.font_size}" '
                    f'font-family="{family}" text-anchor="{anchor}" '
                    f'dominant-baseline="{baseline}" fill="{t.color}">'
                    f'{_svg_escape(t.text)}</text>')
        # Multiline.
        out = (f'<text x="{anchor_x}" y="{t.y}" font-size="{t.font_size}" '
               f'font-family="{family}" text-anchor="{anchor}" '
               f'dominant-baseline="{baseline}" fill="{t.color}">')
        for i, line in enumerate(lines):
            dy = "0" if i == 0 else f"{int(t.font_size * 1.2)}"
            out += f'<tspan x="{anchor_x}" dy="{dy}">{_svg_escape(line)}</tspan>'
        out += '</text>'
        return out

    def _arrow_svg(self, a: Arrow) -> str:
        dash = ' stroke-dasharray="6 4"' if a.dashed else ''
        marker_end = ' marker-end="url(#arr)"' if a.end_arrowhead else ''
        marker_start = ' marker-start="url(#arr)"' if a.start_arrowhead else ''
        return (f'<line x1="{a.x}" y1="{a.y}" x2="{a.x2}" y2="{a.y2}" '
                f'stroke="{a.stroke}" stroke-width="{a.stroke_width}"'
                f'{dash}{marker_start}{marker_end}/>')

    def _line_svg(self, ln: Line) -> str:
        dash = ' stroke-dasharray="6 4"' if ln.dashed else ''
        return (f'<line x1="{ln.x}" y1="{ln.y}" x2="{ln.x2}" y2="{ln.y2}" '
                f'stroke="{ln.stroke}" stroke-width="{ln.stroke_width}"{dash}/>')

    # --- Writers -----------------------------------------------------------

    def write(self, base_path: str) -> tuple[str, str]:
        """Write both .excalidraw and .svg to {base_path}.{ext}. Returns (excalidraw_path, svg_path)."""
        excalidraw_path = f"{base_path}.excalidraw"
        svg_path = f"{base_path}.svg"
        with open(excalidraw_path, "w") as f:
            json.dump(self.to_excalidraw(), f, indent=2)
        with open(svg_path, "w") as f:
            f.write(self.to_svg())
        return excalidraw_path, svg_path


# --- Helpers ----------------------------------------------------------------

def _svg_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def _svg_escape_multiline(s: str, cx: float, cy: float, font_size: int) -> str:
    """Render multi-line labels as nested tspans centered around (cx, cy)."""
    lines = s.split('\n')
    if len(lines) == 1:
        return _svg_escape(s)
    line_height = int(font_size * 1.2)
    total_h = line_height * (len(lines) - 1)
    start_dy = -total_h / 2
    out = []
    for i, line in enumerate(lines):
        if i == 0:
            out.append(f'<tspan x="{cx}" dy="{start_dy}">{_svg_escape(line)}</tspan>')
        else:
            out.append(f'<tspan x="{cx}" dy="{line_height}">{_svg_escape(line)}</tspan>')
    return ''.join(out)
