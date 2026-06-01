import discord
from discord.ext import commands
import asyncio
import os
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

SUPPORTED_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".gif", ".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXTENSIONS     = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".gif"}
AUDIO_VIDEO_EXTS     = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
MAX_FILE_SIZE        = 25 * 1024 * 1024   # 25 MB
MAX_REPETITIONS      = 100

# ─── Effect definitions ───────────────────────────────────────────────────────

IHTX_PRESETS = {
    "chaos":   "extreme glitch + color explosion",
    "glitch":  "heavy RGB datamosh glitch",
    "shake":   "violent shake + zoom pulse",
    "rainbow": "chromatic aberration + hue spin",
    "static":  "VHS TV static noise overlay",
    "melt":    "perspective warp melt",
    "corrupt": "scanlines + gamma crush",
}

_BASE_NOISE = "noise=alls=40:allf=t+u"
_SHAKE      = "crop=iw-20:ih-20:10+5*sin(t*30):10+5*cos(t*17),scale=iw+20:ih+20"
_CHROMAB = (
    "[0:v]split=3[r][g][b];"
    "[r]lutrgb=r=val:g=0:b=0,pad=iw+6:ih:3:0[ro];"
    "[g]lutrgb=r=0:g=val:b=0[go];"
    "[b]lutrgb=r=0:g=0:b=val,pad=iw+6:ih:0:0[bo];"
    "[ro][go]blend=all_mode=addition[rg];"
    "[rg][bo]blend=all_mode=addition"
)

# vf: simple -vf chain | complex: -filter_complex graph
_PRESET_MAP: dict[str, dict] = {
    "chaos": {
        "vf":      f"{_SHAKE},{_BASE_NOISE},hue=h=t*180:s=2,eq=contrast=1.5:brightness=0.05:saturation=3",
        "complex": None,
    },
    "glitch": {
        "vf":      f"rgbashift=rh=8:rv=-8:gh=-4:gv=4:bh=6:bv=-6,{_BASE_NOISE},eq=contrast=1.8:saturation=0",
        "complex": None,
    },
    "shake": {
        "vf":      f"{_SHAKE},{_BASE_NOISE},eq=contrast=1.3:saturation=1.5",
        "complex": None,
    },
    "rainbow": {
        "vf":      None,
        "complex": _CHROMAB,
    },
    "static": {
        "vf":      f"{_BASE_NOISE},curves=vintage,eq=contrast=1.2",
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
        "vf":      f"drawgrid=x=0:y=0:w=iw:h=5:t=1:color=white@0.1,{_BASE_NOISE},eq=gamma=1.5:saturation=0.3:contrast=2",
        "complex": None,
    },
}


def build_pinch_vf(strength: float = 1.0, radius: float = 0.5,
                   cx: float = 0.5, cy: float = 0.5) -> str:
    gauss_arg = (
        f"-3.3333*pow(hypot("
        f"(X-W*{cx})/(W*{radius}),"
        f"(Y-H*{cy})/(H*{radius})"
        f"),2)"
    )
    px = f"W*{cx}+(X-W*{cx})*(1-({strength})*gauss({gauss_arg}))"
    py = f"H*{cy}+(Y-H*{cy})*(1-({strength})*gauss({gauss_arg}))"
    return f"format=yuv444p,geq='p({px},{py})',scale=iw:ih,format=yuv420p"


def build_huehsv_haldclut(tmpdir: str, amount: float = 0.5) -> str:
    hue_val   = int(amount * 200 + 100)
    clut_path = os.path.join(tmpdir, "hald_clut.png")
    result    = subprocess.run(
        ["magick", "hald:8", "-modulate", f"100,100,{hue_val}", clut_path],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ImageMagick failed: {result.stderr}")
    return clut_path


# ─── FFmpeg primitives ────────────────────────────────────────────────────────

def _run(cmd: list[str], timeout: int = 180) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (True, "") if r.returncode == 0 else (False, r.stderr[-2000:])
    except subprocess.TimeoutExpired:
        return False, f"FFmpeg timed out (>{timeout}s)"
    except Exception as e:
        return False, str(e)


def _ffmpeg_single(input_path: str, output_path: str, vf: str | None,
                   fc: str | None, is_video: bool, duration: int = 30) -> tuple[bool, str]:
    if is_video:
        if fc:
            cmd = ["ffmpeg", "-y", "-i", input_path,
                   "-filter_complex", fc,
                   "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                   "-c:a", "copy", "-t", str(duration), output_path]
        else:
            cmd = ["ffmpeg", "-y", "-i", input_path,
                   "-vf", vf,
                   "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                   "-c:a", "copy", "-t", str(duration), output_path]
    else:
        pal = ",split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"
        if fc:
            cmd = ["ffmpeg", "-y", "-loop", "1", "-i", input_path,
                   "-filter_complex", fc + pal, "-t", "3", output_path]
        else:
            cmd = ["ffmpeg", "-y", "-loop", "1", "-i", input_path,
                   "-vf", vf + pal, "-t", "3", output_path]
    return _run(cmd)


def _ffmpeg_haldclut(input_path: str, clut_path: str, output_path: str,
                     is_video: bool, duration: int = 30) -> tuple[bool, str]:
    if is_video:
        cmd = ["ffmpeg", "-y",
               "-i", input_path, "-i", clut_path,
               "-filter_complex", "[0:v][1:v]haldclut",
               "-c:v", "libx264", "-preset", "fast", "-crf", "23",
               "-c:a", "copy", "-t", str(duration), output_path]
    else:
        cmd = ["ffmpeg", "-y",
               "-loop", "1", "-i", input_path, "-i", clut_path,
               "-filter_complex",
               "[0:v][1:v]haldclut,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
               "-t", "3", output_path]
    return _run(cmd)


def _ffmpeg_multipass_preset(
    input_path: str, output_path: str, tmpdir: str,
    preset: str, is_video: bool,
    repetitions: int, duration: int,
) -> tuple[bool, str]:
    """Apply a preset effect N times, piping each pass into the next."""
    cfg  = _PRESET_MAP[preset]
    ext  = Path(input_path).suffix
    cur  = input_path

    for i in range(repetitions):
        is_last = (i == repetitions - 1)
        nxt     = output_path if is_last else os.path.join(tmpdir, f"pass_{i}{ext}")
        ok, err = _ffmpeg_single(cur, nxt, cfg["vf"], cfg["complex"], is_video, duration)
        if not ok:
            return False, f"Pass {i+1}/{repetitions} failed: {err}"
        cur = nxt

    return True, ""


def _ffmpeg_multipitch(
    input_path: str, output_path: str,
    semitones_list: list[float],
) -> tuple[bool, str]:
    """
    Split audio into N streams, pitch-shift each via rubberband, mix together.
    Video stream is copied untouched.
    """
    n   = len(semitones_list)
    fc  = f"[0:a]asplit={n}" + "".join(f"[a{i}]" for i in range(n)) + ";"
    fc += ";".join(
        f"[a{i}]rubberband=pitch={2 ** (st / 12):.6f}[p{i}]"
        for i, st in enumerate(semitones_list)
    ) + ";"
    fc += "".join(f"[p{i}]" for i in range(n)) + f"amix=inputs={n}:normalize=0[aout]"

    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-filter_complex", fc,
        "-map", "0:v", "-map", "[aout]",
        "-c:v", "copy",
        "-t", "300",
        output_path,
    ]
    return _run(cmd, timeout=300)


# ─── Download helper ──────────────────────────────────────────────────────────

async def download_attachment(attachment: discord.Attachment, dest: str):
    async with aiohttp.ClientSession() as session:
        async with session.get(attachment.url) as resp:
            if resp.status != 200:
                raise ValueError(f"HTTP {resp.status}")
            with open(dest, "wb") as f:
                f.write(await resp.read())


# ─── Shared processing core ───────────────────────────────────────────────────

async def process_and_reply(
    ctx: commands.Context,
    effect_label: str,
    desc: str,
    worker,   # sync callable(input_path, output_path, tmpdir, is_video) -> (ok, err)
    out_ext_override: str | None = None,
):
    if not ctx.message.attachments:
        await ctx.reply("Attach a video or image and re-run the command.")
        return

    attachment = ctx.message.attachments[0]
    if attachment.size > MAX_FILE_SIZE:
        await ctx.reply(f"File too large ({attachment.size / 1024 / 1024:.1f} MB, max 25 MB).")
        return

    suffix = Path(attachment.filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        await ctx.reply(f"Unsupported type `{suffix}`. Use: {', '.join(SUPPORTED_EXTENSIONS)}")
        return

    is_video = suffix in VIDEO_EXTENSIONS
    out_ext  = out_ext_override or (".mp4" if is_video else ".gif")

    status_msg = await ctx.reply(f"⚙️ Applying **{effect_label}** — {desc}…")

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
            ok, err = await loop.run_in_executor(
                None, lambda: worker(input_path, output_path, tmpdir, is_video)
            )
        except Exception as e:
            await status_msg.edit(content=f"❌ Processing error: {e}")
            return

        if not ok:
            await status_msg.edit(content=f"❌ FFmpeg failed:\n```\n{err[-1500:]}\n```")
            return

        if os.path.getsize(output_path) > MAX_FILE_SIZE:
            await status_msg.edit(content="❌ Output too large for Discord (>25 MB). Try shorter clip or fewer repetitions.")
            return

        stem        = Path(attachment.filename).stem
        out_fn      = f"ihtx_{effect_label.replace(' ', '_')}_{stem}{out_ext}"
        try:
            await ctx.reply(
                content=f"✅ **{effect_label}** applied!",
                file=discord.File(output_path, filename=out_fn),
            )
            await status_msg.delete()
        except discord.HTTPException as e:
            await status_msg.edit(content=f"❌ Upload failed: {e}")


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
async def ihtx_command(
    ctx: commands.Context,
    preset: str = "chaos",
    repetitions: int = 1,
    duration: int = 30,
):
    """Apply an IHTX effect preset with optional repetitions and duration.

    !ihtx [preset] [repetitions=1] [duration=30]
      preset      — effect name (default: chaos)
      repetitions — 1–100, how many times to chain the effect (default: 1)
      duration    — output length in seconds (default: 30)

    Examples:
      !ihtx glitch         → glitch once, 30s
      !ihtx chaos 5        → chaos applied 5× in sequence
      !ihtx shake 3 10     → shake 3× on a 10-second output
    """
    preset = preset.lower()

    # Route dedicated commands
    if preset == "huehsv":
        await huehsv_command(ctx, 0.5)
        return
    if preset == "pinch":
        await pinch_command(ctx, 1.0, 0.5, 0.5, 0.5)
        return
    if preset == "pitch":
        await pitch_command(ctx, "12")
        return

    if preset not in IHTX_PRESETS:
        plist = ", ".join(f"`{p}`" for p in IHTX_PRESETS)
        await ctx.reply(
            f"Unknown preset `{preset}`. Available: {plist}\n"
            f"Also: `!huehsv`, `!pinch`, `!pitch`"
        )
        return

    repetitions = max(1, min(MAX_REPETITIONS, repetitions))
    duration    = max(1, min(600, duration))

    if not ctx.message.attachments:
        plist = ", ".join(f"`{p}`" for p in IHTX_PRESETS)
        await ctx.reply(
            "**I Hate The X — IHTX Bot**\n"
            "Attach a video or image and run:\n"
            "`!ihtx [preset] [repetitions=1] [duration=30]`\n\n"
            f"**Presets:** {plist}\n"
            "**Dedicated:** `!huehsv` · `!pinch` · `!pitch`\n\n"
            "Example: `!ihtx glitch 5 15` — glitch effect ×5, 15 seconds"
        )
        return

    rep_label = f"×{repetitions}" if repetitions > 1 else ""
    label     = f"IHTX {preset}{rep_label}"
    desc      = f"{IHTX_PRESETS[preset]}, {repetitions} pass(es), {duration}s"

    if repetitions > 10:
        desc += " ⚠️ many passes — may take a while"

    def worker(input_path, output_path, tmpdir, is_video):
        return _ffmpeg_multipass_preset(
            input_path, output_path, tmpdir,
            preset, is_video, repetitions, duration,
        )

    await process_and_reply(ctx, label, desc, worker)


@bot.command(name="huehsv")
async def huehsv_command(ctx: commands.Context, amount: float = 0.5, duration: int = 30):
    """HueHSV haldclut hue rotation via ImageMagick.

    !huehsv [amount] [duration=30]
      amount   — 0.0–1.0 (default 0.5); hue = amount×200+100
      duration — output seconds (default 30)
    """
    amount   = max(0.0, min(2.0, float(amount)))
    duration = max(1, min(600, int(duration)))

    def worker(input_path, output_path, tmpdir, is_video):
        try:
            clut_path = build_huehsv_haldclut(tmpdir, amount)
        except RuntimeError as e:
            return False, str(e)
        return _ffmpeg_haldclut(input_path, clut_path, output_path, is_video, duration)

    hue_val = int(amount * 200 + 100)
    await process_and_reply(
        ctx,
        f"huehsv({amount:.2f})",
        f"HaldCLUT hue rotation — modulate hue={hue_val}, {duration}s",
        worker,
    )


@bot.command(name="pinch")
async def pinch_command(
    ctx: commands.Context,
    strength: float = 1.0,
    radius: float   = 0.5,
    cx: float       = 0.5,
    cy: float       = 0.5,
    duration: int   = 30,
):
    """Pinch and Punch lens-warp via FFmpeg geq.

    !pinch [strength] [radius] [cx] [cy] [duration=30]
      strength — intensity; negative = punch-out (default 1.0)
      radius   — effect radius as fraction of image (default 0.5)
      cx / cy  — warp center 0–1 (default 0.5 0.5)
      duration — output seconds (default 30)
    """
    strength = float(strength)
    radius   = max(0.01, float(radius))
    cx       = max(0.0, min(1.0, float(cx)))
    cy       = max(0.0, min(1.0, float(cy)))
    duration = max(1, min(600, int(duration)))

    vf = build_pinch_vf(strength, radius, cx, cy)

    def worker(input_path, output_path, tmpdir, is_video):
        return _ffmpeg_single(input_path, output_path, vf, None, is_video, duration)

    direction = "pinch-in" if strength > 0 else "punch-out"
    await process_and_reply(
        ctx,
        f"pinch(s={strength},r={radius})",
        f"{direction} lens warp, {duration}s",
        worker,
    )


@bot.command(name="pitch")
async def pitch_command(ctx: commands.Context, *args: str):
    """Pitch-shift audio via rubberband. Supports multiple semitone values (multipitch chord).

    !pitch [semitone1] [semitone2] ...
      Provide one or more semitone values.
      Single value  → simple pitch shift.
      Multiple      → each shifted independently then mixed together (chord/harmony).

    Examples:
      !pitch 12           → octave up
      !pitch -12          → octave down
      !pitch 0 7 12       → root + perfect fifth + octave (power chord)
      !pitch -12 0 12     → sub-octave + original + octave above
      !pitch 0 4 7        → major chord (root + major third + fifth)
      !pitch 3.5          → minor third + quartertone up
    """
    # Parse semitone args
    if not args:
        semitones_list = [12.0]
    else:
        try:
            semitones_list = [max(-36.0, min(36.0, float(s))) for s in args]
        except ValueError:
            await ctx.reply("⚠️ Semitone values must be numbers. Example: `!pitch 0 7 12`")
            return

    # Cap at 8 simultaneous pitches (FFmpeg amix limit is generous but keep it sane)
    if len(semitones_list) > 8:
        await ctx.reply("⚠️ Maximum 8 simultaneous pitches. Truncating to first 8.")
        semitones_list = semitones_list[:8]

    if not ctx.message.attachments:
        examples = [
            "`!pitch 12` — octave up",
            "`!pitch -12` — octave down",
            "`!pitch 0 7 12` — power chord (root+5th+octave)",
            "`!pitch 0 4 7` — major chord",
            "`!pitch -12 0 12` — sub + original + octave",
        ]
        await ctx.reply(
            "Attach a **video with audio** (mp4, mov, mkv, webm, avi) and re-run.\n\n"
            "**Usage:** `!pitch [semitone1] [semitone2] ...`\n"
            "Multiple values = multipitch chord (mixed together)\n\n"
            + "\n".join(examples)
        )
        return

    attachment = ctx.message.attachments[0]
    suffix     = Path(attachment.filename).suffix.lower()

    if suffix not in AUDIO_VIDEO_EXTS:
        await ctx.reply("⚠️ `!pitch` works on video files with audio (mp4, mov, avi, mkv, webm).")
        return

    if attachment.size > MAX_FILE_SIZE:
        await ctx.reply(f"File too large ({attachment.size / 1024 / 1024:.1f} MB, max 25 MB).")
        return

    # Build label
    is_multi  = len(semitones_list) > 1
    st_str    = " + ".join(f"{s:+.1f}st" for s in semitones_list)
    ratios    = [2 ** (s / 12) for s in semitones_list]
    ratio_str = " + ".join(f"{r:.4f}×" for r in ratios)
    kind      = "multipitch" if is_multi else "pitch"
    direction = "▲" if not is_multi and semitones_list[0] >= 0 else ("▼" if not is_multi else "🎵")

    status_msg = await ctx.reply(
        f"⚙️ {'Multipitch mixing' if is_multi else 'Pitch shifting'} {direction} **{st_str}** "
        f"({ratio_str}) via rubberband…"
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path  = os.path.join(tmpdir, f"input{suffix}")
        output_path = os.path.join(tmpdir, f"output{suffix}")

        try:
            await download_attachment(attachment, input_path)
        except Exception as e:
            await status_msg.edit(content=f"❌ Download failed: {e}")
            return

        loop = asyncio.get_event_loop()
        ok, err = await loop.run_in_executor(
            None, lambda: _ffmpeg_multipitch(input_path, output_path, semitones_list)
        )

        if not ok:
            await status_msg.edit(content=f"❌ FFmpeg rubberband failed:\n```\n{err[-1500:]}\n```")
            return

        if os.path.getsize(output_path) > MAX_FILE_SIZE:
            await status_msg.edit(content="❌ Output too large for Discord (>25 MB).")
            return

        stem      = Path(attachment.filename).stem
        st_tag    = st_str.replace(" + ", "_").replace("+", "p").replace("-", "m").replace(".", "d")
        out_fn    = f"{kind}_{st_tag}_{stem}{suffix}"
        try:
            await ctx.reply(
                content=f"✅ **{'Multipitch' if is_multi else 'Pitch'} {st_str}** applied!",
                file=discord.File(output_path, filename=out_fn),
            )
            await status_msg.delete()
        except discord.HTTPException as e:
            await status_msg.edit(content=f"❌ Upload failed: {e}")


@bot.command(name="presets", aliases=["effects", "list"])
async def presets_command(ctx: commands.Context):
    embed = discord.Embed(title="IHTX Bot — Effects", color=discord.Color.red())
    embed.add_field(
        name="!ihtx [preset] [repetitions=1] [duration=30]",
        value="\n".join(f"`{k}` — {v}" for k, v in IHTX_PRESETS.items()),
        inline=False,
    )
    embed.add_field(
        name="Dedicated parameterised commands",
        value=(
            "`!huehsv [amount] [duration]` — HaldCLUT hue rotation (ImageMagick)\n"
            "`!pinch [strength] [radius] [cx] [cy] [duration]` — geq lens warp\n"
            "`!pitch [st1] [st2] ...` — rubberband pitch shift / multipitch chord"
        ),
        inline=False,
    )
    embed.set_footer(text="I Hate The X — FFmpeg logo destruction bot")
    await ctx.reply(embed=embed)


@bot.command(name="ihtxhelp", aliases=["bothelp"])
async def help_command(ctx: commands.Context):
    embed = discord.Embed(title="IHTX Bot — Help", color=discord.Color.dark_red())
    embed.add_field(
        name="!ihtx [preset] [repetitions=1] [duration=30]",
        value=(
            "Apply a visual IHTX effect. Attach a video or image.\n"
            "`repetitions` 1–100 — chains the effect that many times (each pass feeds the next)\n"
            "`duration` — output length in seconds (default 30)\n"
            "Example: `!ihtx chaos 10 20` — chaos ×10, 20s"
        ),
        inline=False,
    )
    embed.add_field(
        name="!huehsv [amount=0.5] [duration=30]",
        value=(
            "HaldCLUT hue rotation via ImageMagick.\n"
            "hue = amount×200+100  (0.0→no shift, 0.5→+180°, 1.0→wraps)\n"
            "Example: `!huehsv 0.75`"
        ),
        inline=False,
    )
    embed.add_field(
        name="!pinch [strength=1] [radius=0.5] [cx=0.5] [cy=0.5] [duration=30]",
        value=(
            "Pinch/punch lens warp via geq.\n"
            "Negative strength = punch-out. cx/cy = warp center (0–1).\n"
            "Example: `!pinch -1 0.4 0.3 0.6`"
        ),
        inline=False,
    )
    embed.add_field(
        name="!pitch [semitone1] [semitone2] ...",
        value=(
            "Pitch-shift audio via rubberband (video copied untouched).\n"
            "**Multiple values = multipitch** — each shifted independently, then mixed.\n"
            "`!pitch 12` — octave up\n"
            "`!pitch 0 7 12` — power chord\n"
            "`!pitch 0 4 7` — major chord\n"
            "`!pitch -12 0 12` — sub + root + octave"
        ),
        inline=False,
    )
    embed.add_field(
        name="!presets",
        value="List all available effects.",
        inline=False,
    )
    embed.add_field(
        name="Supported formats",
        value=(
            "Visual effects: mp4, mov, avi, mkv, webm, gif, png, jpg, jpeg, webp\n"
            "Audio effects (!pitch): mp4, mov, avi, mkv, webm only\n"
            "Max 25 MB"
        ),
        inline=False,
    )
    embed.set_footer(text="I Hate The X — FFmpeg logo destruction bot")
    await ctx.reply(embed=embed)


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.BadArgument):
        await ctx.reply(f"Bad argument: {error}. Use `!ihtxhelp` for usage.")
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply(f"Missing `{error.param.name}`. Use `!ihtxhelp` for usage.")
        return
    raise error


if __name__ == "__main__":
    bot.run(TOKEN)
