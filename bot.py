"""
Discord Moderation Bot — Powered by Groq AI
Groq analyzes every message and decides if it needs moderation.

ENV VARIABLES (set in Render):
  DISCORD_TOKEN   — Discord bot token
  GROQ_API_KEY    — Groq API key (console.groq.com)
"""

import os
import re
import time
import json
import asyncio
from collections import defaultdict

import discord
from discord.ext import commands
from groq import Groq

from keep_alive import keep_alive

# ─────────────────────────────────────────
#  ENV VARIABLES
# ─────────────────────────────────────────
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
GROQ_API_KEY  = os.environ["GROQ_API_KEY"]

# ─────────────────────────────────────────
#  SETTINGS
# ─────────────────────────────────────────
SPAM_MESSAGE_LIMIT    = 5    # max messages before mute
SPAM_TIME_WINDOW      = 5    # seconds
ANTI_RAID_JOIN_LIMIT  = 10   # max joins
ANTI_RAID_TIME_WINDOW = 10   # seconds
MUTED_ROLE_NAME       = "Muted"

# Allowed domains for anti-link
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
  "reason": "short reason in English",
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
        text = response.choices[0].message.content.strip()
        text = re.sub(r"```json|```", "", text).strip()
        return json.loads(text)

    except Exception as e:
        print(f"[Groq Error] {e}")
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
    member = message.author
    guild  = message.guild
    action = result.get("action", "none")
    reason = result.get("reason", "AI Moderation")

    if action == "none":
        return

    try:
        await message.delete()
    except discord.NotFound:
        pass

    severity_emoji = {"low": "🟡", "medium": "🟠", "high": "🔴"}.get(result.get("severity", "low"), "🟡")

    if action in ("warn", "mute", "kick", "ban"):
        warn_data[member.id].append(reason)
        warn_count = len(warn_data[member.id])
        await message.channel.send(
            f"{severity_emoji} {member.mention}, your message was flagged by the AI Moderator.\n"
            f"**Reason:** {reason}\n"
            f"**Warnings:** {warn_count}",
            delete_after=10
        )

    if action == "mute":
        muted_role = await get_or_create_muted_role(guild)
        await member.add_roles(muted_role, reason=f"AI Mod: {reason}")
        await message.channel.send(f"🔇 {member.mention} has been muted for 10 minutes.", delete_after=10)
        await asyncio.sleep(600)
        await member.remove_roles(muted_role, reason="AI Mod mute expired")

    elif action == "kick":
        try:
            await member.kick(reason=f"AI Mod: {reason}")
            await message.channel.send(f"👢 {member} has been kicked.", delete_after=10)
        except discord.Forbidden:
            pass

    elif action == "ban":
        try:
            await member.ban(reason=f"AI Mod: {reason}")
            await message.channel.send(f"🔨 {member} has been banned.", delete_after=10)
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
            await member.kick(reason="⚠️ Anti-Raid: Too many joins in a short time")
        except discord.Forbidden:
            pass


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    member  = message.author
    content = message.content

    # ── ANTI-LINK ──
    if contains_link(content) and not is_allowed_link(content):
        if not member.guild_permissions.manage_messages:
            await message.delete()
            await message.channel.send(
                f"🔗 {member.mention}, posting links is not allowed here!", delete_after=5
            )
            return

    # ── SPAM DETECTION ──
    now = time.time()
    spam_tracker[member.id].append(now)
    recent_msgs = [t for t in spam_tracker[member.id] if now - t < SPAM_TIME_WINDOW]
    spam_tracker[member.id] = recent_msgs

    if len(recent_msgs) >= SPAM_MESSAGE_LIMIT:
        spam_tracker[member.id].clear()
        muted_role = await get_or_create_muted_role(message.guild)
        await member.add_roles(muted_role, reason="Auto-mute: Spam detected")
        await message.channel.send(
            f"🔇 {member.mention} has been muted for spamming. (5 minutes)", delete_after=10
        )
        await asyncio.sleep(300)
        await member.remove_roles(muted_role, reason="Spam mute expired")
        return

    # ── GROQ AI MODERATION ──
    if len(content.strip()) > 3:
        result = await groq_analyze(content)
        if result.get("should_moderate"):
            await execute_mod_action(message, result)
            return

    await bot.process_commands(message)


# ─────────────────────────────────────────
#  MOD COMMANDS
# ─────────────────────────────────────────
def is_mod():
    async def predicate(ctx):
        return ctx.author.guild_permissions.manage_messages
    return commands.check(predicate)


@bot.command()
@is_mod()
async def warn(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    """!warn @user [reason]"""
    warn_data[member.id].append(reason)
    warn_count = len(warn_data[member.id])
    await ctx.send(f"⚠️ {member.mention} has been warned. Total: **{warn_count}**\nReason: {reason}")

    if warn_count == 3:
        muted_role = await get_or_create_muted_role(ctx.guild)
        await member.add_roles(muted_role, reason="3 warnings — auto mute")
        await ctx.send(f"🔇 {member.mention} has been muted for reaching 3 warnings.")
    elif warn_count >= 5:
        await member.ban(reason="5+ warnings — auto ban")
        await ctx.send(f"🔨 {member.mention} has been banned for reaching {warn_count} warnings.")


@bot.command()
@is_mod()
async def warnings(ctx, member: discord.Member):
    """!warnings @user"""
    warns = warn_data.get(member.id, [])
    if not warns:
        return await ctx.send(f"✅ {member.mention} has no warnings.")
    warn_list = "\n".join(f"{i+1}. {w}" for i, w in enumerate(warns))
    await ctx.send(f"⚠️ **Warnings for {member}** ({len(warns)} total):\n{warn_list}")


@bot.command()
@is_mod()
async def clearwarns(ctx, member: discord.Member):
    """!clearwarns @user"""
    warn_data[member.id] = []
    await ctx.send(f"✅ All warnings for {member.mention} have been cleared.")


@bot.command()
@is_mod()
async def mute(ctx, member: discord.Member, duration: int = 10, *, reason: str = "No reason provided"):
    """!mute @user [minutes] [reason]"""
    muted_role = await get_or_create_muted_role(ctx.guild)
    await member.add_roles(muted_role, reason=reason)
    await ctx.send(f"🔇 {member.mention} has been muted for {duration} minute(s). Reason: {reason}")
    await asyncio.sleep(duration * 60)
    await member.remove_roles(muted_role, reason="Mute expired")


@bot.command()
@is_mod()
async def unmute(ctx, member: discord.Member):
    """!unmute @user"""
    muted_role = await get_or_create_muted_role(ctx.guild)
    await member.remove_roles(muted_role)
    await ctx.send(f"🔊 {member.mention} has been unmuted.")


@bot.command()
@is_mod()
async def kick(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    """!kick @user [reason]"""
    await member.kick(reason=reason)
    await ctx.send(f"👢 {member} has been kicked. Reason: {reason}")


@bot.command()
@is_mod()
async def ban(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    """!ban @user [reason]"""
    await member.ban(reason=reason)
    await ctx.send(f"🔨 {member} has been banned. Reason: {reason}")


@bot.command()
@is_mod()
async def purge(ctx, amount: int = 10):
    """!purge [amount]"""
    deleted = await ctx.channel.purge(limit=amount + 1)
    await ctx.send(f"🗑️ Deleted {len(deleted) - 1} messages.", delete_after=5)


# ─────────────────────────────────────────
#  ERROR HANDLER
# ─────────────────────────────────────────
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission to use this.", delete_after=5)
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("❌ User not found.", delete_after=5)
    elif isinstance(error, commands.CheckFailure):
        await ctx.send("❌ Only moderators can use this command.", delete_after=5)
    else:
        print(f"[Error] {error}")


# ─────────────────────────────────────────
#  START
# ─────────────────────────────────────────
keep_alive()

try:
    bot.run(DISCORD_TOKEN)
except Exception as e:
    print(f"❌ BOT CRASH: {e}", flush=True)
    raise
