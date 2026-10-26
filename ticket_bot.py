"""
Glazed - DonutSMP Trading Server Ticket Bot
---------------------------------------------
Lets customers open a ticket by clicking a button, picks a category
(Buy / Sell / Middleman / Support), creates a private channel visible
only to them + staff, and lets staff claim/close tickets with a
saved transcript.

Setup:
1. pip install discord.py
2. Set env vars (or edit the CONFIG block below):
     DISCORD_BOT_TOKEN
     DISCORD_GUILD_ID
3. Fill in CONFIG with your real category ID and staff role IDs
   (run !ticketpanel once the bot is in your server, or use /ticketpanel)
4. Run: python ticket_bot.py
"""

import os
import io
import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "MTU2NDA5MDkzNDM5ODk0NzM0Mg.G4qW7D.U4oMSw3AaiKmMi6Jaua6aN0gRNt_OlT9969xY0")
GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "1564086171628212244"))

# Names must match roles/categories created by setup_server.py
TICKET_CATEGORY_NAME = "TICKETS"          # category new ticket channels go in
LOG_CHANNEL_NAME = "ticket-logs"          # where closed transcripts are posted
STAFF_ROLE_NAMES = ["Owner", "Admin", "Moderator", "Support", "Middleman"]

TICKET_TYPES = {
    "buy": {
        "label": "Buy In-Game Currency",
        "emoji": "💰",
        "prompt": (
            "**New Buy Order**\n"
            "Please provide:\n"
            "1. Amount you want to buy (e.g. 100M, 1B)\n"
            "2. Your DonutSMP in-game username\n"
            "3. Preferred payment method\n\n"
            "A seller or staff member will confirm the rate and assign a Middleman."
        ),
    },
    "sell": {
        "label": "Sell In-Game Currency",
        "emoji": "📤",
        "prompt": (
            "**New Sell Order**\n"
            "Please provide:\n"
            "1. Amount you want to sell\n"
            "2. Your DonutSMP in-game username\n"
            "3. Preferred payout method\n\n"
            "A buyer or staff member will confirm the rate and assign a Middleman."
        ),
    },
    "middleman": {
        "label": "Request a Middleman",
        "emoji": "🤝",
        "prompt": (
            "**Middleman Request**\n"
            "Please provide:\n"
            "1. Both parties involved (@mention if possible)\n"
            "2. What's being traded on each side\n"
            "3. Agreed amount / price\n\n"
            "A Middleman will join shortly to walk the trade through safely."
        ),
    },
    "support": {
        "label": "General Support",
        "emoji": "🎫",
        "prompt": (
            "**Support Request**\n"
            "Describe your issue in as much detail as you can, including any "
            "order numbers, usernames, or screenshots. Staff will be with you shortly."
        ),
    },
}

# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


def staff_roles(guild: discord.Guild):
    return [r for r in guild.roles if r.name in STAFF_ROLE_NAMES]


def ticket_overwrites(guild: discord.Guild, member: discord.Member):
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, attach_files=True
        ),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
    }
    for role in staff_roles(guild):
        overwrites[role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True, manage_messages=True
        )
    return overwrites


async def get_or_create_category(guild: discord.Guild) -> discord.CategoryChannel:
    category = discord.utils.get(guild.categories, name=TICKET_CATEGORY_NAME)
    if category is None:
        category = await guild.create_category(TICKET_CATEGORY_NAME)
    return category


async def get_or_create_log_channel(guild: discord.Guild) -> discord.TextChannel:
    channel = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)
    if channel is None:
        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
        for role in staff_roles(guild):
            overwrites[role] = discord.PermissionOverwrite(view_channel=True)
        channel = await guild.create_text_channel(LOG_CHANNEL_NAME, overwrites=overwrites)
    return channel


# ---------------------------------------------------------------------------
# UI: ticket type picker (the panel users click)
# ---------------------------------------------------------------------------

class TicketTypeSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=data["label"], value=key, emoji=data["emoji"])
            for key, data in TICKET_TYPES.items()
        ]
        super().__init__(
            placeholder="Choose a ticket type...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="glazed_ticket_select",
        )

    async def callback(self, interaction: discord.Interaction):
        key = self.values[0]
        await create_ticket(interaction, key)


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketTypeSelect())


# ---------------------------------------------------------------------------
# UI: inside-ticket controls (claim / close)
# ---------------------------------------------------------------------------

class TicketControlsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.primary, emoji="🙋", custom_id="glazed_claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user
        if not any(r.name in STAFF_ROLE_NAMES for r in member.roles):
            await interaction.response.send_message("Only staff can claim tickets.", ephemeral=True)
            return
        await interaction.channel.send(f"🙋 **{member.mention} has claimed this ticket.**")
        await interaction.response.defer()

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="glazed_close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Closing ticket and saving transcript in 5 seconds...")
        await close_ticket(interaction.channel, interaction.user)


# ---------------------------------------------------------------------------
# Core actions
# ---------------------------------------------------------------------------

async def create_ticket(interaction: discord.Interaction, ticket_key: str):
    guild = interaction.guild
    member = interaction.user
    data = TICKET_TYPES[ticket_key]

    existing = discord.utils.get(
        guild.text_channels, name=f"{ticket_key}-{member.name}".lower().replace(" ", "-")
    )
    if existing:
        await interaction.response.send_message(
            f"You already have an open ticket: {existing.mention}", ephemeral=True
        )
        return

    await interaction.response.send_message("Creating your ticket...", ephemeral=True)

    category = await get_or_create_category(guild)
    overwrites = ticket_overwrites(guild, member)
    channel_name = f"{ticket_key}-{member.name}".lower().replace(" ", "-")

    channel = await guild.create_text_channel(
        channel_name, category=category, overwrites=overwrites,
        topic=f"Ticket type: {data['label']} | Opened by {member} ({member.id})"
    )

    staff_mentions = " ".join(r.mention for r in staff_roles(guild)) or "@staff"
    embed = discord.Embed(
        title=f"{data['emoji']} {data['label']}",
        description=data["prompt"],
        color=0xF45B8C,
        timestamp=datetime.now(timezone.utc),
    )
    embed.set_footer(text=f"Opened by {member}")

    await channel.send(content=f"{member.mention} {staff_mentions}", embed=embed, view=TicketControlsView())


async def close_ticket(channel: discord.TextChannel, closer: discord.Member):
    guild = channel.guild

    # Build a plain-text transcript
    lines = [f"Transcript for #{channel.name} — closed by {closer} at {datetime.now(timezone.utc)}\n"]
    async for msg in channel.history(limit=500, oldest_first=True):
        content = msg.content or "[embed/attachment]"
        lines.append(f"[{msg.created_at:%Y-%m-%d %H:%M}] {msg.author}: {content}")
    transcript = "\n".join(lines)

    log_channel = await get_or_create_log_channel(guild)
    file = discord.File(io.StringIO(transcript), filename=f"{channel.name}-transcript.txt")
    await log_channel.send(content=f"🔒 Ticket **#{channel.name}** closed by {closer.mention}", file=file)

    import asyncio
    await asyncio.sleep(5)
    await channel.delete(reason=f"Ticket closed by {closer}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    bot.add_view(TicketPanelView())
    bot.add_view(TicketControlsView())
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"Sync failed: {e}")
    print(f"Logged in as {bot.user}")


@bot.tree.command(name="ticketpanel", description="Post the ticket creation panel in this channel")
@app_commands.checks.has_permissions(manage_channels=True)
async def ticketpanel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🍩 Open a Ticket",
        description=(
            "Need to buy, sell, trade, or ask for help? Pick a ticket type below "
            "and a private channel will be created just for you and staff."
        ),
        color=0xF45B8C,
    )
    await interaction.channel.send(embed=embed, view=TicketPanelView())
    await interaction.response.send_message("Ticket panel posted.", ephemeral=True)


@bot.tree.command(name="close", description="Close the current ticket")
async def close_slash(interaction: discord.Interaction):
    if not interaction.channel.name.split("-")[0] in TICKET_TYPES:
        await interaction.response.send_message("This isn't a ticket channel.", ephemeral=True)
        return
    await interaction.response.send_message("Closing ticket and saving transcript in 5 seconds...")
    await close_ticket(interaction.channel, interaction.user)


@bot.tree.command(name="add", description="Add a user to this ticket")
@app_commands.describe(member="The member to add to this ticket")
async def add_user(interaction: discord.Interaction, member: discord.Member):
    await interaction.channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)
    await interaction.response.send_message(f"{member.mention} added to this ticket.")


if __name__ == "__main__":
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE" or GUILD_ID == 0:
        print("Set DISCORD_BOT_TOKEN and DISCORD_GUILD_ID before running.")
    else:
        bot.run(BOT_TOKEN)
