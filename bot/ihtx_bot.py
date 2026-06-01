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
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".gif"}
MAX_FILE_SIZE = 25 * 1024 * 1024  # 25 MB

# ─── Effect definitions ───────────────────────────────────────────────────────

IHTX_PRESETS = {
    "chaos":   "extreme glitch + color explosion",
    "glitch":  "heavy RGB datamosh glitch",
    "shake":   "violent shake + zoom pulse",
    "rainbow": "chromatic aberration + hue spin",
    "static":  "VHS TV static noise overlay",
    "melt":    "perspective warp melt",
    "corrupt": "scanlines + gamma crush",
    "huehsv":  "haldclut hue rotation via ImageMagick  — usage: !huehsv [0‑1]",
    "pinch":   "pinch/punch lens warp via geq  — usage: !pinch [strength] [radius] [cx] [cy]",
}

_BASE_NOISE = "noise=alls=40:allf=t+u"
_SHAKE = "crop=iw-20:ih-20:10+5*sin(t*30):10+5*cos(t*17),scale=iw+20:ih+20"
_CHROMAB = (
    "[0:v]split=3[r][g][b];"
    "[r]lutrgb=r=val:g=0:b=0,pad=iw+6:ih:3:0[ro];"
    "[g]lutrgb=r=0:g=val:b=0[go];"
    "[b]lutrgb=r=0:g=0:b=val,pad=iw+6:ih:0:0[bo];"
    "[ro][go]blend=all_mode=addition[rg];"
    "[rg][bo]blend=all_mode=addition"
)

# Each entry: {"vf": str|None, "complex": str|None}
_PRESET_MAP: dict[str, dict] = {
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
        "complex": _CHROMAB,
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


def build_pinch_vf(strength: float = 1.0, radius: float = 0.5,
                   cx: float = 0.5, cy: float = 0.5) -> str:
    """Build the FFmpeg vf string for the Pinch and Punch lens-warp effect."""
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
    """
    Generate a HaldCLUT PNG via ImageMagick with hue rotation.
    hue_value = amount * 200 + 100  (ImageMagick modulate scale: 100 = no change)
    Returns the path to the generated hald clut PNG.
    """
    hue_val = int(amount * 200 + 100)
    clut_path = os.path.join(tmpdir, "hald_clut.png")
    cmd = ["magick", "hald:8", "-modulate", f"100,100,{hue_val}", clut_path]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"ImageMagick failed: {result.stderr}")
    return clut_path


# ─── FFmpeg runner ────────────────────────────────────────────────────────────

def _ffmpeg_single(input_path: str, output_path: str, vf: str | None,
                   fc: str | None, is_video: bool, duration: int = 30) -> tuple[bool, str]:
    """Run ffmpeg with a single input."""
    if is_video:
        if fc:
            cmd = [
                "ffmpeg", "-y", "-i", input_path,
                "-filter_complex", fc,
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "copy", "-t", str(duration),
                output_path,
            ]
        else:
            cmd = [
                "ffmpeg", "-y", "-i", input_path,
                "-vf", vf,
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "copy", "-t", str(duration),
                output_path,
            ]
    else:
        # Static image → 3-second animated GIF
        palette_chain = ",split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"
        if fc:
            cmd = [
                "ffmpeg", "-y",
                "-loop", "1", "-i", input_path,
                "-filter_complex", fc + palette_chain,
                "-t", "3", output_path,
            ]
        else:
            cmd = [
                "ffmpeg", "-y",
                "-loop", "1", "-i", input_path,
                "-vf", vf + palette_chain,
                "-t", "3", output_path,
            ]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return (True, "") if r.returncode == 0 else (False, r.stderr[-2000:])
    except subprocess.TimeoutExpired:
        return False, "FFmpeg timed out (>120s)"
    except Exception as e:
        return False, str(e)


def _ffmpeg_haldclut(input_path: str, clut_path: str, output_path: str,
                     is_video: bool) -> tuple[bool, str]:
    """Run ffmpeg with haldclut applied from a second input (the CLUT image)."""
    if is_video:
        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-i", clut_path,
            "-filter_complex", "[0:v][1:v]haldclut",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "copy", "-t", "30",
            output_path,
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", input_path,
            "-i", clut_path,
            "-filter_complex",
            "[0:v][1:v]haldclut,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
            "-t", "3", output_path,
        ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return (True, "") if r.returncode == 0 else (False, r.stderr[-2000:])
    except subprocess.TimeoutExpired:
        return False, "FFmpeg timed out (>120s)"
    except Exception as e:
        return False, str(e)


def run_preset(input_path: str, output_path: str, preset: str,
               is_video: bool) -> tuple[bool, str]:
    cfg = _PRESET_MAP[preset]
    return _ffmpeg_single(input_path, output_path, cfg["vf"], cfg["complex"], is_video)


# ─── Download helper ──────────────────────────────────────────────────────────

async def download_attachment(attachment: discord.Attachment, dest: str):
    async with aiohttp.ClientSession() as session:
        async with session.get(attachment.url) as resp:
            if resp.status != 200:
                raise ValueError(f"HTTP {resp.status}")
            data = await resp.read()
    with open(dest, "wb") as f:
        f.write(data)


# ─── Shared processing core ───────────────────────────────────────────────────

async def process_and_reply(
    ctx: commands.Context,
    effect_label: str,
    desc: str,
    worker,          # sync callable(input_path, output_path, tmpdir, is_video) -> (ok, err)
):
    if not ctx.message.attachments:
        await ctx.reply(f"Attach a video or image and re-run the command.")
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
    out_ext = ".mp4" if is_video else ".gif"

    status_msg = await ctx.reply(f"⚙️ Applying **{effect_label}** ({desc})…")

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, f"input{suffix}")
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
            await status_msg.edit(content="❌ Output too large for Discord (>25 MB). Try a shorter clip.")
            return

        stem = Path(attachment.filename).stem
        out_filename = f"ihtx_{effect_label.replace(' ', '_')}_{stem}{out_ext}"
        try:
            await ctx.reply(
                content=f"✅ **IHTX `{effect_label}`** applied!",
                file=discord.File(output_path, filename=out_filename),
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
async def ihtx_command(ctx: commands.Context, preset: str = "chaos"):
    """Apply an IHTX FFmpeg effect preset. Attach a file.
    Usage: !ihtx [preset]
    For huehsv or pinch with args use the dedicated commands instead.
    """
    preset = preset.lower()
    if preset not in IHTX_PRESETS:
        plist = ", ".join(f"`{p}`" for p in IHTX_PRESETS)
        await ctx.reply(f"Unknown preset. Available: {plist}")
        return

    # Route special presets to their dedicated handlers
    if preset == "huehsv":
        await huehsv_command(ctx, 0.5)
        return
    if preset == "pinch":
        await pinch_command(ctx, 1.0, 0.5, 0.5, 0.5)
        return

    if not ctx.message.attachments:
        plist = ", ".join(f"`{p}`" for p in IHTX_PRESETS)
        await ctx.reply(
            f"**I Hate The X — IHTX Bot**\n"
            f"Attach a video or image and use `!ihtx [preset]`.\n\n"
            f"**Presets:** {plist}\n\n"
            f"`!ihtx chaos` — {IHTX_PRESETS['chaos']}\n"
            f"`!ihtx glitch` — {IHTX_PRESETS['glitch']}\n"
            f"`!ihtx rainbow` — {IHTX_PRESETS['rainbow']}\n"
            f"`!huehsv [0‑1]` — hue rotation\n"
            f"`!pinch [strength] [radius] [cx] [cy]` — lens warp\n"
        )
        return

    def worker(input_path, output_path, tmpdir, is_video):
        return run_preset(input_path, output_path, preset, is_video)

    await process_and_reply(ctx, preset, IHTX_PRESETS[preset], worker)


@bot.command(name="huehsv")
async def huehsv_command(ctx: commands.Context, amount: float = 0.5):
    """Apply HueHSV haldclut hue rotation via ImageMagick.

    !huehsv [amount]
      amount — 0.0 to 1.0  (default 0.5)
               Hue value = amount × 200 + 100 in ImageMagick modulate scale
               0.0 → hue 100 (no shift), 0.5 → hue 200 (+180°), 1.0 → hue 300 (wraps)
    """
    amount = max(0.0, min(2.0, float(amount)))

    def worker(input_path, output_path, tmpdir, is_video):
        try:
            clut_path = build_huehsv_haldclut(tmpdir, amount)
        except RuntimeError as e:
            return False, str(e)
        return _ffmpeg_haldclut(input_path, clut_path, output_path, is_video)

    hue_val = int(amount * 200 + 100)
    await process_and_reply(
        ctx,
        f"huehsv({amount:.2f})",
        f"HaldCLUT hue rotation — modulate hue={hue_val}",
        worker,
    )


@bot.command(name="pinch")
async def pinch_command(
    ctx: commands.Context,
    strength: float = 1.0,
    radius: float = 0.5,
    cx: float = 0.5,
    cy: float = 0.5,
):
    """Apply Pinch and Punch lens-warp effect via geq.

    !pinch [strength] [radius] [cx] [cy]
      strength — distortion intensity, can be negative for punch-out (default 1.0)
      radius   — effect radius as fraction of image size (default 0.5)
      cx       — horizontal center as fraction of width  (default 0.5)
      cy       — vertical center as fraction of height   (default 0.5)

    Examples:
      !pinch              → default pinch-in
      !pinch -1           → punch-out
      !pinch 2 0.3        → strong pinch, smaller radius
      !pinch 1 0.5 0.2 0.8 → off-center warp
    """
    strength = float(strength)
    radius = max(0.01, float(radius))
    cx = max(0.0, min(1.0, float(cx)))
    cy = max(0.0, min(1.0, float(cy)))

    vf = build_pinch_vf(strength, radius, cx, cy)

    def worker(input_path, output_path, tmpdir, is_video):
        return _ffmpeg_single(input_path, output_path, vf, None, is_video)

    direction = "pinch-in" if strength > 0 else "punch-out"
    await process_and_reply(
        ctx,
        f"pinch(s={strength},r={radius},cx={cx},cy={cy})",
        f"{direction} lens warp — strength={strength}, radius={radius}",
        worker,
    )


@bot.command(name="presets", aliases=["effects", "list"])
async def presets_command(ctx: commands.Context):
    """List all available IHTX effects."""
    embed = discord.Embed(
        title="IHTX Bot — Effects",
        color=discord.Color.red(),
    )
    standard = {k: v for k, v in IHTX_PRESETS.items() if k not in ("huehsv", "pinch")}
    special   = {k: v for k, v in IHTX_PRESETS.items() if k in ("huehsv", "pinch")}

    embed.add_field(
        name="Standard presets — `!ihtx [preset]`",
        value="\n".join(f"`{k}` — {v}" for k, v in standard.items()),
        inline=False,
    )
    embed.add_field(
        name="Parameterised effects",
        value=(
            "`!huehsv [amount]` — HaldCLUT hue rotation (0‑1, default 0.5)\n"
            "`!pinch [strength] [radius] [cx] [cy]` — lens pinch/punch warp"
        ),
        inline=False,
    )
    embed.set_footer(text="I Hate The X — FFmpeg logo destruction bot")
    await ctx.reply(embed=embed)


@bot.command(name="ihtxhelp", aliases=["bothelp"])
async def help_command(ctx: commands.Context):
    embed = discord.Embed(title="IHTX Bot — Help", color=discord.Color.dark_red())
    embed.add_field(
        name="!ihtx [preset]",
        value="Apply a preset IHTX effect to an attachment. Default: `chaos`",
        inline=False,
    )
    embed.add_field(
        name="!huehsv [amount]",
        value=(
            "HaldCLUT hue rotation via ImageMagick.\n"
            "`amount` 0‑1 (default 0.5) → hue value = amount×200+100\n"
            "Example: `!huehsv 0.75`"
        ),
        inline=False,
    )
    embed.add_field(
        name="!pinch [strength] [radius] [cx] [cy]",
        value=(
            "Pinch/punch lens warp via FFmpeg geq.\n"
            "`strength` — intensity, negative = punch-out (default 1.0)\n"
            "`radius`   — effect radius, fraction of image (default 0.5)\n"
            "`cx` `cy`  — warp center, 0‑1 (default 0.5 0.5)\n"
            "Example: `!pinch -1 0.4 0.3 0.6`"
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
        value="mp4, mov, avi, mkv, webm, gif, png, jpg, jpeg, webp (max 25 MB)",
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
