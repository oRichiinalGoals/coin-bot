import os
import random
import time
import discord
from discord.ext import commands
import asyncio
import textwrap
import json
import sqlite3
import re
from pathlib import Path

TOKEN = os.getenv("DISCORD_TOKEN")



PREFIX = "!"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(
    command_prefix=PREFIX,
    intents=intents,
    case_insensitive=True
)

DEFAULT_PLAYER_ACTION_COOLDOWN_SECONDS = 3600
DEFAULT_SLEEP_COOLDOWN_SECONDS = 7200
DEFAULT_INTRA_ISLAND_MOVE_SECONDS = 1800

PLAYER_ACTION_COOLDOWN_SECONDS = DEFAULT_PLAYER_ACTION_COOLDOWN_SECONDS
SLEEP_COOLDOWN_SECONDS = DEFAULT_SLEEP_COOLDOWN_SECONDS
INTRA_ISLAND_MOVE_SECONDS = DEFAULT_INTRA_ISLAND_MOVE_SECONDS
PLAYER_ACTION_COOLDOWNS = {}
LOCATION_ENTRY_TIMES = {}
ACTIVE_ACTION_TASKS = {}

DEFAULT_BAG_TOTAL = 15
INVENTORY_UNDO_WINDOW_SECONDS = 30 * 60
DATA_DIRECTORY = Path(__file__).resolve().parent / "data"
DATABASE_PATH = DATA_DIRECTORY / "hunger_games.db"



def get_database_connection():
    DATA_DIRECTORY.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database():
    with get_database_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                default_bag_total INTEGER NOT NULL DEFAULT 15
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS players (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                bag_total INTEGER NOT NULL DEFAULT 15,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS inventory_items (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                slot INTEGER NOT NULL,
                item_json TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id, slot)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_items (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                item_json TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS inventory_undo (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                created_at REAL NOT NULL,
                undo_json TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )


def get_guild_default_bag_total(guild_id):
    with get_database_connection() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO guild_settings (guild_id, default_bag_total)
            VALUES (?, ?)
            """,
            (guild_id, DEFAULT_BAG_TOTAL),
        )
        row = connection.execute(
            "SELECT default_bag_total FROM guild_settings WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    return int(row["default_bag_total"])


def set_guild_default_bag_total(guild_id, bag_total):
    with get_database_connection() as connection:
        connection.execute(
            """
            INSERT INTO guild_settings (guild_id, default_bag_total)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET default_bag_total = excluded.default_bag_total
            """,
            (guild_id, bag_total),
        )


def ensure_player_record(guild_id, user_id):
    default_bag_total = get_guild_default_bag_total(guild_id)
    with get_database_connection() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO players (guild_id, user_id, bag_total)
            VALUES (?, ?, ?)
            """,
            (guild_id, user_id, default_bag_total),
        )

def get_bag_total(guild_id, user_id):
    ensure_player_record(guild_id, user_id)
    with get_database_connection() as connection:
        row = connection.execute(
            "SELECT bag_total FROM players WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
    return int(row["bag_total"])


def get_inventory_items(guild_id, user_id):
    ensure_player_record(guild_id, user_id)
    with get_database_connection() as connection:
        rows = connection.execute(
            """
            SELECT slot, item_json
            FROM inventory_items
            WHERE guild_id = ? AND user_id = ?
            ORDER BY slot
            """,
            (guild_id, user_id),
        ).fetchall()

    inventory = {}
    for row in rows:
        try:
            inventory[int(row["slot"])] = json.loads(row["item_json"])
        except json.JSONDecodeError:
            inventory[int(row["slot"])] = {
                "name": "Unknown Item",
                "amount": "1",
                "description": "This saved item could not be read.",
            }
    return inventory


def get_pending_item(guild_id, user_id):
    with get_database_connection() as connection:
        row = connection.execute(
            """
            SELECT item_json FROM pending_items
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["item_json"])
    except json.JSONDecodeError:
        return {"name": "Unknown Pending Item", "amount": "1"}


def player_has_pending_item(member):
    if member.guild is None:
        return False
    return get_pending_item(member.guild.id, member.id) is not None


def normalize_inventory_item(item_data, source_type):
    name = item_data.get("name") or item_data.get("item") or "Unknown Item"
    description = item_data.get("description", "")
    categories = list(item_data.get("categories", []))

    if source_type == "food" and "usable" not in categories:
        categories.append("usable")

    category_defaults = {
        "Old Purse": ["equippable_extra_slot"],
        "Rusty Machete": ["equippable_weapon"],
        "Shatterstone Dagger": ["equippable_weapon"],
        "Vine Whip": ["equippable_weapon"],
        "Ice Spike": ["equippable_weapon"],
        "Empty Plastic Water Bottle": ["craftable", "water_container"],
        "Bottle of Alcohol": ["usable", "water_container"],
        "First Aid Kit": ["usable", "medical"],
        "Bottle of Meds": ["usable", "medical"],
    }
    for category in category_defaults.get(name, []):
        if category not in categories:
            categories.append(category)

    carry_water = item_data.get("carry_water", "water_container" in categories)
    water_capacity = item_data.get("water_capacity", 1 if carry_water else 0)

    effects = {
        "hp": 0,
        "thirst": 0,
        "hunger": 0,
        "sleep": 0,
        "bag_total": 5 if name == "Old Purse" else 0,
    }
    effects.update(item_data.get("effects", {}))

    return {
        "name": name,
        "amount": item_data.get("amount", "1"),
        "description": description,
        "source_type": source_type,
        "categories": categories,
        "effects": effects,
        "statuses_add": list(item_data.get("statuses_add", [])),
        "statuses_remove": list(item_data.get("statuses_remove", [])),
        "equipped": bool(item_data.get("equipped", False)),
        "carry_water": bool(carry_water),
        "water_capacity": water_capacity,
        "water_amount": item_data.get("water_amount", 0),
        "metadata": dict(item_data.get("metadata", {})),
    }


def find_first_empty_inventory_slot(guild_id, user_id):
    bag_total = get_bag_total(guild_id, user_id)
    occupied = set(get_inventory_items(guild_id, user_id))
    for slot in range(1, bag_total + 1):
        if slot not in occupied:
            return slot
    return None


def save_inventory_item(guild_id, user_id, slot, item_data):
    with get_database_connection() as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO inventory_items
            (guild_id, user_id, slot, item_json)
            VALUES (?, ?, ?, ?)
            """,
            (guild_id, user_id, slot, json.dumps(item_data)),
        )


def save_pending_item(guild_id, user_id, item_data):
    with get_database_connection() as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO pending_items
            (guild_id, user_id, item_json)
            VALUES (?, ?, ?)
            """,
            (guild_id, user_id, json.dumps(item_data)),
        )


def delete_inventory_item(guild_id, user_id, slot):
    with get_database_connection() as connection:
        cursor = connection.execute(
            """
            DELETE FROM inventory_items
            WHERE guild_id = ? AND user_id = ? AND slot = ?
            """,
            (guild_id, user_id, slot),
        )
    return cursor.rowcount > 0


def clear_pending_item(guild_id, user_id):
    with get_database_connection() as connection:
        connection.execute(
            "DELETE FROM pending_items WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )


def save_inventory_undo(guild_id, user_id, undo_data):
    with get_database_connection() as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO inventory_undo
            (guild_id, user_id, created_at, undo_json)
            VALUES (?, ?, ?, ?)
            """,
            (guild_id, user_id, time.time(), json.dumps(undo_data)),
        )


def get_inventory_undo(guild_id, user_id):
    with get_database_connection() as connection:
        row = connection.execute(
            """
            SELECT created_at, undo_json
            FROM inventory_undo
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        ).fetchone()

    if row is None:
        return None

    try:
        undo_data = json.loads(row["undo_json"])
    except json.JSONDecodeError:
        clear_inventory_undo(guild_id, user_id)
        return None

    undo_data["created_at"] = float(row["created_at"])
    return undo_data


def clear_inventory_undo(guild_id, user_id):
    with get_database_connection() as connection:
        connection.execute(
            "DELETE FROM inventory_undo WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )


def parse_inventory_slots(raw_slots):
    if raw_slots is None:
        return [], []

    tokens = [token.strip() for token in raw_slots.split(",")]
    slots = []
    invalid_tokens = []

    for token in tokens:
        if not token:
            continue
        if not token.isdigit():
            invalid_tokens.append(token)
            continue
        slot = int(token)
        if slot not in slots:
            slots.append(slot)

    return slots, invalid_tokens


def sort_inventory_alphabetically(guild_id, user_id):
    inventory = get_inventory_items(guild_id, user_id)
    sorted_items = sorted(
        inventory.values(),
        key=lambda item: (item.get("name") or item.get("item") or "").casefold(),
    )

    with get_database_connection() as connection:
        connection.execute(
            "DELETE FROM inventory_items WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        for slot, item_data in enumerate(sorted_items, start=1):
            connection.execute(
                """
                INSERT INTO inventory_items (guild_id, user_id, slot, item_json)
                VALUES (?, ?, ?, ?)
                """,
                (guild_id, user_id, slot, json.dumps(item_data)),
            )


def make_item_identifier(name):
    identifier = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    return identifier or "unknown-item"


def build_item_identifier_catalog():
    catalog = {}

    def add_entry(item_data, source_type):
        name = item_data.get("name") or item_data.get("item")
        if not name or name == "Island Specific":
            return
        identifier = make_item_identifier(name)
        catalog.setdefault(identifier, normalize_inventory_item(item_data, source_type))

    for item_data in ITEM_LOOT_TABLE.values():
        add_entry(item_data, "item")

    for island_items in ISLAND_SPECIFIC_ITEMS.values():
        for name in island_items.values():
            add_entry({"item": name, "amount": "1", "description": "Island-specific item."}, "item")

    for item_data in FOOD_LOOT_TABLE.values():
        add_entry(item_data, "food")

    for island_food in ISLAND_SPECIFIC_FOOD.values():
        for item_data in island_food.values():
            add_entry(item_data, "food")

    return catalog


def format_inventory_item_line(slot, item_data):
    name = item_data.get("name") or item_data.get("item") or "Unknown Item"
    amount = item_data.get("amount", "1")
    return f"{slot}. **{name}** — {amount}"


def format_inventory_message(member, include_pending=True):
    guild_id = member.guild.id
    user_id = member.id
    bag_total = get_bag_total(guild_id, user_id)
    inventory = get_inventory_items(guild_id, user_id)
    pending = get_pending_item(guild_id, user_id) if include_pending else None

    message = (
        f"**{member.display_name}'s Inventory**\n"
        f"Slots used: **{len(inventory)}/{bag_total}**\n\n"
    )

    for slot in range(1, bag_total + 1):
        if slot in inventory:
            message += format_inventory_item_line(slot, inventory[slot]) + "\n"
        else:
            message += f"{slot}. *Empty*\n"

    if pending is not None:
        message += "\n" + format_inventory_item_line(100, pending) + " — **NEW ITEM**\n"

    return message.rstrip()


async def add_found_item_to_inventory(ctx, member, item_data, source_type):
    normalized_item = normalize_inventory_item(item_data, source_type)
    guild_id = ctx.guild.id
    user_id = member.id
    empty_slot = find_first_empty_inventory_slot(guild_id, user_id)

    if empty_slot is not None:
        save_inventory_item(guild_id, user_id, empty_slot, normalized_item)
        await ctx.send(
            f"✅ **{normalized_item['name']}** was added to inventory slot **{empty_slot}**."
        )
        return

    save_pending_item(guild_id, user_id, normalized_item)
    await ctx.send(
        "Do you want to keep this? If so, please drop an item.\n\n"
        + format_inventory_message(member)
        + "\n\nUse `!discard <slot>` to replace that slot, or `!discard 100` "
          "to discard the new item."
    )


async def send_inventory_sort_block_message(ctx, member):
    if player_has_pending_item(member):
        await ctx.send("You have to sort out your inventory first!")
        return True
    return False


def get_member_district(member):
    for i in range(1, 13):
        district_role = f"D{i}"

        if member_has_role(member, district_role):
            return district_role

    return "No District"

ITEM_LOOT_TABLE = {
    1: {"item": "First Aid Kit", "amount": "1", "description": "5 bandaids and some neosporin inside +4 HP per bandaid."},
    2: {"item": "Rock", "amount": "1", "description": "If it was bigger, this description would be *boulder*."},
    3: {"item": "Old Purse", "amount": "1", "description": "Adds +5 bag space."},
    4: {"item": "Bottle of Meds", "amount": "5 pills inside", "description": "Adds +3 HP per pill."},
    5: {"item": "Stick", "amount": "1", "description": "It's so... stick-y."},
    6: {"item": "Sleeping Bag", "amount": "1", "description": "As opposed to sleeping bags, like the ones under your eyes."},
    7: {"item": "Bottle of Alcohol", "amount": "1", "description": "Unopened."},
    8: {"item": "Warhorn", "amount": "1", "description": 'Alerts players of the user location: "A horn is heard from X".'},
    9: {"item": "Rope", "amount": "1", "description": "About 3 feet of rope."},
    10: {"item": "Pile of Nails", "amount": "1", "description": "6 nails."},

    11: {"item": "Island Specific", "amount": "1", "description": "Depends on current island."},
    12: {"item": "Island Specific", "amount": "1", "description": "Depends on current island."},
    13: {"item": "Island Specific", "amount": "1", "description": "Depends on current island."},

    14: {"item": "Old Pair of Pants", "amount": "1", "description": "How do you think a dog would wear these?"},
    15: {"item": "Dirty Stuffed Animal", "amount": "1", "description": "When asked if it had room for dessert, it'll remind you it's stuffed."},
    16: {"item": "Bottle of Meds", "amount": "5 pills inside", "description": "Adds +3 HP per pill."},
    17: {"item": "Fork", "amount": "1", "description": "The tines of power to fork up some people."},
    18: {"item": "Bag of Marbles", "amount": "1", "description": "Cloth bag with 10 marbles inside."},
    19: {"item": "Box of Matches", "amount": "1", "description": "5 matches inside."},
    20: {"item": "Old Phone Charger", "amount": "1", "description": "Fresh from the junk drawer."},
    21: {"item": "Spearhead", "amount": "1", "description": "An attachment for people who looked at a stick and said, 'Needs more murder.'"},
    22: {"item": "Empty Plastic Water Bottle", "amount": "1", "description": "Water you talking about it's empty?"},
    23: {"item": "Lighter", "amount": "1", "description": "Light? I hardly know her!"},
    24: {"item": "T-Shirt", "amount": "1", "description": "Makes terrible tea by itself."},
    25: {"item": "Piece of Scrap Metal", "amount": "1", "description": "About the size/length of your arm."},
    26: {"item": "Plank of Wood", "amount": "1", "description": "A premium rectangle sourced from nature."},
    27: {"item": "Spool of Thread", "amount": "1", "description": "I pity the spool who disrespects you."},
    28: {"item": "Bottle of Meds", "amount": "5 pills inside", "description": "Adds +3 HP per pill."},
    29: {"item": "Pair of Broken Glasses", "amount": "1", "description": "Eye hope they weren't prescription."},
    30: {"item": "Rusty Machete", "amount": "1", "description": "As opposed to Rusty's machete, which is in a murder case."},
    31: {"item": "Tarp", "amount": "1", "description": "Owned by dads across the planet to cover their grill."},
    32: {"item": "Playing Cards", "amount": "1", "description": "52 reminders of Callista's Casino."},
    33: {"item": "Pile of Bones", "amount": "1", "description": "A skull with a rib cage."},
    34: {"item": "Magnifying Glass", "amount": "1", "description": "Let's split up and look for clues gang! - Mystery Incorporated"},
    35: {"item": "Rusty Wrench", "amount": "1", "description": "A rusty wrench is also what you'll find in a medieval tavern."},
    36: {"item": "Rubber Mallet", "amount": "1", "description": "Cannot be used as a weapon."},
    37: {"item": "Single Shoe", "amount": "1", "description": "Evidence that somebody had a worse day than you."},
    38: {"item": "Empty Plastic Water Bottle", "amount": "1", "description": "Thank you for picking up that litter."},
    39: {"item": "First Aid Kit", "amount": "1", "description": "5 bandaids and some neosporin inside +4 HP per bandaid."},
    40: {"item": "Stick", "amount": "1", "description": "It's so... stick-y."},
}

ISLAND_SPECIFIC_ITEMS = {
    "Ember Island": {
        11: "Shatterstone Dagger",
        12: "Lava Shard",
        13: "Melted Tech",
    },
    "Skyspire Ruins": {
        11: "Rusted Blade Fragment",
        12: "Wind Totem",
        13: "Shattered Compass",
    },
    "Verdant Veil": {
        11: "Vines",
        12: "Old Hammock",
        13: "Vine Whip",
    },
    "Frostcrest": {
        11: "Pair of Gloves",
        12: "Lantern",
        13: "Ice Spike",
    },
    "Compass Core": {
        11: "Sleeping Bag",
        12: "Plank of Wood",
        13: "Lighter",
    },
}

ISLAND_FALLBACK_ITEMS = {
    11: "Sleeping Bag",
    12: "Plank of Wood",
    13: "Lighter",
}


FOOD_LOOT_TABLE = {
    **{
        roll: {
            "item": "Spoiled Rations",
            "amount": "1 serving",
            "description": "Someone's leftovers. Moldy, questionable. 50/50 heal/damage. Heal: +5 HP +5 hunger. Damage: -5 HP -5 hunger.",
        }
        for roll in range(1, 6)
    },
    **{
        roll: {
            "item": "Mushroom Cluster",
            "amount": "3 mushrooms",
            "description": "Restores +3 hunger, but causes hallucinations. Your next search takes double time because you keep getting distracted.",
        }
        for roll in range(6, 11)
    },
    **{
        roll: {
            "item": "Fruit",
            "amount": "1 serving",
            "description": "Restores +5 hunger +3 HP.",
        }
        for roll in range(11, 16)
    },
    **{
        roll: {
            "item": "Bones of an Animal of Sorts",
            "amount": "2 bones",
            "description": "Might be able to scavenge some meat from the bones. Takes 1 HR to do so. +3 hunger.",
        }
        for roll in range(16, 21)
    },
    **{
        roll: {
            "item": "Berries?",
            "amount": "3",
            "description": "Leech eggs that appear to be berries. Lose 5 HP.",
        }
        for roll in range(21, 26)
    },
    **{
        roll: {
            "item": "Melon",
            "amount": "Can be divided into 4 rations",
            "description": "Watermelon or honeydew. Restores +5 thirst +5 hunger.",
        }
        for roll in range(26, 31)
    },
    31: {"item": "Island Specific", "amount": "1", "description": "Depends on current island."},
    32: {"item": "Island Specific", "amount": "1", "description": "Depends on current island."},
    33: {"item": "Island Specific", "amount": "1", "description": "Depends on current island."},
}

ISLAND_SPECIFIC_FOOD = {
    "Ember Island": {
        31: {"item": "Charred Berries", "amount": "5 berries", "description": "Edible but smoky. +5 hunger -2 HP from smoke inhalation."},
        32: {"item": "Scorched Capital Field Ration", "amount": "1 serving", "description": "Restores +5 hunger, causes nausea -5 HP."},
        33: {"item": "Magma Shroom", "amount": "1 shroom", "description": "Causes hallucinations. +5 hunger, next task takes double time."},
    },
    "Skyspire Ruins": {
        31: {"item": "Moss Crust", "amount": "1 serving", "description": "Restores +2 hunger +2 thirst."},
        32: {"item": "Mineral Water Vein", "amount": "1", "description": "Completely restores thirst."},
        33: {"item": "Bird Nest With 2 Eggs", "amount": "2 eggs", "description": "Eggs restore +5 hunger each. Triggers bird mutt."},
    },
    "Frostcrest": {
        31: {"item": "Frozen Fish in Ice", "amount": "1 fish", "description": "Must be thawed and cooked. +10 hunger +5 HP. If attempted frozen: +5 hunger -5 HP from mouth damage."},
        32: {"item": "Snowcone", "amount": "1", "description": "Restores 10 thirst."},
        33: {"item": "Tundra Tea Leaves", "amount": "1 serving", "description": "Nothing if eaten. If brewed: +5 thirst, +5 hunger, +10 HP."},
    },
    "Verdant Veil": {
        31: {"item": "Tropical Pod", "amount": "Contains 3 fruit", "description": "Sweet, juicy fruit. +3 hunger +3 thirst."},
        32: {"item": "Cloud Frog", "amount": "Living frog", "description": "Edible amphibian. 40% chance of being poisoned if cooked, 100% chance if not. +10 hunger. Poison is -5 HP per hour for 4 hours."},
        33: {"item": "Ant Egg Bundle", "amount": "1 serving", "description": "Restores +3 hunger +3 HP -3 thirst."},
    },
    "Compass Core": {
        31: {"item": "Spoiled Rations", "amount": "1 serving", "description": "Someone's leftovers. Moldy, questionable. 50/50 heal/damage. Heal: +5 HP +5 hunger. Damage: -5 HP -5 hunger."},
        32: {"item": "Mushroom Cluster", "amount": "3 mushrooms", "description": "Restores +3 hunger, but causes hallucinations. Your next search takes double time because you keep getting distracted."},
        33: {"item": "Melon", "amount": "Can be divided into 4 rations", "description": "Watermelon or honeydew. Restores +5 thirst +5 hunger."},
    },
}


TRAVELING_ROLE = "Traveling"
PLAYER_ROLE = "Tribute"

FAST_TRAVEL_ROLE = "D6"
SLOW_TRAVEL_ROLES = ["Wet", "Frostbite"]
AILMENT_ROLES = ["Frostbite", "Wet", "Poisoned", "Burned"]


LOCATIONS = {
    "Skyspire Ruins": ["Stone Building", "Large Cavern", "Garden Crater"],
    "Verdant Veil": ["Hallow Tree", "Mushroom Patch", "Waterfall", "Electronics Building"],
    "Ember Island": ["Partially Collapsed Ruins", "Field of Volcano Vents", "Lava River Border"],
    "Frostcrest": ["Abandoned Tent Site", "Icy Cave", "Frozen Lake"],
    "Compass Core": ["The Eye", "Greenhouse", "Marketplace"],
}

LOCATION_TO_CATEGORY = {
    location.lower(): category
    for category, locations in LOCATIONS.items()
    for location in locations
}

ROOM_STATUS = {
    location.lower(): True
    for locations in LOCATIONS.values()
    for location in locations
}

ROOM_STATUS["the eye"] = False

BRIDGE_PATHS = {
    frozenset(["Verdant Veil", "Ember Island"]): True,
    frozenset(["Verdant Veil", "Frostcrest"]): True,
    frozenset(["Frostcrest", "Skyspire Ruins"]): True,
    frozenset(["Skyspire Ruins", "Ember Island"]): True,

    frozenset(["Verdant Veil", "Compass Core"]): True,
    frozenset(["Verdant Veil", "Skyspire Ruins"]): False,
    frozenset(["Ember Island", "Compass Core"]): False,
    frozenset(["Ember Island", "Frostcrest"]): False,
    frozenset(["Skyspire Ruins", "Compass Core"]): True,
    frozenset(["Frostcrest", "Compass Core"]): False,
}



def format_seconds(seconds):
    seconds = int(max(0, seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)

    if hours > 0:
        return f"{hours}h {minutes}m {seconds}s"

    if minutes > 0:
        return f"{minutes}m {seconds}s"

    return f"{seconds}s"


def format_minutes_seconds(seconds):
    seconds = int(max(0, seconds))
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes} minutes {seconds} seconds"


def start_player_action(member, action_type, seconds, data=None):
    action_id = f"{member.id}-{time.time()}-{random.randint(1000, 9999)}"
    action_data = dict(data or {})
    action_data["action_id"] = action_id

    PLAYER_ACTION_COOLDOWNS[member.id] = {
        "action_type": action_type,
        "end_time": time.time() + seconds,
        "data": action_data,
    }

    return action_id


def action_matches(member, action_id, allowed_action_types=None, destination=None):
    action = get_player_action(member)

    if action is None:
        return False

    if allowed_action_types is not None and action.get("action_type") not in allowed_action_types:
        return False

    data = action.get("data", {})

    if data.get("action_id") != action_id:
        return False

    if destination is not None and data.get("destination") != destination:
        return False

    return True


def track_action_task(member, action_id, task):
    ACTIVE_ACTION_TASKS[member.id] = {
        "action_id": action_id,
        "task": task,
    }


def clear_tracked_action_task(member, action_id=None):
    tracked = ACTIVE_ACTION_TASKS.get(member.id)

    if tracked is None:
        return

    if action_id is not None and tracked.get("action_id") != action_id:
        return

    ACTIVE_ACTION_TASKS.pop(member.id, None)


def cancel_tracked_action_task(member):
    tracked = ACTIVE_ACTION_TASKS.pop(member.id, None)

    if tracked is None:
        return False

    task = tracked.get("task")

    if task is not None and not task.done():
        task.cancel()
        return True

    return False


def get_player_action(member):
    action = PLAYER_ACTION_COOLDOWNS.get(member.id)

    if action is None:
        return None

    # This keeps old saved timestamp-style cooldowns from breaking the bot.
    if isinstance(action, (int, float)):
        remaining = action - time.time()

        if remaining <= 0:
            PLAYER_ACTION_COOLDOWNS.pop(member.id, None)
            return None

        converted_action = {
            "action_type": "unknown",
            "end_time": action,
            "data": {},
            "remaining": remaining,
        }
        PLAYER_ACTION_COOLDOWNS[member.id] = converted_action
        return converted_action

    remaining = action["end_time"] - time.time()
    action["remaining"] = remaining
    return action


def clear_player_action(member):
    PLAYER_ACTION_COOLDOWNS.pop(member.id, None)
    clear_tracked_action_task(member)


def get_player_action_remaining(member):
    action = get_player_action(member)

    if action is None:
        return 0

    remaining = action["end_time"] - time.time()

    if remaining <= 0:
        return 0

    return remaining


async def send_player_action_cooldown_message(ctx, remaining):
    await ctx.send(
        f"You are currently performing an action, please try again in "
        f"{format_minutes_seconds(remaining)}"
    )


ACTION_ACTIVITY_TEXT = {
    "room": "moving",
    "frostcrest-yeti": "moving",
    "search-item": "searching for an item",
    "search-food": "searching for food",
    "search-water": "searching for water",
    "craft": "crafting",
    "sleep": "sleeping",
    "cancelled-cooldown": "waiting for your action cooldown",
    "unknown": "performing an action",
}


def get_action_activity_text(action_type):
    return ACTION_ACTIVITY_TEXT.get(action_type, "performing an action")


async def send_current_action_block_message(ctx, member):
    action = get_player_action(member)

    if action is None:
        return False

    remaining = action["end_time"] - time.time()
    action_type = action.get("action_type", "unknown")

    if remaining > 0:
        if action_type == "frostcrest-yeti":
            await ctx.send(
                "This isn't the time for this now! You're being chased! "
                f"Try again in {format_minutes_seconds(remaining)}....if you survive!"
            )
        elif action_type == "sleep":
            await ctx.send(
                "You do not know how to sleepwalk, please try again in "
                f"{format_minutes_seconds(remaining)}."
            )
        elif action_type == "cancelled-cooldown":
            await ctx.send(
                "Your previous action was cancelled, but its cooldown is still active. "
                f"Please try again in {format_minutes_seconds(remaining)}."
            )
        else:
            activity = get_action_activity_text(action_type)
            await ctx.send(
                f"You are currently {activity}. It will take another "
                f"{format_minutes_seconds(remaining)}."
            )
        return True

    if action_type == "search-item":
        # Fallback: if the bot missed the automatic resolver for any reason,
        # resolve the search when the player tries another action.
        clear_player_action(member)
        await resolve_search_item(ctx, member)
        return True

    if action_type == "search-food":
        # Fallback: if the bot missed the automatic resolver for any reason,
        # resolve the search when the player tries another action.
        clear_player_action(member)
        await resolve_search_food(ctx, member)
        return True

    if action_type == "search-water":
        clear_player_action(member)
        await resolve_search_water(ctx, member)
        return True

    if action_type == "craft":
        clear_player_action(member)
        await resolve_craft(ctx, member)
        return True

    if action_type == "sleep":
        clear_player_action(member)
        await resolve_sleep(ctx, member)
        return True

    clear_player_action(member)
    return False

def player_action_is_blocked(member):
    return get_player_action_remaining(member) > 0


def apply_player_action_cooldown(member, action_type="unknown", seconds=None, data=None):
    if seconds is None:
        seconds = PLAYER_ACTION_COOLDOWN_SECONDS

    start_player_action(member, action_type, seconds, data)


def get_member_current_island(member):
    current_location = get_member_current_location(member)

    if current_location is None:
        return None

    return LOCATION_TO_CATEGORY.get(current_location.lower())


def get_item_for_roll(member, roll):
    if player_is_in_location(member, "Electronics Building") and roll in [20, 21, 22, 23, 24, 25]:
        return {
            "item": "Broken Tech",
            "amount": "1",
            "description": "Just some old tech that seems to be damaged.",
        }

    base_item = ITEM_LOOT_TABLE[roll].copy()

    if roll not in [11, 12, 13]:
        return base_item

    current_island = get_member_current_island(member)

    if current_island in ISLAND_SPECIFIC_ITEMS:
        base_item["item"] = ISLAND_SPECIFIC_ITEMS[current_island][roll]
        base_item["description"] = f"Island-specific item from {current_island}."
    else:
        base_item["item"] = ISLAND_FALLBACK_ITEMS[roll]
        base_item["description"] = "Fallback item because no island-specific location was found."

    base_item["amount"] = "1"
    return base_item


def format_item_found_message(member, roll, item_data, bypass=False):
    amount = item_data.get("amount", "1")
    description = item_data.get("description", "")
    bypass_text = "\n\n_Bypass used: no cooldown applied._" if bypass else ""

    message = (
        f"🔎 You search for an item.\n"
        f"🎲 Roll: **{roll}**\n"
        f"📦 Found: **{item_data['item']}**\n"
        f"Amount: **{amount}**"
    )

    if description:
        message += f"\nDescription: {description}"

    message += bypass_text
    return message


def player_gets_d12_search_boost(member):
    current_location = get_member_current_location(member)

    return (
        member_has_role(member, "D12")
        and current_location is not None
        and clean_name(current_location) in [
            clean_name("Large Cavern"),
            clean_name("Icy Cave"),
            clean_name("Icy-Cave"),
        ]
    )


def player_is_in_location(member, location_name):
    current_location = get_member_current_location(member)

    return (
        current_location is not None
        and clean_name(current_location) == clean_name(location_name)
    )


def player_gets_abandoned_tent_site_search_boost(member):
    return player_is_in_location(member, "Abandoned Tent Site")


def player_is_in_marketplace(member):
    return player_is_in_location(member, "Marketplace")


def player_is_in_mushroom_patch(member):
    return player_is_in_location(member, "Mushroom Patch")


async def resolve_search_item(ctx, member):
    if player_is_in_marketplace(member):
        await ctx.send(f"{member.mention} finds that the Marketplace is plentiful!")

        roll = random.randint(1, 40)
        item_data = get_item_for_roll(member, roll)

        await ctx.send(format_item_found_message(member, roll, item_data))
        await add_found_item_to_inventory(ctx, member, item_data, "item")
        return

    has_abandoned_tent_site_boost = player_gets_abandoned_tent_site_search_boost(member)
    has_d12_boost = player_gets_d12_search_boost(member)

    if has_abandoned_tent_site_boost:
        success_rolls = [4, 5, 6, 7, 8, 9, 10]
        await ctx.send(
            f"{member.mention} rolls a D10! Since you are in the Abandoned tent site, "
            "you get a 20% boost on this roll. You will be successful if you get "
            "either a 4,5,6,7,8,9 or 10."
        )
    elif has_d12_boost:
        success_rolls = [4, 5, 6, 7, 8, 9, 10]
        await ctx.send(
            f"{member.mention} rolls a D10! Since you are from district 12 and you are in a cave, "
            "you get a 20% boost on this role. You will be successful if you get "
            "either a 4,5,6,7,8,9 or 10."
        )
    else:
        success_rolls = [6, 7, 8, 9, 10]
        await ctx.send(
            f"{member.mention} rolls a D10! You must get either a 6,7,8,9, or 10 to succeed."
        )

    d10_roll = random.randint(1, 10)

    await ctx.send(
        f"....you roll.....and it lands on..... a **{d10_roll}**"
    )

    if d10_roll not in success_rolls:
        await ctx.send("You did not find anything.")
        return

    roll = random.randint(1, 40)
    item_data = get_item_for_roll(member, roll)

    await ctx.send(format_item_found_message(member, roll, item_data))
    await add_found_item_to_inventory(ctx, member, item_data, "item")


async def resolve_search_item_after_timer(ctx, member, search_id):
    action = get_player_action(member)

    if action is None:
        return

    remaining = action["end_time"] - time.time()

    if remaining > 0:
        await asyncio.sleep(remaining)

    action = get_player_action(member)

    if action is None:
        return

    if action.get("action_type") != "search-item":
        return

    if action.get("data", {}).get("search_id") != search_id:
        return

    clear_player_action(member)
    await resolve_search_item(ctx, member)




def get_food_for_roll(member, roll):
    base_food = FOOD_LOOT_TABLE[roll].copy()

    if roll not in [31, 32, 33]:
        return base_food

    current_island = get_member_current_island(member)

    if current_island in ISLAND_SPECIFIC_FOOD:
        return ISLAND_SPECIFIC_FOOD[current_island][roll].copy()

    return ISLAND_SPECIFIC_FOOD["Compass Core"][roll].copy()


def format_food_found_message(member, roll, food_data, bypass=False, search_emoji="🍕"):
    amount = food_data.get("amount", "1")
    bypass_text = "\n\n_Bypass used: no cooldown applied._" if bypass else ""

    message = (
        f"{search_emoji} You search for food.\n"
        f"🎲 Roll: **{roll}**\n"
        f"🥫 Found: **{food_data['item']}**\n"
        f"Amount: **{amount}**"
    )

    message += bypass_text
    return message


async def resolve_search_food(ctx, member):
    if player_is_in_marketplace(member):
        await ctx.send(f"{member.mention} finds that the Marketplace is plentiful!")

        roll = random.randint(1, 33)
        food_data = get_food_for_roll(member, roll)

        await ctx.send(format_food_found_message(member, roll, food_data))
        await add_found_item_to_inventory(ctx, member, food_data, "food")
        return

    has_abandoned_tent_site_boost = player_gets_abandoned_tent_site_search_boost(member)
    has_d12_boost = player_gets_d12_search_boost(member)

    if has_abandoned_tent_site_boost:
        success_rolls = [4, 5, 6, 7, 8, 9, 10]
        await ctx.send(
            f"{member.mention} rolls a D10! Since you are in the Abandoned tent site, "
            "you get a 20% boost on this roll. You will be successful if you get "
            "either a 4,5,6,7,8,9 or 10."
        )
    elif has_d12_boost:
        success_rolls = [4, 5, 6, 7, 8, 9, 10]
        await ctx.send(
            f"{member.mention} rolls a D10! Since you are from district 12 and you are in a cave, "
            "you get a 20% boost on this roll. You will be successful if you get "
            "either a 4,5,6,7,8,9 or 10."
        )
    else:
        success_rolls = [6, 7, 8, 9, 10]
        await ctx.send(
            f"{member.mention} rolls a D10! You must get either a 6,7,8,9, or 10 to succeed."
        )

    d10_roll = random.randint(1, 10)

    await ctx.send(
        f"....you roll.....and it lands on..... a **{d10_roll}**"
    )

    if d10_roll not in success_rolls:
        await ctx.send("You did not find anything.")
        return

    if player_is_in_mushroom_patch(member):
        mushroom_data = {
            "item": "Mushroom Cluster",
            "amount": "3 mushrooms",
            "description": (
                "Restores +3 hunger, but causes hallucinations. "
                "Your next search takes double time."
            ),
        }
        await ctx.send(
            "🍄 You search for food.\n"
            "🥫 Found: **Mushroom Cluster**\n"
            "Amount: **3 mushrooms**"
        )
        await add_found_item_to_inventory(ctx, member, mushroom_data, "food")
        return

    roll = random.randint(1, 33)
    food_data = get_food_for_roll(member, roll)

    await ctx.send(format_food_found_message(member, roll, food_data))
    await add_found_item_to_inventory(ctx, member, food_data, "food")


async def resolve_search_food_after_timer(ctx, member, search_id):
    action = get_player_action(member)

    if action is None:
        return

    remaining = action["end_time"] - time.time()

    if remaining > 0:
        await asyncio.sleep(remaining)

    action = get_player_action(member)

    if action is None:
        return

    if action.get("action_type") != "search-food":
        return

    if action.get("data", {}).get("search_id") != search_id:
        return

    clear_player_action(member)
    await resolve_search_food(ctx, member)


async def resolve_search_water(ctx, member):
    gamemakers_role = discord.utils.get(ctx.guild.roles, name="GAMEMAKERS")
    gamemakers_ping = gamemakers_role.mention if gamemakers_role else "@GAMEMAKERS"

    await ctx.send(
        f"💧 {member.mention} finishes searching for water. "
        f"{gamemakers_ping} should now resolve what they found."
    )


async def resolve_search_water_after_timer(ctx, member, search_id):
    action = get_player_action(member)

    if action is None:
        return

    remaining = action["end_time"] - time.time()

    if remaining > 0:
        await asyncio.sleep(remaining)

    action = get_player_action(member)

    if action is None:
        return

    if action.get("action_type") != "search-water":
        return

    if action.get("data", {}).get("search_id") != search_id:
        return

    clear_player_action(member)
    await resolve_search_water(ctx, member)


async def resolve_craft(ctx, member):
    gamemakers_role = discord.utils.get(ctx.guild.roles, name="GAMEMAKERS")
    gamemakers_ping = gamemakers_role.mention if gamemakers_role else "@GAMEMAKERS"

    await ctx.send(
        f"🛠️ {member.mention} finishes crafting. "
        f"{gamemakers_ping} should now resolve the crafted item."
    )


async def resolve_craft_after_timer(ctx, member, craft_id):
    action = get_player_action(member)

    if action is None:
        return

    remaining = action["end_time"] - time.time()

    if remaining > 0:
        await asyncio.sleep(remaining)

    action = get_player_action(member)

    if action is None:
        return

    if action.get("action_type") != "craft":
        return

    if action.get("data", {}).get("craft_id") != craft_id:
        return

    clear_player_action(member)
    await resolve_craft(ctx, member)


async def resolve_sleep(ctx, member):
    await ctx.send(
        f"{member.mention} wakes up and receives **+3 sleep**."
    )


async def resolve_sleep_after_timer(ctx, member, sleep_id):
    action = get_player_action(member)

    if action is None:
        return

    remaining = action["end_time"] - time.time()

    if remaining > 0:
        await asyncio.sleep(remaining)

    action = get_player_action(member)

    if action is None:
        return

    if action.get("action_type") != "sleep":
        return

    if action.get("data", {}).get("sleep_id") != sleep_id:
        return

    clear_player_action(member)
    await resolve_sleep(ctx, member)


async def send_long_message(ctx, message):
    if len(message) <= 2000:
        await ctx.send(message)
        return

    chunks = []
    current_chunk = ""

    for line in message.splitlines(keepends=True):
        if len(current_chunk) + len(line) > 1900:
            chunks.append(current_chunk)
            current_chunk = line
        else:
            current_chunk += line

    if current_chunk:
        chunks.append(current_chunk)

    for chunk in chunks:
        await ctx.send(chunk)


def clean_name(name):
    return " ".join(name.lower().strip().split())


def get_all_location_role_names():
    return [
        location
        for locations in LOCATIONS.values()
        for location in locations
    ]


def get_real_location_name(location_input):
    cleaned_input = clean_name(location_input)

    for location in get_all_location_role_names():
        if clean_name(location) == cleaned_input:
            return location

    return None


def get_real_category_name(category_input):
    cleaned_input = clean_name(category_input)

    for category in LOCATIONS.keys():
        if clean_name(category) == cleaned_input:
            return category

    return None


def get_role_case_insensitive(guild, role_name):
    target = clean_name(role_name)

    for role in guild.roles:
        if clean_name(role.name) == target:
            return role

    return None


def member_has_role(member, role_name):
    for role in member.roles:
        if clean_name(role.name) == clean_name(role_name):
            return True

    return False


def is_tribute(member):
    return member_has_role(member, PLAYER_ROLE)


def get_member_ailments(member):
    ailments = [
        ailment
        for ailment in AILMENT_ROLES
        if member_has_role(member, ailment)
    ]

    return ", ".join(ailments) if ailments else "Healthy"


def calculate_player_travel_seconds(member, base_seconds=None):
    if base_seconds is None:
        base_seconds = PLAYER_ACTION_COOLDOWN_SECONDS

    player_travel_seconds = base_seconds

    if member_has_role(member, FAST_TRAVEL_ROLE):
        player_travel_seconds = player_travel_seconds / 2

    for slow_role in SLOW_TRAVEL_ROLES:
        if member_has_role(member, slow_role):
            player_travel_seconds = player_travel_seconds * 2

    player_travel_seconds = int(player_travel_seconds)
    player_travel_seconds = max(1, player_travel_seconds)
    # Cap travel at 120 minutes (7200 seconds).
    player_travel_seconds = min(7200, player_travel_seconds)

    return player_travel_seconds


def get_member_current_location(member):
    for role in member.roles:
        for official_location in get_all_location_role_names():
            if clean_name(role.name) == clean_name(official_location):
                return official_location

    return None


def bridge_is_enabled(category_one, category_two):
    return BRIDGE_PATHS.get(
        frozenset([category_one, category_two]),
        False
    )



def get_available_move_groups(member, current_location):
    if current_location is None:
        return []

    current_category = LOCATION_TO_CATEGORY[current_location.lower()]
    island_travel_seconds = calculate_player_travel_seconds(member)
    same_island_travel_seconds = calculate_player_travel_seconds(member, INTRA_ISLAND_MOVE_SECONDS)
    groups = []

    # Current island first.
    current_island_moves = []

    for destination_name in LOCATIONS[current_category]:
        destination_key = destination_name.lower()

        if clean_name(destination_name) == clean_name(current_location):
            continue

        if ROOM_STATUS.get(destination_key) is False:
            continue

        current_island_moves.append(f"!move {destination_name.lower()}")

    if current_island_moves:
        groups.append({
            "heading": (
                f"# **Stay on {current_category} "
                f"(Current Island - {format_minutes_seconds(same_island_travel_seconds)})**"
            ),
            "moves": current_island_moves,
        })

    # Then show each enabled bridge destination as its own section.
    for category, locations in LOCATIONS.items():
        if category == current_category:
            continue

        if not bridge_is_enabled(current_category, category):
            continue

        island_moves = []

        for destination_name in locations:
            destination_key = destination_name.lower()

            if ROOM_STATUS.get(destination_key) is False:
                continue

            island_moves.append(f"!move {destination_name.lower()}")

        if island_moves:
            groups.append({
                "heading": (
                    f"# **Move to {category} "
                    f"(New Island - {format_minutes_seconds(island_travel_seconds)})**"
                ),
                "moves": island_moves,
            })

    return groups

def format_available_moves_message(member, current_location):
    groups = get_available_move_groups(member, current_location)

    if not groups:
        return "You do not currently have any available move commands."

    message = "You can currently use the following move commands:\n"

    for group in groups:
        message += f"\n{group['heading']}\n"
        message += "\n".join(group["moves"])
        message += "\n"

    return message.strip()


def set_member_location_entry_time(member, location_name):
    LOCATION_ENTRY_TIMES[member.id] = {
        "location": location_name,
        "entered_at": time.time(),
    }


def get_member_location_duration_text(member, location_name):
    entry = LOCATION_ENTRY_TIMES.get(member.id)

    if entry is None:
        return "Unknown"

    if clean_name(entry.get("location")) != clean_name(location_name):
        return "Unknown"

    return format_minutes_seconds(time.time() - entry.get("entered_at", time.time()))


def format_hh_mm_ss_from_seconds(seconds):
    seconds = int(max(0, seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def get_member_location_duration_hhmmss(member, location_name, start_tracking_if_missing=False):
    entry = LOCATION_ENTRY_TIMES.get(member.id)

    if (
        entry is None
        or clean_name(entry.get("location")) != clean_name(location_name)
    ):
        if start_tracking_if_missing:
            set_member_location_entry_time(member, location_name)
            return "00:00:00"

        return "Unknown"

    return format_hh_mm_ss_from_seconds(time.time() - entry.get("entered_at", time.time()))


def get_member_travel_status_text(member):
    action = get_player_action(member)

    if action is None or action.get("action_type") not in ["room", "frostcrest-yeti"]:
        return "Traveling — Destination unknown. Time left unknown."

    destination_name = action.get("data", {}).get("destination")

    if not destination_name:
        return "Traveling — Destination unknown. Time left unknown."

    destination_island = LOCATION_TO_CATEGORY.get(destination_name.lower(), "Unknown Island")
    end_time = action.get("end_time", time.time())
    remaining = end_time - time.time()
    arrival_unix = int(end_time)

    # Discord renders <t:UNIX:t> in each viewer's own local timezone.
    return (
        f"Traveling to {destination_name}, {destination_island}. "
        f"Expected arrival in **{format_hh_mm_ss_from_seconds(remaining)}** "
        f"at **<t:{arrival_unix}:t>**"
    )


async def send_arrival_message(ctx, member, destination_name):
    await ctx.send(
        f"{member.mention} has arrived at **{destination_name}**.\n"
        f"{format_available_moves_message(member, destination_name)}"
    )


async def remove_location_roles_only(member):
    removable_names = get_all_location_role_names() + [TRAVELING_ROLE]
    roles_to_remove = []

    for role in member.roles:
        for removable_name in removable_names:
            if clean_name(role.name) == clean_name(removable_name):
                roles_to_remove.append(role)
                break

    if roles_to_remove:
        await member.remove_roles(*roles_to_remove)


@bot.event
async def on_ready():
    initialize_database()
    print(f"Logged in as {bot.user}")
    print("Bot is ready.")


@bot.command()
async def ping(ctx):
    await ctx.send("pong")


@bot.command()
async def roles(ctx):
    role_names = [role.name for role in ctx.guild.roles]

    await ctx.send(
        "```txt\n" +
        "\n".join(role_names)[:1900] +
        "\n```"
    )


@bot.command(name="move")
async def move(ctx, *, destination: str):
    member = ctx.author
    guild = ctx.guild

    if await send_inventory_sort_block_message(ctx, member):
        return

    if await send_current_action_block_message(ctx, member):
        return


    destination_name = get_real_location_name(destination)

    if destination_name is None:
        await ctx.send("That location does not exist.")
        return

    destination_key = destination_name.lower()

    if ROOM_STATUS[destination_key] is False:
        await ctx.send("You cannot go there right now.")
        return

    destination_category = LOCATION_TO_CATEGORY[destination_key]
    current_location = get_member_current_location(member)

    destination_role = get_role_case_insensitive(guild, destination_name)

    if destination_role is None:
        await ctx.send(f"The role `{destination_name}` does not exist.")
        return

    if current_location:
        current_category = LOCATION_TO_CATEGORY[current_location.lower()]
    else:
        current_category = None

    if current_category == destination_category:
        player_travel_seconds = calculate_player_travel_seconds(
            member,
            INTRA_ISLAND_MOVE_SECONDS,
        )

        move_action_id = start_player_action(
            member,
            "room",
            player_travel_seconds,
            {"destination": destination_name, "origin": current_location},
        )
        track_action_task(member, move_action_id, asyncio.current_task())

        traveling_role = get_role_case_insensitive(guild, TRAVELING_ROLE)

        if traveling_role is None:
            await ctx.send(f"The role `{TRAVELING_ROLE}` does not exist.")
            return

        await remove_location_roles_only(member)
        await member.add_roles(traveling_role)

        await ctx.send(
            f"{member.mention} is traveling from **{current_location}** "
            f"to **{destination_name}**.\n"
            f"Arrival in **{format_minutes_seconds(player_travel_seconds)}**."
        )

        try:
            await asyncio.sleep(player_travel_seconds)
        except asyncio.CancelledError:
            return

        if action_matches(
            member,
            move_action_id,
            allowed_action_types=["room"],
            destination=destination_name,
        ):
            await remove_location_roles_only(member)
            await member.add_roles(destination_role)
            set_member_location_entry_time(member, destination_name)

            await send_arrival_message(ctx, member, destination_name)
            clear_player_action(member)

        return

    if current_category is None:
        await remove_location_roles_only(member)
        await member.add_roles(destination_role)
        set_member_location_entry_time(member, destination_name)

        await send_arrival_message(ctx, member, destination_name)
        return

    if not bridge_is_enabled(current_category, destination_category):
        await ctx.send("That pathway doesn't seem possible right now.")
        return

    traveling_role = get_role_case_insensitive(guild, TRAVELING_ROLE)

    if traveling_role is None:
        await ctx.send(f"The role `{TRAVELING_ROLE}` does not exist.")
        return

    player_travel_seconds = calculate_player_travel_seconds(member)

    frostcrest_yeti_chase = current_category == "Frostcrest" and destination_category != "Frostcrest"
    travel_action_type = "frostcrest-yeti" if frostcrest_yeti_chase else "room"

    move_action_id = start_player_action(
        member,
        travel_action_type,
        player_travel_seconds,
        {"destination": destination_name, "origin": current_location},
    )
    track_action_task(member, move_action_id, asyncio.current_task())


    await remove_location_roles_only(member)
    await member.add_roles(traveling_role)

    if frostcrest_yeti_chase:
        gamemakers_role = discord.utils.get(ctx.guild.roles, name="GAMEMAKERS")
        gamemakers_ping = gamemakers_role.mention if gamemakers_role else "@GAMEMAKERS"

        await ctx.send(
            f"{member.mention} is traveling from **{current_location}** "
            f"to **{destination_name}**.\n"
            f"As you begin your journey away from the island, which should take "
            f"**{format_minutes_seconds(player_travel_seconds)}**, you hear stomping "
            "behind you as you go to leave...next thing you know a snowball pelts "
            "you in the back of the head! You turn around to see a huge yeti coming after you!"
            f"\n\n{gamemakers_ping}"
        )
    else:
        await ctx.send(
            f"{member.mention} is traveling from **{current_location}** "
            f"to **{destination_name}**.\n"
            f"Arrival in **{format_minutes_seconds(player_travel_seconds)}**."
        )

    try:
        await asyncio.sleep(player_travel_seconds)
    except asyncio.CancelledError:
        return

    if (
        action_matches(
            member,
            move_action_id,
            allowed_action_types=["room", "frostcrest-yeti"],
            destination=destination_name,
        )
        and member_has_role(member, TRAVELING_ROLE)
    ):
        await member.remove_roles(traveling_role)
        await member.add_roles(destination_role)
        set_member_location_entry_time(member, destination_name)

        await send_arrival_message(ctx, member, destination_name)
        clear_player_action(member)




@bot.command(name="search-item")
async def search_item(ctx):
    member = ctx.author

    if await send_inventory_sort_block_message(ctx, member):
        return

    if await send_current_action_block_message(ctx, member):
        return

    search_id = f"{member.id}-{time.time()}"

    start_player_action(
        member,
        "search-item",
        PLAYER_ACTION_COOLDOWN_SECONDS,
        {"search_id": search_id},
    )

    await ctx.send(
        f"You are currently searching for an item. It will take another "
        f"{format_minutes_seconds(PLAYER_ACTION_COOLDOWN_SECONDS)}."
    )

    asyncio.create_task(
        resolve_search_item_after_timer(ctx, member, search_id)
    )


@bot.command(name="Search-Item-Bypass")
@commands.has_permissions(manage_guild=True)
async def search_item_bypass(ctx):
    member = ctx.author

    roll = random.randint(1, 40)
    item_data = get_item_for_roll(member, roll)

    await ctx.send(format_item_found_message(member, roll, item_data, bypass=True))


@bot.command(name="inventory")
async def inventory(ctx):
    await send_long_message(ctx, format_inventory_message(ctx.author))


def format_admin_inventory_support_message(guild_id):
    current_default = get_guild_default_bag_total(guild_id)

    return (
        "**📦 ADMIN INVENTORY SUPPORT**\n\n"
        "**👤 PLAYER COMMANDS**\n"
        "`!inventory`\n"
        "View your inventory and bag capacity.\n\n"
        "`!discard <slot[,slot...]>`\n"
        "Discard one or more inventory slots.\n"
        "Examples: `!discard 4` or `!discard 2, 3, 5, 11`\n\n"
        "`!delete <slot[,slot...]>`\n"
        "Alias for `!discard`.\n\n"
        "`!undo`\n"
        "Restore your most recent discard/delete action within 30 minutes.\n\n"
        "`!sort az`\n"
        "Sort occupied inventory slots alphabetically A–Z.\n\n"
        "**🛡️ ADMIN COMMANDS**\n"
        "`!admin-inventory @player`\n"
        "View another tribute's inventory.\n\n"
        "`!admin-add @player item-id[,item-id...]`\n"
        "Add one or more catalog items by unique identifier.\n"
        "Example: `!admin-add @Richard rusty-knife, old-purse`\n\n"
        "`!admin-delete @player <slot[,slot...]>`\n"
        "Delete one or more slots from another tribute's inventory.\n\n"
        "`!admin-discard @player <slot[,slot...]>`\n"
        "Alias for `!admin-delete`.\n\n"
        "`!admin-item-ids`\n"
        "View the item identifiers currently available to admins.\n\n"
        "**⚙️ INVENTORY SETTINGS**\n"
        "`!set-inventory <number>`\n"
        "Set the default inventory size for new tributes in this server.\n"
        f"Current default: **{current_default} slots**\n\n"
        "**ℹ️ NOTES**\n"
        "• Multiple slots or item IDs may be separated by commas.\n"
        "• Spaces around commas are ignored.\n"
        "• Slot `100` is a newly found item waiting for a full inventory to be resolved.\n"
        "• New items fill the lowest available slot."
    )


@bot.command(name="admin-inventory")
async def admin_inventory(ctx, *, target: str = None):
    if ctx.guild is None:
        await ctx.send("This command can only be used inside a server.")
        return

    if not ctx.author.guild_permissions.manage_guild:
        await ctx.send("You need the **Manage Server** permission to use `!admin-inventory`.")
        return

    if target is None:
        await ctx.send("Use `!admin-inventory @username` or `!admin-inventory support`.")
        return

    if target.strip().lower() in {"support", "help", "commands"}:
        await ctx.send("Inventory help moved to `!admin-support inventory`.")
        return

    try:
        member = await commands.MemberConverter().convert(ctx, target.strip())
    except commands.MemberNotFound:
        await ctx.send(
            "I couldn't find that member. Use `!admin-inventory @username` "
            "or `!admin-inventory support`."
        )
        return

    await send_long_message(ctx, format_inventory_message(member))


async def discard_slots_for_member(ctx, member, raw_slots, admin_action=False):
    guild_id = ctx.guild.id
    user_id = member.id
    inventory = get_inventory_items(guild_id, user_id)
    pending = get_pending_item(guild_id, user_id)
    slots, invalid_tokens = parse_inventory_slots(raw_slots)

    if not slots and not invalid_tokens:
        usage = "!admin-discard @username 2,3,5" if admin_action else "!discard 2,3,5"
        await ctx.send(f"Use `{usage}`.")
        return

    if invalid_tokens:
        await ctx.send(
            "⚠️ These entries were not valid slot numbers: "
            + ", ".join(f"`{token}`" for token in invalid_tokens)
        )
        if not slots:
            return

    if pending is not None and len(slots) != 1:
        await ctx.send(
            "You have a new item waiting in slot **100**. Use exactly one slot number "
            "to replace that item, or use `!discard 100` to reject the new item."
        )
        return

    if 100 in slots:
        if len(slots) != 1 or pending is None:
            await ctx.send("Slot **100** can only be discarded by itself when a new item is pending.")
            return
        if not admin_action:
            save_inventory_undo(
                guild_id,
                user_id,
                {"type": "discard_pending", "pending_item": pending},
            )
        clear_pending_item(guild_id, user_id)
        prefix = f"{member.mention}'s " if admin_action else "The "
        await ctx.send(f"🗑️ {prefix}new item in slot **100** was discarded.")
        return

    discarded = []
    ignored = []

    for slot in slots:
        if slot not in inventory:
            ignored.append(slot)
            continue
        discarded.append((slot, inventory[slot]))
        delete_inventory_item(guild_id, user_id, slot)

    if pending is not None:
        slot = slots[0]
        if not discarded:
            await ctx.send(f"Slot **{slot}** is empty or invalid, so the pending item was not moved.")
            return
        save_inventory_item(guild_id, user_id, slot, pending)
        clear_pending_item(guild_id, user_id)

    if discarded and not admin_action:
        save_inventory_undo(
            guild_id,
            user_id,
            {
                "type": "replace_pending" if pending is not None else "discard_slots",
                "discarded": [
                    {"slot": slot, "item": item_data}
                    for slot, item_data in discarded
                ],
                "moved_pending_item": pending,
            },
        )

    lines = []
    if discarded:
        owner = f" from {member.mention}'s inventory" if admin_action else ""
        lines.append("🗑️ Discarded" + owner + ":")
        lines.extend(
            f"• Slot **{slot}** — **{item_data.get('name', 'item')}**"
            for slot, item_data in discarded
        )
    if pending is not None and discarded:
        lines.append(
            f"✅ **{pending.get('name', 'New item')}** was placed into slot **{slots[0]}**."
        )
    if ignored:
        lines.append("⚠️ Ignored empty or invalid slots: " + ", ".join(map(str, ignored)))

    await ctx.send("\n".join(lines) if lines else "No inventory items were discarded.")


@bot.command(name="discard", aliases=["delete"])
async def discard(ctx, *, slots: str = None):
    await discard_slots_for_member(ctx, ctx.author, slots)


@bot.command(name="undo")
async def undo_inventory_discard(ctx):
    if ctx.guild is None:
        await ctx.send("This command can only be used inside a server.")
        return

    guild_id = ctx.guild.id
    user_id = ctx.author.id
    undo_data = get_inventory_undo(guild_id, user_id)

    if undo_data is None:
        await ctx.send(
            "You do not have a recent inventory discard to undo. "
            "Please ping the Gamemakers for assistance."
        )
        return

    elapsed = time.time() - undo_data.get("created_at", 0)
    if elapsed > INVENTORY_UNDO_WINDOW_SECONDS:
        clear_inventory_undo(guild_id, user_id)
        await ctx.send(
            "Your 30-minute undo window has expired. "
            "Please ping the Gamemakers for assistance."
        )
        return

    undo_type = undo_data.get("type")

    if undo_type == "discard_pending":
        if get_pending_item(guild_id, user_id) is not None:
            await ctx.send(
                "I cannot safely restore that item because slot 100 is already occupied. "
                "Please ping the Gamemakers for assistance."
            )
            return
        save_pending_item(guild_id, user_id, undo_data["pending_item"])
        clear_inventory_undo(guild_id, user_id)
        await ctx.send("✅ Your discarded pending item was restored to slot **100**.")
        return

    discarded_entries = undo_data.get("discarded", [])
    if not discarded_entries:
        clear_inventory_undo(guild_id, user_id)
        await ctx.send(
            "That undo record could not be restored. Please ping the Gamemakers for assistance."
        )
        return

    inventory = get_inventory_items(guild_id, user_id)

    if undo_type == "replace_pending":
        original_slot = int(discarded_entries[0]["slot"])
        moved_item = undo_data.get("moved_pending_item")
        current_item = inventory.get(original_slot)

        if current_item != moved_item or get_pending_item(guild_id, user_id) is not None:
            await ctx.send(
                "I cannot safely reverse that replacement because the inventory changed afterward. "
                "Please ping the Gamemakers for assistance."
            )
            return

        delete_inventory_item(guild_id, user_id, original_slot)
        save_inventory_item(
            guild_id, user_id, original_slot, discarded_entries[0]["item"]
        )
        save_pending_item(guild_id, user_id, moved_item)
        clear_inventory_undo(guild_id, user_id)
        await ctx.send(
            f"✅ Slot **{original_slot}** was restored, and the newer item returned to slot **100**."
        )
        return

    bag_total = get_bag_total(guild_id, user_id)
    occupied = set(inventory)
    planned = []

    for entry in discarded_entries:
        preferred_slot = int(entry["slot"])
        destination = None
        if 1 <= preferred_slot <= bag_total and preferred_slot not in occupied:
            destination = preferred_slot
        else:
            for candidate in range(1, bag_total + 1):
                if candidate not in occupied:
                    destination = candidate
                    break

        if destination is None:
            await ctx.send(
                "I cannot safely restore every discarded item because your inventory is now full. "
                "Please ping the Gamemakers for assistance."
            )
            return

        occupied.add(destination)
        planned.append((destination, entry["item"]))

    for destination, item_data in planned:
        save_inventory_item(guild_id, user_id, destination, item_data)

    clear_inventory_undo(guild_id, user_id)
    restored_slots = ", ".join(str(slot) for slot, _ in planned)
    await ctx.send(
        f"✅ Your last discard/delete action was undone. Restored slot(s): **{restored_slots}**."
    )


@bot.command(name="sort")
async def sort_inventory(ctx, mode: str = None):
    if mode is None or clean_name(mode) != "az":
        await ctx.send("Use `!sort az` to sort your inventory alphabetically.")
        return
    if player_has_pending_item(ctx.author):
        await ctx.send("You have to sort out your inventory first!")
        return

    sort_inventory_alphabetically(ctx.guild.id, ctx.author.id)
    await ctx.send("✅ Inventory sorted alphabetically A–Z.")


@bot.command(name="set-inventory")
@commands.has_permissions(manage_guild=True)
async def set_inventory(ctx, number: int = None):
    if number is None:
        current = get_guild_default_bag_total(ctx.guild.id)
        await ctx.send(
            f"The default inventory size for new tributes is **{current}**. "
            "Use `!set-inventory number` to change it."
        )
        return
    if number < 1 or number > 100:
        await ctx.send("Inventory size must be between **1** and **100** slots.")
        return

    set_guild_default_bag_total(ctx.guild.id, number)
    await ctx.send(
        f"✅ The default inventory size for **new tribute records** is now **{number}** slots. "
        "Existing tribute inventories were not resized."
    )


@bot.command(name="admin-discard", aliases=["admin-delete"])
@commands.has_permissions(manage_guild=True)
async def admin_discard(ctx, member: discord.Member = None, *, slots: str = None):
    if member is None or slots is None:
        await ctx.send("Use `!admin-discard @username 2,3,5`.")
        return
    await discard_slots_for_member(ctx, member, slots, admin_action=True)


@bot.command(name="admin-add")
@commands.has_permissions(manage_guild=True)
async def admin_add(ctx, member: discord.Member = None, *, identifiers: str = None):
    if member is None or not identifiers:
        await ctx.send("Use `!admin-add @username item-identifier, another-identifier`.")
        return

    catalog = build_item_identifier_catalog()
    requested = [part.strip().casefold() for part in identifiers.split(",") if part.strip()]
    added = []
    pending_names = []
    unknown = []

    for identifier in requested:
        item_template = catalog.get(identifier)
        if item_template is None:
            unknown.append(identifier)
            continue

        item_data = json.loads(json.dumps(item_template))
        empty_slot = find_first_empty_inventory_slot(ctx.guild.id, member.id)
        if empty_slot is not None:
            save_inventory_item(ctx.guild.id, member.id, empty_slot, item_data)
            added.append((empty_slot, item_data["name"]))
            continue

        if get_pending_item(ctx.guild.id, member.id) is None:
            save_pending_item(ctx.guild.id, member.id, item_data)
            pending_names.append(item_data["name"])
        else:
            unknown.append(f"{identifier} (inventory and pending slot full)")

    lines = []
    if added:
        lines.append(f"✅ Added to {member.mention}'s inventory:")
        lines.extend(f"• Slot **{slot}** — **{name}**" for slot, name in added)
    if pending_names:
        lines.append("📦 Added to pending slot **100**: " + ", ".join(pending_names))
    if unknown:
        lines.append("⚠️ Not added: " + ", ".join(f"`{value}`" for value in unknown))
    await ctx.send("\n".join(lines) if lines else "No items were added.")


@bot.command(name="admin-item-ids")
@commands.has_permissions(manage_guild=True)
async def admin_item_ids(ctx):
    catalog = build_item_identifier_catalog()
    lines = [f"{identifier} — {item['name']}" for identifier, item in sorted(catalog.items())]
    await send_long_message(
        ctx,
        "**Available Item Identifiers**\n```txt\n" + "\n".join(lines) + "\n```",
    )


@bot.command(name="item-cooldown")
@commands.has_permissions(manage_guild=True)
async def item_cooldown(ctx, value: str = None):
    global PLAYER_ACTION_COOLDOWN_SECONDS
    global SLEEP_COOLDOWN_SECONDS
    global INTRA_ISLAND_MOVE_SECONDS

    if value is None:
        value = str(DEFAULT_PLAYER_ACTION_COOLDOWN_SECONDS)

    if clean_name(value) == "reset":
        PLAYER_ACTION_COOLDOWN_SECONDS = DEFAULT_PLAYER_ACTION_COOLDOWN_SECONDS
        SLEEP_COOLDOWN_SECONDS = DEFAULT_SLEEP_COOLDOWN_SECONDS
        INTRA_ISLAND_MOVE_SECONDS = DEFAULT_INTRA_ISLAND_MOVE_SECONDS

        await ctx.send(
            "Global cooldowns reset to default values.\n"
            f"Movement/search/craft: **{format_seconds(PLAYER_ACTION_COOLDOWN_SECONDS)}** "
            f"({PLAYER_ACTION_COOLDOWN_SECONDS} seconds).\n"
            f"Sleep: **{format_seconds(SLEEP_COOLDOWN_SECONDS)}** "
            f"({SLEEP_COOLDOWN_SECONDS} seconds).\n"
            f"Same-island movement: **{format_seconds(INTRA_ISLAND_MOVE_SECONDS)}** "
            f"({INTRA_ISLAND_MOVE_SECONDS} seconds)."
        )
        return

    try:
        seconds = int(value)
    except ValueError:
        await ctx.send("Use a number of seconds, or `!item-cooldown reset`.")
        return

    if seconds < 0:
        await ctx.send("Global action cooldown cannot be negative.")
        return

    PLAYER_ACTION_COOLDOWN_SECONDS = seconds
    SLEEP_COOLDOWN_SECONDS = seconds
    INTRA_ISLAND_MOVE_SECONDS = seconds

    await ctx.send(
        f"Global action cooldown updated to **{format_seconds(seconds)}** "
        f"({seconds} seconds).\n"
        "This now affects same-island movement, island travel, search, craft, and sleep."
    )

@bot.command(name="item-table")
@commands.has_permissions(manage_guild=True)
async def item_table(ctx):
    message = "**Item Loot Table**\n\n"
    message += "```txt\n"
    message += (
        f"{'Roll':<5} "
        f"{'Item':<25} "
        f"{'Amount':<12} "
        f"Description\n"
    )
    message += "-" * 95 + "\n"

    for roll in range(1, 41):
        item_data = ITEM_LOOT_TABLE[roll]

        wrapped_description = textwrap.wrap(
            item_data["description"],
            width=40
        )

        if not wrapped_description:
            wrapped_description = [""]

        # First line
        message += (
            f"{roll:<5} "
            f"{item_data['item']:<25} "
            f"{item_data['amount']:<12} "
            f"{wrapped_description[0]}\n"
        )

        # Additional wrapped lines
        for line in wrapped_description[1:]:
            message += (
                f"{'':<5} "
                f"{'':<25} "
                f"{'':<12} "
                f"{line}\n"
            )

    message += "```\n\n"

    message += "**Island-Specific Rolls**\n\n"
    message += "```txt\n"
    message += (
        f"{'Roll':<5} "
        f"{'Ember Island':<24} "
        f"{'Skyspire Ruins':<24} "
        f"{'Verdant Veil':<18} "
        f"{'Frostcrest':<18} "
        f"{'Compass Core / Other'}\n"
    )
    message += "-" * 120 + "\n"

    for roll in [11, 12, 13]:
        message += (
            f"{roll:<5} "
            f"{ISLAND_SPECIFIC_ITEMS['Ember Island'][roll]:<24} "
            f"{ISLAND_SPECIFIC_ITEMS['Skyspire Ruins'][roll]:<24} "
            f"{ISLAND_SPECIFIC_ITEMS['Verdant Veil'][roll]:<18} "
            f"{ISLAND_SPECIFIC_ITEMS['Frostcrest'][roll]:<18} "
            f"{ISLAND_FALLBACK_ITEMS[roll]}\n"
        )

    message += "```"

    await send_long_message(ctx, message)

@bot.command(name="search-food")
async def search_food(ctx):
    member = ctx.author

    if await send_inventory_sort_block_message(ctx, member):
        return

    if await send_current_action_block_message(ctx, member):
        return

    search_id = f"{member.id}-{time.time()}"

    start_player_action(
        member,
        "search-food",
        PLAYER_ACTION_COOLDOWN_SECONDS,
        {"search_id": search_id},
    )

    await ctx.send(
        f"You are currently searching for food. It will take another "
        f"{format_minutes_seconds(PLAYER_ACTION_COOLDOWN_SECONDS)}."
    )

    asyncio.create_task(
        resolve_search_food_after_timer(ctx, member, search_id)
    )


@bot.command(name="Search-Food-Bypass")
@commands.has_permissions(manage_guild=True)
async def search_food_bypass(ctx):
    member = ctx.author

    roll = random.randint(1, 33)
    food_data = get_food_for_roll(member, roll)

    await ctx.send(format_food_found_message(member, roll, food_data, bypass=True))



@bot.command(name="search-water")
async def search_water(ctx):
    member = ctx.author

    if await send_inventory_sort_block_message(ctx, member):
        return

    if await send_current_action_block_message(ctx, member):
        return

    search_id = f"{member.id}-{time.time()}"

    start_player_action(
        member,
        "search-water",
        PLAYER_ACTION_COOLDOWN_SECONDS,
        {"search_id": search_id},
    )

    await ctx.send(
        f"You are currently searching for water. It will take another "
        f"{format_minutes_seconds(PLAYER_ACTION_COOLDOWN_SECONDS)}."
    )

    asyncio.create_task(
        resolve_search_water_after_timer(ctx, member, search_id)
    )


@bot.command(name="craft")
async def craft(ctx):
    member = ctx.author

    if await send_inventory_sort_block_message(ctx, member):
        return

    if await send_current_action_block_message(ctx, member):
        return

    craft_id = f"{member.id}-{time.time()}"

    start_player_action(
        member,
        "craft",
        PLAYER_ACTION_COOLDOWN_SECONDS,
        {"craft_id": craft_id},
    )

    await ctx.send(
        f"You are currently crafting. It will take another "
        f"{format_minutes_seconds(PLAYER_ACTION_COOLDOWN_SECONDS)}."
    )

    asyncio.create_task(
        resolve_craft_after_timer(ctx, member, craft_id)
    )


@bot.command(name="sleep")
async def sleep(ctx):
    member = ctx.author

    if await send_inventory_sort_block_message(ctx, member):
        return

    if await send_current_action_block_message(ctx, member):
        return

    sleep_id = f"{member.id}-{time.time()}"

    start_player_action(
        member,
        "sleep",
        SLEEP_COOLDOWN_SECONDS,
        {"sleep_id": sleep_id},
    )

    await ctx.send(
        f"{member.mention} goes to sleep. It will take another "
        f"{format_minutes_seconds(SLEEP_COOLDOWN_SECONDS)}."
    )

    asyncio.create_task(
        resolve_sleep_after_timer(ctx, member, sleep_id)
    )



@bot.command(name="Admin-Disable")
@commands.has_permissions(manage_guild=True)
async def admin_disable(ctx, *, room_name: str):
    real_name = get_real_location_name(room_name)

    if real_name is None:
        await ctx.send("That room does not exist.")
        return

    ROOM_STATUS[real_name.lower()] = False

    await ctx.send(f"**{real_name}** has been disabled.")


@bot.command(name="Admin-Enable")
@commands.has_permissions(manage_guild=True)
async def admin_enable(ctx, *, room_name: str):
    real_name = get_real_location_name(room_name)

    if real_name is None:
        await ctx.send("That room does not exist.")
        return

    ROOM_STATUS[real_name.lower()] = True

    await ctx.send(f"**{real_name}** has been enabled.")


@bot.command(name="Admin-Room-Status")
@commands.has_permissions(manage_guild=True)
async def admin_room_status(ctx):
    message = "**Room Status**\n\n"

    for category, rooms in LOCATIONS.items():
        message += f"**{category}**\n"

        for room_name in rooms:
            status = (
                "Enabled"
                if ROOM_STATUS[room_name.lower()]
                else "Disabled"
            )

            message += f"- {room_name}: {status}\n"

        message += "\n"

    await ctx.send(message)


@bot.command(name="Admin-Bridge-Enable")
@commands.has_permissions(manage_guild=True)
async def admin_bridge_enable(ctx, *, bridge: str):
    if "->" not in bridge:
        await ctx.send(
            "Use format: !Admin-Bridge-Enable Category One -> Category Two"
        )
        return

    category_one_input, category_two_input = [
        part.strip()
        for part in bridge.split("->", 1)
    ]

    category_one = get_real_category_name(category_one_input)
    category_two = get_real_category_name(category_two_input)

    if category_one is None or category_two is None:
        await ctx.send("One or both categories do not exist.")
        return

    bridge_key = frozenset([category_one, category_two])

    if bridge_key not in BRIDGE_PATHS:
        await ctx.send("That bridge path does not exist.")
        return

    BRIDGE_PATHS[bridge_key] = True

    await ctx.send(
        f"Bridge path between **{category_one}** and "
        f"**{category_two}** has been enabled."
    )


@bot.command(name="Admin-Bridge-Disable")
@commands.has_permissions(manage_guild=True)
async def admin_bridge_disable(ctx, *, bridge: str):
    if "->" not in bridge:
        await ctx.send(
            "Use format: !Admin-Bridge-Disable Category One -> Category Two"
        )
        return

    category_one_input, category_two_input = [
        part.strip()
        for part in bridge.split("->", 1)
    ]

    category_one = get_real_category_name(category_one_input)
    category_two = get_real_category_name(category_two_input)

    if category_one is None or category_two is None:
        await ctx.send("One or both categories do not exist.")
        return

    bridge_key = frozenset([category_one, category_two])

    if bridge_key not in BRIDGE_PATHS:
        await ctx.send("That bridge path does not exist.")
        return

    BRIDGE_PATHS[bridge_key] = False

    await ctx.send(
        f"Bridge path between **{category_one}** and "
        f"**{category_two}** has been disabled."
    )


@bot.command(name="Admin-Bridge-Status")
@commands.has_permissions(manage_guild=True)
async def admin_bridge_status(ctx):
    message = "**Bridge Path Status**\n\n"

    for bridge, enabled in BRIDGE_PATHS.items():
        category_one, category_two = sorted(list(bridge))

        status = "Enabled" if enabled else "Disabled"

        message += f"- {category_one} <-> {category_two}: {status}\n"

    await ctx.send(message)


@bot.command(name="Admin-Broadcast")
@commands.has_permissions(manage_guild=True)
async def admin_broadcast(ctx):
    rows = []

    for member in ctx.guild.members:
        if member.bot:
            continue

        if not is_tribute(member):
            continue

        member_location = None
        member_island = None
        member_ailments = get_member_ailments(member)

        for role in member.roles:
            if clean_name(role.name) == clean_name(TRAVELING_ROLE):
                member_location = TRAVELING_ROLE
                member_island = TRAVELING_ROLE
                break

            for official_location in get_all_location_role_names():
                if clean_name(role.name) == clean_name(official_location):
                    member_location = official_location
                    member_island = LOCATION_TO_CATEGORY[official_location.lower()]
                    break

            if member_location:
                break

        if member_location:
            if member_location == TRAVELING_ROLE:
                location_line = get_member_travel_status_text(member)
            else:
                time_here = get_member_location_duration_hhmmss(
                    member,
                    member_location,
                    start_tracking_if_missing=True,
                )
                location_line = f"{member_location} — Time here: **{time_here}**"

            rows.append({
                "user": member.display_name,
                "district": get_member_district(member),
                "location": member_location,
                "island": member_island,
                "ailments": member_ailments,
                "location_line": location_line,
            })

    if not rows:
        await ctx.send("No Tributes currently have a room or traveling role.")
        return

    island_order = {
        "Ember Island": 1,
        "Skyspire Ruins": 2,
        "Verdant Veil": 3,
        "Frostcrest": 4,
        "Compass Core": 5,
        "Traveling": 6,
    }

    rows.sort(
        key=lambda row: (
            island_order.get(row["island"], 99),
            row["island"],
            row["user"].lower(),
        )
    )

    island_icons = {
        "Ember Island": "🔥",
        "Skyspire Ruins": "🏛️",
        "Verdant Veil": "🌳",
        "Frostcrest": "❄️",
        "Compass Core": "🧭",
        "Traveling": "🚶",
    }

    message = "**Room Members — Tributes Only**\n\n"
    current_section = None

    for row in rows:
        icon = island_icons.get(row["island"], "📍")

        if row["island"] != current_section:
            current_section = row["island"]
            message += f"## {icon} {current_section}\n"

        message += (
            f"**{row['user']}** — {row['district']} — {row['ailments']}\n"
            f"{row['location_line']}\n\n"
        )

    await send_long_message(ctx, message)



@bot.command(name="Admin-Support")
@commands.has_permissions(manage_guild=True)
async def admin_support(ctx, *, topic: str = None):
    if topic is not None:
        cleaned_topic = clean_name(topic)
        if cleaned_topic in {"inventory", "inventories", "inv"}:
            await send_long_message(
                ctx,
                format_admin_inventory_support_message(ctx.guild.id),
            )
            return

        await ctx.send(
            "That admin support section does not exist yet. "
            "Available section: `!admin-support inventory`."
        )
        return

    sections = {
        "PLAYER COMMANDS": [
            ("!move Room Name", "Move rooms. Same-island movement defaults to 30 minutes; island travel uses the global cooldown."),
            ("!search-item", "Search for items using the shared action timer."),
            ("!search-food", "Search for food using the shared action timer."),
            ("!search-water", "Search for water using the shared action timer."),
            ("!craft", "Craft using the shared action timer."),
            ("!sleep", "Sleep using the current global cooldown and receive +3 sleep."),
            ("!reset-action", "Cancels your own active action. Tributes cannot reset another player."),
        ],

        "ADMIN SEARCH COMMANDS": [
            ("!search-item-bypass", "Instant item roll that ignores cooldown."),
            ("!search-food-bypass", "Instant food roll that ignores cooldown."),
            ("!reset-action @member", "Clears another player's active action. Manage Server permission required."),
        ],

        "ADMIN SETTINGS": [
            ("!item-cooldown seconds", "Updates the global cooldown for movement, searching, crafting, and sleep."),
            ("!item-cooldown reset", "Resets movement/search/craft, sleep, and same-island movement cooldowns to defaults."),
        ],

        "ADMIN TABLES": [
            ("!item-table", "Displays the item loot table."),
        ],

        "ADMIN ROOM CONTROLS": [
            ("!Admin-Disable Room Name", "Disables a room."),
            ("!Admin-Enable Room Name", "Enables a room."),
            ("!Admin-Room-Status", "Displays room statuses."),
            ("!Admin-Broadcast", "Shows all Tribute locations and ailments."),
        ],

        "ADMIN BRIDGE CONTROLS": [
            ("!Admin-Bridge-Enable Island A -> Island B", "Enables a bridge."),
            ("!Admin-Bridge-Disable Island A -> Island B", "Disables a bridge."),
            ("!Admin-Bridge-Status", "Displays bridge statuses."),
        ],

        "UTILITY": [
            ("!ping", "Tests bot connection."),
            ("!roles", "Displays all Discord roles."),
            ("!Admin-Support", "Displays this general admin menu."),
            ("!Admin-Support Inventory", "Displays the mobile-friendly inventory support menu."),
        ],
    }

    message = '**Admin Support Commands**' + chr(10) + chr(10)

    for section_name, commands_list in sections.items():
        message += f"**{section_name}**" + chr(10)
        message += "```txt" + chr(10)
        message += f"{'Command':<40} Description" + chr(10)
        message += "-" * 90 + chr(10)

        for command_name, description in commands_list:
            message += f"{command_name:<40} {description}" + chr(10)

        message += "```" + chr(10) + chr(10)

    await send_long_message(ctx, message)



@bot.command(name="reset-action")
async def reset_action(ctx, member: discord.Member = None):
    # Everyone may reset their own action. Resetting another player is restricted
    # to members with the Manage Server permission.
    if member is not None and member.id != ctx.author.id:
        if not ctx.author.guild_permissions.manage_guild:
            await ctx.send("❌ You can only reset your own active action.")
            return
        target = member
    else:
        target = ctx.author

    active_action = get_player_action(target)
    tracked_action = ACTIVE_ACTION_TASKS.get(target.id)

    if active_action is None and tracked_action is None:
        if target.id == ctx.author.id:
            await ctx.send("You aren't currently performing an action.")
        else:
            await ctx.send(f"{target.mention} isn't currently performing an action.")
        return

    original_end_time = active_action.get("end_time", time.time()) if active_action else time.time()
    original_action_type = active_action.get("action_type") if active_action else None
    original_data = active_action.get("data", {}) if active_action else {}

    cancel_tracked_action_task(target)

    # Movement removes the tribute's location role while they travel. Cancelling the
    # movement must return them to their point of origin rather than leaving them
    # with no location role, which would otherwise allow an instant move.
    if original_action_type in {"room", "frostcrest-yeti"}:
        origin_name = original_data.get("origin")
        traveling_role = get_role_case_insensitive(ctx.guild, TRAVELING_ROLE)

        await remove_location_roles_only(target)

        if traveling_role is not None and traveling_role in target.roles:
            await target.remove_roles(traveling_role)

        if origin_name:
            origin_role = get_role_case_insensitive(ctx.guild, origin_name)
            if origin_role is not None:
                await target.add_roles(origin_role)

    remaining = max(0, original_end_time - time.time())

    # Resetting cancels the result of the action, but it does not erase the time
    # commitment. The tribute must wait out the original cooldown before starting
    # another movement, search, craft, or sleep action.
    if remaining > 0:
        PLAYER_ACTION_COOLDOWNS[target.id] = {
            "action_type": "cancelled-cooldown",
            "end_time": original_end_time,
            "data": {"cancelled_action_type": original_action_type},
        }
    else:
        PLAYER_ACTION_COOLDOWNS.pop(target.id, None)

    cooldown_text = (
        f" The original cooldown remains active for {format_minutes_seconds(remaining)}."
        if remaining > 0
        else ""
    )

    if target.id == ctx.author.id:
        await ctx.send(f"✅ Your current action has been cancelled.{cooldown_text}")
    else:
        await ctx.send(
            f"✅ {target.mention}'s current action has been cancelled.{cooldown_text}"
        )




initialize_database()

if TOKEN is None:
    raise ValueError("Missing DISCORD_TOKEN environment variable.")

bot.run(TOKEN)
