
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


if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not configured.")

bot.run(TOKEN)
