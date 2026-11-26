# 0070. Licences cleared for production: every model listed, the rest on rnd only

Date: 2026-11-26. Status: accepted.

## Context

Rodeo FX has over $100M of yearly revenue. A model whose licence caps revenue, forbids
commercial use, or needs a paid licence above some size can't be used on a client's show
unless Rodeo holds that licence. The project's masking licence check (26 November 2026, in the
project's docs) found:
- LTX-2.x's community licence needs a paid commercial licence from Lightricks for a company
  with $10M or more of yearly revenue. That covers the farm's three LTX workflows
  (`ltx25_alpha_gen`, `ltx25_layout_render`, `sam3_ltx_alpha`), which Peter turned off on 25
  November (decision 0066);
- ViTMatte's Composition-1k weights are labelled Apache-2.0 but trained on Adobe's Deep Image
  Matting set, whose terms are non-commercial: not cleared. `sam3_ltx_alpha` loads them.

CHORD's weights are the Ubisoft Machine Learning License, research-only. `chord_pbr` lists
them already (decision 0044).

The farm's policy has had licence rules since phase 7 (0044): a rule allows or denies the
licences of the models a definition lists under `models:`, on the shows it names. Only CHORD,
the GPU test and the fal.ai definitions listed theirs; Wan 2.2 and the three LTX workflows
didn't.

Peter, on 26 November 2026:
- "Drop any model that requires licencing. For example LTX requires licence even though we
  run it on API. So drop it. If a model grants a licence by running it via API, we can use it
  in production."
- CHORD "can only run on rnd project, not on production projects".

## Decision

- **Every definition lists its models with their licences.** `models: []` for one that loads
  none. Added:
  - `wan22_i2i`: Wan 2.2, its VAE and umT5, Apache-2.0;
  - the LTX workflows: LTX-2.5's weights, `ltx-2-community-license`. The 12B Gemma 4 text
    encoder comes with LTX's projection, so it is LTX's; the small Gemma 4 one is Apache-2.0;
  - `sam3_ltx_alpha`: SAM 3 (`sam-license`) and ViTMatte
    (`apache-2.0-noncommercial-training-data`).
- **Rodeo's rules, in `policy.example.yaml` for the bank's `policy.yaml`:**
  - `production-cleared-licences`: every show but `rnd` allows only cleared licences:
    `apache-2.0`, `mit`, `sam-license`, and the licences of models used through a provider's
    API, which come with that use (`google-via-fal`, `fal-commercial`);
  - `no-licence-rodeo-lacks`: no show, `rnd` included, may use `ltx-2-community-license`;
  - `unlisted_models: deny`.
  So CHORD and anything else not cleared runs on `rnd` only, LTX on no show, and a licence
  nobody has looked at yet is refused on production until a rule names it.
- **A test keeps the repo to it** (`tests/test_licences.py`):
  - every definition lists its models;
  - every licence a definition carries is named by one of the example's rules, or is one of
    the rnd-only ones;
  - the rules refuse and allow as Peter said.
- **Listing models changes a definition's fingerprint** (0044: head-only fields count when
  set): Wan 2.2's and the LTX workflows' change. The manager and the agents update together,
  as for any definition change.
- No new code: the policy's licence rules do it. The bank's `policy.yaml` is Peter's to edit.

## Consequences

- With the rules in the bank's `policy.yaml`, CHORD is refused on production shows with the
  rule and the licence named, and runs on `rnd`. LTX is refused everywhere, as well as being
  turned off.
- A new definition, local or at a provider, names its models' licences, and a licence the
  rules don't name yet needs a decision, recorded in a rule, before it reaches production.
- The licences of models used through an API are the provider's licence types for them. A
  model whose maker's terms need a separate licence even through an API (LTX) goes in
  `no-licence-rodeo-lacks`.
- The masking menu (the plan's next item) adds models whose licences are already cleared:
  SAM 3.1, BiRefNet (MIT) and MultiMatte.
