# 0065. fal.ai definitions drafted from fal.ai's own description, through the gateway, and reviewed by hand

Date: 2026-11-25. Status: accepted.

## Context

Phase 11's step 9 is more fal.ai models: "`comfyfarm fal draft <endpoint>` drafts a process.yaml
from fal.ai's schema, and each one is reviewed. Gate: a new model is one reviewed process.yaml
in one pull request." On 24 November Peter asked for fal.ai's endpoints before LoRA training
(step 7). fal.ai lists over a thousand models, most with several endpoints, and the farm only
runs reviewed definitions, each with a reviewed price for the ledger (decisions 0059, 0060).
Peter chose:

- the draft tool, and a reviewed first set of the most-used models, in a few pull requests,
  more later one each;
- for image generation and editing, video generation, and video editing and upscaling.

fal.ai's model API (`GET https://api.fal.ai/v1/models?endpoint_id=...&expand=openapi-3.0`,
read 25 November 2026) describes each endpoint:

- its name, category (text-to-image, image-to-video...), status and licence type (commercial,
  research or private);
- an OpenAPI schema: a POST of its input to the endpoint, and a GET of its output from the
  request, each a `$ref` into the schema's components.

The model API is public, and a key raises its rate limit. The pricing API, which the gateway
already reads (0059), gives each endpoint's unit price.

The gateway's API runner today takes image inputs and brings back images (frames), and a fal.ai
definition needs an input image: it names the job's outputs.

## Decision

- **`comfyfarm fal draft <endpoint>`** prints a draft process.yaml, or writes it into
  `workflows/<name>/` with `--write`, never over a folder that is there. What to review goes to
  stderr: the draft's TODO lines, and what the farm says of the draft as it is.
- **It reads fal.ai as the gateway does:** `gateway.yaml` (`--gateway` for another file), the
  fal adapter, its requests key, and fal.ai itself only with `live: true`, otherwise the
  stand-in (0062). It asks two free questions, never a job:
  - the adapter's new `describe(endpoint)`, the model API with the OpenAPI schema;
  - its prices, as the gateway reads them daily.
- **The draft is written the way the farm's fal.ai definitions are** (`nano_banana2_edit_fal`):
  - each image field (`..._url`, `..._urls`) is an input. The first is `input`, required even
    where fal.ai's is optional, since it names the outputs; a field that takes several also
    gets an optional `reference`;
  - the prompt is a text param; choices, numbers (with their range) and yes/no fields are
    params with fal.ai's defaults, labels and help;
  - fixed, never the artist's: one image a job, PNGs, `sync_mode` (0062), no web search;
  - a model's finer knobs are left to fal.ai's defaults and named in a comment: temperature,
    top_p, thinking, safety levels and checker, system prompt, conversation history and the
    like;
  - an `images` or `image` result is frames;
  - `cost`: the pricing API's price, its unit as one (`images` is `image`), quantity 1 for an
    image, or `{param: duration}` for a price per second with a duration param;
  - `models:` names the endpoint, licence `fal-<licence type>`; the task follows fal.ai's
    category; the name is the endpoint without fal.ai's own owner (`fal-ai/`), in snake case,
    with `_fal`.
- **A draft is not a definition.** What it couldn't decide is a `TODO` line at its top. A price
  it couldn't read is written 0, which the farm refuses, so the draft can't run until someone
  writes the reviewed price. The review checks:
  - the price against the model's page, and what changes it (`cost.by`);
  - what the artist may set, with labels and help for artists;
  - the licence, the task, label, tag and description;
  - its first line, which then says it was reviewed and when.
- **Endpoints the farm can't run yet are still drafted, with a TODO saying why:** one that
  takes no image, one that takes a video or audio, one that returns a video. The following
  pull requests bring those, each with the definitions that need it.
- **The stand-in describes its models** (`/v1/models` with their schemas), so the command and
  its tests run without fal.ai. Its schema is its own check of a job: a definition that sends
  a field fal.ai doesn't have fails there as at fal.ai.
- **The tests take every fal.ai definition alike**, so a new one needs no test changed: the
  menus' tests check the farm's own definitions by name and fal.ai's by rule. Every reviewed
  definition the stand-in knows sends only fields its schema has, and fixes what its draft
  fixes.
- **The first new one: `nano_banana_pro_edit_fal`**, Nano Banana Pro edit, drafted, then
  reviewed against fal.ai's page:
  - $0.15 an image, twice that at 4K (`by: resolution`);
  - the same params as Nano Banana 2 edit, at 1K, 2K or 4K;
  - its licence written `google-via-fal`, as Nano Banana 2's.

## Consequences

- A new fal.ai model the farm can run is a draft, a review, and one pull request. Peter or the
  pipeline team can draft one on the gateway's machine.
- fal.ai's descriptions differ in what they say; a field's help is fal.ai's text until the
  review rewrites it for artists.
- Prices fal.ai gives in tokens, megapixels or compute seconds don't fit the cost block yet:
  the draft says to set its quantity. The pull request for text-to-image settles how they are
  estimated, with the models that need it.
- The order of phase 11 changes: step 9 comes before step 7, as Peter asked. Steps 7 and 8
  follow it.
