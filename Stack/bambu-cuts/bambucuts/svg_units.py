#!/usr/bin/env python3
"""
SVG unit handling.

The svg-to-gcode library ignores the viewBox entirely ("Todo deal with viewBoxes"),
so path coordinates come out in SVG *user units*, not millimetres. This module
computes the correct user-unit -> millimetre scale from the root <svg> element so
that the generated G-code has real world dimensions and the machine cuts exactly
on the SVG contour.

    mm_per_user_unit = physical_width_in_mm / viewBox_width

Example: Inkscape document 100x100 mm with viewBox="0 0 377.9528 377.9528"
         -> 100 / 377.9528 = 0.264583 mm per unit (i.e. 96 dpi pixels).
"""

import re
import xml.etree.ElementTree as ET
from typing import Optional, Tuple

# CSS absolute length units, in millimetres.
# A user unit with no suffix is a CSS px (1/96 inch) per the SVG 1.1/2 spec.
UNIT_TO_MM = {
    '': 25.4 / 96.0,
    'px': 25.4 / 96.0,
    'mm': 1.0,
    'cm': 10.0,
    'q': 0.25,
    'in': 25.4,
    'pt': 25.4 / 72.0,
    'pc': 25.4 / 6.0,
}

_LENGTH_RE = re.compile(
    r'^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*'
    r'(px|mm|cm|q|in|pt|pc|%|em|ex)?\s*$',
    re.IGNORECASE,
)


def parse_length_mm(value: Optional[str]) -> Optional[float]:
    """Parse an SVG length attribute into millimetres. Returns None if unusable."""
    if not value:
        return None

    match = _LENGTH_RE.match(value)
    if not match:
        return None

    number = float(match.group(1))
    unit = (match.group(2) or '').lower()

    # Relative units cannot be resolved without a viewport / font context.
    if unit in ('%', 'em', 'ex'):
        return None

    factor = UNIT_TO_MM.get(unit)
    if factor is None:
        return None

    return number * factor


def parse_viewbox(value: Optional[str]) -> Optional[Tuple[float, float, float, float]]:
    """Parse a viewBox attribute into (min_x, min_y, width, height)."""
    if not value:
        return None

    parts = [p for p in re.split(r'[\s,]+', value.strip()) if p]
    if len(parts) < 4:
        return None

    try:
        min_x, min_y, width, height = (float(p) for p in parts[:4])
    except ValueError:
        return None

    if width <= 0 or height <= 0:
        return None

    return min_x, min_y, width, height


def svg_mm_per_unit(svg_path: str, verbose: bool = True) -> float:
    """
    Return how many millimetres one SVG user unit is worth.

    Falls back to 1.0 (user unit == mm) whenever the document does not carry
    enough information, which keeps hand written "viewBox in mm" files working.
    """
    try:
        root = ET.parse(svg_path).getroot()
    except Exception as exc:  # pragma: no cover - malformed file
        if verbose:
            print(f"Warning: could not read SVG units ({exc}), assuming 1 unit = 1 mm")
        return 1.0

    viewbox = parse_viewbox(root.get('viewBox'))
    width_mm = parse_length_mm(root.get('width'))
    height_mm = parse_length_mm(root.get('height'))

    if viewbox is None:
        # No viewBox: user units are whatever width/height say, i.e. CSS px.
        if width_mm is not None or height_mm is not None:
            if verbose:
                print("SVG has no viewBox, treating user units as CSS px (96 dpi)")
            return UNIT_TO_MM['px']
        if verbose:
            print("SVG has no viewBox and no size, assuming 1 unit = 1 mm")
        return 1.0

    _, _, vb_width, vb_height = viewbox

    scale_x = width_mm / vb_width if width_mm else None
    scale_y = height_mm / vb_height if height_mm else None

    if scale_x is None and scale_y is None:
        if verbose:
            print("SVG has no usable width/height, assuming 1 unit = 1 mm")
        return 1.0

    if scale_x is None:
        scale = scale_y
    elif scale_y is None:
        scale = scale_x
    else:
        if abs(scale_x - scale_y) > 1e-4 * max(scale_x, scale_y):
            if verbose:
                print(
                    f"Warning: non-uniform SVG scale (x={scale_x:.6f} y={scale_y:.6f} "
                    f"mm/unit), using the X scale"
                )
        scale = scale_x

    if verbose and abs(scale - 1.0) > 1e-9:
        print(f"SVG scale: 1 user unit = {scale:.6f} mm")

    return scale
