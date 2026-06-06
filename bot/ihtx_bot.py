"""
IHTX Bot — I Hate The X FFmpeg Discord Bot

Full implementation with preset effects, custom effect chaining (g!ihtx),
and the preview1280 TV-simulator montage command.

Dependencies required at runtime: ffmpeg, aiohttp, discord.py, optionally yt-dlp,
ImageMagick/sox/etc. depending on advanced effects.
"""

import discord
from discord.ext import commands
import asyncio
import json
import math
import os
import re
import shlex
import tempfile
import shutil
import subprocess
import aiohttp
import sys
import time
from pathlib import Path

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

# ---------- Configuration & constants ----------

TOKEN = os.environ.get("DISCORD_TOKEN")

# Default owner (can be extended via owner file)
OWNER_ID = 1355759019330895973

OWNER_IDS_FILE = Path("bot/owner_ids.json")
owner_ids: set[int] = {OWNER_ID}


def _load_owner_ids():
    global owner_ids
    try:
        if OWNER_IDS_FILE.exists():
            with OWNER_IDS_FILE.open() as f:
                owner_ids = set(int(x) for x in json.load(f))
        else:
            owner_ids = {OWNER_ID}
    except Exception:
        owner_ids = {OWNER_ID}


def _save_owner_ids():
    OWNER_IDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OWNER_IDS_FILE.open("w") as f:
        json.dump(list(owner_ids), f)


def _is_owner(ctx: commands.Context) -> bool:
    return ctx.author.id in owner_ids


def _is_owner_by_id(user_id: int) -> bool:
    return user_id in owner_ids

_load_owner_ids()

# Heavy command rate limiting
HEAVY_COMMANDS = {"ihtx", "effect", "destroy", "preview1280", "p1280", "multipitch", "mp", "multi", "ihtxsync", "download", "dl"}
HEAVY_LIMIT_DEFAULT = 10
HEAVY_LIMIT_OWNER = 5340
LIMITS_FILE = Path("bot/limits.json")
heavy_limits: dict[int, int] = {}
heavy_usage: dict[int, list[float]] = {}


def _load_limits():
    global heavy_limits
    try:
        if LIMITS_FILE.exists():
            with LIMITS_FILE.open() as f:
                heavy_limits = {int(k): int(v) for k, v in json.load(f).items()}
        else:
            heavy_limits = {}
    except Exception:
        heavy_limits = {}


def _save_limits():
    LIMITS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LIMITS_FILE.open("w") as f:
        json.dump(heavy_limits, f)


def _check_heavy_limit(user_id: int) -> tuple[bool, str]:
    if _is_owner_by_id(user_id):
        return True, ""
    limit = heavy_limits.get(user_id, HEAVY_LIMIT_DEFAULT)
    now = time.time()
    day_ago = now - 86400
    usage = [t for t in heavy_usage.get(user_id, []) if t > day_ago]
    heavy_usage[user_id] = usage
    if len(usage) >= limit:
        return False, f"Heavy command limit reached ({limit}/{limit} per 24h). Contact an owner."
    usage.append(now)
    return True, ""

_load_limits()

# Blocklist (users)
BLOCKLIST_FILE = Path("bot/blocklist.json")
blocklist: set[int] = set()


def _load_blocklist():
    global blocklist
    try:
        if BLOCKLIST_FILE.exists():
            with BLOCKLIST_FILE.open() as f:
                blocklist = set(int(x) for x in json.load(f))
        else:
            blocklist = set()
    except Exception:
        blocklist = set()


def _save_blocklist():
    BLOCKLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    with BLOCKLIST_FILE.open("w") as f:
        json.dump(list(blocklist), f)

_load_blocklist()

# Channel blocklist
CHANNEL_BLOCK_FILE = Path("bot/channel_blocks.json")
channel_blocks: set[int] = set()


def _load_channel_blocks():
    global channel_blocks
    try:
        if CHANNEL_BLOCK_FILE.exists():
            with CHANNEL_BLOCK_FILE.open() as f:
                channel_blocks = set(int(x) for x in json.load(f))
        else:
            channel_blocks = set()
    except Exception:
        channel_blocks = set()


def _save_channel_blocks():
    CHANNEL_BLOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with CHANNEL_BLOCK_FILE.open("w") as f:
        json.dump(list(channel_blocks), f)

_load_channel_blocks()

# Tags (custom presets)
TAGS_FILE = Path("bot/tags.json")
tags: dict[str, dict] = {}


def _load_tags():
    global tags
    try:
        if TAGS_FILE.exists():
            with TAGS_FILE.open() as f:
                tags = json.load(f)
        else:
            tags = {}
    except Exception:
        tags = {}


def _save_tags():
    TAGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with TAGS_FILE.open("w") as f:
        json.dump(tags, f, indent=2)

_load_tags()

# Intents and bot
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="g!", intents=intents)

# Runtime stats
_bot_start_time: float = time.time()
_renders_completed: int = 0
_renders_in_progress: int = 0

# File handling constants
SUPPORTED_EXTENSIONS  = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".gif", ".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXTENSIONS      = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".gif"}
AUDIO_VIDEO_EXTS      = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
MAX_FILE_SIZE         = 25 * 1024 * 1024
MAX_REPETITIONS       = 100
MAX_DURATION          = 600

# Effect filter definitions
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

`g!ihtx effect=value,effect=value,...`

(Full help included in repository's README/help text.)
"""

# ---------- Global checks ----------

@bot.check
async def _global_checks(ctx: commands.Context) -> bool:
    # Channel blocked
    if ctx.channel.id in channel_blocks:
        return False
    # User blocked
    if ctx.author.id in blocklist:
        return False
    # Heavy command rate limiting
    if ctx.command and ctx.command.name in HEAVY_COMMANDS:
        ok, reason = _check_heavy_limit(ctx.author.id)
        if not ok:
            await ctx.reply(f"❌ {reason}")
            return False
    return True

# ---------- Helpers: download and ffmpeg ----------

async def download_attachment(attachment: discord.Attachment, dest: str):
    """Download a discord.Attachment to path `dest`."""
    async with aiohttp.ClientSession() as session:
        async with session.get(attachment.url) as resp:
            if resp.status != 200:
                raise ValueError(f"Failed to download attachment (HTTP {resp.status})")
            data = await resp.read()
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)


async def download_url(url: str, dest: str):
    """Download an arbitrary URL to path `dest`."""
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status != 200:
                raise ValueError(f"Failed to download URL (HTTP {resp.status})")
            data = await resp.read()
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)


def _ffprobe(input_path: str, *args: str) -> str:
    """Run ffprobe and return stripped stdout."""
    cmd = ["ffprobe", "-v", "error"] + list(args) + [input_path]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return result.stdout.strip()


def _ffprobe_duration(input_path: str) -> float:
    """Get duration in seconds."""
    out = _ffprobe(input_path, "-show_entries", "format=duration",
                   "-of", "csv=p=0")
    try:
        return float(out)
    except (ValueError, TypeError):
        return 0.0


def _ffprobe_video_info(input_path: str) -> dict:
    """Return width, height, duration, nb_frames, r_frame_rate."""
    info = {"width": 0, "height": 0, "duration": 0.0,
            "nb_frames": 0, "r_frame_rate": "30"}
    w = _ffprobe(input_path, "-select_streams", "v:0",
                 "-show_entries", "stream=width",
                 "-of", "default=nw=1:nk=1")
    h = _ffprobe(input_path, "-select_streams", "v:0",
                 "-show_entries", "stream=height",
                 "-of", "default=nw=1:nk=1")
    fc = _ffprobe(input_path, "-select_streams", "v:0",
                  "-show_entries", "stream=nb_frames",
                  "-of", "default=nokey=1:noprint_wrappers=1")
    fr = _ffprobe(input_path, "-select_streams", "v:0",
                  "-show_entries", "stream=r_frame_rate",
                  "-of", "default=nokey=1:noprint_wrappers=1")
    dur = _ffprobe_duration(input_path)
    try:
        info["width"] = int(w)
    except (ValueError, TypeError):
        pass
    try:
        info["height"] = int(h)
    except (ValueError, TypeError):
        pass
    try:
        info["nb_frames"] = int(fc)
    except (ValueError, TypeError):
        pass
    if fr:
        info["r_frame_rate"] = fr
    info["duration"] = dur
    return info


def _ffprobe_sample_rate(input_path: str) -> int:
    """Return the audio sample rate of the input file, defaulting to 44100."""
    sr = _ffprobe(input_path, "-select_streams", "a:0",
                  "-show_entries", "stream=sample_rate",
                  "-of", "default=nw=1:nk=1")
    try:
        return int(sr)
    except (ValueError, TypeError):
        return 44100


def _run_ffmpeg_raw(cmd: list[str], timeout: int = 180) -> tuple[bool, str]:
    """Run an arbitrary ffmpeg command. Returns (ok, stderr-or-empty)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            return False, result.stderr[-2000:]
        return True, ""
    except subprocess.TimeoutExpired:
        return False, f"FFmpeg timed out (>{timeout}s)"
    except Exception as e:
        return False, str(e)


def run_ffmpeg(input_path: str, output_path: str, preset: str, is_video: bool) -> tuple[bool, str]:
    """Run ffmpeg using PRESET_FILTERS. Returns (ok, stderr-or-empty)."""
    cfg = PRESET_FILTERS.get(preset)
    if cfg is None:
        cfg = PRESET_FILTERS["chaos"]

    if is_video:
        if cfg["complex"]:
            cmd = [
                "ffmpeg", "-y", "-i", input_path,
                "-filter_complex", cfg["complex"],
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "copy",
                "-t", "30",
                output_path
            ]
        else:
            cmd = [
                "ffmpeg", "-y", "-i", input_path,
                "-vf", cfg["vf"],
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "copy",
                "-t", "30",
                output_path
            ]
    else:
        # Image → animated GIF
        if cfg["complex"]:
            fc = cfg["complex"] + ",split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"
            cmd = [
                "ffmpeg", "-y",
                "-loop", "1", "-i", input_path,
                "-filter_complex", fc,
                "-t", "3",
                output_path
            ]
        else:
            vf = cfg["vf"] + ",split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"
            cmd = [
                "ffmpeg", "-y",
                "-loop", "1", "-i", input_path,
                "-vf", vf,
                "-t", "3",
                output_path
            ]

    return _run_ffmpeg_raw(cmd)


def get_output_ext(input_ext: str, is_video: bool) -> str:
    return ".mp4" if is_video else ".gif"

# ---------- Effect chaining engine ----------

def _parse_effect_chain(effects_str: str) -> list[tuple[str, list[str]]]:
    """Parse 'effect=value,effect=value,...' into [(name, [params]), ...].

    Params are semicolon-separated sub-params within each effect.
    For multipitch/mp/multi, params are semicolon-separated (e.g. multipitch=25;5;8.5).
    """
    effects = []
    for part in effects_str.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            name, value = part.split("=", 1)
            name = name.strip().lower()
            params = [p.strip() for p in value.split(";")]
        else:
            name = part.lower()
            params = []
        effects.append((name, params))
    return effects


def _build_video_filters(effects: list[tuple[str, list[str]]], w: int, h: int) -> list[str]:
    """Build a list of FFmpeg -vf filter strings from parsed effects.

    Returns a list of individual filter chain strings (to be joined with ',').
    """
    vf_parts = []

    for name, params in effects:
        if name == "hflip":
            vf_parts.append("hflip")

        elif name == "vflip":
            vf_parts.append("vflip")

        elif name == "invert":
            vf_parts.append("negate")

        elif name == "invlum":
            # Invert luminosity only via LUT
            vf_parts.append("lumakey=L=128:H=255:mode=1,format=yuv420p"
                            if False else
                            "curves=all='0/1 1/0'")

        elif name == "invertrgb":
            # Invert specific channels: invertrgb=1;0;1 → invert R and B
            r_inv = params[0] if len(params) > 0 else "1"
            g_inv = params[1] if len(params) > 1 else "0"
            b_inv = params[2] if len(params) > 2 else "0"
            r_curve = "0/1 1/0" if r_inv == "1" else "0/0 1/1"
            g_curve = "0/1 1/0" if g_inv == "1" else "0/0 1/1"
            b_curve = "0/1 1/0" if b_inv == "1" else "0/0 1/1"
            vf_parts.append(f"curves=r='{r_curve}':g='{g_curve}':b='{b_curve}'")

        elif name == "grayscale":
            vf_parts.append("hue=s=0")

        elif name == "sepia":
            vf_parts.append("colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131")

        elif name == "rotate":
            angle = params[0] if params else "0"
            vf_parts.append(f"rotate={angle}*PI/180")

        elif name == "hue":
            deg = params[0] if params else "0"
            vf_parts.append(f"hue=h={deg}")

        elif name == "huehsv":
            # Use FFmpeg hue filter with h parameter mapped from magick-style
            val = params[0] if params else "0"
            deg = float(val) * 1.8 if val else 0
            vf_parts.append(f"hue=h={deg}")

        elif name == "ffmpeghue":
            deg = params[0] if params else "0"
            vf_parts.append(f"hue=h={deg}")

        elif name == "brightness":
            val = params[0] if params else "0"
            vf_parts.append(f"eq=brightness={val}")

        elif name == "contrast":
            val = params[0] if params else "1"
            vf_parts.append(f"eq=contrast={val}")

        elif name == "saturation":
            val = params[0] if params else "1"
            vf_parts.append(f"eq=saturation={val}")

        elif name == "channelblend":
            # channelblend=r;g;b — swap/mix RGB channels
            r = params[0] if len(params) > 0 else "r"
            g = params[1] if len(params) > 1 else "g"
            b = params[2] if len(params) > 2 else "b"
            ch_map = {"r": "1:0:0", "g": "0:1:0", "b": "0:0:1"}
            rr = ch_map.get(r, "1:0:0")
            gg = ch_map.get(g, "0:1:0")
            bb = ch_map.get(b, "0:0:1")
            vf_parts.append(
                f"colorchannelmixer=rr={rr.split(':')[0]}:rg={rr.split(':')[1]}:rb={rr.split(':')[2]}"
                f":gr={gg.split(':')[0]}:gg={gg.split(':')[1]}:gb={gg.split(':')[2]}"
                f":br={bb.split(':')[0]}:bg={bb.split(':')[1]}:bb={bb.split(':')[2]}"
            )

        elif name == "swapuv":
            vf_parts.append("swapuv")

        elif name == "gm4":
            # Selective colour boost (blacks/whites)
            vf_parts.append("eq=contrast=1.4:brightness=0.02:saturation=1.3,curves=preset=lighter")

        elif name == "realgm4":
            # Solarise via curves inversion
            vf_parts.append("curves=all='0/0 0.5/1 1/0'")

        elif name == "fisheye":
            # fisheye=strength;radius;cx;cy
            strength = params[0] if len(params) > 0 else "1"
            radius = params[1] if len(params) > 1 else "0.5"
            cx = params[2] if len(params) > 2 else "0.5"
            cy = params[3] if len(params) > 3 else "0.5"
            vf_parts.append(
                f'format=yuv444p,geq='
                f'"p(W*{cx}+(X-W*{cx})*(1-({strength})*gauss(-3.3333*pow(hypot((X-W*{cx})/(W*{radius}),(Y-H*{cy})/(H*{radius})),2))),'
                f'H*{cy}+(Y-H*{cy})*(1-({strength})*gauss(-3.3333*pow(hypot((X-W*{cx})/(W*{radius}),(Y-H*{cy})/(H*{radius})),2))))",'
                f'scale=iw:ih,format=yuv420p'
            )

        elif name == "swirl":
            # swirl=angle;radius;cx;cy;fallout;lockaspect
            angle = params[0] if len(params) > 0 else "180"
            radius = params[1] if len(params) > 1 else "0.5"
            cx = params[2] if len(params) > 2 else "0.5"
            cy = params[3] if len(params) > 3 else "0.5"
            fallout = params[4] if len(params) > 4 else "quad"
            lock = params[5] if len(params) > 5 else "false"
            exp_str = "" if fallout == "linear" else "^2"
            min_wh = "min(W,H)"
            if lock.lower() in ("1", "true", "t", "y", "yes", "+", "on"):
                vf_parts.append(
                    f'format=yuv444p,scale={h}:{h},'
                    f'geq="p(W*{cx}+(hypot(X-W*{cx},Y-H*{cy})+1e-6)*cos((atan2(Y-H*{cy},X-W*{cx}))+(({angle})/180*PI)*(if(lt(hypot(X-W*{cx},Y-H*{cy})+1e-6,{min_wh}*{radius}),1-(hypot(X-W*{cx},Y-H*{cy})+1e-6)/({min_wh}*{radius}),0){exp_str})),'
                    f'H*{cy}+(hypot(X-W*{cx},Y-H*{cy})+1e-6)*sin((atan2(Y-H*{cy},X-W*{cx}))+(({angle})/180*PI)*(if(lt(hypot(X-W*{cx},Y-H*{cy})+1e-6,{min_wh}*{radius}),1-(hypot(X-W*{cx},Y-H*{cy})+1e-6)/({min_wh}*{radius}),0){exp_str})))",'
                    f'scale={w}:{h},setsar=1:1,format=yuv420p'
                )
            else:
                vf_parts.append(
                    f'format=yuv444p,'
                    f'geq="p(W*{cx}+(hypot(X-W*{cx},Y-H*{cy})+1e-6)*cos((atan2(Y-H*{cy},X-W*{cx}))+(({angle})/180*PI)*(if(lt(hypot(X-W*{cx},Y-H*{cy})+1e-6,{min_wh}*{radius}),1-(hypot(X-W*{cx},Y-H*{cy})+1e-6)/({min_wh}*{radius}),0){exp_str})),'
                    f'H*{cy}+(hypot(X-W*{cx},Y-H*{cy})+1e-6)*sin((atan2(Y-H*{cy},X-W*{cx}))+(({angle})/180*PI)*(if(lt(hypot(X-W*{cx},Y-H*{cy})+1e-6,{min_wh}*{radius}),1-(hypot(X-W*{cx},Y-H*{cy})+1e-6)/({min_wh}*{radius}),0){exp_str})))",'
                    f'scale=iw:ih,format=yuv420p'
                )

        elif name == "wave":
            # wave=hspeed;hfreq;hampli;hphase;vspeed;vfreq;vampli;vphase [separate] [noclip]
            hs = params[0] if len(params) > 0 else "1"
            hf = params[1] if len(params) > 1 else "10"
            ha = params[2] if len(params) > 2 else "15"
            hp = params[3] if len(params) > 3 else "0"
            vs = params[4] if len(params) > 4 else "0"
            vf_val = params[5] if len(params) > 5 else "10"
            va = params[6] if len(params) > 6 else "15"
            vp = params[7] if len(params) > 7 else "0"
            separate = params[8].lower() if len(params) > 8 else "false"
            noclip = params[9].lower() if len(params) > 9 else "false"

            separate_b = separate in ("1", "true", "t", "y", "yes", "+", "on")
            noclip_b = noclip in ("1", "true", "t", "y", "yes", "+", "on")

            prefix = "drawbox=t=1," if not noclip_b else ""
            if separate_b:
                h_wave = (
                    f'geq="p(X\\,Y-((sin((T*5*{hs}+({hp}*15))+(X/W)*(PI*{hf})))*(-15*{ha}))))"'
                )
                v_wave = (
                    f'geq="p(X-((sin((T*5*{vs}+({vp}*15))+(Y/H)*(PI*{vf_val})))*(-15*{va}))\\,Y)"'
                )
                vf_parts.append(
                    f'{prefix}format=yuv444p,scale=640:640,{h_wave},{v_wave},scale={w}:{h},setsar=1:1,format=yuv420p'
                )
            else:
                combined = (
                    f'geq="p(X-((sin((T*5*{vs}+({vp}*15))+(Y/H)*(PI*{vf_val})))*(-15*{va})),'
                    f'Y-((sin((T*5*{hs}+({hp}*15))+(X/W)*(PI*{hf})))*(-15*{ha})))"'
                )
                vf_parts.append(
                    f'{prefix}format=yuv444p,scale=640:640,{combined},scale={w}:{h},setsar=1:1,format=yuv420p'
                )

        elif name == "zoom":
            scale_val = params[0] if params else "2"
            vf_parts.append(f"scale=iw*{scale_val}:ih*{scale_val},crop=iw/2:ih/2")

        elif name == "mirror":
            angle = params[0] if params else "0"
            # Mirror fold: flip one half over at the given angle
            if float(angle) == 0:
                vf_parts.append("split[l][r];[r]hflip[rf];[l][rf]hstack")
            else:
                rad = float(angle) * math.pi / 180
                vf_parts.append(
                    f"split[l][r];[r]hflip,rotate={angle}*PI/180:ow=iw:(oh*2-ih)/2[rf];"
                    f"[l][rf]overlay"
                )

        elif name == "tile":
            tx = params[0] if len(params) > 0 else "2"
            ty = params[1] if len(params) > 1 else "2"
            vf_parts.append(f"tile={tx}x{ty}")

        elif name == "polar":
            vf_parts.append("geq=p(X+W/2-Y+H/2:sin(2*PI*X/W)*H/2+H/2),format=yuv420p")

        elif name == "depolar":
            vf_parts.append("geq=p(W/2+sin(2*PI*Y/H)*(H/2):cos(2*PI*Y/H)*(W/2)),format=yuv420p")

        elif name == "orb":
            # Fisheye orb
            vf_parts.append(
                'format=yuv444p,geq='
                '"p(W/2+(X-W/2)/sqrt(1-4*((X-W/2)/W)^2-4*((Y-H/2)/H)^2),'
                'H/2+(Y-H/2)/sqrt(1-4*((X-W/2)/W)^2-4*((Y-H/2)/H)^2))",'
                'scale=iw:ih,format=yuv420p'
            )

        elif name == "deorb":
            vf_parts.append(
                'format=yuv444p,geq='
                '"p(W/2+(X-W/2)*sqrt(1-4*((X-W/2)/W)^2-4*((Y-H/2)/H)^2),'
                'H/2+(Y-H/2)*sqrt(1-4*((X-W/2)/W)^2-4*((Y-H/2)/H)^2))",'
                'scale=iw:ih,format=yuv420p'
            )

        elif name == "gm91deform":
            vf_parts.append(
                f'format=yuv444p,scale=360:360,setsar=1:1,rotate=0:iw*1.05:ih*1.05,'
                f'geq='
                f'"p((W/2)+((X-W/2)/lerp(1,asin(sin(-Y/H)),0.164))/lerp(1,1.22)+((Y-H/2)*(-0.136))+((0.047*W)*pow((Y-H/2)/(H/2),2))+(-W/40),(H/2)+((Y-H/2)/lerp(1,1.27))/lerp(1,sin((X/W)*PI),0.12)-(((0.014)*H)*pow((X-W/2)/(W/2),2))+((X-W/2)*(0.12))-(1.2))",'
                f'scale=640*1.05:360*1.05,crop=640:360:(in_w-in_h)/2+8,scale={w}:{h},setsar=1,format=yuv420p'
            )

        elif name == "scroll":
            # scroll=h;v or scroll=x1;y1;x2;y2;dur
            if len(params) == 1 and params[0].startswith("hpos="):
                x = params[0].split("=", 1)[1]
                vf_parts.append(f"scroll=hpos={x}")
            elif len(params) == 1 and params[0].startswith("ypos="):
                y = params[0].split("=", 1)[1]
                vf_parts.append(f"scroll=vpos={y}")
            elif len(params) >= 4:
                x1, y1, x2, y2 = params[0], params[1], params[2], params[3]
                dur = params[4] if len(params) > 4 else "10"
                vf_parts.append(f"scroll=h_speed=({x2}-{x1})/{dur}:v_speed=({y2}-{y1})/{dur}")
            else:
                h_speed = params[0] if len(params) > 0 else "0"
                v_speed = params[1] if len(params) > 1 else "0"
                vf_parts.append(f"scroll=h_speed={h_speed}:v_speed={v_speed}")

        elif name == "pan":
            px = params[0] if len(params) > 0 else "0"
            py = params[1] if len(params) > 1 else "0"
            vf_parts.append(f"pan=x={px}:y={py}")

        elif name == "vreverse":
            vf_parts.append("reverse")

        elif name == "watermark":
            url = params[0] if params else ""
            if url:
                vf_parts.append(f"movie='{url}',[in]overlay=0:0")
            # URL download handled separately in _build_ffmpeg_cmd_for_effects

        elif name == "ring":
            url = params[0] if params else "https://files.catbox.moe/ns8i66.png"
            vf_parts.append(f"movie='{url}',[in]overlay=0:0")

        elif name == "miui":
            vf_parts.append(
                "drawtext=text='MIUI':fontsize=24:fontcolor=white@0.6:"
                "x=W-tw-10:y=H-th-10:borderw=1:bordercolor=black@0.5"
            )

        elif name == "reddit":
            vf_parts.append(
                "drawtext=text='reddit':fontsize=20:fontcolor=white@0.5:"
                "x=W-tw-10:y=H-th-10:borderw=1:bordercolor=black@0.4"
            )

        elif name == "caption":
            text = params[0] if params else ""
            # Escape special chars for FFmpeg drawtext
            text = text.replace("'", "'\\''").replace(":", "\\:").replace("=", "\\=")
            vf_parts.append(
                f"drawtext=text='{text}':fontsize=h/15:fontcolor=white:"
                f"borderw=2:bordercolor=black:x=(W-tw)/2:y=10"
            )

        elif name == "zoom":
            scale_val = params[0] if params else "2"
            vf_parts.append(f"scale=iw*{scale_val}:ih*{scale_val},crop=iw/2:ih/2")

        elif name in ("multipitch", "mp", "multi", "volume", "vibrato", "areverse"):
            # Audio effects — handled separately in _build_af_for_effects / _run_multipitch
            pass

        elif name == "lut":
            # LUT from URL — handled separately
            pass

        elif name == "ffmpeg":
            # Raw ffmpeg flags — handled separately
            pass

    return vf_parts


def _build_af_for_effects(effects: list[tuple[str, list[str]]], input_path: str = "") -> str | None:
    """Build an -af audio filter string from parsed effects."""
    af_parts = []
    for name, params in effects:
        if name in ("multipitch", "mp", "multi"):
            # multipitch in semitones — semicolon-separated, e.g. multipitch=25;5;8.5
            # Uses SoX pitch shifting (external pipeline) — handled separately in _run_multipitch
            # Cannot be expressed as a simple -af string; requires WAV extraction + sox + re-merge
            pass

        elif name == "volume":
            val = params[0] if params else "1"
            af_parts.append(f"volume={val}")

        elif name == "vibrato":
            freq = params[0] if len(params) > 0 else "5"
            depth = params[1] if len(params) > 1 else "0.5"
            af_parts.append(f"vibrato=f={freq}:d={depth}")

        elif name == "areverse":
            af_parts.append("areverse")

    if af_parts:
        return ",".join(af_parts)
    return None


def _build_ffmpeg_cmd_for_effects(
    input_path: str,
    output_path: str,
    effects: list[tuple[str, list[str]]],
    w: int,
    h: int,
    duration: float,
) -> list[str]:
    """Build a full ffmpeg command for the custom effect chain."""
    vf_parts = _build_video_filters(effects, w, h)
    af = _build_af_for_effects(effects, input_path)

    cmd = ["ffmpeg", "-y", "-i", input_path]

    # Handle special cases that need filter_complex
    needs_complex = any(n in ("watermark", "ring", "polar", "depolar") for n, _ in effects)
    # Check for raw ffmpeg() effect
    raw_ffmpeg_args = []
    for name, params in effects:
        if name == "ffmpeg":
            raw_ffmpeg_args.extend(params)

    if raw_ffmpeg_args:
        cmd.extend(raw_ffmpeg_args)
    else:
        if vf_parts:
            vf_str = ",".join(vf_parts)
            cmd.extend(["-vf", vf_str])
        if af:
            cmd.extend(["-af", af])

    cmd.extend([
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-t", str(min(duration, MAX_DURATION)),
        output_path
    ])
    return cmd


def _run_effect_chain(
    input_path: str,
    output_path: str,
    effects_str: str,
    repetitions: int = 1,
    duration_override: float | None = None,
) -> tuple[bool, str]:
    """Run the full effect chain: parse → build → multi-segment → concat."""
    effects = _parse_effect_chain(effects_str)
    if not effects:
        return False, "No valid effects specified."

    info = _ffprobe_video_info(input_path)
    w, h = info["width"], info["height"]
    dur = info["duration"]

    if w == 0 or h == 0:
        return False, "Could not read video dimensions from input."

    actual_dur = duration_override if duration_override else dur
    actual_dur = min(actual_dur, MAX_DURATION)

    if repetitions <= 1:
        # Simple single-pass render
        cmd = _build_ffmpeg_cmd_for_effects(input_path, output_path, effects, w, h, actual_dur)
        return _run_ffmpeg_raw(cmd, timeout=180)

    # Multi-segment: render N segments with slight parameter variation, then concat
    # Each segment re-applies the effects with shifted parameters
    with tempfile.TemporaryDirectory() as tmpdir:
        segment_files = []
        for i in range(repetitions):
            seg_path = os.path.join(tmpdir, f"seg_{i:04d}.ts")
            # Build a slightly varied effect chain per segment
            varied_effects = []
            for ename, eparams in effects:
                varied_params = list(eparams)
                # Add progressive offset for hue, brightness, etc.
                if ename in ("hue", "ffmpeghue") and eparams:
                    base_val = float(eparams[0]) if eparams[0] else 0
                    varied_params[0] = str(base_val + i * 30)
                elif ename == "brightness" and eparams:
                    base_val = float(eparams[0]) if eparams[0] else 0
                    varied_params[0] = str(base_val + (i % 3 - 1) * 0.05)
                varied_effects.append((ename, varied_params))

            cmd = _build_ffmpeg_cmd_for_effects(input_path, seg_path, varied_effects, w, h, actual_dur)
            ok, err = _run_ffmpeg_raw(cmd, timeout=180)
            if not ok:
                return False, f"Segment {i+1}/{repetitions} failed: {err}"
            segment_files.append(seg_path)

        # Concat segments
        concat_list = os.path.join(tmpdir, "concat.txt")
        with open(concat_list, "w") as f:
            for sf in segment_files:
                f.write(f"file '{sf}'\n")

        concat_cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", concat_list,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            output_path
        ]
        return _run_ffmpeg_raw(concat_cmd, timeout=300)




# ---------- Multipitch (SoX pitch-shift pipeline) ----------

def _run_multipitch(
    input_path: str,
    output_path: str,
    pitch_values: list[str],
) -> tuple[bool, str]:
    """Run the multipitch pipeline using SoX pitch shifting + ffmpeg re-merge.

    Pipeline per pitch voice:
      1. ffmpeg -i input -c:a pcm_s16le -preset ultrafast a0.mp4  (export segment)
      2. ffmpeg -i a0.mp4 b0.wav                                   (extract WAV)
      3. sox b0.wav 1{i}.wav pitch -q {semitones*100} 25 5 8.5     (SoX pitch shift)
      4. Single pitch: ffmpeg -i a0.mp4 -i 10.wav -filter_complex "[1:a]highpass=10[a]"
         -map 0:v -map "[a]" -c:a pcm_s16le output
         Multiple pitches: ffmpeg -i a0.mp4 -i 10.wav -i 11.wav ...
         -filter_complex "[1][2]...amix=N:normalize=0,highpass=7.5"
         -c:a pcm_s16le -preset ultrafast output

    SoX pitch effect syntax: pitch -q cents 25 5 8.5
      where cents = semitones * 100
    """
    if not pitch_values:
        return False, "No pitch values provided."

    n = len(pitch_values)

    # Validate pitch values
    for pv in pitch_values:
        try:
            float(pv)
        except ValueError:
            return False, f"Invalid pitch value: {pv!r}"

    with tempfile.TemporaryDirectory() as tmpdir:
        # Step 1: Export base video with pcm_s16le audio
        base_mp4 = os.path.join(tmpdir, "a0.mp4")
        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-c:a", "pcm_s16le", "-preset", "ultrafast",
            base_mp4,
        ]
        ok, err = _run_ffmpeg_raw(cmd, timeout=120)
        if not ok:
            return False, f"Step 1 (export base) failed: {err}"

        # Step 2: Extract WAV audio from base
        base_wav = os.path.join(tmpdir, "b0.wav")
        cmd = [
            "ffmpeg", "-y", "-i", base_mp4,
            base_wav,
        ]
        ok, err = _run_ffmpeg_raw(cmd, timeout=120)
        if not ok:
            return False, f"Step 2 (extract WAV) failed: {err}"

        # Step 3: SoX pitch shift for each voice
        pitched_wavs = []
        for i, pitch_val in enumerate(pitch_values):
            cents = int(float(pitch_val) * 100)
            out_wav = os.path.join(tmpdir, f"1{i}.wav")
            cmd = [
                "sox", base_wav, out_wav,
                "pitch", "-q", str(cents), "25", "5", "8.5",
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                return False, f"SoX pitch shift for voice {i+1} failed: {result.stderr}"
            pitched_wavs.append(out_wav)

        # Step 4: Re-merge with video
        if n == 1:
            # Single pitch: ffmpeg -i a0.mp4 -i 10.wav -filter_complex "[1:a]highpass=10[a]"
            #   -map 0:v -map "[a]" -c:a pcm_s16le output
            cmd = [
                "ffmpeg", "-y",
                "-i", base_mp4,
                "-i", pitched_wavs[0],
                "-filter_complex", "[1:a]highpass=10[a]",
                "-map", "0:v", "-map", "[a]",
                "-c:a", "pcm_s16le",
                output_path,
            ]
        else:
            # Multiple pitches: ffmpeg -i a0.mp4 -i 10.wav -i 11.wav ...
            #   -filter_complex "[1][2]...[N]amix=N:normalize=0,highpass=7.5"
            #   -c:a pcm_s16le -preset ultrafast output
            cmd = ["ffmpeg", "-y", "-i", base_mp4]
            for wav in pitched_wavs:
                cmd.extend(["-i", wav])

            # Build filter_complex: [1][2]...[N]amix=N:normalize=0,highpass=7.5
            mix_inputs = "".join(f"[{i+1}]" for i in range(n))
            filter_complex = f"{mix_inputs}amix={n}:normalize=0,highpass=7.5"

            cmd.extend([
                "-filter_complex", filter_complex,
                "-map", "0:v",
                "-c:a", "pcm_s16le", "-preset", "ultrafast",
                output_path,
            ])

        return _run_ffmpeg_raw(cmd, timeout=300)

# ---------- Preview1280 (TV-simulator montage) ----------

async def _ensure_displacement_map(workdir: str) -> str:
    """Ensure the TV simulator displacement map exists, downloading if needed.

    Returns the path to the .mov file.
    """
    # First check if the bundled copy exists
    bundled = Path("bot/displacemaps/tvsimulator.mov")
    if bundled.exists():
        return str(bundled)

    # Try to download it
    disp_dir = os.path.join(workdir, "displacemaps")
    os.makedirs(disp_dir, exist_ok=True)
    dest = os.path.join(disp_dir, "tvsimulator.mov")
    if os.path.exists(dest):
        return dest

    try:
        await download_url(
            "https://file.garden/aTXso15ukD3mnuPI/tv_sim_displacement_map.mov",
            dest
        )
        return dest
    except Exception:
        # Last resort: check common locations
        for candidate in [
            "displacemaps/tvsimulator.mov",
            "bot/displacemaps/tvsimulator.mov",
            "/app/bot/displacemaps/tvsimulator.mov",
        ]:
            if os.path.exists(candidate):
                return candidate
        raise FileNotFoundError(
            "TV simulator displacement map not found and could not be downloaded. "
            "Place it at bot/displacemaps/tvsimulator.mov"
        )


def _generate_hald_cluts(workdir: str) -> list[str]:
    """Generate Hald CLUT .ppm files for hue shifts using ImageMagick.

    Returns paths to [hslhue_54.ppm, hslhue_180.ppm, hslhue_22.ppm, hslhue_108_30.ppm].
    CLUT hue values use ImageMagick -modulate formula: hue_frac * 200 + 100 (or +200 for sat boost).
    """
    # (filename, brightness, saturation, hue_mod_value)
    # hue_mod_value = hue_fraction * 200 + 100
    # For saturation-boosted CLUTs, saturation > 100 and hue_mod = hue_fraction * 200 + 200
    clut_specs = [
        # hslhue_54: hue shift 54° → fraction 0.15, mod = 0.15*200+100 = 130
        ("hslhue_54.ppm", 100, 100, 130),
        # hslhue_180: hue shift 180° → fraction 0.5, mod = 0.5*200+100 = 200
        ("hslhue_180.ppm", 100, 100, 200),
        # hslhue_22: hue shift 22° → fraction 0.06, mod = 0.06*200+100 = 112
        ("hslhue_22.ppm", 100, 100, 112),
        # hslhue_108_30: hue shift 108° + saturation boost → fraction 0.3, mod = 0.3*200+200 = 260
        ("hslhue_108_30.ppm", 100, 130, 260),
    ]
    paths = []
    for filename, brightness, saturation, hue_mod in clut_specs:
        path = os.path.join(workdir, filename)
        cmd = [
            "magick", "hald:4",
            "-modulate", f"{brightness},{saturation},{hue_mod}",
            path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            # Fallback: create a simple identity CLUT
            # If magick isn't available, skip CLUT effects
            pass
        paths.append(path)
    return paths


def _run_preview1280(
    input_path: str,
    output_path: str,
    start_offset: float = 1.85,
    segment_dur: float = 0.85,
) -> tuple[bool, str]:
    """Run the preview1280 TV-simulator montage pipeline.

    This creates a 12-segment montage at 640x360, then scales to original size.
    Requires: ffmpeg, ImageMagick (magick), and the tvsimulator.mov displacement map.
    Uses rubberband audio filter for high-quality pitch shifting.
    """
    # Helper: rubberband pitch filter string for N semitones
    # Pre-compute 2^(N/12) as a float to avoid FFmpeg expression parsing issues
    def _rb(semitones: float, transients: str = "mixed") -> str:
        pitch_ratio = 2 ** (semitones / 12)
        return (
            f"rubberband=pitch={pitch_ratio:.6f}:"
            f"window=short:transients={transients}:"
            f"detector=soft:channels=together:pitchq=consistency"
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        info = _ffprobe_video_info(input_path)
        w, h = info["width"], info["height"]
        dur = info["duration"]

        if w == 0 or h == 0:
            return False, "Could not read input video dimensions."

        # Generate Hald CLUTs
        cluts = _generate_hald_cluts(tmpdir)
        clut_54 = cluts[0] if os.path.exists(cluts[0]) else None
        clut_180 = cluts[1] if os.path.exists(cluts[1]) else None
        clut_22 = cluts[2] if os.path.exists(cluts[2]) else None
        clut_108_30 = cluts[3] if os.path.exists(cluts[3]) else None

        # Locate displacement map
        disp_map = None
        for candidate in [
            "bot/displacemaps/tvsimulator.mov",
            "displacemaps/tvsimulator.mov",
            "/app/bot/displacemaps/tvsimulator.mov",
        ]:
            if os.path.exists(candidate):
                disp_map = candidate
                break

        # Compute timing
        t = segment_dur
        t2 = segment_dur / 2
        t3 = start_offset + segment_dur

        # Step 1: Pre-process input to 640x360 FFV1
        avi0 = os.path.join(tmpdir, "0.avi")
        cmd = [
            "ffmpeg", "-y", "-stream_loop", "-1", "-i", input_path,
            "-vf", "scale=640:360,setsar=1:1",
            "-ss", str(start_offset), "-to", str(t3),
            "-c:v", "ffv1", "-c:a", "pcm_s16le",
            avi0
        ]
        ok, err = _run_ffmpeg_raw(cmd, timeout=120)
        if not ok:
            return False, f"Step 1 (pre-process) failed: {err}"

        avi_w = _ffprobe(avi0, "-select_streams", "v:0",
                         "-show_entries", "stream=width",
                         "-of", "default=nw=1:nk=1") or "640"
        avi_h = _ffprobe(avi0, "-select_streams", "v:0",
                         "-show_entries", "stream=height",
                         "-of", "default=nw=1:nk=1") or "360"

        # Helper to build segment ffmpeg commands
        segments = []

        # Segment 1: plain copy, duration t
        seg1 = os.path.join(tmpdir, "1.avi")
        segments.append(([
            "ffmpeg", "-y", "-i", avi0,
            "-t", str(t), "-c:v", "ffv1", "-c:a", "pcm_s16le",
            seg1
        ], seg1))

        # Segment 2: hue +54 (hslhue_54), pitch +1 semitone (rubberband)
        seg2 = os.path.join(tmpdir, "2.avi")
        if clut_54:
            segments.append(([
                "ffmpeg", "-y", "-i", avi0,
                "-vf", f"movie={clut_54},[in]haldclut,format=yuv420p",
                "-af", _rb(1),
                "-t", str(t), "-c:v", "ffv1", "-c:a", "pcm_s16le",
                seg2
            ], seg2))
        else:
            segments.append(([
                "ffmpeg", "-y", "-i", avi0,
                "-vf", "hue=h=54",
                "-af", _rb(1),
                "-t", str(t), "-c:v", "ffv1", "-c:a", "pcm_s16le",
                seg2
            ], seg2))

        # Segment 3: hue +180 + displacement map + mirror + pitch -2 semitones
        seg3 = os.path.join(tmpdir, "3.avi")
        if disp_map and clut_180:
            fc = (
                f"movie={clut_180}[h];"
                f"[0][h]haldclut,hflip,crop=iw/2:ih:0:0,split[left][tmp];"
                f"[tmp]hflip[right];[left][right]hstack,format=yuv420p,format=bgr32[00];"
                f"[1]crop=iw:ih/1:0:0,scale={avi_w}:{avi_h},eq=contrast=0.4,format=bgr32,hue=b=-0.033[x];"
                f"nullsrc=1x1,geq=r=128:g=128:b=128,scale={avi_w}:{avi_h},format=bgr32[y];"
                f"[00][x][y]displace=edge=wrap[v]"
            )
            segments.append(([
                "ffmpeg", "-y", "-i", avi0, "-stream_loop", "-1", "-i", disp_map,
                "-filter_complex", fc,
                "-af", _rb(-2),
                "-map", "[v]", "-map", "0:a",
                "-pix_fmt", "yuv420p",
                "-t", str(t), "-c:v", "ffv1", "-c:a", "pcm_s16le",
                seg3
            ], seg3))
        else:
            # Fallback without displacement
            segments.append(([
                "ffmpeg", "-y", "-i", avi0,
                "-vf", "hue=h=180,hflip,crop=iw/2:ih:0:0,split[left][tmp];[tmp]hflip[right];[left][right]hstack,format=yuv420p",
                "-af", _rb(-2),
                "-t", str(t), "-c:v", "ffv1", "-c:a", "pcm_s16le",
                seg3
            ], seg3))

        # Segment 4: hue +54 (hslhue_54), pitch +1 semitone (same as seg2)
        seg4 = os.path.join(tmpdir, "4.avi")
        if clut_54:
            segments.append(([
                "ffmpeg", "-y", "-i", avi0,
                "-vf", f"movie={clut_54},[in]haldclut,format=yuv420p",
                "-af", _rb(1),
                "-t", str(t), "-c:v", "ffv1", "-c:a", "pcm_s16le",
                seg4
            ], seg4))
        else:
            segments.append(([
                "ffmpeg", "-y", "-i", avi0,
                "-vf", "hue=h=54",
                "-af", _rb(1),
                "-t", str(t), "-c:v", "ffv1", "-c:a", "pcm_s16le",
                seg4
            ], seg4))

        # Segments 5-12: shorter segments (t2 duration)
        short_specs = [
            # (seg_num, vf_filter, af_filter)
            (5, None, None),  # plain copy
            (6, f"movie={clut_22},[in]haldclut,hflip,format=yuv420p" if clut_22 else "hue=h=22,hflip,format=yuv420p",
             _rb(2, "smooth")),  # hue+22, hflip, pitch+2 (smooth transients)
            (7, f"movie={clut_54},[in]haldclut,format=yuv420p" if clut_54 else "hue=h=54,format=yuv420p",
             _rb(1)),  # hue+54, pitch+1
            (8, f"movie={clut_108_30},[in]haldclut,hflip,format=yuv420p" if clut_108_30 else "hue=h=108,hflip,format=yuv420p",
             _rb(3)),  # hue+108+sat30, hflip, pitch+3
            (9, f"movie={clut_180},[in]haldclut,format=yuv420p" if clut_180 else "hue=h=180,format=yuv420p",
             _rb(-2)),  # hue+180, pitch-2
            (10, "hflip", None),  # just hflip
            (11, f"movie={clut_54},[in]haldclut,format=yuv420p" if clut_54 else "hue=h=54,format=yuv420p",
             _rb(1)),  # hue+54, pitch+1
            (12, f"movie={clut_108_30},[in]haldclut,hflip,format=yuv420p" if clut_108_30 else "hue=h=108,hflip,format=yuv420p",
             _rb(3)),  # hue+108+sat30, hflip, pitch+3
        ]

        for seg_num, vf, af in short_specs:
            seg_path = os.path.join(tmpdir, f"{seg_num}.avi")
            cmd = ["ffmpeg", "-y", "-i", avi0]
            if vf:
                cmd.extend(["-vf", vf])
            if af:
                cmd.extend(["-af", af])
            cmd.extend(["-t", str(t2), "-c:v", "ffv1", "-c:a", "pcm_s16le", seg_path])
            segments.append((cmd, seg_path))

        # Render all segments
        for i, (cmd, seg_path) in enumerate(segments):
            ok, err = _run_ffmpeg_raw(cmd, timeout=120)
            if not ok:
                return False, f"Segment {i+1}/{len(segments)} failed: {err}"

        # Concat all segments using concat protocol
        avi_files = [sp for _, sp in segments if os.path.exists(sp)]
        if not avi_files:
            return False, "No segments were produced."

        concat_str = "|".join(avi_files)
        concat_out = os.path.join(tmpdir, "concat.avi")
        cmd = [
            "ffmpeg", "-y",
            "-i", f"concat:{concat_str}",
            "-vf", f"scale={w}:{h},setsar=1",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            output_path
        ]
        return _run_ffmpeg_raw(cmd, timeout=180)


# ---------- Bot events & commands ----------

@bot.event
async def on_ready():
    print(f"IHTX Bot online as {bot.user} (ID: {bot.user.id})")
    print("------")
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching,
        name="Meet the Sparkles! ✨👗 | Sparkles Magical Market Full Episode | Cartoons for Kids"
    ))


@bot.command(name="ihtx", aliases=["effect", "destroy"])
async def ihtx_command(ctx: commands.Context, *, args: str = "chaos"):
    """Apply an IHTX FFmpeg effect to an attached video or image.

    Usage:
      g!ihtx [preset]                  — use a built-in preset (chaos, glitch, etc.)
      g!ihtx effect=value,effect=value [rep] [dur]   — custom effect chain
    """
    # Parse arguments: could be a preset name, or an effect chain with optional rep/dur
    parts = args.split()
    first = parts[0].lower()

    # Check if it's a preset or a custom effect chain
    is_preset = first in VISUAL_PRESETS and "=" not in first
    is_chain = "=" in first

    if is_preset:
        preset = first
    elif is_chain:
        effects_str = first
        repetitions = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
        dur_override = float(parts[2]) if len(parts) > 2 else None
        repetitions = min(repetitions, MAX_REPETITIONS)
    else:
        # Unknown token — show help
        preset_list = ", ".join(f"`{p}`" for p in sorted(VISUAL_PRESETS))
        await ctx.reply(
            f"Unknown preset or effect. Available presets: {preset_list}\n"
            f"Or chain effects: `g!ihtx hflip,hue=90,multipitch=25;5;8.5 3 10`\n"
            f"Use `g!ihtxhelp` for full effect list."
        )
        return

    # Look for attachments (or referenced message attachments)
    attachment = None
    if ctx.message.attachments:
        attachment = ctx.message.attachments[0]
    elif ctx.message.reference:
        try:
            ref = await ctx.channel.fetch_message(ctx.message.reference.message_id)
            if ref.attachments:
                attachment = ref.attachments[0]
        except Exception:
            pass

    if not attachment:
        preset_list = ", ".join(f"`{p}`" for p in sorted(VISUAL_PRESETS))
        await ctx.reply(
            f"**I HATE THE X — IHTX Bot**\n"
            f"Attach a video or image and use `g!ihtx [preset]` or `g!ihtx effect=value,...`.\n\n"
            f"**Presets:** {preset_list}\n\n"
            f"**Custom effects:** `g!ihtx hflip,hue=90,multipitch=25;5;8.5 3 10`\n"
            f"Use `g!ihtxhelp` for full effect list.\n\n"
            f"Examples:\n"
            f"`g!ihtx chaos`\n"
            f"`g!ihtx glitch`\n"
            f"`g!ihtx mirror=45,hue=90,multipitch=25;5;8.5 3 10`"
        )
        return

    if attachment.size > MAX_FILE_SIZE:
        await ctx.reply(f"File too large (max 25 MB). Your file is {attachment.size / 1024 / 1024:.1f} MB.")
        return

    suffix = Path(attachment.filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        await ctx.reply(f"Unsupported file type `{suffix}`. Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
        return

    is_video = suffix in VIDEO_EXTENSIONS
    out_ext = get_output_ext(suffix, is_video)

    status_msg = await ctx.reply(
        f"⚙️ Applying **{'preset: ' + preset if is_preset else 'custom effects'}**... this may take a moment."
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, f"input{suffix}")
        output_path = os.path.join(tmpdir, f"output{out_ext}")

        try:
            await download_attachment(attachment, input_path)
        except Exception as e:
            await status_msg.edit(content=f"❌ Failed to download your file: {e}")
            return

        loop = asyncio.get_event_loop()

        if is_preset:
            ok, err = await loop.run_in_executor(
                None, run_ffmpeg, input_path, output_path, preset, is_video
            )
        else:
            # Custom effect chain — only supports video
            if not is_video:
                await status_msg.edit(content="❌ Custom effect chains require video input (not images/GIFs).")
                return
            ok, err = await loop.run_in_executor(
                None, _run_effect_chain,
                input_path, output_path, effects_str, repetitions, dur_override
            )

        if not ok:
            await status_msg.edit(content=f"❌ FFmpeg failed:\n```\n{err[-1500:]}\n```")
            return

        out_size = os.path.getsize(output_path)
        if out_size > MAX_FILE_SIZE:
            await status_msg.edit(content="❌ Output file too large for Discord (>25 MB). Try a shorter clip.")
            return

        if is_preset:
            out_filename = f"ihtx_{preset}_{Path(attachment.filename).stem}{out_ext}"
        else:
            out_filename = f"ihtx_custom_{Path(attachment.filename).stem}{out_ext}"

        try:
            await ctx.reply(
                content=f"✅ **IHTX `{'preset: ' + preset if is_preset else 'custom'}`** applied!",
                file=discord.File(output_path, filename=out_filename),
            )
            await status_msg.delete()
        except discord.HTTPException as e:
            await status_msg.edit(content=f"❌ Failed to upload result: {e}")


@bot.command(name="preview1280", aliases=["p1280"])
async def preview1280_command(ctx: commands.Context, start: float = 1.85, duration: float = 0.85):
    """Create a 12-segment TV-simulator preview montage from an attached video.

    Usage: g!preview1280 [start_offset] [segment_duration]
    Default: start=1.85, duration=0.85
    """
    # Look for attachments
    attachment = None
    if ctx.message.attachments:
        attachment = ctx.message.attachments[0]
    elif ctx.message.reference:
        try:
            ref = await ctx.channel.fetch_message(ctx.message.reference.message_id)
            if ref.attachments:
                attachment = ref.attachments[0]
        except Exception:
            pass

    if not attachment:
        await ctx.reply(
            "**IHTX Preview1280**\n"
            "Attach a video and use `g!preview1280 [start] [duration]`.\n\n"
            "Creates a 12-segment TV-simulator montage with hue shifts, "
            "displacement mapping, and pitch variations.\n\n"
            "Defaults: start=1.85s, duration=0.85s per segment.\n"
            "Example: `g!preview1280 2.0 1.0`"
        )
        return

    if attachment.size > MAX_FILE_SIZE:
        await ctx.reply(f"File too large (max 25 MB). Your file is {attachment.size / 1024 / 1024:.1f} MB.")
        return

    suffix = Path(attachment.filename).suffix.lower()
    if suffix not in VIDEO_EXTENSIONS:
        await ctx.reply(f"Preview1280 requires a video file. Got `{suffix}`.")
        return

    start = max(0.0, start)
    duration = max(0.1, min(duration, 10.0))

    status_msg = await ctx.reply(
        f"⚙️ Creating **preview1280** montage (start={start}s, dur={duration}s)... this will take a while."
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, f"input{suffix}")
        output_path = os.path.join(tmpdir, "output_p1280.mp4")

        try:
            await download_attachment(attachment, input_path)
        except Exception as e:
            await status_msg.edit(content=f"❌ Failed to download your file: {e}")
            return

        # Ensure displacement map is available
        try:
            disp_path = await _ensure_displacement_map(tmpdir)
        except FileNotFoundError as e:
            await status_msg.edit(content=f"❌ {e}")
            return

        loop = asyncio.get_event_loop()
        ok, err = await loop.run_in_executor(
            None, _run_preview1280, input_path, output_path, start, duration
        )

        if not ok:
            await status_msg.edit(content=f"❌ Preview1280 failed:\n```\n{err[-1500:]}\n```")
            return

        out_size = os.path.getsize(output_path)
        if out_size > MAX_FILE_SIZE:
            await status_msg.edit(content="❌ Output file too large for Discord (>25 MB). Try shorter segments.")
            return

        out_filename = f"p1280_{Path(attachment.filename).stem}.mp4"
        try:
            await ctx.reply(
                content=f"✅ **IHTX preview1280** applied!",
                file=discord.File(output_path, filename=out_filename),
            )
            await status_msg.delete()
        except discord.HTTPException as e:
            await status_msg.edit(content=f"❌ Failed to upload result: {e}")




@bot.command(name="multipitch", aliases=["mp", "multi"])
async def multipitch_command(ctx: commands.Context, *, args: str = ""):
    """Apply multi-voice pitch shifting to an attached video using SoX pitch.

    Usage:
      g!multipitch <pitch_values>     — semicolon-separated semitone values
      g!mp 25;5;8.5                    — aliases
      g!multi -3;0;5                  — negative values supported

    Example: g!multipitch 25;5;8.5
    """
    if not args:
        await ctx.reply(
            "**IHTX Multipitch**\n"
            "Attach a video and use `g!multipitch <pitches>`.\n\n"
            "Pitches are semicolon-separated semitone values.\n"
            "Each pitch creates a separate shifted voice, then they are mixed together.\n\n"
            "Example: `g!multipitch 25;5;8.5`\n"
            "Aliases: `g!mp`, `g!multi`"
        )
        return

    # Parse semicolon-separated pitch values
    pitch_values = [v.strip() for v in args.split(";") if v.strip()]
    if not pitch_values:
        await ctx.reply("No pitch values provided. Use semicolon-separated values like `25;5;8.5`.")
        return

    # Look for attachments
    attachment = None
    if ctx.message.attachments:
        attachment = ctx.message.attachments[0]
    elif ctx.message.reference:
        try:
            ref = await ctx.channel.fetch_message(ctx.message.reference.message_id)
            if ref.attachments:
                attachment = ref.attachments[0]
        except Exception:
            pass

    if not attachment:
        await ctx.reply(
            "Attach a video and use `g!multipitch <pitches>`.\n"
            "Example: `g!multipitch 25;5;8.5`"
        )
        return

    if attachment.size > MAX_FILE_SIZE:
        await ctx.reply(f"File too large (max 25 MB). Your file is {attachment.size / 1024 / 1024:.1f} MB.")
        return

    suffix = Path(attachment.filename).suffix.lower()
    if suffix not in VIDEO_EXTENSIONS:
        await ctx.reply(f"Multipitch requires a video file. Got `{suffix}`.")
        return

    pitch_str = ";".join(pitch_values)
    status_msg = await ctx.reply(
        f"⚙️ Applying **multipitch** ({pitch_str})... this may take a moment."
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, f"input{suffix}")
        output_path = os.path.join(tmpdir, "output_multipitch.mp4")

        try:
            await download_attachment(attachment, input_path)
        except Exception as e:
            await status_msg.edit(content=f"❌ Failed to download your file: {e}")
            return

        loop = asyncio.get_event_loop()
        ok, err = await loop.run_in_executor(
            None, _run_multipitch,
            input_path, output_path, pitch_values
        )

        if not ok:
            await status_msg.edit(content=f"❌ Multipitch failed:\n```\n{err[-1500:]}\n```")
            return

        out_size = os.path.getsize(output_path)
        if out_size > MAX_FILE_SIZE:
            await status_msg.edit(content="❌ Output file too large for Discord (>25 MB). Try a shorter clip.")
            return

        out_filename = f"multipitch_{pitch_str}_{Path(attachment.filename).stem}.mp4"
        try:
            await ctx.reply(
                content=f"✅ **IHTX multipitch** ({pitch_str}) applied!",
                file=discord.File(output_path, filename=out_filename),
            )
            await status_msg.delete()
        except discord.HTTPException as e:
            await status_msg.edit(content=f"❌ Failed to upload result: {e}")


@bot.command(name="presets", aliases=["effects", "list"])
async def presets_command(ctx: commands.Context):
    """List all available IHTX presets."""
    lines = [f"`{name}` — {PRESET_FILTERS[name]['vf'] or PRESET_FILTERS[name]['complex']}" for name in sorted(PRESET_FILTERS)]
    embed = discord.Embed(
        title="IHTX Bot — Available Presets",
        description="\n".join(lines),
        color=discord.Color.red(),
    )
    embed.add_field(
        name="Usage",
        value="Attach a video or image and run:\n`g!ihtx [preset]`\n\nDefault preset: `chaos`",
        inline=False,
    )
    embed.set_footer(text="I Hate The X — FFmpeg logo destruction bot")
    await ctx.reply(embed=embed)


@bot.command(name="ihtxhelp", aliases=["bothelp"])
async def help_command(ctx: commands.Context):
    embed = discord.Embed(
        title="IHTX Bot — Help",
        color=discord.Color.dark_red(),
    )
    embed.add_field(
        name="g!ihtx [preset]",
        value="Apply a preset effect to an attached video/image.\nDefault preset: `chaos`",
        inline=False,
    )
    embed.add_field(
        name="g!ihtx effect=value,effect=value [rep] [dur]",
        value="Chain custom effects. Params use `=`, sub-params use `;`.\n"
              "Example: `g!ihtx mirror=45,hue=90,multipitch=25;5;8.5 3 10`",
        inline=False,
    )
    embed.add_field(
        name="g!preview1280 [start] [dur]",
        value="12-segment TV-simulator montage.\nDefaults: start=1.85, dur=0.85",
        inline=False,
    )
    embed.add_field(
        name="g!multipitch <pitches>  (aliases: mp, multi)",
        value="Multi-voice pitch shift via SoX pitch.\n"
              "Semicolon-separated semitones: `g!multipitch 25;5;8.5`",
        inline=False,
    )
    embed.add_field(
        name="g!presets",
        value="List all available effect presets.",
        inline=False,
    )
    # Effect reference
    video_effects = (
        "hflip, vflip, invert, invlum, invertrgb=r;g;b, grayscale, sepia, "
        "rotate=<deg>, hue=<deg>, ffmpeghue=<deg>, brightness=<val>, "
        "contrast=<val>, saturation=<val>, swapuv, gm4, realgm4"
    )
    distortion_effects = (
        "fisheye=strength;radius;cx;cy, swirl=angle;radius;cx;cy;fallout;lock, "
        "wave=hs;hf;ha;hp;vs;vf;va;vp, zoom=<scale>, mirror=<angle>, "
        "tile=x;y, polar, depolar, orb, deorb, gm91deform"
    )
    transform_effects = (
        "scroll=h;v, pan=x;y, vreverse, watermark=<url>, ring[=<url>], "
        "miui, reddit, caption=<text>"
    )
    audio_effects = "multipitch=<semitones> (semi-sep: 25;5;8.5), volume=<val>, vibrato=freq;depth, areverse"
    lut_effects = "lut=<url>, invlum, ffmpeg(<raw args>)"

    embed.add_field(name="Video Effects", value=video_effects, inline=False)
    embed.add_field(name="Distortion", value=distortion_effects, inline=False)
    embed.add_field(name="Transform/Overlay", value=transform_effects, inline=False)
    embed.add_field(name="Audio", value=audio_effects, inline=False)
    embed.add_field(name="LUT/Raw", value=lut_effects, inline=False)

    embed.add_field(
        name="Supported formats",
        value=", ".join(sorted(SUPPORTED_EXTENSIONS)),
        inline=False,
    )
    embed.add_field(
        name="Max file size",
        value=f"{MAX_FILE_SIZE // (1024*1024)} MB",
        inline=False,
    )
    embed.set_footer(text="I Hate The X — FFmpeg logo destruction bot")
    await ctx.reply(embed=embed)


# ---------- Owner-only moderation / utility commands ----------

def _parse_digits(s: str) -> int:
    """Extract numeric ID from mention or plain id string."""
    if not s:
        raise ValueError("No id provided")
    m = re.search(r"(\d{6,20})", s)
    if m:
        return int(m.group(1))
    try:
        return int(s)
    except Exception:
        raise ValueError("Could not parse id")


@bot.command(name="blockuser")
@commands.check(_is_owner)
async def blockuser(ctx: commands.Context, user: str):
    """Owner-only: add a user ID or mention to the user blocklist."""
    try:
        user_id = _parse_digits(user)
    except ValueError:
        await ctx.reply("❌ Invalid user. Provide a mention or numeric ID.")
        return
    if user_id in blocklist:
        await ctx.reply(f"User `{user_id}` is already blocked.")
        return
    blocklist.add(user_id)
    _save_blocklist()
    await ctx.reply(f"✅ Blocked user `{user_id}`.")


@bot.command(name="unblockuser")
@commands.check(_is_owner)
async def unblockuser(ctx: commands.Context, user: str):
    """Owner-only: remove a user ID or mention from the user blocklist."""
    try:
        user_id = _parse_digits(user)
    except ValueError:
        await ctx.reply("❌ Invalid user. Provide a mention or numeric ID.")
        return
    if user_id not in blocklist:
        await ctx.reply(f"User `{user_id}` is not blocked.")
        return
    blocklist.discard(user_id)
    _save_blocklist()
    await ctx.reply(f"✅ Unblocked user `{user_id}`.")


@bot.command(name="blockchannel")
@commands.check(_is_owner)
async def blockchannel(ctx: commands.Context, channel: str = None):
    """Owner-only: add a channel to the channel blocklist. If omitted, blocks current channel."""
    if channel is None:
        channel_id = ctx.channel.id
    else:
        try:
            channel_id = _parse_digits(channel)
        except ValueError:
            await ctx.reply("❌ Invalid channel. Provide a channel mention or numeric ID.")
            return
    if channel_id in channel_blocks:
        await ctx.reply(f"Channel `{channel_id}` is already blocked.")
        return
    channel_blocks.add(channel_id)
    _save_channel_blocks()
    await ctx.reply(f"✅ Blocked channel `{channel_id}`.")


@bot.command(name="unblockchannel")
@commands.check(_is_owner)
async def unblockchannel(ctx: commands.Context, channel: str = None):
    """Owner-only: remove a channel from the channel blocklist. If omitted, unblocks current channel."""
    if channel is None:
        channel_id = ctx.channel.id
    else:
        try:
            channel_id = _parse_digits(channel)
        except ValueError:
            await ctx.reply("❌ Invalid channel. Provide a channel mention or numeric ID.")
            return
    if channel_id not in channel_blocks:
        await ctx.reply(f"Channel `{channel_id}` is not blocked.")
        return
    channel_blocks.discard(channel_id)
    _save_channel_blocks()
    await ctx.reply(f"✅ Unblocked channel `{channel_id}`.")


@bot.command(name="say")
@commands.check(_is_owner)
async def say(ctx: commands.Context, *, message: str):
    """Owner-only: make the bot send a plain message in the current channel."""
    try:
        await ctx.send(message)
        await ctx.message.add_reaction("✅")
    except Exception as e:
        await ctx.reply(f"❌ Failed to send message: {e}")


@bot.command(name="sayembed")
@commands.check(_is_owner)
async def sayembed(ctx: commands.Context, *, content: str):
    """
    Owner-only: send an embed.
    If `content` contains a '|' it will split into title|description, otherwise content is used as description.
    Example:
      g!sayembed Title | This is the embed body
    """
    try:
        if "|" in content:
            title, desc = [p.strip() for p in content.split("|", 1)]
        else:
            title = ""
            desc = content
        emb = discord.Embed(title=title or None, description=desc or None, color=discord.Color.dark_red())
        await ctx.send(embed=emb)
        await ctx.message.add_reaction("✅")
    except Exception as e:
        await ctx.reply(f"❌ Failed to send embed: {e}")


# ---------- Error handling & run ----------

@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply(f"Missing argument: `{error.param.name}`. Use `g!ihtxhelp` for usage.")
        return
    # Permission errors from owner-only commands
    if isinstance(error, commands.CheckFailure):
        # If check failed for owner-only commands, be quiet (or you could notify)
        return
    raise error


if __name__ == "__main__":
    if not TOKEN:
        print("ERROR: DISCORD_TOKEN environment variable not set.", file=sys.stderr)
        sys.exit(1)
    bot.run(TOKEN)
