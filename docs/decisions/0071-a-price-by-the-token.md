# 0071. A price by the token: the reviewed one, never fal.ai's "$1 a unit"

Date: 2026-11-27. Status: accepted.

## Context

Step 9's last image models are GPT Image 2 (`openai/gpt-image-2`, and its edit) and Nano
Banana 2.1 (`google/nano-banana-2.1`), the first in the order Peter agreed on 26 November.
Peter's live drafts on 27 November drew them, and showed that for all three fal.ai's pricing
API gives `unit_price: 1, unit: "unit"`. fal.ai bills them by the token, and its pages say so:
- GPT Image 2: $5 a million text input tokens, $8 a million image input tokens, $30 a million
  image output tokens. "Changing the quality parameter significantly affects cost; by default
  we use high." A high-quality 1024 x 1024 is 7,024 image tokens, $0.21 (OpenAI's figures, read
  9 October 2026);
- Nano Banana 2.1: $35.294 a million image output tokens and $8.823 a million thinking tokens.
  An image is 1,120, 1,680 or 3,780 image tokens at 1K, 2K or 4K (Google's figures, 12 October
  2026): $0.040, $0.059 and $0.133 at fal.ai.

The farm estimates a paid job from the provider's price that day (decision 0059), and refuses
one whose provider bills in another unit than its definition's: "needs reviewing". A
definition priced per image would be refused ("now bills per unit"). One priced per "unit" at
$1 would be held at $1 a job.

## Decision

- **`cost.provider_price: false`** says the provider's pricing doesn't price a job of this
  definition. The estimate is the reviewed price, and the text says why ("the price its
  definition was reviewed at, by the token; fal.ai's pricing doesn't price a job"). Default
  true. It is true or false, nothing else.
- **The reviewed price is worked out from the model's page and its token counts, and never
  under what is charged.** It is held against the show's cap as any other, and fal.ai's bill
  settles it (decision 0061).
- **The draft tool** sees fal.ai's "$1 a unit" and writes `provider_price: false` with price
  0, which the farm refuses until someone writes it. A TODO says to work out a job's price from
  the model's page.
- **The three definitions,** reviewed:
  - `gpt_image2_fal`, GPT Image 2 from a prompt: six sizes, as runner.values (0069), from 1024
    x 1024 to 3840 x 2160; quality low, medium or high (the default); background auto, opaque
    or transparent. $0.22 an image at 1024 x 1024, by size in proportion to its area, so $0.495
    at 2048 x 1152 and $1.76 at 3840 x 2160. Low and medium are held at the high price;
  - `gpt_image2_edit_fal`, its edit: an input, an optional reference and an optional mask, the
    size the input's ("auto", the default) or one of the six. $0.30: the result's $0.22 and
    $0.08 for the images sent. "Same as the input" is held as 2048 x 2048, the largest an input
    is sent at (work_resolution 2048);
  - `nano_banana2_1_fal`, Nano Banana 2.1 from a prompt: $0.06 at 1K, 1.4× at 2K, 2.7× at 4K,
    each over the image's tokens to leave room for its thinking (fal.ai's default, medium).
    Web search fixed off.
  Their licences are fal.ai's licence types for them: their use through the API is licensed
  (0070).
- The stand-in prices them at "$1 a unit", as fal.ai does, and bills their tokens as fal.ai's
  pages price them, so the tests check a hold is never under its charge.

## Consequences

- Step 9's image models are in: nine at fal.ai, each reviewed.
- A token-billed model's price is the review's, not fal.ai's that day, so a change at fal.ai
  isn't seen until the bill shows it: the ledger's reconcile against fal.ai's bill (decision
  0061, nightly or `comfyfarm spend reconcile`) is where it shows.
- If fal.ai's pricing API starts to price them per image, the definition can drop
  `provider_price: false` and take that unit in a review.
