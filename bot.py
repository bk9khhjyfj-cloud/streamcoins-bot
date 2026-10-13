
import os
import random
import sqlite3
import time

import discord
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")
CHAT_REWARD = 5
CHAT_COOLDOWN = 60
STARTING_COINS = 100

db = sqlite3.connect("streamcoins.db")
db.execute("""
CREATE TABLE IF NOT EXISTS balances (
    user_id INTEGER PRIMARY KEY,
    coins INTEGER NOT NULL DEFAULT 100
)
""")
db.commit()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
last_reward = {}


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


@bot.event
async def on_ready():
    print(f"StreamCoins online as {bot.user}")


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    now = time.time()
    uid = message.author.id

    if now - last_reward.get(uid, 0) >= CHAT_COOLDOWN:
        add_coins(uid, CHAT_REWARD)
        last_reward[uid] = now

    await bot.process_commands(message)


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
            f"🎰 You won {amount:,} coins! "
            f"Balance: {balance(uid):,}"
        )
    else:
        add_coins(uid, -amount)
        await ctx.send(
            f"🎰 You lost {amount:,} coins. "
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
    await ctx.send(f"🪙 Gave {amount:,} coins to {member.mention}.")


@givecoins.error
async def givecoins_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need Manage Server permission.")
    else:
        await ctx.send("Usage: !givecoins @member 500")


# ----- GAMBLE AND RAFFLE COMMANDS -----

import asyncio

# Change this to match your Discord role name exactly.
MODERATOR_ROLE = "Moderator"

active_raffle = None


@bot.command()
async def gamble(ctx, amount: int):
    uid = ctx.author.id
    coins = balance(uid)

    if amount <= 0:
        await ctx.send("Enter a positive amount, e.g. !gamble 100")
        return

    if amount > coins:
        await ctx.send(
            f"You only have {coins:,} coins to bet."
        )
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
@commands.has_role(MODERATOR_ROLE)
async def raffle(ctx, prize: int):
    global active_raffle

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
    }

    await ctx.send(
        f"🎟️ **RAFFLE STARTED!**\n"
        f"Prize pool: 🪙 {prize:,} coins\n"
        f"React by typing `!join` within 60 seconds to enter!"
    )

    await asyncio.sleep(60)

    raffle_data = active_raffle
    active_raffle = None

    entrants = list(raffle_data["entrants"])
    prize = raffle_data["prize"]
    channel = raffle_data["channel"]

    if not entrants:
        await channel.send(
            "🎟️ Raffle ended with no entrants. No coins awarded."
        )
        return

    winners = random.sample(entrants, min(2, len(entrants)))
    each_prize = prize // len(winners)
    remainder = prize % len(winners)

    for index, winner_id in enumerate(winners):
        reward = each_prize + (1 if index < remainder else 0)
        add_coins(winner_id, reward)

    winner_text = "\n".join(
        f"🏆 <@{winner_id}> — 🪙 "
        f"{each_prize + (1 if index < remainder else 0):,} coins"
        for index, winner_id in enumerate(winners)
    )

    await channel.send(
        f"🎉 **RAFFLE FINISHED!**\n{winner_text}"
    )


@bot.command()
async def join(ctx):
    if active_raffle is None:
        await ctx.send("There isn't a raffle running right now.")
        return

    if ctx.guild is None:
        return

    if ctx.author.bot:
        return

    active_raffle["entrants"].add(ctx.author.id)
    await ctx.send(f"🎟️ {ctx.author.mention}, you're in the raffle!")


@raffle.error
async def raffle_error(ctx, error):
    if isinstance(error, commands.MissingRole):
        await ctx.send(
            "Only members with the Moderator role can start a raffle."
        )
    elif isinstance(error, commands.BadArgument):
        await ctx.send("Usage: !raffle 10000")
    else:
        await ctx.send("Couldn't start the raffle. Check the command and try again.")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not configured.")

bot.run(TOKEN)
