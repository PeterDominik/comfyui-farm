"""ComfyFarm Pro Alpha: the Comfy Pro Alpha Generator run from one word instead of a click.

The generator (ComfyUI-Comfy-Pro-Alpha-Generator, Rikin Patel) mattes one subject of a
plate: SAM3 tracks the subject and everything else, LTX-2.5 alpha-gen draws the edges.
It is built around its picker: you load the plate in the node, type a word or click the
subject, and the picker writes `picks`, a JSON with the subject's box on one frame and
the boxes of everything else to paint out. On the farm nobody clicks, so this node does
what the picker does when a word is typed:
  1. SAM3 finds the subject word on one frame (the pick frame: the first, or the frame
     number the artist gives), with the same detection graph the picker queues.
  2. The detection SAM3 scores highest is the subject. The others (the nearest 8) are
     excluded: painted out of the plate LTX sees, so it mattes the subject alone.
  3. The unmodified generator mattes the whole shot with those picks, tracking the subject
     forward and backward from the pick frame.
Steps 2 and 3 are added to the graph when this node runs. ComfyUI checks every node's
inputs before a prompt starts, and the generator refuses empty picks then; nodes added by
a node at run time aren't checked that way, so the picks only have to exist by then.

Fixed here, as the farm runs it: Best quality (two LTX passes), the full shot, 24 fps,
alpha only (edge colour off), no native-resolution edges (a plate bigger than 1920x1088
is matted at that size and the alpha is scaled up to the plate's).

SAM3 and ViTMatte read their models from fixed places in ComfyUI's own models folder and
would download them from Hugging Face when missing; this node stops first, saying where
they go. The generator also writes its own copy of the alpha into ComfyUI's output
folder: each run clears the previous run's copy and the generator's temp files.
"""
from __future__ import annotations

import json
import math
import os
import shutil

# The generator's node and the values it takes (ComfyUI-Comfy-Pro-Alpha-Generator v0.6.0).
GENERATOR = "ComfyProAlphaGenerator"
BEST = "Best (v12, 2 LTX passes)"
BUILTIN_PROMPT = "built-in empty prompt (no text encoder needed)"
OWN_OUTPUT = "comfyfarm_pro_alpha"              # the generator's own copy, under ComfyUI's output folder
TEMP_DIRS = ("comfy_pro_alpha_seq", "comfy_pro_alpha_preview", "comfy_pro_alpha_detect", "comfy_pro_alpha_plate")
MAX_OTHERS = 8                                  # as the picker: each excluded subject costs a SAM3 track
MIN_PIXELS = 50                                 # as the picker: smaller detections are noise
SAM3_MODEL = ("sam3", "sam3.safetensors")       # under ComfyUI's folder: models/sam3/sam3.safetensors
VITMATTE = ("vitmatte", "vitmatte-base-composition-1k")   # under models/, a Hugging Face folder

# Defaults for the model inputs: the files the farm's libraries hold.
MODELS = {
    "ltx_model": ("diffusion_models", "ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors"),
    "alpha_lora": ("loras", "LTX/ltx-2.5-22b-ic-lora-alpha-gen-0.9.safetensors"),
    "video_vae": ("vae", "ltx-2.5-video-vae-conv-bf16.safetensors"),
    "audio_vae": ("vae", "ltx-2.5-audio-vae-bf16.safetensors"),
}


class ProAlphaError(ValueError):
    """Something the artist or the machine's setup has to fix: shown as the job's error."""


# ------------------------------------------------------------------ pure helpers (tested without ComfyUI)

def parse_frame_numbers(text: str | None) -> list[int]:
    """'1001,1002,1011' (ComfyFarm Load Images' frame_numbers) as numbers; [] when unknown."""
    out = []
    for part in (text or "").replace("\n", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            return []                   # a file without a frame number: no numbering to go by
    return out


def pick_index(pick_frame: int | None, frame_numbers: str | None, count: int) -> tuple[int, str]:
    """The batch index of the pick frame, and how to name that frame to the artist.
    pick_frame below 0 (or None) is the first frame. With the input's frame numbers it is
    a frame number (1050); without them, a position counted from 0."""
    nums = parse_frame_numbers(frame_numbers)
    if len(nums) != count:
        nums = []
    if pick_frame is None or int(pick_frame) < 0:
        return 0, f"frame {nums[0]}" if nums else "the first frame"
    pick_frame = int(pick_frame)
    if nums:
        if pick_frame not in nums:
            span = f"{nums[0]}-{nums[-1]}" if len(nums) > 1 else str(nums[0])
            raise ProAlphaError(f"Pick frame {pick_frame} isn't one of the input's frames ({span}). Give a frame "
                                f"where the subject is clearly visible, or leave it empty for the first frame.")
        return nums.index(pick_frame), f"frame {pick_frame}"
    if pick_frame >= count:
        raise ProAlphaError(f"Pick frame {pick_frame} is past the input's last image (it has {count}, counted "
                            f"from 0).")
    return pick_frame, f"image {pick_frame}"


def _as_numpy(x):
    if hasattr(x, "detach"):
        x = x.detach().float().cpu().numpy()
    import numpy as np
    return np.asarray(x, dtype=np.float32)


def detections(masks) -> list[dict]:
    """SAM3's masks on the pick frame as the picker sees them: box (centre x, centre y,
    width, height, all 0-1), area (share of the frame) and the mask's place in SAM3's list.
    Masks under MIN_PIXELS pixels are left out (SAM3's empty result is one blank mask)."""
    import numpy as np
    m = _as_numpy(masks)
    if m.ndim == 2:
        m = m[None]
    h, w = m.shape[1:]
    out = []
    for i, mk in enumerate(m):
        ys, xs = np.where(mk > 0.5)
        if len(ys) < MIN_PIXELS:
            continue
        x0, x1, y0, y1 = xs.min() / w, (xs.max() + 1) / w, ys.min() / h, (ys.max() + 1) / h
        out.append({"index": i, "box": [float((x0 + x1) / 2), float((y0 + y1) / 2), float(x1 - x0), float(y1 - y0)],
                    "area": float((mk > 0.5).mean())})
    return out


def parse_scores(text: str | None) -> list[float]:
    """SAM3 Text Segmentation's scores output (a JSON list, one per mask)."""
    try:
        data = json.loads(text or "[]")
    except ValueError:
        return []
    out = []
    for s in data if isinstance(data, list) else []:
        while isinstance(s, list) and len(s) == 1:
            s = s[0]
        if not isinstance(s, (int, float)) or isinstance(s, bool):
            return []
        out.append(float(s))
    return out


def choose(dets: list[dict], scores: list[float], mask_count: int) -> tuple[dict, list[dict]]:
    """The subject (the detection SAM3 scores highest; SAM3 lists them best first, so the
    first one when there are no scores) and the others to exclude, nearest first."""
    if not dets:
        raise ProAlphaError("nothing found")
    usable = len(scores) == mask_count
    for d in dets:
        d["score"] = scores[d["index"]] if usable else None
    target = max(dets, key=lambda d: (d["score"] if usable else 0.0, -d["index"]))
    tx, ty = target["box"][:2]
    others = sorted((d for d in dets if d is not target),
                    key=lambda d: math.hypot(d["box"][0] - tx, d["box"][1] - ty))[:MAX_OTHERS]
    return target, others


def make_picks(target: dict, others: list[dict], frame: int, subject: str) -> dict:
    """The generator's picks, as its picker writes them after a detection is clicked."""
    return {"target": {"id": 0, "frame": int(frame), "box": target["box"]},
            "others": [{"id": i + 1, "frame": int(frame), "box": o["box"]} for i, o in enumerate(others)],
            "cuts": [], "adds": [], "prompt": subject}


def generator_settings(seed: int, sam3_threshold: float, ltx_model: str, alpha_lora: str, video_vae: str,
                       audio_vae: str) -> dict:
    """Every input of the generator but images and picks, as the farm runs it."""
    return {"fps": 24.0, "first_frame": 1, "quality": BEST, "render": "full shot",
            "filename_prefix": f"{OWN_OUTPUT}/alpha", "format": "png16",
            "ltx_model": ltx_model, "alpha_lora": alpha_lora, "text_encoder": BUILTIN_PROMPT,
            "video_vae": video_vae, "audio_vae": audio_vae, "vitmatte": "auto-download",
            "chunk": 49, "overlap": 9, "seed": int(seed), "erode": 10, "dilate": 15, "near_px": 15,
            "stabilize_radius": 2, "sam3_threshold": float(sam3_threshold), "fast_attention": True,
            "overlap_mode": False, "native_edges": False, "edge_colour": "off", "track_backward": True,
            "skip_sam3": False}


def missing_models(comfy_dir: str, models_dir: str) -> list[str]:
    """SAM3's and ViTMatte's models where their node packs read them (they download them
    from Hugging Face when missing, which a farm machine shouldn't do on its own)."""
    sam3 = os.path.join(comfy_dir, "models", *SAM3_MODEL)
    vit = os.path.join(models_dir, *VITMATTE)
    out = []
    if not os.path.isfile(sam3):
        out.append(f"SAM3's model: {sam3} (3.5 GB, from Hugging Face apozz/sam3-safetensors)")
    if not os.path.isfile(os.path.join(vit, "config.json")):
        out.append(f"ViTMatte: the folder {vit} (0.4 GB, from Hugging Face hustvl/vitmatte-base-composition-1k)")
    return out


def clear_previous(output_dir: str, temp_dir: str) -> None:
    """The previous run's copy of the alpha and the generator's temp files (proxy movie of
    the plate, previews): a farm ComfyUI runs one job at a time, so they are done with."""
    shutil.rmtree(os.path.join(output_dir, OWN_OUTPUT), ignore_errors=True)
    for d in TEMP_DIRS:
        shutil.rmtree(os.path.join(temp_dir, d), ignore_errors=True)


def _model_list(widget: str) -> list[str]:
    folder, default = MODELS[widget]
    try:
        import folder_paths
        names = list(folder_paths.get_filename_list(folder))
    except Exception:  # noqa: BLE001 - outside ComfyUI (tests), or a folder ComfyUI doesn't know
        names = []
    same = [n for n in names if n.replace("\\", "/") == default]
    return same + [n for n in names if n not in same] if same else [default] + names


def _images_link(dynprompt, unique_id):
    """What feeds this node's images: passed on to the nodes it adds, so the plate is read
    once and never copied."""
    return dynprompt.get_node(unique_id)["inputs"]["images"]


# ------------------------------------------------------------------ nodes

class ComfyFarmProAlpha:
    """Matte one subject, named by a word: SAM3 picks it, LTX-2.5 alpha-gen draws its edges
    (Comfy Pro Alpha Generator, run without its picker). Returns the alpha of every frame."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "subject": ("STRING", {"default": "person", "tooltip": (
                    "What to matte, as SAM3 knows it: person, car, dog, red car. When it finds several, the one "
                    "it scores highest is the subject; the others are painted out.")}),
                "pick_frame": ("INT", {"default": -1, "min": -1, "max": 10 ** 7, "tooltip": (
                    "The frame SAM3 looks for the subject on: a frame number of the input (with frame_numbers "
                    "connected), else a position from 0. -1 = the first frame. The subject is tracked forward "
                    "and backward from it, so any frame where it is clearly visible works.")}),
                "seed": ("INT", {"default": 42, "min": 0, "max": 2 ** 31 - 1}),
                "sam3_threshold": ("FLOAT", {"default": 0.3, "min": 0.0, "max": 1.0, "step": 0.01, "tooltip": (
                    "SAM3's lowest score for a detection, on the pick frame and when tracking.")}),
                "ltx_model": (_model_list("ltx_model"),),
                "alpha_lora": (_model_list("alpha_lora"),),
                "video_vae": (_model_list("video_vae"), {"tooltip": "LTX-2.5's conv video VAE: only it can "
                                                                    "encode the plate that guides alpha-gen."}),
                "audio_vae": (_model_list("audio_vae"),),
            },
            "optional": {
                "frame_numbers": ("STRING", {"forceInput": True, "tooltip": (
                    "ComfyFarm Load Images' frame_numbers: makes pick_frame a frame number.")}),
            },
            "hidden": {"unique_id": "UNIQUE_ID", "dynprompt": "DYNPROMPT"},
        }

    RETURN_TYPES = ("MASK", "STRING")
    RETURN_NAMES = ("alpha", "picks")
    FUNCTION = "run"
    CATEGORY = "ComfyFarm"
    DESCRIPTION = ("Mattes one subject named by a word: SAM3 finds it on the pick frame (the highest score wins, "
                   "the others are painted out), the Comfy Pro Alpha Generator draws its edges with LTX-2.5 "
                   "alpha-gen over the whole shot. Needs ComfyUI-Comfy-Pro-Alpha-Generator and ComfyUI-SAM3.")

    def run(self, images, subject, pick_frame, seed, sam3_threshold, ltx_model, alpha_lora, video_vae, audio_vae,
            frame_numbers=None, unique_id=None, dynprompt=None):
        import folder_paths
        from comfy_execution.graph_utils import GraphBuilder

        subject = (subject or "").strip()
        if not subject:
            raise ProAlphaError("No subject: say what to matte (person, car, dog).")
        missing = missing_models(folder_paths.base_path, folder_paths.models_dir)
        if missing:
            raise ProAlphaError("Missing on this machine: " + "; ".join(missing) + ". Copy them there from the farm "
                                "library (/rdo/bank/comfy_farm/comfyui/models/...), or link the folders.")
        index, label = pick_index(pick_frame, frame_numbers, len(images))
        clear_previous(folder_paths.get_output_directory(), folder_paths.get_temp_directory())

        src = _images_link(dynprompt, unique_id)
        settings = generator_settings(seed, sam3_threshold, ltx_model, alpha_lora, video_vae, audio_vae)
        g = GraphBuilder()
        frame = g.node("ComfyProAlpha_Frame", images=src, index=index)
        sam = g.node("LoadSAM3Model", precision="auto")
        found = g.node("SAM3Grounding", sam3_model_config=sam.out(0), image=frame.out(0),
                       confidence_threshold=float(sam3_threshold), text_prompt=subject, max_detections=-1)
        pick = g.node("ComfyFarmProAlphaPick", images=src, masks=found.out(0), scores=found.out(3),
                      subject=subject, frame=index, frame_label=label, settings=json.dumps(settings))
        return {"result": (pick.out(0), pick.out(1)), "expand": g.finalize()}


class ComfyFarmProAlphaPick:
    """ComfyFarm Pro Alpha's second step (added by it, not placed by hand): SAM3's
    detections on the pick frame become the generator's picks, and the generator runs."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
                "masks": ("MASK",),
                "scores": ("STRING", {"default": "[]"}),
                "subject": ("STRING", {"default": ""}),
                "frame": ("INT", {"default": 0, "min": 0, "max": 10 ** 7}),
                "frame_label": ("STRING", {"default": ""}),
                "settings": ("STRING", {"default": "{}"}),
            },
            "hidden": {"unique_id": "UNIQUE_ID", "dynprompt": "DYNPROMPT"},
        }

    RETURN_TYPES = ("MASK", "STRING")
    RETURN_NAMES = ("alpha", "picks")
    FUNCTION = "run"
    CATEGORY = "ComfyFarm/internal"
    DESCRIPTION = "Used by ComfyFarm Pro Alpha: turns SAM3's detections into the generator's picks."

    def run(self, images, masks, scores, subject, frame, frame_label, settings, unique_id=None, dynprompt=None):
        from comfy_execution.graph_utils import GraphBuilder

        m = _as_numpy(masks)
        m = m[None] if m.ndim == 2 else m
        dets = detections(m)
        where = frame_label or f"image {frame}"
        if not dets:
            raise ProAlphaError(f"SAM3 found no '{subject}' on {where}. Try another word (SAM3 knows everyday "
                                f"names: person, car, dog, red car), or a pick frame where it is clearly visible.")
        target, others = choose(dets, parse_scores(scores), len(m))
        picks = make_picks(target, others, frame, subject)
        score = "" if target["score"] is None else f", score {target['score']:.2f}"
        print(f"[ComfyFarm Pro Alpha] '{subject}' on {where}: {len(dets)} found; matting the best{score}, "
              f"excluding {len(others)}", flush=True)
        g = GraphBuilder()
        gen = g.node(GENERATOR, images=_images_link(dynprompt, unique_id), picks=json.dumps(picks),
                     **json.loads(settings))
        info = json.dumps({**picks, "found": len(dets), "score": target["score"], "frame_label": where})
        return {"result": (gen.out(0), info), "expand": g.finalize()}
