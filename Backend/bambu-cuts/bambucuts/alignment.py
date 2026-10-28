#!/usr/bin/env python3
"""
Manual 3-point alignment for Bambu Cuts.

Instead of aligning the job with a single point (jog the head to the bottom left
corner and let `G92 X0 Y0` define the origin), the operator probes three corners
of the design's bounding box on the real material:

    P1 - top right      (max X, max Y)
    P2 - bottom right   (max X, min Y)
    P3 - bottom left    (min X, min Y)   <- becomes the origin

From those three measurements a 2D transform is fitted and applied to every XY
coordinate of the G-code, so translation, rotation of the sheet, scale error and
(in `affine` mode) non-squareness of the material are all compensated. P3 is
probed last and stays the origin, which keeps the existing `G92 X0 Y0` header and
the existing print workflow untouched.

Modes
-----
affine      : exact fit through all three points (6 DOF: shift, rotation,
              independent X/Y scale, shear). Most accurate, but it trusts the
              probing - a sloppy point skews the result.
similarity  : least squares shift + rotation + one uniform scale (4 DOF).
rigid       : least squares shift + rotation only (3 DOF). Keeps the SVG
              dimensions exactly as designed.
"""

from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

Point = Tuple[float, float]
Matrix = np.ndarray  # 2x3: [[a, b, tx], [d, e, ty]]

# G-code words: a letter followed by a number.
_WORD_RE = re.compile(r'([A-Za-z])\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)')

# Motion commands whose XY arguments describe a position we have to transform.
_MOTION_COMMANDS = {'G0', 'G1', 'G2', 'G3'}
_ARC_COMMANDS = {'G2', 'G3'}

# Marker written into the header of an aligned file so it can be detected later.
ALIGNMENT_MARKER = 'Bambu Cuts - manual 3-point alignment'

POINT_LABELS = {
    'p1': 'top right',
    'p2': 'bottom right',
    'p3': 'bottom left (origin)',
}
POINT_ORDER = ('p1', 'p2', 'p3')


class AlignmentError(ValueError):
    """Raised when an alignment cannot be computed or applied."""


# ---------------------------------------------------------------------------
# G-code line parsing helpers
# ---------------------------------------------------------------------------

def _split_comment(raw: str) -> Tuple[str, str]:
    """Split a line into (code, comment) keeping the ';' with the comment."""
    index = raw.find(';')
    if index < 0:
        return raw, ''
    return raw[:index], raw[index:]


def _normalise_command(letter: str, number: str) -> Optional[str]:
    """'G01' -> 'G1', 'G00' -> 'G0', 'G92' -> 'G92'."""
    try:
        value = float(number)
    except ValueError:
        return None
    if value.is_integer():
        return f"{letter.upper()}{int(value)}"
    return f"{letter.upper()}{number}"


def _parse_line(raw: str):
    """Return (code, comment, words, command) for a single G-code line."""
    code, comment = _split_comment(raw)
    words = _WORD_RE.findall(code)
    command = _normalise_command(*words[0]) if words else None
    return code, comment, words, command


def _format_number(value: float, decimals: int) -> str:
    text = f"{value:.{decimals}f}"
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text or '0'


# ---------------------------------------------------------------------------
# Bounding box of the job
# ---------------------------------------------------------------------------

def gcode_xy_bounds(gcode: str) -> Tuple[float, float, float, float]:
    """
    Return (min_x, min_y, max_x, max_y) of all XY motion in the G-code.

    Coordinates are taken exactly as the machine will see them, so the box
    always matches what is really going to be cut.
    """
    absolute = True
    x = y = 0.0
    xs: List[float] = []
    ys: List[float] = []

    for raw in gcode.splitlines():
        _, _, words, command = _parse_line(raw)
        if not words:
            continue

        if command == 'G90':
            absolute = True
            continue
        if command == 'G91':
            absolute = False
            continue

        if command == 'G92':
            for letter, number in words[1:]:
                if letter.upper() == 'X':
                    x = float(number)
                elif letter.upper() == 'Y':
                    y = float(number)
            continue

        if command not in _MOTION_COMMANDS:
            continue

        moved = False
        for letter, number in words[1:]:
            upper = letter.upper()
            if upper == 'X':
                x = float(number)
                moved = True
            elif upper == 'Y':
                y = float(number)
                moved = True

        if not moved:
            continue

        if not absolute:
            raise AlignmentError(
                "The G-code contains relative XY moves (G91). Alignment needs "
                "absolute XY coordinates (G90)."
            )

        xs.append(x)
        ys.append(y)

    if not xs:
        raise AlignmentError("No XY moves found in the G-code.")

    return min(xs), min(ys), max(xs), max(ys)


def reference_points(bounds: Sequence[float]) -> Dict[str, Point]:
    """Bounding box corners that the operator is asked to probe."""
    min_x, min_y, max_x, max_y = bounds
    return {
        'p1': (max_x, max_y),  # top right
        'p2': (max_x, min_y),  # bottom right
        'p3': (min_x, min_y),  # bottom left, becomes the origin
    }


# ---------------------------------------------------------------------------
# Fitting the transform
# ---------------------------------------------------------------------------

def solve_transform(source: Sequence[Point], target: Sequence[Point],
                    mode: str = 'affine') -> Matrix:
    """
    Fit a 2x3 transform mapping `source` points onto `target` points.

    x' = a*x + b*y + tx
    y' = d*x + e*y + ty
    """
    src = np.asarray(source, dtype=float)
    dst = np.asarray(target, dtype=float)

    if src.shape != (3, 2) or dst.shape != (3, 2):
        raise AlignmentError("Exactly three source and three target points are required.")

    mode = (mode or 'affine').lower()

    if mode == 'affine':
        design = np.column_stack([src, np.ones(3)])
        if abs(np.linalg.det(design)) < 1e-9:
            raise AlignmentError(
                "The three reference points are collinear (or two coincide), "
                "an affine fit is impossible."
            )
        row_x = np.linalg.solve(design, dst[:, 0])
        row_y = np.linalg.solve(design, dst[:, 1])
        return np.vstack([row_x, row_y])

    if mode not in ('similarity', 'rigid'):
        raise AlignmentError(f"Unknown alignment mode: {mode}")

    # Least squares fit (Umeyama / Kabsch) for similarity and rigid modes.
    src_centre = src.mean(axis=0)
    dst_centre = dst.mean(axis=0)
    src_c = src - src_centre
    dst_c = dst - dst_centre

    covariance = src_c.T @ dst_c
    u, singular, vt = np.linalg.svd(covariance)
    correction = np.diag([1.0, np.sign(np.linalg.det(vt.T @ u.T)) or 1.0])
    rotation = vt.T @ correction @ u.T

    if mode == 'similarity':
        variance = float((src_c ** 2).sum())
        if variance < 1e-12:
            raise AlignmentError("The three reference points coincide.")
        scale = float((singular * np.diag(correction)).sum()) / variance
    else:
        scale = 1.0

    linear = scale * rotation
    translation = dst_centre - linear @ src_centre
    return np.column_stack([linear, translation])


def apply_matrix(matrix: Matrix, x: float, y: float) -> Point:
    """Map a single point through the 2x3 matrix."""
    return (
        matrix[0, 0] * x + matrix[0, 1] * y + matrix[0, 2],
        matrix[1, 0] * x + matrix[1, 1] * y + matrix[1, 2],
    )


def decompose(matrix: Matrix) -> Dict[str, float]:
    """Human readable breakdown of the fitted transform."""
    a, b = float(matrix[0, 0]), float(matrix[0, 1])
    d, e = float(matrix[1, 0]), float(matrix[1, 1])

    determinant = a * e - b * d
    scale_x = math.hypot(a, d)
    scale_y = determinant / scale_x if scale_x else 0.0

    return {
        'rotation_deg': math.degrees(math.atan2(d, a)),
        'scale_x': scale_x,
        'scale_y': scale_y,
        'skew_deg': math.degrees(math.atan2(a * b + d * e, a * a + d * d)),
        'determinant': determinant,
        'mirrored': determinant < 0,
    }


def _is_uniform(matrix: Matrix, tolerance: float = 1e-4) -> bool:
    """True when the transform is a pure rotation + uniform scale."""
    info = decompose(matrix)
    if abs(info['skew_deg']) > 1e-3:
        return False
    scale_x, scale_y = info['scale_x'], abs(info['scale_y'])
    if max(scale_x, scale_y) <= 0:
        return False
    return abs(scale_x - scale_y) / max(scale_x, scale_y) < tolerance


# ---------------------------------------------------------------------------
# Rewriting the G-code
# ---------------------------------------------------------------------------

def transform_gcode_xy(gcode: str, matrix: Matrix, decimals: int = 4,
                       start: Point = (0.0, 0.0)) -> Tuple[str, List[str]]:
    """
    Apply the transform to every XY coordinate of every motion command.

    Lines without X/Y (Z moves, feed rates, comments, everything else) are copied
    through byte for byte. Because a rotation mixes X and Y, motion lines that
    only carried one of the two get both coordinates written out.

    Returns (transformed_gcode, warnings).
    """
    warnings: List[str] = []
    uniform = _is_uniform(matrix)
    absolute = True
    x, y = float(start[0]), float(start[1])
    output: List[str] = []
    partial_moves = 0
    arc_moves = 0

    for raw in gcode.splitlines():
        code, comment, words, command = _parse_line(raw)

        if not words:
            output.append(raw)
            continue

        if command == 'G90':
            absolute = True
            output.append(raw)
            continue
        if command == 'G91':
            absolute = False
            output.append(raw)
            continue

        # G92 redefines the work origin; it is emitted verbatim so the existing
        # "G92 X0 Y0 at the probed origin" convention keeps working.
        if command == 'G92':
            for letter, number in words[1:]:
                if letter.upper() == 'X':
                    x = float(number)
                elif letter.upper() == 'Y':
                    y = float(number)
            output.append(raw)
            continue

        if command not in _MOTION_COMMANDS:
            output.append(raw)
            continue

        has_x = any(letter.upper() == 'X' for letter, _ in words[1:])
        has_y = any(letter.upper() == 'Y' for letter, _ in words[1:])

        if not (has_x or has_y):
            output.append(raw)
            continue

        if not absolute:
            raise AlignmentError(
                "The G-code contains relative XY moves (G91). Alignment needs "
                "absolute XY coordinates (G90)."
            )

        if has_x != has_y:
            partial_moves += 1

        for letter, number in words[1:]:
            upper = letter.upper()
            if upper == 'X':
                x = float(number)
            elif upper == 'Y':
                y = float(number)

        new_x, new_y = apply_matrix(matrix, x, y)

        if command in _ARC_COMMANDS:
            arc_moves += 1

        # Rebuild the line, substituting X/Y and rotating the I/J arc vector.
        rebuilt: List[str] = [command]
        written_x = written_y = False
        for letter, number in words[1:]:
            upper = letter.upper()
            if upper == 'X':
                rebuilt.append(f"X{_format_number(new_x, decimals)}")
                written_x = True
                if not has_y:
                    rebuilt.append(f"Y{_format_number(new_y, decimals)}")
                    written_y = True
            elif upper == 'Y':
                if not written_x:
                    rebuilt.append(f"X{_format_number(new_x, decimals)}")
                    written_x = True
                rebuilt.append(f"Y{_format_number(new_y, decimals)}")
                written_y = True
            elif upper in ('I', 'J'):
                # I/J are offsets, so only the linear part applies.
                if upper == 'I':
                    i_value = float(number)
                    j_value = next(
                        (float(n) for l, n in words[1:] if l.upper() == 'J'), 0.0
                    )
                    new_i = matrix[0, 0] * i_value + matrix[0, 1] * j_value
                    rebuilt.append(f"I{_format_number(new_i, decimals)}")
                else:
                    j_value = float(number)
                    i_value = next(
                        (float(n) for l, n in words[1:] if l.upper() == 'I'), 0.0
                    )
                    new_j = matrix[1, 0] * i_value + matrix[1, 1] * j_value
                    rebuilt.append(f"J{_format_number(new_j, decimals)}")
            else:
                rebuilt.append(f"{letter.upper()}{number}")

        line = ' '.join(rebuilt)
        if comment:
            line = f"{line} {comment}" if line else comment
        output.append(line)

    if partial_moves:
        warnings.append(
            f"{partial_moves} move(s) specified only X or only Y; both coordinates "
            f"were written out because a rotation couples the two axes."
        )
    if arc_moves and not uniform:
        warnings.append(
            f"{arc_moves} arc move(s) (G2/G3) were scaled non-uniformly. Arcs cannot "
            f"stay circular under a non-uniform transform - use 'similarity' or "
            f"'rigid' mode, or convert arcs to line segments."
        )

    return '\n'.join(output), warnings


# ---------------------------------------------------------------------------
# Top level entry point
# ---------------------------------------------------------------------------

def _header_comment(report: Dict) -> str:
    matrix = report['matrix']
    lines = [
        '; ---------------------------------------------------------------',
        f'; {ALIGNMENT_MARKER}',
        f"; mode                : {report['mode']}",
        f"; P1 top right        : X{report['points']['p1'][0]:.3f} Y{report['points']['p1'][1]:.3f}",
        f"; P2 bottom right     : X{report['points']['p2'][0]:.3f} Y{report['points']['p2'][1]:.3f}",
        f"; P3 bottom left      : X{report['points']['p3'][0]:.3f} Y{report['points']['p3'][1]:.3f}  <- origin",
        f"; design size         : {report['design_size'][0]:.3f} x {report['design_size'][1]:.3f} mm",
        f"; measured size       : {report['measured_size'][0]:.3f} x {report['measured_size'][1]:.3f} mm",
        f"; rotation            : {report['rotation_deg']:+.4f} deg",
        f"; scale               : X {report['scale_x']:.6f}  Y {report['scale_y']:.6f}",
        f"; skew                : {report['skew_deg']:+.4f} deg",
        f"; squareness error    : {report['squareness_deg']:+.4f} deg",
        f"; max residual        : {report['max_residual_mm']:.4f} mm",
        f"; matrix              : [{matrix[0][0]:.8f} {matrix[0][1]:.8f} {matrix[0][2]:.6f}]",
        f";                       [{matrix[1][0]:.8f} {matrix[1][1]:.8f} {matrix[1][2]:.6f}]",
        '; Start the job with the tool head parked on P3 (bottom left).',
    ]
    for warning in report['warnings']:
        lines.append(f"; WARNING: {warning}")
    lines.append('; ---------------------------------------------------------------')
    return '\n'.join(lines)


def align_gcode(gcode: str, p1: Point, p2: Point, p3: Point,
                mode: str = 'affine', decimals: int = 4,
                add_header: bool = True) -> Tuple[str, Dict]:
    """
    Rewrite `gcode` so the job lands on the three probed points.

    Args:
        gcode: absolute-mode G-code (as produced by the SVG/DXF converter).
        p1: measured position of the design's top right corner.
        p2: measured position of the design's bottom right corner.
        p3: measured position of the design's bottom left corner (the origin).
        mode: 'affine', 'similarity' or 'rigid'.
        decimals: number of decimals used for the rewritten coordinates.
        add_header: prepend a comment block documenting the alignment.

    The probed positions are used as relative measurements: P3 is subtracted from
    all three, so the resulting G-code stays in the "origin at the current tool
    head position" convention of the original project.
    """
    bounds = gcode_xy_bounds(gcode)
    source = reference_points(bounds)

    measured = {'p1': tuple(map(float, p1)),
                'p2': tuple(map(float, p2)),
                'p3': tuple(map(float, p3))}

    origin = measured['p3']
    target = {key: (value[0] - origin[0], value[1] - origin[1])
              for key, value in measured.items()}

    source_list = [source[key] for key in POINT_ORDER]
    target_list = [target[key] for key in POINT_ORDER]

    matrix = solve_transform(source_list, target_list, mode)

    residuals = [
        math.dist(apply_matrix(matrix, *src), dst)
        for src, dst in zip(source_list, target_list)
    ]

    design_width = bounds[2] - bounds[0]
    design_height = bounds[3] - bounds[1]
    measured_width = math.dist(target['p3'], target['p2'])   # bottom edge
    measured_height = math.dist(target['p2'], target['p1'])  # right edge

    # Angle of the corner at P2, which should be 90 degrees on a square sheet.
    vector_a = (target['p3'][0] - target['p2'][0], target['p3'][1] - target['p2'][1])
    vector_b = (target['p1'][0] - target['p2'][0], target['p1'][1] - target['p2'][1])
    norm_a = math.hypot(*vector_a)
    norm_b = math.hypot(*vector_b)
    if norm_a < 1e-9 or norm_b < 1e-9:
        raise AlignmentError("Two of the probed points are identical.")
    cosine = max(-1.0, min(1.0, (vector_a[0] * vector_b[0] + vector_a[1] * vector_b[1])
                           / (norm_a * norm_b)))
    corner_angle = math.degrees(math.acos(cosine))

    info = decompose(matrix)
    warnings: List[str] = []

    if design_width <= 1e-9 or design_height <= 1e-9:
        raise AlignmentError("The job has no area in X or Y, three points cannot be fitted.")

    if abs(info['determinant']) < 1e-6:
        raise AlignmentError(
            "The probed points are (nearly) collinear, the fitted transform would "
            "collapse the job onto a line."
        )

    if info['mirrored']:
        warnings.append(
            "The fitted transform mirrors the job. The three points were probably "
            "captured in the wrong order (expected top right, bottom right, bottom left)."
        )

    size_error_x = measured_width - design_width
    size_error_y = measured_height - design_height
    if max(abs(size_error_x), abs(size_error_y)) > 0.05 * max(design_width, design_height):
        warnings.append(
            f"Measured size differs from the design by more than 5% "
            f"({size_error_x:+.2f} mm in X, {size_error_y:+.2f} mm in Y). "
            f"Check that the points were probed on the design's bounding box corners."
        )

    if abs(corner_angle - 90.0) > 5.0:
        warnings.append(
            f"The corner at P2 measures {corner_angle:.2f} deg instead of 90. "
            f"Check the probed points."
        )

    transformed, transform_warnings = transform_gcode_xy(
        gcode, matrix, decimals=decimals, start=(bounds[0], bounds[1])
    )
    warnings.extend(transform_warnings)

    new_bounds = gcode_xy_bounds(transformed)

    report = {
        'mode': mode,
        'points': measured,
        'points_relative': target,
        'reference': source,
        'matrix': [[float(v) for v in matrix[0]], [float(v) for v in matrix[1]]],
        'rotation_deg': info['rotation_deg'],
        'scale_x': info['scale_x'],
        'scale_y': info['scale_y'],
        'skew_deg': info['skew_deg'],
        'mirrored': info['mirrored'],
        'design_size': (design_width, design_height),
        'measured_size': (measured_width, measured_height),
        'size_error_mm': (size_error_x, size_error_y),
        'squareness_deg': corner_angle - 90.0,
        'residuals_mm': residuals,
        'max_residual_mm': max(residuals),
        'bounds_before': tuple(bounds),
        'bounds_after': tuple(new_bounds),
        'warnings': warnings,
    }

    if add_header:
        transformed = f"{_header_comment(report)}\n{transformed}"

    return transformed, report


def frame_gcode(gcode: str, safe_z: float = 5.0, feed: float = 3000.0) -> str:
    """
    Dry-run block that traces the bounding box of an already aligned job.

    Useful to verify the registration before the knife touches the material:
    the head lifts to `safe_z`, walks the four corners and returns to the origin.
    """
    min_x, min_y, max_x, max_y = gcode_xy_bounds(gcode)
    corners = [
        (min_x, min_y), (max_x, min_y), (max_x, max_y), (min_x, max_y), (min_x, min_y)
    ]
    lines = [
        '; --- alignment frame test (no cutting) ---',
        'G90',
        f'G1 Z{_format_number(safe_z, 3)} F{_format_number(feed, 0)}',
    ]
    for corner_x, corner_y in corners:
        lines.append(
            f'G1 X{_format_number(corner_x, 4)} Y{_format_number(corner_y, 4)} '
            f'F{_format_number(feed, 0)}'
        )
    lines.append('; --- end of frame test ---')
    return '\n'.join(lines)
