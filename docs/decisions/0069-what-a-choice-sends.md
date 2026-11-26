# 0069. What a choice sends: runner.values, and FLUX 2 Pro by the megapixel

Date: 2026-11-26. Status: accepted.

## Context

Step 9's second pull request, its second half (docs/plan.md): fal.ai's image models priced in
megapixels or tokens. Peter's live draft of `fal-ai/flux-2-pro` on 26 November showed:
- fal.ai's pricing API bills it at $0.03 a "processed megapixel". Its page charges the first
  megapixel at $0.03 and each more at $0.015, rounded up;
- the draft left out `image_size`. fal.ai's field takes either a preset's name (`square_hd`,
  `landscape_4_3`...) or a width and height, and the draft only knew plain choices. The
  presets are 1 megapixel or less, and FLUX.2 [pro] makes up to 4.

An API definition sends a param's value as it is (decision 0055): a choice sends its text.
A size an artist reads ("2048 x 1152 (16:9)") isn't what fal.ai takes (`{width: 2048, height:
1152}`). GPT Image 2's image_size takes the same two forms.

The same drafts showed that fal.ai describes endpoints that aren't its own (`openai/...`,
`google/...`) in a form the draft tool doesn't read. GPT Image 2 and Nano Banana 2.1 wait for
those descriptions.

## Decision

- **`runner.values`, optional in an API definition's runner block:** `{param: {choice:
  value}}`. A choice param that is sent sends the value for the artist's choice instead of the
  choice's text. Every choice has a value, so a choice can't go as its text by mistake. A value
  is text, a number, true or false, or a list or section of them. It is part of the
  fingerprint, since it changes what is sent; a definition without it keeps the fingerprint it
  had.
- **The draft tool** writes a field that takes a preset's name or something else as a choice
  of the presets. A TODO says what else fal.ai takes, and that sizes beyond the presets are
  choices named for artists, with what each sends under `runner.values`.
- **`flux2_pro_fal`, FLUX.2 [pro] from a prompt** (fal.ai > Image > FLUX 2 Pro), reviewed:
  - six sizes, as pixels and shape, each a multiple of 16 and 4 megapixels at most: 1024 x 576,
    1024 x 1024, 1536 x 1024, 2048 x 864, 2048 x 1152 (the default) and 2048 x 2048;
  - cost: unit `processed-megapixel` at $0.03, by size, the factor being the size's megapixels
    rounded up. That counts every megapixel at $0.03 when fal.ai's page charges those after the
    first at half, so the hold is never under the charge ($0.09 at 2048 x 1152 for $0.06), and
    fal.ai's bill settles it (decision 0061);
  - a prompt and an optional seed; safety tolerance and checker left to fal.ai.
- The stand-in describes FLUX.2 [pro] with fal.ai's image_size (a preset or a width and
  height), makes images that size, and bills them by its page's rule.

## Consequences

- An image model with sizes beyond its presets is a reviewed list of sizes, each with what it
  sends and its cost factor.
- A reviewed definition may name a param for artists (`size`, not `image_size`): the tests
  check the fields it sends against its draft, not the params' names.
- GPT Image 2 and Nano Banana 2.1 come in the next pull request, once the draft tool reads
  fal.ai's descriptions of partner endpoints.
