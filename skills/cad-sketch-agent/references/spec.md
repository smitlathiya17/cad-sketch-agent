# Sketch spec and export

Use finite numbers in millimetres. The coordinate system is standard CAD XY with Y up. An outline is a circle with a centre and radius, or a simple polygon with at least three vertices in perimeter order. Do not repeat the first vertex at the end. For a strictly convex polygon, optional `outline.corner_radius` is a positive radius in millimetres applied at every vertex. Use the original sharp vertex positions; the exporter trims the edges to join the corner arcs tangentially. Leave enough length between adjacent corners for a straight segment. This radius does not round the top or bottom edges of an extrusion. Holes are optional circles that must lie strictly inside the final outline, including its rounded corners, without touching it or one another. Radius means half the diameter. Positive `thickness_mm` adds a straight extrusion along +Z; all holes run through the part.

```json
{
  "name": "Simple band",
  "units": "mm",
  "outline": {"type": "circle", "center": [0, 0], "radius": 10.75},
  "holes": [{"type": "circle", "center": [0, 0], "radius": 9}],
  "thickness_mm": 4
}
```

This example has an 18 mm inside diameter, a 21.5 mm outside diameter, a 1.75 mm radial wall, and a 4 mm axial height. It is not a standardized ring size or a finished comfort-fit profile.

### Rounded pendant example

```json
{
  "name": "Rounded rectangle pendant concept",
  "units": "mm",
  "outline": {
    "type": "polygon",
    "points": [[-9, -13], [9, -13], [9, 13], [-9, 13]],
    "corner_radius": 3
  },
  "holes": [{"type": "circle", "center": [0, 8.5], "radius": 1.5}],
  "thickness_mm": 2
}
```

These illustrative dimensions produce an 18 × 26 mm flat pendant with four R3 corners, 2 mm thickness, and a 3 mm through-hole with a 3 mm gap to the top edge. The hole is not a stone seat. This is concept geometry; confirm material, thickness, attachment clearance, and fabrication with the jeweller before producing it.

Output: `sketch.svg`, `sketch.dxf`, and `report.json`. When thickness is present, add `solid.step` (3D CAD solid) and `solid.stl` (mesh). STL itself is unitless; interpret it as millimetres. DXF contains editable 2D entities, and STEP contains solid geometry; neither includes the source sketch constraints or native feature history. Keep the JSON and edit its parameters to regenerate all outputs.

The tested environment is Python 3.12 with ezdxf 1.4.5 and CadQuery 2.7.0. 2D builds do not need CadQuery. Use a fresh environment if you need to install dependencies. The exporter runs locally and does not need an AI service or API key.
