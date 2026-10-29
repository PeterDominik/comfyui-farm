# The farm's OCIO config

The ComfyUI farm does two colour conversions, and this folder holds just enough to do them
exactly as Nuke does:

| What | From | To |
|---|---|---|
| EXR inputs (plates, textures) → 8-bit PNG for ComfyUI | `scene_linear` (`acescg`) | `color_picking` (`comfy_png`) |
| Texture colour maps from ComfyUI → EXR | `color_picking` (`comfy_png`) | `scene_linear` (`acescg`) |
| Texture data maps (normal, bump, ...) | values kept, tagged `data` (`raw`) | |

These are a Nuke Write and Read set to `default (color_picking)` in Rodeo's published show
config.

## Where it comes from

`reference/config_1_0_v1.ocio` is the config Nuke loads for bri3:
`/rdo/shows/bri3/.published/ocioConfig/config_1_0/v1/config_1_0_v1.ocio`. Nuke resolves
its LUTs from the Rez package `rdo_ocio_config` 2.6.3. In it:

- **`scene_linear` is `rdo`:** ACEScg with the shot's white balance applied.
- **`color_picking` is `ACES-look`:** the ACES 1.x RRT + Rec.709 output transform, with the
  white balance undone. The display is called `sRGB-monitor`, but it is set to Rec.709.

The white balance is applied and removed again in both directions, so it cancels for any
shot. `config.ocio` leaves it out. What remains:

- **`acescg`:** the ACEScg → ACES2065-1 matrix.
- **`comfy_png`:** the Rec.709 output transform. It uses three files in `luts/`:
  - `Log2_48_nits_Shaper_to_linear.spi1d`
  - `Log2_48_nits_Shaper.RRT.Rec.709.spi3d` (the way out)
  - `InvRRT.Rec.709.Log2_48_nits_Shaper.spi3d` (the way back)

The old names still work: `rdo`, `ACES-look`, `ACEScg` and `Utility - Raw` are aliases, and
`linear` is a role, as in Rodeo's config.

## Checked against Nuke

Linear grey through `scene_linear` → `color_picking`, measured in Nuke 16 with
`config_1_0_v1`:

| Linear | 0.02 | 0.05 | 0.18 | 0.5 | 1.0 |
|---|---|---|---|---|---|
| PNG code | 23 | 43 | 99 | 170 | 209 |

`tests/test_ocio_repo.py` checks two things:
- this config gives those values;
- it matches `reference/config_1_0_v1.ocio` exactly, in both directions, even with a
  non-identity white balance.

On the farm machine:
- **`python -m comfyfarm check`** runs the same check.
- **`python tools\ocio_compare.py`** compares the farm's oiiotool with OpenColorIO pixel for
  pixel. Pass `--exr X.exr --nuke-png X.png` to check against a PNG Nuke wrote.

## Things to know

- **Bright colour maps come back above 1.0.** The output transform puts linear 1.0 at
  PNG 209. On the way back, 220 is 1.32, 240 is 3.0 and 255 is 16.3. So a ComfyUI colour
  map brighter than 209 gives an EXR above 1.0.
- **If the published config changes `ACES-look`:**
  1. Copy the new config into `reference/`.
  2. Copy the files its `ACES-look` uses into `luts/`.
  3. Update `comfy_png` here.
  4. Measure the ramp in Nuke again, and update it here and in `comfyfarm/convert.py`
     (`NUKE_RAMP`).
- **Nuke can load `config.ocio`.** It is a regular OCIO v2 config, so you can compare by eye:
  a Write with `default (color_picking)` gives the farm's PNG.
