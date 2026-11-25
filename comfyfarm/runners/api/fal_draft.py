"""A fal.ai definition drafted from fal.ai's own description of an endpoint (decision 0065):
`comfyfarm fal draft <endpoint>`.

fal.ai describes each endpoint through its model API (GET /v1/models?endpoint_id=...
&expand=openapi-3.0): its name, category and licence type, and an OpenAPI schema of what it
takes and what it returns. Its pricing API gives the price of one unit. The draft turns them
into a process.yaml written the way the farm's fal.ai definitions are
(workflows/nano_banana2_edit_fal):

- an image field (..._url, ..._urls) becomes an input: the first is the job's `input`, which
  names its outputs, and a field that takes several images also gets an optional `reference`;
- the prompt is a text param; a field with choices a choice, a number a number with its
  range, a yes/no a checkbox, each with fal.ai's default, label and help;
- what the farm decides is fixed and never the artist's: one image a job, PNGs, the result in
  fal.ai's answer (sync_mode, decision 0062) and no web search;
- a model's finer knobs (temperature, safety levels, conversation history...) are left to
  fal.ai's defaults, and named in the draft;
- an `images` or `image` result is frames; the job writes PNGs into the output folder picked;
- the cost block is the pricing API's price and unit. What the page says on top of it (a
  multiple at 4K) is the reviewer's to add.

A draft is not a definition: it is reviewed before it is committed, as every definition is
(its price against the model's page, what the artist may set, the licence, the task). What
the draft couldn't decide is a TODO line at its top. One whose price wasn't read has price 0,
which the farm refuses, so it can't run until someone writes the reviewed price.

Standard library and yaml only: what fal.ai said comes in as plain data."""

from __future__ import annotations

import json
import re
import textwrap
from dataclasses import dataclass, field

FIXED = {"num_images": 1, "num_outputs": 1, "sync_mode": True, "enable_web_search": False,
         "enable_image_search": False}
# Left to fal.ai's defaults: a model's finer knobs, a conversation, and its content checks.
LEFT_OUT = ("history", "context", "return_history", "system_prompt", "thinking_level", "include_thoughts",
            "temperature", "top_p", "max_output_tokens", "limit_generations", "output_compression",
            "safety_tolerance", "enable_safety_checker", "safety_checker", "enable_prompt_expansion")
PROMPTS = ("prompt", "negative_prompt")
FORMATS = ("png", "jpeg", "webp")
TASKS = {"text-to-image": "image", "image-to-image": "image", "text-to-video": "video", "image-to-video": "video",
         "video-to-video": "video", "reference-to-video": "video"}
LIMITS = {"image": 10, "video": 30}                     # minutes at fal.ai, queue included
OWNERS = ("fal-ai",)                                    # left out of a definition's name: fal.ai's own models
WIDTH = 92
INPUT_HELP = ("The image it starts from: a file, or a sequence or movie with the frame to use after it, e.g. "
              "/rdo/shows/<show>/.../<shot>.plate.%04d.exr 1050. EXRs are converted with the shot's OCIO look "
              "first, and images over 2048 pixels are scaled down.")


class DraftError(Exception):
    """fal.ai's description of the endpoint can't be drafted from: what is wrong with it."""


@dataclass
class Field:
    name: str                       # fal.ai's field
    param: str                      # the definition's param
    kind: str                       # input | text | choice | bool | int | float
    label: str
    help: str = ""
    required: bool = False
    default: object = None
    choices: list | None = None
    minimum: float | None = None
    maximum: float | None = None
    many: bool = False              # an input field that takes several URLs


@dataclass
class Draft:
    name: str                       # the definition's folder and id
    endpoint: str
    text: str                       # its process.yaml
    todo: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ fal.ai's schema
def _resolve(s, comps: dict, depth: int = 0):
    """A schema with its $ref followed, and a nullable anyOf (X or null) as X."""
    if depth > 20 or not isinstance(s, dict):
        return s if isinstance(s, dict) else {}
    if "$ref" in s:
        target = comps.get(str(s["$ref"]).rsplit("/", 1)[-1])
        if target is None:
            return {}
        return _resolve({**target, **{k: v for k, v in s.items() if k != "$ref"}}, comps, depth + 1)
    for key in ("anyOf", "oneOf"):
        if isinstance(s.get(key), list):
            options = [o for o in s[key] if not (isinstance(o, dict) and o.get("type") == "null")]
            if len(options) == 1:
                rest = {k: v for k, v in s.items() if k != key}
                return _resolve({**_resolve(options[0], comps, depth + 1), **rest}, comps, depth + 1)
    if isinstance(s.get("allOf"), list) and len(s["allOf"]) == 1:
        rest = {k: v for k, v in s.items() if k != "allOf"}
        return _resolve({**_resolve(s["allOf"][0], comps, depth + 1), **rest}, comps, depth + 1)
    return s


def schemas(model: dict) -> tuple[dict, dict, dict]:
    """(what the endpoint takes, what it returns, the schema's components), from its OpenAPI."""
    endpoint, api = model.get("endpoint_id") or "", model.get("openapi")
    if not isinstance(api, dict) or "error" in api or not isinstance(api.get("paths"), dict):
        why = (api or {}).get("error") if isinstance(api, dict) else None
        raise DraftError(f"fal.ai gave no schema for {endpoint}" + (f": {why}" if why else ""))
    comps = (api.get("components") or {}).get("schemas") or {}
    paths = api["paths"]

    def body(path: str, method: str, where: tuple) -> dict:
        node = (paths.get(path) or {}).get(method) or {}
        for k in where:
            node = node.get(k) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            raise DraftError(f"{endpoint}'s schema has no {method.upper()} {path} with a JSON body")
        return _resolve(node, comps)
    takes = body(f"/{endpoint}", "post", ("requestBody", "content", "application/json", "schema"))
    gives = body(f"/{endpoint}/requests/{{request_id}}", "get",
                 ("responses", "200", "content", "application/json", "schema"))
    return takes, gives, comps


def _media(name: str) -> str | None:
    """What a URL field takes, by fal.ai's naming: image, video, audio or another file."""
    if not (name.endswith("_url") or name.endswith("_urls")):
        return None
    for kind, words in (("image", ("image", "mask")), ("video", ("video",)), ("audio", ("audio", "voice")),):
        if any(w in name for w in words):
            return kind
    return "file"


def _label(name: str, s: dict) -> str:
    text = str(s.get("title") or name.replace("_", " ")).strip()
    return text[:1].upper() + text[1:].lower() if text else name


def _help(s: dict) -> str:
    text = " ".join(str(s.get("description") or "").split())
    return text if len(text) <= 300 else text[:297].rsplit(" ", 1)[0] + "..."


def fields(takes: dict, comps: dict) -> tuple[list[Field], dict, list[str], list[str]]:
    """The definition's params from what the endpoint takes: (params, fixed fields, fields left
    to fal.ai's defaults, TODO lines)."""
    props = takes.get("properties") or {}
    required = set(takes.get("required") or [])
    out: list[Field] = []
    fixed: dict = {}
    left: list[str] = []
    todo: list[str] = []
    images = []
    for name, raw in props.items():
        s = _resolve(raw, comps)
        media = _media(name)
        if name in FIXED:
            fixed[name] = FIXED[name]
        elif name == "output_format":
            if "png" in (s.get("enum") or []):
                fixed[name] = "png"
            else:
                todo.append(f"output_format takes {s.get('enum')}: the farm writes PNGs; fix one it takes and set "
                            f"writes.frames.ext to match")
        elif media == "image":
            images.append((name, s))
        elif media is not None:
            todo.append(f"{name}: a {media} input, which the farm's fal.ai definitions don't take yet: "
                        + ("required, so this endpoint can't run until they do" if name in required
                           else "left out"))
        elif name in LEFT_OUT:
            left.append(name)
        elif name in PROMPTS and s.get("type") == "string":
            out.append(Field(name, name, "text", _label(name, s), _help(s), name in required))
        elif isinstance(s.get("enum"), list) and s["enum"]:
            out.append(Field(name, name, "choice", _label(name, s), _help(s), name in required, s.get("default"),
                             list(s["enum"])))
        elif s.get("type") == "boolean":
            out.append(Field(name, name, "bool", _label(name, s), _help(s), False, bool(s.get("default", False))))
        elif s.get("type") in ("integer", "number"):
            out.append(Field(name, name, "int" if s["type"] == "integer" else "float", _label(name, s), _help(s),
                             name in required, s.get("default"), None, s.get("minimum"), s.get("maximum")))
        else:
            left.append(name)
    params: list[Field] = []
    for i, (name, s) in enumerate(images):
        many = s.get("type") == "array"
        if i == 0:
            # The job's input names its outputs (show, shot, version): the farm needs it.
            params.append(Field(name, "input", "input", "Input image", INPUT_HELP, True, many=many))
            if name not in required:
                todo.append(f"{name} is optional at fal.ai: the farm needs an input image to name the job's "
                            f"outputs, so the draft requires one")
            if many:
                params.append(Field(name, "reference", "input", "Reference image",
                                    "Optional: a second image the prompt can refer to. Sent after the input.",
                                    False, many=True))
        else:
            param = re.sub(r"_urls?$", "", name)
            params.append(Field(name, param, "input", _label(param, s), _help(s), name in required, many=many))
    if not images:
        todo.append("it takes no image: the farm's fal.ai definitions need an input image to name the job's "
                    "outputs, until text-only jobs come")
    return params + out, fixed, left, todo


def writes(gives: dict, comps: dict) -> tuple[dict, list[str]]:
    """What the job writes, from what the endpoint returns."""
    props = gives.get("properties") or {}
    if "images" in props or "image" in props:
        return {"frames": {"ext": "png"}}, []
    for kind in ("video", "audio"):
        if kind in props:
            return {}, [f"it returns a {kind}, which the farm's fal.ai definitions don't bring back yet"]
    return {}, [f"it returns {', '.join(props) or 'nothing the draft knows'}: say what the job writes"]


# ------------------------------------------------------------------ the draft
def name_for(endpoint: str) -> str:
    """The definition's name: the endpoint without fal.ai's own owner, in snake case, then _fal."""
    parts = endpoint.split("/")
    if parts[0] in OWNERS:
        parts = parts[1:]
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", "/".join(parts).lower())).strip("_") + "_fal"


def tag_for(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.removesuffix("_fal"))[:20] or "fal"


def _unit(unit: str) -> str:
    u = "_".join(str(unit or "").lower().split())
    return u[:-1] if len(u) > 3 and u.endswith("s") and not u.endswith("ss") else u


def _q(v) -> str:
    """A value as YAML: JSON is YAML, quoted where it must be."""
    return json.dumps(v, ensure_ascii=False)


def _flow(key: str, items: list, indent: int) -> list[str]:
    """A list in YAML's flow style, wrapped to the farm's line length."""
    pad, lines, line = " " * indent, [], f"{' ' * indent}{key}: ["
    cont = " " * len(line)
    for i, item in enumerate(items):
        word = _q(item) + ("]" if i == len(items) - 1 else ",")
        if len(line) + 1 + len(word) > WIDTH and line.strip() not in (f"{key}: [", ""):
            lines.append(line.rstrip())
            line = cont + word
        else:
            line += ("" if line.endswith("[") else " ") + word
    return lines + [line] if items else [f"{pad}{key}: []"]


def _comments(text: str, first: str = "# ", more: str = "#   ") -> list[str]:
    return textwrap.wrap(text, WIDTH, initial_indent=first, subsequent_indent=more, break_on_hyphens=False)


def _block(key: str, text: str, indent: int) -> list[str]:
    pad = " " * indent
    if not text:
        return []
    if len(pad) + len(key) + 2 + len(_q(text)) <= WIDTH:
        return [f"{pad}{key}: {_q(text)}"]
    lines = textwrap.wrap(text, WIDTH - indent - 2, break_on_hyphens=False)
    return [f"{pad}{key}: >"] + [f"{pad}  {ln}" for ln in lines]


def draft(model: dict, price: dict | None, today: str) -> Draft:
    """The process.yaml for one endpoint, from fal.ai's description of it (`model`, its model
    API's answer with expand=openapi-3.0) and its price ({unit_price, unit, currency}, or None
    when it wasn't read). `today`: when, for the comments."""
    endpoint = str(model.get("endpoint_id") or "")
    if not endpoint:
        raise DraftError("fal.ai's answer names no endpoint")
    meta = model.get("metadata") if isinstance(model.get("metadata"), dict) else {}
    takes, gives, comps = schemas(model)
    params, fixed, left, todo = fields(takes, comps)
    wr, more = writes(gives, comps)
    todo += more
    name, category = name_for(endpoint), str(meta.get("category") or "")
    task = TASKS.get(category)
    if task is None:
        todo.append(f"task: fal.ai's category is {category or 'not given'}; pick the menu's task "
                    f"(workflows/tasks.yaml)")
    if meta.get("status") == "deprecated":
        todo.append("fal.ai says this endpoint is deprecated")
    licence = f"fal-{meta.get('license_type') or 'unknown'}"
    label = str(meta.get("display_name") or endpoint)
    about = " ".join(str(meta.get("description") or "").split())
    sends = {f.param: f.name for f in params if f.kind != "input"}
    upload = {f.param: f.name for f in params if f.kind == "input"}

    # Its cost: the pricing API's price and unit; quantity 1 image (num_images is fixed), or a
    # video's seconds when it has a duration param.
    if price and price.get("currency", "USD") == "USD" and price.get("unit_price"):
        unit, amount = _unit(price.get("unit") or ""), float(price["unit_price"])
        cost = [f"  unit: {_q(unit)}", f"  price: {amount:g}"]
        if unit == "second" and any(f.param == "duration" and f.kind in ("int", "float") for f in params):
            cost.append("  quantity: {param: duration}")
        elif unit == "image":
            cost.append("  quantity: 1                   # num_images, fixed below")
        else:
            cost.append("  quantity: 1")
            todo.append(f"cost: fal.ai bills it by {price.get('unit')}: say how many a job is (cost.quantity)")
        todo.append("cost: check the price against the model's page, and add what changes it "
                    "(cost.by, e.g. a multiple at 4K)")
    else:
        cost = ["  unit: image", "  price: 0                      # the farm refuses 0: write the reviewed price",
                "  quantity: 1"]
        todo.append("cost: fal.ai's pricing API gave no price in US dollars: write it from the model's page")

    L = [*_comments(f"Drafted by `comfyfarm fal draft {endpoint}` on {today}, from fal.ai's description of "
                    f"the endpoint and its pricing API (decision 0065). Reviewed by: TODO, before it is "
                    f"committed.", more="# "),
         *[ln for t in todo for ln in _comments(f"TODO: {t}", more="#       ")],
         "#",
         "# An API definition (decisions 0055, 0056): the job runs at fal.ai, sent by the farm's",
         "# gateway, never on a farm GPU. Full reference: \"API definitions and the gateway\" in README.md.",
         "",
         f"id: {name}",
         f"label: {_q(label)}"]
    L += _block("description", (about + " " if about else "") + "The inputs and the prompt leave Rodeo's network: "
                "they are sent to fal.ai, which keeps the inputs an hour.", 0)
    L += [f"tag: {tag_for(name)}",
          f"task: {task or 'image'}" + ("" if task else "                    # TODO: from fal.ai's category"),
          "provider: fal",
          f"family: {_q(endpoint.split('/', 1)[-1].replace('/', '-'))}",
          "class: interactive",
          f"limits: {{max_minutes: {LIMITS.get(task or 'image', 10)}}}",
          f"models: [{{file: {endpoint}, licence: {licence}}}]     # fal.ai's licence type for it",
          f"# What a job costs (decision 0059), from fal.ai's pricing API on {today}: review it against",
          "# the model's page.",
          "cost:", *cost,
          "publish: false",
          "shotgrid: false"]
    if any(f.kind == "input" for f in params):
        L.append("work_resolution: 2048          # inputs over this are scaled down first: a smaller upload is quicker")
    L += ["", "params:"]
    for f in params:
        L.append(f"  {f.param}:")
        if f.kind == "input":
            L += ["    type: input", "    takes: image", f"    label: {_q(f.label)}"]
            if f.required:
                L.append("    required: true")
            if f.param == "input":
                L.append("    primary: true")
        else:
            L += [f"    type: {f.kind}", f"    label: {_q(f.label)}"]
            if f.required:
                L.append("    required: true")
            if f.choices is not None:
                L += _flow("choices", f.choices, 4)
            if f.default is not None:
                L.append(f"    default: {_q(f.default)}")
            if f.minimum is not None:
                L.append(f"    min: {_q(f.minimum)}")
            if f.maximum is not None:
                L.append(f"    max: {_q(f.maximum)}")
        L += _block("help", f.help, 4)
        L.append("")
    L += ["  output_path:", "    type: output_folder", "    label: Output folder",
          *_block("help", f"The result goes into a folder named after the shot inside this one, as <source>."
                          f"{tag_for(name)}_vNNN.<frame>.png. Leave empty for the show's renders folder. Never "
                          f"inside .published.", 4), ""]
    if wr:
        L += ["writes:", "  frames: {ext: png}", ""]
    L += ["runner:", "  type: api", f"  endpoint: {endpoint}",
          f"  upload: {{{', '.join(f'{p}: {n}' for p, n in upload.items())}}}",
          f"  send: {{{', '.join(f'{p}: {n}' for p, n in sends.items())}}}"]
    if left:
        L.append(f"  # Left to fal.ai's defaults: {', '.join(left)}.")
    if fixed:
        L.append("  # Never the artist's (decision 0062): the result comes back in fal.ai's answer (sync_mode).")
        L += ["  fixed:", *[f"    {k}: {_q(v)}" for k, v in fixed.items()]]
    else:
        L.append("  fixed: {}")
    return Draft(name=name, endpoint=endpoint, text="\n".join(L) + "\n", todo=todo)


def check(text: str, name: str) -> list[str]:
    """What the farm says of a draft, as it would of the committed definition: the head's
    problems and fal.ai's adapter's (runners/api/fal.check). Loaded from a folder of its own."""
    import tempfile
    from pathlib import Path

    from ...definitions import Definition
    from . import fal
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / name
        folder.mkdir()
        (folder / "process.yaml").write_text(text, encoding="utf-8")
        try:
            wf = Definition(folder)
        except Exception as e:  # noqa: BLE001 - what the farm can't read is what a reviewer must fix
            return [f"it doesn't load: {e}"]
        problems = list(wf.problems)
        if wf.api:
            problems += [p for p in fal.check(wf) if p not in problems]
            other = sorted(set(wf.writes) - {"frames"})
            if other:                   # the gateway's API runner says the same (ApiRunner.wiring)
                problems.append(f"writes.{other[0]}: an API definition writes frames only, for now")
        return problems


__all__ = ["Draft", "DraftError", "draft", "check", "name_for", "schemas", "fields", "writes"]
