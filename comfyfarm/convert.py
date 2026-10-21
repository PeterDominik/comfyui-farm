"""EXR sequences and movies -> the PNG frames the workflow reads.

One ComfyUI graph handles every input format: before a job is dispatched, the gateway
converts non-PNG inputs into a scratch folder on the farm machine, and the loader node
reads from there.

- EXR: oiiotool with the show's OCIO config and the shot's context (SEQ/SHOT), so the
  per-shot client LUT from _color_bundle is applied exactly as in the PNG preprocess.
- Movies (.mov/.mp4/...): ffmpeg, numbered from the movie's timecode. Treated as
  display-referred already (look baked in), so no colour transform.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

from .config import ConvertConfig, PathsConfig
from .paths import (Sequence, SequenceError, _pattern_regex, context_from_path, normalize, parse_input,
                    render, resolve_sequence, source_stem, to_farm, to_posix)

MOVIE_EXTENSIONS = {".mov", ".mp4", ".m4v", ".mxf"}


class ConvertError(RuntimeError):
    """Conversion failed while the job was running; the message is shown to the artist."""


# ============================================================== tools
def _pip_ffmpeg() -> str | None:
    """The ffmpeg binary that ships inside the imageio-ffmpeg wheel (no ffprobe)."""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        return exe if exe and os.path.isfile(exe) else None
    except Exception:  # noqa: BLE001 - not installed, or no binary for this platform
        return None


def find_tool(name: str, configured: str | list[str] | None = None) -> list[str] | None:
    """Configured command, else the venv next to this Python (pip-installed OpenImageIO
    puts oiiotool there), else ffmpeg from the imageio-ffmpeg package, else PATH."""
    if configured:
        return list(configured) if isinstance(configured, list) else [configured]
    here = Path(sys.executable).parent
    for cand in (here / f"{name}.exe", here / name):
        if cand.is_file():
            return [str(cand)]
    if name == "ffmpeg":
        exe = _pip_ffmpeg()
        if exe:
            return [exe]
    found = shutil.which(name)
    return [found] if found else None


def require_tool(cc: ConvertConfig, name: str) -> list[str]:
    cmd = find_tool(name, cc.tools.get(name))
    if not cmd:
        hint = {"oiiotool": "run scripts\\setup.ps1 (installs OpenImageIO into the gateway's venv)",
                "ffmpeg": "run scripts\\setup.ps1 (installs imageio-ffmpeg into the gateway's venv)",
                "ffprobe": "it is optional; set convert.tools.ffprobe to use one"}[name]
        raise SequenceError(f"{name} isn't available on the farm machine: {hint}.")
    return cmd


# ============================================================== timecode / movies
_TC = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})([:;.,])(\d{2,3})$")


def timecode_to_frame(tc: str, fps: float) -> int:
    """'00:00:41:17' at 24 fps -> 1001. Handles drop-frame (';') at 29.97/59.94."""
    m = _TC.match(tc.strip())
    if not m:
        raise ValueError(f"unreadable timecode {tc!r}")
    h, mi, se, sep, fr = int(m[1]), int(m[2]), int(m[3]), m[4], int(m[5])
    nominal = round(fps)
    frames = (h * 3600 + mi * 60 + se) * nominal + fr
    if sep in ";.," and nominal in (30, 60) and abs(fps - nominal) > 0.001:
        drop = 2 * nominal // 30
        minutes = h * 60 + mi
        frames -= drop * (minutes - minutes // 10)
    return frames


def parse_probe(data: dict) -> dict:
    """ffprobe JSON -> what we need from a movie."""
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    if video is None:
        raise SequenceError("The movie has no video stream.")
    rate = video.get("avg_frame_rate") if video.get("avg_frame_rate") not in (None, "0/0") \
        else video.get("r_frame_rate")
    try:
        fps = Fraction(rate)
    except (TypeError, ValueError, ZeroDivisionError):
        raise SequenceError("Couldn't read the movie's frame rate.")
    if fps <= 0:
        raise SequenceError("Couldn't read the movie's frame rate.")
    frames = None
    if str(video.get("nb_frames") or "").isdigit():
        frames = int(video["nb_frames"])
    else:
        dur = video.get("duration") or (data.get("format") or {}).get("duration")
        if dur:
            frames = round(float(dur) * fps)
    if not frames:
        raise SequenceError("Couldn't work out how many frames the movie has.")
    tc = (video.get("tags") or {}).get("timecode")
    if not tc:
        for s in streams:
            if s.get("codec_tag_string") == "tmcd" or s.get("codec_type") == "data":
                tc = (s.get("tags") or {}).get("timecode") or tc
    tc = tc or ((data.get("format") or {}).get("tags") or {}).get("timecode")
    return {"fps": float(fps), "fps_str": f"{fps.numerator}/{fps.denominator}", "frames": frames,
            "timecode": tc, "width": video.get("width"), "height": video.get("height"),
            "codec": video.get("codec_name"), "color_space": video.get("color_space")}


def probe_movie(path: str, ffprobe: list[str]) -> dict:
    try:
        out = subprocess.run([*ffprobe, "-v", "error", "-print_format", "json", "-show_streams",
                              "-show_format", path], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SequenceError(f"Couldn't read the movie with ffprobe: {e}")
    if out.returncode != 0:
        raise SequenceError(f"ffprobe can't read {path}: {out.stderr.strip()[-300:]}")
    return parse_probe(json.loads(out.stdout or "{}"))


# ffmpeg prints NTSC rates rounded; map them back to the exact fractions.
_NTSC = {"23.98": Fraction(24000, 1001), "29.97": Fraction(30000, 1001), "47.95": Fraction(48000, 1001),
         "59.94": Fraction(60000, 1001), "119.88": Fraction(120000, 1001)}
_COLOR_SPACES = {"bt709", "bt470bg", "smpte170m", "bt2020nc", "bt2020c", "smpte240m", "fcc", "ycgco"}


def parse_ffmpeg_info(text: str, packets: int | None) -> dict:
    """`ffmpeg -i file` stderr (+ a packet count) -> the same dict as parse_probe."""
    video = None
    for m in re.finditer(r"^\s*Stream #\d+:\d+.*?: Video: (.+)$", text, re.MULTILINE):
        if "attached pic" not in m.group(1):
            video = m.group(1)
            break
    if video is None:
        raise SequenceError("The movie has no video stream.")
    rate = re.search(r", ([\d.]+) fps", video) or re.search(r", ([\d.]+) tbr", video)
    if not rate:
        raise SequenceError("Couldn't read the movie's frame rate.")
    fps = _NTSC.get(rate.group(1)) or Fraction(rate.group(1)).limit_denominator(1001)
    size = re.search(r", (\d{2,5})x(\d{2,5})\b", video)
    color = None
    pix = re.search(r"^\w+[^,]*?, \w+\(([^)]*)\)", video)
    if pix:
        for token in re.split(r"[,/\s]+", pix.group(1)):
            if token in _COLOR_SPACES:
                color = token
                break
    tc = re.search(r"^\s*timecode\s*:\s*(\d{1,2}:\d{2}:\d{2}[:;.,]\d{2,3})\s*$", text, re.MULTILINE)
    frames = packets
    if not frames:
        dur = re.search(r"Duration: (\d+):(\d+):([\d.]+)", text)
        if dur:
            frames = round((int(dur[1]) * 3600 + int(dur[2]) * 60 + float(dur[3])) * fps)
    if not frames:
        raise SequenceError("Couldn't work out how many frames the movie has.")
    return {"fps": float(fps), "fps_str": f"{fps.numerator}/{fps.denominator}", "frames": frames,
            "timecode": tc.group(1) if tc else None, "width": int(size[1]) if size else None,
            "height": int(size[2]) if size else None, "codec": video.split()[0].strip(","),
            "color_space": color}


def probe_movie_ffmpeg(path: str, ffmpeg: list[str]) -> dict:
    """Movie info without ffprobe (pip's ffmpeg doesn't include it): stream details from
    `ffmpeg -i`, and the exact frame count from a packet listing (no decoding)."""
    try:
        info = subprocess.run([*ffmpeg, "-hide_banner", "-nostdin", "-i", path],
                              capture_output=True, text=True, timeout=60)
        count = subprocess.run([*ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", path,
                                "-map", "0:v:0", "-c", "copy", "-f", "framecrc", "-"],
                               capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SequenceError(f"Couldn't read the movie with ffmpeg: {e}")
    if "Stream #" not in info.stderr:
        raise SequenceError(f"ffmpeg can't read {path}: {info.stderr.strip()[-300:]}")
    packets = sum(1 for line in count.stdout.splitlines() if line and not line.startswith("#")) \
        if count.returncode == 0 else None
    return parse_ffmpeg_info(info.stderr, packets)


def read_movie_info(path: str, cc: ConvertConfig) -> dict:
    """ffprobe when it's installed, else ffmpeg alone."""
    ffprobe = find_tool("ffprobe", cc.tools.get("ffprobe"))
    if ffprobe:
        return probe_movie(path, ffprobe)
    return probe_movie_ffmpeg(path, require_tool(cc, "ffmpeg"))


def resolve_movie(raw: str, pc: PathsConfig, cc: ConvertConfig, show: str | None = None,
                  shot: str | None = None) -> Sequence:
    path, first, last = parse_input(raw)
    farm_path = to_farm(path, pc.mappings)
    if not os.path.isfile(farm_path):
        raise SequenceError(f"{to_posix(farm_path, pc.mappings) or farm_path} doesn't exist, or the farm "
                            f"machine can't see it.")
    directory, _, filename = farm_path.rpartition("/")
    ctx = context_from_path(farm_path, pc.context_patterns, pc.shot_name)
    info = read_movie_info(farm_path, cc)
    notes = []
    if info["timecode"]:
        try:
            start = timecode_to_frame(info["timecode"], info["fps"])
        except ValueError as e:
            raise SequenceError(f"The movie's timecode can't be used: {e}.")
    else:
        start = cc.movie.default_start
        notes.append(f"The movie has no timecode, so frames are numbered from {start}.")
    end = start + info["frames"] - 1
    if first is None:
        first, last = start, end
    elif first < start or last > end:
        raise SequenceError(f"Frames {first}-{last} are outside the movie, which runs {start}-{end} "
                            f"(from its timecode {info['timecode'] or 'none'}).")
    seq = Sequence(raw=raw.strip(), farm_path=farm_path, directory=directory + "/", filename=filename,
                   first=first, last=last, frame_count=last - first + 1, skip=0, cap=0,
                   show=show or ctx.get("show"), shot=shot or ctx.get("shot"), seq_name=ctx.get("seq"),
                   source=source_stem(filename, ""), kind="movie")
    seq.convert = {"kind": "movie", "src": farm_path, "start": start, "end": end,
                   "index_first": first - start, "index_last": last - start, "notes": notes, **info}
    return seq


# ============================================================== OCIO
def _shot_short(seq_name: str | None, shot: str | None) -> str | None:
    if shot and seq_name and shot.lower().startswith(seq_name.lower() + "_") and len(shot) > len(seq_name) + 1:
        return shot[len(seq_name) + 1:]
    return shot


def _search_path(config_text: str) -> list[str]:
    m = re.search(r'^search_path:\s*"?([^"\n]*)"?\s*$', config_text, re.MULTILINE)
    return [p for p in (m.group(1).split(":") if m else []) if p]


def ocio_setup(seq: Sequence, pc: PathsConfig, cc: ConvertConfig) -> dict:
    """Which config, context and look an EXR job uses, checked before it's queued."""
    ex = cc.exr
    tokens = {"show": seq.show, "seq": seq.seq_name, "shot": seq.shot,
              "shot_short": _shot_short(seq.seq_name, seq.shot)}
    if not seq.show:
        raise SequenceError("Couldn't work out the show from the input path, so there's no OCIO config "
                            "for the EXR conversion.")
    config = to_farm(render(ex.ocio_config, "OCIO config path", **tokens), pc.mappings)
    if pc.validate_inputs and os.path.isdir(config):
        config = config.rstrip("/") + "/config.ocio"
    context, notes = {}, []
    for key, template in ex.context.items():
        try:
            context[key] = render(template, "OCIO context", **tokens)
        except SequenceError:
            pass   # not in a publish path: OCIO falls back to the show-level LUTs
    if len(context) < len(ex.context):
        notes.append("No seq/shot in the input path, so the EXRs get the show's default LUTs.")
    bundle = None
    if pc.validate_inputs:
        try:
            text = Path(config).read_text(encoding="utf-8", errors="replace")
        except OSError:
            raise SequenceError(f"No OCIO config at {to_posix(config, pc.mappings) or config}.")
        if not re.search(r"\{\s*name:\s*" + re.escape(ex.view) + r"\s*,", text):
            raise SequenceError(f"The view '{ex.view}' isn't in {to_posix(config, pc.mappings) or config}.")
        # Report which context-dependent search path (the shot/seq colour bundle) is used.
        base = config.rpartition("/")[0]
        for entry in _search_path(text):
            if "${" not in entry:
                continue
            expanded = re.sub(r"\$\{(\w+)\}", lambda m: context.get(m[1], "\0"), entry)
            if "\0" in expanded:
                continue
            path = normalize(os.path.normpath(os.path.join(base, expanded)))
            if os.path.isdir(path):
                bundle = path
                break
        if bundle is None and context:
            notes.append("This shot has no colour bundle, so the EXRs get the show's default LUTs.")
    return {"config": config, "context": context, "bundle": bundle, "input_colorspace": ex.input_colorspace,
            "display": ex.display, "view": ex.view, "extra_args": list(ex.extra_args), "notes": notes}


# ============================================================== resolve
def resolve_input(raw: str, pc: PathsConfig, cc: ConvertConfig, show: str | None = None,
                  shot: str | None = None) -> Sequence:
    """Any supported input -> a Sequence. Converted kinds carry `convert` details."""
    if not raw or not raw.strip():
        raise SequenceError("No input sequence given.")
    path, _, _ = parse_input(raw)
    if os.path.splitext(path)[1].lower() in MOVIE_EXTENSIONS:
        return resolve_movie(raw, pc, cc, show, shot)
    seq = resolve_sequence(raw, pc, show=show, shot=shot)
    if seq.kind == "exr":
        if pc.validate_inputs:
            require_tool(cc, "oiiotool")
        seq.convert = {"kind": "exr", "src_dir": seq.directory, "src_pattern": seq.filename,
                       "first": seq.first, "last": seq.last, **ocio_setup(seq, pc, cc)}
    return seq


def point_loader_at_scratch(seq: Sequence, scratch_root: str, job_id: str) -> None:
    """Converted frames go to <scratch>/<job id>/<source>.<frame>.png; the loader reads
    exactly those, so no skip/cap. Padding keeps name order = frame order."""
    d = normalize(f"{scratch_root}/{job_id}") + "/"
    width = max(4, len(str(seq.last if seq.last is not None else 1)))
    seq.load_dir = d
    seq.skip = seq.cap = 0
    seq.convert.update(dst_dir=d, dst_pattern=f"{seq.source}.%0{width}d.png")


def summary(seq: Sequence | None, pc: PathsConfig) -> dict | None:
    """What the dashboard shows about a conversion."""
    if not seq or not seq.convert:
        return None
    c = seq.convert
    if c["kind"] == "exr":
        return {"kind": "exr", "display": c["display"], "view": c["view"],
                "input_colorspace": c["input_colorspace"], "context": c["context"],
                "config": to_posix(c["config"], pc.mappings) or c["config"],
                "bundle": (to_posix(c["bundle"], pc.mappings) or c["bundle"]) if c.get("bundle") else None}
    return {"kind": "movie", "timecode": c.get("timecode"), "fps": c["fps"], "start": c["start"],
            "end": c["end"], "codec": c.get("codec"), "width": c.get("width"), "height": c.get("height")}


# ============================================================== run
async def _run(cmd: list[str], env: dict | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, env=env)
    try:
        out, err = await proc.communicate()
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, (err or b"").decode(errors="replace") + (out or b"").decode(errors="replace")


def exr_command(oiiotool: list[str], c: dict, src: str, dst: str, input_colorspace: str | None = None,
                view: str | None = None) -> list[str]:
    """The oiiotool call for one EXR frame (also used by `comfyfarm color-debug`)."""
    return [*oiiotool, "--colorconfig", c["config"], src, *c["extra_args"],
            f"--ociodisplay:from={input_colorspace or c['input_colorspace']}", c["display"], view or c["view"],
            "-d", "uint8", "-o", dst]


async def convert_exr(c: dict, cc: ConvertConfig, progress) -> None:
    oiiotool = require_tool(cc, "oiiotool")
    try:
        names = os.listdir(c["src_dir"])
    except OSError as e:
        raise ConvertError(f"Can't open {c['src_dir']}: {e}")
    rx = _pattern_regex(c["src_pattern"])
    by_frame = {int(m.group(1)): n for n, m in ((n, rx.match(n)) for n in names) if m} if rx \
        else {c["first"]: c["src_pattern"]}
    frames = list(range(c["first"], c["last"] + 1))
    missing = [f for f in frames if f not in by_frame]
    if missing:
        raise ConvertError(f"Frames disappeared since submitting: {missing[:10]}")
    env = dict(os.environ, **c["context"])
    sem = asyncio.Semaphore(max(1, cc.frame_workers))
    done = 0

    async def one(frame: int) -> None:
        nonlocal done
        src = c["src_dir"] + by_frame[frame]
        dst = c["dst_dir"] + (c["dst_pattern"] % frame)
        cmd = exr_command(oiiotool, c, src, dst)
        async with sem:
            code, out = await _run(cmd, env)
        if code != 0 or not os.path.isfile(dst):
            raise ConvertError(f"oiiotool failed on {by_frame[frame]}: {out.strip()[-600:] or f'exit {code}'}")
        done += 1
        progress(done, len(frames))

    tasks = [asyncio.create_task(one(f)) for f in frames]
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def convert_movie(c: dict, cc: ConvertConfig, first: int, count: int, progress) -> None:
    ffmpeg = require_tool(cc, "ffmpeg")
    vf = f"select='between(n,{c['index_first']},{c['index_last']})'"
    if not c.get("color_space") or c["color_space"] in ("unknown", "reserved"):
        vf += ",scale=in_color_matrix=bt709"   # untagged HD: ffmpeg would otherwise assume BT.601
    cmd = [*ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y", "-i", c["src"], "-vf", vf,
           "-fps_mode", "passthrough", "-start_number", str(first), "-pix_fmt", "rgb24",
           "-progress", "pipe:1", c["dst_dir"] + c["dst_pattern"]]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    try:
        async for line in proc.stdout:
            m = re.match(rb"frame=(\d+)", line)
            if m:
                progress(min(int(m[1]), count), count)
        err = (await proc.stderr.read()).decode(errors="replace")
        code = await proc.wait()
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    written = len([n for n in os.listdir(c["dst_dir"]) if n.endswith(".png")])
    if code != 0 or written != count:
        raise ConvertError(f"ffmpeg wrote {written} of {count} frames: {err.strip()[-600:] or f'exit {code}'}")


async def convert(seq_info: dict, cc: ConvertConfig, progress) -> None:
    """Run the conversion described in a job's info['input']['convert']."""
    c = seq_info["convert"]
    shutil.rmtree(c["dst_dir"], ignore_errors=True)
    os.makedirs(c["dst_dir"], exist_ok=True)
    if c["kind"] == "exr":
        await convert_exr(c, cc, progress)
    elif c["kind"] == "movie":
        await convert_movie(c, cc, seq_info["first"], seq_info["frame_count"], progress)
    else:
        raise ConvertError(f"Unknown conversion {c['kind']}")


__all__ = ["MOVIE_EXTENSIONS", "ConvertError", "find_tool", "timecode_to_frame", "parse_probe",
           "parse_ffmpeg_info", "probe_movie_ffmpeg", "read_movie_info",
           "resolve_input", "point_loader_at_scratch", "summary", "convert"]
