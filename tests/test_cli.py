"""Integration checks for the documented CLI and on-disk CAD artifacts."""

import importlib.util
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

import ezdxf
from ezdxf import units


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "cad_sketch.py"
EXAMPLES = ROOT / "examples"
SVG_NS = "{http://www.w3.org/2000/svg}"
HAS_CADQUERY = importlib.util.find_spec("cadquery") is not None


class CLI(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.output = self.scratch / "output"

    def run_build(self, spec_file):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "build", str(spec_file), "--out", str(self.output)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )

    def write_spec(self, spec):
        source = self.scratch / "test-run.json"
        source.write_text(json.dumps(spec), encoding="utf-8")
        return source

    @staticmethod
    def example(filename):
        return json.loads((EXAMPLES / filename).read_text(encoding="utf-8"))

    def check_ok(self, process):
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)

    def check_invalid(self, spec, expected_reason):
        result = self.run_build(self.write_spec(spec))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        message = result.stdout + result.stderr
        self.assertNotIn("Traceback (most recent call last)", message)
        self.assertRegex(message.lower(), expected_reason)
        self.assertEqual(list(self.output.glob("*")), [], "invalid input created artifacts")

    def test_sketch_only_dxf_and_svg(self):
        spec = self.example("band.json")
        del spec["thickness_mm"]
        self.check_ok(self.run_build(self.write_spec(spec)))
        dxf = ezdxf.readfile(self.output / "sketch.dxf")
        self.assertEqual(dxf.units, units.MM)
        circles = dxf.modelspace().query("CIRCLE")
        self.assertEqual(len(circles), 2)
        self.assertEqual(sorted(circle.dxf.radius for circle in circles), [9.0, 10.75])
        for circle in circles:
            self.assertEqual(tuple(circle.dxf.center), (0.0, 0.0, 0.0))
        self.assertNotEqual(circles[0].dxf.layer, circles[1].dxf.layer)
        svg = ET.parse(self.output / "sketch.svg").getroot()
        self.assertEqual(svg.tag, f"{SVG_NS}svg")
        self.assertIn("viewBox", svg.attrib)
        self.assertFalse((self.output / "solid.step").exists())
        self.assertFalse((self.output / "solid.stl").exists())

    def test_polygon_has_closed_dxf_boundary(self):
        spec = self.example("tag.json")
        del spec["thickness_mm"]
        self.check_ok(self.run_build(self.write_spec(spec)))
        msp = ezdxf.readfile(self.output / "sketch.dxf").modelspace()
        circles = msp.query("CIRCLE")
        self.assertEqual(len(circles), 1)
        self.assertEqual(tuple(circles[0].dxf.center), (0.0, 9.0, 0.0))
        self.assertAlmostEqual(circles[0].dxf.radius, 1.5)
        polylines = msp.query("LWPOLYLINE POLYLINE")
        self.assertEqual(len(polylines), 1)
        polyline = polylines[0]
        if polyline.dxftype() == "LWPOLYLINE":
            points = [tuple(point) for point in polyline.get_points("xy")]
            self.assertTrue(polyline.closed)
        else:
            points = [(v.dxf.location.x, v.dxf.location.y) for v in polyline.vertices]
            self.assertTrue(polyline.is_closed)
        self.assertEqual(set(points), {tuple(point) for point in spec["outline"]["points"]})
        svg = ET.parse(self.output / "sketch.svg").getroot()
        self.assertEqual(svg.tag, f"{SVG_NS}svg")

    def test_dxf_omits_location_author_and_path_and_has_neutral_metadata(self):
        spec = self.example("band.json")
        del spec["thickness_mm"]
        source = self.write_spec(spec)
        self.check_ok(self.run_build(source))
        output = self.output / "sketch.dxf"
        for attempt in range(2):
            if attempt:
                self.check_ok(self.run_build(source))
            data = output.read_bytes()
            for field in (b"$LATITUDE", b"$LONGITUDE", b"$NORTHDIRECTION", b"$TIMEZONE",
                          b"$LASTSAVEDBY", b"$HYPERLINKBASE", b"$PROJECTNAME", b"$MENU", b"$STYLESHEET",
                          b"37.795", b"-122.394"):
                self.assertNotIn(field, data)
            self.assertNotIn(str(self.scratch).encode(), data)
            dxf = ezdxf.readfile(output)
            self.assertEqual(dxf.units, units.MM)
            self.assertEqual(sorted(c.dxf.radius for c in dxf.modelspace().query("CIRCLE")), [9.0, 10.75])
            # Inspect serialized tags: the ezdxf reader can synthesize new metadata.
            lines = data.splitlines()
            expected_headers = {
                b"$TDCREATE": b"2451545.0", b"$TDUCREATE": b"2451545.0",
                b"$TDUPDATE": b"2451545.0", b"$TDUUPDATE": b"2451545.0",
                b"$FINGERPRINTGUID": b"{00000000-0000-0000-0000-000000000000}",
                b"$VERSIONGUID": b"{00000000-0000-0000-0000-000000000000}",
            }
            for key, value in expected_headers.items():
                self.assertEqual(lines[lines.index(key) + 2], value)

    def test_rounded_sketch_has_exact_arcs_and_reports_trimmed_area(self):
        spec = self.example("rounded_pendant.json")
        del spec["thickness_mm"]
        # Both clockwise and counterclockwise source sketches must produce the same
        # inward rounding, including positive DXF bulges on the normalized CCW path.
        spec["outline"]["points"].reverse()
        self.check_ok(self.run_build(self.write_spec(spec)))
        dxf = ezdxf.readfile(self.output / "sketch.dxf")
        self.assertEqual(dxf.units, units.MM)
        poly = dxf.modelspace().query("LWPOLYLINE").first
        self.assertTrue(poly.closed)
        segments = list(poly.virtual_entities())
        arcs = [curve for curve in segments if curve.dxftype() == "ARC"]
        self.assertEqual(len(segments), 8)
        self.assertEqual(len(arcs), 4)
        for arc in arcs:
            self.assertAlmostEqual(arc.dxf.radius, 3)
            self.assertAlmostEqual((arc.dxf.end_angle - arc.dxf.start_angle) % 360, 90)
        path = ET.parse(self.output / "sketch.svg").find(f".//{SVG_NS}path")
        self.assertIsNotNone(path)
        self.assertEqual(path.attrib["d"].split().count("A"), 4)
        self.assertTrue(path.attrib["d"].endswith("Z"))
        report = json.loads((self.output / "report.json").read_text())
        self.assertAlmostEqual(report["outline_area_mm2"], 18*26 - 9*(4-math.pi))
        self.assertAlmostEqual(report["net_area_mm2"], report["outline_area_mm2"] - 1.5**2*math.pi)
        self.assertEqual(report["corner_radius_mm"], 3)

    @unittest.skipUnless(HAS_CADQUERY, "optional 3D dependency CadQuery not installed")
    def test_rounded_extrusion_preserves_native_cylinders_in_step(self):
        import cadquery

        self.check_ok(self.run_build(EXAMPLES / "rounded_pendant.json"))
        shape = cadquery.importers.importStep(str(self.output / "solid.step")).val()
        self.assertEqual(len(shape.Solids()), 1)
        self.assertTrue(shape.isValid())
        self.assertEqual(sum(face.geomType() == "CYLINDER" for face in shape.Faces()), 5)
        self.assertAlmostEqual(shape.Volume(), (18*26 - 9*(4-math.pi) - 1.5**2*math.pi)*2, places=6)
        self.assertAlmostEqual(shape.BoundingBox().xlen, 18, places=6)
        self.assertAlmostEqual(shape.BoundingBox().ylen, 26, places=6)
        self.assertGreater((self.output / "solid.stl").stat().st_size, 84)

    @unittest.skipUnless(HAS_CADQUERY, "optional 3D dependency CadQuery not installed")
    def test_triangle_rounded_bounds_are_the_actual_solid_extents(self):
        import cadquery

        spec = self.example("rounded_pendant.json")
        spec["outline"] = {"type": "polygon", "points": [[0, 0], [10, 0], [5, 9]], "corner_radius": 1}
        spec["holes"] = []
        self.check_ok(self.run_build(self.write_spec(spec)))
        box = cadquery.importers.importStep(str(self.output / "solid.step")).val().BoundingBox()
        report = json.loads((self.output / "report.json").read_text())
        self.assertLess(report["bounds_mm"]["max_y"], 9)
        self.assertAlmostEqual(box.xlen, report["width_mm"], places=6)
        self.assertAlmostEqual(box.ylen, report["height_mm"], places=6)

    def test_rejects_hole_that_touches_or_lies_outside_rounding(self):
        spec = self.example("rounded_pendant.json")
        square = [[0, 0], [10, 0], [10, 10], [0, 10]]
        spec["outline"] = {"type": "polygon", "points": square, "corner_radius": 3}
        del spec["thickness_mm"]
        tangent_center = 7 + 2.5/math.sqrt(2)
        cases = [([tangent_center, tangent_center], 0.5), ([9.5, 9.5], 0.2)]
        for center, radius in cases:
            with self.subTest(center=center, radius=radius):
                spec["holes"] = [{"type": "circle", "center": center, "radius": radius}]
                self.check_invalid(spec, "strictly inside the rounded outline")
        safe_center = 7 + 2.4/math.sqrt(2)
        spec["holes"] = [{"type": "circle", "center": [safe_center, safe_center], "radius": 0.5}]
        self.check_ok(self.run_build(self.write_spec(spec)))

    def test_rejects_radii_that_touch_adjacent_fillets_or_use_concave_outline(self):
        spec = self.example("rounded_pendant.json")
        spec["outline"]["corner_radius"] = 9
        self.check_invalid(spec, "corner_radius is too large")
        spec["outline"] = {"type": "polygon", "points": [[0, 0], [10, 0], [10, 10], [5, 5], [0, 10]],
                           "corner_radius": 1}
        self.check_invalid(spec, "strictly convex")
        spec["outline"]["points"] = [[0, 0], [5, 0], [10, 0], [10, 10], [0, 10]]
        self.check_invalid(spec, "strictly convex")

    @unittest.skipUnless(HAS_CADQUERY, "optional 3D dependency CadQuery not installed")
    def test_extruded_band_reimports_with_expected_volume_and_size(self):
        import cadquery

        self.check_ok(self.run_build(EXAMPLES / "band.json"))
        imported = cadquery.importers.importStep(str(self.output / "solid.step"))
        solids = imported.solids().vals()
        self.assertEqual(len(solids), 1)
        band = solids[0]
        self.assertTrue(band.isValid())
        self.assertAlmostEqual(band.Volume(), math.pi * (10.75**2 - 9**2) * 4, places=5)
        box = band.BoundingBox()
        self.assertAlmostEqual(box.xlen, 21.5, places=5)
        self.assertAlmostEqual(box.ylen, 21.5, places=5)
        self.assertAlmostEqual(box.zlen, 4.0, places=5)
        self.assertGreater((self.output / "solid.stl").stat().st_size, 84)

    @unittest.skipUnless(HAS_CADQUERY, "optional 3D dependency CadQuery not installed")
    def test_extruded_pendant_reimports_with_hole_preserved(self):
        import cadquery

        self.check_ok(self.run_build(EXAMPLES / "tag.json"))
        spec = self.example("tag.json")
        vertices = spec["outline"]["points"]
        area = abs(sum(p[0] * q[1] - q[0] * p[1] for p, q in zip(vertices, vertices[1:] + vertices[:1]))) / 2
        expected = (area - math.pi * spec["holes"][0]["radius"]**2) * spec["thickness_mm"]
        shape = cadquery.importers.importStep(str(self.output / "solid.step")).val()
        self.assertEqual(len(shape.Solids()), 1)
        self.assertTrue(shape.isValid())
        self.assertAlmostEqual(shape.Volume(), expected, places=5)

    def test_rejects_overlapping_holes(self):
        spec = self.example("band.json")
        spec["holes"] = [
            {"type": "circle", "center": [0, 0], "radius": 2},
            {"type": "circle", "center": [1, 0], "radius": 2},
        ]
        self.check_invalid(spec, r"overlap|intersect|touch|clearance")

    def test_rejects_hole_outside_polygon(self):
        spec = self.example("tag.json")
        spec["holes"][0]["center"] = [0, 16]
        self.check_invalid(spec, r"inside|outside|interior|boundary|clearance|contain")

    def test_rejects_hole_crossing_or_touching_circle(self):
        spec = self.example("band.json")
        spec["holes"] = [{"type": "circle", "center": [8.75, 0], "radius": 2}]
        self.check_invalid(spec, r"inside|outside|interior|boundary|clearance|contain|touch")

    def test_rejects_nonzero_area_self_crossing_polygon(self):
        spec = self.example("tag.json")
        spec["outline"]["points"] = [[0, 0], [6, 6], [0, 6], [8, 0]]
        spec["holes"] = []
        self.check_invalid(spec, r"intersects itself|self.intersect|cross|simple|invalid.*polygon")

    def test_rejects_non_finite_coordinates(self):
        spec = self.example("band.json")
        spec["outline"]["center"] = [float("inf"), 0]
        self.check_invalid(spec, r"finite|infinity|json|constant|number")

    def test_rejects_huge_finite_coordinates(self):
        spec = self.example("band.json")
        spec["outline"]["center"] = [1e25, 0]
        self.check_invalid(spec, r"bound|range|magnitude|large|limit|between|within")

    def test_rejects_unknown_key_and_nonpositive_thickness(self):
        spec = self.example("tag.json")
        spec["run_code"] = "not executable"
        self.check_invalid(spec, r"unknown|unexpected|extra|invalid.*(field|key)")
        del spec["run_code"]
        spec["thickness_mm"] = 0
        self.check_invalid(spec, r"thickness|positive|larger|greater")


if __name__ == "__main__":
    unittest.main()
