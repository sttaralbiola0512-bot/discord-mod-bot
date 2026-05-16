"""
Discord Moderation Bot — Powered by Groq AI
Groq ang mag-aanalyze ng bawat message at magde-decide kung i-moderate.

ENV VARIABLES (i-set sa Render):
  DISCORD_TOKEN   — Discord bot token
  GROQ_API_KEY    — Groq API key (console.groq.com)
"""

import os
import re
import time
import asyncio
from collections import defaultdict

import discord
from discord.ext import commands
from groq import Groq

from keep_alive import keep_alive

# ─────────────────────────────────────────
#  ENV VARIABLES — hindi na hardcoded!
# ─────────────────────────────────────────
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GROQ_API_KEY  = os.environ["GROQ_API_KEY"]

# ─────────────────────────────────────────
#  SETTINGS
# ─────────────────────────────────────────
SPAM_MESSAGE_LIMIT   = 5    # max messages bago ma-mute
SPAM_TIME_WINDOW     = 5    # seconds
ANTI_RAID_JOIN_LIMIT = 10   # max joins
ANTI_RAID_TIME_WINDOW = 10  # seconds
MUTED_ROLE_NAME      = "Muted"

# Allowed domains para sa anti-link
ALLOWED_LINKS = ["discord.com", "discord.gg", "youtube.com", "youtu.be"]

# ─────────────────────────────────────────
#  CLIENTS
# ─────────────────────────────────────────
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
groq_client = Groq(api_key=GROQ_API_KEY)

# ─────────────────────────────────────────
#  IN-MEMORY STORAGE
# ─────────────────────────────────────────
warn_data    = defaultdict(list)   # {user_id: [reasons]}
spam_tracker = defaultdict(list)   # {user_id: [timestamps]}
raid_tracker = []                  # [join timestamps]


# ─────────────────────────────────────────
#  GROQ AI — MODERATION ANALYZER
# ─────────────────────────────────────────
async def groq_analyze(message_content: str) -> dict:
    """
    Tanungin ang Groq kung dapat i-moderate ang message.
    Returns: {
        "should_moderate": bool,
        "action": "delete" | "warn" | "mute" | "kick" | "ban" | "none",
        "reason": str,
        "severity": "low" | "medium" | "high"
    }
    """
    prompt = f"""You are a Discord chat moderator AI. Analyze this message and decide if it needs moderation.

Message: "{message_content}"

Rules to check:
1. Hate speech, racism, discrimination
2. Severe profanity or insults directed at users
3. Threats or calls to violence
4. NSFW or explicit sexual content
5. Phishing links or scam content
6. Doxxing (sharing personal info)
7. Spam-like content (gibberish, all caps shouting, excessive emojis)

Respond ONLY in this exact JSON format, no extra text:
{{
  "should_moderate": true or false,
  "action": "delete" or "warn" or "mute" or "kick" or "ban" or "none",
  "reason": "short reason in Filipino or English",
  "severity": "low" or "medium" or "high"
}}

Action guide:
- none: normal message, do nothing
- delete: remove the message only (mild offense)
- warn: delete + issue a warning (moderate offense)
- mute: delete + mute user (repeated or serious offense)
- kick: delete + kick user (very serious)
- ban: delete + ban user (extreme: threats, doxxing, severe hate speech)"""

    try:
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, lambda: groq_client.chat.completions.create(
            model="llama3-70b-8192",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=150,
        ))

        import json
        text = response.choices[0].message.content.strip()
        # Strip markdown code blocks kung meron
        text = re.sub(r"```json|```", "", text).strip()
        return json.loads(text)

    except Exception as e:
        print(f"[Groq Error] {e}")
        # Default: huwag mag-moderate pag may error
        return {"should_moderate": False, "action": "none", "reason": "", "severity": "low"}


# ─────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────
async def get_or_create_muted_role(guild: discord.Guild) -> discord.Role:
    role = discord.utils.get(guild.roles, name=MUTED_ROLE_NAME)
    if not role:
        role = await guild.create_role(name=MUTED_ROLE_NAME, reason="Auto-created by Mod Bot")
        for channel in guild.channels:
            await channel.set_permissions(role, send_messages=False, speak=False)
    return role


def contains_link(text: str) -> bool:
    return bool(re.search(r"(https?://|www\.)[^\s]+", text))


def is_allowed_link(text: str) -> bool:
    return any(domain in text.lower() for domain in ALLOWED_LINKS)


# ─────────────────────────────────────────
#  EXECUTE MOD ACTION
# ─────────────────────────────────────────
async def execute_mod_action(message: discord.Message, result: dict):
    """I-execute ang action na sinesuggest ng Groq."""
    member = message.author
    guild  = message.guild
    action = result.get("action", "none")
    reason = result.get("reason", "AI Moderation")

    if action == "none":
        return

    # Always delete the message
    try:
        await message.delete()
    except discord.NotFound:
        pass

    severity_emoji = {"low": "🟡", "medium": "🟠", "high": "🔴"}.get(result.get("severity", "low"), "🟡")

    # Warn
    if action in ("warn", "mute", "kick", "ban"):
        warn_data[member.id].append(reason)
        warn_count = len(warn_data[member.id])

        await message.channel.send(
            f"{severity_emoji} {member.mention}, na-flag ang iyong message ng AI Moderator.\n"
            f"**Dahilan:** {reason}\n"
            f"**Warnings:** {warn_count}",
            delete_after=10
        )

    # Mute
    if action == "mute":
        muted_role = await get_or_create_muted_role(guild)
        await member.add_roles(muted_role, reason=f"AI Mod: {reason}")
        await message.channel.send(
            f"🔇 {member.mention} ay na-mute ng 10 minuto.", delete_after=10
        )
        await asyncio.sleep(600)
        await member.remove_roles(muted_role, reason="AI Mod mute expired")

    # Kick
    elif action == "kick":
        try:
            await member.kick(reason=f"AI Mod: {reason}")
            await message.channel.send(f"👢 {member} ay na-kick.", delete_after=10)
        except discord.Forbidden:
            pass

    # Ban
    elif action == "ban":
        try:
            await member.ban(reason=f"AI Mod: {reason}")
            await message.channel.send(f"🔨 {member} ay na-ban.", delete_after=10)
        except discord.Forbidden:
            pass


# ─────────────────────────────────────────
#  EVENTS
# ─────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"✅ Bot online: {bot.user} (ID: {bot.user.id})")
    print(f"🤖 Groq AI moderation: ACTIVE")
    print("─" * 40)


@bot.event
async def on_member_join(member: discord.Member):
    """Anti-raid protection."""
    now = time.time()
    raid_tracker.append(now)
    recent = [t for t in raid_tracker if now - t < ANTI_RAID_TIME_WINDOW]
    raid_tracker.clear()
    raid_tracker.extend(recent)

    if len(recent) >= ANTI_RAID_JOIN_LIMIT:
        try:
            await member.kick(reason="⚠️ Anti-Raid: Mabilis na pag-join")
        except discord.Forbidden:
            pass


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    member  = message.author
    content = message.content

    # ── ANTI-LINK (mabilis, walang AI needed) ──
    if contains_link(content) and not is_allowed_link(content):
        if not member.guild_permissions.manage_messages:
            await message.delete()
            await message.channel.send(
                f"🔗 {member.mention}, bawal mag-post ng links dito!", delete_after=5
            )
            return

    # ── SPAM DETECTION (mabilis din) ──
    now = time.time()
    spam_tracker[member.id].append(now)
    recent_msgs = [t for t in spam_tracker[member.id] if now - t < SPAM_TIME_WINDOW]
    spam_tracker[member.id] = recent_msgs

    if len(recent_msgs) >= SPAM_MESSAGE_LIMIT:
        spam_tracker[member.id].clear()
        muted_role = await get_or_create_muted_role(message.guild)
        await member.add_roles(muted_role, reason="Auto-mute: Spam")
        await message.channel.send(
            f"🔇 {member.mention} ay na-mute dahil sa spam. (5 minuto)", delete_after=10
        )
        await asyncio.sleep(300)
        await member.remove_roles(muted_role, reason="Spam mute expired")
        return

    # ── GROQ AI MODERATION ──
    # Skip kung maikli lang o walang laman ang message
    if len(content.strip()) > 3:
        result = await groq_analyze(content)
        if result.get("should_moderate"):
            await execute_mod_action(message, result)
            return  # Huwag na i-process bilang command

    await bot.process_commands(message)


# ─────────────────────────────────────────
#  MOD COMMANDS (para sa manual moderation)
# ─────────────────────────────────────────
def is_mod():
    async def predicate(ctx):
        return ctx.author.guild_permissions.manage_messages
    return commands.check(predicate)


@bot.command()
@is_mod()
async def warn(ctx, member: discord.Member, *, reason: str = "Walang dahilan"):
    """!warn @user [reason]"""
    warn_data[member.id].append(reason)
    warn_count = len(warn_data[member.id])
    await ctx.send(f"⚠️ {member.mention} ay na-warn. Total: **{warn_count}**\nDahilan: {reason}")

    if warn_count == 3:
        muted_role = await get_or_create_muted_role(ctx.guild)
        await member.add_roles(muted_role, reason="3 warnings")
        await ctx.send(f"🔇 {member.mention} ay na-mute dahil sa 3 warnings.")
    elif warn_count >= 5:
        await member.ban(reason="5+ warnings")
        await ctx.send(f"🔨 {member.mention} ay na-ban dahil sa {warn_count} warnings.")


@bot.command()
@is_mod()
async def warnings(ctx, member: discord.Member):
    """!warnings @user"""
    warns = warn_data.get(member.id, [])
    if not warns:
        return await ctx.send(f"✅ {member.mention} walang warnings.")
    warn_list = "\n".join(f"{i+1}. {w}" for i, w in enumerate(warns))
    await ctx.send(f"⚠️ **Warnings ni {member}** ({len(warns)}):\n{warn_list}")


@bot.command()
@is_mod()
async def clearwarns(ctx, member: discord.Member):
    """!clearwarns @user"""
    warn_data[member.id] = []
    await ctx.send(f"✅ Na-clear ang warnings ni {member.mention}.")


@bot.command()
@is_mod()
async def mute(ctx, member: discord.Member, duration: int = 10, *, reason: str = "Walang dahilan"):
    """!mute @user [minuto] [reason]"""
    muted_role = await get_or_create_muted_role(ctx.guild)
    await member.add_roles(muted_role, reason=reason)
    await ctx.send(f"🔇 {member.mention} na-mute ng {duration} minuto.")
    await asyncio.sleep(duration * 60)
    await member.remove_roles(muted_role, reason="Mute expired")


@bot.command()
@is_mod()
async def unmute(ctx, member: discord.Member):
    """!unmute @user"""
    muted_role = await get_or_create_muted_role(ctx.guild)
    await member.remove_roles(muted_role)
    await ctx.send(f"🔊 {member.mention} na-unmute na.")


@bot.command()
@is_mod()
async def kick(ctx, member: discord.Member, *, reason: str = "Walang dahilan"):
    """!kick @user [reason]"""
    await member.kick(reason=reason)
    await ctx.send(f"👢 {member} na-kick. Dahilan: {reason}")


@bot.command()
@is_mod()
async def ban(ctx, member: discord.Member, *, reason: str = "Walang dahilan"):
    """!ban @user [reason]"""
    await member.ban(reason=reason)
    await ctx.send(f"🔨 {member} na-ban. Dahilan: {reason}")


@bot.command()
@is_mod()
async def purge(ctx, amount: int = 10):
    """!purge [amount]"""
    deleted = await ctx.channel.purge(limit=amount + 1)
    await ctx.send(f"🗑️ Na-delete ang {len(deleted) - 1} messages.", delete_after=5)


# ─────────────────────────────────────────
#  ERROR HANDLER
# ─────────────────────────────────────────
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Wala kang permission.", delete_after=5)
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("❌ Hindi mahanap ang user.", delete_after=5)
    elif isinstance(error, commands.CheckFailure):
        await ctx.send("❌ Mods lang ang pwedeng gumamit nito.", delete_after=5)
    else:
        print(f"[Error] {error}")


# ─────────────────────────────────────────
#  START
# ─────────────────────────────────────────
keep_alive()        # Para hindi mag-sleep sa Render
bot.run(DISCORD_TOKEN)
