---
name: cad-sketch-agent
description: Turn an image, hand sketch, or dimensioned description into a reproducible millimetre CAD sketch (SVG and DXF) or a simple constant-thickness solid (STEP and STL). Use for simple rings, flat pendants with optional rounded corners, tags, templates, and plates; not photorealistic renders or complex stone settings.
---

# CAD Sketch Agent

1. Separate **observed shape**, **confirmed dimensions**, and **assumptions**. For an image, identify the visible circles, straight edges, corner rounds, symmetries, and holes rather than guessing the hidden geometry. For an accurate traced sketch, use a square-on view and at least one known dimension. Do not derive millimetres from an unscaled or perspective image. If the user only wants a concept, choose and label provisional sizes; ask for a necessary dimension only if the user wants an exact fit or a production handoff.
2. Write a JSON spec using [the spec format](references/spec.md). Use an XY plane in millimetres, one closed circular or polygon outline, and optional interior circular through-holes. For a strictly convex polygon, `outline.corner_radius` rounds all corners with the same planar radius. Use a concentric hole for a simple ring band. Set a thickness only if the user wants a simple solid. Do not fake unsupported curves, separate solids, prongs, lofts, countersinks, or gemstone seats; flag them and explain what would require a different CAD workflow.
For fidelity checks when tracing jewellery, use [design notes](references/design-notes.md).

3. Run the bundled script from a workspace you can write to, with an empty output directory. From the skill directory:

   ```bash
   python scripts/cad_sketch.py build path/to/spec.json --out path/to/result
   ```

   Use an environment with ezdxf for sketches and CadQuery for optional solids. The tested versions are listed in [the spec format](references/spec.md). If dependencies are unavailable, provide the editable JSON and say which exports you could not generate.
4. Check the report, confirm that the generated files exist, and view the SVG when possible. Verify critical sizes against the request, including hole diameter and centre placement, a rounded pendant’s overall size and corner radius, and a concentric ring’s radial wall thickness. Distinguish a through-hole for a jump ring from a fitted gemstone seat, which the exporter does not support. If a supplied feature doesn't fit, report the geometry error and propose a correction instead of silently altering a specified dimension.
5. Deliver the spec alongside the DXF and an SVG preview, plus STEP/STL if generated. Name unresolved measurements and label assumptions. Describe solids as concept geometry unless manufacturing and fit have been separately verified. Use the JSON to edit source dimensions and regenerate outputs. DXF offers editable curves and lines, not a fully constrained native CAD sketch or feature history.
