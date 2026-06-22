"""
EconomyCog — Server Economy, RPG Inventory & Fun Commands for IHTX Bot.

Commands (all hybrid — work as text prefix t! AND slash /):
  /profile [user]   — Rich profile card with Edit Bio modal + Inventory view
  /ihtxgen          — Slash-native t!ihtx pipeline with live embed feedback
  /slot             — Slot machine (777 → +200 XP), 1-hour cooldown

Data persistence: JSON file at bot/economy_data.json (zero external deps).
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

# ---------------------------------------------------------------------------
# Economy database — lightweight JSON store
# ---------------------------------------------------------------------------

_DB_PATH = Path("bot/economy_data.json")

_DEFAULT_USER: dict[str, Any] = {
    "wallet": 0,
    "bank": 0,
    "xp": 0,
    "bio": "No bio set.",
    "inventory": [],
}

_MOCK_SEED: dict[str, Any] = {}


class EconomyDB:
    """Thread-safe (asyncio-level) JSON database for user economy profiles."""

    def __init__(self, path: Path = _DB_PATH) -> None:
        self._path = path
        self._data: dict[str, Any] = {}
        self._load()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> None:
        try:
            if self._path.exists():
                with self._path.open("r", encoding="utf-8") as fh:
                    self._data = json.load(fh)
            else:
                self._data = dict(_MOCK_SEED)
        except Exception:
            self._data = dict(_MOCK_SEED)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2)
        tmp.replace(self._path)

    def _profile(self, user_id: int) -> dict[str, Any]:
        key = str(user_id)
        if key not in self._data:
            self._data[key] = dict(_DEFAULT_USER)
        return self._data[key]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, user_id: int) -> dict[str, Any]:
        return dict(self._profile(user_id))

    def set_bio(self, user_id: int, bio: str) -> None:
        self._profile(user_id)["bio"] = bio[:300]
        self._save()

    def add_xp(self, user_id: int, amount: int) -> int:
        p = self._profile(user_id)
        p["xp"] = max(0, p.get("xp", 0) + amount)
        self._save()
        return p["xp"]

    def add_wallet(self, user_id: int, amount: int) -> int:
        p = self._profile(user_id)
        p["wallet"] = max(0, p.get("wallet", 0) + amount)
        self._save()
        return p["wallet"]

    def add_inventory_item(self, user_id: int, item: str) -> None:
        p = self._profile(user_id)
        inv: list[str] = p.setdefault("inventory", [])
        inv.append(item)
        self._save()

    def level_from_xp(self, xp: int) -> int:
        """Simple formula: level = floor(sqrt(xp / 50))."""
        return max(1, int(math.sqrt(max(0, xp) / 50)))


# Module-level singleton
db = EconomyDB()


# ---------------------------------------------------------------------------
# UI — Bio Edit Modal
# ---------------------------------------------------------------------------

class BioModal(discord.ui.Modal, title="Edit Your Profile Bio"):
    bio_input: discord.ui.TextInput = discord.ui.TextInput(
        label="Bio",
        style=discord.TextStyle.paragraph,
        placeholder="Write something about yourself…",
        min_length=1,
        max_length=300,
        required=True,
    )

    def __init__(self, target_id: int) -> None:
        super().__init__()
        self._target_id = target_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        new_bio = self.bio_input.value.strip()
        db.set_bio(self._target_id, new_bio)
        await interaction.response.send_message(
            embed=discord.Embed(
                description=f"✅ Bio updated!\n> {new_bio}",
                color=0x57F287,
            ),
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# UI — Profile View (Edit Bio button + View Inventory button)
# ---------------------------------------------------------------------------

class ProfileView(discord.ui.View):
    def __init__(
        self,
        target: discord.User | discord.Member,
        author: discord.User | discord.Member,
        original_embed: discord.Embed,
    ) -> None:
        super().__init__(timeout=120)
        self._target = target
        self._author = author
        self._original_embed = original_embed
        self._showing_inventory = False

        # Only the profile owner can edit the bio
        if author.id != target.id:
            self.edit_bio_btn.disabled = True
            self.edit_bio_btn.label = "Edit Bio (owner only)"

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    @discord.ui.button(label="✏️ Edit Bio", style=discord.ButtonStyle.secondary)
    async def edit_bio_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self._author.id:
            await interaction.response.send_message(
                "❌ Only the profile owner can edit their bio.", ephemeral=True
            )
            return
        await interaction.response.send_modal(BioModal(self._target.id))

    @discord.ui.button(label="🎒 View Inventory", style=discord.ButtonStyle.primary)
    async def view_inventory_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer()
        self._showing_inventory = not self._showing_inventory

        if self._showing_inventory:
            button.label = "📊 View Stats"
            button.style = discord.ButtonStyle.success

            profile = db.get(self._target.id)
            inv: list[str] = profile.get("inventory", [])

            embed = discord.Embed(
                title=f"🎒 {self._target.display_name}'s Inventory",
                color=0xE67E22,
            )
            embed.set_thumbnail(url=self._target.display_avatar.url)
            if inv:
                chunks: list[str] = []
                for i, item in enumerate(inv[-25:], 1):
                    chunks.append(f"`{i}.` {item}")
                embed.description = "\n".join(chunks)
                embed.set_footer(text=f"{len(inv)} item(s) total")
            else:
                embed.description = "_Inventory is empty._"
        else:
            button.label = "🎒 View Inventory"
            button.style = discord.ButtonStyle.primary
            embed = self._original_embed

        await interaction.edit_original_response(embed=embed, view=self)


# ---------------------------------------------------------------------------
# UI — Slot result view (re-spin button, disabled after win)
# ---------------------------------------------------------------------------

_SLOT_SYMBOLS: list[str] = ["🍒", "🍊", "🍋", "🍇", "⭐", "🔔", "7️⃣"]
_SLOT_JACKPOT = "7️⃣"


def _spin_slots() -> tuple[str, str, str]:
    return (
        random.choice(_SLOT_SYMBOLS),
        random.choice(_SLOT_SYMBOLS),
        random.choice(_SLOT_SYMBOLS),
    )


def _slots_embed(
    s1: str,
    s2: str,
    s3: str,
    *,
    user: discord.User | discord.Member,
    xp_total: int | None = None,
    win: bool = False,
    jackpot: bool = False,
) -> discord.Embed:
    color = 0xF1C40F if jackpot else (0x57F287 if win else 0xED4245)
    title = "🎰 JACKPOT — 777! 🎰" if jackpot else ("🎰 Winner!" if win else "🎰 Slot Machine")
    embed = discord.Embed(title=title, color=color)
    embed.description = (
        f"╔══════════════╗\n"
        f"║  {s1}  {s2}  {s3}  ║\n"
        f"╚══════════════╝"
    )
    if jackpot and xp_total is not None:
        embed.add_field(name="🏆 Jackpot!", value=f"+200 XP awarded!\nTotal XP: **{xp_total}**", inline=False)
    elif win:
        embed.add_field(name="🎉 Match!", value="Two matching symbols — keep spinning!", inline=False)
    else:
        embed.add_field(name="😔 No match", value="Better luck next time!", inline=False)
    embed.set_footer(text=f"{user.display_name} · 1-hour cooldown")
    return embed


# ---------------------------------------------------------------------------
# Main Cog
# ---------------------------------------------------------------------------

class EconomyCog(commands.Cog, name="Economy"):
    """Server economy, RPG inventory, and fun commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # -----------------------------------------------------------------------
    # /profile
    # -----------------------------------------------------------------------

    @commands.hybrid_command(
        name="profile",
        description="View a user's economy profile card.",
    )
    @app_commands.describe(user="The user whose profile to view (defaults to you).")
    async def profile(
        self,
        ctx: commands.Context,
        user: Optional[discord.Member] = None,
    ) -> None:
        target: discord.User | discord.Member = user or ctx.author
        profile_data = db.get(target.id)

        wallet: int = profile_data.get("wallet", 0)
        bank: int = profile_data.get("bank", 0)
        xp: int = profile_data.get("xp", 0)
        bio: str = profile_data.get("bio", "No bio set.")
        inv: list[str] = profile_data.get("inventory", [])
        level: int = db.level_from_xp(xp)

        embed = discord.Embed(
            title=f"👤 {target.display_name}'s Profile",
            description=f"*{bio}*",
            color=0x5865F2,
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="💵 Wallet", value=f"`${wallet:,}`", inline=True)
        embed.add_field(name="🏦 Bank", value=f"`${bank:,}`", inline=True)
        embed.add_field(name="⭐ Level", value=f"`{level}` *(XP: {xp:,})*", inline=True)
        embed.add_field(name="🎒 Inventory", value=f"`{len(inv)} item(s)`", inline=True)
        embed.set_footer(
            text=f"Requested by {ctx.author.display_name}",
            icon_url=ctx.author.display_avatar.url,
        )

        view = ProfileView(target=target, author=ctx.author, original_embed=embed)
        await ctx.reply(embed=embed, view=view, mention_author=False)

    # -----------------------------------------------------------------------
    # /ihtxgen — slash-native IHTX pipeline with live embed feedback
    # -----------------------------------------------------------------------

    async def _preset_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        try:
            from bot.ihtx_bot import PRESET_FILTERS
            presets = list(PRESET_FILTERS.keys())
        except Exception:
            presets = ["chaos", "glitch", "shake", "rainbow", "static", "melt", "corrupt"]
        choices = [
            app_commands.Choice(name=p.title(), value=p)
            for p in presets
            if current.lower() in p.lower()
        ]
        return choices[:25]

    @commands.hybrid_command(
        name="ihtxgen",
        description="Run an IHTX FFmpeg effect on a media file or URL with live embed feedback.",
    )
    @app_commands.describe(
        effect="The IHTX preset to apply (e.g. chaos, glitch, melt).",
        url="Direct URL to a media file (alternative to attaching a file).",
        attachment="Attach a video or image directly (slash command only).",
    )
    @app_commands.autocomplete(effect=_preset_autocomplete)
    async def ihtxgen(
        self,
        ctx: commands.Context,
        effect: str = "chaos",
        url: Optional[str] = None,
        attachment: Optional[discord.Attachment] = None,
    ) -> None:
        # Import what we need from the main bot module
        try:
            from bot.ihtx_bot import (
                PRESET_FILTERS,
                SUPPORTED_EXTENSIONS,
                VIDEO_EXTENSIONS,
                MAX_FILE_SIZE,
                get_output_ext,
                run_ffmpeg,
                download_attachment,
                download_url,
                _upload_to_catbox,
                _ffprobe_video_info,
            )
        except ImportError as exc:
            await ctx.reply(f"❌ Internal error importing IHTX pipeline: `{exc}`", ephemeral=True)
            return

        effect = effect.lower().strip()
        if effect not in PRESET_FILTERS:
            preset_list = ", ".join(f"`{p}`" for p in sorted(PRESET_FILTERS.keys()))
            await ctx.reply(
                embed=discord.Embed(
                    title="❌ Unknown Preset",
                    description=f"Available presets: {preset_list}",
                    color=0xED4245,
                ),
                ephemeral=True,
            )
            return

        # Resolve media source: slash attachment > url param > message attachments > referenced message
        media_url: Optional[str] = None
        media_filename: str = "input"
        media_size: int = 0

        if attachment is not None:
            media_url = attachment.url
            media_filename = attachment.filename
            media_size = attachment.size
        elif url:
            media_url = url
            media_filename = url.split("?")[0].split("/")[-1] or "input.mp4"
            media_size = 0
        elif ctx.message and ctx.message.attachments:
            a = ctx.message.attachments[0]
            media_url = a.url
            media_filename = a.filename
            media_size = a.size
        elif ctx.message and ctx.message.reference:
            try:
                ref = await ctx.channel.fetch_message(ctx.message.reference.message_id)
                if ref.attachments:
                    a = ref.attachments[0]
                    media_url = a.url
                    media_filename = a.filename
                    media_size = a.size
            except Exception:
                pass

        if not media_url:
            await ctx.reply(
                embed=discord.Embed(
                    title="📎 No Media Provided",
                    description=(
                        "Attach a file, provide a `url:` parameter, or reply to a message with media.\n\n"
                        f"**Effect selected:** `{effect}`\n"
                        "**Usage:** `/ihtxgen effect:chaos` with an attachment\n"
                        "**Text prefix:** `t!ihtxgen chaos` (attach or reply to a file)"
                    ),
                    color=0xFEE75C,
                ),
                ephemeral=True,
            )
            return

        if media_size > MAX_FILE_SIZE:
            await ctx.reply(
                embed=discord.Embed(
                    description=f"❌ File too large (max 25 MB, got {media_size / 1024 / 1024:.1f} MB).",
                    color=0xED4245,
                ),
                ephemeral=True,
            )
            return

        suffix = Path(media_filename).suffix.lower() or ".mp4"
        if suffix not in SUPPORTED_EXTENSIONS:
            await ctx.reply(
                embed=discord.Embed(
                    description=f"❌ Unsupported file type `{suffix}`.",
                    color=0xED4245,
                ),
                ephemeral=True,
            )
            return

        is_video = suffix in VIDEO_EXTENSIONS
        out_ext = get_output_ext(suffix, is_video)
        preset_out_ext = PRESET_FILTERS.get(effect, {}).get("output_ext")
        if preset_out_ext:
            out_ext = preset_out_ext

        # Send live "processing" embed
        loading_embed = discord.Embed(
            title="⚙️ IHTX Generator",
            description=(
                f"**Effect:** `{effect}`\n"
                f"**File:** `{media_filename}`\n\n"
                "⏳ Downloading and processing your media…"
            ),
            color=0x5865F2,
        )
        loading_embed.set_thumbnail(url="https://files.catbox.moe/xli8jw.png")
        loading_embed.set_footer(text=f"Requested by {ctx.author.display_name}")

        status_msg = await ctx.reply(embed=loading_embed, mention_author=False)

        async def _update(description: str, color: int = 0x5865F2) -> None:
            e = discord.Embed(
                title="⚙️ IHTX Generator",
                description=description,
                color=color,
            )
            e.set_thumbnail(url="https://files.catbox.moe/xli8jw.png")
            e.set_footer(text=f"Requested by {ctx.author.display_name}")
            try:
                await status_msg.edit(embed=e)
            except Exception:
                pass

        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, f"input{suffix}")
            output_path = os.path.join(tmpdir, f"output{out_ext}")

            # Download
            try:
                await _update(
                    f"**Effect:** `{effect}`\n**File:** `{media_filename}`\n\n"
                    "⬇️ Downloading media…"
                )
                async with __import__("aiohttp").ClientSession() as session:
                    async with session.get(media_url) as resp:
                        if resp.status != 200:
                            await _update(
                                f"❌ Failed to download media (HTTP {resp.status}).",
                                0xED4245,
                            )
                            return
                        data = await resp.read()
                with open(input_path, "wb") as fh:
                    fh.write(data)
            except Exception as exc:
                await _update(f"❌ Download failed: `{exc}`", 0xED4245)
                return

            # FFmpeg
            await _update(
                f"**Effect:** `{effect}`\n**File:** `{media_filename}`\n\n"
                f"🔧 Running FFmpeg `{effect}` preset…"
            )
            loop = asyncio.get_event_loop()
            ok, err = await loop.run_in_executor(
                None, run_ffmpeg, input_path, output_path, effect, is_video
            )

            if not ok:
                await _update(f"❌ FFmpeg failed:\n```\n{err[-1200:]}\n```", 0xED4245)
                return

            out_size = os.path.getsize(output_path)

            # Probe for metadata
            try:
                vinfo = _ffprobe_video_info(output_path)
                w, h = int(vinfo["width"]), int(vinfo["height"])
                fr_raw = vinfo.get("r_frame_rate", "")
                try:
                    fn, fd = fr_raw.split("/")
                    fps_str = f"{float(fn) / float(fd):.2f} fps"
                except Exception:
                    fps_str = "N/A"
                gcd = math.gcd(w, h) if w and h else 1
                res_str = f"{w}×{h}" if w and h else "N/A"
                ar_str = f"{w // gcd}:{h // gcd}" if gcd else "N/A"
            except Exception:
                res_str = ar_str = fps_str = "N/A"

            size_mb = out_size / (1024 * 1024)

            if out_size > MAX_FILE_SIZE:
                await _update("⬆️ Output exceeds 25 MB — uploading to Catbox…")
                catbox_url = await _upload_to_catbox(output_path)
                if catbox_url:
                    result_embed = discord.Embed(
                        title="✅ IHTX Generator — Done!",
                        description=(
                            f"**Effect:** `{effect}`\n"
                            f"**Resolution:** {res_str} ({ar_str}) · {fps_str}\n"
                            f"**Size:** {size_mb:.2f} MB (uploaded to Catbox)\n\n"
                            f"🔗 [Download from Catbox]({catbox_url})\n`{catbox_url}`"
                        ),
                        color=0x57F287,
                    )
                    result_embed.set_thumbnail(url="https://files.catbox.moe/xli8jw.png")
                    result_embed.set_footer(text=f"Requested by {ctx.author.display_name}")
                    await status_msg.edit(embed=result_embed)
                else:
                    await _update(
                        "❌ Output too large for Discord (>25 MB) and Catbox upload failed.",
                        0xED4245,
                    )
                return

            out_filename = f"ihtx_{effect}_{Path(media_filename).stem}{out_ext}"
            result_embed = discord.Embed(
                title="✅ IHTX Generator — Done!",
                description=(
                    f"**Effect applied:** `{effect}`\n"
                    f"**Resolution:** {res_str} ({ar_str}) · {fps_str}\n"
                    f"**Output size:** {size_mb:.2f} MB"
                ),
                color=0x57F287,
            )
            result_embed.set_thumbnail(url="https://files.catbox.moe/xli8jw.png")
            result_embed.set_footer(text=f"Requested by {ctx.author.display_name}")

            try:
                await status_msg.edit(
                    embed=result_embed,
                    attachments=[discord.File(output_path, filename=out_filename)],
                )
            except discord.HTTPException:
                await status_msg.edit(embed=result_embed)
                await ctx.send(file=discord.File(output_path, filename=out_filename))

    # -----------------------------------------------------------------------
    # /slot — Slot machine with 1-hour cooldown and 777 jackpot
    # -----------------------------------------------------------------------

    @commands.hybrid_command(
        name="jackpot",
        description="Spin the slot machine! Hit 777 to win 200 XP. (1-hour cooldown)",
    )
    @commands.cooldown(1, 3600, commands.BucketType.user)
    async def jackpot(self, ctx: commands.Context) -> None:
        s1, s2, s3 = _spin_slots()

        is_jackpot = s1 == s2 == s3 == _SLOT_JACKPOT
        two_match = (s1 == s2 or s2 == s3 or s1 == s3) and not is_jackpot
        win = is_jackpot or two_match

        xp_total: Optional[int] = None
        if is_jackpot:
            xp_total = db.add_xp(ctx.author.id, 200)

        embed = _slots_embed(
            s1, s2, s3,
            user=ctx.author,
            xp_total=xp_total,
            win=win,
            jackpot=is_jackpot,
        )
        await ctx.reply(embed=embed, mention_author=False)

    @jackpot.error
    async def jackpot_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.CommandOnCooldown):
            remaining = error.retry_after
            minutes = int(remaining // 60)
            seconds = int(remaining % 60)

            time_parts: list[str] = []
            if minutes:
                time_parts.append(f"**{minutes}m**")
            if seconds or not minutes:
                time_parts.append(f"**{seconds}s**")
            time_str = " ".join(time_parts)

            embed = discord.Embed(
                title="⏳ Slot Machine on Cooldown",
                description=(
                    f"You already spun the slots recently!\n\n"
                    f"Come back in {time_str}."
                ),
                color=0xFEE75C,
            )
            embed.set_footer(text="Cooldown: 1 hour per spin")

            if ctx.interaction:
                await ctx.interaction.response.send_message(embed=embed, ephemeral=True)
            else:
                await ctx.reply(embed=embed, mention_author=False, delete_after=20)
        else:
            raise error


# ---------------------------------------------------------------------------
# Cog setup
# ---------------------------------------------------------------------------

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EconomyCog(bot))
