"""Inkscape-style bitmap -> SVG tracing for a single-pen plotter.

What Inkscape's "Trace Bitmap" does
-----------------------------------
Inkscape is a thin UI around Peter Selinger's Potrace. For a one-colour plot
the two useful single-scan filters are:

* **Brightness cutoff** - pixels darker than a threshold become black. This
  is the right choice for logos, clean line art and dark shapes.
* **Edge detection** - luminance edges become black lines on a white canvas.
  This is the right choice for photos and smooth gradients.

Potrace then converts the black bitmap into Bezier curves with these Inkscape
defaults: ``turdsize=2``, ``turnpolicy=minority``, ``alphamax=1.0``,
``opticurve=True``, ``opttolerance=0.2``. The pure-Python ``potracer``
package implements the same algorithm, so an Inkscape installation is not
required.

What is different for the plotter
---------------------------------
* Every Potrace curve is written as its own ``<path>``. The bundled
  ``svg2gcode_grbl`` converter treats each SVG shape as one continuous
  polyline; putting several sub-paths into one ``<path>`` makes the plotter
  draw a connecting line between contours. Separate paths produce a pen-up
  move between contours instead.
* Paths use ``fill="none" stroke="#000000"`` so the browser preview shows the
  actual pen strokes. The G-code converter follows path geometry and ignores
  fill/stroke, so this only affects the preview.
* The bitmap is downscaled before tracing. A 1200 px side keeps the G-code
  short enough for FluidNC while preserving the shapes a pen can draw.

The SVG is deliberately simple (``M``/``L``/``C``/``Z`` only) because
``svg2gcode_grbl`` uses an old Inkscape path parser that does not support
every modern SVG feature.
"""

from __future__ import annotations

from io import BytesIO

import cv2
import numpy as np
from PIL import Image, ImageOps
from potrace import POTRACE_TURNPOLICY_MINORITY, Bitmap

# Inkscape Trace Bitmap defaults.
DEFAULT_TURDSIZE = 2
DEFAULT_ALPHAMAX = 1.0
DEFAULT_OPT_TOLERANCE = 0.2
BRIGHTNESS_THRESHOLD = 0.45
EDGE_THRESHOLD = 0.35

VALID_MODES = {"auto", "brightness", "edge"}


def _flatten_to_grayscale(data: bytes, max_side: int) -> tuple[np.ndarray, int, int, bool]:
    """Decode, orient, flatten transparency, resize and describe the image.

    Returns ``(grayscale, width, height, colorful)``. ``colorful`` is used by
    the automatic mode choice: saturated pixels usually mean a photo or
    coloured artwork, where edge detection gives better plotter lines than a
    brightness threshold.
    """
    with Image.open(BytesIO(data)) as source:
        image = ImageOps.exif_transpose(source)
        if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
            rgba = image.convert("RGBA")
            background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            image = Image.alpha_composite(background, rgba).convert("RGB")
        else:
            image = image.convert("RGB")
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        width, height = image.size
        if width < 3 or height < 3:
            raise ValueError("image too small")

        saturation = np.asarray(image.convert("HSV"), dtype=np.uint8)[:, :, 1]
        colorful = float(np.count_nonzero(saturation > 60)) / saturation.size > 0.05
        gray = np.asarray(image.convert("L"), dtype=np.uint8)
    return gray, width, height, colorful


def _brightness_cutoff(gray: np.ndarray, threshold: float = BRIGHTNESS_THRESHOLD) -> np.ndarray:
    """Inkscape Brightness Cutoff: pixels darker than threshold become black."""
    cutoff = int(round(max(0.0, min(1.0, threshold)) * 255))
    return np.where(gray < cutoff, 0, 255).astype(np.uint8)


def _edge_detection(gray: np.ndarray, threshold: float = EDGE_THRESHOLD) -> np.ndarray:
    """Inkscape Edge Detection: Canny edges as black lines on white."""
    # Inkscape scales the Canny high threshold from the 0-1 UI slider.
    high = int(round(40 + max(0.0, min(1.0, threshold)) * 160))
    low = max(10, high // 2)
    normalized = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
    smoothed = cv2.bilateralFilter(normalized, 7, 50, 50)
    edges = cv2.Canny(smoothed, low, high, L2gradient=True)
    edges[[0, -1], :] = 0
    edges[:, [0, -1]] = 0
    # White background, black strokes - the polarity Potrace expects.
    return np.where(edges > 0, 0, 255).astype(np.uint8)


def _choose_auto_mode(gray: np.ndarray, colorful: bool) -> tuple[str, bool]:
    """Pick an Inkscape-style filter and whether to invert the image.

    The rules are intentionally conservative:

    * saturated pictures -> edge detection;
    * a mostly white, mostly neutral image with a small amount of ink ->
      brightness cutoff (logos, line art, text);
    * a mostly dark, mostly neutral image -> inverted brightness cutoff
      (white chalk on a blackboard);
    * everything else -> edge detection.
    """
    white = float(np.count_nonzero(gray >= 235)) / gray.size
    dark = float(np.count_nonzero(gray <= 40)) / gray.size
    mid = 1.0 - white - dark

    if colorful:
        return "edge", False
    if white >= 0.5 and 0.0005 <= dark <= 0.55 and mid <= 0.4:
        return "brightness", False
    if dark >= 0.6 and white <= 0.2 and mid <= 0.4:
        return "brightness", True
    return "edge", False


def _resolve_mode(gray: np.ndarray, colorful: bool, mode: str) -> tuple[str, bool]:
    if mode not in VALID_MODES:
        raise ValueError(f"unknown trace mode: {mode}")
    if mode == "auto":
        return _choose_auto_mode(gray, colorful)
    return mode, False


def _make_bitmap(gray: np.ndarray, mode: str, threshold: float | None) -> np.ndarray:
    if mode == "brightness":
        return _brightness_cutoff(
            gray, BRIGHTNESS_THRESHOLD if threshold is None else threshold
        )
    return _edge_detection(gray, EDGE_THRESHOLD if threshold is None else threshold)


def _format_number(value: float) -> str:
    return f"{value:.3f}"


def _curve_to_path_data(curve) -> str:
    """Convert one Potrace curve into an SVG path data string."""
    start = curve.start_point
    parts = [f"M{_format_number(start.x)},{_format_number(start.y)}"]
    segments = 0
    for segment in curve.segments:
        if segment.is_corner:
            corner = segment.c
            end = segment.end_point
            parts.append(
                f"L{_format_number(corner.x)},{_format_number(corner.y)}"
                f"L{_format_number(end.x)},{_format_number(end.y)}"
            )
        else:
            c1 = segment.c1
            c2 = segment.c2
            end = segment.end_point
            parts.append(
                f"C{_format_number(c1.x)},{_format_number(c1.y)} "
                f"{_format_number(c2.x)},{_format_number(c2.y)} "
                f"{_format_number(end.x)},{_format_number(end.y)}"
            )
        segments += 1
    if segments == 0:
        return ""
    parts.append("Z")
    return "".join(parts)


def _potrace_svg(binary_bw: np.ndarray, width: int, height: int, mode: str) -> str:
    """Trace a black-on-white bitmap into an SVG with one path per contour."""
    # potracer's Bitmap expects a grayscale/boolean image; after its internal
    # inversion, dark pixels become the traced foreground.
    bitmap = Bitmap(binary_bw, blacklevel=0.5)
    curves = bitmap.trace(
        turdsize=DEFAULT_TURDSIZE,
        turnpolicy=POTRACE_TURNPOLICY_MINORITY,
        alphamax=DEFAULT_ALPHAMAX,
        opticurve=True,
        opttolerance=DEFAULT_OPT_TOLERANCE,
    )

    paths: list[str] = []
    for curve in curves:
        path_data = _curve_to_path_data(curve)
        if path_data:
            paths.append(f'<path d="{path_data}"/>')

    if not paths:
        raise ValueError("no contours")

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
        f'<g fill="none" stroke="#000000" stroke-width="1" stroke-linecap="round" '
        f'stroke-linejoin="round" data-trace-mode="{mode}">'
        f"{''.join(paths)}"
        f"</g></svg>"
    )


def raster_to_svg(
    data: bytes,
    max_side: int = 1200,
    mode: str = "auto",
    threshold: float | None = None,
) -> str:
    """Convert a raster image to an Inkscape-style monochrome SVG.

    ``mode`` is ``auto`` (default), ``brightness`` or ``edge``.
    ``threshold`` overrides the Inkscape default for the selected filter.
    """
    gray, width, height, colorful = _flatten_to_grayscale(data, max_side)
    selected, invert = _resolve_mode(gray, colorful, mode)
    working = 255 - gray if invert else gray

    binary = _make_bitmap(working, selected, threshold)
    if not np.any(binary < 128):
        # An empty result usually means the auto choice was wrong (for
        # example, a very light photo). Try the other Inkscape filter once.
        if mode == "auto":
            alternative = "edge" if selected == "brightness" else "brightness"
            binary = _make_bitmap(working, alternative, threshold)
            selected = alternative
        if not np.any(binary < 128):
            raise ValueError("no contours")

    return _potrace_svg(binary, width, height, selected)


def prepare_debug_bitmap(data: bytes, max_side: int = 1200, mode: str = "auto") -> bytes:
    """Return the intermediate black/white bitmap PNG (tests and debugging)."""
    gray, _, _, colorful = _flatten_to_grayscale(data, max_side)
    selected, invert = _resolve_mode(gray, colorful, mode)
    working = 255 - gray if invert else gray
    binary = _make_bitmap(working, selected, None)
    if not np.any(binary < 128) and mode == "auto":
        alternative = "edge" if selected == "brightness" else "brightness"
        binary = _make_bitmap(working, alternative, None)
    image = Image.fromarray(binary)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
