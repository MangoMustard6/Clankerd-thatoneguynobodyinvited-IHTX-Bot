"""
TagCog — discord.py Cog providing the full tag system.

All commands live under the `t!tag` group (alias: `t!tags`).
Direct invocation: `t!tag <name> [args...]`
"""

import math
import textwrap
from datetime import datetime, timezone

import discord
from discord.ext import commands

from .storage import TagStorage
from .parser import parse, build_context
from . import engines

MAX_TAG_NAME = 50
MAX_TAG_CONTENT = 4000
TAGS_PER_PAGE = 15

_RESERVED = frozenset({
    "create", "add", "new", "edit", "update", "delete", "remove", "del",
    "info", "list", "ls", "all", "search", "find", "alias", "unalias",
    "raw", "transfer", "stats", "help", "rename", "perms",
})

storage = TagStorage()


def _is_bot_owner(bot: commands.Bot, user_id: int) -> bool:
    try:
        from bot.ihtx_bot import owner_ids
        return user_id in owner_ids
    except Exception:
        return False


def _can_use(tag: dict, member: discord.Member) -> bool:
    if member.id in tag.get("denied_users", []):
        return False
    allowed = tag.get("allowed_roles", [])
    if not allowed:
        return True
    return bool({r.id for r in member.roles} & set(allowed))


def _valid_name(name: str) -> bool:
    return bool(name) and name.replace("-", "").replace("_", "").isalnum()


class TagCog(commands.Cog, name="Tags"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _owner(self, ctx: commands.Context) -> bool:
        return _is_bot_owner(self.bot, ctx.author.id)

    # ------------------------------------------------------------------
    # Group root — invoke a tag OR dispatch to subcommand
    # ------------------------------------------------------------------

    @commands.group(name="tag", aliases=["tags"], invoke_without_command=True)
    async def tag(self, ctx: commands.Context, name: str = None, *, args: str = ""):
        """Invoke a tag by name, or use a subcommand."""
        if name is None:
            return await ctx.reply(
                "Usage: `t!tag <name> [args]`\n"
                "Run `t!tag help` to see all subcommands."
            )
        if ctx.guild is None:
            return await ctx.reply("Tags can only be used in servers.")

        tag = await storage.get(ctx.guild.id, name.lower())
        if tag is None:
            return await ctx.reply(f"❌ Tag `{name}` not found.")

        if isinstance(ctx.author, discord.Member):
            if not _can_use(tag, ctx.author) and not self._owner(ctx):
                return await ctx.reply("❌ You don't have permission to use this tag.")

        tag_ctx = build_context(ctx, args)
        text, engine_blocks = parse(tag["content"], tag_ctx)

        files = []
        errors = []
        extra_texts = []

        for block in engine_blocks:
            engine = engines.get(block["engine"])
            if engine is None:
                errors.append(f"Unknown engine: {block['engine']}")
                continue
            result = await engine.execute(block["content"], ctx, tag_ctx)
            if result.error:
                errors.append(result.error)
            else:
                files.extend(result.files)
                if result.text:
                    extra_texts.append(result.text)

        parts = [text] + extra_texts
        final = "\n".join(p for p in parts if p).strip()
        if errors:
            final = (final + "\n" + "\n".join(f"⚠️ {e}" for e in errors)).strip()

        if not final and not files:
            return await ctx.reply("*(tag produced no output)*")

        if len(final) > 2000:
            final = final[:1997] + "..."

        await ctx.reply(
            final or None,
            files=files if files else discord.utils.MISSING,
        )
        await storage.increment_uses(ctx.guild.id, name.lower())

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    @tag.command(name="create", aliases=["add", "new"])
    async def tag_create(self, ctx: commands.Context, name: str, *, content: str):
        """Create a new tag. The tag name must be alphanumeric (hyphens/underscores ok)."""
        if ctx.guild is None:
            return await ctx.reply("Tags can only be created in servers.")

        name = name.lower().strip()
        if len(name) > MAX_TAG_NAME:
            return await ctx.reply(f"❌ Name too long (max {MAX_TAG_NAME} chars).")
        if not _valid_name(name):
            return await ctx.reply(
                "❌ Tag name may only contain letters, numbers, hyphens, and underscores."
            )
        if name in _RESERVED:
            return await ctx.reply(f"❌ `{name}` is a reserved subcommand name.")
        if len(content) > MAX_TAG_CONTENT:
            return await ctx.reply(f"❌ Content too long (max {MAX_TAG_CONTENT} chars).")

        ok = await storage.create(ctx.guild.id, name, content, ctx.author.id)
        if not ok:
            return await ctx.reply(f"❌ Tag `{name}` already exists.")
        await ctx.reply(f"✅ Tag `{name}` created.")

    # ------------------------------------------------------------------
    # Edit
    # ------------------------------------------------------------------

    @tag.command(name="edit", aliases=["update"])
    async def tag_edit(self, ctx: commands.Context, name: str, *, content: str):
        """Edit a tag you own (or any tag if you're a bot owner)."""
        if ctx.guild is None:
            return await ctx.reply("Tags can only be edited in servers.")
        if len(content) > MAX_TAG_CONTENT:
            return await ctx.reply(f"❌ Content too long (max {MAX_TAG_CONTENT} chars).")

        result = await storage.edit(
            ctx.guild.id, name.lower(), content, ctx.author.id, self._owner(ctx)
        )
        msgs = {
            "ok": f"✅ Tag `{name}` updated.",
            "not_found": f"❌ Tag `{name}` not found.",
            "not_owner": "❌ You don't own this tag.",
        }
        await ctx.reply(msgs.get(result, f"❌ {result}"))

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    @tag.command(name="delete", aliases=["remove", "del"])
    async def tag_delete(self, ctx: commands.Context, name: str):
        """Delete a tag you own (or any tag if you're a bot owner)."""
        if ctx.guild is None:
            return await ctx.reply("Tags can only be deleted in servers.")

        result = await storage.delete(
            ctx.guild.id, name.lower(), ctx.author.id, self._owner(ctx)
        )
        msgs = {
            "ok": f"✅ Tag `{name}` deleted.",
            "not_found": f"❌ Tag `{name}` not found.",
            "not_owner": "❌ You don't own this tag.",
        }
        await ctx.reply(msgs.get(result, f"❌ {result}"))

    # ------------------------------------------------------------------
    # Info
    # ------------------------------------------------------------------

    @tag.command(name="info")
    async def tag_info(self, ctx: commands.Context, name: str):
        """Show metadata about a tag."""
        if ctx.guild is None:
            return await ctx.reply("Tags can only be used in servers.")

        tag = await storage.get(ctx.guild.id, name.lower())
        if tag is None:
            return await ctx.reply(f"❌ Tag `{name}` not found.")

        owner = self.bot.get_user(tag["owner_id"])
        owner_str = owner.mention if owner else f"<@{tag['owner_id']}>"

        created_str = "unknown"
        try:
            dt = datetime.fromisoformat(tag.get("created_at", ""))
            created_str = f"<t:{int(dt.timestamp())}:R>"
        except Exception:
            pass

        embed = discord.Embed(title=f"Tag: {tag['name']}", color=0x5865F2)
        embed.add_field(name="Owner", value=owner_str, inline=True)
        embed.add_field(name="Uses", value=str(tag.get("uses", 0)), inline=True)
        embed.add_field(name="Created", value=created_str, inline=True)

        aliases = tag.get("aliases", [])
        if aliases:
            embed.add_field(
                name="Aliases",
                value=", ".join(f"`{a}`" for a in aliases),
                inline=False,
            )

        allowed_roles = tag.get("allowed_roles", [])
        denied_users = tag.get("denied_users", [])
        if allowed_roles:
            embed.add_field(
                name="Allowed roles",
                value=" ".join(f"<@&{r}>" for r in allowed_roles),
                inline=False,
            )
        if denied_users:
            embed.add_field(
                name="Denied users",
                value=" ".join(f"<@{u}>" for u in denied_users),
                inline=False,
            )

        preview = tag["content"]
        if len(preview) > 120:
            preview = preview[:120] + "…"
        embed.add_field(
            name="Content preview",
            value=f"```\n{preview}\n```",
            inline=False,
        )
        await ctx.reply(embed=embed)

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------

    @tag.command(name="list", aliases=["ls", "all"])
    async def tag_list(self, ctx: commands.Context, page: int = 1):
        """List all tags in this server. Paginated."""
        if ctx.guild is None:
            return await ctx.reply("Tags can only be used in servers.")

        tags = await storage.list_tags(ctx.guild.id)
        if not tags:
            return await ctx.reply(
                "No tags yet. Create one with `t!tag create <name> <content>`."
            )

        total = math.ceil(len(tags) / TAGS_PER_PAGE)
        page = max(1, min(page, total))
        chunk = tags[(page - 1) * TAGS_PER_PAGE : page * TAGS_PER_PAGE]

        embed = discord.Embed(
            title=f"Tags — {ctx.guild.name}",
            description=", ".join(f"`{t['name']}`" for t in chunk),
            color=0x5865F2,
        )
        embed.set_footer(text=f"Page {page}/{total} · {len(tags)} total tags")
        await ctx.reply(embed=embed)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    @tag.command(name="search", aliases=["find"])
    async def tag_search(self, ctx: commands.Context, *, query: str):
        """Search tags by name or content."""
        if ctx.guild is None:
            return await ctx.reply("Tags can only be used in servers.")

        results = await storage.search_tags(ctx.guild.id, query)
        if not results:
            return await ctx.reply(f"No tags match `{query}`.")

        results = results[:25]
        embed = discord.Embed(
            title=f"Search: '{query}'",
            description=", ".join(f"`{t['name']}`" for t in results),
            color=0x5865F2,
        )
        embed.set_footer(text=f"{len(results)} result(s)")
        await ctx.reply(embed=embed)

    # ------------------------------------------------------------------
    # Raw
    # ------------------------------------------------------------------

    @tag.command(name="raw")
    async def tag_raw(self, ctx: commands.Context, name: str):
        """Show the unparsed source content of a tag."""
        if ctx.guild is None:
            return await ctx.reply("Tags can only be used in servers.")

        tag = await storage.get(ctx.guild.id, name.lower())
        if tag is None:
            return await ctx.reply(f"❌ Tag `{name}` not found.")

        content = tag["content"]
        if len(content) > 1900:
            content = content[:1900] + "…"
        await ctx.reply(f"```\n{content}\n```")

    # ------------------------------------------------------------------
    # Alias / Unalias
    # ------------------------------------------------------------------

    @tag.command(name="alias")
    async def tag_alias(self, ctx: commands.Context, name: str, alias: str):
        """Add an alias to a tag you own."""
        if ctx.guild is None:
            return await ctx.reply("Tags can only be used in servers.")

        alias = alias.lower().strip()
        if not _valid_name(alias):
            return await ctx.reply(
                "❌ Alias may only contain letters, numbers, hyphens, and underscores."
            )

        result = await storage.add_alias(
            ctx.guild.id, name.lower(), alias, ctx.author.id, self._owner(ctx)
        )
        msgs = {
            "ok": f"✅ Alias `{alias}` → `{name}` added.",
            "not_found": f"❌ Tag `{name}` not found.",
            "not_owner": "❌ You don't own this tag.",
            "conflict": f"❌ `{alias}` is already a tag or alias.",
        }
        await ctx.reply(msgs.get(result, f"❌ {result}"))

    @tag.command(name="unalias")
    async def tag_unalias(self, ctx: commands.Context, alias: str):
        """Remove an alias you own."""
        if ctx.guild is None:
            return await ctx.reply("Tags can only be used in servers.")

        result = await storage.remove_alias(
            ctx.guild.id, alias.lower(), ctx.author.id, self._owner(ctx)
        )
        msgs = {
            "ok": f"✅ Alias `{alias}` removed.",
            "not_found": f"❌ Alias `{alias}` not found.",
            "not_owner": "❌ You don't own the tag this alias belongs to.",
        }
        await ctx.reply(msgs.get(result, f"❌ {result}"))

    # ------------------------------------------------------------------
    # Transfer
    # ------------------------------------------------------------------

    @tag.command(name="transfer")
    async def tag_transfer(
        self, ctx: commands.Context, name: str, new_owner: discord.Member
    ):
        """Transfer ownership of a tag to another server member."""
        if ctx.guild is None:
            return await ctx.reply("Tags can only be used in servers.")

        result = await storage.transfer(
            ctx.guild.id, name.lower(), new_owner.id, ctx.author.id, self._owner(ctx)
        )
        msgs = {
            "ok": f"✅ Tag `{name}` transferred to {new_owner.mention}.",
            "not_found": f"❌ Tag `{name}` not found.",
            "not_owner": "❌ You don't own this tag.",
        }
        await ctx.reply(msgs.get(result, f"❌ {result}"))

    # ------------------------------------------------------------------
    # Rename
    # ------------------------------------------------------------------

    @tag.command(name="rename")
    async def tag_rename(self, ctx: commands.Context, old_name: str, new_name: str):
        """Rename a tag you own."""
        if ctx.guild is None:
            return await ctx.reply("Tags can only be used in servers.")

        new_name = new_name.lower().strip()
        if not _valid_name(new_name):
            return await ctx.reply(
                "❌ Tag name may only contain letters, numbers, hyphens, and underscores."
            )
        if len(new_name) > MAX_TAG_NAME:
            return await ctx.reply(f"❌ Name too long (max {MAX_TAG_NAME} chars).")
        if new_name in _RESERVED:
            return await ctx.reply(f"❌ `{new_name}` is a reserved subcommand name.")

        result = await storage.rename(
            ctx.guild.id, old_name.lower(), new_name, ctx.author.id, self._owner(ctx)
        )
        msgs = {
            "ok": f"✅ Tag `{old_name}` renamed to `{new_name}`.",
            "not_found": f"❌ Tag `{old_name}` not found.",
            "not_owner": "❌ You don't own this tag.",
            "conflict": f"❌ `{new_name}` already exists.",
        }
        await ctx.reply(msgs.get(result, f"❌ {result}"))

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    @tag.command(name="stats")
    async def tag_stats(self, ctx: commands.Context):
        """Show tag statistics for this server."""
        if ctx.guild is None:
            return await ctx.reply("Tags can only be used in servers.")

        s = await storage.stats(ctx.guild.id)
        embed = discord.Embed(title=f"Tag Stats — {ctx.guild.name}", color=0x5865F2)
        embed.add_field(name="Tags", value=str(s["total_tags"]), inline=True)
        embed.add_field(name="Aliases", value=str(s["total_aliases"]), inline=True)
        embed.add_field(name="Total uses", value=str(s["total_uses"]), inline=True)

        if s["top_tags"]:
            top = "\n".join(
                f"`{t['name']}` — {t.get('uses', 0)} uses" for t in s["top_tags"]
            )
            embed.add_field(name="Top tags", value=top, inline=False)

        await ctx.reply(embed=embed)

    # ------------------------------------------------------------------
    # Perms
    # ------------------------------------------------------------------

    @tag.command(name="perms")
    async def tag_perms(self, ctx: commands.Context, name: str, *, options: str = ""):
        """
        View or modify tag permissions.

        View:      `t!tag perms <name>`
        Allow roles: `t!tag perms <name> allow @Role1 @Role2`
        Open to all: `t!tag perms <name> everyone`
        Deny user:  `t!tag perms <name> deny @User`
        Un-deny:    `t!tag perms <name> undeny @User`
        """
        if ctx.guild is None:
            return await ctx.reply("Tags can only be used in servers.")

        tag = await storage.get(ctx.guild.id, name.lower())
        if tag is None:
            return await ctx.reply(f"❌ Tag `{name}` not found.")

        if not options:
            allowed = tag.get("allowed_roles", [])
            denied = tag.get("denied_users", [])
            embed = discord.Embed(title=f"Permissions: {tag['name']}", color=0x5865F2)
            embed.add_field(
                name="Allowed roles",
                value=" ".join(f"<@&{r}>" for r in allowed) or "Everyone",
                inline=False,
            )
            if denied:
                embed.add_field(
                    name="Denied users",
                    value=" ".join(f"<@{u}>" for u in denied),
                    inline=False,
                )
            return await ctx.reply(embed=embed)

        if not self._owner(ctx) and tag["owner_id"] != ctx.author.id:
            return await ctx.reply("❌ You don't own this tag.")

        parts = options.strip().split(None, 1)
        mode = parts[0].lower()

        allowed_roles = list(tag.get("allowed_roles", []))
        denied_users = list(tag.get("denied_users", []))

        if mode == "everyone":
            allowed_roles = []
        elif mode == "allow":
            role_ids = [r.id for r in ctx.message.role_mentions]
            if not role_ids:
                return await ctx.reply("❌ Mention at least one @Role.")
            allowed_roles = role_ids
        elif mode == "deny":
            user_ids = [u.id for u in ctx.message.mentions]
            if not user_ids:
                return await ctx.reply("❌ Mention at least one @User.")
            denied_users = list(set(denied_users) | set(user_ids))
        elif mode == "undeny":
            user_ids = {u.id for u in ctx.message.mentions}
            denied_users = [u for u in denied_users if u not in user_ids]
        else:
            return await ctx.reply(
                "❌ Unknown mode. Use `allow @Role`, `everyone`, `deny @User`, or `undeny @User`."
            )

        result = await storage.set_perms(
            ctx.guild.id, name.lower(),
            allowed_roles, denied_users,
            ctx.author.id, self._owner(ctx),
        )
        if result == "ok":
            await ctx.reply(f"✅ Permissions updated for tag `{name}`.")
        else:
            await ctx.reply(f"❌ {result}")

    # ------------------------------------------------------------------
    # Help
    # ------------------------------------------------------------------

    @tag.command(name="help")
    async def tag_help(self, ctx: commands.Context):
        """Show the full tag system reference."""
        embed = discord.Embed(
            title="Tag System Reference",
            description=(
                "Create and invoke reusable, scriptable tags.\n"
                "All commands start with `t!tag` (alias: `t!tags`)."
            ),
            color=0x5865F2,
        )
        embed.add_field(
            name="Commands",
            value=textwrap.dedent("""\
                `t!tag <name> [args]` — invoke a tag
                `t!tag create <name> <content>` — create
                `t!tag edit <name> <content>` — edit (owner)
                `t!tag delete <name>` — delete (owner)
                `t!tag raw <name>` — see raw source
                `t!tag info <name>` — metadata
                `t!tag list [page]` — list all tags
                `t!tag search <query>` — search by name/content
                `t!tag stats` — server statistics
                `t!tag alias <name> <alias>` — add alias
                `t!tag unalias <alias>` — remove alias
                `t!tag transfer <name> @user` — transfer ownership
                `t!tag rename <old> <new>` — rename
                `t!tag perms <name> [options]` — manage permissions\
            """),
            inline=False,
        )
        embed.add_field(
            name="Variables",
            value=textwrap.dedent("""\
                `{args}` · `{args.1}` `{args.2}` … — invocation arguments
                `{user}` — your display name
                `{mention}` — your @mention
                `{id}` — your user ID
                `{avatar}` — your avatar URL
                `{server}` — server name
                `{channel}` — channel name\
            """),
            inline=False,
        )
        embed.add_field(
            name="Inline blocks",
            value=textwrap.dedent("""\
                `{random:a|b|c}` — pick random option
                `{upper:text}` · `{lower:text}` — case conversion
                `{repeat:N|text}` — repeat text N times
                `{len:text}` — length of text\
            """),
            inline=False,
        )
        embed.add_field(
            name="{attach:URL}",
            value="Download and attach a file from an http/https URL.",
            inline=False,
        )
        embed.add_field(
            name="{iscript:...} — image processing",
            value=textwrap.dedent("""\
                `load_attachment` / `load URL`
                `blur N` · `sharpen N` · `grayscale` · `negate` · `sepia`
                `pixelate N` · `brightness N` · `contrast N`
                `rotate N` · `resize WxH` · `thumbnail WxH`
                `flip` · `flop` · `output jpg|png|gif|webp`\
            """),
            inline=False,
        )
        embed.add_field(
            name="{mediascript:...} — FFmpeg media processing",
            value=textwrap.dedent("""\
                `load_attachment` / `load URL`
                `scale WxH` · `fps N` · `trim START END`
                `speed FACTOR` · `volume LEVEL` · `strip_audio`
                `reverse` · `grayscale` · `rotate N` · `loop N`
                `fade_in N` · `fade_out N` · `gif`
                `output mp4|gif|mp3|webm|wav`\
            """),
            inline=False,
        )
        embed.add_field(
            name="{py:...} — sandboxed Python",
            value=(
                "Run Python code. `args`, `user`, `server`, `channel`, `mention` "
                "are injected. `print()` output becomes the block's text. "
                "No imports, no file I/O, 5 s timeout."
            ),
            inline=False,
        )
        await ctx.reply(embed=embed)
