
import os
import random
import time
import uuid
import json
import sqlite3
import asyncio
import discord
from discord.ext import commands

# ---------------- CONFIGURATION ----------------

TOKEN = os.getenv("DISCORD_TOKEN")

CHAT_REWARD = 5
CHAT_COOLDOWN = 60
STARTING_COINS = 100

MODERATOR_ROLE = "Stream Mod"
RAFFLE_SECONDS = 60
COINS_CHANNEL_ID = 1559575575239069737

# Keep the existing database. Do not delete or reset it.
db = sqlite3.connect("streamcoins.db", timeout=30)
db.execute("PRAGMA busy_timeout = 30000")

db.execute("""
CREATE TABLE IF NOT EXISTS balances (
    user_id INTEGER PRIMARY KEY,
    coins INTEGER NOT NULL DEFAULT 100
)
""")

# Records completed raffle payouts to prevent duplicate payout IDs.
db.execute("""
CREATE TABLE IF NOT EXISTS raffle_payouts (
    raffle_id TEXT PRIMARY KEY,
    prize INTEGER NOT NULL,
    payouts TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
""")
db.commit()

# ---------------- DISCORD SETUP ----------------

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

last_reward = {}
active_raffle = None


# ---------------- BALANCE FUNCTIONS ----------------

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


# ---------------- SAFE RAFFLE PAYOUT ----------------

def pay_raffle_atomically(raffle_id, prize, payouts):
    """Apply every payout in one database transaction."""

    total = sum(payouts.values())
    if total != prize:
        raise ValueError(
            f"Payout total {total} does not equal prize {prize}"
        )

    payout_json = json.dumps({
        str(user_id): amount
        for user_id, amount in payouts.items()
    })

    db.execute("BEGIN IMMEDIATE")

    try:
        # The unique raffle ID prevents this raffle being paid twice.
        db.execute(
            """
            INSERT INTO raffle_payouts (raffle_id, prize, payouts)
            VALUES (?, ?, ?)
            """,
            (raffle_id, prize, payout_json)
        )

        for user_id, amount in payouts.items():
            db.execute(
                """
                INSERT OR IGNORE INTO balances (user_id, coins)
                VALUES (?, ?)
                """,
                (user_id, STARTING_COINS)
            )

            before = db.execute(
                "SELECT coins FROM balances WHERE user_id = ?",
                (user_id,)
            ).fetchone()[0]

            db.execute(
                """
                UPDATE balances
                SET coins = coins + ?
                WHERE user_id = ?
                """,
                (amount, user_id)
            )

            after = db.execute(
                "SELECT coins FROM balances WHERE user_id = ?",
                (user_id,)
            ).fetchone()[0]

            if after != before + amount:
                raise RuntimeError(
                    f"Balance verification failed for user {user_id}"
                )

            print(
                f"RAFFLE PAYOUT CHECK | raffle={raffle_id} "
                f"| user={user_id} | before={before} "
                f"| added={amount} | after={after}"
            )

        db.commit()

    except Exception:
        db.rollback()
        raise

    # Read balances again after commit for console verification.
    for user_id, amount in payouts.items():
        saved = db.execute(
            "SELECT coins FROM balances WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if saved is None:
            raise RuntimeError(
                f"Missing balance after commit for user {user_id}"
            )

        print(
            f"RAFFLE PAYOUT SAVED | raffle={raffle_id} "
            f"| user={user_id} | added={amount} "
            f"| saved_balance={saved[0]}"
        )

    print(
        f"RAFFLE COMPLETE | raffle={raffle_id} "
        f"| total_paid={total}"
    )


# ---------------- CHANNEL RESTRICTION ----------------

@bot.check
async def commands_only_in_coins(ctx):
    if ctx.guild is None:
        return False

    if ctx.channel.id != COINS_CHANNEL_ID:
        await ctx.send(
            f"🪙 Please use my commands in <#{COINS_CHANNEL_ID}>!"
        )
        return False

    return True


# ---------------- CHAT REWARDS ----------------

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    user_id = message.author.id
    now = time.time()

    if now - last_reward.get(user_id, 0) >= CHAT_COOLDOWN:
        try:
            add_coins(user_id, CHAT_REWARD)
            last_reward[user_id] = now
        except sqlite3.Error as error:
            print(f"CHAT REWARD DATABASE ERROR: {error!r}")

    await bot.process_commands(message)


# ---------------- STARTUP ----------------

@bot.event
async def on_ready():
    print(f"StreamCoins online as {bot.user}")
    print(f"Database: {os.path.abspath('streamcoins.db')}")
    print(f"Commands channel ID: {COINS_CHANNEL_ID}")


# ---------------- BALANCE ----------------

@bot.command(name="balance")
async def show_balance(ctx):
    try:
        coins = balance(ctx.author.id)
        await ctx.send(
            f"🪙 {ctx.author.display_name}: {coins:,} coins"
        )
    except sqlite3.Error as error:
        print(f"BALANCE DATABASE ERROR: {error!r}")
        await ctx.send(
            "Couldn't read your balance just now. Please try again."
        )


# ---------------- GAMBLE ----------------

@bot.command()
async def gamble(ctx, amount: int):
    user_id = ctx.author.id

    if amount <= 0:
        await ctx.send("Enter a positive amount, e.g. `!gamble 100`.")
        return

    try:
        coins = balance(user_id)

        if amount > coins:
            await ctx.send(f"You only have {coins:,} coins.")
            return

        if random.random() < 0.45:
            add_coins(user_id, amount)
            result = f"won {amount:,} coins!"
        else:
            add_coins(user_id, -amount)
            result = f"lost {amount:,} coins."

        await ctx.send(
            f"🎰 {ctx.author.mention} {result} "
            f"Balance: {balance(user_id):,} coins."
        )

    except sqlite3.Error as error:
        print(f"GAMBLE DATABASE ERROR: {error!r}")
        await ctx.send("Couldn't save that gamble. Please try again.")


# ---------------- LEADERBOARD ----------------

@bot.command()
async def leaderboard(ctx):
    try:
        rows = db.execute(
            """
            SELECT user_id, coins
            FROM balances
            ORDER BY coins DESC
            LIMIT 10
            """
        ).fetchall()

        lines = []

        for rank, (user_id, coins) in enumerate(rows, start=1):
            member = ctx.guild.get_member(user_id)

            # First try to fetch their current server membership.
            if member is None:
                try:
                    member = await ctx.guild.fetch_member(user_id)
                except (
                    discord.NotFound,
                    discord.Forbidden,
                    discord.HTTPException
                ):
                    member = None

            # If they are not in the server, try their global username.
            if member is not None:
                name = member.display_name
            else:
                try:
                    user = await bot.fetch_user(user_id)
                    name = user.name
                except (
                    discord.NotFound,
                    discord.Forbidden,
                    discord.HTTPException
                ):
                    name = f"Unknown player ({user_id})"

            medal = {
                1: "🥇",
                2: "🥈",
                3: "🥉"
            }.get(rank, f"**{rank}.**")

            lines.append(
                f"{medal} **{discord.utils.escape_markdown(name)}** "
                f"— 🪙 {coins:,} coins"
            )

        text = "\n".join(lines) if lines else "No scores yet."
        await ctx.send(f"🏆 **StreamCoins Leaderboard**\n{text}")

    except sqlite3.Error as error:
        print(f"LEADERBOARD DATABASE ERROR: {error!r}")
        await ctx.send("Couldn't load the leaderboard just now.")


# ---------------- GIVE COINS ----------------

@bot.command()
@commands.has_permissions(manage_guild=True)
async def givecoins(ctx, member: discord.Member, amount: int):
    if amount <= 0:
        await ctx.send("The amount must be positive.")
        return

    try:
        add_coins(member.id, amount)
        await ctx.send(
            f"🪙 Gave {amount:,} coins to {member.mention}."
        )
    except sqlite3.Error as error:
        print(f"GIVECOINS DATABASE ERROR: {error!r}")
        await ctx.send("Couldn't save those coins. Please try again.")


@givecoins.error
async def givecoins_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need Manage Server permission.")
    else:
        await ctx.send("Usage: `!givecoins @member 500`")


# ---------------- START RAFFLE ----------------

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

    raffle_id = uuid.uuid4().hex

    active_raffle = {
        "id": raffle_id,
        "prize": prize,
        "entrants": set(),
        "channel": ctx.channel,
    }

    await ctx.send(
        f"🎟️ **RAFFLE STARTED!**\n"
        f"Prize pool: 🪙 {prize:,} coins\n"
        f"Two random bonus winners will each receive 30% "
        f"of the prize pool.\n"
        f"The remaining coins will be shared equally among "
        f"all other entrants.\n"
        f"Type `!join` within {RAFFLE_SECONDS} seconds "
        f"to enter for free!"
    )

    await asyncio.sleep(RAFFLE_SECONDS)

    raffle_data = active_raffle

    if raffle_data is None or raffle_data["id"] != raffle_id:
        return

    entrants = list(raffle_data["entrants"])

    if not entrants:
        active_raffle = None
        await ctx.channel.send(
            "🎟️ Raffle ended with no entrants. No coins awarded."
        )
        return

    payouts = {}

    if len(entrants) < 3:
        # With one or two entrants, split the entire prize.
        each = prize // len(entrants)
        remainder = prize % len(entrants)

        for index, user_id in enumerate(entrants):
            payouts[user_id] = each + (1 if index < remainder else 0)

    else:
        bonus_winners = random.sample(entrants, 2)
        other_entrants = [
            user_id for user_id in entrants
            if user_id not in bonus_winners
        ]

        bonus_each = (prize * 30) // 100

        for user_id in bonus_winners:
            payouts[user_id] = bonus_each

        remaining_pool = prize - (bonus_each * 2)
        each_share = remaining_pool // len(other_entrants)
        remainder = remaining_pool % len(other_entrants)

        for index, user_id in enumerate(other_entrants):
            payouts[user_id] = each_share + (
                1 if index < remainder else 0
            )

    # Close the raffle before applying the payout.
    active_raffle = None

    try:
        pay_raffle_atomically(raffle_id, prize, payouts)

    except Exception as error:
        print(
            f"RAFFLE PAYOUT FAILED | raffle={raffle_id} "
            f"| prize={prize} | entrants={entrants} "
            f"| payouts={payouts} | error={error!r}"
        )

        await ctx.channel.send(
            "⚠️ The raffle payout could not be verified. "
            "Please check the FadeHost console before running "
            "another raffle. No success announcement was made."
        )
        return

    lines = ["🎉 **RAFFLE PAYOUTS SAVED SUCCESSFULLY!**"]

    for user_id, amount in payouts.items():
        member = ctx.guild.get_member(user_id)
        name = member.mention if member else f"<@{user_id}>"
        lines.append(f"{name} received 🪙 **{amount:,} coins**")

    lines.append(
        f"\nTotal distributed: 🪙 **{sum(payouts.values()):,}**"
    )
    lines.append(f"Raffle reference: `{raffle_id}`")

    await ctx.channel.send("\n".join(lines))


# ---------------- JOIN RAFFLE ----------------

@bot.command()
async def join(ctx):
    if active_raffle is None:
        await ctx.send("There isn't a raffle running right now.")
        return

    if active_raffle["channel"].id != ctx.channel.id:
        await ctx.send("This raffle is running in a different channel.")
        return

    user_id = ctx.author.id

    if user_id in active_raffle["entrants"]:
        await ctx.send("You're already entered in this raffle!")
        return

    active_raffle["entrants"].add(user_id)
    await ctx.send(
        f"🎟️ {ctx.author.mention} entered the raffle for free!"
    )


# ---------------- RUN BOT ----------------

if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN is missing. Set it in FadeHost environment variables."
    )

bot.run(TOKEN)
