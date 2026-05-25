import discord
import anthropic
import asyncio
import os
import json
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')
YOUR_DISCORD_ID = int(os.getenv('YOUR_DISCORD_ID', '386704722872238090'))
SUGGESTIONS_CHANNEL = os.getenv('SUGGESTIONS_CHANNEL', 'suggestions')

TZ_UTC8 = timezone(timedelta(hours=8))

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

client = discord.Client(intents=intents)
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

sent_today = {"daily": None, "weekly": None, "suggestions": None}

SUBSCRIBERS_FILE = "subscribers.json"
CHANNELS_FILE = "channels.json"


# --- Persistence ---

def load_subscribers() -> set:
    try:
        if os.path.exists(SUBSCRIBERS_FILE):
            with open(SUBSCRIBERS_FILE) as f:
                data = set(json.load(f))
                data.add(YOUR_DISCORD_ID)  # owner is always subscribed
                return data
    except Exception:
        pass
    return {YOUR_DISCORD_ID}


def save_subscribers(subs: set):
    with open(SUBSCRIBERS_FILE, 'w') as f:
        json.dump(list(subs), f)


def load_monitored_channels() -> list:
    try:
        if os.path.exists(CHANNELS_FILE):
            with open(CHANNELS_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return []  # empty = monitor all channels


def save_monitored_channels(channels: list):
    with open(CHANNELS_FILE, 'w') as f:
        json.dump(channels, f)


subscribers = load_subscribers()
monitored_channels = load_monitored_channels()


# --- Message Collection ---

async def collect_messages(hours_back: int, max_per_channel: int = 500):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    all_data = {}

    for guild in client.guilds:
        channels_data = {}
        for channel in guild.text_channels:
            if monitored_channels and channel.name not in monitored_channels:
                continue
            try:
                messages = []
                async for msg in channel.history(after=cutoff, limit=max_per_channel):
                    if not msg.author.bot and msg.content.strip():
                        messages.append(
                            f"[#{channel.name}] {msg.author.display_name}: {msg.content[:300]}"
                        )
                if messages:
                    channels_data[channel.name] = messages
            except discord.Forbidden:
                continue
            except Exception as e:
                logger.warning(f"Skipping #{channel.name} in {guild.name}: {e}")

        if channels_data:
            all_data[guild.name] = channels_data

    return all_data


async def collect_top_suggestions(days_back: int = 7):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    results = []

    for guild in client.guilds:
        for channel in guild.text_channels:
            if channel.name != SUGGESTIONS_CHANNEL:
                continue
            try:
                async for msg in channel.history(after=cutoff, limit=1000):
                    if msg.author.bot:
                        continue
                    total_reactions = sum(r.count for r in msg.reactions)
                    results.append({
                        'server': guild.name,
                        'author': msg.author.display_name,
                        'content': msg.content[:400],
                        'reactions': total_reactions,
                        'url': msg.jump_url,
                    })
            except discord.Forbidden:
                logger.warning(f"No access to #{SUGGESTIONS_CHANNEL} in {guild.name}")
            except Exception as e:
                logger.error(f"Error collecting suggestions from {guild.name}: {e}")

    results.sort(key=lambda x: x['reactions'], reverse=True)
    return results[:10]


# --- Claude Analysis ---

def format_for_claude(messages_data: dict, max_messages: int = 4000):
    lines = []
    total = 0

    for server_name, channels in messages_data.items():
        lines.append(f"\n{'='*40}\nServer: {server_name}\n{'='*40}")
        for channel_name, messages in channels.items():
            lines.append(f"\n--- #{channel_name} ({len(messages)} messages) ---")
            for msg in messages:
                if total >= max_messages:
                    lines.append("[... remaining messages truncated due to volume ...]")
                    return "\n".join(lines), total
                lines.append(msg)
                total += 1

    return "\n".join(lines), total


def call_claude(messages_text: str, report_type: str, date_range: str) -> str:
    if report_type == "daily":
        system = "You are a professional community analytics assistant. Write concise, actionable daily reports for gaming Discord community managers."
        prompt = f"""Analyze these Discord gaming community messages and write a daily report.

**Date:** {date_range}

## 🔥 Hot Topics (Top 5)
- Topic 1: brief description
(etc.)

## 😊 Sentiment Overview
Overall mood: [Positive / Mixed / Negative]
- Positive: X%
- Neutral: X%
- Negative: X%
What's driving the sentiment:

## ⚠️ Issues & Complaints Needing Attention
(Specific bugs, frustrations, or conflicts — be concrete)

## ✨ Community Highlights
(Positive moments, player excitement, notable achievements)

## 📋 Today's Action Items
1. (Most urgent)
2.
3.

---
Messages from yesterday:
{messages_text}"""

    else:
        system = "You are a professional community analytics assistant. Write strategic weekly reports for gaming Discord community managers."
        prompt = f"""Analyze these Discord gaming community messages and write a weekly summary report.

**Week:** {date_range}

## 📈 Week at a Glance
(2-3 sentence summary of community health and activity)

## 🔥 Top 7 Topics This Week
1. Topic: description
(etc.)

## 😊 Sentiment Trend
How did sentiment shift through the week? Any notable spikes or drops?

## ⚠️ Top Issues This Week
(Most important unresolved problems needing follow-up)

## ✨ Week Highlights & Wins
(Best community moments, successful events, top player engagement)

## 👥 Community Dynamics
(Engagement patterns, active community segments, notable behavior trends)

## 📋 Priorities for Next Week
1.
2.
3.

---
Messages from this week:
{messages_text}"""

    response = anthropic_client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2500,
        system=system,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text


# --- Sending ---

async def send_dm(user_id: int, content: str, title: str = None):
    try:
        user = await client.fetch_user(user_id)
        if title:
            await user.send(title)
        for i in range(0, len(content), 1900):
            await user.send(content[i:i + 1900])
            await asyncio.sleep(0.5)
    except Exception as e:
        logger.error(f"Failed to DM {user_id}: {e}")


async def broadcast(content: str, title: str = None):
    for user_id in list(subscribers):
        await send_dm(user_id, content, title)


# --- Reports ---

async def do_daily_report():
    now_utc8 = datetime.now(TZ_UTC8)
    date_str = (now_utc8 - timedelta(days=1)).strftime("%Y-%m-%d")
    logger.info(f"Starting daily report for {date_str}")
    await send_dm(YOUR_DISCORD_ID, f"⏳ Generating daily report for {date_str}...")

    try:
        data = await collect_messages(hours_back=24)
        if not data:
            await broadcast("No player messages found in the past 24 hours.", f"📊 **Daily Report — {date_str}**")
            return

        text, count = format_for_claude(data)
        logger.info(f"Collected {count} messages for daily report")
        report = call_claude(text, "daily", date_str)
        await broadcast(report, f"📊 **Daily Community Report — {date_str}** ({count} messages analyzed)")

    except Exception as e:
        logger.error(f"Daily report failed: {e}")
        await send_dm(YOUR_DISCORD_ID, f"❌ Daily report failed: {str(e)}")


async def do_weekly_report():
    now_utc8 = datetime.now(TZ_UTC8)
    week_end = now_utc8.strftime("%Y-%m-%d")
    week_start = (now_utc8 - timedelta(days=6)).strftime("%Y-%m-%d")
    date_range = f"{week_start} to {week_end}"
    logger.info(f"Starting weekly report for {date_range}")
    await send_dm(YOUR_DISCORD_ID, f"⏳ Generating weekly report for {date_range}...")

    try:
        data = await collect_messages(hours_back=168, max_per_channel=300)
        if not data:
            await broadcast("No player messages found this week.", f"📈 **Weekly Report — {date_range}**")
            return

        text, count = format_for_claude(data, max_messages=5000)
        logger.info(f"Collected {count} messages for weekly report")
        report = call_claude(text, "weekly", date_range)
        await broadcast(report, f"📈 **Weekly Community Report — {date_range}** ({count} messages analyzed)")

    except Exception as e:
        logger.error(f"Weekly report failed: {e}")
        await send_dm(YOUR_DISCORD_ID, f"❌ Weekly report failed: {str(e)}")


async def do_suggestions_report():
    now_utc8 = datetime.now(TZ_UTC8)
    week_end = now_utc8.strftime("%Y-%m-%d")
    week_start = (now_utc8 - timedelta(days=6)).strftime("%Y-%m-%d")
    date_range = f"{week_start} to {week_end}"
    logger.info("Starting suggestions report")
    await send_dm(YOUR_DISCORD_ID, f"⏳ Collecting top suggestions for {date_range}...")

    try:
        top = await collect_top_suggestions(days_back=7)
        if not top:
            await broadcast(
                f"No suggestions found in #{SUGGESTIONS_CHANNEL} this week.",
                f"💡 **Top Suggestions — {date_range}**"
            )
            return

        lines = []
        for i, s in enumerate(top):
            lines.append(
                f"**#{i+1}** 👍 {s['reactions']} reactions — @{s['author']} ({s['server']})\n"
                f"> {s['content']}\n"
                f"🔗 {s['url']}\n"
            )

        report = "\n".join(lines)
        await broadcast(report, f"💡 **Top 10 Suggestions This Week — {date_range}**")

    except Exception as e:
        logger.error(f"Suggestions report failed: {e}")
        await send_dm(YOUR_DISCORD_ID, f"❌ Suggestions report failed: {str(e)}")


# --- Commands ---

async def handle_dm_command(message):
    global subscribers, monitored_channels
    raw = message.content.strip()
    cmd = raw.lower()
    is_owner = message.author.id == YOUR_DISCORD_ID

    if cmd == "!help":
        if is_owner:
            await message.channel.send(
                "**📖 Bot Commands**\n\n"
                "**Reports:**\n"
                "`!daily` — send yesterday's daily report now\n"
                "`!weekly` — send this week's report now\n"
                "`!suggestions` — send top 10 suggestions now\n\n"
                "**Channel monitoring:**\n"
                "`!addchannel general` — add channel to monitor\n"
                "`!removechannel general` — remove channel\n"
                "`!channels` — list monitored channels\n\n"
                "**Subscribers:**\n"
                "`!addsubscriber USER_ID` — add a subscriber\n"
                "`!removesubscriber USER_ID` — remove a subscriber\n"
                "`!subscribers` — list all subscribers\n\n"
                "**Other:**\n"
                "`!status` — bot status overview"
            )
        else:
            await message.channel.send("❌ You don't have permission to use this bot.")
        return

    # All commands are owner-only
    if not is_owner:
        await message.channel.send("❌ You don't have permission to use this bot.")
        return

    if cmd == "!daily":
        await message.channel.send("Generating yesterday's daily report now...")
        await do_daily_report()

    elif cmd == "!weekly":
        await message.channel.send("Generating weekly report now...")
        await do_weekly_report()

    elif cmd == "!suggestions":
        await message.channel.send("Collecting top suggestions now...")
        await do_suggestions_report()

    elif cmd == "!status":
        ch_status = ", ".join([f"#{c}" for c in monitored_channels]) if monitored_channels else "All channels"
        servers = ", ".join([g.name for g in client.guilds])
        now = datetime.now(TZ_UTC8).strftime("%Y-%m-%d %H:%M UTC+8")
        await message.channel.send(
            f"✅ **Bot Status**\n"
            f"Time: {now}\n"
            f"Servers: {servers}\n"
            f"Monitoring: {ch_status}\n"
            f"Suggestions channel: #{SUGGESTIONS_CHANNEL}\n"
            f"Subscribers: {len(subscribers)}"
        )

    elif cmd.startswith("!addchannel "):
        name = raw[len("!addchannel "):].strip().lstrip("#")
        if name not in monitored_channels:
            monitored_channels.append(name)
            save_monitored_channels(monitored_channels)
            await message.channel.send(
                f"✅ Now monitoring: #{name}\n"
                f"All monitored: {', '.join(['#'+c for c in monitored_channels])}"
            )
        else:
            await message.channel.send(f"#{name} is already being monitored.")

    elif cmd.startswith("!removechannel "):
        name = raw[len("!removechannel "):].strip().lstrip("#")
        if name in monitored_channels:
            monitored_channels.remove(name)
            save_monitored_channels(monitored_channels)
            ch_status = ", ".join(['#'+c for c in monitored_channels]) if monitored_channels else "All channels"
            await message.channel.send(f"✅ Removed: #{name}\nNow monitoring: {ch_status}")
        else:
            await message.channel.send(f"#{name} was not in the monitored list.")

    elif cmd == "!channels":
        ch_status = ", ".join(['#'+c for c in monitored_channels]) if monitored_channels else "All channels (no filter set)"
        await message.channel.send(f"📋 Monitored channels: {ch_status}")

    elif cmd.startswith("!addsubscriber "):
        uid_str = raw[len("!addsubscriber "):].strip()
        try:
            uid = int(uid_str)
            if uid in subscribers:
                await message.channel.send(f"`{uid}` is already subscribed.")
            else:
                subscribers.add(uid)
                save_subscribers(subscribers)
                await message.channel.send(f"✅ Added subscriber `{uid}`.")
        except ValueError:
            await message.channel.send("Usage: `!addsubscriber USER_ID`")

    elif cmd == "!subscribers":
        lines = [f"📬 **Subscribers ({len(subscribers)}):**"]
        for uid in subscribers:
            tag = " *(you)*" if uid == YOUR_DISCORD_ID else ""
            lines.append(f"• `{uid}`{tag}")
        await message.channel.send("\n".join(lines))

    elif cmd.startswith("!removesubscriber "):
        uid_str = raw[len("!removesubscriber "):].strip()
        try:
            uid = int(uid_str)
            if uid == YOUR_DISCORD_ID:
                await message.channel.send("❌ Cannot remove the owner.")
            elif uid in subscribers:
                subscribers.discard(uid)
                save_subscribers(subscribers)
                await message.channel.send(f"✅ Removed subscriber `{uid}`.")
            else:
                await message.channel.send(f"`{uid}` is not subscribed.")
        except ValueError:
            await message.channel.send("Usage: `!removesubscriber USER_ID`")

    else:
        await message.channel.send("Unknown command. Type `!help` to see all commands.")


@client.event
async def on_message(message):
    if message.author.bot:
        return
    if isinstance(message.channel, discord.DMChannel):
        await handle_dm_command(message)


async def scheduler_loop():
    await client.wait_until_ready()
    logger.info("Scheduler started")

    while not client.is_closed():
        now = datetime.now(TZ_UTC8)
        today_key = now.strftime("%Y-%m-%d")

        if now.hour == 10 and now.minute == 0:
            if sent_today["daily"] != today_key:
                sent_today["daily"] = today_key
                asyncio.ensure_future(do_daily_report())

            if now.weekday() == 3:  # Thursday
                if sent_today["weekly"] != today_key:
                    sent_today["weekly"] = today_key
                    asyncio.ensure_future(do_weekly_report())
                if sent_today["suggestions"] != today_key:
                    sent_today["suggestions"] = today_key
                    asyncio.ensure_future(do_suggestions_report())

        await asyncio.sleep(60)


@client.event
async def on_ready():
    servers = [g.name for g in client.guilds]
    logger.info(f"Bot online: {client.user} | Servers: {servers}")
    ch_status = ", ".join(monitored_channels) if monitored_channels else "All channels"

    try:
        user = await client.fetch_user(YOUR_DISCORD_ID)
        await user.send(
            f"✅ **Community Reporter Bot is online!**\n"
            f"Connected to: {', '.join(servers)}\n"
            f"Monitoring: {ch_status}\n"
            f"Subscribers: {len(subscribers)}\n\n"
            f"**Schedule:**\n"
            f"• Daily report: every day at 10:00 AM UTC+8\n"
            f"• Weekly report + Top suggestions: every Thursday at 10:00 AM UTC+8\n\n"
            f"Type `!help` to see all commands."
        )
    except Exception as e:
        logger.error(f"Could not DM owner on startup: {e}")

    asyncio.ensure_future(scheduler_loop())


client.run(DISCORD_TOKEN)
