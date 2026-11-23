"""Build dimensioned, planar CAD profiles from a bounded JSON specification."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, NamedTuple
from xml.sax.saxutils import escape


MIN_FEATURE = 1e-5  # mm; below which geometry-kernel results can be fragile
MAX_MM = 100_000.0  # mm; keeps calculations in a reasonable coordinate range
MAX_PROFILE_ITEMS = 256
MAX_SPEC_BYTES = 256 * 1024
Point = tuple[float, float]


class SpecError(ValueError):
    """The CAD spec is invalid or cannot be built."""


class Corner(NamedTuple):
    start: Point
    end: Point
    center: Point
    start_angle: float
    turn: float
    radius: float
    setback: float

    def point_at(self, angle: float) -> Point:
        return (self.center[0] + self.radius*math.cos(angle),
                self.center[1] + self.radius*math.sin(angle))

    def includes_angle(self, angle: float) -> bool:
        return (angle - self.start_angle) % math.tau <= self.turn


def _object(value: Any, field: str, required: set[str], optional: set[str] | None = None) -> None:
    if not isinstance(value, dict):
        raise SpecError(f"{field} must be an object")
    missing = required - value.keys()
    unknown = value.keys() - required - (optional or set())
    if missing:
        raise SpecError(f"{field} is missing: {', '.join(sorted(missing))}")
    if unknown:
        raise SpecError(f"{field} has unknown keys: {', '.join(sorted(unknown))}")


def _mm(value: Any, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SpecError(f"{field} must be a finite number in mm")
    if not -MAX_MM <= value <= MAX_MM:
        raise SpecError(f"{field} must be finite and within the ±{MAX_MM:g} mm range")
    if positive and value < MIN_FEATURE:
        raise SpecError(f"{field} must be positive and at least {MIN_FEATURE:g} mm")
    return float(value)


def _point(value: Any, field: str) -> Point:
    if not isinstance(value, list) or len(value) != 2:
        raise SpecError(f"{field} must be an [x, y] array")
    return _mm(value[0], f"{field}[0]"), _mm(value[1], f"{field}[1]")


def _circle(value: Any, field: str) -> dict[str, Any]:
    _object(value, field, {"type", "center", "radius"})
    if value["type"] != "circle":
        raise SpecError(f"{field}.type must be 'circle'")
    center = _point(value["center"], f"{field}.center")
    radius = _mm(value["radius"], f"{field}.radius", positive=True)
    if any(abs(c) + radius > MAX_MM for c in center):
        raise SpecError(f"{field} exceeds the ±{MAX_MM:g} mm coordinate range")
    return {"type": "circle", "center": center, "radius": radius}


def _edges(points: list[Point]) -> list[tuple[Point, Point]]:
    return list(zip(points, points[1:] + points[:1]))


def _cross(a: Point, b: Point, c: Point) -> float:
    return (b[0]-a[0]) * (c[1]-a[1]) - (b[1]-a[1]) * (c[0]-a[0])


def _distance_to_segment(p: Point, a: Point, b: Point) -> float:
    dx, dy = b[0]-a[0], b[1]-a[1]
    t = ((p[0]-a[0])*dx + (p[1]-a[1])*dy) / (dx*dx + dy*dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(p[0] - a[0] - t*dx, p[1] - a[1] - t*dy)


def _segments_cross(a: Point, b: Point, c: Point, d: Point) -> bool:
    v1, v2 = _cross(a, b, c), _cross(a, b, d)
    v3, v4 = _cross(c, d, a), _cross(c, d, b)
    if ((v1 > 0 > v2) or (v2 > 0 > v1)) and ((v3 > 0 > v4) or (v4 > 0 > v3)):
        return True
    return any(math.dist(p, q) <= MIN_FEATURE or _distance_to_segment(p, q, r) <= MIN_FEATURE
               for p, q, r in ((a, c, d), (b, c, d), (c, a, b), (d, a, b)))


def _signed_area(points: list[Point]) -> float:
    # Translate first to avoid cancellation for small profiles far from the origin.
    origin = points[0]
    return math.fsum(_cross(origin, a, b) for a, b in _edges(points)) / 2


def _polygon(value: Any) -> dict[str, Any]:
    _object(value, "outline", {"type", "points"}, {"corner_radius"})
    raw = value["points"]
    if not isinstance(raw, list) or not 3 <= len(raw) <= MAX_PROFILE_ITEMS:
        raise SpecError(f"outline.points must contain 3–{MAX_PROFILE_ITEMS} vertices")
    points = [_point(p, f"outline.points[{i}]") for i, p in enumerate(raw)]
    edges = _edges(points)
    if any(math.dist(a, b) <= MIN_FEATURE for a, b in edges):
        raise SpecError("outline polygon has repeated or too-close vertices; don't repeat the first vertex at the end")
    for i, (a, b) in enumerate(edges):
        c = points[(i+2) % len(points)]
        # At a corner, an adjacent edge must not fold back onto the preceding edge.
        if _distance_to_segment(a, b, c) <= MIN_FEATURE or _distance_to_segment(c, a, b) <= MIN_FEATURE:
            raise SpecError("outline polygon contains overlapping adjacent edges")
        for j in range(i+2, len(edges)):
            if i == 0 and j == len(edges)-1:
                continue  # first and last edges share one endpoint
            if _segments_cross(a, b, *edges[j]):
                raise SpecError("outline polygon has a self-intersection or nonadjacent edges that touch")
    signed = _signed_area(points)
    if abs(signed) <= MIN_FEATURE**2:
        raise SpecError("outline polygon must have nonzero area")
    # Keep DXF/CAD winding consistent without changing the first point.
    if signed < 0:
        points = points[:1] + points[:0:-1]
    outline = {"type": "polygon", "points": points}
    if "corner_radius" in value:
        outline["corner_radius"] = _mm(value["corner_radius"], "outline.corner_radius", positive=True)
    return outline


def _rounded_corners(outline: dict[str, Any]) -> list[Corner]:
    """Return the tangent arc at each CCW vertex, with straight segments between them."""
    if "corner_radius" not in outline:
        return []
    radius = outline["corner_radius"]
    points = outline["points"]
    corners: list[Corner] = []
    for i, p in enumerate(points):
        a, b = points[i-1], points[(i+1) % len(points)]
        incoming, outgoing = math.dist(a, p), math.dist(p, b)
        ux, uy = (p[0]-a[0])/incoming, (p[1]-a[1])/incoming
        vx, vy = (b[0]-p[0])/outgoing, (b[1]-p[1])/outgoing
        turn = math.atan2(ux*vy - uy*vx, ux*vx + uy*vy)
        if turn <= 0:
            raise SpecError("outline.corner_radius requires a strictly convex polygon (no collinear corners)")
        setback = radius * math.tan(turn/2)
        if setback >= min(incoming, outgoing) - MIN_FEATURE:
            raise SpecError("outline.corner_radius is too large for an adjacent edge")
        # The arc must have meaningful chord length and sagitta (depth) for the kernel.
        if 2*radius*math.sin(turn/2) <= MIN_FEATURE or 2*radius*math.sin(turn/4)**2 <= 1e-8:
            raise SpecError("outline.corner_radius creates an arc too small or nearly straight to export")
        start = (p[0]-setback*ux, p[1]-setback*uy)
        end = (p[0]+setback*vx, p[1]+setback*vy)
        center = (start[0]-radius*uy, start[1]+radius*ux)
        corners.append(Corner(start, end, center, math.atan2(-ux, uy), turn, radius, setback))
    for i, corner in enumerate(corners):
        if corner.setback + corners[i-1].setback + MIN_FEATURE >= math.dist(points[i-1], points[i]):
            raise SpecError("outline.corner_radius is too large: neighboring arcs leave no straight edge")
    return corners


def _inside_rounded_profile(p: Point, clearance: float, corners: list[Corner]) -> bool:
    """Check that a point inside the original polygon is clear of the rounded boundary."""
    for i, corner in enumerate(corners):
        dx, dy = p[0]-corner.center[0], p[1]-corner.center[1]
        radial = math.hypot(dx, dy)
        # Only the vertex side of the tangent chord loses material.
        if _cross(corner.start, corner.end, p) < 0 and radial > corner.radius:
            return False
        if _distance_to_segment(p, corners[i-1].end, corner.start) <= clearance:
            return False
        arc_distance = min(math.dist(p, corner.start), math.dist(p, corner.end))
        if radial == 0 or corner.includes_angle(math.atan2(dy, dx)):
            arc_distance = min(arc_distance, abs(radial-corner.radius))
        if arc_distance <= clearance:
            return False
    return True


def _inside_polygon(p: Point, points: list[Point]) -> bool:
    x, y = p
    crossings = sum((ay > y) != (by > y) and x < (bx-ax)*(y-ay)/(by-ay)+ax
                    for (ax, ay), (bx, by) in _edges(points))
    return bool(crossings % 2)


def validate_spec(value: Any) -> dict[str, Any]:
    """Validate and normalize a JSON-compatible object; raise SpecError on failure."""
    _object(value, "spec", {"name", "units", "outline"}, {"holes", "thickness_mm"})
    name = value["name"]
    if (not isinstance(name, str) or not name.strip() or len(name) > 100
            or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF or ord(c) in (0xFFFE, 0xFFFF) for c in name)):
        raise SpecError("name must be nonempty text, up to 100 characters, with no control characters")
    if value["units"] != "mm":
        raise SpecError("units must be 'mm'; convert other units before building")
    raw_outline = value["outline"]
    if not isinstance(raw_outline, dict) or raw_outline.get("type") not in ("circle", "polygon"):
        raise SpecError("outline.type must be 'circle' or 'polygon'")
    outline = _circle(raw_outline, "outline") if raw_outline["type"] == "circle" else _polygon(raw_outline)
    corners = _rounded_corners(outline)
    raw_holes = value.get("holes", [])
    if not isinstance(raw_holes, list) or len(raw_holes) > MAX_PROFILE_ITEMS:
        raise SpecError(f"holes must be an array of up to {MAX_PROFILE_ITEMS} circles")
    holes: list[dict[str, Any]] = []
    for i, raw_hole in enumerate(raw_holes):
        field = f"holes[{i}]"
        hole = _circle(raw_hole, field)
        center, radius = hole["center"], hole["radius"]
        if outline["type"] == "circle":
            inside = (math.dist(center, outline["center"]) + radius + MIN_FEATURE < outline["radius"])
        else:
            points = outline["points"]
            inside = _inside_polygon(center, points) and (
                _inside_rounded_profile(center, radius + MIN_FEATURE, corners) if corners else
                all(_distance_to_segment(center, a, b) > radius + MIN_FEATURE for a, b in _edges(points))
            )
        if not inside:
            qualifier = "rounded " if corners else ""
            raise SpecError(f"{field} must be strictly inside the {qualifier}outline, without touching it")
        if any(math.dist(center, other["center"]) <= radius + other["radius"] + MIN_FEATURE for other in holes):
            raise SpecError(f"{field} overlaps or touches a previous hole")
        holes.append(hole)
    result = {"name": name, "units": "mm", "outline": outline, "holes": holes}
    if "thickness_mm" in value:
        result["thickness_mm"] = _mm(value["thickness_mm"], "thickness_mm", positive=True)
    return result


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, val in pairs:
        if key in result:
            raise SpecError(f"duplicate JSON key: {key}")
        result[key] = val
    return result


def read_spec(path: Path) -> dict[str, Any]:
    with path.open("rb") as infile:
        raw = infile.read(MAX_SPEC_BYTES + 1)
    if len(raw) > MAX_SPEC_BYTES:
        raise SpecError(f"spec exceeds {MAX_SPEC_BYTES} bytes")
    try:
        return validate_spec(json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys))
    except SpecError:
        raise
    except (ValueError, RecursionError) as exc:
        raise SpecError(f"invalid UTF-8 JSON: {exc}") from exc


def _bounds(outline: dict[str, Any]) -> tuple[float, float, float, float]:
    if outline["type"] == "circle":
        x, y = outline["center"]
        r = outline["radius"]
        return x-r, y-r, x+r, y+r
    corners = _rounded_corners(outline)
    if not corners:
        xs, ys = zip(*outline["points"])
        return min(xs), min(ys), max(xs), max(ys)
    extremities = [p for c in corners for p in (c.start, c.end)]
    extremities.extend(c.point_at(angle) for c in corners
                       for angle in (0, math.pi/2, math.pi, 3*math.pi/2) if c.includes_angle(angle))
    xs, ys = zip(*extremities)
    return min(xs), min(ys), max(xs), max(ys)


def write_svg(spec: dict[str, Any], path: Path) -> None:
    xmin, ymin, xmax, ymax = _bounds(spec["outline"])
    span = max(xmax-xmin, ymax-ymin)
    pad = 0.07 * span
    width, height = xmax-xmin+2*pad, ymax-ymin+2*pad
    contours: list[str] = []
    for idx, profile in enumerate([spec["outline"], *spec["holes"]]):
        ident = "outline" if idx == 0 else f"hole-{idx}"
        if profile["type"] == "circle":
            x, y = profile["center"]
            contours.append(f'    <circle id="{ident}" cx="{x}" cy="{y}" r="{profile["radius"]}"/>')
        elif "corner_radius" in profile:
            corners = _rounded_corners(profile)
            start = corners[0].start
            commands = [f"M {start[0]} {start[1]}"]
            for i, c in enumerate(corners):
                if i:
                    commands.append(f"L {c.start[0]} {c.start[1]}")
                commands.append(f"A {c.radius} {c.radius} 0 0 1 {c.end[0]} {c.end[1]}")
            commands.append("Z")
            contours.append(f'    <path id="{ident}" d="{" ".join(commands)}"/>')
        else:
            points = " ".join(f"{x},{y}" for x, y in profile["points"])
            contours.append(f'    <polygon id="{ident}" points="{points}"/>')
    svg = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}mm" height="{height}mm"',
        f'     viewBox="{xmin-pad} {-ymax-pad} {width} {height}">',
        f'  <title>{escape(spec["name"])}</title>',
        '  <desc>Dimensioned in millimeters. Closed outline followed by optional circular holes.</desc>',
        f'  <g transform="scale(1,-1)" fill="none" stroke="#132f42" stroke-width="{span*0.004}">',
        *contours,
        '  </g>',
        '</svg>',
    ]
    path.write_text("\n".join(svg) + "\n", encoding="utf-8")


def write_dxf(spec: dict[str, Any], path: Path) -> None:
    try:
        import ezdxf
    except ImportError as exc:
        raise SpecError("DXF export requires ezdxf; install the project dependencies") from exc
    # ezdxf's default header contains a real-world place, even for an unlocated
    # drawing. Omit geographic and author/path fields, and serialize constant
    # neutral values instead of using the host clock or creating random GUIDs.
    saved_option = ezdxf.options.write_fixed_meta_data_for_testing
    ezdxf.options.write_fixed_meta_data_for_testing = True
    try:
        doc = ezdxf.new("R2010", units=4)  # INSUNITS=4 is mm
        for name in ("$LATITUDE", "$LONGITUDE", "$NORTHDIRECTION", "$TIMEZONE",
                     "$LASTSAVEDBY", "$HYPERLINKBASE", "$PROJECTNAME", "$MENU", "$STYLESHEET"):
            if name in doc.header.varnames():
                del doc.header[name]
        doc.header["$MEASUREMENT"] = 1  # metric
        doc.layers.new("OUTLINE", dxfattribs={"color": 7})
        doc.layers.new("HOLES", dxfattribs={"color": 4})
        msp = doc.modelspace()
        for idx, profile in enumerate([spec["outline"], *spec["holes"]]):
            attrs = {"layer": "OUTLINE" if idx == 0 else "HOLES"}
            if profile["type"] == "circle":
                msp.add_circle(profile["center"], profile["radius"], dxfattribs=attrs)
            elif "corner_radius" in profile:
                vertices = [vertex for c in _rounded_corners(profile)
                            for vertex in ((*c.start, math.tan(c.turn/4)), (*c.end, 0.0))]
                msp.add_lwpolyline(vertices, format="xyb", close=True, dxfattribs=attrs)
            else:
                msp.add_lwpolyline(profile["points"], close=True, dxfattribs=attrs)
        doc.saveas(path)
    finally:
        ezdxf.options.write_fixed_meta_data_for_testing = saved_option


def write_solid(spec: dict[str, Any], directory: Path) -> float:
    try:
        import cadquery as cq
    except ImportError as exc:
        raise SpecError("solid export requires CadQuery; install the project dependencies") from exc
    height = spec["thickness_mm"]
    outline = spec["outline"]
    try:
        work = cq.Workplane("XY")
        if outline["type"] == "circle":
            base = work.moveTo(*outline["center"]).circle(outline["radius"]).extrude(height)
        elif "corner_radius" in outline:
            corners = _rounded_corners(outline)
            edges = []
            for i, c in enumerate(corners):
                start = cq.Vector(*c.start, 0)
                edges.append(cq.Edge.makeLine(cq.Vector(*corners[i-1].end, 0), start))
                mid = cq.Vector(*c.point_at(c.start_angle + c.turn/2), 0)
                edges.append(cq.Edge.makeThreePointArc(start, mid, cq.Vector(*c.end, 0)))
            face_wire = cq.Wire.assembleEdges(edges)
            base = work.newObject([cq.Solid.extrudeLinear(face_wire, [], cq.Vector(0, 0, height))])
        else:
            base = work.polyline(outline["points"]).close().extrude(height)
        if spec["holes"]:
            # Extend cutters past both faces to avoid coincident-face tolerances.
            extra = max(height, 1.0)
            # Hole radii vary, so build each cutter and combine its solids for one cut.
            cutters = [cq.Solid.makeCylinder(item["radius"], height + 2*extra,
                                             cq.Vector(*item["center"], -extra))
                       for item in spec["holes"]]
            base = base.cut(cq.Compound.makeCompound(cutters))
        shape = base.val()
        if len(shape.Solids()) != 1 or not shape.isValid() or shape.Volume() <= 0:
            raise SpecError("geometry kernel did not produce one valid positive-volume solid")
        cq.exporters.export(shape, str(directory / "solid.step"))
        cq.exporters.export(shape, str(directory / "solid.stl"), tolerance=0.002, angularTolerance=0.05)
        return float(shape.Volume())
    except SpecError:
        raise
    except Exception as exc:
        raise SpecError(f"CAD kernel could not build the solid ({type(exc).__name__}): {exc}") from exc


def build(spec: dict[str, Any], out: Path) -> dict[str, Any]:
    """Write DXF/SVG/report and optional STEP/STL from a normalized spec."""
    out.mkdir(parents=True, exist_ok=True)
    if "thickness_mm" not in spec and any((out / n).exists() for n in ("solid.step", "solid.stl")):
        raise SpecError("output directory already contains a solid; choose a new --out for a sketch-only build")
    xmin, ymin, xmax, ymax = _bounds(spec["outline"])
    outline = spec["outline"]
    surface = (math.pi * outline["radius"]**2 if outline["type"] == "circle"
               else abs(_signed_area(outline["points"])))
    if "corner_radius" in outline:
        surface -= math.fsum(c.radius*c.setback - c.radius**2*c.turn/2 for c in _rounded_corners(outline))
    net = surface - math.fsum(math.pi * hole["radius"]**2 for hole in spec["holes"])
    report: dict[str, Any] = {
        "name": spec["name"], "units": "mm", "outline_type": outline["type"],
        "bounds_mm": {"min_x": xmin, "min_y": ymin, "max_x": xmax, "max_y": ymax},
        "width_mm": xmax-xmin, "height_mm": ymax-ymin, "hole_count": len(spec["holes"]),
        "outline_area_mm2": surface, "net_area_mm2": net,
        "files": ["sketch.svg", "sketch.dxf", "report.json"],
    }
    if "corner_radius" in outline:
        report["corner_radius_mm"] = outline["corner_radius"]
    with tempfile.TemporaryDirectory(prefix=".cad-sketch-", dir=out) as staging:
        target = Path(staging)
        write_svg(spec, target / "sketch.svg")
        write_dxf(spec, target / "sketch.dxf")
        if "thickness_mm" in spec:
            report["thickness_mm"] = spec["thickness_mm"]
            volume = write_solid(spec, target)
            expected = net * spec["thickness_mm"]
            if not math.isclose(volume, expected, rel_tol=1e-6, abs_tol=1e-12):
                raise SpecError("CAD solid volume did not match the sketch and specified thickness")
            report["volume_mm3"] = volume
            report["files"] += ["solid.step", "solid.stl"]
        (target / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        for filename in report["files"]:
            os.replace(target / filename, out / filename)
    return report


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    builder = commands.add_parser("build", help="validate and export a JSON sketch specification")
    builder.add_argument("spec", type=Path)
    builder.add_argument("--out", type=Path, required=True, help="output folder")
    args = cli.parse_args(argv)
    try:
        spec = read_spec(args.spec)
        report = build(spec, args.out)
    except (OSError, SpecError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote {', '.join(report['files'])} to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
