"""A stand-in for fal.ai, for the gateway with no fal.ai account and no money (decision 0056).

It speaks the parts of fal.ai's API the gateway uses: the upload token and the CDN upload,
the queue (submit, status, result, cancel), the result files, the pricing API, the bill and
balance (decision 0061), and the model API's description of each of its models, with an
OpenAPI schema for `comfyfarm fal draft` (decision 0065), all on one address. It checks the
keys the way fal.ai does, and runs its models in a few seconds, making grey PNGs.

    python tools/mock_fal.py --gateway gateway.yaml [--queue-seconds 1] [--run-seconds 3] [--bill]
    python tools/mock_fal.py --port 8189 --key-file <a key file> [--billing-key-file <another>]

With --gateway it answers where gateway.yaml sends fal.ai's jobs (providers.fal.url, e.g.
http://127.0.0.1:8189) and takes the keys in the files providers.fal.keys names: requests,
and billing for the bill (deploy/linux/farmctl.sh start mock_fal does this). Make the keys
up (any words): the stand-in never sees a real one, and it answers on 127.0.0.1 only.

Its bill charges each job that finished well when it finished, and nothing for one that
failed or was cancelled. It takes no money, so its bill says $0 for each, unless --bill:
then each is charged at its price (PRICES, by resolution), as fal.ai would, and taken from
its balance (--balance, $50).

It is stricter than fal.ai: a field its model doesn't know is refused, so a typo in a
definition shows here. Its models (PNG only): Nano Banana 2 and Nano Banana Pro, each to make an
image (fal-ai/nano-banana-2, fal-ai/nano-banana-pro) and to edit one (.../edit), FLUX.2 [pro]
(fal-ai/flux-2-pro), priced by the megapixel, BiRefNet (fal-ai/birefnet/v2), by the compute
second, and, described but not priced yet, GPT Image 2 (openai/gpt-image-2, and its /edit) and
Nano Banana 2.1 (google/nano-banana-2.1). To make a job go
wrong, put in the prompt: MOCK_FAIL (the model fails), MOCK_POLICY (the content check
refuses it), MOCK_SLOW (it runs a minute).

The tests use it in memory: MockFal(key).transport() is an httpx transport."""

from __future__ import annotations

import argparse
import base64
import json
import math
import re
import struct
import sys
import threading
import time
import uuid
import zlib
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ENUM = "enum"
SIZE = "size"           # one of these presets, or {width, height} (fal.ai's image_size)
PRESETS = {"square_hd": (1024, 1024), "square": (512, 512), "portrait_4_3": (768, 1024),
           "portrait_16_9": (576, 1024), "landscape_4_3": (1024, 768), "landscape_16_9": (1024, 576)}
MODELS = {
    "fal-ai/nano-banana-2/edit": {
        "prompt": (str, True), "image_urls": (list, True), "num_images": (int, False),
        "seed": (int, False), "sync_mode": (bool, False), "enable_web_search": (bool, False),
        "enable_image_search": (bool, False), "limit_generations": (bool, False),
        "aspect_ratio": ((ENUM, ("auto", "21:9", "16:9", "3:2", "4:3", "5:4", "1:1", "4:5", "3:4", "2:3", "9:16",
                                 "4:1", "1:4", "8:1", "1:8")), False),
        "output_format": ((ENUM, ("jpeg", "png", "webp")), False),
        "resolution": ((ENUM, ("0.5K", "1K", "2K", "4K")), False),
        "safety_tolerance": ((ENUM, ("1", "2", "3", "4", "5", "6")), False),
    },
    # As fal.ai's model API describes it (25 November 2026): its image_urls are optional there.
    "fal-ai/nano-banana-pro/edit": {
        "prompt": (str, True), "image_urls": (list, False), "num_images": (int, False), "seed": (int, False),
        "aspect_ratio": ((ENUM, ("auto", "21:9", "16:9", "3:2", "4:3", "5:4", "1:1", "4:5", "3:4", "2:3", "9:16",
                                 "4:1", "1:4", "8:1", "1:8")), False),
        "output_format": ((ENUM, ("jpeg", "png", "webp")), False),
        "safety_tolerance": ((ENUM, ("1", "2", "3", "4", "5", "6")), False),
        "sync_mode": (bool, False), "system_prompt": (str, False), "temperature": (float, False),
        "resolution": ((ENUM, ("1K", "2K", "4K")), False), "limit_generations": (bool, False),
        "enable_web_search": (bool, False),
    },
    # Text to image (fal.ai's pages, 26 November 2026), the plain fields: their conversation
    # history, context parts and the like are left out.
    "fal-ai/nano-banana-2": {
        "prompt": (str, True), "num_images": (int, False), "seed": (int, False),
        "aspect_ratio": ((ENUM, ("auto", "21:9", "16:9", "3:2", "4:3", "5:4", "1:1", "4:5", "3:4", "2:3", "9:16",
                                 "4:1", "1:4", "8:1", "1:8")), False),
        "output_format": ((ENUM, ("jpeg", "png", "webp")), False),
        "safety_tolerance": ((ENUM, ("1", "2", "3", "4", "5", "6")), False),
        "sync_mode": (bool, False), "system_prompt": (str, False), "temperature": (float, False),
        "top_p": (float, False), "thinking_level": ((ENUM, ("minimal", "high")), False),
        "resolution": ((ENUM, ("0.5K", "1K", "2K", "4K")), False), "limit_generations": (bool, False),
        "enable_web_search": (bool, False), "enable_image_search": (bool, False),
    },
    "fal-ai/nano-banana-pro": {
        "prompt": (str, True), "num_images": (int, False), "seed": (int, False),
        "aspect_ratio": ((ENUM, ("auto", "21:9", "16:9", "3:2", "4:3", "5:4", "1:1", "4:5", "3:4", "2:3", "9:16",
                                 "4:1", "1:4", "8:1", "1:8")), False),
        "output_format": ((ENUM, ("jpeg", "png", "webp")), False),
        "safety_tolerance": ((ENUM, ("1", "2", "3", "4", "5", "6")), False),
        "sync_mode": (bool, False), "system_prompt": (str, False), "temperature": (float, False),
        "top_p": (float, False), "thinking_level": ((ENUM, ("high",)), False),
        "resolution": ((ENUM, ("1K", "2K", "4K")), False), "limit_generations": (bool, False),
        "enable_web_search": (bool, False),
    },
    # FLUX.2 [pro] from a prompt (fal.ai's page and Peter's draft, 26 November 2026): no
    # num_images, an image_size preset or a width and height, JPEG unless told.
    "fal-ai/flux-2-pro": {
        "prompt": (str, True), "image_size": ((SIZE, tuple(PRESETS)), False), "seed": (int, False),
        "safety_tolerance": ((ENUM, ("1", "2", "3", "4", "5")), False), "enable_safety_checker": (bool, False),
        "output_format": ((ENUM, ("jpeg", "png")), False), "sync_mode": (bool, False),
    },
    # Models fal.ai runs for their makers, as its model API described them to Peter on 26
    # November 2026 (tests/data/fal/): their schemas file them under fal-ai/ (see openapi()).
    "openai/gpt-image-2": {
        "prompt": (str, True), "image_size": ((SIZE, tuple(PRESETS) + ("auto",)), False),
        "background": ((ENUM, ("auto", "transparent", "opaque")), False),
        "quality": ((ENUM, ("auto", "low", "medium", "high")), False), "num_images": (int, False),
        "output_format": ((ENUM, ("jpeg", "png", "webp")), False), "output_compression": (int, False),
        "sync_mode": (bool, False), "enable_safety_checker": (bool, False),
    },
    "openai/gpt-image-2/edit": {
        "prompt": (str, True), "image_urls": (list, True), "image_size": ((SIZE, tuple(PRESETS) + ("auto",)), False),
        "background": ((ENUM, ("auto", "transparent", "opaque")), False),
        "quality": ((ENUM, ("auto", "low", "medium", "high")), False), "num_images": (int, False),
        "output_format": ((ENUM, ("jpeg", "png", "webp")), False), "output_compression": (int, False),
        "sync_mode": (bool, False), "enable_safety_checker": (bool, False), "mask_url": (str, False),
    },
    "google/nano-banana-2.1": {
        "prompt": (str, True), "num_images": (int, False), "seed": (int, False),
        "aspect_ratio": ((ENUM, ("auto", "21:9", "16:9", "3:2", "4:3", "5:4", "1:1", "4:5", "3:4", "2:3", "9:16",
                                 "4:1", "1:4", "8:1", "1:8")), False),
        "output_format": ((ENUM, ("jpeg", "png", "webp")), False),
        "safety_tolerance": ((ENUM, ("1", "2", "3", "4", "5", "6")), False), "sync_mode": (bool, False),
        "system_prompt": (str, False), "resolution": ((ENUM, ("1K", "2K", "4K")), False),
        "limit_generations": (bool, False), "enable_web_search": (bool, False),
        "thinking_level": ((ENUM, ("minimal", "medium", "high")), False),
    },
    # As fal.ai's model API described it to Peter's draft (25 November 2026). One image in, one out.
    "fal-ai/birefnet/v2": {
        "image_url": (str, True),
        "model": ((ENUM, ("General Use (Light)", "General Use (Light 2K)", "General Use (Heavy)", "Matting",
                          "Portrait", "General Use (Dynamic)")), False),
        "operating_resolution": ((ENUM, ("1024x1024", "2048x2048", "2304x2304")), False),
        "refine_foreground": (bool, False), "output_mask": (bool, False), "mask_only": (bool, False),
        "sync_mode": (bool, False), "output_format": ((ENUM, ("webp", "png", "gif")), False),
    },
}
# What each returns, when it isn't a list of images: BiRefNet's one image.
OUTPUT = {"fal-ai/birefnet/v2": "image"}
# What the model API says of each, beside its schema (fal.ai's model pages, 25 November 2026).
ABOUT = {
    "fal-ai/nano-banana-2/edit": {"display_name": "Nano Banana 2", "category": "image-to-image",
                                  "license_type": "commercial", "status": "active",
                                  "description": "Google's image editing model."},
    "fal-ai/nano-banana-pro/edit": {"display_name": "Nano Banana Pro", "category": "image-to-image",
                                    "license_type": "commercial", "status": "active",
                                    "description": "Google's state-of-the-art image editing model."},
    "fal-ai/nano-banana-2": {"display_name": "Nano Banana 2", "category": "text-to-image",
                             "license_type": "commercial", "status": "active",
                             "description": "Google's fast image generation model."},
    "fal-ai/nano-banana-pro": {"display_name": "Nano Banana Pro", "category": "text-to-image",
                               "license_type": "commercial", "status": "active",
                               "description": "Google's state-of-the-art image generation model."},
    "fal-ai/flux-2-pro": {"display_name": "Flux 2 Pro", "category": "text-to-image", "license_type": "commercial",
                          "status": "active", "description": "Image editing with FLUX.2 [pro] from Black Forest "
                          "Labs. Ideal for high-quality image manipulation, style transfer, and sequential editing "
                          "workflows."},
    "openai/gpt-image-2": {"display_name": "GPT Image 2 API", "category": "text-to-image",
                           "license_type": "commercial", "status": "active",
                           "description": "GPT Image 2, OpenAI's latest image model, is capable of creating extremely "
                                          "detailed images with fine typography."},
    "openai/gpt-image-2/edit": {"display_name": "GPT Image 2 API", "category": "image-to-image",
                                "license_type": "commercial", "status": "active",
                                "description": "GPT Image 2, OpenAI's latest image model, is capable of making "
                                               "fine-grained, detailed edits to images."},
    "google/nano-banana-2.1": {"display_name": "Nano Banana 2.1", "category": "text-to-image",
                               "license_type": "commercial", "status": "active",
                               "description": "Nano Banana 2.1 by Google generates images from text prompts, with "
                                              "output resolutions up to 4K, adjustable aspect ratios, and optional "
                                              "web search grounding."},
    "fal-ai/birefnet/v2": {"display_name": "Birefnet Background Removal", "category": "image-to-image",
                           "license_type": "commercial", "status": "active",
                           "description": "bilateral reference framework (BiRefNet) for high-resolution dichotomous "
                                          "image segmentation (DIS)"},
}
DEFAULTS = {"num_images": 1, "aspect_ratio": "auto", "output_format": "png", "resolution": "1K",
            "safety_tolerance": "4", "sync_mode": False, "enable_web_search": False, "enable_image_search": False,
            "limit_generations": True, "system_prompt": "", "temperature": 1.0, "top_p": 0.95,
            "model": "General Use (Light)", "operating_resolution": "1024x1024", "refine_foreground": True,
            "output_mask": False, "mask_only": False}
# Where a model's default isn't the others': Nano Banana Pro makes squares unless told.
DEFAULTS_BY = {"fal-ai/nano-banana-pro": {"aspect_ratio": "1:1", "thinking_level": "high"},
               "fal-ai/flux-2-pro": {"image_size": "landscape_4_3", "output_format": "jpeg", "safety_tolerance": "2",
                                     "enable_safety_checker": True},
               "fal-ai/nano-banana-2": {"thinking_level": "minimal"},
               "openai/gpt-image-2": {"image_size": "landscape_4_3", "quality": "high", "background": "auto"},
               "openai/gpt-image-2/edit": {"image_size": "auto", "quality": "high", "background": "auto"},
               "google/nano-banana-2.1": {"thinking_level": "medium", "safety_tolerance": "4"}}
SAID = {"prompt": "The prompt for image editing.",
        "image_urls": "The URLs of the images to use for image-to-image generation or image editing.",
        "aspect_ratio": "The aspect ratio of the generated image.", "resolution": "The resolution of the image to "
        "generate.", "seed": "The seed for the random number generator.",
        "image_url": "URL of the image to remove background from",
        "model": "Model to use for background removal.", "operating_resolution": "The resolution to operate on.",
        "mask_only": "Whether to return only the segmentation mask without applying it to the image."}
SAID_BY = {**{e: {"prompt": "The text prompt to generate an image from."}
              for e in ("fal-ai/nano-banana-2", "fal-ai/nano-banana-pro")},
           "fal-ai/flux-2-pro": {"prompt": "The prompt to generate an image from.",
                                 "seed": "The seed to use for the generation."}}
SIDES = {"0.5K": 512, "1K": 1024, "2K": 2048, "4K": 4096}
# Each model's price by resolution, as a multiple of its price at 1K (fal.ai's pages, 24 and 25 November).
FACTORS = {"fal-ai/nano-banana-2/edit": {"0.5K": 0.75, "1K": 1.0, "2K": 1.5, "4K": 2.0},
           "fal-ai/nano-banana-pro/edit": {"1K": 1.0, "2K": 1.0, "4K": 2.0},
           "fal-ai/nano-banana-2": {"0.5K": 0.75, "1K": 1.0, "2K": 1.5, "4K": 2.0},
           "fal-ai/nano-banana-pro": {"1K": 1.0, "2K": 1.0, "4K": 2.0}}
# What its pricing API says (fal.ai's pages): {endpoint: (unit price, unit, currency)}.
PRICES = {"fal-ai/nano-banana-2/edit": (0.08, "images", "USD"),      # "images", as fal.ai's pricing says it
          "fal-ai/nano-banana-pro/edit": (0.15, "images", "USD"),
          "fal-ai/nano-banana-2": (0.08, "images", "USD"),
          "fal-ai/nano-banana-pro": (0.15, "images", "USD"),
          "fal-ai/birefnet/v2": (0.000575, "compute seconds", "USD"),     # Peter's draft, 25 November
          "fal-ai/flux-2-pro": (0.03, "processed megapixels", "USD")}      # Peter's draft, 26 November


def _size(data: dict) -> tuple[int, int]:
    """The width and height of what a job makes: its image_size, else its resolution's square."""
    side = SIDES.get(data.get("resolution") or "1K", 1024)
    size = data.get("image_size")
    return (size["width"], size["height"]) if isinstance(size, dict) else PRESETS.get(size, (side, side))


def openapi(endpoint: str) -> dict:
    """An OpenAPI schema of one of its models, shaped as fal.ai's model API gives one: a POST
    of its input to the endpoint, and a GET of its output from the request."""
    base = "".join(w.capitalize() for w in re.split(r"[^A-Za-z0-9]+", endpoint.split("/", 1)[1]) if w)
    props, required = {}, []
    for f, (kind, req) in MODELS[endpoint].items():
        s: dict = {"title": f.replace("_", " ").title()}
        if isinstance(kind, tuple) and kind[0] == SIZE:
            s["anyOf"] = [{"$ref": "#/components/schemas/ImageSize"},
                          {"type": "string", "enum": list(kind[1])}]
        elif isinstance(kind, tuple):
            s.update(type="string", enum=list(kind[1]))
        elif kind is list:
            s.update(type="array", items={"type": "string"})
        else:
            s["type"] = {str: "string", int: "integer", float: "number", bool: "boolean"}[kind]
        defaults, said = {**DEFAULTS, **DEFAULTS_BY.get(endpoint, {})}, {**SAID, **SAID_BY.get(endpoint, {})}
        if f in defaults:
            s["default"] = defaults[f]
        if f in said:
            s["description"] = said[f]
        props[f] = s
        if req:
            required.append(f)
    ref = "#/components/schemas/"
    gives = OUTPUT.get(endpoint, "images")
    # fal.ai files a model it runs for its maker under its own owner: openai/gpt-image-2's
    # schema says POST /fal-ai/gpt-image-2 (26 November 2026). Jobs go to the endpoint's name.
    filed = endpoint if endpoint.startswith("fal-ai/") else "fal-ai/" + endpoint.split("/", 1)[1]
    made = ({"image": {"$ref": ref + "ImageFile"}, "mask_image": {"$ref": ref + "ImageFile"}} if gives == "image" else
            {"images": {"title": "Images", "type": "array", "items": {"$ref": ref + "ImageFile"}},
             "description": {"title": "Description", "type": "string"}})
    return {"openapi": "3.0.4", "info": {"title": "Queue OpenAPI for " + endpoint, "version": "1.0.0"},
            "components": {"schemas": {
                f"{base}Input": {"type": "object", "properties": props, "required": required},
                f"{base}Output": {"type": "object", "required": [k for k in made if k != "mask_image"],
                                  "properties": made},
                "ImageSize": {"type": "object", "properties": {
                    "width": {"title": "Width", "type": "integer", "default": 512, "exclusiveMaximum": 14142},
                    "height": {"title": "Height", "type": "integer", "default": 512, "exclusiveMaximum": 14142}}},
                "ImageFile": {"type": "object", "required": ["url"], "properties": {
                    "url": {"type": "string"}, "content_type": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "width": {"anyOf": [{"type": "integer"}, {"type": "null"}]}}}}},
            "paths": {
                f"/{filed}": {"post": {"requestBody": {"required": True, "content": {
                    "application/json": {"schema": {"$ref": f"{ref}{base}Input"}}}}}},
                f"/{filed}/requests/{{request_id}}": {"get": {"responses": {"200": {"content": {
                    "application/json": {"schema": {"$ref": f"{ref}{base}Output"}}}}}}}}}


def grey_png(width: int, height: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    rows = (b"\x00" + b"\x80" * width) * height
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


def _error(status: int, detail) -> tuple[int, dict, bytes]:
    return status, {"Content-Type": "application/json"}, json.dumps({"detail": detail}).encode()


def _ok(data, status: int = 200) -> tuple[int, dict, bytes]:
    return status, {"Content-Type": "application/json"}, json.dumps(data).encode()


class MockFal:
    """fal.ai in memory. `calls` records each request: its method, path and whether it came
    with the key (never the key itself)."""

    def __init__(self, key: str, base: str = "http://127.0.0.1:8189", queue_seconds: float = 0.0,
                 run_seconds: float = 0.0, clock=time.monotonic, billing_key: str | None = None, bill: bool = False,
                 balance: float = 50.0, wall=time.time):
        self.key = key
        self.billing_key = billing_key or key   # its bill and balance take this one
        self.bill = bill                        # charge each job at its price; otherwise $0, it takes no money
        self.balance = balance
        self.wall = wall
        self.base = base.rstrip("/")
        self.queue_seconds, self.run_seconds = queue_seconds, run_seconds
        self.clock = clock
        self.paused = False             # while set, requests wait in the queue (for the tests)
        self.files: dict[str, tuple[bytes, str]] = {}
        self.tokens: set[str] = set()
        self.requests: dict[str, dict] = {}
        self.prices = dict(PRICES)      # the tests change them
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    # ---------------------------------------------------------------- one request
    def respond(self, method: str, url: str, headers: dict, body: bytes) -> tuple[int, dict, bytes]:
        parts = urlsplit(url)
        path, query = parts.path.rstrip("/") or "/", parse_qs(parts.query)
        auth = {k.lower(): v for k, v in headers.items()}.get("authorization", "")
        keyed = auth == f"Key {self.key}"
        billing = path in ("/v1/models/billing-events", "/v1/account/billing")
        with self.lock:
            said = {k.lower(): v for k, v in headers.items()}
            self.calls.append({"method": method, "path": path, "keyed": keyed,
                               "lifecycle": said.get("x-fal-object-lifecycle-preference"),
                               "store_io": said.get("x-fal-store-io"),
                               "no_fallback": said.get("x-app-fal-disable-fallback"), "host": parts.netloc})
            if method == "GET" and path.startswith("/files/b/"):
                f = self.files.get(path)
                return (200, {"Content-Type": f[1]}, f[0]) if f else _error(404, "No such file")
            if method == "POST" and path == "/files/upload":
                if auth.partition(" ")[2] not in self.tokens or not auth.startswith("Bearer "):
                    return _error(401, "Invalid upload token")
                return self._upload(headers, body)
            if billing:
                if auth != f"Key {self.billing_key}":
                    return 403, {"Content-Type": "application/json"}, json.dumps({"error": {
                        "type": "authorization_error", "message": "This key can't read billing (billing:usage:read)"}
                    }).encode()
                return self._billing(method, path, query)
            if not keyed:
                return _error(401, "No user found for Key ID and Secret" if auth else "Missing authorization")
            if method == "POST" and path == "/storage/auth/token":
                if query.get("storage_type") != ["fal-cdn-v3"]:
                    return _error(422, "storage_type: fal-cdn-v3 only")
                token = uuid.uuid4().hex
                self.tokens.add(token)
                until = datetime.now(timezone.utc) + timedelta(hours=1)
                return _ok({"token": token, "token_type": "Bearer", "base_url": self.base,
                            "expires_at": until.isoformat()})
            if method == "GET" and path == "/v1/models":
                wanted = [e for e in query.get("endpoint_id", []) if e in MODELS]
                expand = "openapi-3.0" in query.get("expand", [])
                return _ok({"models": [{"endpoint_id": e, "metadata": ABOUT[e], **({"openapi": openapi(e)}
                                                                                   if expand else {})}
                                       for e in wanted], "has_more": False, "next_cursor": None})
            if method == "GET" and path == "/v1/models/pricing":
                wanted = [e for v in query.get("endpoint_id", []) for e in v.split(",") if e]
                if not 1 <= len(wanted) <= 50:
                    return 400, {"Content-Type": "application/json"}, json.dumps({"error": {
                        "type": "validation_error", "message": "endpoint_id: 1 to 50 endpoint ids"}}).encode()
                return _ok({"prices": [{"endpoint_id": e, "unit_price": p[0], "unit": p[1], "currency": p[2]}
                                       for e in wanted for p in [self.prices.get(e)] if p],
                            "next_cursor": None, "has_more": False})
            segs = path.strip("/").split("/")
            if len(segs) >= 4 and segs[2] == "requests":
                return self._request(method, "/".join(segs[:2]), segs[3], segs[4:])
            if method == "POST":
                return self._submit("/".join(segs), body)
            return _error(405, "Method not allowed")

    def _upload(self, headers: dict, body: bytes) -> tuple[int, dict, bytes]:
        h = {k.lower(): v for k, v in headers.items()}
        name = h.get("x-fal-file-name") or "upload.bin"
        path = f"/files/b/{uuid.uuid4().hex[:12]}/{name}"
        self.files[path] = (body, h.get("content-type") or "application/octet-stream")
        return _ok({"access_url": self.base + path})

    def _submit(self, endpoint: str, body: bytes) -> tuple[int, dict, bytes]:
        schema = MODELS.get(endpoint)
        if schema is None:
            return _error(404, f"Application '{endpoint}' not found")
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return _error(422, [{"loc": ["body"], "msg": "Input should be a JSON object", "type": "json_invalid"}])
        problems = self._check(schema, data if isinstance(data, dict) else {})
        if problems:
            return _error(422, problems)
        rid = str(uuid.uuid4())
        prompt = data.get("prompt") or ""
        run = 60.0 if "MOCK_SLOW" in prompt else self.run_seconds
        self.requests[rid] = {"endpoint": endpoint, "input": data, "at": self.clock(), "wall": self.wall(),
                              "run": run, "cancel": None, "ended": None}
        owner_app = "/".join(endpoint.split("/")[:2])
        url = f"{self.base}/{owner_app}/requests/{rid}"
        return _ok({"request_id": rid, "response_url": url, "status_url": url + "/status",
                    "cancel_url": url + "/cancel", "queue_position": 0})

    def _check(self, schema: dict, data: dict) -> list[dict]:
        problems = []
        for field, (kind, required) in schema.items():
            if field not in data:
                if required:
                    problems.append({"loc": ["body", field], "msg": "Field required", "type": "missing"})
                continue
            v = data[field]
            if isinstance(kind, tuple) and kind[0] == SIZE:
                sized = isinstance(v, dict) and set(v) == {"width", "height"} and all(
                    isinstance(x, int) and not isinstance(x, bool) and 0 < x < 14142 for x in v.values())
                if v not in kind[1] and not sized:
                    problems.append({"loc": ["body", field], "msg": f"Input should be one of {', '.join(kind[1])}, "
                                     f"or a width and height", "type": "union"})
            elif isinstance(kind, tuple):
                if v not in kind[1]:
                    problems.append({"loc": ["body", field], "msg": f"Input should be one of {', '.join(kind[1])}",
                                     "type": "enum"})
            elif not isinstance(v, (int, float) if kind is float else kind) or (kind in (int, float)
                                                                                and isinstance(v, bool)):
                problems.append({"loc": ["body", field], "msg": f"Input should be a valid {kind.__name__}",
                                 "type": f"{kind.__name__}_type"})
        for field in sorted(set(data) - set(schema)):
            problems.append({"loc": ["body", field], "msg": "Not a field of this model (the stand-in is strict)",
                             "type": "extra_forbidden"})
        urls = [("image_urls", i, u) for i, u in enumerate(data.get("image_urls") or []
                                                         if isinstance(data.get("image_urls"), list) else [])]
        urls += [("image_url", None, data["image_url"])] if isinstance(data.get("image_url"), str) else []
        for field, i, u in urls:
            if not (isinstance(u, str) and (u.startswith("data:") or urlsplit(u).path in self.files)):
                problems.append({"loc": ["body", field] + ([] if i is None else [i]), "msg": "The stand-in can't "
                                 "download this file (upload it to its CDN first)", "type": "file_download_error"})
        if data.get("output_format", "png") != "png":
            problems.append({"loc": ["body", "output_format"], "msg": "The stand-in makes PNGs only",
                             "type": "feature_not_supported"})
        return problems

    def _state(self, r: dict) -> str:
        if r["ended"]:
            return r["ended"]
        t = self.clock() - r["at"]
        if r["cancel"] == "queue":
            return "cancelled"
        if self.paused or t < self.queue_seconds:
            return "queued"
        if t < self.queue_seconds + r["run"]:
            return "running"
        r["ended"] = "done"
        return r["ended"]

    def _request(self, method: str, owner_app: str, rid: str, rest: list) -> tuple[int, dict, bytes]:
        r = self.requests.get(rid)
        if r is None or "/".join(r["endpoint"].split("/")[:2]) != owner_app:
            return _error(404, "Request not found")
        state = self._state(r)
        url = f"{self.base}/{owner_app}/requests/{rid}"
        prompt = r["input"].get("prompt") or ""
        if rest == ["status"] and method == "GET":
            d = {"request_id": rid, "response_url": url}
            if state == "queued":
                return _ok({**d, "status": "IN_QUEUE", "queue_position": 0})
            if state == "running":
                return _ok({**d, "status": "IN_PROGRESS", "logs": []})
            d.update(status="COMPLETED", logs=[], metrics={"inference_time": r["run"]})
            if state == "cancelled":
                d.update(error="Request was cancelled", error_type="client_cancelled")
            elif "MOCK_FAIL" in prompt:
                d.update(error="MOCK_FAIL: the stand-in's model failed", error_type="runner_server_error")
            return _ok(d)
        if rest == ["cancel"] and method == "PUT":
            if state == "queued":
                r["cancel"] = "queue"
                return _ok({"status": "OK"})
            if state == "running":
                r["cancel"], r["ended"] = "running", "cancelled"     # its runner stops at once
                return _ok({"status": "CANCELLATION_REQUESTED"}, 202)
            return _ok({"status": "ALREADY_COMPLETED"}, 400)
        if rest == [] and method == "GET":
            if state in ("queued", "running"):
                return _error(400, "Request is still in progress")
            if state == "cancelled" or "MOCK_FAIL" in prompt:
                return _error(500, "MOCK_FAIL: the stand-in's model failed")
            if "MOCK_POLICY" in prompt:
                return _error(422, [{"loc": ["body", "prompt"], "msg": "The content was flagged",
                                     "type": "content_policy_violation"}])
            return _ok(self._made(r))
        return _error(405, "Method not allowed")

    # ---------------------------------------------------------------- the bill
    def charges(self) -> list[dict]:
        """Its bill: a charge for each job that finished well, when it finished."""
        out = []
        for rid, r in self.requests.items():
            prompt = r["input"].get("prompt") or ""
            if self._state(r) != "done" or r["cancel"] or "MOCK_FAIL" in prompt or "MOCK_POLICY" in prompt:
                continue
            price, unit, _ = self.prices.get(r["endpoint"], (0.0, "image", "USD"))
            factor = FACTORS.get(r["endpoint"], {}).get(r["input"].get("resolution") or "1K", 1.0)
            quantity = factor * int(r["input"].get("num_images") or 1)
            if "second" in unit:        # by the compute second: the time it ran, a second at least
                quantity = max(float(r["run"]), 1.0)
            elif "megapixel" in unit:   # fal.ai's page: the first at the price, the rest at half, rounded up
                w, h = _size(r["input"])
                quantity = 1 + 0.5 * (math.ceil(w * h / 1048576) - 1)
            cost = round(price * quantity, 9) if self.bill else 0.0
            ended = r["wall"] + self.queue_seconds + r["run"]
            out.append({"request_id": rid, "endpoint_id": r["endpoint"], "at": ended,
                        "quantity": quantity, "output_units": quantity, "unit": unit, "unit_price": price,
                        "percent_discount": 0, "cost_subtotal": cost, "cost_discount": 0, "cost_total": cost,
                        "cost_estimate_nano_usd": round(cost * 1e9)})
        return sorted(out, key=lambda e: e["at"])

    def _billing(self, method: str, path: str, query: dict) -> tuple[int, dict, bytes]:
        if method != "GET":
            return _error(405, "Method not allowed")
        if path == "/v1/account/billing":
            out = {"username": "stand-in"}
            if "credits" in query.get("expand", []):
                spent = sum(e["cost_total"] for e in self.charges())
                out["credits"] = {"current_balance": round(max(self.balance - spent, 0.0), 9), "currency": "USD"}
            return _ok(out)

        def when(name, default):
            v = (query.get(name) or [None])[0]
            if v is None:
                return default
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        try:
            start, end = when("start", self.wall() - 86400), when("end", self.wall())
            limit = max(1, min(int((query.get("limit") or ["50"])[0]), 10000))
            offset = int(base64.b64decode((query.get("cursor") or [""])[0] or b"MA==").decode())
        except ValueError:
            return 400, {"Content-Type": "application/json"}, json.dumps({"error": {
                "type": "validation_error", "message": "start, end, limit or cursor isn't valid"}}).encode()
        if end - start > 90 * 86400:
            return 400, {"Content-Type": "application/json"}, json.dumps({"error": {
                "type": "validation_error", "message": "Date range capped at 90 days per request"}}).encode()
        events = [e for e in self.charges() if start <= e["at"] < end]
        page = events[offset:offset + limit]
        nxt = base64.b64encode(str(offset + limit).encode()).decode() if offset + limit < len(events) else None
        for e in page:
            e["timestamp"] = datetime.fromtimestamp(e.pop("at"), timezone.utc).isoformat().replace("+00:00", "Z")
        return _ok({"billing_events": page, "next_cursor": nxt, "has_more": nxt is not None})

    def _made(self, r: dict) -> dict:
        if "made" not in r:
            data = r["input"]
            w, h = _size(data)
            images = []
            for i in range(int(data.get("num_images") or 1)):
                png = grey_png(w, h)
                if data.get("sync_mode"):
                    url = "data:image/png;base64," + base64.b64encode(png).decode()
                else:
                    path = f"/files/b/{uuid.uuid4().hex[:12]}/out_{i}.png"
                    self.files[path] = (png, "image/png")
                    url = self.base + path
                images.append({"url": url, "content_type": "image/png", "file_name": f"out_{i}.png",
                               "file_size": len(png), "width": w, "height": h})
            if OUTPUT.get(r["endpoint"]) == "image":
                r["made"] = {"image": images[0]}
            else:
                r["made"] = {"images": images, "description": "A grey square, from the stand-in."}
        return r["made"]

    # ---------------------------------------------------------------- serving it
    def transport(self):
        """An httpx transport answering from here, for tests in one process."""
        import httpx

        def handle(request: httpx.Request) -> httpx.Response:
            status, headers, body = self.respond(request.method, str(request.url), dict(request.headers),
                                                 request.read())
            return httpx.Response(status, headers=headers, content=body)
        return httpx.MockTransport(handle)

    def server(self, port: int) -> ThreadingHTTPServer:
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def _go(self):
                size = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(size) if size else b""
                status, headers, out = mock.respond(self.command, f"http://{self.headers.get('Host')}{self.path}",
                                                    dict(self.headers.items()), body)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            do_GET = do_POST = do_PUT = _go

            def log_message(self, fmt, *args):     # one line each, never a header
                sys.stderr.write(f"mock_fal: {self.command} {self.path.split('?')[0]} -> {args[1]}\n")

        return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def read_key_file(path: str) -> str:
    """The key, as the gateway reads it: a byte-order mark some editors put first dropped."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            key = f.read().strip()
    except (OSError, UnicodeDecodeError) as e:
        raise SystemExit(f"mock_fal: can't read the key file {path}: {e}") from None
    if not key or not (key.isascii() and key.isprintable()) or any(c.isspace() for c in key):
        raise SystemExit(f"mock_fal: the key file {path} should hold the key alone, on one line, as plain text")
    return key


def from_gateway(path: str) -> tuple[int, str, str | None]:
    """Its port and key files (requests, billing), from where gateway.yaml sends fal.ai's jobs."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from comfyfarm.config import load_agent_config
    from comfyfarm.runners.api.runner import key_path
    cfg = load_agent_config(path)
    fal = cfg.gateway.providers.get("fal")
    url = urlsplit(fal.url) if fal and fal.url else None
    if url is None or url.hostname not in ("127.0.0.1", "localhost") or not url.port:
        raise SystemExit(f"mock_fal: {path} doesn't send fal.ai's jobs to a stand-in on this machine: set "
                         f"providers.fal.url to e.g. http://127.0.0.1:8189")
    if not fal.keys.get("requests"):
        raise SystemExit(f"mock_fal: {path} names no key file (providers.fal.keys.requests)")
    billing = fal.keys.get("billing")
    return url.port, str(key_path(cfg, fal.keys["requests"])), str(key_path(cfg, billing)) if billing else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="A stand-in for fal.ai, on 127.0.0.1.")
    ap.add_argument("--gateway", help="gateway.yaml: answer where it sends fal.ai's jobs, with its key file")
    ap.add_argument("--port", type=int, default=8189)
    ap.add_argument("--key-file", help="the file holding the key it accepts (made up)")
    ap.add_argument("--queue-seconds", type=float, default=1.0, help="each job waits this long in its queue")
    ap.add_argument("--run-seconds", type=float, default=3.0, help="then runs this long")
    ap.add_argument("--billing-key-file", help="the file holding the key its bill takes (made up; default: the "
                                               "requests key)")
    ap.add_argument("--bill", action="store_true", help="charge each job at its price on its bill, as fal.ai "
                                                         "would (default: $0, it takes no money)")
    ap.add_argument("--balance", type=float, default=50.0, help="its account's balance in US dollars, before --bill")
    args = ap.parse_args(argv)
    if args.gateway:
        args.port, args.key_file, billing = from_gateway(args.gateway)
        args.billing_key_file = args.billing_key_file or billing
    if not args.key_file:
        ap.error("give --gateway gateway.yaml, or --key-file")
    key = read_key_file(args.key_file)
    billing_key = read_key_file(args.billing_key_file) if args.billing_key_file else None
    mock = MockFal(key, f"http://127.0.0.1:{args.port}", args.queue_seconds, args.run_seconds,
                   billing_key=billing_key, bill=args.bill, balance=args.balance)
    srv = mock.server(args.port)
    print(f"mock_fal: fal.ai's stand-in on http://127.0.0.1:{args.port} (Ctrl+C to stop)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
