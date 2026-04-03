import os
import time
import random
import asyncio
import discord
from discord.ext import commands

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
PREFIX = "!"
COOLDOWN_SECONDS = 10.0
BAR_LENGTH = 20

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)


def create_game_state(machine_name: str) -> dict:
    return {
        "machine_name": machine_name,
        "progress": 0.0,
        "finished": False,
    }


main_game = create_game_state("Main Machine")
test_game = create_game_state("Test Machine")

cooldowns = {
    "main": {},
    "test": {},
}

# resets with reset commands
round_leaderboard = {}

# persists while bot process is running
all_time_leaderboard = {}

# each tier uses: chance, min, max
main_rates = {
    "tier1": {"chance": 95.0, "min": 0.1, "max": 0.1},
    "tier2": {"chance": 4.8, "min": 0.2, "max": 1.5},
    "tier3": {"chance": 0.2, "min": 1.5, "max": 3.0},
}

test_rates = {
    "tier1": {"chance": 34.0, "min": 15.0, "max": 20.0},
    "tier2": {"chance": 33.0, "min": 10.0, "max": 15.0},
    "tier3": {"chance": 33.0, "min": 1.0, "max": 9.0},
}


def make_progress_bar(progress: float) -> str:
    progress = max(0.0, min(100.0, progress))
    filled = int((progress / 100.0) * BAR_LENGTH)
    return "█" * filled + "░" * (BAR_LENGTH - filled)


def format_progress(progress: float) -> str:
    return f"[{make_progress_bar(progress)}] {progress:.1f}%"


def validate_rates(rates: dict) -> None:
    total = sum(t["chance"] for t in rates.values())
    if abs(total - 100.0) > 0.001:
        raise ValueError(total)

    for t in rates.values():
        if t["chance"] < 0 or t["min"] < 0 or t["max"] < 0:
            raise ValueError("Rates cannot be negative.")
        if t["min"] > t["max"]:
            raise ValueError("Min cannot be greater than max.")


def format_rates_table(name: str, rates: dict) -> str:
    def fmt(tier: dict) -> str:
        if tier["min"] == tier["max"]:
            return f'{tier["chance"]:.1f}% → {tier["min"]}'
        return f'{tier["chance"]:.1f}% → {tier["min"]}-{tier["max"]}'

    total = sum(t["chance"] for t in rates.values())

    return (
        "```text\n"
        f"{name} Rates\n"
        "────────────────────────\n"
        f"Tier 1: {fmt(rates['tier1'])}\n"
        f"Tier 2: {fmt(rates['tier2'])}\n"
        f"Tier 3: {fmt(rates['tier3'])}\n"
        "────────────────────────\n"
        f"Total: {total:.1f}%\n"
        "```"
    )


def add_leaderboard_entry(user: discord.abc.User) -> None:
    uid = user.id

    if uid not in round_leaderboard:
        round_leaderboard[uid] = {"name": user.display_name, "count": 0}

    if uid not in all_time_leaderboard:
        all_time_leaderboard[uid] = {"name": user.display_name, "count": 0}

    round_leaderboard[uid]["name"] = user.display_name
    all_time_leaderboard[uid]["name"] = user.display_name

    round_leaderboard[uid]["count"] += 1
    all_time_leaderboard[uid]["count"] += 1


def roll_from_rates(rates: dict, texts: list[str]) -> tuple[float, str]:
    r = random.uniform(0, 100)

    if r < rates["tier1"]["chance"]:
        tier = rates["tier1"]
        text = texts[0]
    elif r < rates["tier1"]["chance"] + rates["tier2"]["chance"]:
        tier = rates["tier2"]
        text = texts[1]
    else:
        tier = rates["tier3"]
        text = texts[2]

    amount = round(random.uniform(tier["min"], tier["max"]), 1)
    return amount, text


async def handle_push(ctx, game: dict, mode: str, rates: dict, texts: list[str]):
    global COOLDOWN_SECONDS

    if game["finished"]:
        await ctx.send(
            "You look at the machine and see that the jackpot has been claimed.\n"
            "You lose your motivation to play."
        )
        return

    now = time.time()
    last_used = cooldowns[mode].get(ctx.author.id, 0)

    if now - last_used < COOLDOWN_SECONDS:
        remaining = COOLDOWN_SECONDS - (now - last_used)
        await ctx.send(
            f"Cooldown is **{COOLDOWN_SECONDS:g} seconds**.\n"
            f"You can play again in **{remaining:.1f}s**."
        )
        return

    cooldowns[mode][ctx.author.id] = now
    add_leaderboard_entry(ctx.author)

    old_progress = game["progress"]
    amount, text = roll_from_rates(rates, texts)

    game["progress"] += amount
    if game["progress"] >= 100:
        game["progress"] = 100
        game["finished"] = True

    msg = await ctx.send(f"You play...\n\n{format_progress(old_progress)}")
    await asyncio.sleep(0.6)

    if game["finished"]:
        await msg.edit(
            content=(
                f"{text}\n\n"
                f"{format_progress(game['progress'])}\n\n"
                f"🎉 JACKPOT 🎉\n"
                f"{ctx.author.mention} hit the jackpot!\n"
                f"Visit the prize counter."
            )
        )
        return

    await msg.edit(content=f"{text}\n\n{format_progress(game['progress'])}")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Bot is ready.")


@bot.command()
async def push(ctx):
    await handle_push(
        ctx,
        main_game,
        "main",
        main_rates,
        [
            "You play and progress is made....even though it doesn't look like it",
            "You play and you can see progress! Though its small",
            "You play and you see major progress!",
        ],
    )


@bot.command(name="push-test")
async def push_test(ctx):
    await handle_push(
        ctx,
        test_game,
        "test",
        test_rates,
        [
            "You play and you see major progress!",
            "You play and you can see progress! Though its small",
            "You play and progress is made....even though it doesn't look like it",
        ],
    )


@bot.command()
async def status(ctx):
    status_text = "JACKPOT CLAIMED" if main_game["finished"] else "ACTIVE"
    await ctx.send(
        f"**Main Machine**\n"
        f"Status: **{status_text}**\n"
        f"{format_progress(main_game['progress'])}"
    )


@bot.command(name="status-test")
async def status_test(ctx):
    status_text = "JACKPOT CLAIMED" if test_game["finished"] else "ACTIVE"
    await ctx.send(
        f"**Test Machine**\n"
        f"Status: **{status_text}**\n"
        f"{format_progress(test_game['progress'])}"
    )


@bot.command()
@commands.has_permissions(manage_guild=True)
async def reset(ctx):
    main_game["progress"] = 0
    main_game["finished"] = False
    cooldowns["main"].clear()
    round_leaderboard.clear()

    await ctx.send("Main machine reset. Current-round leaderboard reset too.")


@bot.command(name="reset-test")
@commands.has_permissions(manage_guild=True)
async def reset_test(ctx):
    test_game["progress"] = 0
    test_game["finished"] = False
    cooldowns["test"].clear()
    round_leaderboard.clear()

    await ctx.send("Test machine reset. Current-round leaderboard reset too.")


@bot.command()
async def leaderboard(ctx):
    if not round_leaderboard:
        await ctx.send("No data.")
        return

    sorted_entries = sorted(
        round_leaderboard.values(),
        key=lambda x: x["count"],
        reverse=True
    )

    lines = ["**Current Round Leaderboard**"]
    lines.extend(
        f"{i + 1}. {entry['name']} - {entry['count']}"
        for i, entry in enumerate(sorted_entries[:10])
    )
    await ctx.send("\n".join(lines))


@bot.command(name="all-time-leaderboard")
async def all_time_leaderboard_command(ctx):
    if not all_time_leaderboard:
        await ctx.send("No data.")
        return

    sorted_entries = sorted(
        all_time_leaderboard.values(),
        key=lambda x: x["count"],
        reverse=True
    )

    lines = ["**All-Time Leaderboard**"]
    lines.extend(
        f"{i + 1}. {entry['name']} - {entry['count']}"
        for i, entry in enumerate(sorted_entries[:10])
    )
    await ctx.send("\n".join(lines))


@bot.command()
async def rates(ctx):
    await ctx.send(format_rates_table("Main", main_rates))
    await ctx.send(format_rates_table("Test", test_rates))
    await ctx.send(f"**Current cooldown:** {COOLDOWN_SECONDS:g} seconds")


@bot.command(name="rates-main")
async def rates_main(ctx):
    await ctx.send(format_rates_table("Main", main_rates))


@bot.command(name="rates-test")
async def rates_test(ctx):
    await ctx.send(format_rates_table("Test", test_rates))


@bot.command(name="set-main-rates")
@commands.has_permissions(manage_guild=True)
async def set_main_rates(
    ctx,
    c1: float, min1: float, max1: float,
    c2: float, min2: float, max2: float,
    c3: float, min3: float, max3: float,
):
    new_rates = {
        "tier1": {"chance": c1, "min": min1, "max": max1},
        "tier2": {"chance": c2, "min": min2, "max": max2},
        "tier3": {"chance": c3, "min": min3, "max": max3},
    }

    try:
        validate_rates(new_rates)
    except ValueError as e:
        total = c1 + c2 + c3
        if isinstance(e.args[0], (int, float)):
            diff = total - 100
            if diff > 0:
                msg = f"❌ Rates not updated. Total = {total:.1f}% (over by {diff:.1f}%)"
            else:
                msg = f"❌ Rates not updated. Total = {total:.1f}% (short by {abs(diff):.1f}%)"
        else:
            msg = f"❌ Rates not updated. {e.args[0]}"
        await ctx.send(msg + "\n" + format_rates_table("Attempted Main", new_rates))
        return

    main_rates.update(new_rates)
    await ctx.send("✅ Updated\n" + format_rates_table("Main", main_rates))


@bot.command(name="set-test-rates")
@commands.has_permissions(manage_guild=True)
async def set_test_rates(
    ctx,
    c1: float, min1: float, max1: float,
    c2: float, min2: float, max2: float,
    c3: float, min3: float, max3: float,
):
    new_rates = {
        "tier1": {"chance": c1, "min": min1, "max": max1},
        "tier2": {"chance": c2, "min": min2, "max": max2},
        "tier3": {"chance": c3, "min": min3, "max": max3},
    }

    try:
        validate_rates(new_rates)
    except ValueError as e:
        total = c1 + c2 + c3
        if isinstance(e.args[0], (int, float)):
            diff = total - 100
            if diff > 0:
                msg = f"❌ Rates not updated. Total = {total:.1f}% (over by {diff:.1f}%)"
            else:
                msg = f"❌ Rates not updated. Total = {total:.1f}% (short by {abs(diff):.1f}%)"
        else:
            msg = f"❌ Rates not updated. {e.args[0]}"
        await ctx.send(msg + "\n" + format_rates_table("Attempted Test", new_rates))
        return

    test_rates.update(new_rates)
    await ctx.send("✅ Updated\n" + format_rates_table("Test", test_rates))


@bot.command(name="set-cooldown")
@commands.has_permissions(manage_guild=True)
async def set_cooldown(ctx, seconds: float):
    global COOLDOWN_SECONDS

    if seconds < 0:
        await ctx.send("Cooldown cannot be negative.")
        return

    COOLDOWN_SECONDS = seconds
    await ctx.send(f"✅ Cooldown updated to **{COOLDOWN_SECONDS:g} seconds**.")


@bot.command(name="set-progress")
@commands.has_permissions(manage_guild=True)
async def set_progress(ctx, value: float):
    if value < 0:
        await ctx.send("Progress cannot be negative.")
        return

    if value > 100:
        value = 100

    main_game["progress"] = value

    if value >= 100:
        main_game["progress"] = 100
        main_game["finished"] = True
        await ctx.send("🎉 Main machine forced to **100%**.\nJACKPOT is now claimed.")
        return

    main_game["finished"] = False
    await ctx.send(
        f"Main machine progress set to **{value:.1f}%**.\n"
        f"{format_progress(main_game['progress'])}"
    )


@bot.command(name="set-progress-test")
@commands.has_permissions(manage_guild=True)
async def set_progress_test(ctx, value: float):
    if value < 0:
        await ctx.send("Progress cannot be negative.")
        return

    if value > 100:
        value = 100

    test_game["progress"] = value

    if value >= 100:
        test_game["progress"] = 100
        test_game["finished"] = True
        await ctx.send("🎉 Test machine forced to **100%**.\nJACKPOT is now claimed.")
        return

    test_game["finished"] = False
    await ctx.send(
        f"Test machine progress set to **{value:.1f}%**.\n"
        f"{format_progress(test_game['progress'])}"
    )


@bot.command(name="help-commands")
async def help_commands(ctx):
    await ctx.send(
        "```text\n"
        "COIN PUSHER BOT COMMANDS\n\n"
        "GAMEPLAY\n"
        "!push\n"
        "!push-test\n\n"
        "STATUS\n"
        "!status\n"
        "!status-test\n\n"
        "RESET (ADMIN)\n"
        "!reset\n"
        "!reset-test\n\n"
        "LEADERBOARD\n"
        "!leaderboard\n"
        "!all-time-leaderboard\n\n"
        "RATES\n"
        "!rates\n"
        "!rates-main\n"
        "!rates-test\n\n"
        "SET RATES (ADMIN)\n"
        "!set-main-rates c1 min1 max1 c2 min2 max2 c3 min3 max3\n"
        "!set-test-rates c1 min1 max1 c2 min2 max2 c3 min3 max3\n\n"
        "COOLDOWN (ADMIN)\n"
        "!set-cooldown seconds\n\n"
        "SET PROGRESS (ADMIN)\n"
        "!set-progress value\n"
        "!set-progress-test value\n"
        "```"
    )


@reset.error
@reset_test.error
@set_main_rates.error
@set_test_rates.error
@set_cooldown.error
@set_progress.error
@set_progress_test.error
async def admin_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You need the **Manage Server** permission to use that command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("You are missing one or more required values for that command.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("One or more values were invalid. Make sure they are numbers.")
    else:
        raise error


if not TOKEN:
    raise ValueError("DISCORD_BOT_TOKEN not set")

bot.run(TOKEN)
Discord.py
python-dotenv
