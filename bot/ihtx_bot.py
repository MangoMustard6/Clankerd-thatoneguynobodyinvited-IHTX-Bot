"""
IHTX Bot — I Hate The X FFmpeg Discord Bot

All effects go through one unified command:
  !ihtx effect1=val,effect2=val,...

Pipe syntax (comma-separated key=value):
  chaos, glitch, shake, rainbow, static, melt, corrupt
        =true  or  =N  (N passes of that effect)
  huehsv=0.5            hue amount 0-1
  pinch=1;0.5;0.5;0.5   strength;radius;cx;cy  (all optional, defaults shown)
  pitch=0;7;12          semicolon-separated semitones (multipitch = mixed together)
  reverse=true          reverse video + audio (applied once at end)
  rep=N                 number of render cycles (default 1)
  duration=N            seconds per segment (default 0.5)
  concat=true           TRUE IHTX MODE — render→render→concat
                        each rep re-encodes the previous segment (artifacts compound),
                        then all segments are joined: total = rep × duration seconds
"""

import discord
from discord.ext import commands
import asyncio
import os
import re
import tempfile
import subprocess
import aiohttp
import sys
from pathlib import Path

TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    print("ERROR: DISCORD_TOKEN environment variable not set.", file=sys.stderr)
    sys.exit(1)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

SUPPORTED_EXTENSIONS  = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".gif", ".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXTENSIONS      = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".gif"}
AUDIO_VIDEO_EXTS      = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
MAX_FILE_SIZE         = 25 * 1024 * 1024
MAX_REPETITIONS       = 100
MAX_DURATION          = 600

# ─── Effect filter definitions ────────────────────────────────────────────────

_BASE_NOISE = "noise=alls=40:allf=t+u"
_SHAKE      = "crop=iw-20:ih-20:10+5*sin(t*30):10+5*cos(t*17),scale=iw+20:ih+20"
_CHROMAB = (
    "[IN]split=3[r][g][b];"
    "[r]lutrgb=r=val:g=0:b=0,pad=iw+6:ih:3:0[ro];"
    "[g]lutrgb=r=0:g=val:b=0[go];"
    "[b]lutrgb=r=0:g=0:b=val,pad=iw+6:ih:0:0[bo];"
    "[ro][go]blend=all_mode=addition[rg];"
    "[rg][bo]blend=all_mode=addition[OUT]"
)

# vf = simple -vf chain; complex = -filter_complex graph using [0:v] as input
PRESET_FILTERS: dict[str, dict] = {
    "chaos": {
        "vf": f"{_SHAKE},{_BASE_NOISE},hue=h=t*180:s=2,eq=contrast=1.5:brightness=0.05:saturation=3",
        "complex": None,
    },
    "glitch": {
        "vf": f"rgbashift=rh=8:rv=-8:gh=-4:gv=4:bh=6:bv=-6,{_BASE_NOISE},eq=contrast=1.8:saturation=0",
        "complex": None,
    },
    "shake": {
        "vf": f"{_SHAKE},{_BASE_NOISE},eq=contrast=1.3:saturation=1.5",
        "complex": None,
    },
    "rainbow": {
        "vf": None,
        "complex": (
            "[0:v]split=3[r][g][b];"
            "[r]lutrgb=r=val:g=0:b=0,pad=iw+6:ih:3:0[ro];"
            "[g]lutrgb=r=0:g=val:b=0[go];"
            "[b]lutrgb=r=0:g=0:b=val,pad=iw+6:ih:0:0[bo];"
            "[ro][go]blend=all_mode=addition[rg];"
            "[rg][bo]blend=all_mode=addition"
        ),
    },
    "static": {
        "vf": f"{_BASE_NOISE},curves=vintage,eq=contrast=1.2",
        "complex": None,
    },
    "melt": {
        "vf": (
            "perspective=x0=0:y0=0:x1=iw:y1=20*sin(t*3)"
            ":x2=0:y2=ih:x3=iw:y3=ih-20*sin(t*3),"
            + _BASE_NOISE
        ),
        "complex": None,
    },
    "corrupt": {
        "vf": f"drawgrid=x=0:y=0:w=iw:h=5:t=1:color=white@0.1,{_BASE_NOISE},eq=gamma=1.5:saturation=0.3:contrast=2",
        "complex": None,
    },
}

VISUAL_PRESETS = set(PRESET_FILTERS.keys())

HELP_TEXT = """\
**I Hate The X — IHTX Bot**
One command, pipe-style syntax:

`!ihtx effect=value,effect=value,...`

**Visual effects** (value = `true` or N passes):
`chaos` `glitch` `shake` `rainbow` `static` `melt` `corrupt`

**Parameterised effects:**
`huehsv=0.5` — hue shift via hald-CLUT (0–2)
`pinch=1;0.5;0.5;0.5` — strength;radius;cx;cy (all optional)
`pitch=0;7;12` — semitones `;`-separated (multipitch = mixed)
`reverse=true` — reverse video + audio (applied last)
`hue2=90` — secondary hue rotation in degrees + premultiply blend

**Wave distortion:**
`wave=hs;hf;ha;hp;vs;vf;va;vp;separate;noclip`
  h = horizontal  v = vertical  s=speed f=freq a=amp p=phase
  separate=true — two geq passes instead of one combined
  noclip=true   — drawbox border to suppress edge wrap artifacts
  Example: `wave=1;1;1;0;0.5;1;0.5;0` (horizontal wobble + light vertical)

**TV / CRT simulator:**
`tv=line_sync;zoom_grill;vertical`
  line_sync 0–1 — hsync degradation (0=strong contrast, 1=flat)
  zoom_grill >0 — scanline width (1=tight, 2=wide bands)
  vertical=true — vertical bars instead of horizontal scanlines
  Example: `tv=0.3;1;false`

**Mirror:**
`hmirror=1` — left half reflected right (left mirror)
`hmirror=2` — right half reflected left (right mirror)
`vmirror=1` — top half reflected down  (top mirror)
`vmirror=2` — bottom half reflected up (bottom mirror)

**Global options:**
`rep=N` — render cycles (default 1)
`duration=N` — seconds per segment (default 0.5)
`concat=true` — **TRUE IHTX MODE** ✦
  Each rep re-encodes from the *previous* render (artifacts compound).
  All segments joined → total = rep × duration seconds.
  Escalates from slightly degraded → pure chaos.

**Examples:**
`!ihtx chaos=true`
`!ihtx wave=1;2;1;0;1;1;0.5;0`
`!ihtx tv=0.2;1.5,hmirror=1`
`!ihtx glitch=true,concat=true,rep=20,duration=0.5`
`!ihtx wave=1;1;1,concat=true,rep=15,duration=0.4`
`!ihtx hmirror=1,vmirror=1,hue2=180`
"""


# ─── Pipe parser ──────────────────────────────────────────────────────────────

def parse_pipe(pipe_str: str) -> list[tuple[str, str]]:
    """
    Parse 'shake=true,glitch=3,pitch=0;7;12' into ordered [(key, raw_val), ...].
    Preserves order for effect sequencing.
    """
    entries = []
    for part in re.split(r"\s*,\s*", pipe_str.strip()):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            key, _, val = part.partition("=")
            entries.append((key.strip().lower(), val.strip()))
        else:
            entries.append((part.lower(), "true"))
    return entries


def is_true(val: str) -> bool:
    return val.strip().lower() in ("true", "1", "yes", "on")


def extract_globals(entries: list[tuple[str, str]]) -> tuple[int, float, bool]:
    """Extract rep, duration, and concat from entries."""
    rep      = 1
    duration = 0.5
    concat   = False
    for k, v in entries:
        if k in ("rep", "repetitions"):
            try:
                rep = max(1, min(MAX_REPETITIONS, int(v)))
            except ValueError:
                pass
        elif k == "duration":
            try:
                duration = max(0.1, min(MAX_DURATION, float(v)))
            except ValueError:
                pass
        elif k == "concat":
            concat = is_true(v)
    return rep, duration, concat


def build_steps(entries: list[tuple[str, str]]) -> list[dict]:
    """Convert ordered entries into step dicts (skip rep/duration/unknown/false)."""
    steps = []
    for key, val in entries:
        if key in ("rep", "repetitions", "duration", "concat"):
            continue

        if key in VISUAL_PRESETS:
            if is_true(val):
                passes = 1
            else:
                try:
                    passes = max(1, min(MAX_REPETITIONS, int(val)))
                except ValueError:
                    continue
            steps.append({"type": "preset", "name": key, "passes": passes})

        elif key == "huehsv":
            try:
                amount = max(0.0, min(2.0, float(val)))
            except ValueError:
                amount = 0.5
            steps.append({"type": "huehsv", "amount": amount})

        elif key == "pinch":
            parts = [p.strip() for p in val.split(";")]
            try:
                strength = float(parts[0]) if len(parts) > 0 else 1.0
                radius   = max(0.01, float(parts[1])) if len(parts) > 1 else 0.5
                cx       = max(0.0, min(1.0, float(parts[2]))) if len(parts) > 2 else 0.5
                cy       = max(0.0, min(1.0, float(parts[3]))) if len(parts) > 3 else 0.5
            except ValueError:
                strength, radius, cx, cy = 1.0, 0.5, 0.5, 0.5
            steps.append({"type": "pinch", "strength": strength, "radius": radius, "cx": cx, "cy": cy})

        elif key == "pitch":
            raw_parts = [p.strip() for p in val.split(";")]
            semitones = []
            for p in raw_parts:
                try:
                    semitones.append(max(-36.0, min(36.0, float(p))))
                except ValueError:
                    pass
            if semitones:
                steps.append({"type": "pitch", "semitones": semitones[:8]})

        elif key == "reverse":
            if is_true(val):
                steps.append({"type": "reverse"})

        elif key == "wave":
            p = [x.strip() for x in val.split(";")]
            def _wfp(i, d):
                try: return float(p[i]) if len(p) > i else d
                except ValueError: return d
            def _wbp(i, d=False):
                if len(p) <= i: return d
                return p[i].lower() in ("1","true","t","y","yes","+","on")
            steps.append({
                "type": "wave",
                "hs": _wfp(0, 1.0), "hf": _wfp(1, 1.0), "ha": _wfp(2, 1.0), "hp": _wfp(3, 0.0),
                "vs": _wfp(4, 0.0), "vf": _wfp(5, 0.0), "va": _wfp(6, 0.0), "vp": _wfp(7, 0.0),
                "separate": _wbp(8), "noclip": _wbp(9),
            })

        elif key == "tv":
            p = [x.strip() for x in val.split(";")]
            def _tvfp(i, d):
                try: return float(p[i]) if len(p) > i else d
                except ValueError: return d
            def _tvbp(i, d=False):
                if len(p) <= i: return d
                return p[i].lower() in ("1","true","t","y","yes","+","on")
            steps.append({
                "type": "tv",
                "line_sync":  max(0.0, min(1.0, _tvfp(0, 0.25))),
                "zoom_grill": max(0.1, _tvfp(1, 1.0)),
                "vertical":   _tvbp(2),
            })

        elif key == "hmirror":
            try: side = max(1, min(2, int(val)))
            except ValueError: side = 1
            steps.append({"type": "hmirror", "side": side})

        elif key == "vmirror":
            try: side = max(1, min(2, int(val)))
            except ValueError: side = 1
            steps.append({"type": "vmirror", "side": side})

        elif key == "hue2":
            try: degrees = max(-360.0, min(360.0, float(val)))
            except ValueError: degrees = 0.0
            steps.append({"type": "hue2", "degrees": degrees})

    return steps


# ─── Effect builders ──────────────────────────────────────────────────────────

def _build_pinch_vf(strength: float, radius: float, cx: float, cy: float) -> str:
    gauss_arg = (
        f"-3.3333*pow(hypot("
        f"(X-W*{cx})/(W*{radius}),"
        f"(Y-H*{cy})/(H*{radius})"
        f"),2)"
    )
    px = f"W*{cx}+(X-W*{cx})*(1-({strength})*gauss({gauss_arg}))"
    py = f"H*{cy}+(Y-H*{cy})*(1-({strength})*gauss({gauss_arg}))"
    return f"format=yuv444p,geq='p({px},{py})',scale=iw:ih,format=yuv420p"


def _get_video_dims(path: str) -> tuple[int, int]:
    """Return (width, height) of the first video stream; defaults to 640×640."""
    try:
        r = subprocess.run([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0:nk=1", path,
        ], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            w, h = r.stdout.strip().split(",")
            return int(w), int(h)
    except Exception:
        pass
    return 640, 640


def _build_wave_vf(
    src: str,
    hs: float, hf: float, ha: float, hp: float,
    vs: float, vf_: float, va: float, vp: float,
    separate: bool, noclip: bool,
) -> str:
    """
    Sine-wave spatial displacement.
    h_off = sin(T*5*hs + hp*15 + Y/H*PI*hf) * -15*ha   (horizontal shift)
    v_off = sin(T*5*vs + vp*15 + X/W*PI*vf) * -15*va   (vertical shift)
    """
    w, h = _get_video_dims(src)
    h_off = f"(sin((T*5*{hs}+({hp}*15))+(Y/H)*(PI*{hf})))*(-15*{ha})"
    v_off = f"(sin((T*5*{vs}+({vp}*15))+(X/W)*(PI*{vf_})))*(-15*{va})"
    prefix = "drawbox=t=1," if noclip else ""
    if separate:
        geq_str = f"geq='p(X-({h_off}),Y)',geq='p(X,Y-({v_off}))'"
    else:
        geq_str = f"geq='p(X-({h_off}),Y-({v_off}))'"
    return (
        f"{prefix}format=yuv444p,scale=640:640,"
        f"{geq_str},"
        f"scale={w}:{h},setsar=1:1,format=yuv420p"
    )


def _build_tv_vf(line_sync: float, zoom_grill: float, vertical: bool) -> str:
    """
    CRT/TV simulator.
    line_sync  0–1  — amount of hsync distortion (contrast degradation)
    zoom_grill >0   — scanline pitch (1 = 1 line per 2px, 2 = wider bands)
    vertical        — rotate scanlines 90° (vertical bars instead of horizontal)
    """
    contrast = (1.0 - line_sync) * 2.366666
    freq      = 2.0 / max(0.01, zoom_grill)
    if vertical:
        luma = f"lum(X,Y)*(0.65+0.35*sin(X*{freq:.6f}*PI))"
    else:
        luma = f"lum(X,Y)*(0.65+0.35*sin(Y*{freq:.6f}*PI))"
    return (
        f"format=yuv444p,"
        f"eq=contrast={contrast:.6f},"
        f"hue=b=-0.033,"
        f"geq='lum={luma}',"
        f"format=yuv420p"
    )


def _build_mirror_complex(axis: str, side: int) -> str:
    """
    axis='h', side=1 → left half reflected to fill right  (left mirror)
    axis='h', side=2 → right half reflected to fill left  (right mirror)
    axis='v', side=1 → top half reflected to fill bottom  (top mirror)
    axis='v', side=2 → bottom half reflected to fill top  (bottom mirror)
    """
    if axis == "h":
        if side == 1:
            return (
                "[0:v]split[a][b];"
                "[a]crop=iw/2:ih:0:0[lft];"
                "[lft]hflip[rgt];"
                "[lft][rgt]hstack"
            )
        else:
            return (
                "[0:v]split[a][b];"
                "[a]crop=iw/2:ih:iw/2:0[rgt];"
                "[rgt]hflip[lm];"
                "[lm][rgt]hstack"
            )
    else:  # v
        if side == 1:
            return (
                "[0:v]split[a][b];"
                "[a]crop=iw:ih/2:0:0[top];"
                "[top]vflip[bot];"
                "[top][bot]vstack"
            )
        else:
            return (
                "[0:v]split[a][b];"
                "[a]crop=iw:ih/2:0:ih/2[bot];"
                "[bot]vflip[tm];"
                "[tm][bot]vstack"
            )


def _build_huehsv_clut(tmpdir: str, amount: float) -> str:
    hue_val   = int(amount * 200 + 100)
    clut_path = os.path.join(tmpdir, f"hald_clut_{int(amount*1000)}.png")
    if not os.path.exists(clut_path):
        r = subprocess.run(
            ["magick", "hald:8", "-modulate", f"100,100,{hue_val}", clut_path],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            raise RuntimeError(f"ImageMagick: {r.stderr[:500]}")
    return clut_path


# ─── FFmpeg runners ───────────────────────────────────────────────────────────

def _run(cmd: list[str], timeout: int = 180) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (True, "") if r.returncode == 0 else (False, r.stderr[-2000:])
    except subprocess.TimeoutExpired:
        return False, f"FFmpeg timed out (>{timeout}s)"
    except Exception as e:
        return False, str(e)


def _apply_vf(src: str, dst: str, vf: str, is_video: bool, duration: int) -> tuple[bool, str]:
    if is_video:
        cmd = ["ffmpeg", "-y", "-i", src,
               "-vf", vf,
               "-c:v", "libx264", "-preset", "fast", "-crf", "23",
               "-c:a", "copy", "-t", str(duration), dst]
    else:
        pal = ",split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"
        cmd = ["ffmpeg", "-y", "-loop", "1", "-i", src,
               "-vf", vf + pal, "-t", "3", dst]
    return _run(cmd)


def _apply_complex(src: str, dst: str, fc: str, is_video: bool, duration: int) -> tuple[bool, str]:
    if is_video:
        cmd = ["ffmpeg", "-y", "-i", src,
               "-filter_complex", fc,
               "-c:v", "libx264", "-preset", "fast", "-crf", "23",
               "-c:a", "copy", "-t", str(duration), dst]
    else:
        pal = ",split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"
        cmd = ["ffmpeg", "-y", "-loop", "1", "-i", src,
               "-filter_complex", fc + pal, "-t", "3", dst]
    return _run(cmd)


def _apply_haldclut(src: str, clut: str, dst: str, is_video: bool, duration: int) -> tuple[bool, str]:
    if is_video:
        cmd = ["ffmpeg", "-y", "-i", src, "-i", clut,
               "-filter_complex", "[0:v][1:v]haldclut",
               "-c:v", "libx264", "-preset", "fast", "-crf", "23",
               "-c:a", "copy", "-t", str(duration), dst]
    else:
        pal = ",split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"
        cmd = ["ffmpeg", "-y", "-loop", "1", "-i", src, "-i", clut,
               "-filter_complex", f"[0:v][1:v]haldclut{pal}", "-t", "3", dst]
    return _run(cmd)


def _apply_pitch(src: str, dst: str, semitones: list[float]) -> tuple[bool, str]:
    n  = len(semitones)
    fc = f"[0:a]asplit={n}" + "".join(f"[a{i}]" for i in range(n)) + ";"
    fc += ";".join(
        f"[a{i}]rubberband=pitch={2 ** (st / 12):.6f}[p{i}]"
        for i, st in enumerate(semitones)
    ) + ";"
    fc += "".join(f"[p{i}]" for i in range(n)) + f"amix=inputs={n}:normalize=0[aout]"
    cmd = ["ffmpeg", "-y", "-i", src,
           "-filter_complex", fc,
           "-map", "0:v", "-map", "[aout]",
           "-c:v", "copy", "-t", "300", dst]
    return _run(cmd, timeout=300)


def _apply_reverse(src: str, dst: str, is_video: bool) -> tuple[bool, str]:
    if is_video:
        cmd = ["ffmpeg", "-y", "-i", src,
               "-vf", "reverse", "-af", "areverse",
               "-c:v", "libx264", "-preset", "fast", "-crf", "23",
               dst]
    else:
        cmd = ["ffmpeg", "-y", "-i", src,
               "-vf", "reverse", dst]
    return _run(cmd, timeout=300)


def _apply_step(
    step: dict, src: str, dst: str, tmpdir: str,
    is_video: bool, duration: int,
    step_idx: int,
) -> tuple[bool, str]:
    """Apply a single pipeline step: src → dst."""
    t = step["type"]

    if t == "preset":
        cfg = PRESET_FILTERS[step["name"]]
        if cfg["complex"]:
            return _apply_complex(src, dst, cfg["complex"], is_video, duration)
        return _apply_vf(src, dst, cfg["vf"], is_video, duration)

    elif t == "huehsv":
        try:
            clut = _build_huehsv_clut(tmpdir, step["amount"])
        except RuntimeError as e:
            return False, str(e)
        return _apply_haldclut(src, clut, dst, is_video, duration)

    elif t == "pinch":
        vf = _build_pinch_vf(step["strength"], step["radius"], step["cx"], step["cy"])
        return _apply_vf(src, dst, vf, is_video, duration)

    elif t == "pitch":
        if not is_video:
            # No audio in images — skip gracefully by copying
            import shutil; shutil.copy2(src, dst)
            return True, ""
        return _apply_pitch(src, dst, step["semitones"])

    elif t == "reverse":
        return _apply_reverse(src, dst, is_video)

    elif t == "wave":
        vf = _build_wave_vf(
            src,
            step["hs"], step["hf"], step["ha"], step["hp"],
            step["vs"], step["vf"], step["va"], step["vp"],
            step["separate"], step["noclip"],
        )
        return _apply_vf(src, dst, vf, is_video, duration)

    elif t == "tv":
        vf = _build_tv_vf(step["line_sync"], step["zoom_grill"], step["vertical"])
        return _apply_vf(src, dst, vf, is_video, duration)

    elif t == "hmirror":
        fc = _build_mirror_complex("h", step["side"])
        return _apply_complex(src, dst, fc, is_video, duration)

    elif t == "vmirror":
        fc = _build_mirror_complex("v", step["side"])
        return _apply_complex(src, dst, fc, is_video, duration)

    elif t == "hue2":
        vf = f"hue=h={step['degrees']},frei0r=premultiply"
        return _apply_vf(src, dst, vf, is_video, duration)

    return False, f"Unknown step type: {t}"


def execute_pipeline(
    input_path: str, output_path: str, tmpdir: str,
    steps: list[dict], is_video: bool, rep: int, duration: float,
) -> tuple[bool, str]:
    """
    Run the pipeline.
    - Non-reverse steps run rep times.
    - Reverse steps (if any) run once at the very end.
    """
    # Separate reverse from everything else
    main_steps    = [s for s in steps if s["type"] != "reverse"]
    has_reverse   = any(s["type"] == "reverse" for s in steps)

    ext     = Path(input_path).suffix
    cur     = input_path
    counter = 0

    all_main = main_steps * rep   # repeat pipeline N times

    for i, step in enumerate(all_main):
        is_last_main = (i == len(all_main) - 1) and not has_reverse
        nxt = output_path if is_last_main else os.path.join(tmpdir, f"s{counter}{ext}")
        counter += 1
        ok, err = _apply_step(step, cur, nxt, tmpdir, is_video, duration, i)
        if not ok:
            name = step.get("name", step["type"])
            return False, f"Step {i+1}/{len(all_main)} ({name}) failed:\n{err}"
        cur = nxt

    if has_reverse:
        ok, err = _apply_reverse(cur, output_path, is_video)
        if not ok:
            return False, f"Reverse failed:\n{err}"

    # Edge case: no steps at all, just copy input to output
    if not all_main and not has_reverse:
        import shutil; shutil.copy2(input_path, output_path)

    return True, ""


def _render_to_ts(
    src: str, dst: str, tmpdir: str,
    steps: list[dict], is_source_video: bool,
    duration: float, seg_idx: int,
) -> tuple[bool, str]:
    """
    Render src through all steps → dst as MPEG-TS (H.264+AAC).
    Images are first looped into a short video.
    TS format is required for the byte-level concat protocol.
    """
    # Images → looping video with silent audio so TS has consistent streams
    if not is_source_video:
        loop_mp4 = os.path.join(tmpdir, f"loop_{seg_idx}.mp4")
        ok, err = _run([
            "ffmpeg", "-y",
            "-loop", "1", "-i", src,
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac",
            "-t", str(duration), "-shortest",
            loop_mp4,
        ])
        if not ok:
            return False, f"Image→video failed: {err}"
        src = loop_mp4
        is_source_video = True

    # Apply effect steps (visual + pitch, treat as video from here)
    main_steps  = [s for s in steps if s["type"] != "reverse"]
    has_reverse = any(s["type"] == "reverse" for s in steps)
    intermediate = src

    for i, step in enumerate(main_steps):
        nxt = os.path.join(tmpdir, f"seg{seg_idx}_step{i}.mp4")
        ok, err = _apply_step(step, intermediate, nxt, tmpdir, True, duration, i)
        if not ok:
            return False, f"Seg {seg_idx} step {i} ({step.get('name', step['type'])}) failed: {err}"
        intermediate = nxt

    if has_reverse:
        rev_out = os.path.join(tmpdir, f"seg{seg_idx}_rev.mp4")
        ok, err  = _apply_reverse(intermediate, rev_out, True)
        if not ok:
            return False, f"Seg {seg_idx} reverse failed: {err}"
        intermediate = rev_out

    # Final encode to .ts — H.264 + AAC required for concat protocol
    ok, err = _run([
        "ffmpeg", "-y", "-i", intermediate,
        "-c:v", "libx264", "-preset", "fast", "-crf", "28",
        "-c:a", "aac", "-ar", "44100",
        "-t", str(duration),
        dst,
    ])
    return ok, err


def execute_ihtx_concat(
    input_path: str, output_path: str, tmpdir: str,
    steps: list[dict], is_video: bool, rep: int, duration: float,
) -> tuple[bool, str]:
    """
    TRUE IHTX algorithm (render → render → concat):

    segment_1 = render(original)          ← slightly degraded
    segment_2 = render(segment_1)         ← more artifacts (re-encode loss)
    ...
    segment_N = render(segment_{N-1})     ← pure chaos

    output = byte-level concat of all segments (no extra re-encode at join)
    Total length = rep × duration seconds. Escalates left→right.
    """
    ts_files = []
    cur      = input_path

    for i in range(rep):
        ts_path = os.path.join(tmpdir, f"{i + 1}.ts")
        ok, err = _render_to_ts(cur, ts_path, tmpdir, steps, is_video, duration, i + 1)
        if not ok:
            return False, f"Segment {i + 1}/{rep} failed:\n{err}"
        ts_files.append(ts_path)
        cur = ts_path   # ← each segment re-rendered from the previous one

    # Byte-level concat via MPEG-TS concat protocol (no re-encode)
    concat_uri = "concat:" + "|".join(ts_files)
    ok, err = _run(["ffmpeg", "-y", "-i", concat_uri, "-c", "copy", output_path], timeout=600)
    return ok, err


# ─── Download helper ──────────────────────────────────────────────────────────

async def download_attachment(attachment: discord.Attachment, dest: str):
    async with aiohttp.ClientSession() as session:
        async with session.get(attachment.url) as resp:
            if resp.status != 200:
                raise ValueError(f"HTTP {resp.status}")
            with open(dest, "wb") as f:
                f.write(await resp.read())


# ─── Commands ─────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"IHTX Bot online as {bot.user} (ID: {bot.user.id})")
    print("------")
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching,
        name="logos get destroyed | !ihtx"
    ))


@bot.command(name="ihtx", aliases=["effect", "destroy"])
async def ihtx_command(ctx: commands.Context, *, pipe_str: str = ""):
    """
    Apply IHTX effects via pipe syntax.
    !ihtx effect=value,effect=value,...

    Run !ihtxhelp for full documentation.
    """
    pipe_str = pipe_str.strip()

    if not pipe_str:
        await ctx.reply(HELP_TEXT)
        return

    # Parse early so we can show help if steps are empty
    entries          = parse_pipe(pipe_str)
    rep, dur, concat = extract_globals(entries)
    steps            = build_steps(entries)

    if not steps:
        await ctx.reply("No valid effects found in pipe. " + HELP_TEXT)
        return

    # Resolve attachment: own message first, then the message being replied to
    attachment = None
    if ctx.message.attachments:
        attachment = ctx.message.attachments[0]
    elif ctx.message.reference:
        try:
            ref_msg = ctx.message.reference.resolved
            if ref_msg is None:
                ref_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
            if ref_msg and ref_msg.attachments:
                attachment = ref_msg.attachments[0]
        except (discord.NotFound, discord.HTTPException):
            pass

    if attachment is None:
        await ctx.reply(
            "No attachment found. Either attach a file to your `!ihtx` message, "
            "or **reply to a message** that contains a video or image."
        )
        return
    if attachment.size > MAX_FILE_SIZE:
        await ctx.reply(f"File too large ({attachment.size / 1024 / 1024:.1f} MB, max 25 MB).")
        return

    suffix = Path(attachment.filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        await ctx.reply(f"Unsupported type `{suffix}`. Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
        return

    is_video = suffix in VIDEO_EXTENSIONS
    # concat mode always produces .mp4 (TS concat → mp4 mux)
    out_ext  = ".mp4" if (is_video or concat) else ".gif"

    # Build human-readable label
    effect_parts = []
    for s in steps:
        if s["type"] == "preset":
            label = s["name"] if s["passes"] == 1 else f"{s['name']}×{s['passes']}"
            effect_parts.append(label)
        elif s["type"] == "huehsv":
            effect_parts.append(f"huehsv({s['amount']:.2f})")
        elif s["type"] == "pinch":
            effect_parts.append(f"pinch({s['strength']})")
        elif s["type"] == "pitch":
            effect_parts.append("pitch(" + ";".join(f"{st:+.1f}" for st in s["semitones"]) + ")")
        elif s["type"] == "reverse":
            effect_parts.append("reverse")
        elif s["type"] == "wave":
            flags = []
            if s["separate"]: flags.append("sep")
            if s["noclip"]:   flags.append("noclip")
            tag = f",{','.join(flags)}" if flags else ""
            effect_parts.append(f"wave(h={s['hs']};{s['hf']};{s['ha']},v={s['vs']};{s['vf']};{s['va']}{tag})")
        elif s["type"] == "tv":
            vert = ",vert" if s["vertical"] else ""
            effect_parts.append(f"tv({s['line_sync']:.2f};{s['zoom_grill']:.1f}{vert})")
        elif s["type"] == "hmirror":
            effect_parts.append("hmirror-left" if s["side"] == 1 else "hmirror-right")
        elif s["type"] == "vmirror":
            effect_parts.append("vmirror-top" if s["side"] == 1 else "vmirror-bottom")
        elif s["type"] == "hue2":
            effect_parts.append(f"hue2({s['degrees']:+.0f}°)")

    pipeline_label = " → ".join(effect_parts)

    if concat:
        total_secs = rep * dur
        rep_label  = f" ×{rep} concat"
        full_label = f"✦ IHTX {pipeline_label} — {rep} segments × {dur}s = {total_secs:.1f}s"
        warn       = f" ⚠️ {rep} render passes + concat — may take a while" if rep > 10 else ""
    else:
        rep_label  = f" ×{rep}" if rep > 1 else ""
        full_label = f"{pipeline_label}{rep_label}, {dur}s"
        total_visual_passes = sum(
            s.get("passes", 1) for s in steps if s["type"] in ("preset", "pinch", "huehsv")
        ) * rep
        warn = f" ⚠️ {total_visual_passes} visual passes — may take a while" if total_visual_passes > 15 else ""

    status_msg = await ctx.reply(f"⚙️ Pipeline: **{full_label}**{warn}")

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path  = os.path.join(tmpdir, f"input{suffix}")
        output_path = os.path.join(tmpdir, f"output{out_ext}")

        try:
            await download_attachment(attachment, input_path)
        except Exception as e:
            await status_msg.edit(content=f"❌ Download failed: {e}")
            return

        loop = asyncio.get_event_loop()
        try:
            if concat:
                ok, err = await loop.run_in_executor(
                    None,
                    lambda: execute_ihtx_concat(input_path, output_path, tmpdir, steps, is_video, rep, dur),
                )
            else:
                ok, err = await loop.run_in_executor(
                    None,
                    lambda: execute_pipeline(input_path, output_path, tmpdir, steps, is_video, rep, dur),
                )
        except Exception as e:
            await status_msg.edit(content=f"❌ Processing error: {e}")
            return

        if not ok:
            await status_msg.edit(content=f"❌ Failed:\n```\n{err[-1500:]}\n```")
            return

        if os.path.getsize(output_path) > MAX_FILE_SIZE:
            await status_msg.edit(content="❌ Output too large for Discord (>25 MB). Reduce `rep` or `duration`.")
            return

        stem   = Path(attachment.filename).stem
        tag    = re.sub(r"[^\w]", "_", pipeline_label)[:40]
        out_fn = f"ihtx_{tag}_{stem}{out_ext}"

        done_label = f"✦ IHTX {pipeline_label}{rep_label}" if concat else f"**{pipeline_label}**{rep_label}"
        try:
            await ctx.reply(
                content=f"✅ {done_label} — done!",
                file=discord.File(output_path, filename=out_fn),
            )
            await status_msg.delete()
        except discord.HTTPException as e:
            await status_msg.edit(content=f"❌ Upload failed: {e}")


@bot.command(name="ihtxhelp", aliases=["bothelp"])
async def help_command(ctx: commands.Context):
    await ctx.reply(HELP_TEXT)


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.BadArgument):
        await ctx.reply(f"Bad argument: {error}. Use `!ihtxhelp`.")
        return
    raise error


if __name__ == "__main__":
    bot.run(TOKEN)
