# Stage recordings

A file named `<op>.json` replaces the generated output for that stage, so a
response captured from a real backend can be replayed through the same code
path. Nothing here yet.

```jsonc
// reconstruct.json
{
  "artifacts": [{ "kind": "mesh", "format": "ply", "ref": "local://...", "units": "mm" }],
  "measurements": [{ "name": "arch_width_molar", "value": 54.1, "unit": "mm" }],
  "geometry": { "voxel_size_mm": 0.4 }
}
```

Top-level keys are merged into the envelope: `artifacts`, `measurements` and
`warnings` append, anything else replaces.

Record with a real capture. Strip patient identifiers first — dentition geometry
is identifying on its own.
