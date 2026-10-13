
import os
import random
import sqlite3
import time
import asyncio
import discord
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")

CHAT_REWARD = 5
CHAT_COOLDOWN = 60
STARTING_COINS = 100
MODERATOR_ROLE = "Stream Mod"
RAFFLE_SECONDS = 60

# Your new server's #coins channel ID
COINS_CHANNEL_ID = 1559575575239069737

db = sqlite3.connect("streamcoins.db")

db.execute("""
CREATE TABLE IF NOT EXISTS balances (
    user_id INTEGER PRIMARY KEY,
    coins INTEGER NOT NULL DEFAULT 100
)
""")
db.commit()


def balance(user_id):
    row = db.execute(
        "SELECT coins FROM balances WHERE user_id = ?",
        (user_id,)
    ).fetchone()

    if row is None:
        db.execute(
            "INSERT INTO balances (user_id, coins) VALUES (?, ?)",
            (user_id, STARTING_COINS)
        )
        db.commit()
        return STARTING_COINS

    return row[0]


def add_coins(user_id, amount):
    balance(user_id)
    db.execute(
        "UPDATE balances SET coins = coins + ? WHERE user_id = ?",
        (amount, user_id)
    )
    db.commit()


intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

last_reward = {}
active_raffle = None


@bot.event
async def on_ready():
    print(f"StreamCoins online as {bot.user}")


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # Award chat coins in any channel
    uid = message.author.id
    now = time.time()

    if now - last_reward.get(uid, 0) >= CHAT_COOLDOWN:
        add_coins(uid, CHAT_REWARD)
        last_reward[uid] = now

    await bot.process_commands(message)


@bot.check
async def commands_only_in_coins(ctx):
    if ctx.guild is None:
        return False

    if ctx.channel.id != COINS_CHANNEL_ID:
        if ctx.command:
            await ctx.send(
                f"🪙 Please use my commands in <#{COINS_CHANNEL_ID}>!"
            )
        return False

    return True


@bot.command(name="balance")
async def show_balance(ctx):
    await ctx.send(
        f"🪙 {ctx.author.display_name}: "
        f"{balance(ctx.author.id):,} coins"
    )


@bot.command()
async def gamble(ctx, amount: int):
    uid = ctx.author.id
    coins = balance(uid)

    if amount <= 0:
        await ctx.send("Enter a positive amount, e.g. !gamble 100")
        return

    if amount > coins:
        await ctx.send(f"You only have {coins:,} coins.")
        return

    if random.random() < 0.45:
        add_coins(uid, amount)
        await ctx.send(
            f"🎰 {ctx.author.mention} won {amount:,} coins! "
            f"Balance: {balance(uid):,}"
        )
    else:
        add_coins(uid, -amount)
        await ctx.send(
            f"🎰 {ctx.author.mention} lost {amount:,} coins. "
            f"Balance: {balance(uid):,}"
        )


@bot.command()
async def leaderboard(ctx):
    rows = db.execute(
        "SELECT user_id, coins FROM balances "
        "ORDER BY coins DESC LIMIT 10"
    ).fetchall()

    lines = []

    for rank, (uid, coins) in enumerate(rows, 1):
        member = ctx.guild.get_member(uid) if ctx.guild else None
        name = member.display_name if member else f"User {uid}"
        lines.append(f"**{rank}.** {name} — 🪙 {coins:,}")

    await ctx.send(
        "🏆 **StreamCoins Leaderboard**\n" +
        ("\n".join(lines) if lines else "No scores yet.")
    )


@bot.command()
@commands.has_permissions(manage_guild=True)
async def givecoins(ctx, member: discord.Member, amount: int):
    if amount <= 0:
        await ctx.send("Amount must be positive.")
        return

    add_coins(member.id, amount)

    await ctx.send(
        f"🪙 Gave {amount:,} coins to {member.mention}."
    )


@givecoins.error
async def givecoins_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need Manage Server permission.")
    else:
        await ctx.send("Usage: !givecoins @member 500")


@bot.command()
@commands.has_role(MODERATOR_ROLE)
async def raffle(ctx, prize: int):
    global active_raffle

    if ctx.guild is None:
        await ctx.send("Start a raffle inside your Discord server.")
        return

    if active_raffle is not None:
        await ctx.send("A raffle is already running!")
        return

    if prize <= 0:
        await ctx.send("The prize must be more than 0 coins.")
        return

    active_raffle = {
        "prize": prize,
        "entrants": set(),
        "channel": ctx.channel,
        "guild_id": ctx.guild.id,
    }

    await ctx.send(
        f"🎟️ **RAFFLE STARTED!**\n"
        f"Prize pool: 🪙 {prize:,} coins\n"
        f"Two random bonus winners will each receive "
        f"30% of the prize pool.\n"
        f"The remaining 40% will be shared equally among "
        f"all other entrants.\n"
        f"Type `!join` within {RAFFLE_SECONDS} seconds "
        f"to enter for free!"
    )

    await asyncio.sleep(RAFFLE_SECONDS)

    raffle_data = active_raffle

    if raffle_data is None:
        return

    active_raffle = None

    entrants = list(raffle_data["entrants"])
    prize = raffle_data["prize"]
    channel = raffle_data["channel"]

    if not entrants:
        await channel.send(
            "🎟️ Raffle ended with no entrants. No coins awarded."
        )
        return

    # If fewer than 3 people enter, split the whole prize pool.
    if len(entrants) < 3:
        each_prize = prize // len(entrants)
        remainder = prize % len(entrants)
        results = []

        for index, winner_id in enumerate(entrants):
            reward = each_prize + (1 if index < remainder else 0)
            add_coins(winner_id, reward)
            results.append(
                f"🪙 <@{winner_id}> — {reward:,} coins"
            )

        await channel.send(
            "🎉 **RAFFLE FINISHED!**\n"
            "Fewer than 3 people entered, so the full prize "
            "pool was shared between them.\n" +
            "\n".join(results)
        )
        return

    # Pick two different random bonus winners.
    bonus_winners = random.sample(entrants, 2)

    # All remaining entrants share the rest of the pool.
    other_entrants = [
        uid for uid in entrants if uid not in bonus_winners
    ]

    # Each bonus winner receives 30% of the total prize.
    bonus_each = (prize * 30) // 100
    bonus_total = bonus_each * 2
    remaining_pool = prize - bonus_total

    # Split all remaining coins evenly, including any leftovers.
    each_share = remaining_pool // len(other_entrants)
    remainder = remaining_pool % len(other_entrants)

    results = [
        f"🏆 <@{uid}> — 🪙 {bonus_each:,} bonus coins"
        for uid in bonus_winners
    ]

    for index, uid in enumerate(other_entrants):
        reward = each_share + (1 if index < remainder else 0)

        if reward > 0:
            add_coins(uid, reward)

        results.append(
            f"🪙 <@{uid}> — {reward:,} shared coins"
        )

    await channel.send(
        "🎉 **RAFFLE FINISHED!**\n" +
        "\n".join(results)
    )


@raffle.error
async def raffle_error(ctx, error):
    if isinstance(error, commands.MissingRole):
        await ctx.send(
            f"Only members with the **{MODERATOR_ROLE}** role "
            "can start a raffle."
        )
    elif isinstance(error, commands.BadArgument):
        await ctx.send("Usage: !raffle 20000")
    else:
        print(f"Raffle error: {repr(error)}")
        await ctx.send(
            "Something went wrong starting the raffle. "
            "Please check the bot logs."
        )


@bot.command()
async def join(ctx):
    if active_raffle is None:
        await ctx.send("There isn't a raffle running right now.")
        return

    if ctx.guild is None:
        return

    if ctx.guild.id != active_raffle["guild_id"]:
        await ctx.send("There isn't a raffle running in this server.")
        return

    entrants = active_raffle["entrants"]

    if ctx.author.id in entrants:
        await ctx.send("You're already entered in this raffle!")
        return

    entrants.add(ctx.author.id)

    await ctx.send(
        f"🎟️ {ctx.author.mention}, you're in the raffle!"
    )


if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not configured.")

bot.run(TOKEN)
