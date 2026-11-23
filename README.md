# CAD Sketch Agent

Turn a dimensioned description or a reference image into a reproducible 2D CAD sketch and, optionally, a simple 3D solid. Start with a flat pendant, a plain ring band, or a tag. An agent interprets the request and writes a small JSON spec; a deterministic exporter builds the CAD locally. The exporter does not guess geometry from pixels or call a model.

## Try it

Use Python 3.12. Install `requirements.txt` for 2D only, or install `requirements-solid.txt` for both 2D and 3D. The included examples request 3D; remove `thickness_mm` from a copy of an example if you want only 2D.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-solid.txt

python cad_sketch.py build examples/band.json --out out/band
python cad_sketch.py build examples/tag.json --out out/tag
python cad_sketch.py build examples/rounded_pendant.json --out out/rounded-pendant
python -m unittest discover -s tests -v
```

Open `sketch.svg` for a quick preview or `sketch.dxf` to edit the exported 2D geometry. If you specify thickness, import `solid.step` as a CAD solid, or use `solid.stl` as a mesh. To change design parameters and regenerate every export, edit the JSON. DXF and STEP do not recreate a native constrained sketch or a feature tree.

## Use with a CAD agent

The self-contained skill is under [skills/cad-sketch-agent](skills/cad-sketch-agent/SKILL.md). You can also point any agent with file and Python access at the [spec](skills/cad-sketch-agent/references/spec.md). Try:

> Use CAD Sketch Agent to make a plain band: 18 mm inside diameter, 1.75 mm radial wall, and 4 mm axial height. Return the spec, DXF, STEP, and a preview.

The [rounded pendant example](examples/rounded_pendant.json) uses an 18 × 26 mm outline with R3 corners, a 3 mm through-hole centred 4.5 mm below the top edge, and 2 mm thickness. These are chosen example dimensions, not measurements inferred from a photo. This is a flat concept, not a checked production design.

For an image request, supply a square-on photo or line drawing and at least one reference measurement. Identify which feature it measures. A dimensioned description can produce exact geometry within the supported shapes; a partial or perspective photo cannot establish hidden geometry or absolute scale.

## Scope

One circular or simple polygon outline; optional uniform corner radius on strictly convex polygons; optional non-touching circular through-holes and straight extrusion. All dimensions are millimetres. Rounded corners are in the XY outline, not bevels or edge rounds on the top and bottom of the solid. Curved or tapered bands, prongs, filigree, and fitted gemstone settings are unsupported. Valid geometry does not establish durability, fit, or suitability for casting.

## What informed the design

- [GenCAD](https://github.com/ferdous-alam/GenCAD) inspired the split between reading an image and building from explicit CAD operations. This starter does not copy or require GenCAD, its model weights, or CUDA.
- [Awesome Jewelry AI](https://github.com/formanova-ai/awesome-jewelry-ai) informed the jewellery fidelity checks: preserve silhouette, symmetry, feature count, and orientation rather than judging a design by a plausible-looking render.

The `sample-output/` folder contains ready-to-open CAD exports you can open without installing anything. Start with the SVG preview, then load the STEP or DXF in your CAD editor.

## License

[MIT](LICENSE).
