# 0068. Jobs with no input image: the Output folder says the show and names the result

Date: 2026-11-26. Status: accepted.

## Context

Step 9's second pull request is image generation (docs/plan.md): fal.ai's models that make an
image from a prompt alone. Every definition so far had an input, and the input did three
things:
- its path gave the job's show, sequence and shot (`paths.context_patterns`). The policy's
  rules and a paid job's cap (decisions 0044, 0058, 0060) see the show;
- its file name named the outputs (`{source}` in `paths.frame_name`);
- its frame numbered them.

Decision 0065 drafted such endpoints with a TODO: "it takes no image: the farm's fal.ai
definitions need an input image to name the job's outputs, until text-only jobs come".

The clients don't send a show: the Nuke node and the dashboard send the params, and the farm
works the show out of the input. A job with no input still has an Output folder, and artists
put it on show storage.

## Decision

- **A definition with no input says its show with its Output folder.** Its `output_folder`
  param is `required: true`; a definition with no input that writes something and has no
  required folder isn't loaded ("no input: needs an output_folder param with required: true,
  which says the job's show"). This holds for every runner, not only fal.ai's.
- **Show, sequence and shot come from the folder's path,** read with `paths.context_patterns`
  as an input's path is. The show and shot a request names (the API's `show`, `shot`) only
  fill what the folder doesn't say. A folder in no show is refused, saying so and giving the
  show's renders folder as an example. The policy and the cap see that show.
- **The result is named after the folder's shot, else its show:** `{source}` is the shot's
  name, else the show's, so `rnd.nanobananagen_v001.1001.png`. The tag says which model made
  it, and each run takes the next free version as before.
- **Frames are numbered from 1001**, the studio's first frame, as for a movie with no
  timecode (`convert.movie.default_start`). Inputs still number their outputs.
- **A shot's folder** holds the frames in a folder named after the shot, as `output_dir`
  says; a folder with no shot holds them itself, with the note the farm already gives.
- **The draft tool** writes the required folder, with help saying it names the show, for an
  endpoint that takes no image, and no longer a TODO for it.
- **The first three image definitions,** reviewed against fal.ai's pages:
  - `nano_banana2_fal`, Nano Banana 2 from a prompt: $0.08 at 1K, 0.75× at 0.5K, 1.5× at 2K,
    2× at 4K, as its edit; web and image search, and high thinking, left off;
  - `nano_banana_pro_fal`, Nano Banana Pro from a prompt: $0.15, 2× at 4K; web search off;
  - `birefnet_v2_fal`, BiRefNet background removal (an input, under Masking): $0.000575 a
    compute second, from fal.ai's pricing API as Peter's draft read it on 25 November (its
    page rounds it to $0). A job is held at 20 compute seconds, more than one image takes,
    and fal.ai's bill settles it (decision 0061). Its input goes up at its own size, so the
    matte lines up with the plate; one image comes back (`output_mask` off).
  Each offers aspect ratio, resolution and an optional seed; the edits' finer knobs stay
  fal.ai's defaults.
- Money under a cent is shown to the millionth the ledger keeps ($0.000575), not rounded to
  four places, in the manager, the dashboard and Nuke.

## Consequences

- An image from a prompt is a Nuke node with no input arrow, or a dashboard form, with a
  prompt and an Output folder.
- FLUX 2 Pro, GPT Image 2 and Nano Banana 2.1 are priced in megapixels or tokens, which one
  price per unit doesn't estimate. They come in the next pull request, from live drafts that
  show what fal.ai's pricing API says of them.
- Video from a prompt (step 9's third pull request) takes the same route.
