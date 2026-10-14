
import os
import json
import math
import random
import sqlite3
import time
import uuid
import asyncio
from datetime import datetime, timezone

import discord
from discord.ext import commands

# ============================================================
# CONFIGURATION
# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN")

COINS_CHANNEL_ID = 1559575575239069737
MODERATOR_ROLE = "Stream Mod"

STARTING_COINS = 100
CHAT_REWARD = 5
CHAT_COOLDOWN = 60
CHAT_XP = 5
RAFFLE_SECONDS = 60
DAILY_BONUS = 1000
WHEEL_COOLDOWN = 24 * 60 * 60

# Virtual, cosmetic-only shop items.
SHOP_ITEMS = {
    "gold_badge": {
        "name": "Golden Badge",
        "price": 5000,
        "description": "A collectible virtual badge."
    },
    "lucky_charm": {
        "name": "Lucky Charm",
        "price": 7500,
        "description": "A collectible virtual lucky charm."
    },
    "trophy": {
        "name": "Champion Trophy",
        "price": 15000,
        "description": "A collectible virtual trophy."
    },
}

# ============================================================
# DATABASE
# ============================================================

# IMPORTANT: keep using the existing streamcoins.db.
# Never delete this file to update the bot.
db = sqlite3.connect("streamcoins.db", timeout=30)
db.execute("PRAGMA busy_timeout = 30000")
db.execute("PRAGMA foreign_keys = ON")

db.execute("""
CREATE TABLE IF NOT EXISTS balances (
    user_id INTEGER PRIMARY KEY,
    coins INTEGER NOT NULL DEFAULT 100
)
""")

# Existing raffle payout records are preserved.
db.execute("""
CREATE TABLE IF NOT EXISTS raffle_payouts (
    raffle_id TEXT PRIMARY KEY,
    prize INTEGER NOT NULL,
    payouts TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS active_raffles (
    raffle_id TEXT PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    prize INTEGER NOT NULL,
    end_time REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS raffle_entries (
    raffle_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (raffle_id, user_id)
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    action TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS daily_claims (
    user_id INTEGER PRIMARY KEY,
    claimed_at REAL NOT NULL
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS wheel_claims (
    user_id INTEGER PRIMARY KEY,
    claimed_at REAL NOT NULL
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS player_xp (
    user_id INTEGER PRIMARY KEY,
    xp INTEGER NOT NULL DEFAULT 0
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS inventory (
    user_id INTEGER NOT NULL,
    item_id TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, item_id)
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS weekly_earnings (
    week_start TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    coins INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (week_start, user_id)
)
""")

db.commit()


def current_week():
    now = datetime.now(timezone.utc).date()
    monday = now.fromordinal(now.toordinal() - now.weekday())
    return monday.isoformat()


def log_audit(user_id, amount, action, details=""):
    db.execute(
        """
        INSERT INTO audit_log (user_id, amount, action, details)
        VALUES (?, ?, ?, ?)
        """,
        (user_id, amount, action, details)
    )


def record_weekly_earning(user_id, amount):
    if amount <= 0:
        return

    db.execute(
        """
        INSERT INTO weekly_earnings (week_start, user_id, coins)
        VALUES (?, ?, ?)
        ON CONFLICT(week_start, user_id)
        DO UPDATE SET coins = coins + excluded.coins
        """,
        (current_week(), user_id, amount)
    )


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


def add_coins(user_id, amount, action="adjustment", details=""):
    # Ensure existing users retain their existing balances.
    balance(user_id)

    with db:
        db.execute(
            "UPDATE balances SET coins = coins + ? WHERE user_id = ?",
            (amount, user_id)
        )
        log_audit(user_id, amount, action, details)

        # Positive rewards count towards weekly champions.
        if amount > 0:
            record_weekly_earning(user_id, amount)


def level_from_xp(xp):
    # 100 XP for level 1, 400 XP for level 2, etc.
    return int(math.sqrt(max(0, xp) / 100))


def add_xp(user_id, amount):
    with db:
        db.execute(
            """
            INSERT INTO player_xp (user_id, xp)
            VALUES (?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET xp = xp + excluded.xp
            """,
            (user_id, amount)
        )


# ============================================================
# ATOMIC RAFFLE PAYOUT
# ============================================================

def pay_raffle_atomically(raffle_id, prize, payouts):
    if sum(payouts.values()) != prize:
        raise ValueError(
            f"Payout total {sum(payouts.values())} "
            f"does not equal prize {prize}"
        )

    payout_json = json.dumps({
        str(user_id): amount
        for user_id, amount in payouts.items()
    })

    db.execute("BEGIN IMMEDIATE")

    try:
        # Unique ID prevents paying the same raffle twice.
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
                UPDATE balances SET coins = coins + ?
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
                    f"Balance verification failed for {user_id}"
                )

            log_audit(
                user_id,
                amount,
                "raffle_payout",
                f"Raffle {raffle_id}"
            )
            record_weekly_earning(user_id, amount)

            print(
                f"RAFFLE PAYOUT CHECK | raffle={raffle_id} "
                f"| user={user_id} | before={before} "
                f"| added={amount} | after={after}"
            )

        db.execute(
            """
            UPDATE active_raffles
            SET status = 'completed'
            WHERE raffle_id = ?
            """,
            (raffle_id,)
        )

        db.commit()

    except Exception:
        db.rollback()
        raise

    # Read saved balances again after commit.
    for user_id, amount in payouts.items():
        saved = db.execute(
            "SELECT coins FROM balances WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if saved is None:
            raise RuntimeError(
                f"Missing balance after commit for {user_id}"
            )

        print(
            f"RAFFLE PAYOUT SAVED | raffle={raffle_id} "
            f"| user={user_id} | added={amount} "
            f"| saved_balance={saved[0]}"
        )

    print(
        f"RAFFLE COMPLETE | raffle={raffle_id} "
        f"| total_paid={sum(payouts.values())}"
    )


# ============================================================
# DISCORD SETUP
# ============================================================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

last_chat_reward = {}
last_chat_xp = {}

active_raffle = None
raffle_task = None


@bot.event
async def on_ready():
    global active_raffle, raffle_task

    print(f"StreamCoins online as {bot.user}")
    print(f"Database: {os.path.abspath('streamcoins.db')}")
    print(f"Commands channel: {COINS_CHANNEL_ID}")

    # Resume a saved raffle after a bot restart.
    if active_raffle is None:
        row = db.execute(
            """
            SELECT raffle_id, guild_id, channel_id, prize, end_time
            FROM active_raffles
            WHERE status = 'active'
            ORDER BY end_time ASC
            LIMIT 1
            """
        ).fetchone()

        if row:
            raffle_id, guild_id, channel_id, prize, end_time = row
            active_raffle = {
                "id": raffle_id,
                "guild_id": guild_id,
                "channel_id": channel_id,
                "prize": prize,
                "end_time": end_time,
            }

            if raffle_task is None or raffle_task.done():
                raffle_task = asyncio.create_task(
                    finish_raffle(raffle_id)
                )

            print(f"Recovered active raffle {raffle_id}")


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


# ============================================================
# CHAT REWARDS AND XP
# ============================================================

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    user_id = message.author.id
    now = time.time()

    # Coin reward: once every 60 seconds per user.
    if now - last_chat_reward.get(user_id, 0) >= CHAT_COOLDOWN:
        try:
            add_coins(
                user_id,
                CHAT_REWARD,
                "chat_reward",
                "Chat activity reward"
            )
            last_chat_reward[user_id] = now
        except sqlite3.Error as error:
            print(f"CHAT REWARD ERROR: {error!r}")

    # XP reward also has a cooldown to discourage spam farming.
    if now - last_chat_xp.get(user_id, 0) >= CHAT_COOLDOWN:
        try:
            add_xp(user_id, CHAT_XP)
            last_chat_xp[user_id] = now
        except sqlite3.Error as error:
            print(f"CHAT XP ERROR: {error!r}")

    await bot.process_commands(message)


# ============================================================
# BALANCE
# ============================================================

@bot.command(name="balance")
async def show_balance(ctx):
    try:
        coins = balance(ctx.author.id)
        await ctx.send(
            f"🪙 {ctx.author.display_name}: **{coins:,} coins**"
        )
    except sqlite3.Error as error:
        print(f"BALANCE ERROR: {error!r}")
        await ctx.send("Couldn't read your balance. Please try again.")


# ============================================================
# STANDARD GAMBLE
# ============================================================

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
            add_coins(user_id, amount, "gamble_win", "Won a gamble")
            result = f"won {amount:,} coins!"
        else:
            add_coins(user_id, -amount, "gamble_loss", "Lost a gamble")
            result = f"lost {amount:,} coins."

        await ctx.send(
            f"🎲 {ctx.author.mention} {result} "
            f"Balance: {balance(user_id):,} coins."
        )
    except sqlite3.Error as error:
        print(f"GAMBLE ERROR: {error!r}")
        await ctx.send("Couldn't save that gamble. Please try again.")


# ============================================================
# JACKPOT SLOTS
# ============================================================

@bot.command()
async def slots(ctx, amount: int):
    user_id = ctx.author.id

    if amount <= 0:
        await ctx.send("Enter a positive stake, e.g. `!slots 500`.")
        return

    try:
        coins = balance(user_id)

        if amount > coins:
            await ctx.send(f"You only have {coins:,} coins.")
            return

        # Weighted outcomes: jackpot, triple, pair, or miss.
        outcome = random.choices(
            ["jackpot", "triple", "pair", "miss"],
            weights=[1, 5, 24, 70],
            k=1
        )[0]

        if outcome == "jackpot":
            payout = amount * 10
            symbols = "💎 💎 💎"
        elif outcome == "triple":
            payout = amount * 5
            symbols = "7️⃣ 7️⃣ 7️⃣"
        elif outcome == "pair":
            payout = (amount * 3) // 2
            symbols = "🍒 🍒 🍋"
        else:
            payout = 0
            symbols = "🍋 🍊 🍒"

        # Net balance change: subtract stake, then credit any winnings.
        net = payout - amount

        add_coins(
            user_id,
            net,
            "slots",
            f"Symbols {symbols}; stake {amount}; payout {payout}"
        )

        if payout > amount:
            result = f"You won **{payout:,} coins**!"
        elif payout == amount:
            result = "You got your stake back!"
        else:
            result = f"You lost **{amount:,} coins**."

        await ctx.send(
            f"🎰 **{symbols}**\n{ctx.author.mention}, {result}\n"
            f"Balance: 🪙 {balance(user_id):,}"
        )

    except sqlite3.Error as error:
        print(f"SLOTS ERROR: {error!r}")
        await ctx.send("Couldn't save the slot result. Please try again.")


# ============================================================
# DAILY BONUS
# ============================================================

@bot.command()
async def daily(ctx):
    user_id = ctx.author.id
    now = time.time()

    row = db.execute(
        "SELECT claimed_at FROM daily_claims WHERE user_id = ?",
        (user_id,)
    ).fetchone()

    if row and now - row[0] < 86400:
        remaining = int(86400 - (now - row[0]))
        hours, remainder = divmod(remaining, 3600)
        minutes = remainder // 60

        await ctx.send(
            f"🎁 You've already claimed your daily bonus! "
            f"Come back in {hours}h {minutes}m."
        )
        return

    db.execute("BEGIN IMMEDIATE")
    try:
        # Recheck within the transaction to prevent duplicate claims.
        row = db.execute(
            "SELECT claimed_at FROM daily_claims WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if row and now - row[0] < 86400:
            db.rollback()
            await ctx.send("You've already claimed your daily bonus.")
            return

        db.execute(
            """
            INSERT INTO daily_claims (user_id, claimed_at)
            VALUES (?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET claimed_at = excluded.claimed_at
            """,
            (user_id, now)
        )

        db.execute(
            """
            INSERT OR IGNORE INTO balances (user_id, coins)
            VALUES (?, ?)
            """,
            (user_id, STARTING_COINS)
        )

        db.execute(
            "UPDATE balances SET coins = coins + ? WHERE user_id = ?",
            (DAILY_BONUS, user_id)
        )

        log_audit(user_id, DAILY_BONUS, "daily_bonus")
        record_weekly_earning(user_id, DAILY_BONUS)
        db.commit()

    except Exception as error:
        db.rollback()
        print(f"DAILY BONUS ERROR: {error!r}")
        await ctx.send("Couldn't save your daily bonus. Please try again.")
        return

    await ctx.send(
        f"🎁 {ctx.author.mention} claimed **{DAILY_BONUS:,} coins**!\n"
        f"Balance: 🪙 {balance(user_id):,}"
    )


# ============================================================
# LUCKY WHEEL
# ============================================================

@bot.command()
async def spin(ctx):
    user_id = ctx.author.id
    now = time.time()

    row = db.execute(
        "SELECT claimed_at FROM wheel_claims WHERE user_id = ?",
        (user_id,)
    ).fetchone()

    if row and now - row[0] < WHEEL_COOLDOWN:
        remaining = int(WHEEL_COOLDOWN - (now - row[0]))
        hours, remainder = divmod(remaining, 3600)
        minutes = remainder // 60

        await ctx.send(
            f"🎡 Your wheel is recharging! Try again in {hours}h {minutes}m."
        )
        return

    prize = random.choices(
        [100, 250, 500, 1000, 2000, 5000],
        weights=[30, 25, 20, 15, 8, 2],
        k=1
    )[0]

    db.execute("BEGIN IMMEDIATE")
    try:
        # Recheck the cooldown within the transaction.
        row = db.execute(
            "SELECT claimed_at FROM wheel_claims WHERE user_id = ?",
            (user_id,)
        ).fetchone()

        if row and now - row[0] < WHEEL_COOLDOWN:
            db.rollback()
            await ctx.send("Your wheel is still recharging.")
            return

        db.execute(
            """
            INSERT INTO wheel_claims (user_id, claimed_at)
            VALUES (?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET claimed_at = excluded.claimed_at
            """,
            (user_id, now)
        )

        db.execute(
            """
            INSERT OR IGNORE INTO balances (user_id, coins)
            VALUES (?, ?)
            """,
            (user_id, STARTING_COINS)
        )

        db.execute(
            "UPDATE balances SET coins = coins + ? WHERE user_id = ?",
            (prize, user_id)
        )

        log_audit(user_id, prize, "lucky_wheel")
        record_weekly_earning(user_id, prize)
        db.commit()

    except Exception as error:
        db.rollback()
        print(f"WHEEL ERROR: {error!r}")
        await ctx.send("Couldn't save your spin. Please try again.")
        return

    await ctx.send(
        f"🎡 **The wheel landed on {prize:,}!**\n"
        f"{ctx.author.mention} won 🪙 **{prize:,} coins**!\n"
        f"Balance: {balance(user_id):,}"
    )


# ============================================================
# LEVELS AND VIP RANKS
# ============================================================

def vip_rank(level):
    if level >= 25:
        return "💎 Diamond VIP"
    if level >= 15:
        return "👑 Gold VIP"
    if level >= 8:
        return "🥈 Silver VIP"
    if level >= 3:
        return "🥉 Bronze VIP"
    return "🌱 Newcomer"


@bot.command()
async def level(ctx, member: discord.Member = None):
    member = member or ctx.author

    row = db.execute(
        "SELECT xp FROM player_xp WHERE user_id = ?",
        (member.id,)
    ).fetchone()

    xp = row[0] if row else 0
    current_level = level_from_xp(xp)
    next_level_xp = (current_level + 1) ** 2 * 100
    remaining = max(0, next_level_xp - xp)

    await ctx.send(
        f"👑 **{member.display_name}'s profile**\n"
        f"Rank: {vip_rank(current_level)}\n"
        f"Level: **{current_level}**\n"
        f"XP: **{xp:,}**\n"
        f"XP to next level: **{remaining:,}**"
    )


# ============================================================
# LEADERBOARD
# ============================================================

@bot.command()
async def leaderboard(ctx):
    rows = db.execute(
        """
        SELECT user_id, coins FROM balances
        ORDER BY coins DESC LIMIT 10
        """
    ).fetchall()

    lines = []

    for rank, (user_id, coins) in enumerate(rows, start=1):
        member = ctx.guild.get_member(user_id)

        if member is None:
            try:
                member = await ctx.guild.fetch_member(user_id)
            except (
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException
            ):
                member = None

        if member:
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

        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"**{rank}.**")
        lines.append(
            f"{medal} **{discord.utils.escape_markdown(name)}** "
            f"— 🪙 {coins:,}"
        )

    await ctx.send(
        "🏆 **StreamCoins Leaderboard**\n" +
        ("\n".join(lines) if lines else "No scores yet.")
    )


# ============================================================
# WEEKLY CHAMPIONS
# ============================================================

@bot.command()
async def weekly(ctx):
    rows = db.execute(
        """
        SELECT user_id, coins FROM weekly_earnings
        WHERE week_start = ?
        ORDER BY coins DESC LIMIT 10
        """,
        (current_week(),)
    ).fetchall()

    if not rows:
        await ctx.send("🏆 No weekly earnings recorded yet.")
        return

    lines = []

    for rank, (user_id, coins) in enumerate(rows, start=1):
        member = ctx.guild.get_member(user_id)

        if member:
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

        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, f"**{rank}.**")
        lines.append(f"{medal} **{name}** — 🪙 {coins:,} earned")

    await ctx.send(
        f"🏆 **Weekly Champions** — week starting {current_week()}\n" +
        "\n".join(lines)
    )


# ============================================================
# COIN SHOP AND INVENTORY
# ============================================================

@bot.command()
async def shop(ctx):
    lines = ["🛍️ **StreamCoins Shop**"]

    for item_id, item in SHOP_ITEMS.items():
        lines.append(
            f"`{item_id}` — **{item['name']}**: "
            f"🪙 {item['price']:,}\n"
            f"  {item['description']}"
        )

    lines.append("Buy an item with `!buy item_id`.")
    await ctx.send("\n".join(lines))


@bot.command()
async def buy(ctx, item_id: str):
    item_id = item_id.lower()
    item = SHOP_ITEMS.get(item_id)

    if item is None:
        await ctx.send("That item isn't in the shop. Use `!shop` to see items.")
        return

    user_id = ctx.author.id
    price = item["price"]

    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute(
            """
            INSERT OR IGNORE INTO balances (user_id, coins)
            VALUES (?, ?)
            """,
            (user_id, STARTING_COINS)
        )

        coins = db.execute(
            "SELECT coins FROM balances WHERE user_id = ?",
            (user_id,)
        ).fetchone()[0]

        if coins < price:
            db.rollback()
            await ctx.send(
                f"You need {price:,} coins, but you only have {coins:,}."
            )
            return

        db.execute(
            "UPDATE balances SET coins = coins - ? WHERE user_id = ?",
            (price, user_id)
        )

        db.execute(
            """
            INSERT INTO inventory (user_id, item_id, quantity)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, item_id)
            DO UPDATE SET quantity = quantity + 1
            """,
            (user_id, item_id)
        )

        log_audit(user_id, -price, "shop_purchase", item_id)
        db.commit()

    except Exception as error:
        db.rollback()
        print(f"SHOP ERROR: {error!r}")
        await ctx.send("Purchase failed. Please try again.")
        return

    await ctx.send(
        f"🛍️ {ctx.author.mention} purchased **{item['name']}**!\n"
        f"Balance: 🪙 {balance(user_id):,}"
    )


@bot.command()
async def inventory(ctx, member: discord.Member = None):
    member = member or ctx.author

    rows = db.execute(
        """
        SELECT item_id, quantity FROM inventory
        WHERE user_id = ? AND quantity > 0
        ORDER BY item_id
        """,
        (member.id,)
    ).fetchall()

    if not rows:
        await ctx.send(f"🎒 {member.display_name} has no shop items yet.")
        return

    lines = ["🎒 **Inventory**"]

    for item_id, quantity in rows:
        item = SHOP_ITEMS.get(item_id)
        name = item["name"] if item else item_id
        lines.append(f"• {name} × {quantity}")

    await ctx.send("\n".join(lines))


# ============================================================
# PLAYER-TO-PLAYER TRANSFERS
# ============================================================

@bot.command()
async def pay(ctx, member: discord.Member, amount: int):
    sender_id = ctx.author.id
    receiver_id = member.id

    if receiver_id == sender_id:
        await ctx.send("You can't send coins to yourself.")
        return

    if member.bot:
        await ctx.send("You can't send coins to a bot.")
        return

    if amount <= 0:
        await ctx.send("Enter a positive amount, e.g. `!pay @member 500`.")
        return

    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute(
            """
            INSERT OR IGNORE INTO balances (user_id, coins)
            VALUES (?, ?)
            """,
            (sender_id, STARTING_COINS)
        )
        db.execute(
            """
            INSERT OR IGNORE INTO balances (user_id, coins)
            VALUES (?, ?)
            """,
            (receiver_id, STARTING_COINS)
        )

        sender_balance = db.execute(
            "SELECT coins FROM balances WHERE user_id = ?",
            (sender_id,)
        ).fetchone()[0]

        if sender_balance < amount:
            db.rollback()
            await ctx.send(
                f"You only have {sender_balance:,} coins."
            )
            return

        db.execute(
            "UPDATE balances SET coins = coins - ? WHERE user_id = ?",
            (amount, sender_id)
        )
        db.execute(
            "UPDATE balances SET coins = coins + ? WHERE user_id = ?",
            (amount, receiver_id)
        )

        log_audit(sender_id, -amount, "transfer_sent", f"To {receiver_id}")
        log_audit(receiver_id, amount, "transfer_received", f"From {sender_id}")
        db.commit()

    except Exception as error:
        db.rollback()
        print(f"TRANSFER ERROR: {error!r}")
        await ctx.send("Transfer failed. Please try again.")
        return

    await ctx.send(
        f"💸 {ctx.author.mention} sent {member.mention} "
        f"🪙 **{amount:,} coins**!"
    )


# ============================================================
# ADMIN COIN GRANTS
# ============================================================

@bot.command()
@commands.has_permissions(manage_guild=True)
async def givecoins(ctx, member: discord.Member, amount: int):
    if amount <= 0:
        await ctx.send("The amount must be positive.")
        return

    try:
        add_coins(
            member.id,
            amount,
            "admin_grant",
            f"Granted by {ctx.author.id}"
        )
        await ctx.send(
            f"🪙 Gave {amount:,} coins to {member.mention}."
        )
    except sqlite3.Error as error:
        print(f"GIVECOINS ERROR: {error!r}")
        await ctx.send("Couldn't save those coins. Please try again.")


@givecoins.error
async def givecoins_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need Manage Server permission.")
    else:
        await ctx.send("Usage: `!givecoins @member 500`")


# ============================================================
# AUDIT LOG
# ============================================================

@bot.command()
@commands.has_permissions(manage_guild=True)
async def audit(ctx, member: discord.Member = None):
    if member:
        rows = db.execute(
            """
            SELECT user_id, amount, action, details, created_at
            FROM audit_log WHERE user_id = ?
            ORDER BY id DESC LIMIT 10
            """,
            (member.id,)
        ).fetchall()
    else:
        rows = db.execute(
            """
            SELECT user_id, amount, action, details, created_at
            FROM audit_log ORDER BY id DESC LIMIT 10
            """
        ).fetchall()

    if not rows:
        await ctx.send("No audit entries found.")
        return

    lines = ["🛡️ **Recent coin activity**"]

    for user_id, amount, action, details, created_at in rows:
        lines.append(
            f"User `{user_id}` | {amount:+,} | `{action}` "
            f"| {details[:60]} | {created_at}"
        )

    # Discord messages have a character limit.
    await ctx.send("\n".join(lines)[:1900])


# ============================================================
# RAFFLE COMMAND
# ============================================================

async def finish_raffle(raffle_id):
    global active_raffle, raffle_task

    row = db.execute(
        """
        SELECT guild_id, channel_id, prize, end_time
        FROM active_raffles
        WHERE raffle_id = ? AND status = 'active'
        """,
        (raffle_id,)
    ).fetchone()

    if not row:
        return

    guild_id, channel_id, prize, end_time = row
    await asyncio.sleep(max(0, end_time - time.time()))

    row = db.execute(
        """
        SELECT guild_id, channel_id, prize
        FROM active_raffles
        WHERE raffle_id = ? AND status = 'active'
        """,
        (raffle_id,)
    ).fetchone()

    if not row:
        return

    entrants = [
        entry[0]
        for entry in db.execute(
            "SELECT user_id FROM raffle_entries WHERE raffle_id = ?",
            (raffle_id,)
        ).fetchall()
    ]

    channel = bot.get_channel(channel_id)

    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            print(f"RAFFLE CHANNEL UNAVAILABLE: {channel_id}")
            return

    if not entrants:
        with db:
            db.execute(
                """
                UPDATE active_raffles SET status = 'no_entries'
                WHERE raffle_id = ?
                """,
                (raffle_id,)
            )

        active_raffle = None
        await channel.send(
            "🎟️ Raffle ended with no entrants. No coins awarded."
        )
        return

    payouts = {}

    if len(entrants) < 3:
        each = prize // len(entrants)
        remainder = prize % len(entrants)

        for index, user_id in enumerate(entrants):
            payouts[user_id] = each + (1 if index < remainder else 0)

    else:
        bonus_winners = random.sample(entrants, 2)
        others = [
            user_id for user_id in entrants
            if user_id not in bonus_winners
        ]

        bonus_each = (prize * 30) // 100

        for user_id in bonus_winners:
            payouts[user_id] = bonus_each

        remaining_pool = prize - (bonus_each * 2)
        each = remaining_pool // len(others)
        remainder = remaining_pool % len(others)

        for index, user_id in enumerate(others):
            payouts[user_id] = each + (1 if index < remainder else 0)

    try:
        pay_raffle_atomically(raffle_id, prize, payouts)

    except Exception as error:
        print(
            f"RAFFLE PAYOUT FAILED | raffle={raffle_id} "
            f"| prize={prize} | entrants={entrants} "
            f"| payouts={payouts} | error={error!r}"
        )

        await channel.send(
            "⚠️ Raffle payout could not be confirmed. "
            "Please check the FadeHost console before running another raffle."
        )
        return

    active_raffle = None
    raffle_task = None

    lines = ["🎉 **RAFFLE PAYOUTS SAVED SUCCESSFULLY!**"]

    for user_id, amount in payouts.items():
        lines.append(f"<@{user_id}> received 🪙 **{amount:,} coins**")

    lines.append(f"\nTotal distributed: 🪙 **{sum(payouts.values()):,}**")
    lines.append(f"Raffle reference: `{raffle_id}`")

    await channel.send("\n".join(lines))


@bot.command()
@commands.has_role(MODERATOR_ROLE)
async def raffle(ctx, prize: int):
    global active_raffle, raffle_task

    if active_raffle is not None:
        await ctx.send("A raffle is already running!")
        return

    if prize <= 0:
        await ctx.send("The prize must be more than 0 coins.")
        return

    raffle_id = uuid.uuid4().hex
    end_time = time.time() + RAFFLE_SECONDS

    try:
        with db:
            db.execute(
                """
                INSERT INTO active_raffles
                (raffle_id, guild_id, channel_id, prize, end_time, status)
                VALUES (?, ?, ?, ?, ?, 'active')
                """,
                (
                    raffle_id,
                    ctx.guild.id,
                    ctx.channel.id,
                    prize,
                    end_time
                )
            )
    except sqlite3.Error as error:
        print(f"RAFFLE START ERROR: {error!r}")
        await ctx.send("Couldn't save the raffle. Please try again.")
        return

    active_raffle = {
        "id": raffle_id,
        "guild_id": ctx.guild.id,
        "channel_id": ctx.channel.id,
        "prize": prize,
        "end_time": end_time,
    }

    await ctx.send(
        f"🎟️ **RAFFLE STARTED!**\n"
        f"Prize pool: 🪙 {prize:,} coins\n"
        f"Two random bonus winners will each receive 30% of the prize.\n"
        f"The remaining 40% will be shared among all other entrants.\n"
        f"With fewer than 3 entrants, the full prize is shared equally.\n"
        f"Type `!join` within {RAFFLE_SECONDS} seconds to enter for free!"
    )

    raffle_task = asyncio.create_task(finish_raffle(raffle_id))


@bot.command()
async def join(ctx):
    if active_raffle is None:
        await ctx.send("There isn't a raffle running right now.")
        return

    if active_raffle["channel_id"] != ctx.channel.id:
        await ctx.send("This raffle is running in a different channel.")
        return

    try:
        with db:
            db.execute(
                """
                INSERT INTO raffle_entries (raffle_id, user_id)
                VALUES (?, ?)
                """,
                (active_raffle["id"], ctx.author.id)
            )
    except sqlite3.IntegrityError:
        await ctx.send("You're already entered in this raffle!")
        return
    except sqlite3.Error as error:
        print(f"RAFFLE JOIN ERROR: {error!r}")
        await ctx.send("Couldn't save your entry. Please try again.")
        return

    await ctx.send(
        f"🎟️ {ctx.author.mention} entered the raffle for free!"
    )


# ============================================================
# RUN
# ============================================================

if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN is missing. Set it in FadeHost environment variables."
    )

bot.run(TOKEN)
