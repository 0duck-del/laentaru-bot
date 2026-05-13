import json
import os
import random
import asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")
if Path(".env").exists():
    for line in Path(".env").read_text().splitlines():
        if line.strip().startswith("DISCORD_TOKEN=") and not TOKEN:
            TOKEN = line.split("=", 1)[1].strip().strip("\'").strip('"')

# -----------------------------
# Persistent Data Storage
# -----------------------------
# Railway's new volume mount exposes the path through RAILWAY_VOLUME_MOUNT_PATH.
# If Railway does not provide it, this falls back to /data, then finally local storage.
# Your live player data should live on the mounted volume, not inside the GitHub repo.

def get_data_dir():
    preferred_paths = [
        os.getenv("RAILWAY_VOLUME_MOUNT_PATH"),
        "/data",
        ".",
    ]

    for raw_path in preferred_paths:
        if not raw_path:
            continue

        path = Path(raw_path)

        try:
            path.mkdir(parents=True, exist_ok=True)
            test_file = path / ".write_test"
            test_file.write_text("ok", encoding="utf-8")
            test_file.unlink(missing_ok=True)
            return path
        except Exception:
            continue

    return Path(".")


DATA_DIR = get_data_dir()
DATA_FILE = DATA_DIR / "players.json"
LOTTERY_FILE = DATA_DIR / "lottery.json"
CONFIG_FILE = Path("config.json")

if not DATA_FILE.exists():
    DATA_FILE.write_text("{}", encoding="utf-8")

print(f"Using player data file: {DATA_FILE.resolve()}")
def load_config():
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(
            "Missing config.json. Keep config.json in the same folder as bot.py."
        )

    with open(CONFIG_FILE, "r") as file:
        return json.load(file)



def validate_config(config):
    required = ["BASE_STATS", "AFFINITIES", "NATURE_TYPES", "CLANS", "JUTSU", "RANKS", "DEVELOPER_IDS"]
    missing = [key for key in required if key not in config]
    if missing:
        raise ValueError(f"config.json is missing required keys: {', '.join(missing)}")
    if "TURN_PVP" not in config and isinstance(config.get("JUTSU"), dict) and "TURN_PVP" in config["JUTSU"]:
        raise ValueError("TURN_PVP is currently inside JUTSU. Move TURN_PVP to the root of config.json.")
    return True

CONFIG = load_config()
validate_config(CONFIG)
BOT_VERSION = CONFIG.get("BOT_VERSION", "1.4")
REROLLS_PER_PLAYER = int(CONFIG.get("REROLLS_PER_PLAYER", 3))

# Everything below is sourced from config.json so you can balance the bot without editing code.
TRAINING_COOLDOWN_MINUTES = CONFIG.get("TRAINING_COOLDOWN_MINUTES", 30)
DUEL_DURATION_SECONDS = CONFIG.get("DUEL_DURATION_SECONDS", 30)
DUEL_ROUND_DELAY_SECONDS = CONFIG.get("DUEL_ROUND_DELAY_SECONDS", 1.5)
BASE_STATS = CONFIG.get("BASE_STATS", {})
AFFINITIES = CONFIG.get("AFFINITIES", [])
NATURE_TYPES = CONFIG.get("NATURE_TYPES", [])
KEKKEI_GENKAI = CONFIG.get("KEKKEI_GENKAI", [])
ITEMS = CONFIG.get("ITEMS", {})
JUTSU = CONFIG.get("JUTSU", {})
VILLAGES = CONFIG.get("VILLAGES", {})
RANKS = CONFIG.get("RANKS", [])
LEVELS_PER_RANK = CONFIG.get("LEVELS_PER_RANK", 10)
XP_BASE = CONFIG.get("XP_BASE", 100)
XP_EXPONENT = CONFIG.get("XP_EXPONENT", 1.35)
DEV_USER_IDS = set(CONFIG.get("DEVELOPER_IDS", []))

DEV_MODE_STAT_VALUE = 5_000_000
DEV_MODE_AFFINITY_VALUE = 5_000_000
DEV_MODE_LEVEL = 100
DEV_MODE_RANK = "Tsuchi Clan Founder"
DEV_MODE_CLAN = "Tsuchi"
DEV_MODE_KEKKEI_GENKAI = "Maddosukin"
ACTIVE_EVENT = None
ACTIVE_TOURNAMENT = None

DUEL_REQUESTS = {}
ACTIVE_DUELS = {}
ACTIVE_TRADES = {}
RANDOM_EVENT_TASK_STARTED = False
LOTTERY_TASK_STARTED = False

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="$", intents=intents, help_command=None)


# -----------------------------
# Discord UI / Embed Utilities
# -----------------------------
THEME = {
    "brand": 0xF97316,
    "success": 0x22C55E,
    "danger": 0xEF4444,
    "warning": 0xEAB308,
    "info": 0x38BDF8,
    "neutral": 0x2F3136,
    "purple": 0xA855F7,
    "gold": 0xF59E0B,
}

ICONS = {
    "level": "⭐",
    "rank": "🎖️",
    "ryo": "💰",
    "clan": "🏯",
    "village": "🌍",
    "bloodline": "🧬",
    "nature": "🌪️",
    "jutsu": "📜",
    "inventory": "🎒",
    "combat": "⚔️",
    "training": "🏋️",
    "event": "🎁",
    "dev": "🛠️",
}


def ui_embed(title, description=None, tone="brand"):
    embed = discord.Embed(
        title=title,
        description=description or "",
        color=THEME.get(tone, THEME["brand"])
    )
    embed.set_footer(text=f"Laentaru Bot v{BOT_VERSION} • Use $help for commands")
    return embed


def fmt_num(value):
    try:
        return f"{int(value):,}"
    except Exception:
        return str(value)


def truncate_text(value, limit=1024):
    value = str(value or "")
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)] + "..."


def compact_list(items, empty="None", limit=8):
    items = list(items or [])
    if not items:
        return empty
    shown = items[:limit]
    text = ", ".join(str(item) for item in shown)
    if len(items) > limit:
        text += f" +{len(items) - limit} more"
    return text


def progress_bar(current, maximum, size=10):
    maximum = max(1, int(maximum or 1))
    current = max(0, min(int(current or 0), maximum))
    filled = round((current / maximum) * size)
    return "█" * filled + "░" * (size - filled)


def resource_line(label, current, maximum):
    return f"**{label}:** `{progress_bar(current, maximum)}` {fmt_num(current)}/{fmt_num(maximum)}"


def kv_line(label, value):
    return f"**{label}:** {value}"


def add_field(embed, name, value, inline=False):
    embed.add_field(name=name, value=truncate_text(value or "None"), inline=inline)
    return embed




async def send_notice(ctx, title, description=None, tone="info"):
    embed = ui_embed(title, description or "", tone)
    await ctx.send(embed=embed)


def build_notice_embed(title, description=None, tone="info"):
    return ui_embed(title, description or "", tone)


def command_usage(command, description):
    return f"`{command}`\n{description}"


def duel_embed(title, duel, description=None, tone="brand"):
    embed = ui_embed(title, description or "", tone)
    add_field(embed, "Battlefield", build_duel_status_text(duel), False)
    current = duel.get("turn")
    if current:
        add_field(embed, "Current Turn", get_duel_member_text(duel, current), False)
    add_field(embed, "Moves", "`$attack` • `$heavy` • `$taijutsu` • `$defend` • `$genjutsu` • `$transformation` • `$jutsu [name]` • `$forfeit`", False)
    return embed


def result_embed(title, lines, tone="success"):
    return ui_embed(title, "\n".join(str(line) for line in lines if line), tone)



def event_config(event_type):
    return CONFIG.get("EVENTS", {}).get(event_type, {})


def event_title(event_type):
    titles = {
        "beast": "Tailed-Beast Capture",
        "shinobi": "Shinobi Group Fight",
        "cursemark": "Curse Mark Event",
        "xp": "Random XP Event",
    }
    return titles.get(event_type, "World Event")


def build_event_embed(title, description, event_type="event", tone="brand"):
    embed = ui_embed(title, description, tone)
    embed.set_footer(text=f"Laentaru Bot v{BOT_VERSION} • World Events")
    return embed


def calculate_event_win_chance(player_power, enemy_power, event_type):
    config = event_config(event_type)
    base = float(config.get("base_win_chance", 0.35))
    weight = float(config.get("power_difference_weight", 0.25))
    minimum = float(config.get("min_win_chance", 0.05))
    maximum = float(config.get("max_win_chance", 0.85))
    enemy_power = max(1, int(enemy_power or 1))
    chance = base + ((player_power - enemy_power) / enemy_power) * weight
    return clamp(chance, minimum, maximum)


def format_percent(value):
    return f"{round(float(value) * 100)}%"


def event_join_instruction(event_type):
    if event_type == "beast":
        return "First valid player to use `$event` becomes the challenger."
    if event_type == "shinobi":
        return "Use `$event` to join the group fight before it closes."
    return "Use `$event` before the event closes."


def now_utc():
    return datetime.now(timezone.utc)


def load_players():
    if not DATA_FILE.exists():
        DATA_FILE.write_text("{}", encoding="utf-8")
        return {}

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as file:
            return json.load(file)
    except json.JSONDecodeError:
        backup_file = DATA_FILE.with_suffix(f".broken-{int(datetime.now().timestamp())}.json")
        DATA_FILE.replace(backup_file)
        DATA_FILE.write_text("{}", encoding="utf-8")
        print(f"WARNING: players.json was invalid JSON. Backed it up to {backup_file}")
        return {}


def save_players(players):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_file = DATA_FILE.with_suffix(".json.tmp")
    with open(tmp_file, "w", encoding="utf-8") as file:
        json.dump(players, file, indent=4)
    tmp_file.replace(DATA_FILE)


# -----------------------------
# Hourly Lottery System
# -----------------------------

def get_lottery_config():
    return CONFIG.get("LOTTERY", {})


def is_lottery_enabled():
    return bool(get_lottery_config().get("enabled", True))


def get_lottery_ticket_price():
    return int(get_lottery_config().get("ticket_price", 10))


def get_lottery_interval_seconds():
    return int(get_lottery_config().get("draw_interval_seconds", 3600))


def get_lottery_channel_id():
    lottery_channel = get_lottery_config().get("channel_id")
    if lottery_channel:
        return int(lottery_channel)
    return int(CONFIG.get("RANDOM_EVENTS", {}).get("channel_id", 0) or 0)


def get_default_lottery_state():
    return {
        "pot": int(get_lottery_config().get("starting_pot", 0)),
        "tickets": {},
        "round_started_at": now_utc().isoformat(),
        "last_draw_at": None,
        "total_rounds": 0,
        "last_winner_id": None,
        "last_winner_tickets": 0,
        "last_prize": 0,
    }


def load_lottery():
    if not LOTTERY_FILE.exists():
        state = get_default_lottery_state()
        save_lottery(state)
        return state

    try:
        with open(LOTTERY_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)
    except json.JSONDecodeError:
        backup_file = LOTTERY_FILE.with_suffix(f".broken-{int(datetime.now().timestamp())}.json")
        LOTTERY_FILE.replace(backup_file)
        state = get_default_lottery_state()
        save_lottery(state)
        print(f"WARNING: lottery.json was invalid JSON. Backed it up to {backup_file}")
        return state

    if not isinstance(state, dict):
        state = {}

    defaults = get_default_lottery_state()
    for key, value in defaults.items():
        state.setdefault(key, value)
    if not isinstance(state.get("tickets"), dict):
        state["tickets"] = {}
    state["pot"] = int(state.get("pot", 0) or 0)
    return state


def save_lottery(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_file = LOTTERY_FILE.with_suffix(".json.tmp")
    with open(tmp_file, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=4)
    tmp_file.replace(LOTTERY_FILE)


def get_lottery_total_tickets(state):
    return sum(int(amount or 0) for amount in state.get("tickets", {}).values())


def get_next_lottery_draw_text(state):
    started_raw = state.get("round_started_at")
    try:
        started = datetime.fromisoformat(started_raw)
    except Exception:
        started = now_utc()
    next_draw = started + timedelta(seconds=get_lottery_interval_seconds())
    remaining = max(0, int((next_draw - now_utc()).total_seconds()))
    minutes = remaining // 60
    seconds = remaining % 60
    return f"{minutes}m {seconds}s"


def choose_lottery_winner(tickets):
    pool = []
    for user_id, amount in tickets.items():
        amount = int(amount or 0)
        if amount > 0:
            pool.extend([str(user_id)] * amount)
    if not pool:
        return None
    return random.choice(pool)


async def lottery_loop():
    await bot.wait_until_ready()

    if not is_lottery_enabled():
        return

    # Start checking often, but only draw when the configured interval has passed.
    while not bot.is_closed():
        try:
            state = load_lottery()
            started_raw = state.get("round_started_at")
            try:
                started = datetime.fromisoformat(started_raw)
            except Exception:
                state["round_started_at"] = now_utc().isoformat()
                save_lottery(state)
                started = now_utc()

            if now_utc() >= started + timedelta(seconds=get_lottery_interval_seconds()):
                await resolve_lottery_round()
        except Exception as exc:
            print(f"Lottery loop error: {exc}")

        await asyncio.sleep(60)


async def resolve_lottery_round():
    state = load_lottery()
    tickets = state.get("tickets", {})
    total_tickets = get_lottery_total_tickets(state)
    prize = int(state.get("pot", 0) or 0)

    channel = None
    channel_id = get_lottery_channel_id()
    if channel_id:
        channel = bot.get_channel(channel_id)

    if total_tickets <= 0 or prize <= 0:
        state = get_default_lottery_state()
        state["last_draw_at"] = now_utc().isoformat()
        save_lottery(state)
        if channel and get_lottery_config().get("announce_empty_rounds", True):
            embed = ui_embed("Hourly Lottery", "No tickets were purchased this round, so the lottery has reset.", "neutral")
            add_field(embed, "Ticket Price", f"**{fmt_num(get_lottery_ticket_price())} Ryo** each", True)
            add_field(embed, "Next Draw", "About **1 hour**", True)
            await channel.send(embed=embed)
        return

    winner_id = choose_lottery_winner(tickets)
    players = migrate_all_players(load_players())

    if winner_id and winner_id in players:
        players[winner_id]["ryo"] = int(players[winner_id].get("ryo", 0)) + prize
        players[winner_id]["lottery_wins"] = int(players[winner_id].get("lottery_wins", 0)) + 1
        players[winner_id]["lottery_ryo_won"] = int(players[winner_id].get("lottery_ryo_won", 0)) + prize
        save_players(players)

    winner_tickets = int(tickets.get(winner_id, 0) or 0) if winner_id else 0
    old_rounds = int(state.get("total_rounds", 0) or 0)

    state = get_default_lottery_state()
    state["last_draw_at"] = now_utc().isoformat()
    state["total_rounds"] = old_rounds + 1
    state["last_winner_id"] = winner_id
    state["last_winner_tickets"] = winner_tickets
    state["last_prize"] = prize
    save_lottery(state)

    if channel:
        if winner_id and winner_id in players:
            embed = ui_embed("Hourly Lottery Winner", f"<@{winner_id}> won the hourly lottery!", "gold")
            add_field(embed, "Prize", f"**{fmt_num(prize)} Ryo**", True)
            add_field(embed, "Winning Tickets", f"**{fmt_num(winner_tickets)}** / {fmt_num(total_tickets)} total", True)
            add_field(embed, "Next Round", f"Tickets are open again. Use `$ticket [amount]` to buy in for **{fmt_num(get_lottery_ticket_price())} Ryo** each.", False)
            await channel.send(embed=embed)
        else:
            await channel.send("🎟️ Lottery draw failed because the winning player profile no longer exists. The pot has reset.")


def migrate_player(player):
    """Keeps older players.json files compatible with the new version."""
    player.setdefault("level", 1)
    player.setdefault("xp", 0)
    player.setdefault("rank", get_ninja_rank(player["level"]))
    player.setdefault("stats", BASE_STATS.copy())
    for stat_name, stat_value in BASE_STATS.items():
        player["stats"].setdefault(stat_name, stat_value)
    player.setdefault("affinities", None)
    player.setdefault("inventory", {})
    player.setdefault("clan", None)
    player.setdefault("devmode_enabled", False)
    player.setdefault("hidden_skill_score", 0)
    player.setdefault("has_rolled", False)
    player.setdefault("last_training", None)
    player.setdefault("known_jutsu", [])
    player.setdefault("village", None)
    player.setdefault("rerolls_remaining", REROLLS_PER_PLAYER)
    player.setdefault("ryo", CONFIG.get("ECONOMY", {}).get("starting_ryo", 0))
    player.setdefault("daily_streak", 0)
    player.setdefault("weekly_streak", 0)
    player.setdefault("best_daily_streak", player.get("daily_streak", 0))
    player.setdefault("best_weekly_streak", player.get("weekly_streak", 0))
    player.setdefault("last_daily", None)
    player.setdefault("last_weekly", None)
    player.setdefault("total_daily_claims", 0)
    player.setdefault("total_weekly_claims", 0)
    player.setdefault("gacha_rolls", 0)
    player.setdefault("gacha_pity", 0)
    player.setdefault("gacha_mythic_pulls", 0)
    player.setdefault("gacha_legendary_pulls", 0)
    player.setdefault("gacha_mythic_pity", 0)
    player.setdefault("missions_completed", 0)
    player.setdefault("mission_cooldowns", {})
    player.setdefault("kekkei_genkai_list", [])
    player.setdefault("kekkei_evolution", {})
    player.setdefault("bloodline_fragments", {})
    sync_player_bloodlines(player)

    if isinstance(player.get("inventory"), list):
        new_inventory = {}
        for item in player["inventory"]:
            new_inventory[item] = new_inventory.get(item, 0) + 1
        player["inventory"] = new_inventory

    if "chakra_natures" not in player:
        chakra_natures = []

        if player.get("chakra_nature"):
            chakra_natures.append(player["chakra_nature"])

        if player.get("dual_nature") and player["dual_nature"] not in chakra_natures:
            chakra_natures.append(player["dual_nature"])

        player["chakra_natures"] = chakra_natures

    player.pop("chakra_nature", None)
    player.pop("dual_nature", None)

    if player.get("affinities"):
        player["hidden_skill_score"] = calculate_hidden_skill_score(player)

    return player


def migrate_all_players(players):
    for user_id in list(players.keys()):
        players[user_id] = migrate_player(players[user_id])
    return players


def get_player_bloodlines(player):
    """Returns every Kekkei Genkai / dojutsu the player owns.

    Backwards compatible with the old single `kekkei_genkai` field.
    """
    if not isinstance(player, dict):
        return []
    bloodlines = []
    legacy = player.get("kekkei_genkai")
    if legacy:
        bloodlines.append(legacy)
    for bloodline in player.get("kekkei_genkai_list", []) or []:
        if bloodline and bloodline not in bloodlines:
            bloodlines.append(bloodline)
    return bloodlines


def sync_player_bloodlines(player):
    bloodlines = get_player_bloodlines(player)
    player["kekkei_genkai_list"] = bloodlines
    player["kekkei_genkai"] = bloodlines[0] if bloodlines else None
    player.setdefault("kekkei_evolution", {})
    for bloodline in bloodlines:
        player["kekkei_evolution"].setdefault(bloodline, 0)
    return bloodlines


def has_player_bloodline(player, bloodline_name):
    return bool(bloodline_name) and bloodline_name in get_player_bloodlines(player)


def find_owned_bloodline(player, search_text=None):
    bloodlines = get_player_bloodlines(player)
    if not bloodlines:
        return None
    if not search_text:
        return bloodlines[0]
    cleaned = str(search_text).lower().strip()
    for bloodline in bloodlines:
        if bloodline.lower() == cleaned:
            return bloodline
    for bloodline in bloodlines:
        if cleaned in bloodline.lower():
            return bloodline
    return None


def add_player_bloodline(player, bloodline_name, apply_modifiers=True):
    if not bloodline_name:
        return False
    bloodlines = get_player_bloodlines(player)
    if bloodline_name in bloodlines:
        sync_player_bloodlines(player)
        return False
    bloodlines.append(bloodline_name)
    player["kekkei_genkai_list"] = bloodlines
    player["kekkei_genkai"] = player.get("kekkei_genkai") or bloodline_name
    player.setdefault("kekkei_evolution", {})[bloodline_name] = 0
    if bloodline_name == "Sharingan":
        player.setdefault("sharingan_level", 0)
    if apply_modifiers:
        modifier = get_trait_modifier("kekkei_genkai", bloodline_name) if "get_trait_modifier" in globals() else {}
        apply_trait_delta(player.setdefault("stats", BASE_STATS.copy()), modifier.get("stats", {}))
        apply_trait_delta(player.setdefault("affinities", {}), modifier.get("skills", {}))
    player["hidden_skill_score"] = calculate_hidden_skill_score(player) if player.get("affinities") else 0
    return True


def format_player_bloodlines(player, limit=8):
    bloodlines = get_player_bloodlines(player)
    if not bloodlines:
        return "None"
    parts = []
    for bloodline in bloodlines[:limit]:
        stage = None
        if "get_kekkei_stage_name" in globals():
            try:
                stage = get_kekkei_stage_name(player, bloodline)
            except TypeError:
                stage = get_kekkei_stage_name(player)
        parts.append(f"{bloodline}" + (f" ({stage})" if stage else ""))
    if len(bloodlines) > limit:
        parts.append(f"+{len(bloodlines) - limit} more")
    return ", ".join(parts)


def weighted_affinity_roll():
    roll = random.random()

    for bracket in CONFIG.get("AFFINITY_ROLL_BRACKETS", []):
        if roll <= bracket.get("chance_until", 1):
            return random.randint(bracket.get("min", 1), bracket.get("max", 10000))

    return random.randint(1, 10000)


def get_skill_level(value):
    for tier in CONFIG.get("SKILL_TIERS", []):
        if value >= tier.get("min", 0):
            return tier.get("label", "Unknown")

    return "Novice"


def get_required_xp(level):
    return int(XP_BASE * (level ** XP_EXPONENT))


def get_ninja_rank(level):
    rank_index = (level - 1) // LEVELS_PER_RANK
    return RANKS[min(rank_index, len(RANKS) - 1)]


def calculate_hidden_skill_score(player):
    if not player.get("affinities"):
        return 0

    player = get_effective_player(player)
    affinity_total = sum(player["affinities"].values())
    stat_total = sum(player["stats"].values())
    skill_config = CONFIG.get("SKILL_SCORE", {})
    level_multiplier = 1 + (player["level"] * skill_config.get("level_multiplier_per_level", 0.08))

    kekkei_bonus = skill_config.get("kekkei_bonus", 1500) * len(get_player_bloodlines(player))
    clan_bonus = skill_config.get("non_civilian_clan_bonus", 500) if player.get("clan") and player.get("clan") != "Civilian" else 0
    nature_count = len(player.get("chakra_natures", []))
    nature_bonus = max(0, nature_count - 1) * skill_config.get("extra_chakra_nature_bonus", 750)
    village_name = player.get("village")
    village_bonus = CONFIG.get("VILLAGES", {}).get(village_name, {}).get("skill_bonus", 0) if village_name else 0

    return int((affinity_total + stat_total + kekkei_bonus + clan_bonus + nature_bonus + village_bonus) * level_multiplier)


def calculate_duel_power(player):
    return max(1, calculate_combat_rating(player))



def get_beast_config():
    return CONFIG.get("TAILED_BEASTS", {})


def get_default_tailed_beasts():
    return [
        {"name": "One-Tail Shukaku", "tails": 1, "boost_percent": 20},
        {"name": "Two-Tails Matatabi", "tails": 2, "boost_percent": 25},
        {"name": "Three-Tails Isobu", "tails": 3, "boost_percent": 30},
        {"name": "Four-Tails Son Goku", "tails": 4, "boost_percent": 35},
        {"name": "Five-Tails Kokuo", "tails": 5, "boost_percent": 40},
        {"name": "Six-Tails Saiken", "tails": 6, "boost_percent": 45},
        {"name": "Seven-Tails Chomei", "tails": 7, "boost_percent": 50},
        {"name": "Eight-Tails Gyuki", "tails": 8, "boost_percent": 55},
        {"name": "Nine-Tails Kurama", "tails": 9, "boost_percent": 60},
    ]


def get_tailed_beast_pool():
    config = get_beast_config()
    return config.get("beasts", get_default_tailed_beasts())


def cleanup_expired_tailed_beast(player):
    beast = player.get("jinchuriki")
    if not isinstance(beast, dict):
        return None

    expires_at = beast.get("expires_at")
    if not expires_at:
        player["jinchuriki"] = None
        return beast

    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError:
        player["jinchuriki"] = None
        return beast

    if now_utc() >= expiry:
        player["jinchuriki"] = None
        return beast

    return None


def cleanup_all_expired_tailed_beasts(players):
    expired = []
    for user_id, player in players.items():
        beast = cleanup_expired_tailed_beast(player)
        if beast:
            expired.append((user_id, beast))
    return expired


def get_active_tailed_beast(player):
    cleanup_expired_tailed_beast(player)
    beast = player.get("jinchuriki")
    if isinstance(beast, dict):
        return beast
    return None


def get_tailed_beast_multiplier(player):
    beast = get_active_tailed_beast(player)
    if not beast:
        return 1.0
    return 1 + (float(beast.get("boost_percent", 0)) / 100)


def get_effective_player(player):
    """Returns a temporary combat copy with active tailed-beast boosts applied."""
    multiplier = get_tailed_beast_multiplier(player)
    if multiplier <= 1:
        return player

    effective = dict(player)
    effective["stats"] = {
        name: int(value * multiplier)
        for name, value in player.get("stats", {}).items()
    }
    effective["affinities"] = {
        name: int(value * multiplier)
        for name, value in (player.get("affinities") or {}).items()
    }
    return effective


def get_occupied_tailed_beast_names(players):
    occupied = set()
    for player in players.values():
        beast = get_active_tailed_beast(player)
        if beast:
            occupied.add(beast.get("name"))
    return occupied


def get_available_tailed_beasts(players):
    occupied = get_occupied_tailed_beast_names(players)
    return [beast for beast in get_tailed_beast_pool() if beast.get("name") not in occupied]


def assign_tailed_beast(player, beast):
    duration_hours = int(get_beast_config().get("duration_hours", 24))
    expires_at = now_utc() + timedelta(hours=duration_hours)
    player["jinchuriki"] = {
        "name": beast.get("name", "Unknown Beast"),
        "tails": int(beast.get("tails", 1)),
        "boost_percent": int(beast.get("boost_percent", 20)),
        "captured_at": now_utc().isoformat(),
        "expires_at": expires_at.isoformat()
    }
    return player["jinchuriki"]


def format_active_tailed_beast(player):
    beast = get_active_tailed_beast(player)
    if not beast:
        return "None"

    try:
        expiry = datetime.fromisoformat(beast["expires_at"])
        expires_text = expiry.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        expires_text = "Unknown"

    return f"{beast.get('name')} ({beast.get('tails')} tails) | +{beast.get('boost_percent')}% all traits until {expires_text}"



def get_tournament_config():
    return CONFIG.get("TOURNAMENT", {})


def get_tournament_status_text(tournament):
    if not tournament:
        return "No active tournament."

    status = tournament.get("status", "signup")
    participants = tournament.get("participants", [])
    lines = [
        f"**Status:** {status.title()}",
        f"**Participants:** {len(participants)}"
    ]

    if status == "signup":
        lines.append("\n".join(f"{index}. <@{user_id}>" for index, user_id in enumerate(participants, start=1)) if participants else "No entrants yet.")
        return "\n".join(lines)

    alive = tournament.get("alive", [])
    eliminated = tournament.get("eliminated", [])
    current_match = tournament.get("current_match")
    match_queue = tournament.get("match_queue", [])

    lines.append(f"**Round:** {tournament.get('round', 1)}")
    lines.append(f"**Still In:** {len(alive)}")

    if current_match:
        lines.append(f"**Current Match:** <@{current_match['a']}> vs <@{current_match['b']}>")

    if match_queue:
        lines.append("**Upcoming Matches:**")
        lines.extend(f"- <@{a}> vs <@{b}>" for a, b in match_queue[:5])

    if eliminated:
        lines.append("**Eliminated:** " + ", ".join(f"<@{user_id}>" for user_id in eliminated[-8:]))

    return "\n".join(lines)


def get_tournament_member(channel, user_id):
    try:
        if getattr(channel, "guild", None):
            member = channel.guild.get_member(int(user_id))
            if member:
                return member
        return bot.get_user(int(user_id))
    except Exception:
        return None


def get_tournament_member_name(channel, user_id):
    member = get_tournament_member(channel, user_id)
    if member:
        return member.name
    return f"User {user_id}"


def get_tournament_member_mention(channel, user_id):
    member = get_tournament_member(channel, user_id)
    if member:
        return member.mention
    return f"<@{user_id}>"


async def start_tournament_duel(channel, user_id_a, user_id_b):
    """Starts a real turn-based PvP duel for the current tournament match."""
    global ACTIVE_TOURNAMENT

    players = migrate_all_players(load_players())
    user_id_a = str(user_id_a)
    user_id_b = str(user_id_b)

    if user_id_a not in players or user_id_b not in players:
        missing = user_id_a if user_id_a not in players else user_id_b
        winner = user_id_b if missing == user_id_a else user_id_a
        await channel.send(f"🏟️ <@{missing}> no longer has a valid profile. <@{winner}> advances by default.")
        await process_tournament_match_result(channel, winner, missing)
        return

    existing_a, _ = find_active_duel_for_user(user_id_a, channel.id)
    existing_b, _ = find_active_duel_for_user(user_id_b, channel.id)
    if existing_a or existing_b:
        await channel.send("🏟️ Tournament paused because one of the selected players is already in an active duel.")
        return

    duel_key = get_duel_key(channel.id, user_id_a, user_id_b)
    first_turn = user_id_a if random.random() < 0.5 else user_id_b
    config = get_pvp_config()
    effective_players = {
        user_id_a: get_effective_player(players[user_id_a]),
        user_id_b: get_effective_player(players[user_id_b])
    }

    ACTIVE_DUELS[duel_key] = {
        "channel_id": channel.id,
        "players": [user_id_a, user_id_b],
        "members": {
            user_id_a: {"name": get_tournament_member_name(channel, user_id_a), "mention": get_tournament_member_mention(channel, user_id_a)},
            user_id_b: {"name": get_tournament_member_name(channel, user_id_b), "mention": get_tournament_member_mention(channel, user_id_b)}
        },
        "hp": {
            user_id_a: calculate_scaled_resource(players[user_id_a], "hp"),
            user_id_b: calculate_scaled_resource(players[user_id_b], "hp")
        },
        "max_hp": {
            user_id_a: calculate_scaled_resource(players[user_id_a], "hp"),
            user_id_b: calculate_scaled_resource(players[user_id_b], "hp")
        },
        "chakra": {
            user_id_a: calculate_scaled_resource(players[user_id_a], "chakra"),
            user_id_b: calculate_scaled_resource(players[user_id_b], "chakra")
        },
        "max_chakra": {
            user_id_a: calculate_scaled_resource(players[user_id_a], "chakra"),
            user_id_b: calculate_scaled_resource(players[user_id_b], "chakra")
        },
        "stamina": {
            user_id_a: calculate_scaled_resource(players[user_id_a], "stamina"),
            user_id_b: calculate_scaled_resource(players[user_id_b], "stamina")
        },
        "max_stamina": {
            user_id_a: calculate_scaled_resource(players[user_id_a], "stamina"),
            user_id_b: calculate_scaled_resource(players[user_id_b], "stamina")
        },
        "guard": {user_id_a: False, user_id_b: False},
        "statuses": {user_id_a: {}, user_id_b: {}},
        "turn": first_turn,
        "round": 1,
        "started_at": now_utc().isoformat(),
        "last_action_at": now_utc().isoformat(),
        "tournament_match": True
    }

    if ACTIVE_TOURNAMENT and ACTIVE_TOURNAMENT.get("current_match"):
        ACTIVE_TOURNAMENT["current_match"]["duel_key"] = duel_key

    await channel.send(
        f"🏟️ **Tournament Match Started**\n"
        f"<@{user_id_a}> vs <@{user_id_b}>\n"
        f"Loser is eliminated. Winner advances.\n\n"
        f"{build_duel_status_text(ACTIVE_DUELS[duel_key])}\n\n"
        f"First turn: {get_duel_member_text(ACTIVE_DUELS[duel_key], first_turn)}\n"
        "Use `$attack`, `$heavy`, `$taijutsu`, `$transformation`, `$genjutsu`, `$jutsu [name]`, or `$defend`."
    )


async def create_next_tournament_round(channel):
    global ACTIVE_TOURNAMENT

    tournament = ACTIVE_TOURNAMENT
    if not tournament:
        return

    alive = list(dict.fromkeys(tournament.get("alive", [])))

    if len(alive) <= 1:
        await finish_tournament(channel)
        return

    random.shuffle(alive)
    match_queue = []
    next_round = []
    bye_text = None

    if len(alive) % 2 == 1:
        bye = alive.pop()
        next_round.append(bye)
        bye_text = f"🟦 <@{bye}> gets a bye and advances to the next round."

    for index in range(0, len(alive), 2):
        match_queue.append((alive[index], alive[index + 1]))

    tournament["match_queue"] = match_queue
    tournament["next_round"] = next_round
    tournament["current_match"] = None

    lines = [
        f"🏟️ **Tournament Round {tournament.get('round', 1)}**",
        "Matches are now real turn-based PvP. Each loser is eliminated."
    ]
    if bye_text:
        lines.append(bye_text)
    lines.extend(f"⚔️ <@{a}> vs <@{b}>" for a, b in match_queue)
    await channel.send("\n".join(lines))

    await start_next_tournament_match(channel)


async def start_next_tournament_match(channel):
    global ACTIVE_TOURNAMENT

    tournament = ACTIVE_TOURNAMENT
    if not tournament or tournament.get("status") != "running":
        return

    if tournament.get("current_match"):
        return

    match_queue = tournament.get("match_queue", [])
    if not match_queue:
        tournament["alive"] = list(dict.fromkeys(tournament.get("next_round", [])))
        tournament["next_round"] = []
        tournament["round"] = tournament.get("round", 1) + 1

        if len(tournament.get("alive", [])) <= 1:
            await finish_tournament(channel)
        else:
            await create_next_tournament_round(channel)
        return

    user_id_a, user_id_b = match_queue.pop(0)
    tournament["match_queue"] = match_queue
    tournament["current_match"] = {"a": str(user_id_a), "b": str(user_id_b), "started_at": now_utc().isoformat()}

    await start_tournament_duel(channel, user_id_a, user_id_b)


async def process_tournament_match_result(channel, winner_id, loser_id):
    global ACTIVE_TOURNAMENT

    tournament = ACTIVE_TOURNAMENT
    if not tournament or tournament.get("status") != "running":
        return

    if tournament.get("channel_id") != channel.id:
        return

    current_match = tournament.get("current_match")
    if not current_match:
        return

    winner_id = str(winner_id)
    loser_id = str(loser_id)
    match_players = {str(current_match.get("a")), str(current_match.get("b"))}

    if winner_id not in match_players or loser_id not in match_players:
        return

    tournament.setdefault("next_round", []).append(winner_id)
    tournament.setdefault("eliminated", []).append(loser_id)
    tournament.setdefault("history", []).append({
        "round": tournament.get("round", 1),
        "winner_id": winner_id,
        "loser_id": loser_id,
        "ended_at": now_utc().isoformat()
    })
    tournament["current_match"] = None

    await channel.send(
        f"🏟️ **Tournament Result**\n"
        f"✅ <@{winner_id}> advances.\n"
        f"❌ <@{loser_id}> is eliminated."
    )

    await start_next_tournament_match(channel)


async def finish_tournament(channel):
    global ACTIVE_TOURNAMENT

    tournament = ACTIVE_TOURNAMENT
    if not tournament:
        return

    alive = list(dict.fromkeys(tournament.get("alive", []) or tournament.get("next_round", [])))
    if not alive:
        ACTIVE_TOURNAMENT = None
        await channel.send("🏟️ Tournament ended with no winner.")
        return

    champion_id = str(alive[0])
    players = migrate_all_players(load_players())
    champion = players.get(champion_id)
    config = get_tournament_config()
    xp_reward = random.randint(config.get("winner_xp_min", 125), config.get("winner_xp_max", 250))

    final_text = f"🏆 **Tournament Champion:** <@{champion_id}>"

    if champion:
        leveled_up, levels_gained = add_xp(champion, xp_reward)
        champion["tournament_wins"] = champion.get("tournament_wins", 0) + 1
        champion["hidden_skill_score"] = calculate_hidden_skill_score(champion)
        players[champion_id] = champion
        save_players(players)
        final_text += f"\n**Champion Reward:** {xp_reward} XP"
        final_text += f"\n**Tournament Wins:** {champion['tournament_wins']}"
        if leveled_up:
            final_text += f"\n**Level Up:** +{levels_gained} level(s). Now level {champion['level']} — {champion['rank']}."

    ACTIVE_TOURNAMENT = None
    await channel.send(final_text)


async def run_tournament_bracket(channel):
    global ACTIVE_TOURNAMENT

    tournament = ACTIVE_TOURNAMENT
    if not tournament:
        return

    if tournament.get("status") != "signup":
        return

    participants = list(dict.fromkeys(tournament.get("participants", [])))
    if len(participants) < 2:
        ACTIVE_TOURNAMENT = None
        await channel.send("🏟️ Tournament cancelled. Not enough players joined.")
        return

    players = migrate_all_players(load_players())
    valid_participants = [str(user_id) for user_id in participants if str(user_id) in players and players[str(user_id)].get("has_rolled")]

    if len(valid_participants) < 2:
        ACTIVE_TOURNAMENT = None
        await channel.send("🏟️ Tournament cancelled. Not enough valid rolled players joined.")
        return

    random.shuffle(valid_participants)
    tournament["status"] = "running"
    tournament["round"] = 1
    tournament["participants"] = valid_participants
    tournament["alive"] = valid_participants[:]
    tournament["eliminated"] = []
    tournament["history"] = []
    tournament["match_queue"] = []
    tournament["next_round"] = []
    tournament["current_match"] = None

    await channel.send(
        "🏟️ **Tournament Started**\n"
        f"Entrants: **{len(valid_participants)}**\n"
        "Each bracket match is a real turn-based PvP duel. Lose once and you are out."
    )

    await create_next_tournament_round(channel)


async def tournament_signup_timer(channel, seconds):
    await asyncio.sleep(seconds)
    if ACTIVE_TOURNAMENT and ACTIVE_TOURNAMENT.get("status") == "signup":
        await run_tournament_bracket(channel)



def roll_chakra_nature(exclude=None):
    exclude = exclude or []
    weights_config = CONFIG.get("NATURE_ROLL_WEIGHTS", {})

    available = [
        nature for nature in NATURE_TYPES
        if nature not in exclude
    ]

    if not available:
        return None

    weights = [
        weights_config.get(nature, 1)
        for nature in available
    ]

    return random.choices(available, weights=weights, k=1)[0]


def is_dojutsu(kekkei_name):
    return kekkei_name in ["Sharingan", "Byakugan"]


def get_sharingan_level_name(player):
    if not has_player_bloodline(player, "Sharingan"):
        return None

    sharingan_data = CONFIG.get("SHARINGAN", {})
    levels = sharingan_data.get("levels", [])

    current_index = player.get("sharingan_level", 0)

    if not levels:
        return "Sharingan"

    current_index = max(0, min(current_index, len(levels) - 1))
    return levels[current_index]


def try_unlock_training_kekkei(player):
    config = CONFIG.get("TRAINING_KEKKEI", {})

    if not config.get("enabled", False):
        return None

    # Dojutsu roll: rarer than normal Kekkei Genkai.
    if random.randint(1, config.get("dojutsu_roll_max", 150)) == config.get("dojutsu_success_value", 1):
        pool = config.get("dojutsu_pool", [])
        if pool:
            unlocked = random.choice(pool)
            if add_player_bloodline(player, unlocked, apply_modifiers=True):
                return unlocked
            player.setdefault("bloodline_fragments", {})[unlocked] = player.setdefault("bloodline_fragments", {}).get(unlocked, 0) + 1
            return f"{unlocked} Fragment"

    # General Kekkei Genkai roll.
    if random.randint(1, config.get("general_roll_max", 100)) == config.get("general_success_value", 1):
        pool = config.get("general_pool", [])
        if pool:
            unlocked = random.choice(pool)
            if add_player_bloodline(player, unlocked, apply_modifiers=True):
                return unlocked
            player.setdefault("bloodline_fragments", {})[unlocked] = player.setdefault("bloodline_fragments", {}).get(unlocked, 0) + 1
            return f"{unlocked} Fragment"

    return None


def try_level_sharingan(player, source="training"):
    if not has_player_bloodline(player, "Sharingan"):
        return None

    config = CONFIG.get("SHARINGAN", {})

    if not config.get("enabled", False):
        return None

    current_level = player.get("sharingan_level", 0)
    max_level = config.get("max_level_index", 3)

    if current_level >= max_level:
        return None

    chance = config.get("training_level_chance", 4)

    if source == "fight":
        chance = config.get("fight_level_chance", 8)

    if random.randint(1, config.get("roll_max", 100)) <= chance:
        player["sharingan_level"] = current_level + 1

        player.setdefault("affinities", {})
        player["affinities"]["Genjutsu"] = player["affinities"].get("Genjutsu", 0) + config.get("genjutsu_bonus_per_level", 250)
        player["affinities"]["Chakra Control"] = player["affinities"].get("Chakra Control", 0) + config.get("chakra_control_bonus_per_level", 150)
        player["affinities"]["Speed"] = player["affinities"].get("Speed", 0) + config.get("speed_bonus_per_level", 100)

        player["hidden_skill_score"] = calculate_hidden_skill_score(player)
        return get_sharingan_level_name(player)

    return None


def get_curse_mark_multiplier(player):
    curse_mark = player.get("curse_mark")

    if not curse_mark:
        return 1.0

    expires_at = curse_mark.get("expires_at")

    if not expires_at:
        return 1.0

    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError:
        return 1.0

    if now_utc() >= expiry:
        player["curse_mark"] = None
        return 1.0

    return curse_mark.get("multiplier", 1.0)


def apply_curse_mark_to_random_player(players):
    eligible = [
        user_id for user_id, player in players.items()
        if player.get("has_rolled")
    ]

    if not eligible:
        return None, None

    user_id = random.choice(eligible)
    player = players[user_id]
    config = CONFIG.get("CURSE_MARK", {})

    outcomes = config.get("outcomes")
    if isinstance(outcomes, dict) and outcomes:
        names = list(outcomes.keys())
        weights = [max(0, outcomes[name].get("weight", 1)) for name in names]
        selected = random.choices(names, weights=weights, k=1)[0]
        outcome = outcomes[selected]
        mark_type = selected
        multiplier = float(outcome.get("multiplier", 1.0))
        display_name = outcome.get("name", selected.title())
        description = outcome.get("description", "A strange seal reacts to the player's chakra.")
    else:
        buff_chance = config.get("buff_chance", 50)
        roll = random.randint(1, 100)
        if roll <= buff_chance:
            mark_type = "buff"
            multiplier = config.get("buff_multiplier", 1.10)
            display_name = "Empowered Curse Mark"
            description = "Power surges through the target, increasing their combat potential."
        else:
            mark_type = "debuff"
            multiplier = config.get("debuff_multiplier", 0.90)
            display_name = "Unstable Curse Mark"
            description = "The seal fights against the target's chakra, reducing their combat potential."

    expires_at = now_utc() + timedelta(hours=config.get("duration_hours", 24))

    player["curse_mark"] = {
        "type": mark_type,
        "name": display_name,
        "description": description,
        "multiplier": multiplier,
        "expires_at": expires_at.isoformat(),
        "applied_at": now_utc().isoformat(),
    }

    player["hidden_skill_score"] = calculate_hidden_skill_score(player)
    return user_id, player["curse_mark"]


async def trigger_curse_mark_event(channel):
    players = migrate_all_players(load_players())
    user_id, curse_mark = apply_curse_mark_to_random_player(players)

    if not user_id:
        embed = build_event_embed(
            "Curse Mark Event Failed",
            "No eligible rolled players were found.",
            "cursemark",
            "warning",
        )
        await channel.send(embed=embed)
        return

    save_players(players)

    multiplier = float(curse_mark.get("multiplier", 1.0))
    percent = int(abs(multiplier - 1) * 100)
    direction = "boost" if multiplier >= 1 else "reduction"
    expires_at = datetime.fromisoformat(curse_mark["expires_at"]).strftime("%Y-%m-%d %H:%M UTC")

    embed = build_event_embed(
        "Curse Mark Event",
        f"<@{user_id}> has been marked by **{curse_mark.get('name', 'Curse Mark')}**.",
        "cursemark",
        "purple" if multiplier >= 1 else "danger",
    )
    add_field(embed, "Effect", f"**{percent}% skill {direction}** until **{expires_at}**", False)
    add_field(embed, "Lore", curse_mark.get("description", "A strange seal reacts to their chakra."), False)
    await channel.send(embed=embed)



def choose_random_world_event():
    config = CONFIG.get("RANDOM_EVENTS", {})
    weighted = config.get("weighted_events", {})

    if not weighted:
        return "xp"

    names = list(weighted.keys())
    weights = list(weighted.values())

    return random.choices(names, weights=weights, k=1)[0]


async def start_random_world_event(channel):
    global ACTIVE_EVENT

    if ACTIVE_EVENT is not None:
        return

    random_event_config = CONFIG.get("RANDOM_EVENTS", {})
    event_type = choose_random_world_event()

    if event_type == "cursemark":
        await trigger_curse_mark_event(channel)
        return

    duration = random_event_config.get("event_duration_seconds", 120)

    if event_type == "xp":
        value = random.choice(random_event_config.get("xp_values", [100]))
        ACTIVE_EVENT = {
            "type": "xp",
            "value": value,
            "accepted": [],
            "started_at": now_utc().isoformat()
        }
        embed = build_event_embed(
            "Random XP Event",
            f"Claim **{fmt_num(value)} XP** before the event closes.",
            "xp",
            "gold",
        )
        add_field(embed, "How To Join", "Use `$event` to claim the reward.", False)
        add_field(embed, "Time Limit", f"{duration} seconds", True)
        await channel.send(embed=embed)

    elif event_type == "shinobi":
        value = random.choice(random_event_config.get("shinobi_difficulties", [1]))
        enemy = build_shinobi_enemy(value)
        ACTIVE_EVENT = {
            "type": "shinobi",
            "value": value,
            "enemy": enemy,
            "accepted": [],
            "started_at": now_utc().isoformat()
        }
        embed = build_event_embed(
            "Shinobi Group Fight",
            f"**{enemy['name']}** has entered the area. This is a group PvE fight.",
            "shinobi",
            "danger",
        )
        add_field(embed, "Threat Level", f"Difficulty **{value}** | Power **{fmt_num(enemy['power'])}** | HP **{fmt_num(enemy['hp'])}**", False)
        add_field(embed, "Rewards", f"**{fmt_num(enemy['xp_reward'])} XP** per survivor | Item chance **{enemy['item_reward_chance']}%**", False)
        add_field(embed, "How To Join", event_join_instruction("shinobi"), False)
        add_field(embed, "Time Limit", f"{duration} seconds", True)
        await channel.send(embed=embed)

    elif event_type == "beast":
        value = random.choice(random_event_config.get("beast_difficulties", [1]))
        players = migrate_all_players(load_players())
        enemy = build_tailed_beast(value, players)
        save_players(players)
        if enemy is None:
            embed = build_event_embed(
                "Tailed-Beast Capture Failed",
                "Every tailed beast is currently sealed inside a player. Try again after one expires.",
                "beast",
                "warning",
            )
            await channel.send(embed=embed)
            return
        ACTIVE_EVENT = {
            "type": "beast",
            "value": value,
            "enemy": enemy,
            "accepted": [],
            "started_at": now_utc().isoformat()
        }
        embed = build_event_embed(
            "Tailed-Beast Capture",
            f"**{enemy['name']}** has appeared. Only one shinobi can attempt the seal.",
            "beast",
            "danger",
        )
        add_field(embed, "Beast Details", f"Tails **{enemy['tails']}** | Trait Boost **+{enemy['boost_percent']}%** | Power **{fmt_num(enemy['power'])}**", False)
        add_field(embed, "Capture Reward", f"**{fmt_num(enemy['xp_reward'])} XP**, **Tailed Beast Chakra Fragment**, and 24-hour Jinchuriki boost", False)
        add_field(embed, "How To Join", event_join_instruction("beast"), False)
        add_field(embed, "Time Limit", f"{duration} seconds", True)
        await channel.send(embed=embed)

    bot.loop.create_task(event_timer(channel, duration))

async def random_event_loop():
    await bot.wait_until_ready()

    config = CONFIG.get("RANDOM_EVENTS", {})

    if not config.get("enabled", False):
        return

    channel_id = config.get("channel_id")

    while not bot.is_closed():
        await asyncio.sleep(config.get("interval_seconds", 3600))

        channel = bot.get_channel(channel_id)

        if channel:
            await start_random_world_event(channel)




def roll_clan():
    clan_names = list(CONFIG.get("CLANS", {}).keys())

    if not clan_names:
        return "Civilian"

    weights = [
        CONFIG["CLANS"][clan].get("weight", 1)
        for clan in clan_names
    ]

    return random.choices(clan_names, weights=weights, k=1)[0]


def apply_clan_bonuses(affinities, clan):
    """Legacy compatibility: clan skill bonuses still apply to rolled skills."""
    clan_data = CONFIG.get("CLANS", {}).get(clan, {})
    bonuses = clan_data.get("skill_bonus") or clan_data.get("affinity_bonus", {})

    for trait, bonus in bonuses.items():
        if trait in affinities:
            affinities[trait] = max(1, affinities[trait] + int(bonus))

    return affinities


def apply_trait_delta(target, delta):
    if not isinstance(delta, dict):
        return target
    for key, amount in delta.items():
        if key in target:
            target[key] = max(1, target.get(key, 0) + int(amount))
    return target


def get_trait_modifier(section, name):
    return CONFIG.get("TRAIT_MODIFIERS", {}).get(section, {}).get(name, {})


def apply_configured_trait_modifiers(stats, skills, clan=None, kekkei_genkai=None, chakra_natures=None):
    """Applies v1.9.0 config-driven trait effects to stats and skills.

    This is the control panel you wanted: clans, dojutsu, Kekkei Genkai,
    extra natures, and future traits can all modify exact stats/skills in config.json.
    """
    if clan:
        clan_mod = get_trait_modifier("clans", clan)
        apply_trait_delta(stats, clan_mod.get("stats", {}))
        apply_trait_delta(skills, clan_mod.get("skills", {}))

    for bloodline in ([kekkei_genkai] if isinstance(kekkei_genkai, str) else (kekkei_genkai or [])):
        kg_mod = get_trait_modifier("kekkei_genkai", bloodline)
        apply_trait_delta(stats, kg_mod.get("stats", {}))
        apply_trait_delta(skills, kg_mod.get("skills", {}))

    chakra_natures = chakra_natures or []
    nature_config = CONFIG.get("TRAIT_MODIFIERS", {}).get("chakra_natures", {})
    rare_natures = set(nature_config.get("rare_natures", []))

    # Every additional nature after the first gives a configurable Chakra Control-style bonus.
    extra_count = max(0, len(chakra_natures) - 1)
    for _ in range(extra_count):
        extra = nature_config.get("extra_nature_bonus", {})
        apply_trait_delta(stats, extra.get("stats", {}))
        apply_trait_delta(skills, extra.get("skills", {}))

    # Rare natures like Ying/Yang can add their own configured bonuses.
    for nature in chakra_natures:
        if nature in rare_natures:
            rare = nature_config.get("rare_nature_bonus", {})
            apply_trait_delta(stats, rare.get("stats", {}))
            apply_trait_delta(skills, rare.get("skills", {}))

    return stats, skills


def roll_kekkei_genkai_from_clan(clan):
    clan_data = CONFIG.get("CLANS", {}).get(clan, {})
    chance = clan_data.get("kekkei_chance", 1)

    if random.randint(1, 100) > chance:
        return None

    possible = clan_data.get("possible_kekkei", [])

    if possible:
        return random.choice(possible)

    return random.choice(KEKKEI_GENKAI)


def roll_character():
    clan = roll_clan()
    stats = BASE_STATS.copy()
    skills = {}

    for affinity in AFFINITIES:
        skills[affinity] = weighted_affinity_roll()

    chakra_nature = roll_chakra_nature()

    dual_nature = None
    if random.randint(1, 10) == 1:
        dual_nature = roll_chakra_nature(exclude=[chakra_nature])

    chakra_natures = [chakra_nature]
    if dual_nature:
        chakra_natures.append(dual_nature)

    kekkei_genkai = roll_kekkei_genkai_from_clan(clan)
    stats, skills = apply_configured_trait_modifiers(stats, skills, clan, kekkei_genkai, chakra_natures)

    return clan, stats, skills, chakra_nature, dual_nature, kekkei_genkai


def apply_devmode_to_player(player, username="0duck"):
    """Gives the developer an intentionally overpowered profile after $devmode is used."""
    player["name"] = player.get("name") or username
    player["level"] = max(player.get("level", 1), DEV_MODE_LEVEL)
    player["xp"] = 0
    player["rank"] = DEV_MODE_RANK
    player["clan"] = DEV_MODE_CLAN
    player["kekkei_genkai"] = DEV_MODE_KEKKEI_GENKAI
    player["kekkei_genkai_list"] = [DEV_MODE_KEKKEI_GENKAI]
    player.setdefault("kekkei_evolution", {})[DEV_MODE_KEKKEI_GENKAI] = 0
    player["chakra_natures"] = NATURE_TYPES.copy()
    player["has_rolled"] = True
    player["devmode_enabled"] = True
    player["dev_mode"] = True

    player["stats"] = {
        stat: DEV_MODE_STAT_VALUE
        for stat in BASE_STATS
    }

    player["affinities"] = {
        affinity: DEV_MODE_AFFINITY_VALUE
        for affinity in AFFINITIES
    }

    player.setdefault("inventory", {})
    player["inventory"]["Tailed Beast Chakra Fragment"] = max(
        player["inventory"].get("Tailed Beast Chakra Fragment", 0),
        99
    )
    player["inventory"]["Forbidden Scroll Page"] = max(
        player["inventory"].get("Forbidden Scroll Page", 0),
        99
    )

    player["hidden_skill_score"] = calculate_hidden_skill_score(player)
    return player


def create_player(ctx):
    return {
        "name": ctx.author.name,
        "level": 1,
        "xp": 0,
        "rank": "Academy Student",
        "stats": BASE_STATS.copy(),
        "affinities": None,
        "chakra_natures": [],
        "clan": None,
        "kekkei_genkai": None,
        "kekkei_genkai_list": [],
        "devmode_enabled": False,
        "inventory": {},
        "hidden_skill_score": 0,
        "has_rolled": False,
        "last_training": None,
        "known_jutsu": [],
        "village": None,
        "rerolls_remaining": REROLLS_PER_PLAYER,
        "ryo": CONFIG.get("ECONOMY", {}).get("starting_ryo", 0),
        "daily_streak": 0,
        "weekly_streak": 0,
        "best_daily_streak": 0,
        "best_weekly_streak": 0,
        "last_daily": None,
        "last_weekly": None,
        "total_daily_claims": 0,
        "total_weekly_claims": 0,
        "gacha_rolls": 0,
        "gacha_pity": 0,
        "gacha_mythic_pulls": 0,
        "gacha_legendary_pulls": 0,
        "gacha_mythic_pity": 0,
        "kekkei_evolution": {},
        "bloodline_fragments": {}
    }



def get_village_training_xp_bonus(player):
    village_name = player.get("village")
    if not village_name:
        return 0
    return CONFIG.get("VILLAGES", {}).get(village_name, {}).get("training_xp_bonus", 0)


def player_meets_jutsu_requirements(player, jutsu_data):
    requirements = jutsu_data.get("requirements", {})

    if player.get("level", 1) < requirements.get("level", 1):
        return False, f"Requires level {requirements.get('level', 1)}."

    required_nature = requirements.get("chakra_nature")
    if required_nature and required_nature not in player.get("chakra_natures", []):
        return False, f"Requires chakra nature: {required_nature}."

    required_clan = requirements.get("clan")
    if required_clan and player.get("clan") != required_clan:
        return False, f"Requires clan: {required_clan}."

    required_village = requirements.get("village")
    if required_village and player.get("village") != required_village:
        return False, f"Requires village: {required_village}."

    required_kekkei = requirements.get("kekkei_genkai")
    if required_kekkei and not has_player_bloodline(player, required_kekkei):
        return False, f"Requires Kekkei Genkai: {required_kekkei}."

    return True, None


def calculate_jutsu_power(player, jutsu_data):
    base_power = jutsu_data.get("base_power", 100)
    scaling_stat = jutsu_data.get("scaling", "Chakra Control")
    scaling_divisor = jutsu_data.get("scaling_divisor", 100)

    scaling_value = 0
    if scaling_stat in player.get("stats", {}):
        scaling_value = player["stats"].get(scaling_stat, 0)
    elif player.get("affinities") and scaling_stat in player.get("affinities", {}):
        scaling_value = player["affinities"].get(scaling_stat, 0)

    return int(base_power + (scaling_value / max(1, scaling_divisor)))

def level_up_scaling(player):
    for stat in player["stats"]:
        player["stats"][stat] += random.randint(CONFIG.get("LEVEL_UP", {}).get("stat_gain_min", 3), CONFIG.get("LEVEL_UP", {}).get("stat_gain_max", 8))

    for affinity in player["affinities"]:
        gain = random.randint(CONFIG.get("LEVEL_UP", {}).get("affinity_gain_min", 10), CONFIG.get("LEVEL_UP", {}).get("affinity_gain_max", 40))

        if player["affinities"][affinity] >= CONFIG.get("LEVEL_UP", {}).get("normal_affinity_cap", 10000):
            if random.randint(1, CONFIG.get("LEVEL_UP", {}).get("scale_break_roll_max", 100)) <= CONFIG.get("LEVEL_UP", {}).get("scale_break_chance", 1):
                player["affinities"][affinity] += random.randint(CONFIG.get("LEVEL_UP", {}).get("scale_break_gain_min", 50), CONFIG.get("LEVEL_UP", {}).get("scale_break_gain_max", 250))
        else:
            player["affinities"][affinity] = min(CONFIG.get("LEVEL_UP", {}).get("normal_affinity_cap", 10000), player["affinities"][affinity] + gain)

    unlock_levels = CONFIG.get("LEVEL_UP", {}).get("chakra_nature_unlock_levels", [15, 30, 50, 75])

    if player["level"] in unlock_levels:
        current = player.get("chakra_natures", [])
        available = [nature for nature in NATURE_TYPES if nature not in current]

        if available:
            current.append(random.choice(available))
            player["chakra_natures"] = current


def add_xp(player, amount):
    player["xp"] += amount
    leveled_up = False
    levels_gained = 0

    while player["xp"] >= get_required_xp(player["level"]):
        player["xp"] -= get_required_xp(player["level"])
        player["level"] += 1
        player["rank"] = get_ninja_rank(player["level"])
        level_up_scaling(player)
        leveled_up = True
        levels_gained += 1

    player["hidden_skill_score"] = calculate_hidden_skill_score(player)

    return leveled_up, levels_gained


def add_item(player, item_name, amount=1):
    if "inventory" not in player or isinstance(player["inventory"], list):
        player["inventory"] = {}

    player["inventory"][item_name] = player["inventory"].get(item_name, 0) + amount


def remove_item(player, item_name, amount=1):
    if item_name not in player["inventory"]:
        return False

    if player["inventory"][item_name] < amount:
        return False

    player["inventory"][item_name] -= amount

    if player["inventory"][item_name] <= 0:
        del player["inventory"][item_name]

    return True


def get_random_item_drop():
    roll = random.random()

    for entry in CONFIG.get("ITEM_DROP_TABLE", []):
        if roll <= entry.get("chance_until", 1):
            items = entry.get("items", [])
            return random.choice(items) if items else None

    return random.choice(list(ITEMS.keys())) if ITEMS else None


def can_train(player):
    if not player.get("last_training"):
        return True, None

    last_training = datetime.fromisoformat(player["last_training"])
    next_training = last_training + timedelta(minutes=TRAINING_COOLDOWN_MINUTES)

    if now_utc() >= next_training:
        return True, None

    remaining = next_training - now_utc()
    minutes = int(remaining.total_seconds() // 60)
    seconds = int(remaining.total_seconds() % 60)

    return False, f"{minutes}m {seconds}s"




def resolve_training_stat_name(stat_text):
    """Maps user-friendly stat names to the real stat names stored in players.json."""
    if not stat_text:
        return None

    cleaned = stat_text.lower().strip().replace("_", " ").replace("-", " ")

    aliases = {
        "hp": "Health",
        "health": "Health",
        "life": "Health",
        "chakra": "Chakra Reserves",
        "chakra reserves": "Chakra Reserves",
        "reserves": "Chakra Reserves",
        "stamina": "Stamina",
        "energy": "Stamina",
        "durability": "Durability",
        "defense": "Durability",
        "defence": "Durability",
        "tank": "Durability",
    }

    if cleaned in aliases:
        return aliases[cleaned]

    for stat_name in BASE_STATS:
        if cleaned == stat_name.lower():
            return stat_name

    return None


def generate_attack(attacker_name, defender_name):
    attacks = [
        f"{attacker_name} kicked {defender_name}",
        f"{attacker_name} landed a clean punch on {defender_name}",
        f"{attacker_name} threw a kunai at {defender_name}",
        f"{attacker_name} used a quick taijutsu combo on {defender_name}",
        f"{attacker_name} struck {defender_name} with a chakra-enhanced hit",
        f"{attacker_name} caught {defender_name} off guard"
    ]

    return random.choice(attacks)


def build_shinobi_enemy(value):
    difficulty = max(1, int(value))
    config = event_config("shinobi")
    enemies = config.get("enemy_names") or [
        "Rogue Mist Shinobi",
        "Hidden Sound Assassin",
        "Missing-Nin",
        "Elite Leaf Traitor",
        "Akatsuki Scout",
    ]

    return {
        "name": random.choice(enemies),
        "difficulty": difficulty,
        "hp": int(config.get("base_hp", 150) + (difficulty * config.get("hp_per_difficulty", 75))),
        "power": int(config.get("base_power", 5000) + (difficulty * config.get("power_per_difficulty", 1500))),
        "xp_reward": int(config.get("base_xp", 100) + (difficulty * config.get("xp_per_difficulty", 50))),
        "item_reward_chance": min(
            int(config.get("max_item_reward_chance", 80)),
            int(config.get("base_item_reward_chance", 25) + difficulty * config.get("item_reward_chance_per_difficulty", 5)),
        ),
    }


def build_tailed_beast(value, players=None):
    difficulty = max(1, int(value))
    config = event_config("beast")
    players = players or migrate_all_players(load_players())
    cleanup_all_expired_tailed_beasts(players)
    available_beasts = get_available_tailed_beasts(players)

    if not available_beasts:
        return None

    beast_data = random.choice(available_beasts)

    return {
        "name": beast_data.get("name", "Unknown Tailed Beast"),
        "tails": int(beast_data.get("tails", 1)),
        "boost_percent": int(beast_data.get("boost_percent", 20)),
        "difficulty": difficulty,
        "hp": int(config.get("base_hp", 300) + (difficulty * config.get("hp_per_difficulty", 150))),
        "power": int(config.get("base_power", 10000) + (difficulty * config.get("power_per_difficulty", 2500))),
        "xp_reward": int(config.get("base_xp", 300) + (difficulty * config.get("xp_per_difficulty", 125))),
        "item_reward_chance": min(
            int(config.get("max_item_reward_chance", 95)),
            int(config.get("base_item_reward_chance", 50) + difficulty * config.get("item_reward_chance_per_difficulty", 5)),
        ),
    }


async def close_active_event(channel):
    global ACTIVE_EVENT

    if ACTIVE_EVENT is None:
        return

    event = ACTIVE_EVENT

    embed = build_event_embed("Event Closing", "Resolving in **3 seconds**.", event.get("type", "event"), "warning")
    await channel.send(embed=embed)
    await asyncio.sleep(3)

    if ACTIVE_EVENT is None:
        return

    ACTIVE_EVENT = None

    if event["type"] == "xp":
        await resolve_xp_event(channel, event)
    elif event["type"] == "shinobi":
        await resolve_shinobi_event(channel, event)
    elif event["type"] == "beast":
        await resolve_beast_event(channel, event)


async def event_timer(channel, time_sec):
    await asyncio.sleep(time_sec)
    await close_active_event(channel)


async def resolve_xp_event(channel, event):
    players = migrate_all_players(load_players())
    xp_amount = event["value"]

    if not event["accepted"]:
        await channel.send("XP event ended. Nobody joined.")
        return

    results = []

    econ = CONFIG.get("ECONOMY", {})
    for user_id in event["accepted"]:
        if user_id not in players:
            continue

        ryo_reward = random.randint(econ.get("event_xp_ryo_min", 20), econ.get("event_xp_ryo_max", 60))
        players[user_id]["ryo"] = int(players[user_id].get("ryo", 0)) + ryo_reward
        leveled_up, levels_gained = add_xp(players[user_id], xp_amount)
        line = f"<@{user_id}> gained **{xp_amount} XP** and **{ryo_reward} Ryo**"

        if leveled_up:
            line += f" and gained **{levels_gained} level(s)**"

        results.append(line)

    save_players(players)

    await channel.send("🎁 **XP Event Complete**\n" + "\n".join(results))


async def resolve_shinobi_event(channel, event):
    players = migrate_all_players(load_players())
    enemy = event["enemy"]

    if not event["accepted"]:
        embed = build_event_embed(
            "Shinobi Escaped",
            f"**{enemy['name']}** disappeared before anyone joined the fight.",
            "shinobi",
            "warning",
        )
        await channel.send(embed=embed)
        return

    survivors = []
    defeated = []
    results = []

    for user_id in event["accepted"]:
        if user_id not in players:
            continue

        player = players[user_id]
        player_power = calculate_duel_power(player)
        win_chance = calculate_event_win_chance(player_power, enemy["power"], "shinobi")

        if random.random() <= win_chance:
            survivors.append(user_id)
            econ = CONFIG.get("ECONOMY", {})
            ryo_reward = random.randint(econ.get("shinobi_event_ryo_min", 75), econ.get("shinobi_event_ryo_max", 180))
            player["ryo"] = int(player.get("ryo", 0)) + ryo_reward
            leveled_up, levels_gained = add_xp(player, enemy["xp_reward"])
            line = f"✅ <@{user_id}> survived | Chance **{format_percent(win_chance)}** | +**{fmt_num(enemy['xp_reward'])} XP** | +**{fmt_num(ryo_reward)} Ryo**"

            if random.randint(1, 100) <= enemy["item_reward_chance"]:
                item = get_random_item_drop()
                if item:
                    add_item(player, item)
                    line += f" | Found **{item}**"

            sharingan_upgrade = try_level_sharingan(player, source="fight")
            evolution_upgrade = try_auto_kekkei_evolution(player, source="fight")
            if evolution_upgrade:
                line += f" | Evolution: **{evolution_upgrade}**"
            if sharingan_upgrade:
                line += f" | Sharingan: **{sharingan_upgrade}**"
            if leveled_up:
                line += f" | Level Up **+{levels_gained}**"
            results.append(line)
        else:
            defeated.append(user_id)
            results.append(f"❌ <@{user_id}> was defeated | Chance **{format_percent(win_chance)}**")

        player["hidden_skill_score"] = calculate_hidden_skill_score(player)

    save_players(players)

    tone = "success" if survivors else "danger"
    title = "Shinobi Group Fight Complete" if survivors else "Shinobi Group Fight Failed"
    embed = build_event_embed(
        title,
        f"**{enemy['name']}** challenged the group. Survivors: **{len(survivors)}** / **{len(event['accepted'])}**.",
        "shinobi",
        tone,
    )
    add_field(embed, "Enemy", f"Power **{fmt_num(enemy['power'])}** | Difficulty **{enemy.get('difficulty', event.get('value', 1))}**", False)
    add_field(embed, "Results", "\n".join(results) if results else "No valid participants.", False)
    await channel.send(embed=embed)


async def resolve_beast_event(channel, event):
    players = migrate_all_players(load_players())
    cleanup_all_expired_tailed_beasts(players)
    beast = event["enemy"]

    if not event["accepted"]:
        embed = build_event_embed(
            "Tailed Beast Vanished",
            f"**{beast['name']}** disappeared. Nobody attempted the capture.",
            "beast",
            "warning",
        )
        await channel.send(embed=embed)
        return

    user_id = event["accepted"][0]

    if user_id not in players:
        embed = build_event_embed("Capture Failed", "The challenger no longer has a valid profile.", "beast", "warning")
        await channel.send(embed=embed)
        return

    player = players[user_id]
    player_power = calculate_duel_power(player)
    win_chance = calculate_event_win_chance(player_power, beast["power"], "beast")

    if random.random() <= win_chance:
        leveled_up, levels_gained = add_xp(player, beast["xp_reward"])
        sealed_beast = assign_tailed_beast(player, beast)
        econ = CONFIG.get("ECONOMY", {})
        ryo_reward = random.randint(econ.get("beast_event_ryo_min", 200), econ.get("beast_event_ryo_max", 450))
        player["ryo"] = int(player.get("ryo", 0)) + ryo_reward
        item = "Tailed Beast Chakra Fragment"
        add_item(player, item)

        expiry = datetime.fromisoformat(sealed_beast["expires_at"]).strftime("%Y-%m-%d %H:%M UTC")
        embed = build_event_embed(
            "Tailed Beast Captured",
            f"<@{user_id}> successfully sealed **{beast['name']}**.",
            "beast",
            "success",
        )
        add_field(embed, "Capture Details", f"Tails **{sealed_beast['tails']}** | Chance **{format_percent(win_chance)}** | Expires **{expiry}**", False)
        add_field(embed, "Temporary Boost", f"+**{sealed_beast['boost_percent']}%** to all traits for **24 hours**", False)
        reward_text = f"+**{fmt_num(beast['xp_reward'])} XP** | +**{fmt_num(ryo_reward)} Ryo** | **{item}**"
        if leveled_up:
            reward_text += f" | Level Up **+{levels_gained}**"
        add_field(embed, "Rewards", reward_text, False)
    else:
        embed = build_event_embed(
            "Tailed Beast Capture Failed",
            f"<@{user_id}> failed to seal **{beast['name']}**.",
            "beast",
            "danger",
        )
        add_field(embed, "Attempt Details", f"Chance **{format_percent(win_chance)}** | Beast Power **{fmt_num(beast['power'])}** | Your Power **{fmt_num(player_power)}**", False)
        add_field(embed, "Result", "The beast escapes back into the world event pool.", False)

    player["hidden_skill_score"] = calculate_hidden_skill_score(player)
    save_players(players)
    await channel.send(embed=embed)

def get_duel_key(channel_id, user_id_a, user_id_b):
    ids = sorted([str(user_id_a), str(user_id_b)])
    return f"{channel_id}:{ids[0]}:{ids[1]}"


def find_active_duel_for_user(user_id, channel_id=None):
    user_id = str(user_id)
    for duel_key, duel in ACTIVE_DUELS.items():
        if user_id in duel.get("players", []):
            if channel_id is None or duel.get("channel_id") == channel_id:
                return duel_key, duel
    return None, None


def find_jutsu_name(search_text):
    search_text = search_text.lower().strip()

    for name in JUTSU:
        if name.lower() == search_text:
            return name

    for name in JUTSU:
        lowered = name.lower()
        if search_text in lowered or lowered.replace(" jutsu", "") == search_text:
            return name

    return None


def get_pvp_config():
    return CONFIG.get("TURN_PVP", {})


def get_duel_member_text(duel, user_id):
    data = duel.get("members", {}).get(str(user_id), {})
    return data.get("mention") or data.get("name") or f"<@{user_id}>"


def get_duel_opponent_id(duel, user_id):
    user_id = str(user_id)
    for player_id in duel.get("players", []):
        if player_id != user_id:
            return player_id
    return None

def is_tsuchi(player):
    return player.get("clan") == "Tsuchi"


def apply_tsuchi_damage_bonus(attacker_player, damage):
    if is_tsuchi(attacker_player):
        return int(damage * 3.5)
    return damage


def apply_tsuchi_defense_bonus(defender_player, damage):
    if is_tsuchi(defender_player):
        return max(1, int(damage * 0.3))
    return damage


def tsuchi_cannot_miss(attacker_player):
    return is_tsuchi(attacker_player)


def tsuchi_immune_to_status(defender_player):
    return is_tsuchi(defender_player)

def get_combat_cost(move_type, resource_type):
    costs = get_pvp_config().get("costs", {})
    return int(costs.get(move_type, {}).get(resource_type, 0))


def has_combat_resource(duel, user_id, move_type):
    user_id = str(user_id)
    stamina_cost = get_combat_cost(move_type, "stamina")
    chakra_cost = get_combat_cost(move_type, "chakra")

    if stamina_cost > 0 and duel.get("stamina", {}).get(user_id, 0) < stamina_cost:
        return False, f"You need **{stamina_cost} stamina** to use this move. Current stamina: **{duel.get('stamina', {}).get(user_id, 0)}**."

    if chakra_cost > 0 and duel.get("chakra", {}).get(user_id, 0) < chakra_cost:
        return False, f"You need **{chakra_cost} chakra** to use this move. Current chakra: **{duel.get('chakra', {}).get(user_id, 0)}**."

    return True, None


def spend_combat_resource(duel, user_id, move_type):
    user_id = str(user_id)
    stamina_cost = get_combat_cost(move_type, "stamina")
    chakra_cost = get_combat_cost(move_type, "chakra")

    if stamina_cost > 0:
        duel["stamina"][user_id] = max(0, duel.get("stamina", {}).get(user_id, 0) - stamina_cost)

    if chakra_cost > 0:
        duel["chakra"][user_id] = max(0, duel.get("chakra", {}).get(user_id, 0) - chakra_cost)

    return stamina_cost, chakra_cost


def build_cost_text(stamina_cost=0, chakra_cost=0):
    parts = []
    if stamina_cost:
        parts.append(f"{stamina_cost} stamina")
    if chakra_cost:
        parts.append(f"{chakra_cost} chakra")
    return "Spent " + " and ".join(parts) + "." if parts else "No resource spent."


def regenerate_duel_resources(duel, user_id):
    user_id = str(user_id)
    config = get_pvp_config()
    chakra_regen = int(config.get("chakra_regen_per_turn", 10))
    stamina_regen = int(config.get("stamina_regen_per_turn", 10))

    messages = []

    if chakra_regen > 0:
        current_chakra = duel.setdefault("chakra", {}).get(user_id, 0)
        max_chakra = duel.setdefault("max_chakra", {}).get(user_id, current_chakra)
        new_chakra = min(max_chakra, current_chakra + chakra_regen)
        recovered = new_chakra - current_chakra
        duel["chakra"][user_id] = new_chakra
        if recovered > 0:
            messages.append(f"Recovered **{recovered} chakra**.")

    if stamina_regen > 0:
        current_stamina = duel.setdefault("stamina", {}).get(user_id, 0)
        max_stamina = duel.setdefault("max_stamina", {}).get(user_id, current_stamina)
        new_stamina = min(max_stamina, current_stamina + stamina_regen)
        recovered = new_stamina - current_stamina
        duel["stamina"][user_id] = new_stamina
        if recovered > 0:
            messages.append(f"Recovered **{recovered} stamina**.")

    return messages


def clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def get_player_stat(player, stat_name, default=0):
    return player.get("stats", {}).get(stat_name, default)


def get_player_affinity(player, affinity_name, default=0):
    return player.get("affinities", {}).get(affinity_name, default)



def get_scaling_config():
    return CONFIG.get("SCALING", {})


def softcap(value, start, strength):
    value = float(value)
    start = max(1, float(start))
    strength = max(0.05, min(1.0, float(strength)))
    if value <= start:
        return value
    overflow = value - start
    return start + (overflow ** strength) * (start ** (1 - strength))


def get_bloodline_power(player):
    scaling = get_scaling_config()
    total = 0
    for kekkei in get_player_bloodlines(player):
        trait_mod = CONFIG.get("TRAIT_MODIFIERS", {}).get("kekkei_genkai", {}).get(kekkei, {})
        category = trait_mod.get("category")
        if category == "dojutsu":
            base = scaling.get("base_power_by_dojutsu", {}).get(kekkei, trait_mod.get("power", 0))
        else:
            base = scaling.get("base_power_by_kekkei", {}).get(kekkei, trait_mod.get("power", 0))
        if kekkei == "Sharingan":
            base += player.get("sharingan_level", 0) * CONFIG.get("SHARINGAN", {}).get("skill_bonus_per_level", 750)
        total += int(base)
    return total

def get_nature_power(player):
    scaling = get_scaling_config()
    natures = player.get("chakra_natures", []) or []
    rare = sum(1 for nature in natures if nature in ["Ying", "Yang"])
    return (len(natures) * scaling.get("nature_power_per_nature", 120)) + (rare * scaling.get("rare_nature_bonus", 180))


def calculate_player_power_profile(player):
    """Unified v1.9.0 power model used by rankings, PvP, PvE, and balancing.

    Formula summary:
    global base power + level power + stat power + skill power + bloodline/nature power,
    then clan/village multipliers, global scale, and softcap.
    """
    player = get_effective_player(player)
    scaling = get_scaling_config()
    stats = player.get("stats", {}) or {}
    skills = player.get("affinities", {}) or {}

    stat_weights = scaling.get("stat_power_weights", {})
    skill_weights = scaling.get("affinity_power_weights", {})
    stat_base = scaling.get("base_power_by_stat", {})
    skill_base = scaling.get("base_power_by_skill", {})

    stat_power = sum((stats.get(name, 0) + stat_base.get(name, 0)) * stat_weights.get(name, 0) for name in set(stat_weights) | set(stat_base))
    skill_power = sum((skills.get(name, 0) + skill_base.get(name, 0)) * skill_weights.get(name, 0) for name in set(skill_weights) | set(skill_base))
    level_power = scaling.get("base_power_by_level", scaling.get("level_power_base", 80)) * (max(1, player.get("level", 1)) ** scaling.get("level_power_exponent", 1.18))
    bloodline_power = get_bloodline_power(player)
    nature_power = get_nature_power(player)
    global_base_power = scaling.get("global_base_power", 100)

    raw = global_base_power + stat_power + skill_power + level_power + bloodline_power + nature_power

    clan = player.get("clan") or "Civilian"
    raw *= scaling.get("clan_power_multiplier", {}).get(clan, 1.0)

    village_name = player.get("village")
    village_bonus = CONFIG.get("VILLAGES", {}).get(village_name, {}).get("skill_bonus", 0) if village_name else 0
    raw *= 1 + (village_bonus * scaling.get("village_power_multiplier_per_skill_bonus", 0.00008))

    global_power_scale = max(1, int(scaling.get("global_power_scale", 100)))
    raw *= (100 / global_power_scale)

    final = softcap(raw, scaling.get("rating_softcap_start", 25000), scaling.get("rating_softcap_strength", 0.55))
    return {
        "raw": int(raw),
        "final": max(1, int(final)),
        "stat_power": int(stat_power),
        "affinity_power": int(skill_power),
        "level_power": int(level_power),
        "bloodline_power": int(bloodline_power),
        "nature_power": int(nature_power),
    }


def calculate_scaled_resource(player, resource_type):
    player = get_effective_player(player)
    scaling = get_scaling_config()
    stats = player.get("stats", {}) or {}
    if resource_type == "hp":
        return max(
            scaling.get("duel_hp_base", 100),
            int(scaling.get("duel_hp_base", 100) + (stats.get("Health", 100) * scaling.get("duel_hp_per_health", 1.0)) + (stats.get("Durability", 100) * scaling.get("duel_hp_per_durability", 0.35)))
        )
    if resource_type == "chakra":
        return max(25, int(player.get("affinities", {}).get("Chakra Reserves", 100) * scaling.get("duel_chakra_per_reserve", 1.0)))
    if resource_type == "stamina":
        return max(25, int(stats.get("Stamina", 100) * scaling.get("duel_stamina_per_stamina", 1.0)))
    return 100


def get_power_ratio_multiplier(attacker_player, defender_player):
    scaling = get_scaling_config()
    attacker_rating = calculate_combat_rating(attacker_player)
    defender_rating = calculate_combat_rating(defender_player)
    ratio = attacker_rating / max(1, defender_rating)
    modifier = 1 + ((ratio - 1) * scaling.get("damage_ratio_strength", 0.34))
    return clamp(modifier, scaling.get("damage_ratio_min", 0.72), scaling.get("damage_ratio_max", 1.35))


def apply_damage_softcap(damage):
    scaling = get_scaling_config()
    return softcap(damage, scaling.get("damage_softcap_start", 160), scaling.get("damage_softcap_strength", 0.6))

def calculate_combat_rating(player):
    return calculate_player_power_profile(player)["final"]

def apply_global_damage_scale(damage):
    config = get_pvp_config()
    scale = max(1, int(config.get("global_damage_scale", 100)))
    return damage * (100 / scale)


def apply_damage_variance(damage):
    config = get_pvp_config()
    return damage * random.uniform(config.get("damage_variance_min", 0.94), config.get("damage_variance_max", 1.06))


def apply_level_damage_scaling(attacker_player, defender_player, damage):
    config = get_pvp_config()
    level_gap = attacker_player.get("level", 1) - defender_player.get("level", 1)
    modifier = clamp(
        level_gap * config.get("level_damage_bonus_per_level", 0.01),
        -config.get("max_level_damage_bonus", 0.20),
        config.get("max_level_damage_bonus", 0.20)
    )
    return damage * (1 + modifier)


def cap_damage_by_move(damage, defender_player, move_type):
    config = get_pvp_config()
    max_hp = calculate_scaled_resource(defender_player, "hp")
    if move_type == "jutsu":
        cap_percent = config.get("max_damage_percent_jutsu", 0.30)
    elif move_type == "heavy":
        cap_percent = config.get("max_damage_percent_heavy", 0.26)
    elif move_type == "taijutsu_combo":
        cap_percent = config.get("max_damage_percent_taijutsu_combo", 0.23)
    else:
        cap_percent = config.get("max_damage_percent_attack", 0.18)
    return min(damage, max(config.get("minimum_damage", 4), int(max_hp * cap_percent)))


def finalize_pvp_damage(damage, attacker_player, defender_player, move_type):
    config = get_pvp_config()
    scaling = get_scaling_config()
    damage = apply_level_damage_scaling(attacker_player, defender_player, damage)
    damage *= get_power_ratio_multiplier(attacker_player, defender_player)

    durability = get_player_stat(defender_player, "Durability", 100)
    reduction = min(scaling.get("defender_durability_reduction_cap", 0.32), durability / 25000)
    damage *= (1 - reduction)

    damage = apply_damage_variance(damage)
    damage = apply_global_damage_scale(damage)
    damage = apply_damage_softcap(damage)
    damage = cap_damage_by_move(damage, defender_player, move_type)

    min_floor = config.get("minimum_damage", 4)
    return max(min_floor, int(damage))


def calculate_pvp_jutsu_damage(attacker_player, defender_player, jutsu_data, duel=None, attacker_id=None):
    config = get_pvp_config()
    base_power = jutsu_data.get("base_power", 100)
    scaling_stat = jutsu_data.get("scaling", "Chakra Control")
    scaling_divisor = max(1, jutsu_data.get("scaling_divisor", 150))
    scaling_value = get_player_stat(attacker_player, scaling_stat, None)
    if scaling_value is None:
        scaling_value = get_player_affinity(attacker_player, scaling_stat, 0)

    damage = base_power
    damage += scaling_value / scaling_divisor
    damage += get_player_affinity(attacker_player, "Chakra Control", 0) * config.get("jutsu_control_multiplier", 0.0019)
    damage += get_player_affinity(attacker_player, "Chakra Reserves", 0) * config.get("jutsu_reserves_multiplier", 0.0013)
    damage += attacker_player.get("level", 1) * config.get("jutsu_level_multiplier", 1.05)
    damage += get_nature_power(attacker_player) * 0.015
    damage += get_bloodline_power(attacker_player) * 0.008

    if not jutsu_data.get("special_effects", {}).get("ignore_defense"):
        damage -= get_player_stat(defender_player, "Durability", 100) / config.get("jutsu_defense_divisor", 48)

    if duel is not None and attacker_id is not None:
        current_chakra = duel.get("chakra", {}).get(str(attacker_id), 0)
        max_chakra = max(1, duel.get("max_chakra", {}).get(str(attacker_id), current_chakra))
        if current_chakra / max_chakra <= config.get("low_chakra_damage_threshold", 0.25):
            damage *= config.get("low_chakra_damage_multiplier", 0.85)

    return finalize_pvp_damage(damage, attacker_player, defender_player, "jutsu")


def reset_player_for_reroll(player, ctx):
    remaining = player.get("rerolls_remaining", REROLLS_PER_PLAYER)
    preserved_ryo = player.get("ryo", CONFIG.get("ECONOMY", {}).get("starting_ryo", 0))
    preserved_daily_streak = player.get("daily_streak", 0)
    preserved_weekly_streak = player.get("weekly_streak", 0)
    preserved_best_daily_streak = player.get("best_daily_streak", 0)
    preserved_best_weekly_streak = player.get("best_weekly_streak", 0)
    preserved_last_daily = player.get("last_daily")
    preserved_last_weekly = player.get("last_weekly")
    preserved_total_daily_claims = player.get("total_daily_claims", 0)
    preserved_total_weekly_claims = player.get("total_weekly_claims", 0)
    clan, stats, affinities, chakra_nature, dual_nature, kekkei_genkai = roll_character()
    chakra_natures = [chakra_nature]
    if dual_nature:
        chakra_natures.append(dual_nature)
    player.clear()
    player.update({
        "name": ctx.author.name,
        "level": 1,
        "xp": 0,
        "rank": "Academy Student",
        "stats": stats,
        "affinities": affinities,
        "chakra_natures": chakra_natures,
        "clan": clan,
        "kekkei_genkai": kekkei_genkai,
        "kekkei_genkai_list": [kekkei_genkai] if kekkei_genkai else [],
        "kekkei_evolution": {kekkei_genkai: 0} if kekkei_genkai else {},
        "bloodline_fragments": {},
        "devmode_enabled": False,
        "inventory": {},
        "hidden_skill_score": 0,
        "has_rolled": True,
        "last_training": None,
        "known_jutsu": [],
        "village": None,
        "rerolls_remaining": remaining,
        "ryo": preserved_ryo,
        "daily_streak": preserved_daily_streak,
        "weekly_streak": preserved_weekly_streak,
        "best_daily_streak": preserved_best_daily_streak,
        "best_weekly_streak": preserved_best_weekly_streak,
        "last_daily": preserved_last_daily,
        "last_weekly": preserved_last_weekly,
        "total_daily_claims": preserved_total_daily_claims,
        "total_weekly_claims": preserved_total_weekly_claims
    })
    player["hidden_skill_score"] = calculate_hidden_skill_score(player)
    return player

def calculate_pvp_hit_chance(attacker_player, defender_player, move_type, duel=None, attacker_id=None):
    config = get_pvp_config()
    base = config.get("base_hit_chance", 0.76)
    attacker_speed = get_player_affinity(attacker_player, "Speed", 0)
    attacker_control = get_player_affinity(attacker_player, "Chakra Control", 0)
    attacker_accuracy = get_player_stat(attacker_player, "Accuracy", 100)
    defender_speed = get_player_affinity(defender_player, "Speed", 0)
    accuracy = (attacker_accuracy * config.get("accuracy_stat_weight", 0.45)) + (attacker_speed * config.get("accuracy_speed_weight", 0.35)) + (attacker_control * config.get("accuracy_control_weight", 0.20))
    evasion = defender_speed * config.get("evasion_speed_weight", 0.75)
    hit = base + ((accuracy - evasion) / max(1, config.get("hit_rating_divisor", 25000)))
    level_gap = attacker_player.get("level", 1) - defender_player.get("level", 1)
    hit += clamp(level_gap * config.get("level_hit_bonus", 0.003), -config.get("max_level_hit_bonus", 0.06), config.get("max_level_hit_bonus", 0.06))
    if move_type == "heavy":
        hit -= config.get("heavy_hit_penalty", 0.12)
    elif move_type == "jutsu":
        hit -= config.get("jutsu_hit_penalty", 0.04)
    elif move_type == "transformation":
        hit -= config.get("transformation_hit_penalty", 0.02)
    elif move_type == "genjutsu":
        hit -= config.get("genjutsu_hit_penalty", 0.06)
    if duel is not None and attacker_id is not None:
        attacker_id = str(attacker_id)
        statuses = duel.get("statuses", {}).get(attacker_id, {})
        confused = statuses.get("confused")
        if confused:
            hit -= confused.get("miss_penalty", config.get("genjutsu_miss_penalty", 0.18))
        current_chakra = duel.get("chakra", {}).get(attacker_id, 0)
        max_chakra = max(1, duel.get("max_chakra", {}).get(attacker_id, current_chakra))
        if current_chakra / max_chakra <= config.get("low_chakra_hit_threshold", 0.25):
            hit -= config.get("low_chakra_hit_penalty", 0.04)
    return clamp(hit, config.get("min_hit_chance", 0.48), config.get("max_hit_chance", 0.93))


def calculate_basic_pvp_damage(attacker_player, defender_player, move_type):
    config = get_pvp_config()
    taijutsu = get_player_affinity(attacker_player, "Taijutsu", 0)
    speed = get_player_affinity(attacker_player, "Speed", 0)
    durability = get_player_stat(defender_player, "Durability", 100)
    level = attacker_player.get("level", 1)

    if move_type == "heavy":
        damage = config.get("heavy_base_damage", 24)
        damage += taijutsu * config.get("heavy_taijutsu_multiplier", 0.0027)
        damage += speed * 0.0008
        damage += level * config.get("heavy_level_multiplier", 1.8)
    elif move_type == "taijutsu_combo":
        damage = config.get("combo_base_damage", 20)
        damage += taijutsu * config.get("combo_taijutsu_multiplier", 0.0023)
        damage += speed * 0.0011
        damage += level * config.get("combo_level_multiplier", 1.5)
    else:
        damage = config.get("attack_base_damage", 14)
        damage += taijutsu * config.get("attack_taijutsu_multiplier", 0.0019)
        damage += speed * 0.0006
        damage += level * config.get("attack_level_multiplier", 1.2)

    damage -= durability * config.get("durability_damage_reduction_multiplier", 0.030)
    damage += get_bloodline_power(attacker_player) * 0.004
    return finalize_pvp_damage(damage, attacker_player, defender_player, move_type)


def get_status_summary(duel, user_id):
    statuses = duel.get("statuses", {}).get(str(user_id), {})
    if not statuses:
        return "None"
    parts = []
    for name, data in statuses.items():
        parts.append(f"{name.title()} ({data.get('turns', 0)} turn(s))")
    return ", ".join(parts)


def add_status_effect(duel, target_id, status_type, turns, damage=0, miss_penalty=0):
    target_id = str(target_id)
    duel.setdefault("statuses", {}).setdefault(target_id, {})
    duel["statuses"][target_id][status_type] = {
        "turns": max(1, int(turns)),
        "damage": max(0, int(damage)),
        "miss_penalty": max(0, float(miss_penalty))
    }


def apply_start_of_turn_effects(duel, user_id):
    user_id = str(user_id)
    messages = []
    skipped = False
    statuses = duel.setdefault("statuses", {}).setdefault(user_id, {})

    for status_name in list(statuses.keys()):
        data = statuses[status_name]
        turns = data.get("turns", 0)
        damage = data.get("damage", 0)

        if status_name in ["burn", "bleed", "annihilation"] and damage > 0:
            duel["hp"][user_id] = max(0, duel["hp"].get(user_id, 0) - damage)
            messages.append(f"**{status_name.title()}** deals **{damage}** damage to {get_duel_member_text(duel, user_id)}.")

        if status_name == "regen" and damage > 0:
            max_hp = duel.get("max_hp", {}).get(user_id, duel["hp"].get(user_id, 0) + damage)
            duel["hp"][user_id] = min(max_hp, duel["hp"].get(user_id, 0) + damage)
            messages.append(f"**Regen** restores **{damage}** HP to {get_duel_member_text(duel, user_id)}.")

        if status_name == "stun":
            skipped = True
            messages.append(f"{get_duel_member_text(duel, user_id)} is **stunned** and loses this turn.")

        data["turns"] = turns - 1
        if data["turns"] <= 0:
            del statuses[status_name]

    return messages, skipped


def build_duel_status_text(duel):
    lines = []
    for player_id in duel["players"]:
        lines.append(
            f"**{get_duel_member_text(duel, player_id)}**\n"
            f"{resource_line('HP', duel['hp'].get(player_id, 0), duel.get('max_hp', {}).get(player_id, duel['hp'].get(player_id, 0)))}\n"
            f"{resource_line('Chakra', duel['chakra'].get(player_id, 0), duel.get('max_chakra', {}).get(player_id, duel['chakra'].get(player_id, 0)))}\n"
            f"{resource_line('Stamina', duel['stamina'].get(player_id, 0), duel.get('max_stamina', {}).get(player_id, duel['stamina'].get(player_id, 0)))}\n"
            f"**Status:** {get_status_summary(duel, player_id)}"
        )
    return "\n\n".join(lines)

def advance_duel_turn(duel):
    current = duel["turn"]
    opponent = get_duel_opponent_id(duel, current)
    duel["turn"] = opponent
    duel["round"] = duel.get("round", 1) + 1
    duel["last_action_at"] = now_utc().isoformat()
    return opponent


async def finish_turn_duel(ctx, duel_key, winner_id, loser_id):
    players = migrate_all_players(load_players())
    winner_player = players.get(str(winner_id))

    result = f"🏆 **{get_duel_member_text(ACTIVE_DUELS[duel_key], winner_id)} wins against {get_duel_member_text(ACTIVE_DUELS[duel_key], loser_id)}!**"

    if winner_player:
        config = get_pvp_config()
        xp_reward = random.randint(config.get("win_xp_min", 50), config.get("win_xp_max", 110))
        econ = CONFIG.get("ECONOMY", {})
        ryo_reward = random.randint(econ.get("combat_win_ryo_min", 35), econ.get("combat_win_ryo_max", 90))
        winner_player["ryo"] = int(winner_player.get("ryo", 0)) + ryo_reward
        leveled_up, levels_gained = add_xp(winner_player, xp_reward)
        sharingan_upgrade = try_level_sharingan(winner_player, source="fight")
        evolution_upgrade = try_auto_kekkei_evolution(winner_player, source="fight")
        players[str(winner_id)] = winner_player
        save_players(players)
        result += f"\n**XP Reward:** {xp_reward}"
        result += f"\n**Ryo Reward:** {ryo_reward}"
        if sharingan_upgrade:
            result += f"\n**Sharingan Progression:** {sharingan_upgrade}"
        if evolution_upgrade:
            result += f"\n**Bloodline Evolution:** {evolution_upgrade}"
        if leveled_up:
            result += f"\n**Level Up:** +{levels_gained} level(s). Now level {winner_player['level']} — {winner_player['rank']}."

    del ACTIVE_DUELS[duel_key]
    embed = ui_embed("Duel Complete", result, "success")
    await ctx.send(embed=embed)
    await process_tournament_match_result(ctx.channel, winner_id, loser_id)


async def start_turn_based_duel(ctx, challenger, opponent):
    players = migrate_all_players(load_players())
    challenger_id = str(challenger.id)
    opponent_id = str(opponent.id)

    duel_key = get_duel_key(ctx.channel.id, challenger_id, opponent_id)
    if duel_key in ACTIVE_DUELS:
        await send_notice(ctx, "Duel Already Active", "That duel is already active.", "warning")
        return

    first_turn = challenger_id if random.random() < 0.5 else opponent_id
    config = get_pvp_config()
    effective_players = {
        challenger_id: get_effective_player(players[challenger_id]),
        opponent_id: get_effective_player(players[opponent_id])
    }

    ACTIVE_DUELS[duel_key] = {
        "channel_id": ctx.channel.id,
        "players": [challenger_id, opponent_id],
        "members": {
            challenger_id: {"name": challenger.name, "mention": challenger.mention},
            opponent_id: {"name": opponent.name, "mention": opponent.mention}
        },
        "hp": {
            challenger_id: calculate_scaled_resource(players[challenger_id], "hp"),
            opponent_id: calculate_scaled_resource(players[opponent_id], "hp")
        },
        "max_hp": {
            challenger_id: calculate_scaled_resource(players[challenger_id], "hp"),
            opponent_id: calculate_scaled_resource(players[opponent_id], "hp")
        },
        "chakra": {
            challenger_id: calculate_scaled_resource(players[challenger_id], "chakra"),
            opponent_id: calculate_scaled_resource(players[opponent_id], "chakra")
        },
        "max_chakra": {
            challenger_id: calculate_scaled_resource(players[challenger_id], "chakra"),
            opponent_id: calculate_scaled_resource(players[opponent_id], "chakra")
        },
        "stamina": {
            challenger_id: calculate_scaled_resource(players[challenger_id], "stamina"),
            opponent_id: calculate_scaled_resource(players[opponent_id], "stamina")
        },
        "max_stamina": {
            challenger_id: calculate_scaled_resource(players[challenger_id], "stamina"),
            opponent_id: calculate_scaled_resource(players[opponent_id], "stamina")
        },
        "guard": {challenger_id: False, opponent_id: False},
        "statuses": {challenger_id: {}, opponent_id: {}},
        "turn": first_turn,
        "round": 1,
        "started_at": now_utc().isoformat(),
        "last_action_at": now_utc().isoformat()
    }

    embed = duel_embed("Turn-Based Duel Started", ACTIVE_DUELS[duel_key], f"{challenger.mention} vs {opponent.mention}", "brand")
    add_field(embed, "First Turn", get_duel_member_text(ACTIVE_DUELS[duel_key], first_turn), False)
    await ctx.send(embed=embed)

async def handle_turn_action(ctx, action_type, jutsu_name=None):
    duel_key, duel = find_active_duel_for_user(ctx.author.id, ctx.channel.id)
    if not duel:
        await send_notice(ctx, "No Active Duel", "You are not in an active duel in this channel.", "warning")
        return

    actor_id = str(ctx.author.id)
    if duel.get("turn") != actor_id:
        await send_notice(ctx, "Not Your Turn", f"Current turn: {get_duel_member_text(duel, duel.get('turn'))}", "warning")
        return

    opponent_id = get_duel_opponent_id(duel, actor_id)
    players = migrate_all_players(load_players())
    actor_player = players.get(actor_id)
    opponent_player = players.get(opponent_id)

    if actor_player:
        actor_player = get_effective_player(actor_player)
    if opponent_player:
        opponent_player = get_effective_player(opponent_player)

    if not actor_player or not opponent_player:
        del ACTIVE_DUELS[duel_key]
        await send_notice(ctx, "Duel Cancelled", "A player profile was missing, so the duel was cancelled.", "danger")
        return

    start_messages, stunned = apply_start_of_turn_effects(duel, actor_id)
    if duel["hp"].get(actor_id, 0) <= 0:
        embed = ui_embed("Turn Effects", "\n".join(start_messages), "warning")
        await ctx.send(embed=embed)
        await finish_turn_duel(ctx, duel_key, opponent_id, actor_id)
        return

    start_messages.extend(regenerate_duel_resources(duel, actor_id))

    if stunned:
        next_turn = advance_duel_turn(duel)
        embed = duel_embed("Turn Skipped", duel, "\n".join(start_messages), "warning")
        add_field(embed, "Next Turn", get_duel_member_text(duel, next_turn), False)
        await ctx.send(embed=embed)
        return

    resource_check_action = action_type
    if action_type == "jutsu":
        resource_check_action = "jutsu"

    can_spend, resource_error = has_combat_resource(duel, actor_id, resource_check_action)
    if not can_spend:
        await send_notice(ctx, "Not Enough Resources", resource_error, "warning")
        return

    messages = start_messages[:]

    if action_type == "defend":
        stamina_cost, chakra_cost = spend_combat_resource(duel, actor_id, "defend")
        durability = actor_player.get("stats", {}).get("Durability", 100)
        config = get_pvp_config()
        bonus = min(config.get("defend_bonus_cap", 0.25), durability / config.get("defend_durability_divisor", 25000))
        duel["guard"][actor_id] = max(0.15, config.get("defend_damage_multiplier", 0.5) - bonus)
        next_turn = advance_duel_turn(duel)
        action_text = "\n".join(messages + [f"{ctx.author.mention} takes a defensive stance. Incoming damage will be reduced once. {build_cost_text(stamina_cost, chakra_cost)}"])
        embed = duel_embed("Defensive Stance", duel, action_text, "info")
        add_field(embed, "Next Turn", get_duel_member_text(duel, next_turn), False)
        await ctx.send(embed=embed)
        return

    if action_type == "jutsu":
        matched_name = find_jutsu_name(jutsu_name or "")
        if not matched_name:
            await send_notice(ctx, "Jutsu Not Found", "That jutsu does not exist. Use `$myjutsu` to see what you know.", "warning")
            return
        if matched_name not in actor_player.get("known_jutsu", []):
            await send_notice(ctx, "Jutsu Not Learned", "You do not know that jutsu yet.", "warning")
            return

        jutsu_data = JUTSU[matched_name]
        cost = jutsu_data.get("chakra_cost", 0)
        if duel["chakra"].get(actor_id, 0) < cost:
            await send_notice(ctx, "Not Enough Chakra", f"**{matched_name}** needs **{cost} chakra**. Current chakra: **{duel['chakra'].get(actor_id, 0)}**.", "warning")
            return

        duel["chakra"][actor_id] = max(0, duel["chakra"][actor_id] - cost)
        stamina_cost, chakra_cost = 0, cost
        hit_chance = calculate_pvp_hit_chance(actor_player, opponent_player, "jutsu", duel, actor_id)
        move_label = matched_name
        special_effects = jutsu_data.get("special_effects", {})
        damage = calculate_pvp_jutsu_damage(actor_player, opponent_player, jutsu_data, duel, actor_id)
    elif action_type == "transformation":
        stamina_cost, chakra_cost = spend_combat_resource(duel, actor_id, "transformation")
        hit_chance = calculate_pvp_hit_chance(actor_player, opponent_player, "transformation", duel, actor_id)
        move_label = "Transformation"
        damage = 0
        jutsu_data = None
        matched_name = None
        special_effects = {}
    elif action_type == "genjutsu":
        stamina_cost, chakra_cost = spend_combat_resource(duel, actor_id, "genjutsu")
        hit_chance = calculate_pvp_hit_chance(actor_player, opponent_player, "genjutsu", duel, actor_id)
        move_label = "Genjutsu"
        damage = 0
        jutsu_data = None
        matched_name = None
        special_effects = {}
    else:
        stamina_cost, chakra_cost = spend_combat_resource(duel, actor_id, action_type)
        hit_chance = calculate_pvp_hit_chance(actor_player, opponent_player, action_type, duel, actor_id)
        if action_type == "heavy":
            move_label = "Heavy Attack"
        elif action_type == "taijutsu_combo":
            move_label = "Taijutsu Combo"
        else:
            move_label = "Attack"
        damage = calculate_basic_pvp_damage(actor_player, opponent_player, action_type)
        jutsu_data = None
        matched_name = None
        special_effects = {}

    if not tsuchi_cannot_miss(actor_player) and not special_effects.get("cannot_miss") and random.random() > hit_chance:
        next_turn = advance_duel_turn(duel)
        action_text = "\n".join(messages + [f"{ctx.author.mention} used **{move_label}**, but missed. {build_cost_text(stamina_cost, chakra_cost)}"])
        embed = duel_embed("Attack Missed", duel, action_text, "warning")
        add_field(embed, "Next Turn", get_duel_member_text(duel, next_turn), False)
        await ctx.send(embed=embed)
        return

    damage = apply_tsuchi_damage_bonus(actor_player, damage)
    damage = apply_tsuchi_defense_bonus(opponent_player, damage)

    if duel["guard"].get(opponent_id):
        reduction = duel["guard"].get(opponent_id)
        if reduction is True:
            reduction = get_pvp_config().get("defend_damage_multiplier", 0.5)
        damage = max(1, int(damage * float(reduction))) if damage > 0 else 0
        duel["guard"][opponent_id] = False
        messages.append(f"🛡️ {get_duel_member_text(duel, opponent_id)} defended and reduced the damage.")

    if action_type == "transformation":
        config = get_pvp_config()
        chance = config.get("transformation_stun_chance", 65) + actor_player.get("affinities", {}).get("Chakra Control", 0) // config.get("transformation_control_divisor", 500)
        chance = min(config.get("transformation_stun_cap", 90), chance)
        if not tsuchi_immune_to_status(opponent_player) and random.randint(1, 100) <= chance:
            add_status_effect(duel, opponent_id, "stun", 1, 0)
            messages.append(f"🪵 {ctx.author.mention} used **Transformation** and fooled {get_duel_member_text(duel, opponent_id)}. They are stunned for 1 turn. {build_cost_text(stamina_cost, chakra_cost)}")
        else:
            messages.append(f"🪵 {ctx.author.mention} used **Transformation**, but {get_duel_member_text(duel, opponent_id)} saw through it. {build_cost_text(stamina_cost, chakra_cost)}")
    elif action_type == "genjutsu":
        config = get_pvp_config()
        chance = config.get("genjutsu_apply_chance", 60) + actor_player.get("affinities", {}).get("Genjutsu", 0) // config.get("genjutsu_apply_divisor", 450)
        chance = min(config.get("genjutsu_apply_cap", 92), chance)
        penalty = config.get("genjutsu_miss_penalty", 0.18) + min(config.get("genjutsu_penalty_cap", 0.12), actor_player.get("affinities", {}).get("Genjutsu", 0) / config.get("genjutsu_penalty_divisor", 100000))
        if not tsuchi_immune_to_status(opponent_player) and random.randint(1, 100) <= chance:
            add_status_effect(duel, opponent_id, "confused", config.get("genjutsu_turns", 2), 0, penalty)
            messages.append(f"🌀 {ctx.author.mention} used **Genjutsu**. {get_duel_member_text(duel, opponent_id)} has a higher miss chance for {config.get('genjutsu_turns', 2)} turns. {build_cost_text(stamina_cost, chakra_cost)}")
        else:
            messages.append(f"🌀 {ctx.author.mention} used **Genjutsu**, but it failed to take hold. {build_cost_text(stamina_cost, chakra_cost)}")
    else:
        duel["hp"][opponent_id] = max(0, duel["hp"].get(opponent_id, 0) - damage)
        messages.append(f"⚔️ {ctx.author.mention} used **{move_label}** and dealt **{damage}** damage. {build_cost_text(stamina_cost, chakra_cost)}")

    if action_type == "jutsu" and special_effects:
        instant_kill_chance = special_effects.get("instant_kill_chance", 0)
        if instant_kill_chance and random.randint(1, 100) <= instant_kill_chance:
            duel["hp"][opponent_id] = 0
            messages.append(f"🌑 **{move_label}** triggered instant annihilation.")

        chakra_drain = special_effects.get("drain_enemy_chakra", 0)
        if chakra_drain:
            drained = min(duel["chakra"].get(opponent_id, 0), int(chakra_drain))
            duel["chakra"][opponent_id] = max(0, duel["chakra"].get(opponent_id, 0) - drained)
            messages.append(f"🌑 **{move_label}** drained **{drained}** chakra.")

        self_heal_percent = special_effects.get("self_heal_percent", 0)
        if self_heal_percent:
            max_hp = duel.get("max_hp", {}).get(actor_id, duel["hp"].get(actor_id, 0))
            heal_amount = int(max_hp * (self_heal_percent / 100))
            duel["hp"][actor_id] = min(max_hp, duel["hp"].get(actor_id, 0) + heal_amount)
            messages.append(f"🌑 **{move_label}** restored **{heal_amount}** HP to {get_duel_member_text(duel, actor_id)}.")

    if action_type == "jutsu" and jutsu_data:
        status = jutsu_data.get("status_effect")
        if status and random.randint(1, 100) <= status.get("chance", 0):
            status_type = status.get("type")
            status_damage = abs(int(status.get("damage", 0)))

            if status_type == "regen":
                add_status_effect(
                    duel,
                    actor_id,
                    status_type,
                    status.get("turns", 1),
                    status_damage
                )
                messages.append(f"✨ **{matched_name}** activated **Regen**.")
            elif tsuchi_immune_to_status(opponent_player):
                messages.append(f"🌑 {get_duel_member_text(duel, opponent_id)} resisted **{status_type.title()}** through Tsuchi bloodline power.")
            elif status_type == "confused":
                add_status_effect(
                    duel,
                    opponent_id,
                    status_type,
                    status.get("turns", 1),
                    0,
                    status.get("miss_penalty", get_pvp_config().get("genjutsu_miss_penalty", 0.18))
                )
                messages.append(f"✨ **{matched_name}** inflicted **Confused**. Enemy accuracy is lowered.")
            elif status_type in ["burn", "stun", "bleed", "annihilation"]:
                add_status_effect(
                    duel,
                    opponent_id,
                    status_type,
                    status.get("turns", 1),
                    status_damage
                )
                messages.append(f"✨ **{matched_name}** inflicted **{status_type.title()}**.")

    if duel["hp"].get(opponent_id, 0) <= 0:
        embed = duel_embed("Duel Action", duel, "\n".join(messages), "brand")
        await ctx.send(embed=embed)
        await finish_turn_duel(ctx, duel_key, actor_id, opponent_id)
        return

    next_turn = advance_duel_turn(duel)
    embed = duel_embed("Duel Action", duel, "\n".join(messages), "brand")
    add_field(embed, "Next Turn", get_duel_member_text(duel, next_turn), False)
    await ctx.send(embed=embed)


@bot.event

async def on_ready():
    global RANDOM_EVENT_TASK_STARTED, LOTTERY_TASK_STARTED

    print(f"Bot is online as {bot.user}")
    try:
        banner_key, banner_data = get_banner()
        print(f"Weekly banner active: {banner_data.get('name', banner_key)} ({banner_key})")
    except Exception as exc:
        print(f"Could not load weekly banner: {exc}")

    if not RANDOM_EVENT_TASK_STARTED:
        RANDOM_EVENT_TASK_STARTED = True
        bot.loop.create_task(random_event_loop())

    if not LOTTERY_TASK_STARTED:
        LOTTERY_TASK_STARTED = True
        bot.loop.create_task(lottery_loop())


@bot.command(name="start")
async def start(ctx):
    players = migrate_all_players(load_players())
    user_id = str(ctx.author.id)

    if user_id in players:
        await send_notice(ctx, "Profile Already Exists", "Use `$profile` to view your shinobi card.", "warning")
        return

    players[user_id] = create_player(ctx)
    save_players(players)

    embed = ui_embed("Shinobi Profile Created", f"{ctx.author.mention}, your profile is ready.", "success")
    add_field(embed, "Next Step", "Use `$roll` to discover your clan, affinities, chakra nature, and possible Kekkei Genkai.", False)
    await ctx.send(embed=embed)


@bot.command(name="roll")
async def roll(ctx):
    players = migrate_all_players(load_players())
    user_id = str(ctx.author.id)

    if user_id not in players:
        await send_notice(ctx, "Profile Required", "Use `$start` first to create your shinobi profile.", "warning")
        return

    if players[user_id]["has_rolled"]:
        await send_notice(ctx, "Already Rolled", "You have already rolled. Use `$reroll` if you still have rerolls available.", "warning")
        return

    clan, stats, affinities, chakra_nature, dual_nature, kekkei_genkai = roll_character()

    chakra_natures = [chakra_nature]
    if dual_nature:
        chakra_natures.append(dual_nature)

    players[user_id]["clan"] = clan
    players[user_id]["stats"] = stats
    players[user_id]["affinities"] = affinities
    players[user_id]["chakra_natures"] = chakra_natures
    players[user_id]["kekkei_genkai"] = kekkei_genkai
    players[user_id]["kekkei_genkai_list"] = [kekkei_genkai] if kekkei_genkai else []
    players[user_id]["kekkei_evolution"] = {kekkei_genkai: 0} if kekkei_genkai else {}
    players[user_id].setdefault("bloodline_fragments", {})
    players[user_id]["has_rolled"] = True
    players[user_id]["hidden_skill_score"] = calculate_hidden_skill_score(players[user_id])

    save_players(players)

    embed = discord.Embed(
        title=f"{ctx.author.name}'s Ninja Roll",
        description="Your clan, skills, chakra nature, and bloodline path have been decided.",
        color=discord.Color.orange()
    )

    embed.add_field(name="Clan", value=clan, inline=False)

    for name, value in affinities.items():
        embed.add_field(name=name, value=f"{value}/10000 — {get_skill_level(value)}", inline=False)

    embed.add_field(name="Chakra Nature", value=", ".join(chakra_natures), inline=False)
    embed.add_field(name="Kekkei Genkai", value=format_player_bloodlines(players[user_id]), inline=False)

    await ctx.send(embed=embed)


@bot.command(name="reroll")
async def reroll(ctx):
    players = migrate_all_players(load_players())
    user_id = str(ctx.author.id)
    if user_id not in players:
        await send_notice(ctx, "Profile Required", "Use `$start` first to create your shinobi profile.", "warning")
        return
    duel_key, duel = find_active_duel_for_user(ctx.author.id, ctx.channel.id)
    if duel:
        await send_notice(ctx, "Reroll Blocked", "You cannot reroll during an active duel.", "warning")
        return
    player = players[user_id]
    if not player.get("has_rolled"):
        await send_notice(ctx, "Roll Required", "Use `$roll` first. Rerolls are only available after your first roll.", "warning")
        return
    remaining = int(player.get("rerolls_remaining", REROLLS_PER_PLAYER))
    if remaining <= 0:
        await send_notice(ctx, "No Rerolls Remaining", "Rerolls only reset after a full developer world reset.", "danger")
        return
    player["rerolls_remaining"] = remaining - 1
    players[user_id] = reset_player_for_reroll(player, ctx)
    save_players(players)
    new_player = players[user_id]
    embed = discord.Embed(title=f"{ctx.author.name}'s Reroll", description=f"Rerolls remaining: **{new_player['rerolls_remaining']}**", color=discord.Color.orange())
    embed.add_field(name="Clan", value=new_player.get("clan") or "None", inline=False)
    for name, value in new_player.get("affinities", {}).items():
        embed.add_field(name=name, value=f"{value}/10000 — {get_skill_level(value)}", inline=False)
    embed.add_field(name="Chakra Nature", value=", ".join(new_player.get("chakra_natures", [])), inline=False)
    embed.add_field(name="Kekkei Genkai", value=format_player_bloodlines(new_player), inline=False)
    await ctx.send(embed=embed)




def resolve_player_record(players, member):
    user_id = str(member.id)
    if user_id not in players:
        return None, user_id
    return players[user_id], user_id


def build_player_profile_embed(member, player):
    sharingan_name = get_sharingan_level_name(player)
    curse_mark = player.get("curse_mark")
    curse_text = "None"

    if curse_mark:
        try:
            expiry = datetime.fromisoformat(curse_mark["expires_at"])
            if now_utc() < expiry:
                effect = "Buff" if curse_mark.get("type") == "buff" else "Debuff"
                percent = int(abs(curse_mark.get("multiplier", 1.0) - 1) * 100)
                curse_text = f"{effect} {percent}% until {expiry.strftime('%Y-%m-%d %H:%M UTC')}"
            else:
                player["curse_mark"] = None
        except Exception:
            curse_text = "Unknown"

    level = player.get("level", 1)
    required_xp = get_required_xp(level)
    embed = ui_embed(
        f"{member.name}'s Shinobi Card",
        f"{ICONS['rank']} **{player.get('rank', 'Academy Student')}**  •  {ICONS['level']} Level **{level}**",
        "success"
    )

    add_field(embed, "Progress", "\n".join([
        resource_line("XP", player.get("xp", 0), required_xp),
        kv_line("Rerolls", player.get("rerolls_remaining", REROLLS_PER_PLAYER)),
        kv_line("Ryo", f"{fmt_num(player.get('ryo', 0))} Ryo"),
        kv_line("Streaks", f"Daily {player.get('daily_streak', 0)} • Weekly {player.get('weekly_streak', 0)}"),
    ]), False)

    bloodline_text = kv_line("Kekkei Genkai", format_player_bloodlines(player))
    if sharingan_name:
        bloodline_text += f"\n{kv_line('Sharingan', sharingan_name)}"

    add_field(embed, "Identity", "\n".join([
        kv_line("Clan", player.get("clan") or "Not rolled"),
        kv_line("Village", player.get("village") or "None"),
        kv_line("Title", player.get("title") or "None"),
        bloodline_text,
        kv_line("Chakra Nature", compact_list(player.get("chakra_natures", []), "Not rolled")),
    ]), False)

    add_field(embed, "Active Effects", "\n".join([
        kv_line("Jinchuriki", format_active_tailed_beast(player)),
        kv_line("Curse Mark", curse_text),
    ]), False)

    stats_text = "\n".join(f"**{stat}:** {fmt_num(value)}" for stat, value in player.get("stats", {}).items()) or "None"
    add_field(embed, "Base Stats", stats_text, True)

    if player.get("affinities"):
        affinity_text = "\n".join(
            f"**{name}:** {fmt_num(value)} — {get_skill_level(value)}"
            for name, value in player.get("affinities", {}).items()
        )
    else:
        affinity_text = "Use `$roll` to reveal affinities."
    add_field(embed, "Affinities", affinity_text, True)

    known_jutsu = player.get("known_jutsu", [])
    inventory = player.get("inventory", {})
    inventory_text = ", ".join(f"{item} x{amount}" for item, amount in inventory.items()) or "Empty"
    add_field(embed, "Loadout", "\n".join([
        kv_line("Known Jutsu", compact_list(known_jutsu, "None learned", 6)),
        kv_line("Inventory", truncate_text(inventory_text, 450)),
    ]), False)

    return embed

def get_top_traits(player, limit=3):
    combined = {}
    for name, value in player.get("stats", {}).items():
        combined[name] = combined.get(name, 0) + value
    for name, value in player.get("affinities", {}).items():
        combined[name] = combined.get(name, 0) + value
    return sorted(combined.items(), key=lambda item: item[1], reverse=True)[:limit]


def get_low_traits(player, limit=3):
    combined = {}
    for name, value in player.get("stats", {}).items():
        combined[name] = combined.get(name, 0) + value
    for name, value in player.get("affinities", {}).items():
        combined[name] = combined.get(name, 0) + value
    return sorted(combined.items(), key=lambda item: item[1])[:limit]


def get_build_archetype(player):
    affinities = player.get("affinities") or {}
    stats = player.get("stats") or {}
    taijutsu = affinities.get("Taijutsu", 0)
    control = affinities.get("Chakra Control", 0)
    reserves = affinities.get("Chakra Reserves", 0) + stats.get("Chakra Reserves", 0)
    genjutsu = affinities.get("Genjutsu", 0)
    speed = affinities.get("Speed", 0)
    durability = stats.get("Durability", 0)

    scores = {
        "Taijutsu Bruiser": taijutsu + speed + durability,
        "Ninjutsu Caster": control + reserves,
        "Genjutsu Controller": genjutsu + control,
        "Speed Duelist": speed + taijutsu,
        "Tank/Endurance": durability + stats.get("Health", 0) + stats.get("Stamina", 0),
    }
    return max(scores.items(), key=lambda item: item[1])


def estimate_move_damage_against_self(player, move_type):
    if move_type == "jutsu":
        known = player.get("known_jutsu", [])
        if known:
            name = known[0]
            return name, calculate_pvp_jutsu_damage(player, player, JUTSU.get(name, {}))
        return "No learned jutsu", None
    return move_type.title().replace("_", " "), calculate_basic_pvp_damage(player, player, move_type)


def build_damage_formula_text(player):
    profile = calculate_player_power_profile(player)
    scaling = get_scaling_config()
    lines = [
        f"**Final Combat Rating:** {profile['final']:,} from raw {profile['raw']:,}",
        f"**Stats Power:** {profile['stat_power']:,}",
        f"**Affinity Power:** {profile['affinity_power']:,}",
        f"**Level Power:** {profile['level_power']:,}",
        f"**Bloodline Power:** {profile['bloodline_power']:,}",
        f"**Nature Power:** {profile['nature_power']:,}",
        f"**Damage Rule:** base move + trait scaling × attacker/defender power ratio, then durability reduction, variance, global scale, softcap, and max-HP cap.",
        f"**Softcaps:** Rating starts softcapping at {scaling.get('rating_softcap_start', 25000):,}; damage starts softcapping at {scaling.get('damage_softcap_start', 160):,}."
    ]
    return "\n".join(lines)

@bot.command(name="profile")
async def profile(ctx, member: discord.Member = None):
    players = migrate_all_players(load_players())
    member = member or ctx.author
    player, user_id = resolve_player_record(players, member)

    if not player:
        if member.id == ctx.author.id:
            await send_notice(ctx, "Profile Required", "You do not have a profile yet. Use `$start`.", "warning")
        else:
            await send_notice(ctx, "Profile Not Found", f"{member.mention} does not have a profile yet.", "warning")
        return

    embed = build_player_profile_embed(member, player)
    save_players(players)
    await ctx.send(embed=embed)


@bot.command(name="compare")
async def compare(ctx, member: discord.Member):
    players = migrate_all_players(load_players())
    author_id = str(ctx.author.id)
    target_id = str(member.id)

    if author_id not in players:
        await send_notice(ctx, "Profile Required", "Use `$start` first to create your shinobi profile.", "warning")
        return

    if target_id not in players:
        await send_notice(ctx, "Profile Not Found", f"{member.mention} does not have a profile yet.", "warning")
        return

    user_player = players[author_id]
    target_player = players[target_id]

    if not user_player.get("has_rolled") or not target_player.get("has_rolled"):
        await send_notice(ctx, "Roll Required", "Both players need to use `$roll` before comparing builds.", "warning")
        return

    user_rating = calculate_combat_rating(user_player)
    target_rating = calculate_combat_rating(target_player)
    diff = user_rating - target_rating

    embed = discord.Embed(
        title=f"Build Compare: {ctx.author.name} vs {member.name}",
        description=f"Combat Rating Difference: **{diff:+,}**",
        color=discord.Color.blurple()
    )

    embed.add_field(
        name=ctx.author.name,
        value=(
            f"**Level:** {user_player.get('level', 1)}\n"
            f"**Rank:** {user_player.get('rank', 'Academy Student')}\n"
            f"**Clan:** {user_player.get('clan') or 'None'}\n"
            f"**Combat Rating:** {user_rating:,}\n"
            f"**Top Traits:** {', '.join(f'{n} ({v})' for n, v in get_top_traits(user_player))}"
        ),
        inline=False
    )

    embed.add_field(
        name=member.name,
        value=(
            f"**Level:** {target_player.get('level', 1)}\n"
            f"**Rank:** {target_player.get('rank', 'Academy Student')}\n"
            f"**Clan:** {target_player.get('clan') or 'None'}\n"
            f"**Combat Rating:** {target_rating:,}\n"
            f"**Top Traits:** {', '.join(f'{n} ({v})' for n, v in get_top_traits(target_player))}"
        ),
        inline=False
    )

    if diff > 0:
        verdict = f"{ctx.author.mention} has the stronger current combat rating."
    elif diff < 0:
        verdict = f"{member.mention} has the stronger current combat rating."
    else:
        verdict = "Both players are basically even on combat rating."

    embed.add_field(name="Verdict", value=verdict, inline=False)
    await ctx.send(embed=embed)


@bot.command(name="build")
async def build(ctx, member: discord.Member = None):
    players = migrate_all_players(load_players())
    member = member or ctx.author
    player, user_id = resolve_player_record(players, member)

    if not player:
        await send_notice(ctx, "Profile Required", f"{member.mention} does not have a profile yet." if member != ctx.author else "Use `$start` first.", "warning")
        return

    if not player.get("has_rolled"):
        await send_notice(ctx, "Roll Required", f"{member.mention} needs to use `$roll` first." if member != ctx.author else "Use `$roll` first.", "warning")
        return

    archetype, score = get_build_archetype(player)
    strengths = get_top_traits(player, 4)
    weaknesses = get_low_traits(player, 4)
    known_jutsu = player.get("known_jutsu", [])

    embed = discord.Embed(
        title=f"{member.name}'s Build Analysis",
        description=f"Best fit: **{archetype}**",
        color=discord.Color.dark_teal()
    )

    embed.add_field(
        name="Strengths",
        value="\n".join(f"**{name}:** {value}" for name, value in strengths),
        inline=True
    )
    embed.add_field(
        name="Weaknesses",
        value="\n".join(f"**{name}:** {value}" for name, value in weaknesses),
        inline=True
    )

    recommendations = []
    weak_names = [name for name, _ in weaknesses]
    if "Durability" in weak_names or "Health" in weak_names:
        recommendations.append("Train/use items for **Durability** or **Health** so you do not get deleted in duels.")
    if "Chakra Control" in weak_names:
        recommendations.append("Improve **Chakra Control** to boost jutsu damage and accuracy.")
    if "Speed" in weak_names:
        recommendations.append("Improve **Speed** to land more hits and dodge better.")
    if "Taijutsu" in weak_names:
        recommendations.append("Improve **Taijutsu** if you want stronger `$attack`, `$heavy`, and `$taijutsu` turns.")
    if not known_jutsu:
        recommendations.append("Learn at least one jutsu with `$jutsu` and `$learnjutsu [name]`.")

    embed.add_field(
        name="Recommended Focus",
        value="\n".join(f"- {line}" for line in recommendations) if recommendations else "This build is balanced. Keep leveling and expanding jutsu options.",
        inline=False
    )

    embed.add_field(
        name="Combat Rating",
        value=f"{calculate_combat_rating(player):,}",
        inline=False
    )

    await ctx.send(embed=embed)


@bot.command(name="stats")
async def stats(ctx, member: discord.Member = None):
    players = migrate_all_players(load_players())
    member = member or ctx.author
    player, user_id = resolve_player_record(players, member)

    if not player:
        await send_notice(ctx, "Profile Required", f"{member.mention} does not have a profile yet." if member != ctx.author else "Use `$start` first.", "warning")
        return

    if not player.get("has_rolled"):
        await send_notice(ctx, "Roll Required", f"{member.mention} needs to use `$roll` first." if member != ctx.author else "Use `$roll` first.", "warning")
        return

    attack_name, attack_damage = estimate_move_damage_against_self(player, "attack")
    heavy_name, heavy_damage = estimate_move_damage_against_self(player, "heavy")
    combo_name, combo_damage = estimate_move_damage_against_self(player, "taijutsu_combo")
    jutsu_name, jutsu_damage = estimate_move_damage_against_self(player, "jutsu")

    embed = discord.Embed(
        title=f"{member.name}'s Combat Stats",
        description="Damage estimates are based on this player attacking an equal copy of themselves. Real duel damage can change from enemy durability, level gap, guard, low chakra, variance, status effects, and caps.",
        color=discord.Color.orange()
    )

    embed.add_field(
        name="Estimated Damage",
        value=(
            f"**Attack:** {attack_damage}\n"
            f"**Heavy:** {heavy_damage}\n"
            f"**Taijutsu:** {combo_damage}\n"
            f"**Jutsu:** {jutsu_damage if jutsu_damage is not None else 'No learned jutsu'} {f'({jutsu_name})' if jutsu_damage is not None else ''}"
        ),
        inline=False
    )

    embed.add_field(
        name="Formula Breakdown",
        value=build_damage_formula_text(player)[:1024],
        inline=False
    )

    embed.add_field(
        name="Core Combat Rating",
        value=f"**{calculate_combat_rating(player):,}**",
        inline=False
    )

    await ctx.send(embed=embed)


@bot.command(name="train")
async def train(ctx, *, stat_name: str = None):
    players = migrate_all_players(load_players())
    user_id = str(ctx.author.id)

    if user_id not in players:
        await send_notice(ctx, "Profile Required", "Use `$start` first to create your shinobi profile.", "warning")
        return

    player = players[user_id]

    if not player["has_rolled"]:
        await send_notice(ctx, "Roll Required", "Use `$roll` before training.", "warning")
        return

    allowed, remaining = can_train(player)

    if not allowed:
        await send_notice(ctx, "Training Cooldown", f"You already trained recently. Try again in **{remaining}**.", "warning")
        return

    training_config = CONFIG.get("TRAINING", {})
    economy_config = CONFIG.get("ECONOMY", {})
    xp_gained = random.randint(training_config.get("xp_min", 25), training_config.get("xp_max", 75))
    xp_gained += get_village_training_xp_bonus(player)
    ryo_gained = random.randint(economy_config.get("training_ryo_min", 15), economy_config.get("training_ryo_max", 45))
    stat_gain = random.randint(training_config.get("stat_gain_min", 1), training_config.get("stat_gain_max", 5))

    focused_stat = resolve_training_stat_name(stat_name)
    if stat_name and not focused_stat:
        valid_stats = ", ".join(player.get("stats", BASE_STATS).keys())
        embed = ui_embed("Invalid Training Stat", f"That stat does not exist. Valid training stats: **{valid_stats}**.", "warning")
        add_field(embed, "Examples", "`$train health` • `$train chakra` • `$train stamina` • `$train durability`", False)
        await ctx.send(embed=embed)
        return

    chosen_stat = focused_stat or random.choice(list(player["stats"].keys()))

    player["stats"][chosen_stat] += stat_gain
    player["last_training"] = now_utc().isoformat()
    player["ryo"] = int(player.get("ryo", 0)) + ryo_gained

    leveled_up, levels_gained = add_xp(player, xp_gained)

    item_found = None
    if random.randint(1, training_config.get("item_drop_roll_max", 5)) <= training_config.get("item_drop_chance", 1):
        item_found = get_random_item_drop()
        add_item(player, item_found)

    kekkei_found = try_unlock_training_kekkei(player)
    sharingan_upgrade = try_level_sharingan(player, source="training")
    evolution_upgrade = try_auto_kekkei_evolution(player, source="training")

    save_players(players)

    focus_text = f"Focused Training: **{chosen_stat}**" if focused_stat else "Random Training"
    message = (
        f"{ctx.author.mention} completed training.\n"
        f"**Type:** {focus_text}\n"
        f"**XP Gained:** {xp_gained}\n"
        f"**Stat Improved:** {chosen_stat} +{stat_gain}"
    )

    if item_found:
        rarity = ITEMS[item_found]["rarity"]
        message += f"\n**Item Found:** {item_found} ({rarity})"

    if kekkei_found:
        message += f"\n**Rare Awakening:** {kekkei_found}"

    if sharingan_upgrade:
        message += f"\n**Sharingan Progression:** {sharingan_upgrade}"

    if leveled_up:
        message += f"\n**Level Up!** You gained {levels_gained} level(s). You are now level {player['level']} — {player['rank']}."

    embed = ui_embed("Training Complete", f"{ctx.author.mention} finished a training session.", "success")
    add_field(embed, "Results", "\n".join([
        kv_line("Type", focus_text),
        kv_line("XP Gained", f"+{fmt_num(xp_gained)}"),
        kv_line("Ryo Gained", f"+{fmt_num(ryo_gained)} Ryo"),
        kv_line("Stat Improved", f"{chosen_stat} +{fmt_num(stat_gain)}"),
    ]), False)
    bonus_lines = []
    if item_found:
        bonus_lines.append(kv_line("Item Found", f"{item_found} ({ITEMS[item_found]['rarity']})"))
    if kekkei_found:
        bonus_lines.append(kv_line("Rare Awakening", kekkei_found))
    if sharingan_upgrade:
        bonus_lines.append(kv_line("Sharingan Progression", sharingan_upgrade))
    if evolution_upgrade:
        bonus_lines.append(kv_line("Bloodline Evolution", evolution_upgrade))
    if leveled_up:
        bonus_lines.append(kv_line("Level Up", f"+{levels_gained} level(s). Now level {player['level']} — {player['rank']}"))
    if bonus_lines:
        add_field(embed, "Bonus", "\n".join(bonus_lines), False)
    await ctx.send(embed=embed)




# -----------------------------
# Player Trade System
# -----------------------------

def normalize_item_name(search_text):
    """Finds an item from config by exact or partial name, case-insensitive."""
    if not search_text:
        return None

    cleaned = str(search_text).lower().strip()

    for item_name in ITEMS:
        if item_name.lower() == cleaned:
            return item_name

    for item_name in ITEMS:
        lowered = item_name.lower()
        if cleaned in lowered or lowered in cleaned:
            return item_name

    return None


def get_trade_key(channel_id, sender_id, receiver_id):
    return f"{channel_id}:{sender_id}:{receiver_id}"


def find_pending_trade_for_user(user_id, channel_id=None):
    user_id = str(user_id)
    for trade_key, trade in ACTIVE_TRADES.items():
        if channel_id is not None and trade.get("channel_id") != channel_id:
            continue
        if user_id in [trade.get("sender_id"), trade.get("receiver_id")]:
            return trade_key, trade
    return None, None


def build_trade_summary(trade):
    if trade.get("type") == "ryo":
        return f"**{fmt_num(trade.get('amount', 0))} Ryo**"
    return f"**{trade.get('item_name')} x{fmt_num(trade.get('amount', 0))}**"


def player_has_trade_assets(player, trade):
    amount = int(trade.get("amount", 0) or 0)
    if amount <= 0:
        return False, "Trade amount must be greater than 0."

    if trade.get("type") == "ryo":
        current_ryo = int(player.get("ryo", 0) or 0)
        if current_ryo < amount:
            return False, f"You only have **{fmt_num(current_ryo)} Ryo**."
        return True, None

    item_name = trade.get("item_name")
    inventory = player.get("inventory", {}) or {}
    owned = int(inventory.get(item_name, 0) or 0)
    if owned < amount:
        return False, f"You only have **{item_name} x{fmt_num(owned)}**."
    return True, None


def execute_trade(players, trade):
    sender_id = str(trade["sender_id"])
    receiver_id = str(trade["receiver_id"])
    sender = players[sender_id]
    receiver = players[receiver_id]
    amount = int(trade.get("amount", 0) or 0)

    ok, reason = player_has_trade_assets(sender, trade)
    if not ok:
        return False, reason

    if trade.get("type") == "ryo":
        sender["ryo"] = int(sender.get("ryo", 0) or 0) - amount
        receiver["ryo"] = int(receiver.get("ryo", 0) or 0) + amount
        return True, None

    item_name = trade.get("item_name")
    if not remove_item(sender, item_name, amount):
        return False, f"You no longer have enough **{item_name}** to complete this trade."
    add_item(receiver, item_name, amount)
    return True, None


@bot.command(name="trade")
async def trade(ctx, member: discord.Member, trade_type: str = None, amount: int = None, *, item_name: str = None):
    """Creates a trade offer.

    Usage:
    $trade @user ryo 100
    $trade @user item 2 Kunai
    $trade @user item 1 Tailed Beast Chakra Fragment
    """
    players = migrate_all_players(load_players())
    sender_id = str(ctx.author.id)
    receiver_id = str(member.id)

    if sender_id not in players:
        await send_notice(ctx, "Profile Required", "Use `$start` first to create your shinobi profile.", "warning")
        return

    if receiver_id not in players:
        await send_notice(ctx, "Trade Failed", f"{member.mention} does not have a profile yet.", "warning")
        return

    if member.bot or member.id == ctx.author.id:
        await send_notice(ctx, "Invalid Trade", "You need to trade with another real player.", "warning")
        return

    duel_key, _ = find_active_duel_for_user(ctx.author.id, ctx.channel.id)
    target_duel_key, _ = find_active_duel_for_user(member.id, ctx.channel.id)
    if duel_key or target_duel_key:
        await send_notice(ctx, "Trade Blocked", "Players cannot trade while involved in an active duel.", "warning")
        return

    existing_key, existing_trade = find_pending_trade_for_user(sender_id, ctx.channel.id)
    if existing_trade:
        await send_notice(ctx, "Trade Already Pending", "Finish or cancel your current trade first with `$accepttrade`, `$denytrade`, or `$canceltrade`.", "warning")
        return

    existing_key, existing_trade = find_pending_trade_for_user(receiver_id, ctx.channel.id)
    if existing_trade:
        await send_notice(ctx, "Target Has Pending Trade", f"{member.mention} already has a pending trade in this channel.", "warning")
        return

    if not trade_type or amount is None:
        await send_notice(ctx, "Trade Usage", "Use `$trade @user ryo 100` or `$trade @user item 2 Kunai`.", "info")
        return

    trade_type = trade_type.lower().strip()
    if amount <= 0:
        await send_notice(ctx, "Invalid Amount", "Trade amount must be greater than 0.", "warning")
        return

    if trade_type in ["ryo", "money", "cash"]:
        trade_data = {
            "type": "ryo",
            "amount": int(amount),
            "item_name": None,
        }
    elif trade_type in ["item", "items"]:
        matched_item = normalize_item_name(item_name)
        if not matched_item:
            await send_notice(ctx, "Item Not Found", "That item does not exist in config.json. Check spelling with `$items` or `$inventory`.", "warning")
            return
        trade_data = {
            "type": "item",
            "amount": int(amount),
            "item_name": matched_item,
        }
    else:
        await send_notice(ctx, "Trade Usage", "Trade type must be `ryo` or `item`. Example: `$trade @user item 1 Kunai`", "info")
        return

    trade_offer = {
        "channel_id": ctx.channel.id,
        "sender_id": sender_id,
        "receiver_id": receiver_id,
        "created_at": now_utc().isoformat(),
        **trade_data,
    }

    ok, reason = player_has_trade_assets(players[sender_id], trade_offer)
    if not ok:
        await send_notice(ctx, "Trade Failed", reason, "warning")
        return

    trade_key = get_trade_key(ctx.channel.id, sender_id, receiver_id)
    ACTIVE_TRADES[trade_key] = trade_offer

    embed = ui_embed("Trade Offer Created", f"{ctx.author.mention} wants to send {member.mention} {build_trade_summary(trade_offer)}.", "gold")
    add_field(embed, "Receiver", f"{member.mention}, use `$accepttrade` to accept or `$denytrade` to decline.", False)
    add_field(embed, "Sender", "Use `$canceltrade` to cancel before it is accepted.", False)
    await ctx.send(embed=embed)


@bot.command(name="accepttrade", aliases=["tradeaccept"])
async def accept_trade(ctx):
    players = migrate_all_players(load_players())
    receiver_id = str(ctx.author.id)

    trade_key = None
    trade = None
    for key, pending in ACTIVE_TRADES.items():
        if pending.get("receiver_id") == receiver_id and pending.get("channel_id") == ctx.channel.id:
            trade_key = key
            trade = pending
            break

    if not trade:
        await send_notice(ctx, "No Trade Found", "You do not have a pending trade to accept in this channel.", "warning")
        return

    sender_id = str(trade.get("sender_id"))
    if sender_id not in players or receiver_id not in players:
        ACTIVE_TRADES.pop(trade_key, None)
        await send_notice(ctx, "Trade Cancelled", "One of the trade profiles no longer exists.", "danger")
        return

    ok, reason = execute_trade(players, trade)
    if not ok:
        ACTIVE_TRADES.pop(trade_key, None)
        await send_notice(ctx, "Trade Cancelled", reason, "warning")
        return

    save_players(players)
    ACTIVE_TRADES.pop(trade_key, None)

    embed = ui_embed("Trade Complete", f"<@{sender_id}> sent {ctx.author.mention} {build_trade_summary(trade)}.", "success")
    await ctx.send(embed=embed)


@bot.command(name="denytrade", aliases=["declinetrade", "tradedeny"])
async def deny_trade(ctx):
    receiver_id = str(ctx.author.id)

    for key, trade in list(ACTIVE_TRADES.items()):
        if trade.get("receiver_id") == receiver_id and trade.get("channel_id") == ctx.channel.id:
            ACTIVE_TRADES.pop(key, None)
            await send_notice(ctx, "Trade Declined", f"{ctx.author.mention} declined the trade offer.", "neutral")
            return

    await send_notice(ctx, "No Trade Found", "You do not have a pending trade to decline in this channel.", "warning")


@bot.command(name="canceltrade", aliases=["tradecancel"])
async def cancel_trade(ctx):
    sender_id = str(ctx.author.id)

    for key, trade in list(ACTIVE_TRADES.items()):
        if trade.get("sender_id") == sender_id and trade.get("channel_id") == ctx.channel.id:
            ACTIVE_TRADES.pop(key, None)
            await send_notice(ctx, "Trade Cancelled", "Your pending trade offer was cancelled.", "neutral")
            return

    await send_notice(ctx, "No Trade Found", "You do not have a pending trade to cancel in this channel.", "warning")


@bot.command(name="inventory")
async def inventory(ctx):
    players = migrate_all_players(load_players())
    user_id = str(ctx.author.id)

    if user_id not in players:
        await send_notice(ctx, "Profile Required", "Use `$start` first to create your shinobi profile.", "warning")
        return

    player_inventory = players[user_id].get("inventory", {})

    if not player_inventory:
        await send_notice(ctx, "Inventory Empty", "Your inventory is empty.", "neutral")
        return

    embed = ui_embed(f"{ctx.author.name}'s Inventory", "Stacked items and permanent consumable boosts.", "info")

    for item, amount in sorted(player_inventory.items()):
        details = ITEMS.get(item, {})
        rarity = details.get("rarity", "Unknown")
        effect = details.get("effect", "Unknown")
        boost = details.get("boost", 0)
        add_field(
            embed,
            f"{item} x{amount}",
            f"**Rarity:** {rarity}\n**Effect:** +{boost} {effect}\n**Use:** `$use {item}`",
            False
        )

    await ctx.send(embed=embed)


@bot.command(name="use")
async def use_item(ctx, *, item_name: str):
    players = migrate_all_players(load_players())
    user_id = str(ctx.author.id)

    if user_id not in players:
        await send_notice(ctx, "Profile Required", "Use `$start` first to create your shinobi profile.", "warning")
        return

    player = players[user_id]
    matched_item = None

    for item in ITEMS:
        if item.lower() == item_name.lower():
            matched_item = item
            break

    if not matched_item:
        await send_notice(ctx, "Item Not Found", "That item does not exist.", "warning")
        return

    if matched_item not in player.get("inventory", {}):
        await send_notice(ctx, "Missing Item", "You do not have that item in your inventory.", "warning")
        return

    item_data = ITEMS[matched_item]
    effect = item_data.get("effect")
    boost = int(item_data.get("boost", 0))
    effect_text = f"+{fmt_num(boost)} {effect}"

    if effect == "Rerolls":
        player["rerolls_remaining"] = int(player.get("rerolls_remaining", REROLLS_PER_PLAYER)) + boost
        effect_text = f"+{fmt_num(boost)} rerolls"
    elif effect in player.get("stats", {}):
        player["stats"][effect] += boost
    elif player.get("affinities") and effect in player["affinities"]:
        player["affinities"][effect] += boost
    else:
        await send_notice(ctx, "Item Effect Not Supported", f"**{matched_item}** uses effect `{effect}`, but that effect is not supported yet.", "warning")
        return

    remove_item(player, matched_item)
    player["hidden_skill_score"] = calculate_hidden_skill_score(player)

    save_players(players)

    embed = ui_embed("Item Used", f"{ctx.author.mention} used **{matched_item}**.", "success")
    add_field(embed, "Effect", effect_text, False)
    if effect == "Rerolls":
        add_field(embed, "Rerolls Remaining", fmt_num(player.get("rerolls_remaining", 0)), False)
    await ctx.send(embed=embed)


@bot.command(name="duel")
async def duel(ctx, opponent: discord.Member):
    players = migrate_all_players(load_players())

    challenger_id = str(ctx.author.id)
    opponent_id = str(opponent.id)

    if opponent.bot:
        await send_notice(ctx, "Invalid Duel", "You cannot duel a bot.", "warning")
        return

    if ctx.author.id == opponent.id:
        await send_notice(ctx, "Invalid Duel", "You cannot duel yourself.", "warning")
        return

    if challenger_id not in players:
        await send_notice(ctx, "Profile Required", "Use `$start` first to create your shinobi profile.", "warning")
        return

    if opponent_id not in players:
        await send_notice(ctx, "Opponent Missing Profile", "That player needs to use `$start` first.", "warning")
        return

    if not players[challenger_id]["has_rolled"] or not players[opponent_id]["has_rolled"]:
        await send_notice(ctx, "Roll Required", "Both players must use `$roll` before dueling.", "warning")
        return

    save_players(players)

    DUEL_REQUESTS[opponent.id] = {
        "challenger": ctx.author,
        "channel": ctx.channel,
        "created_at": now_utc()
    }

    embed = ui_embed("Duel Challenge", f"{opponent.mention}, {ctx.author.mention} challenged you to a duel.", "brand")
    add_field(embed, "Response", "Use `$accept` to fight or `$deny` to refuse.", False)
    await ctx.send(embed=embed)


@bot.command(name="accept")
async def accept(ctx):
    request = DUEL_REQUESTS.get(ctx.author.id)

    if not request:
        await send_notice(ctx, "No Duel Request", "You have no pending duel request.", "warning")
        return

    challenger = request["challenger"]
    del DUEL_REQUESTS[ctx.author.id]

    await start_turn_based_duel(ctx, challenger, ctx.author)


@bot.command(name="deny")
async def deny(ctx):
    request = DUEL_REQUESTS.get(ctx.author.id)

    if not request:
        await send_notice(ctx, "No Duel Request", "You have no pending duel request.", "warning")
        return

    challenger = request["challenger"]
    del DUEL_REQUESTS[ctx.author.id]

    await send_notice(ctx, "Duel Denied", f"{ctx.author.mention} denied {challenger.mention}'s duel request.", "neutral")


@bot.command(name="attack")
async def pvp_attack(ctx):
    await handle_turn_action(ctx, "attack")


@bot.command(name="heavy")
async def pvp_heavy(ctx):
    await handle_turn_action(ctx, "heavy")


@bot.command(name="taijutsu", aliases=["combo", "taijutsucombo"])
async def pvp_taijutsu_combo(ctx):
    await handle_turn_action(ctx, "taijutsu_combo")


@bot.command(name="transformation", aliases=["transform", "substitution"])
async def pvp_transformation(ctx):
    await handle_turn_action(ctx, "transformation")


@bot.command(name="genjutsu")
async def pvp_genjutsu(ctx):
    await handle_turn_action(ctx, "genjutsu")


@bot.command(name="defend")
async def pvp_defend(ctx):
    await handle_turn_action(ctx, "defend")


@bot.command(name="forfeit")
async def pvp_forfeit(ctx):
    duel_key, duel = find_active_duel_for_user(ctx.author.id, ctx.channel.id)
    if not duel:
        await send_notice(ctx, "No Active Duel", "You are not in an active duel in this channel.", "warning")
        return

    loser_id = str(ctx.author.id)
    winner_id = get_duel_opponent_id(duel, loser_id)
    await send_notice(ctx, "Duel Forfeit", f"{ctx.author.mention} forfeited the duel.", "danger")
    await finish_turn_duel(ctx, duel_key, winner_id, loser_id)


@bot.command(name="devevent")
async def devevent(ctx, event_type: str, value: int, time_sec: int):
    global ACTIVE_EVENT

    if ctx.author.id not in DEV_USER_IDS:
        await send_notice(ctx, "Developer Only", "You do not have permission to use this command.", "danger")
        return

    event_type = event_type.lower()

    event_aliases = {
        "xp": "xp",
        "shinobi": "shinobi",
        "strong": "shinobi",
        "fight": "shinobi",
        "beast": "beast",
        "tailed": "beast",
        "capture": "beast"
    }

    if event_type not in event_aliases:
        embed = ui_embed("Invalid Event Type", "Choose one of the supported developer event types.", "warning")
        add_field(embed, "Usage", "`$devevent xp [value] [time_sec]`\n`$devevent shinobi [difficulty] [time_sec]`\n`$devevent beast [difficulty] [time_sec]`", False)
        await ctx.send(embed=embed)
        return

    if value <= 0 or time_sec <= 0:
        await send_notice(ctx, "Invalid Event Values", "Value and time must be greater than 0.", "warning")
        return

    if ACTIVE_EVENT is not None:
        await send_notice(ctx, "Event Already Active", "There is already an active event.", "warning")
        return

    final_type = event_aliases[event_type]

    ACTIVE_EVENT = {
        "type": final_type,
        "value": value,
        "accepted": [],
        "started_at": now_utc().isoformat()
    }

    if final_type == "xp":
        embed = build_event_embed("XP Event", f"Claim **{fmt_num(value)} XP** before the event closes.", "xp", "gold")
        add_field(embed, "How To Join", "Use `$event` to claim the reward.", False)
        add_field(embed, "Time Limit", f"{time_sec} seconds", True)
    elif final_type == "shinobi":
        enemy = build_shinobi_enemy(value)
        ACTIVE_EVENT["enemy"] = enemy
        embed = build_event_embed("Shinobi Group Fight", f"**{enemy['name']}** has entered the area. This is a group PvE fight.", "shinobi", "danger")
        add_field(embed, "Threat Level", f"Difficulty **{value}** | Power **{fmt_num(enemy['power'])}** | HP **{fmt_num(enemy['hp'])}**", False)
        add_field(embed, "Rewards", f"**{fmt_num(enemy['xp_reward'])} XP** per survivor | Item chance **{enemy['item_reward_chance']}%**", False)
        add_field(embed, "How To Join", event_join_instruction("shinobi"), False)
        add_field(embed, "Time Limit", f"{time_sec} seconds", True)
    else:
        players = migrate_all_players(load_players())
        enemy = build_tailed_beast(value, players)
        save_players(players)
        if enemy is None:
            ACTIVE_EVENT = None
            await send_notice(ctx, "No Beasts Available", "Every tailed beast is currently sealed inside a player. Try again after one expires.", "warning")
            return
        ACTIVE_EVENT["enemy"] = enemy
        embed = build_event_embed("Tailed-Beast Capture", f"**{enemy['name']}** has appeared. Only one shinobi can attempt the seal.", "beast", "danger")
        add_field(embed, "Beast Details", f"Tails **{enemy['tails']}** | Trait Boost **+{enemy['boost_percent']}%** | Power **{fmt_num(enemy['power'])}**", False)
        add_field(embed, "Capture Reward", f"**{fmt_num(enemy['xp_reward'])} XP**, **Tailed Beast Chakra Fragment**, and 24-hour Jinchuriki boost", False)
        add_field(embed, "How To Join", event_join_instruction("beast"), False)
        add_field(embed, "Time Limit", f"{time_sec} seconds", True)

    await ctx.send(embed=embed)

    bot.loop.create_task(event_timer(ctx.channel, time_sec))


@bot.command(name="event")

async def event(ctx):
    global ACTIVE_EVENT

    if ACTIVE_EVENT is None:
        await send_notice(ctx, "No Active Event", "There is no active world event right now.", "neutral")
        return

    players = migrate_all_players(load_players())
    user_id = str(ctx.author.id)

    if user_id not in players:
        await send_notice(ctx, "Profile Required", "Use `$start` first to create your shinobi profile.", "warning")
        return

    if not players[user_id]["has_rolled"]:
        await send_notice(ctx, "Roll Required", "Use `$roll` before joining events.", "warning")
        return

    if user_id in ACTIVE_EVENT["accepted"]:
        await send_notice(ctx, "Already Joined", "You already joined this event.", "warning")
        return

    if ACTIVE_EVENT["type"] == "beast" and len(ACTIVE_EVENT["accepted"]) >= 1:
        await send_notice(ctx, "Capture Locked", "This Tailed-Beast Capture already has a challenger.", "warning")
        return

    ACTIVE_EVENT["accepted"].append(user_id)

    if ACTIVE_EVENT["type"] == "xp":
        embed = build_event_embed("XP Event Joined", f"{ctx.author.mention} joined the XP event.", "xp", "success")
    elif ACTIVE_EVENT["type"] == "shinobi":
        embed = build_event_embed("Group Fight Joined", f"{ctx.author.mention} joined the Shinobi Group Fight.", "shinobi", "success")
        add_field(embed, "Current Fighters", str(len(ACTIVE_EVENT["accepted"])), True)
    elif ACTIVE_EVENT["type"] == "beast":
        embed = build_event_embed("Capture Challenger Selected", f"{ctx.author.mention} is attempting the Tailed-Beast Capture.", "beast", "danger")
        add_field(embed, "Rule", "Only the first valid challenger can attempt this beast.", False)
    else:
        embed = build_event_embed("Event Joined", f"{ctx.author.mention} joined the event.", "event", "success")
    await ctx.send(embed=embed)



@bot.command(name="jutsu", aliases=["Jutsu", "jutsus", "Jutsus"])
async def jutsu_list(ctx, *, jutsu_name: str = None):
    if jutsu_name and not jutsu_name.strip().isdigit():
        await handle_turn_action(ctx, "jutsu", jutsu_name)
        return

    if not JUTSU:
        await send_notice(ctx, "No Jutsu Configured", "No jutsu are configured yet.", "warning")
        return

    page = 1
    if jutsu_name and jutsu_name.strip().isdigit():
        page = max(1, int(jutsu_name.strip()))

    visible_jutsu = []
    for name, data in JUTSU.items():
        if not isinstance(data, dict):
            continue
        requirements = data.get("requirements", {})
        if requirements.get("hidden") or requirements.get("unlockable") is False:
            continue
        visible_jutsu.append((name, data))

    visible_jutsu.sort(key=lambda item: (
        item[1].get("requirements", {}).get("level", 1),
        item[1].get("requirements", {}).get("chakra_nature", ""),
        item[0]
    ))

    per_page = 4
    total_pages = max(1, (len(visible_jutsu) + per_page - 1) // per_page)
    page = min(page, total_pages)
    page_items = visible_jutsu[(page - 1) * per_page:page * per_page]

    embed = ui_embed(
        f"Jutsu Library — Page {page}/{total_pages}",
        "Use `$learnjutsu [name]` to learn. In combat, use `$jutsu [name]`.",
        "brand"
    )

    for name, data in page_items:
        requirements = data.get("requirements", {})
        req_text = ", ".join(f"{key}: {value}" for key, value in requirements.items()) or "None"
        status = data.get("status_effect")
        if isinstance(status, dict):
            status_text = f"{status.get('type', 'Unknown').title()} • {status.get('chance', 0)}% • {status.get('turns', 1)} turn(s)"
            if status.get("damage", 0):
                status_text += f" • {status.get('damage')} per turn"
            if status.get("miss_penalty", 0):
                status_text += f" • +{int(status.get('miss_penalty', 0) * 100)}% miss penalty"
        else:
            status_text = "None"

        add_field(
            embed,
            f"{name}",
            "\n".join([
                f"**Type:** {data.get('type', 'Unknown')}  •  **Cost:** {data.get('chakra_cost', 0)} chakra",
                f"**Power:** {data.get('base_power', 0)}  •  **Scaling:** {data.get('scaling') or 'None'}",
                f"**Status:** {status_text}",
                f"**Requires:** {req_text}",
                truncate_text(data.get('description', ''), 220),
            ]),
            False
        )

    embed.set_footer(text=f"Showing {len(page_items)} of {len(visible_jutsu)} jutsu • Next: $jutsus {min(page + 1, total_pages)}")
    await ctx.send(embed=embed)


@bot.command(name="learnjutsu")
async def learn_jutsu(ctx, *, jutsu_name: str):
    players = migrate_all_players(load_players())
    user_id = str(ctx.author.id)

    if user_id not in players:
        await send_notice(ctx, "Profile Required", "Use `$start` first to create your shinobi profile.", "warning")
        return

    player = players[user_id]
    if not player.get("has_rolled"):
        await send_notice(ctx, "Roll Required", "Use `$roll` before learning jutsu.", "warning")
        return

    matched_name = None
    for name in JUTSU:
        if name.lower() == jutsu_name.lower():
            matched_name = name
            break

    if not matched_name:
        await send_notice(ctx, "Jutsu Not Found", "That jutsu does not exist. Use `$jutsu` to view the list.", "warning")
        return

    player.setdefault("known_jutsu", [])
    if matched_name in player["known_jutsu"]:
        await send_notice(ctx, "Already Learned", "You already know that jutsu.", "warning")
        return

    if JUTSU[matched_name].get("requirements", {}).get("unlockable") is False and ctx.author.id not in DEV_USER_IDS:
        await send_notice(ctx, "Forbidden Jutsu", f"**{matched_name}** cannot be learned normally.", "danger")
        return

    allowed, reason = player_meets_jutsu_requirements(player, JUTSU[matched_name])
    if not allowed:
        await send_notice(ctx, "Requirements Not Met", f"You cannot learn **{matched_name}** yet. {reason}", "warning")
        return

    player["known_jutsu"].append(matched_name)
    player["hidden_skill_score"] = calculate_hidden_skill_score(player)
    save_players(players)

    await send_notice(ctx, "Jutsu Learned", f"{ctx.author.mention} learned **{matched_name}**.", "success")


@bot.command(name="myjutsu")
async def my_jutsu(ctx):
    players = migrate_all_players(load_players())
    user_id = str(ctx.author.id)

    if user_id not in players:
        await send_notice(ctx, "Profile Required", "Use `$start` first to create your shinobi profile.", "warning")
        return

    known = players[user_id].get("known_jutsu", [])
    if not known:
        await send_notice(ctx, "No Known Jutsu", "You do not know any jutsu yet. Use `$jutsu` and `$learnjutsu [name]`.", "neutral")
        return

    embed = discord.Embed(title=f"{ctx.author.name}'s Jutsu", color=discord.Color.dark_orange())
    for name in known:
        data = JUTSU.get(name, {})
        embed.add_field(
            name=name,
            value=f"**Type:** {data.get('type', 'Unknown')}\n**Chakra Cost:** {data.get('chakra_cost', 0)}\n{data.get('description', '')}",
            inline=False
        )

    await ctx.send(embed=embed)


@bot.command(name="villages")
async def village_list(ctx):
    if not VILLAGES:
        await send_notice(ctx, "No Villages Configured", "No villages are configured yet.", "warning")
        return

    embed = discord.Embed(
        title="Villages",
        description="Use `$joinvillage [name]` to join a village.",
        color=discord.Color.teal()
    )

    for name, data in VILLAGES.items():
        embed.add_field(
            name=name,
            value=(
                f"{data.get('description', 'No description.')}\n"
                f"**Training XP Bonus:** +{data.get('training_xp_bonus', 0)}\n"
                f"**Skill Bonus:** +{data.get('skill_bonus', 0)}"
            ),
            inline=False
        )

    await ctx.send(embed=embed)


@bot.command(name="joinvillage")
async def join_village(ctx, *, village_name: str):
    players = migrate_all_players(load_players())
    user_id = str(ctx.author.id)

    if user_id not in players:
        await send_notice(ctx, "Profile Required", "Use `$start` first to create your shinobi profile.", "warning")
        return

    matched_name = None
    for name in VILLAGES:
        if name.lower() == village_name.lower():
            matched_name = name
            break

    if not matched_name:
        await send_notice(ctx, "Village Not Found", "That village does not exist. Use `$villages` to view the list.", "warning")
        return

    player = players[user_id]
    if player.get("village"):
        await send_notice(ctx, "Village Already Joined", f"You are already in **{player['village']}**. Ask a developer to reset/change it if needed.", "warning")
        return

    player["village"] = matched_name
    player["hidden_skill_score"] = calculate_hidden_skill_score(player)
    save_players(players)

    await send_notice(ctx, "Village Joined", f"{ctx.author.mention} joined **{matched_name}**.", "success")


@bot.command(name="village")
async def village_info(ctx):
    players = migrate_all_players(load_players())
    user_id = str(ctx.author.id)

    if user_id not in players:
        await send_notice(ctx, "Profile Required", "Use `$start` first to create your shinobi profile.", "warning")
        return

    village_name = players[user_id].get("village")
    if not village_name:
        await send_notice(ctx, "No Village Joined", "Use `$villages`, then `$joinvillage [name]` to join one.", "warning")
        return

    data = VILLAGES.get(village_name, {})
    members = [p for p in players.values() if p.get("village") == village_name]
    total_power = sum(calculate_hidden_skill_score(p) for p in members)

    embed = discord.Embed(title=village_name, description=data.get("description", ""), color=discord.Color.teal())
    embed.add_field(name="Members", value=str(len(members)), inline=True)
    embed.add_field(name="Total Power", value=str(total_power), inline=True)
    embed.add_field(name="Training XP Bonus", value=f"+{data.get('training_xp_bonus', 0)}", inline=True)
    await ctx.send(embed=embed)


@bot.command(name="villageleaderboard", aliases=["vleaderboard", "villageladder"])
async def village_leaderboard(ctx):
    players = migrate_all_players(load_players())
    totals = {}

    for player in players.values():
        village_name = player.get("village")
        if not village_name:
            continue
        totals.setdefault(village_name, {"members": 0, "power": 0})
        totals[village_name]["members"] += 1
        totals[village_name]["power"] += calculate_hidden_skill_score(player)

    if not totals:
        await send_notice(ctx, "No Village Rankings", "No village rankings exist yet.", "neutral")
        return

    sorted_totals = sorted(totals.items(), key=lambda item: item[1]["power"], reverse=True)
    embed = discord.Embed(title="Village Leaderboard", color=discord.Color.teal())

    for index, (name, data) in enumerate(sorted_totals, start=1):
        embed.add_field(
            name=f"#{index} {name}",
            value=f"**Members:** {data['members']}\n**Total Power:** {data['power']}",
            inline=False
        )

    await ctx.send(embed=embed)




@bot.command(name="devtournament")
async def devtournament(ctx, signup_seconds: int = 60):
    global ACTIVE_TOURNAMENT

    if ctx.author.id not in DEV_USER_IDS:
        await send_notice(ctx, "Developer Only", "You do not have permission to use this command.", "danger")
        return

    if ACTIVE_TOURNAMENT is not None:
        await send_notice(ctx, "Tournament Already Active", "Use `$tournament` to view it or `$devcanceltournament` to cancel it.", "warning")
        return

    signup_seconds = max(15, min(signup_seconds, 600))
    ACTIVE_TOURNAMENT = {
        "status": "signup",
        "channel_id": ctx.channel.id,
        "host_id": str(ctx.author.id),
        "participants": [],
        "started_at": now_utc().isoformat(),
        "signup_seconds": signup_seconds
    }

    embed = ui_embed("Shinobi Tournament Open", "A tournament signup has started.", "purple")
    add_field(embed, "How to Join", "Use `$jointournament` to enter.", False)
    add_field(embed, "Signup Window", f"**{signup_seconds} seconds**", True)
    add_field(embed, "Format", "Turn-based PvP bracket. Lose once and you are eliminated.", False)
    await ctx.send(embed=embed)

    bot.loop.create_task(tournament_signup_timer(ctx.channel, signup_seconds))


@bot.command(name="jointournament", aliases=["jointourny", "jt"])
async def jointournament(ctx):
    global ACTIVE_TOURNAMENT

    if ACTIVE_TOURNAMENT is None or ACTIVE_TOURNAMENT.get("status") != "signup":
        await send_notice(ctx, "No Signup Open", "There is no tournament signup open right now.", "neutral")
        return

    if ACTIVE_TOURNAMENT.get("channel_id") != ctx.channel.id:
        await send_notice(ctx, "Wrong Channel", "The active tournament is in another channel.", "warning")
        return

    players = migrate_all_players(load_players())
    user_id = str(ctx.author.id)

    if user_id not in players:
        await send_notice(ctx, "Profile Required", "Use `$start` first to create your shinobi profile.", "warning")
        return

    if not players[user_id].get("has_rolled"):
        await send_notice(ctx, "Roll Required", "Use `$roll` before joining a tournament.", "warning")
        return

    if user_id in ACTIVE_TOURNAMENT["participants"]:
        await send_notice(ctx, "Already Entered", "You are already in the tournament.", "warning")
        return

    ACTIVE_TOURNAMENT["participants"].append(user_id)
    embed = ui_embed("Tournament Entry Confirmed", f"{ctx.author.mention} joined the tournament.", "success")
    add_field(embed, "Entrants", f"**{len(ACTIVE_TOURNAMENT['participants'])}**", False)
    await ctx.send(embed=embed)


@bot.command(name="tournament", aliases=["tourny", "tourney"])
async def tournament_status(ctx):
    if ACTIVE_TOURNAMENT is None:
        await send_notice(ctx, "No Active Tournament", "There is no active tournament right now.", "neutral")
        return

    embed = ui_embed("Tournament Status", get_tournament_status_text(ACTIVE_TOURNAMENT), "purple")
    await ctx.send(embed=embed)


@bot.command(name="devstarttournament", aliases=["devstarttourny"])
async def devstarttournament(ctx):
    if ctx.author.id not in DEV_USER_IDS:
        await send_notice(ctx, "Developer Only", "You do not have permission to use this command.", "danger")
        return

    if ACTIVE_TOURNAMENT is None or ACTIVE_TOURNAMENT.get("status") != "signup":
        await send_notice(ctx, "No Signup Open", "There is no tournament signup open to start.", "warning")
        return

    await run_tournament_bracket(ctx.channel)


@bot.command(name="devcanceltournament", aliases=["devcanceltourny"])
async def devcanceltournament(ctx):
    global ACTIVE_TOURNAMENT

    if ctx.author.id not in DEV_USER_IDS:
        await send_notice(ctx, "Developer Only", "You do not have permission to use this command.", "danger")
        return

    if ACTIVE_TOURNAMENT is None:
        await send_notice(ctx, "No Active Tournament", "There is no active tournament to cancel.", "neutral")
        return

    ACTIVE_TOURNAMENT = None
    await send_notice(ctx, "Tournament Cancelled", "The active tournament has been cancelled.", "danger")



def parse_iso_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def format_cooldown(remaining):
    total = max(0, int(remaining.total_seconds()))
    hours = total // 3600
    minutes = (total % 3600) // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def choose_reward_item(chance):
    if random.randint(1, 100) > chance:
        return None
    rare_items = [name for name, data in ITEMS.items() if data.get("rarity") in ["Rare", "Legendary", "Mythic"]]
    return random.choice(rare_items or list(ITEMS.keys())) if ITEMS else None


async def claim_engagement_reward(ctx, reward_type):
    players = migrate_all_players(load_players())
    user_id = str(ctx.author.id)
    if user_id not in players:
        await send_notice(ctx, "Profile Required", "Use `$start` first to create your shinobi profile.", "warning")
        return
    player = players[user_id]
    if not player.get("has_rolled"):
        await send_notice(ctx, "Roll Required", "Use `$roll` before claiming rewards.", "warning")
        return

    econ = CONFIG.get("ECONOMY", {})
    now = now_utc()
    if reward_type == "daily":
        last_key = "last_daily"
        streak_key = "daily_streak"
        best_key = "best_daily_streak"
        claims_key = "total_daily_claims"
        cooldown_hours = econ.get("daily_cooldown_hours", 20)
        grace_hours = econ.get("daily_reset_grace_hours", 48)
        streak_cap = econ.get("daily_streak_cap", 14)
        ryo_min, ryo_max = econ.get("daily_ryo_min", 75), econ.get("daily_ryo_max", 150)
        xp_min, xp_max = econ.get("daily_xp_min", 75), econ.get("daily_xp_max", 150)
        bonus_ryo = econ.get("daily_streak_bonus_ryo", 15)
        bonus_xp = econ.get("daily_streak_bonus_xp", 20)
        item_chance = econ.get("rare_item_daily_chance", 8)
        title = "Daily Reward"
    else:
        last_key = "last_weekly"
        streak_key = "weekly_streak"
        best_key = "best_weekly_streak"
        claims_key = "total_weekly_claims"
        cooldown_hours = econ.get("weekly_cooldown_hours", 156)
        grace_hours = econ.get("weekly_reset_grace_hours", 240)
        streak_cap = econ.get("weekly_streak_cap", 8)
        ryo_min, ryo_max = econ.get("weekly_ryo_min", 450), econ.get("weekly_ryo_max", 900)
        xp_min, xp_max = econ.get("weekly_xp_min", 500), econ.get("weekly_xp_max", 950)
        bonus_ryo = econ.get("weekly_streak_bonus_ryo", 100)
        bonus_xp = econ.get("weekly_streak_bonus_xp", 125)
        item_chance = econ.get("rare_item_weekly_chance", 35)
        title = "Weekly Reward"

    last_claim = parse_iso_datetime(player.get(last_key))
    if last_claim and now < last_claim + timedelta(hours=cooldown_hours):
        remaining = (last_claim + timedelta(hours=cooldown_hours)) - now
        await send_notice(ctx, "Reward Cooldown", f"You already claimed your {reward_type}. Try again in **{format_cooldown(remaining)}**.", "warning")
        return

    if last_claim and now <= last_claim + timedelta(hours=grace_hours):
        player[streak_key] = player.get(streak_key, 0) + 1
    else:
        player[streak_key] = 1

    streak = min(player[streak_key], streak_cap)
    ryo_reward = random.randint(ryo_min, ryo_max) + (streak * bonus_ryo)
    xp_reward = random.randint(xp_min, xp_max) + (streak * bonus_xp)
    item = choose_reward_item(item_chance)

    player["ryo"] = player.get("ryo", 0) + ryo_reward
    player[last_key] = now.isoformat()
    player[claims_key] = player.get(claims_key, 0) + 1
    player[best_key] = max(player.get(best_key, 0), player.get(streak_key, 0))
    leveled_up, levels_gained = add_xp(player, xp_reward)
    if item:
        add_item(player, item)

    save_players(players)

    message = (
        f"🎁 **{title} Claimed**\n"
        f"**Streak:** {player[streak_key]}\n"
        f"**XP:** +{xp_reward}\n"
        f"**Ryo:** +{ryo_reward}\n"
        f"**Balance:** {player.get('ryo', 0)} Ryo"
    )
    if item:
        message += f"\n**Bonus Item:** {item}"
    if leveled_up:
        message += f"\n**Level Up:** +{levels_gained} level(s). Now level {player['level']} — {player['rank']}."
    embed = ui_embed(f"{title} Claimed", f"{ctx.author.mention} claimed a reward.", "gold")
    add_field(embed, "Rewards", "\n".join([
        kv_line("Streak", player[streak_key]),
        kv_line("XP", f"+{fmt_num(xp_reward)}"),
        kv_line("Ryo", f"+{fmt_num(ryo_reward)}"),
        kv_line("Balance", f"{fmt_num(player.get('ryo', 0))} Ryo"),
    ]), False)
    extras = []
    if item:
        extras.append(kv_line("Bonus Item", item))
    if leveled_up:
        extras.append(kv_line("Level Up", f"+{levels_gained} level(s). Now level {player['level']} — {player['rank']}"))
    if extras:
        add_field(embed, "Extra", "\n".join(extras), False)
    await ctx.send(embed=embed)





# -----------------------------
# Kekkei / Dojutsu Evolution System
# -----------------------------

def get_evolution_config():
    configured = CONFIG.get("KEKKEI_EVOLUTION", {})
    paths = configured.get("paths", {}) if isinstance(configured, dict) else {}
    default_paths = {}

    for bloodline in KEKKEI_GENKAI:
        default_paths[bloodline] = [
            {"name": bloodline, "level": 1, "stats": {}, "skills": {}},
            {"name": f"Awakened {bloodline}", "level": 5, "ryo_cost": 250, "stats": {"Accuracy": 10}, "skills": {"Chakra Control": 150}},
            {"name": f"Refined {bloodline}", "level": 12, "ryo_cost": 750, "item": "Evolution Shard", "item_amount": 1, "stats": {"Accuracy": 20}, "skills": {"Chakra Control": 300}},
            {"name": f"Mastered {bloodline}", "level": 25, "ryo_cost": 1500, "item": "Bloodline Catalyst", "item_amount": 1, "stats": {"Accuracy": 40}, "skills": {"Chakra Control": 600}},
        ]

    sharingan_levels = CONFIG.get("SHARINGAN", {}).get("levels", [])
    if sharingan_levels:
        default_paths["Sharingan"] = [
            {
                "name": name,
                "level": max(1, index * 6 + 1),
                "ryo_cost": index * 500,
                "stats": {"Accuracy": index * 15},
                "skills": {"Genjutsu": index * 250, "Chakra Control": index * 150, "Speed": index * 100}
            }
            for index, name in enumerate(sharingan_levels)
        ]

    default_paths.update(paths)
    return {
        "enabled": configured.get("enabled", True) if isinstance(configured, dict) else True,
        "paths": default_paths,
        "training_roll_max": configured.get("training_roll_max", 100) if isinstance(configured, dict) else 100,
        "training_success_chance": configured.get("training_success_chance", 0) if isinstance(configured, dict) else 0,
        "fight_roll_max": configured.get("fight_roll_max", 100) if isinstance(configured, dict) else 100,
        "fight_success_chance": configured.get("fight_success_chance", 0) if isinstance(configured, dict) else 0,
    }


def evolution_enabled():
    return bool(get_evolution_config().get("enabled", True))


def get_player_bloodline(player, bloodline_name=None):
    if bloodline_name:
        return find_owned_bloodline(player, bloodline_name)
    bloodlines = get_player_bloodlines(player)
    return bloodlines[0] if bloodlines else None


def get_kekkei_stage_index(player, bloodline_name=None):
    bloodline = get_player_bloodline(player, bloodline_name)
    if not bloodline:
        return -1
    player.setdefault("kekkei_evolution", {}).setdefault(bloodline, 0)
    return int(player["kekkei_evolution"].get(bloodline, 0))


def set_kekkei_stage_index(player, bloodline_name, index):
    bloodline = get_player_bloodline(player, bloodline_name)
    if not bloodline:
        return
    player.setdefault("kekkei_evolution", {})[bloodline] = max(0, int(index))


def get_kekkei_path(player_or_name):
    bloodline = player_or_name if isinstance(player_or_name, str) else get_player_bloodline(player_or_name)
    return get_evolution_config().get("paths", {}).get(bloodline, [])


def get_kekkei_stage_name(player, bloodline_name=None):
    bloodline = get_player_bloodline(player, bloodline_name)
    path = get_kekkei_path(bloodline) if bloodline else []
    if not path:
        return None
    index = max(0, min(get_kekkei_stage_index(player, bloodline), len(path) - 1))
    return path[index].get("name")


def get_next_kekkei_stage(player, bloodline_name=None):
    bloodline = get_player_bloodline(player, bloodline_name)
    path = get_kekkei_path(bloodline) if bloodline else []
    if not path:
        return None, None, None
    next_index = get_kekkei_stage_index(player, bloodline) + 1
    if next_index >= len(path):
        return bloodline, None, None
    return bloodline, next_index, path[next_index]


def player_has_item_amount(player, item_name, amount):
    if not item_name or amount <= 0:
        return True
    return int(player.get("inventory", {}).get(item_name, 0)) >= int(amount)


def apply_stage_rewards(player, stage):
    apply_trait_delta(player.setdefault("stats", BASE_STATS.copy()), stage.get("stats", {}))
    apply_trait_delta(player.setdefault("affinities", {}), stage.get("skills", {}))
    player["hidden_skill_score"] = calculate_hidden_skill_score(player)


def can_evolve_kekkei(player, bloodline_name=None):
    if not evolution_enabled():
        return False, "Kekkei evolution is disabled in config.", None, None, None
    bloodline, next_index, stage = get_next_kekkei_stage(player, bloodline_name)
    if not bloodline:
        return False, "You do not own that Kekkei Genkai / dojutsu.", None, None, None
    if not stage:
        return False, f"**{bloodline}** is already at max evolution or has no path.", bloodline, None, None
    required_level = int(stage.get("level", 1))
    if player.get("level", 1) < required_level:
        return False, f"Requires level **{required_level}**.", bloodline, next_index, stage
    item_name = stage.get("item")
    item_amount = int(stage.get("item_amount", 0))
    if item_name and not player_has_item_amount(player, item_name, item_amount):
        return False, f"Requires **{item_name} x{item_amount}**.", bloodline, next_index, stage
    ryo_cost = int(stage.get("ryo_cost", 0))
    if ryo_cost and int(player.get("ryo", 0)) < ryo_cost:
        return False, f"Requires **{fmt_num(ryo_cost)} Ryo**.", bloodline, next_index, stage
    return True, None, bloodline, next_index, stage


def evolve_kekkei(player, bloodline, stage_index, stage):
    item_name = stage.get("item")
    item_amount = int(stage.get("item_amount", 0))
    if item_name and item_amount:
        remove_item(player, item_name, item_amount)
    ryo_cost = int(stage.get("ryo_cost", 0))
    if ryo_cost:
        player["ryo"] = max(0, int(player.get("ryo", 0)) - ryo_cost)
    set_kekkei_stage_index(player, bloodline, stage_index)
    apply_stage_rewards(player, stage)
    return stage.get("name", "Unknown Evolution")


def try_auto_kekkei_evolution(player, source="training"):
    if not evolution_enabled() or not get_player_bloodlines(player):
        return None
    config = get_evolution_config()
    roll_max = int(config.get(f"{source}_roll_max", 100))
    success = int(config.get(f"{source}_success_chance", 0))
    if success <= 0 or random.randint(1, max(1, roll_max)) > success:
        return None
    candidates = get_player_bloodlines(player)
    random.shuffle(candidates)
    for bloodline in candidates:
        allowed, reason, selected, next_index, stage = can_evolve_kekkei(player, bloodline)
        if allowed:
            return evolve_kekkei(player, selected, next_index, stage)
    return None


@bot.command(name="evolve", aliases=["evolution", "evolvekekkei"])
async def evolve_command(ctx, *, bloodline_name: str = None):
    players = migrate_all_players(load_players())
    user_id = str(ctx.author.id)
    if user_id not in players:
        await send_notice(ctx, "Profile Required", "Use `$start` first.", "warning")
        return
    player = players[user_id]
    allowed, reason, bloodline, next_index, stage = can_evolve_kekkei(player, bloodline_name)
    if not allowed:
        embed = ui_embed("Evolution Blocked", reason, "warning")
        add_field(embed, "Owned Bloodlines", format_player_bloodlines(player), False)
        add_field(embed, "Usage", "`$evolve` or `$evolve <bloodline>`", False)
        await ctx.send(embed=embed)
        return
    evolved_name = evolve_kekkei(player, bloodline, next_index, stage)
    save_players(players)
    embed = ui_embed("Bloodline Evolved", f"{ctx.author.mention} evolved **{bloodline}** into **{evolved_name}**.", "purple")
    add_field(embed, "Owned Bloodlines", format_player_bloodlines(player), False)
    add_field(embed, "New Bonuses", "\n".join([
        kv_line("Stats", compact_list([f"{k} +{v}" for k, v in stage.get("stats", {}).items()], "None")),
        kv_line("Skills", compact_list([f"{k} +{v}" for k, v in stage.get("skills", {}).items()], "None")),
    ]), False)
    await ctx.send(embed=embed)


@bot.command(name="evolutions", aliases=["evolutiontree", "bloodline"])
async def evolutions_command(ctx, *, bloodline_name: str = None):
    players = migrate_all_players(load_players())
    player = players.get(str(ctx.author.id))
    bloodline = bloodline_name or (get_player_bloodlines(player)[0] if player and get_player_bloodlines(player) else None)
    if not bloodline:
        await send_notice(ctx, "No Bloodline", "Use `$evolutions <bloodline>` or acquire a Kekkei Genkai first.", "warning")
        return
    matched = None
    for name in get_evolution_config().get("paths", {}):
        if name.lower() == bloodline.lower():
            matched = name
            break
    if not matched:
        await send_notice(ctx, "No Evolution Path", f"No evolution path is configured for **{bloodline}**.", "warning")
        return
    path = get_kekkei_path(matched)
    embed = ui_embed(f"{matched} Evolution Path", "Use `$evolve <bloodline>` when you meet the next stage requirements.", "purple")
    current_index = get_kekkei_stage_index(player, matched) if player and has_player_bloodline(player, matched) else -1
    for index, stage in enumerate(path):
        marker = "✅" if index <= current_index else "🔒"
        reqs = [f"Level {stage.get('level', 1)}"]
        if stage.get("item"):
            reqs.append(f"{stage.get('item')} x{stage.get('item_amount', 1)}")
        if stage.get("ryo_cost"):
            reqs.append(f"{fmt_num(stage.get('ryo_cost'))} Ryo")
        add_field(embed, f"{marker} {stage.get('name', 'Unknown Stage')}", "Requires: " + ", ".join(reqs), False)
    await ctx.send(embed=embed)


# -----------------------------
# Ryo Gacha / Banner Summoning System
# -----------------------------

def get_gacha_config():
    return CONFIG.get("GACHA", {})


def gacha_enabled():
    return bool(get_gacha_config().get("enabled", True))


def get_weekly_rotation_config():
    return get_gacha_config().get("weekly_rotation", {})


def get_banner_rotation_week_key():
    today = now_utc().date()
    iso_year, iso_week, _ = today.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def get_rotating_banner_keys():
    config = get_gacha_config()
    banners = config.get("banners", {})
    rotation = get_weekly_rotation_config()
    configured = rotation.get("include_banners") or []
    keys = [key for key in configured if key in banners]
    if keys:
        return keys
    return list(banners.keys()) or ["standard"]


def get_active_banner_key():
    config = get_gacha_config()
    rotation = get_weekly_rotation_config()
    banners = config.get("banners", {})

    if rotation.get("enabled", False):
        keys = get_rotating_banner_keys()
        if not keys:
            return config.get("active_banner") or "standard"
        seed = f"{rotation.get('seed_mode', 'year_week')}:{get_banner_rotation_week_key()}:{BOT_VERSION}"
        rng = random.Random(seed)
        return rng.choice(keys)

    return config.get("active_banner") or next(iter(banners or {"standard": {}}), "standard")


def get_banner(banner_key=None):
    config = get_gacha_config()
    key = banner_key or get_active_banner_key()
    banners = config.get("banners", {})
    return key, banners.get(key, banners.get("standard", {}))


def get_gacha_cost(mode="single"):
    config = get_gacha_config()
    if mode == "multi":
        return int(config.get("multi_cost", config.get("single_cost", 250) * config.get("multi_rolls", 10)))
    return int(config.get("single_cost", 250))


def get_gacha_roll_count(mode="single"):
    if mode == "multi":
        return int(get_gacha_config().get("multi_rolls", 10))
    return 1


def choose_gacha_rarity(player=None):
    config = get_gacha_config()
    rarities = config.get("rarities", {})
    pity_rolls = int(config.get("pity_rolls", 0))
    mythic_pity_rolls = int(config.get("mythic_pity_rolls", 0))
    pity_min = config.get("pity_min_rarity", "Legendary")

    if player is not None and mythic_pity_rolls > 0 and int(player.get("gacha_mythic_pity", 0)) + 1 >= mythic_pity_rolls:
        return "Mythic"
    if player is not None and pity_rolls > 0 and int(player.get("gacha_pity", 0)) + 1 >= pity_rolls:
        return pity_min

    names = list(rarities.keys())
    weights = [max(0, float(rarities[name].get("weight", 1))) for name in names]
    if not names or sum(weights) <= 0:
        return "Common"
    return random.choices(names, weights=weights, k=1)[0]


def choose_gacha_prize(rarity, banner_key=None):
    config = get_gacha_config()
    pools = config.get("pools", {})
    key, banner = get_banner(banner_key)
    featured = banner.get("featured", {}).get(rarity, [])
    featured_chance = float(config.get("featured_chance", 0.0))
    if featured and random.random() <= featured_chance:
        prize = dict(random.choice(featured))
        prize["featured"] = True
        prize["banner"] = key
        return prize
    pool = pools.get(rarity, []) or pools.get("Common", [])
    if not pool:
        return {"type": "ryo", "amount_min": 1, "amount_max": 1}
    prize = dict(random.choice(pool))
    prize["featured"] = False
    prize["banner"] = key
    return prize


def resolve_gacha_amount(prize):
    if "amount" in prize:
        return int(prize.get("amount", 1))
    return random.randint(int(prize.get("amount_min", 1)), int(prize.get("amount_max", 1)))


def grant_bloodline(player, bloodline_name):
    player.setdefault("bloodline_fragments", {})
    if not bloodline_name:
        return "Bloodline reward was misconfigured."
    if has_player_bloodline(player, bloodline_name):
        refund = int(get_gacha_config().get("duplicate_bloodline_refund_ryo", 750))
        player["ryo"] = player.get("ryo", 0) + refund
        player["bloodline_fragments"][bloodline_name] = player["bloodline_fragments"].get(bloodline_name, 0) + 1
        return f"Duplicate Bloodline: **{bloodline_name}** → +1 fragment and **{refund} Ryo**"
    add_player_bloodline(player, bloodline_name, apply_modifiers=True)
    return f"Unlocked Bloodline: **{bloodline_name}**"

def apply_gacha_prize(player, rarity, prize):
    prize_type = prize.get("type", "item")
    featured_text = "Featured " if prize.get("featured") else ""
    result = {"rarity": rarity, "type": prize_type, "name": prize.get("name") or prize.get("target") or prize_type, "detail": "", "featured": prize.get("featured", False)}

    if prize_type == "item":
        item_name = prize.get("name")
        amount = resolve_gacha_amount(prize)
        if item_name:
            add_item(player, item_name, amount)
            result["detail"] = f"{featured_text}Item: **{item_name}** x{amount}"
        else:
            result["detail"] = "Item reward was misconfigured."
    elif prize_type == "jutsu":
        jutsu_name = prize.get("name")
        player.setdefault("known_jutsu", [])
        if jutsu_name and jutsu_name in JUTSU:
            if jutsu_name in player["known_jutsu"]:
                refund_percent = int(get_gacha_config().get("duplicate_jutsu_refund_percent", 35))
                refund = max(1, int(get_gacha_cost("single") * refund_percent / 100))
                player["ryo"] = player.get("ryo", 0) + refund
                result["detail"] = f"Duplicate Jutsu: **{jutsu_name}** → refunded **{refund} Ryo**"
            else:
                player["known_jutsu"].append(jutsu_name)
                result["detail"] = f"{featured_text}Unlocked Jutsu: **{jutsu_name}**"
        else:
            result["detail"] = f"Jutsu reward is not configured correctly: **{jutsu_name or 'Unknown'}**"
    elif prize_type in ["bloodline", "kekkei", "dojutsu"]:
        result["detail"] = featured_text + grant_bloodline(player, prize.get("name"))
    elif prize_type == "ryo":
        amount = resolve_gacha_amount(prize)
        player["ryo"] = player.get("ryo", 0) + amount
        result["detail"] = f"Currency: **+{amount} Ryo**"
    elif prize_type == "xp":
        amount = resolve_gacha_amount(prize)
        leveled_up, levels_gained = add_xp(player, amount)
        result["detail"] = f"XP: **+{amount}**"
        if leveled_up:
            result["detail"] += f" | Level Up: **+{levels_gained}**"
    elif prize_type == "stat_boost":
        target = prize.get("target")
        amount = resolve_gacha_amount(prize)
        player.setdefault("stats", BASE_STATS.copy())
        if target in player["stats"]:
            player["stats"][target] += amount
            result["detail"] = f"Stat Boost: **{target} +{amount}**"
        else:
            result["detail"] = f"Stat boost target is invalid: **{target}**"
    elif prize_type == "skill_boost":
        target = prize.get("target")
        amount = resolve_gacha_amount(prize)
        player.setdefault("affinities", {})
        if target in player["affinities"]:
            player["affinities"][target] += amount
            result["detail"] = f"Skill Boost: **{target} +{amount}**"
        else:
            result["detail"] = f"Skill boost target is invalid: **{target}**"
    else:
        result["detail"] = f"Unknown reward type: **{prize_type}**"

    if rarity == "Mythic":
        player["gacha_mythic_pulls"] = player.get("gacha_mythic_pulls", 0) + 1
        player["gacha_pity"] = 0
        player["gacha_mythic_pity"] = 0
    elif rarity == "Legendary":
        player["gacha_legendary_pulls"] = player.get("gacha_legendary_pulls", 0) + 1
        player["gacha_pity"] = 0
        player["gacha_mythic_pity"] = player.get("gacha_mythic_pity", 0) + 1
    else:
        player["gacha_pity"] = player.get("gacha_pity", 0) + 1
        player["gacha_mythic_pity"] = player.get("gacha_mythic_pity", 0) + 1

    player["gacha_rolls"] = player.get("gacha_rolls", 0) + 1
    player["hidden_skill_score"] = calculate_hidden_skill_score(player)
    return result


def format_gacha_result_line(result):
    rarity_icons = {"Common":"⚪","Uncommon":"🔵","Rare":"🟣","Legendary":"🟡","Mythic":"🔴"}
    icon = rarity_icons.get(result.get("rarity"), "🎲")
    featured = " 🌟" if result.get("featured") else ""
    return f"{icon} **{result.get('rarity', 'Unknown')}**{featured} — {result.get('detail', 'Unknown reward')}"


async def run_gacha_pull(ctx, mode="single"):
    if not gacha_enabled():
        await send_notice(ctx, "Gacha Disabled", "The summon system is currently disabled in config.", "warning")
        return
    mode = "multi" if str(mode).lower() in ["multi", "10", "ten", "x10", "10x"] else "single"
    players = migrate_all_players(load_players())
    user_id = str(ctx.author.id)
    if user_id not in players:
        await send_notice(ctx, "Profile Required", "Use `$start` first to create your shinobi profile.", "warning")
        return
    player = players[user_id]
    if not player.get("has_rolled"):
        await send_notice(ctx, "Roll Required", "Use `$roll` before using the summon system.", "warning")
        return
    cost = get_gacha_cost(mode)
    roll_count = get_gacha_roll_count(mode)
    balance = int(player.get("ryo", 0))
    if balance < cost:
        await send_notice(ctx, "Not Enough Ryo", f"You need **{cost} Ryo** for a {mode} summon. Current balance: **{balance} Ryo**.", "warning")
        return
    banner_key, banner = get_banner()
    player["ryo"] = balance - cost
    results = []
    for _ in range(roll_count):
        rarity = choose_gacha_rarity(player)
        prize = choose_gacha_prize(rarity, banner_key)
        results.append(apply_gacha_prize(player, rarity, prize))
    save_players(players)
    best_order = ["Common", "Uncommon", "Rare", "Legendary", "Mythic"]
    best = max(results, key=lambda r: best_order.index(r.get("rarity", "Common")) if r.get("rarity", "Common") in best_order else 0)
    tone = get_gacha_config().get("rarities", {}).get(best.get("rarity"), {}).get("color", "gold")
    embed = ui_embed("Summoning Results", f"{ctx.author.mention} pulled on **{banner.get('name', banner_key)}** for **{cost} Ryo**.", tone)
    add_field(embed, "Pulls", "\n".join(format_gacha_result_line(r) for r in results[:10]), False)
    add_field(embed, "Account", "\n".join([
        kv_line("Balance", f"{fmt_num(player.get('ryo', 0))} Ryo"),
        kv_line("Lifetime Pulls", fmt_num(player.get("gacha_rolls", 0))),
        kv_line("Legendary Pity", f"{player.get('gacha_pity', 0)}/{get_gacha_config().get('pity_rolls', 30)}"),
        kv_line("Mythic Pity", f"{player.get('gacha_mythic_pity', 0)}/{get_gacha_config().get('mythic_pity_rolls', 90)}"),
    ]), False)
    await ctx.send(embed=embed)


@bot.command(name="gacha", aliases=["summoninfo", "gachainfo"])
async def gacha_info(ctx):
    await banner(ctx)


@bot.command(name="banner", aliases=["banners", "rates"])
async def banner(ctx):
    config = get_gacha_config()
    if not config:
        await send_notice(ctx, "Gacha Missing", "No `GACHA` section was found in config.json.", "warning")
        return

    banner_key, active = get_banner()
    rotation = get_weekly_rotation_config()
    embed = ui_embed("Current Summon Banner", active.get("description", "Spend Ryo for rewards."), "gold")
    add_field(embed, "Active Banner", f"**{active.get('name', banner_key)}** (`{banner_key}`)", False)

    if rotation.get("enabled", False):
        add_field(embed, "Weekly Rotation", "\n".join([
            kv_line("Status", "Enabled"),
            kv_line("Week", get_banner_rotation_week_key()),
            kv_line("Changes", rotation.get("change_day", "Monday")),
            kv_line("Pool", f"{len(get_rotating_banner_keys())} banners")
        ]), False)

    add_field(embed, "Costs", "\n".join([
        kv_line("Single", f"{get_gacha_cost('single')} Ryo"),
        kv_line("Multi", f"{get_gacha_cost('multi')} Ryo for {get_gacha_roll_count('multi')} pulls"),
        kv_line("Legendary Pity", f"{config.get('pity_min_rarity', 'Legendary')} at {config.get('pity_rolls', 30)} pulls"),
        kv_line("Mythic Pity", f"Mythic at {config.get('mythic_pity_rolls', 90)} pulls"),
    ]), False)

    rates = [kv_line(rarity, f"weight {data.get('weight', 1)}") for rarity, data in config.get("rarities", {}).items()]
    add_field(embed, "Rates", "\n".join(rates), True)

    featured_lines = []
    for rarity, prizes in active.get("featured", {}).items():
        names = [p.get("name", p.get("target", p.get("type", "Unknown"))) for p in prizes]
        featured_lines.append(f"**{rarity}:** " + compact_list(names, "None", 6))
    add_field(embed, "Featured", "\n".join(featured_lines) if featured_lines else "No featured rewards on this banner.", True)
    add_field(embed, "Commands", "`$summon` single • `$summon multi` x10 • `$rarities` full rarity list", False)
    await ctx.send(embed=embed)


@bot.command(name="rarities", aliases=["gacharates"])
async def rarities(ctx):
    config = get_gacha_config()
    embed = ui_embed("Gacha Rarities", "Weights are relative, not exact percentages.", "gold")
    for rarity, data in config.get("rarities", {}).items():
        pool = config.get("pools", {}).get(rarity, [])
        samples = [p.get("name", p.get("target", p.get("type", "Unknown"))) for p in pool[:8]]
        add_field(embed, rarity, f"**Weight:** {data.get('weight', 1)}\n**Examples:** {compact_list(samples, 'None', 8)}", False)
    await ctx.send(embed=embed)


@bot.command(name="summon", aliases=["pull", "wish"])
async def summon(ctx, mode: str = "single"):
    await run_gacha_pull(ctx, mode)

@bot.command(name="daily")
async def daily(ctx):
    await claim_engagement_reward(ctx, "daily")


@bot.command(name="weekly")
async def weekly(ctx):
    await claim_engagement_reward(ctx, "weekly")


@bot.command(name="streak")
async def streak(ctx, member: discord.Member = None):
    players = migrate_all_players(load_players())
    member = member or ctx.author
    user_id = str(member.id)
    if user_id not in players:
        await send_notice(ctx, "Profile Required", f"{member.mention} does not have a profile yet." if member != ctx.author else "Use `$start` first.", "warning")
        return
    player = players[user_id]
    embed = discord.Embed(title=f"{member.name}'s Engagement Streaks", color=discord.Color.gold())
    embed.add_field(name="Daily", value=f"Current: **{player.get('daily_streak', 0)}**\nBest: **{player.get('best_daily_streak', 0)}**\nClaims: **{player.get('total_daily_claims', 0)}**", inline=True)
    embed.add_field(name="Weekly", value=f"Current: **{player.get('weekly_streak', 0)}**\nBest: **{player.get('best_weekly_streak', 0)}**\nClaims: **{player.get('total_weekly_claims', 0)}**", inline=True)
    embed.add_field(name="Currency", value=f"**{player.get('ryo', 0)} Ryo**", inline=False)
    await ctx.send(embed=embed)


@bot.command(name="clans")
async def clans_command(ctx):
    embed = ui_embed("Clan Registry", "Rollable clans and their rough identities.", "brand")
    lines = []
    for name, data in CONFIG.get("CLANS", {}).items():
        possible = compact_list(data.get("possible_kekkei", []), "None", 3)
        lines.append(f"**{name}** — weight {data.get('weight', 1)} | KG: {possible}")
    chunks = [lines[i:i+10] for i in range(0, len(lines), 10)]
    for index, chunk in enumerate(chunks[:3], start=1):
        add_field(embed, f"Clans {index}", "\n".join(chunk), False)
    await ctx.send(embed=embed)


@bot.command(name="kekkei", aliases=["bloodlines", "dojutsu"])
async def kekkei_command(ctx):
    embed = ui_embed("Bloodlines & Dojutsu", "Configured Kekkei Genkai and dojutsu paths.", "purple")
    dojutsu = CONFIG.get("DOJUTSU", [])
    all_kg = CONFIG.get("KEKKEI_GENKAI", [])
    regular = [name for name in all_kg if name not in dojutsu]
    add_field(embed, "Dojutsu", compact_list(dojutsu, "None", 20), False)
    add_field(embed, "Kekkei Genkai", compact_list(regular, "None", 30), False)
    add_field(embed, "Evolution", "Use `$evolutions` to view your current bloodline path, or `$evolutions <name>`.", False)
    await ctx.send(embed=embed)


@bot.command(name="items")
async def items_command(ctx):
    embed = ui_embed("Item Registry", "Configured items and stat/skill boosts.", "info")
    grouped = {}
    for name, data in ITEMS.items():
        grouped.setdefault(data.get("rarity", "Unknown"), []).append(f"**{name}** (+{data.get('boost', 0)} {data.get('effect', 'Unknown')})")
    for rarity in ["Common", "Uncommon", "Rare", "Epic", "Legendary", "Mythic", "Unknown"]:
        if rarity in grouped:
            add_field(embed, rarity, "\n".join(grouped[rarity][:12]), False)
    await ctx.send(embed=embed)


@bot.command(name="events")
async def events_command(ctx):
    if ACTIVE_EVENT is None:
        await send_notice(ctx, "No Active Event", "No world event is active right now.", "neutral")
        return
    event = ACTIVE_EVENT
    embed = build_event_embed("Active World Event", f"Type: **{event.get('type', 'unknown')}**", event.get("type", "event"), "brand")
    add_field(embed, "Joined", str(len(event.get("accepted", []))), True)
    if event.get("enemy"):
        enemy = event["enemy"]
        add_field(embed, "Enemy", f"**{enemy.get('name')}** | Power {fmt_num(enemy.get('power', 0))}", False)
    await ctx.send(embed=embed)


@bot.command(name="lottery", aliases=["lotto"])
async def lottery_command(ctx):
    if not is_lottery_enabled():
        await send_notice(ctx, "Lottery Disabled", "The lottery system is currently disabled.", "warning")
        return

    state = load_lottery()
    tickets = state.get("tickets", {})
    user_tickets = int(tickets.get(str(ctx.author.id), 0) or 0)
    total_tickets = get_lottery_total_tickets(state)
    pot = int(state.get("pot", 0) or 0)

    embed = ui_embed("Hourly Lottery", "Buy tickets for a chance to win the full pot every hour.", "gold")
    add_field(embed, "Ticket Price", f"**{fmt_num(get_lottery_ticket_price())} Ryo** each", True)
    add_field(embed, "Current Pot", f"**{fmt_num(pot)} Ryo**", True)
    add_field(embed, "Total Tickets", f"**{fmt_num(total_tickets)}**", True)
    add_field(embed, "Your Tickets", f"**{fmt_num(user_tickets)}**", True)
    add_field(embed, "Next Draw", f"About **{get_next_lottery_draw_text(state)}**", True)
    add_field(embed, "Buy Tickets", "Use `$ticket [amount]` or `$buyticket [amount]`.", False)
    last_winner = state.get("last_winner_id")
    if last_winner:
        add_field(embed, "Last Winner", f"<@{last_winner}> won **{fmt_num(state.get('last_prize', 0))} Ryo**.", False)
    await ctx.send(embed=embed)


@bot.command(name="ticket", aliases=["tickets", "buyticket", "buytickets"])
async def ticket_command(ctx, amount: int = 1):
    if not is_lottery_enabled():
        await send_notice(ctx, "Lottery Disabled", "The lottery system is currently disabled.", "warning")
        return

    max_per_purchase = int(get_lottery_config().get("max_tickets_per_purchase", 100))
    max_per_player = int(get_lottery_config().get("max_tickets_per_player", 500))

    if amount <= 0:
        await send_notice(ctx, "Invalid Ticket Amount", "Buy at least **1** ticket.", "warning")
        return

    if amount > max_per_purchase:
        await send_notice(ctx, "Ticket Limit", f"You can buy up to **{fmt_num(max_per_purchase)}** tickets per purchase.", "warning")
        return

    players = migrate_all_players(load_players())
    user_id = str(ctx.author.id)
    player = players.get(user_id)

    if not player:
        await send_notice(ctx, "Profile Required", "Use `$start` before buying lottery tickets.", "warning")
        return

    state = load_lottery()
    current_tickets = int(state.get("tickets", {}).get(user_id, 0) or 0)
    if current_tickets + amount > max_per_player:
        remaining = max(0, max_per_player - current_tickets)
        await send_notice(ctx, "Ticket Limit", f"You can only hold **{fmt_num(max_per_player)}** tickets per round. You can still buy **{fmt_num(remaining)}** this round.", "warning")
        return

    ticket_price = get_lottery_ticket_price()
    cost = ticket_price * amount
    current_ryo = int(player.get("ryo", 0) or 0)

    if current_ryo < cost:
        await send_notice(ctx, "Not Enough Ryo", f"You need **{fmt_num(cost)} Ryo** for **{fmt_num(amount)}** ticket(s). You only have **{fmt_num(current_ryo)} Ryo**.", "warning")
        return

    player["ryo"] = current_ryo - cost
    player["lottery_tickets_bought"] = int(player.get("lottery_tickets_bought", 0)) + amount
    players[user_id] = player

    state.setdefault("tickets", {})
    state["tickets"][user_id] = current_tickets + amount
    state["pot"] = int(state.get("pot", 0) or 0) + cost

    save_players(players)
    save_lottery(state)

    total_tickets = get_lottery_total_tickets(state)
    embed = ui_embed("Lottery Tickets Purchased", f"{ctx.author.mention} bought **{fmt_num(amount)}** ticket(s).", "success")
    add_field(embed, "Cost", f"**{fmt_num(cost)} Ryo**", True)
    add_field(embed, "Your Tickets", f"**{fmt_num(state['tickets'][user_id])}**", True)
    add_field(embed, "Current Pot", f"**{fmt_num(state.get('pot', 0))} Ryo**", True)
    add_field(embed, "Total Tickets", f"**{fmt_num(total_tickets)}**", True)
    add_field(embed, "Next Draw", f"About **{get_next_lottery_draw_text(state)}**", True)
    await ctx.send(embed=embed)


@bot.command(name="balance", aliases=["ryo", "bal"])
async def balance_command(ctx, member: discord.Member = None):
    players = migrate_all_players(load_players())
    member = member or ctx.author
    player = players.get(str(member.id))
    if not player:
        await send_notice(ctx, "Profile Required", f"{member.mention} does not have a profile yet.", "warning")
        return
    await send_notice(ctx, "Ryo Balance", f"{member.mention} has **{fmt_num(player.get('ryo', 0))} Ryo**.", "gold")


@bot.command(name="rank")
async def rank_command(ctx, member: discord.Member = None):
    await profile(ctx, member)




# -----------------------------
# Mission System
# -----------------------------

def get_mission_config():
    return CONFIG.get("MISSIONS", {})


def missions_enabled():
    return bool(get_mission_config().get("enabled", True))


def get_available_missions():
    return get_mission_config().get("missions", {})


def find_mission_key(search_text):
    cleaned = (search_text or "").lower().strip().replace("_", " ").replace("-", " ")
    missions = get_available_missions()
    for key, data in missions.items():
        aliases = [key, data.get("name", "")] + data.get("aliases", [])
        for alias in aliases:
            if cleaned == str(alias).lower().strip().replace("_", " ").replace("-", " "):
                return key
    for key, data in missions.items():
        name = data.get("name", key).lower()
        if cleaned and (cleaned in key.lower() or cleaned in name):
            return key
    return None


def get_mission_cooldown(player, mission_key):
    cooldowns = player.setdefault("mission_cooldowns", {})
    value = cooldowns.get(mission_key)
    if not value:
        return None
    return parse_iso_datetime(value)


def calculate_mission_success_chance(player, mission):
    rating = calculate_combat_rating(player)
    difficulty = max(1, int(mission.get("power", 1000)))
    base = float(mission.get("base_success", get_mission_config().get("base_success", 0.55)))
    weight = float(mission.get("power_weight", get_mission_config().get("power_weight", 0.28)))
    minimum = float(mission.get("min_success", get_mission_config().get("min_success", 0.18)))
    maximum = float(mission.get("max_success", get_mission_config().get("max_success", 0.92)))
    chance = base + ((rating - difficulty) / difficulty) * weight
    return clamp(chance, minimum, maximum)


def mission_reward_roll(mission):
    xp_min = int(mission.get("xp_min", 50))
    xp_max = int(mission.get("xp_max", 100))
    ryo_min = int(mission.get("ryo_min", 50))
    ryo_max = int(mission.get("ryo_max", 100))
    return random.randint(xp_min, xp_max), random.randint(ryo_min, ryo_max)


@bot.command(name="missions", aliases=["missionboard", "jobs"])
async def missions_command(ctx):
    if not missions_enabled():
        await send_notice(ctx, "Missions Disabled", "The mission system is currently disabled in config.", "warning")
        return
    missions = get_available_missions()
    if not missions:
        await send_notice(ctx, "No Missions", "No missions are configured yet.", "warning")
        return
    embed = ui_embed("Mission Board", "Run `$mission <name>` to attempt a mission. Missions reward XP, Ryo, and sometimes items.", "brand")
    for key, mission in missions.items():
        reward = f"{fmt_num(mission.get('xp_min', 0))}-{fmt_num(mission.get('xp_max', 0))} XP | {fmt_num(mission.get('ryo_min', 0))}-{fmt_num(mission.get('ryo_max', 0))} Ryo"
        details = [
            kv_line("Rank", mission.get("rank", "D")),
            kv_line("Power", fmt_num(mission.get("power", 1000))),
            kv_line("Rewards", reward),
            kv_line("Cooldown", f"{mission.get('cooldown_minutes', 60)} minutes"),
        ]
        add_field(embed, f"{mission.get('name', key)} (`{key}`)", "\n".join(details), False)
    await ctx.send(embed=embed)


@bot.command(name="mission", aliases=["domission", "runmission"])
async def mission_command(ctx, *, mission_name: str = None):
    if not missions_enabled():
        await send_notice(ctx, "Missions Disabled", "The mission system is currently disabled in config.", "warning")
        return
    if not mission_name:
        await missions_command(ctx)
        return
    players = migrate_all_players(load_players())
    user_id = str(ctx.author.id)
    if user_id not in players:
        await send_notice(ctx, "Profile Required", "Use `$start` first to create your shinobi profile.", "warning")
        return
    player = players[user_id]
    if not player.get("has_rolled"):
        await send_notice(ctx, "Roll Required", "Use `$roll` before running missions.", "warning")
        return
    mission_key = find_mission_key(mission_name)
    if not mission_key:
        await send_notice(ctx, "Mission Not Found", "Use `$missions` to view available missions.", "warning")
        return
    mission = get_available_missions()[mission_key]
    required_level = int(mission.get("level", 1))
    if player.get("level", 1) < required_level:
        await send_notice(ctx, "Mission Locked", f"This mission requires level **{required_level}**.", "warning")
        return
    last_run = get_mission_cooldown(player, mission_key)
    cooldown = timedelta(minutes=int(mission.get("cooldown_minutes", get_mission_config().get("default_cooldown_minutes", 60))))
    if last_run and now_utc() < last_run + cooldown:
        await send_notice(ctx, "Mission Cooldown", f"Try this mission again in **{format_cooldown((last_run + cooldown) - now_utc())}**.", "warning")
        return

    chance = calculate_mission_success_chance(player, mission)
    success = random.random() <= chance
    player.setdefault("mission_cooldowns", {})[mission_key] = now_utc().isoformat()
    player["missions_completed"] = int(player.get("missions_completed", 0)) + (1 if success else 0)
    embed = ui_embed("Mission Result", f"{ctx.author.mention} attempted **{mission.get('name', mission_key)}**.", "success" if success else "danger")
    add_field(embed, "Mission", "\n".join([
        kv_line("Rank", mission.get("rank", "D")),
        kv_line("Success Chance", format_percent(chance)),
        kv_line("Required Power", fmt_num(mission.get("power", 1000))),
        kv_line("Your Power", fmt_num(calculate_combat_rating(player))),
    ]), False)
    if success:
        xp_reward, ryo_reward = mission_reward_roll(mission)
        item = None
        if random.randint(1, 100) <= int(mission.get("item_chance", 0)):
            pool = mission.get("items") or list(ITEMS.keys())
            if pool:
                item = random.choice(pool)
                add_item(player, item)
        player["ryo"] = int(player.get("ryo", 0)) + ryo_reward
        leveled_up, levels_gained = add_xp(player, xp_reward)
        reward_lines = [
            kv_line("XP", f"+{fmt_num(xp_reward)}"),
            kv_line("Ryo", f"+{fmt_num(ryo_reward)}"),
            kv_line("Balance", f"{fmt_num(player.get('ryo', 0))} Ryo"),
        ]
        if item:
            reward_lines.append(kv_line("Item", item))
        if leveled_up:
            reward_lines.append(kv_line("Level Up", f"+{levels_gained} level(s). Now level {player['level']} — {player['rank']}"))
        add_field(embed, "Rewards", "\n".join(reward_lines), False)
    else:
        fail_ryo = int(mission.get("fail_ryo", get_mission_config().get("fail_ryo", 10)))
        fail_xp = int(mission.get("fail_xp", get_mission_config().get("fail_xp", 15)))
        player["ryo"] = int(player.get("ryo", 0)) + fail_ryo
        add_xp(player, fail_xp)
        add_field(embed, "Failure Reward", f"You still gained **{fail_xp} XP** and **{fail_ryo} Ryo** for the attempt.", False)
    save_players(players)
    await ctx.send(embed=embed)

@bot.command(name="help")
async def help_command(ctx):
    embed = ui_embed("Laentaru Bot Help", "Compact command menu for v1.9.0.", "brand")
    add_field(embed, "Start", "`$start` • `$roll` • `$reroll` • `$profile` • `$build` • `$stats`", False)
    add_field(embed, "Combat", "`$duel @user` • `$accept` • `$attack` • `$heavy` • `$taijutsu` • `$defend` • `$genjutsu` • `$jutsu <name>`", False)
    add_field(embed, "Progression", "`$train` • `$missions` • `$mission <name>` • `$daily` • `$weekly` • `$streak` • `$inventory` • `$trade`", False)
    add_field(embed, "World", "`$event` • `$events` • `$lottery` • `$ticket [amount]` • `$tournament` • `$jointournament` • `$ladder` • `$villages`", False)
    add_field(embed, "Trading", "`$trade @user ryo 100` • `$trade @user item 1 Kunai` • `$accepttrade` • `$denytrade` • `$canceltrade`", False)
    add_field(embed, "Gacha", "`$banner` • `$gacha` • `$summon` • `$summon multi` • `$pull` • `$wish` • `$rarities`", False)
    add_field(embed, "Content", "`$clans` • `$kekkei` • `$dojutsu` • `$items` • `$evolutions` • `$evolve`", False)
    embed.set_footer(text=f"Laentaru Bot v{BOT_VERSION} • Use $info for system overview")
    await ctx.send(embed=embed)

@bot.command(name="info")
async def info(ctx):
    embed = discord.Embed(
        title="About Laentaru Bot",
        description=(
            "Laentaru Bot is a Naruto-inspired Discord RPG bot. "
            "Players create a shinobi, roll a clan and affinities, train over time, collect items, duel other players, "
            "and join world events."
        ),
        color=discord.Color.teal()
    )

    embed.add_field(
        name="Core Systems",
        value=(
            "**Clans:** Rolled at character creation and affect affinity growth.\n"
            "**Skills:** Chakra Reserves, Chakra Control, Genjutsu, Taijutsu, and Speed.\n"
            "**Chakra Natures:** Fire, Water, Earth, Wind, Lightning, with rare Ying/Yang rolls.\n"
            "**Kekkei Genkai:** Rare awakenings from rolls, training, or special odds.\n"
            "**Sharingan:** Can level through training and fights if unlocked.\n"
            "**Curse Marks:** Temporary buffs or debuffs from events.\n"
            "**Tailed Beasts:** Captured beasts stay sealed for 24 hours and boost all traits by tail count."
        ),
        inline=False
    )

    embed.add_field(
        name="Progression",
        value=(
            "Training gives XP, stat growth, item chances, rare Kekkei Genkai chances, and possible Sharingan progression. "
            "PvP uses a unified scaling model across level, stats, affinities, clan, Kekkei Genkai, chakra natures, village, softcaps, resources, accuracy, guard, and status effects."
        ),
        inline=False
    )

    await ctx.send(embed=embed)


@bot.command(name="ladder", aliases=["rankings", "leaderboard"])
async def ladder(ctx):
    players = migrate_all_players(load_players())

    if not players:
        await send_notice(ctx, "No Players Found", "No player profiles exist yet.", "neutral")
        return

    leaderboard = []

    for user_id, player in players.items():
        if not player.get("has_rolled"):
            continue

        skill = calculate_hidden_skill_score(player)
        player["hidden_skill_score"] = skill

        leaderboard.append({
            "user_id": user_id,
            "name": player.get("name", "Unknown"),
            "level": player.get("level", 1),
            "rank": player.get("rank", get_ninja_rank(player.get("level", 1))),
            "clan": player.get("clan") or "Unknown",
            "skill": skill
        })

    if not leaderboard:
        await send_notice(ctx, "No Ranked Players", "Players need to use `$roll` before they appear on the ladder.", "neutral")
        return

    leaderboard.sort(key=lambda p: (p["level"], p["skill"]), reverse=True)
    save_players(players)

    embed = discord.Embed(
        title="Shinobi Ranking Ladder",
        description="Public rankings based on level and combat rating.",
        color=discord.Color.blurple()
    )

    for index, player in enumerate(leaderboard[:10], start=1):
        embed.add_field(
            name=f"#{index} {player['name']}",
            value=(
                f"**Level:** {player['level']}\n"
                f"**Rank:** {player['rank']}\n"
                f"**Clan:** {player['clan']}\n"
                f"**Combat Rating:** {player['skill']}"
            ),
            inline=False
        )

    await ctx.send(embed=embed)


@bot.command(name="devcursemark")
async def devcursemark(ctx):
    if ctx.author.id not in DEV_USER_IDS:
        await send_notice(ctx, "Developer Only", "You do not have permission to use this command.", "danger")
        return

    channel_id = CONFIG.get("CURSE_MARK", {}).get("announce_channel_id", ctx.channel.id)
    channel = bot.get_channel(channel_id) or ctx.channel

    await trigger_curse_mark_event(channel)


@bot.command(name="devconfig", aliases=["devscales"])
async def devconfig(ctx):
    if ctx.author.id not in DEV_USER_IDS:
        await send_notice(ctx, "Developer Only", "You do not have permission to use this command.", "danger")
        return

    embed = ui_embed("Developer Config", "Balance and content live inside `config.json`.", "info")
    add_field(embed, "You Can Tune", "Affinity rarity, clan rarity, Kekkei Genkai chances, Sharingan progression, curse marks, XP curve, training rewards, PvP hit chance, PvP damage, events, and difficulty.", False)
    add_field(embed, "Reload", "Use `$devreloadconfig` after most config changes. Restart only after code/startup setting changes.", False)
    await ctx.send(embed=embed)


@bot.command(name="devmode")
async def devmode(ctx):
    if ctx.author.id not in DEV_USER_IDS:
        await send_notice(ctx, "Developer Only", "You do not have permission to use this command.", "danger")
        return

    import copy
    players = migrate_all_players(load_players())
    user_id = str(ctx.author.id)

    if user_id not in players:
        players[user_id] = create_player(ctx)

    if not players[user_id].get("dev_backup"):
        players[user_id]["dev_backup"] = copy.deepcopy(players[user_id])

    players[user_id]["dev_mode"] = True
    players[user_id] = apply_devmode_to_player(players[user_id], ctx.author.name)
    save_players(players)

    embed = ui_embed("Developer Mode Enabled", f"{ctx.author.mention}, your test profile has been activated.", "purple")
    add_field(embed, "Profile Override", "\n".join([
        kv_line("Clan", DEV_MODE_CLAN),
        kv_line("Kekkei Genkai", DEV_MODE_KEKKEI_GENKAI),
        kv_line("Stats/Affinities", f"{fmt_num(DEV_MODE_STAT_VALUE)} across the board"),
    ]), False)
    await ctx.send(embed=embed)


@bot.command(name="devreset")
async def devreset(ctx):
    if ctx.author.id not in DEV_USER_IDS:
        await send_notice(ctx, "Developer Only", "You do not have permission to use this command.", "danger")
        return

    with open(DATA_FILE, "w") as file:
        json.dump({}, file, indent=4)

    await send_notice(ctx, "World Reset Complete", "All player data has been reset.", "danger")


@bot.command(name="devshowskill")
async def devshowskill(ctx):
    if ctx.author.id not in DEV_USER_IDS:
        await send_notice(ctx, "Developer Only", "You do not have permission to use this command.", "danger")
        return

    players = migrate_all_players(load_players())

    if not players:
        await send_notice(ctx, "No Players Found", "No player profiles exist yet.", "neutral")
        return

    leaderboard = []

    for user_id, player in players.items():
        skill = calculate_hidden_skill_score(player)
        player["hidden_skill_score"] = skill
        name = player.get("name", "Unknown")
        level = player.get("level", 1)
        rank = player.get("rank", get_ninja_rank(level))
        leaderboard.append((name, level, rank, skill))

    save_players(players)
    leaderboard.sort(key=lambda x: x[3], reverse=True)

    embed = discord.Embed(
        title="Developer Skill Score View",
        description="Hidden player power rankings",
        color=discord.Color.purple()
    )

    for i, (name, level, rank, skill) in enumerate(leaderboard, start=1):
        embed.add_field(
            name=f"{i}. {name}",
            value=f"Level: {level} | Rank: {rank} | Skill Score: {skill}",
            inline=False
        )

    await ctx.send(embed=embed)

@bot.command(name="devmodeoff")
async def devmodeoff(ctx):
    if ctx.author.id not in DEV_USER_IDS:
        await send_notice(ctx, "Developer Only", "You do not have permission to use this command.", "danger")
        return

    players = load_players()
    user_id = str(ctx.author.id)

    if user_id not in players:
        await send_notice(ctx, "Profile Required", "You do not have a profile.", "warning")
        return

    player = players[user_id]

    if not player.get("dev_mode"):
        await send_notice(ctx, "Dev Mode Inactive", "Dev mode is not active on your profile.", "warning")
        return

    # Restore backup
    backup = player.get("dev_backup")

    if not backup:
        await send_notice(ctx, "Backup Missing", "No dev mode backup was found, so your profile cannot be restored automatically.", "danger")
        return

    players[user_id] = backup

    save_players(players)

    await send_notice(ctx, "Dev Mode Disabled", "Your original character has been restored.", "success")


@bot.command(name="trainingreset", aliases=["resettraining", "devtrainingreset"])
async def trainingreset(ctx, member: discord.Member = None):
    if ctx.author.id not in DEV_USER_IDS:
        await send_notice(ctx, "Developer Only", "You do not have permission to use this command.", "danger")
        return

    players = migrate_all_players(load_players())
    member = member or ctx.author
    user_id = str(member.id)

    if user_id not in players:
        await send_notice(ctx, "Profile Not Found", f"{member.mention} does not have a profile.", "warning")
        return

    players[user_id]["last_training"] = None
    save_players(players)

    await send_notice(ctx, "Training Cooldown Reset", f"{member.mention}'s training cooldown has been reset.", "success")

@bot.command(name="devplayerreset")
async def devplayerreset(ctx, member: discord.Member):
    if ctx.author.id not in DEV_USER_IDS:
        await send_notice(ctx, "Developer Only", "You do not have permission to use this command.", "danger")
        return

    players = migrate_all_players(load_players())
    user_id = str(member.id)

    if user_id not in players:
        await send_notice(ctx, "Profile Not Found", f"{member.mention} does not have a profile.", "warning")
        return

    del players[user_id]
    save_players(players)

    embed = ui_embed("Player Profile Reset", f"{member.mention}'s profile has been reset and deleted.", "danger")
    add_field(embed, "Next Step", "They can use `$start` again to create a new profile.", False)
    await ctx.send(embed=embed)

@bot.command(name="fixtsuchi")
async def fixtsuchi(ctx):
    if ctx.author.id not in DEV_USER_IDS:
        return await send_notice(ctx, "Developer Only", "This command is developer only.", "danger")

    players = migrate_all_players(load_players())
    fixed_count = 0

    for user_id, profile in players.items():
        if profile.get("clan") != "Tsuchi":
            continue

        # Prevent duplicate stacking if the command gets run more than once.
        if profile.get("tsuchi_buff_applied"):
            continue

        profile.setdefault("stats", BASE_STATS.copy())
        profile.setdefault("affinities", {})
        profile.setdefault("known_jutsu", [])
        profile.setdefault("chakra_natures", [])

        # Apply Tsuchi clan affinity bonuses from config.json.
        bonuses = CONFIG.get("CLANS", {}).get("Tsuchi", {}).get("affinity_bonus", {})
        for stat, amount in bonuses.items():
            profile["affinities"][stat] = profile["affinities"].get(stat, 0) + amount

        # Extra god-tier Tsuchi stat buffs.
        profile["stats"]["Health"] = profile["stats"].get("Health", 100) + 2500
        profile["stats"]["Durability"] = profile["stats"].get("Durability", 100) + 2500
        profile["stats"]["Stamina"] = profile["stats"].get("Stamina", 100) + 2500
        profile.setdefault("affinities", {})
        profile["affinities"]["Chakra Reserves"] = profile["affinities"].get("Chakra Reserves", 100) + 2500

        # Force Maddosukin and give the forbidden Tsuchi jutsu.
        add_player_bloodline(profile, "Maddosukin", apply_modifiers=False)
        if "Tsuchi Maddosurappu" not in profile["known_jutsu"]:
            profile["known_jutsu"].append("Tsuchi Maddosurappu")

        # Unlock every chakra nature.
        for nature in NATURE_TYPES:
            if nature not in profile["chakra_natures"]:
                profile["chakra_natures"].append(nature)

        profile["hidden_skill_score"] = calculate_hidden_skill_score(profile)
        profile["tsuchi_buff_applied"] = True
        fixed_count += 1

    save_players(players)

    await send_notice(ctx, "Tsuchi Reawakened", f"Reawakened **{fixed_count}** Tsuchi clan member(s).", "purple")


# -----------------------------
# Developer Give Utility
# -----------------------------

def dev_normalize(value):
    return str(value or "").strip().lower().replace("_", " ").replace("-", " ")


def dev_find_key(collection, search_text):
    """Case-insensitive finder for config dictionaries/lists that supports multi-word names."""
    if not search_text:
        return None

    cleaned = dev_normalize(search_text)

    if isinstance(collection, dict):
        keys = list(collection.keys())
    else:
        keys = list(collection or [])

    for key in keys:
        if dev_normalize(key) == cleaned:
            return key

    for key in keys:
        key_clean = dev_normalize(key)
        if cleaned in key_clean or key_clean in cleaned:
            return key

    return None


def dev_split_name_amount(raw_text, default_amount=1):
    """Allows `$devgive @user item Hope of Tsuchi 3` without breaking multi-word names."""
    raw_text = str(raw_text or "").strip()
    if not raw_text:
        return "", default_amount

    parts = raw_text.rsplit(" ", 1)
    if len(parts) == 2:
        name_part, maybe_amount = parts
        try:
            amount = int(maybe_amount)
            return name_part.strip(), max(1, amount)
        except ValueError:
            pass

    return raw_text, default_amount


def dev_resolve_trait_name(raw_text):
    name = dev_find_key(BASE_STATS, raw_text)
    if name:
        return "stat", name
    name = dev_find_key(AFFINITIES, raw_text)
    if name:
        return "skill", name
    return None, None


def dev_available_options_text():
    return (
        "`ryo`, `xp`, `level`, `rerolls`, `item`, `jutsu`, `nature`, `kekkei`, `dojutsu`, "
        "`clan`, `village`, `stat`, `skill`, `setstat`, `setskill`, `title`, `allnatures`, `alljutsu`"
    )


@bot.command(name="devgive", aliases=["dgive", "giveplayer", "devgrant"])
async def devgive(ctx, member: discord.Member, give_type: str = None, *, value: str = None):
    if ctx.author.id not in DEV_USER_IDS:
        await send_notice(ctx, "Developer Only", "You do not have permission to use this command.", "danger")
        return

    if not give_type:
        embed = ui_embed("Devgive Usage", "Give almost anything to a player profile.", "info")
        add_field(embed, "Format", "`$devgive @player <type> <value>`", False)
        add_field(embed, "Types", dev_available_options_text(), False)
        add_field(embed, "Examples", "`$devgive @user ryo 5000`\n`$devgive @user item Hope of Tsuchi 1`\n`$devgive @user jutsu Phoenix Flame Jutsu`\n`$devgive @user setskill Chakra Control 12000`", False)
        await ctx.send(embed=embed)
        return

    players = migrate_all_players(load_players())
    user_id = str(member.id)

    if user_id not in players:
        players[user_id] = {
            "name": member.name,
            "level": 1,
            "xp": 0,
            "rank": "Academy Student",
            "stats": BASE_STATS.copy(),
            "affinities": {name: 1 for name in AFFINITIES},
            "chakra_natures": [],
            "clan": None,
            "kekkei_genkai": None,
            "kekkei_genkai_list": [],
            "kekkei_evolution": {},
            "bloodline_fragments": {},
            "devmode_enabled": False,
            "inventory": {},
            "hidden_skill_score": 0,
            "has_rolled": True,
            "last_training": None,
            "known_jutsu": [],
            "village": None,
            "rerolls_remaining": REROLLS_PER_PLAYER,
            "ryo": CONFIG.get("ECONOMY", {}).get("starting_ryo", 0),
            "daily_streak": 0,
            "weekly_streak": 0,
            "best_daily_streak": 0,
            "best_weekly_streak": 0,
            "last_daily": None,
            "last_weekly": None,
            "total_daily_claims": 0,
            "total_weekly_claims": 0,
            "gacha_rolls": 0,
            "gacha_pity": 0,
            "gacha_mythic_pulls": 0,
            "gacha_legendary_pulls": 0,
            "gacha_mythic_pity": 0,
            "missions_completed": 0,
            "mission_cooldowns": {},
            "kekkei_evolution": {},
            "bloodline_fragments": {}
        }

    player = migrate_player(players[user_id])
    player.setdefault("inventory", {})
    player.setdefault("known_jutsu", [])
    player.setdefault("chakra_natures", [])
    player.setdefault("stats", BASE_STATS.copy())
    player.setdefault("affinities", {name: 1 for name in AFFINITIES})

    give_type_clean = dev_normalize(give_type)
    value = str(value or "").strip()
    changes = []

    try:
        if give_type_clean in ["ryo", "money", "cash"]:
            amount = int(value)
            player["ryo"] = int(player.get("ryo", 0)) + amount
            changes.append(f"Ryo `{amount:+,}` → `{player['ryo']:,}`")

        elif give_type_clean in ["xp", "exp", "experience"]:
            amount = int(value)
            leveled_up, levels_gained = add_xp(player, amount)
            changes.append(f"XP `{amount:+,}`")
            if leveled_up:
                changes.append(f"Level Up `+{levels_gained}` → Level `{player.get('level', 1)}`")

        elif give_type_clean in ["level", "levels"]:
            amount = int(value)
            player["level"] = max(1, int(player.get("level", 1)) + amount)
            player["rank"] = get_ninja_rank(player["level"])
            changes.append(f"Level `{amount:+,}` → `{player['level']}`")

        elif give_type_clean in ["setlevel"]:
            amount = int(value)
            player["level"] = max(1, amount)
            player["rank"] = get_ninja_rank(player["level"])
            changes.append(f"Level set to `{player['level']}`")

        elif give_type_clean in ["reroll", "rerolls"]:
            amount = int(value)
            player["rerolls_remaining"] = int(player.get("rerolls_remaining", REROLLS_PER_PLAYER)) + amount
            changes.append(f"Rerolls `{amount:+,}` → `{player['rerolls_remaining']}`")

        elif give_type_clean == "item":
            item_name, amount = dev_split_name_amount(value, 1)
            matched = dev_find_key(ITEMS, item_name)
            if not matched:
                await send_notice(ctx, "Item Not Found", f"Could not find item `{item_name}` in config.json.", "warning")
                return
            add_item(player, matched, amount)
            changes.append(f"Item `{matched}` x`{amount}`")

        elif give_type_clean == "jutsu":
            matched = dev_find_key(JUTSU, value)
            if not matched:
                await send_notice(ctx, "Jutsu Not Found", f"Could not find jutsu `{value}` in config.json.", "warning")
                return
            if matched not in player["known_jutsu"]:
                player["known_jutsu"].append(matched)
                changes.append(f"Jutsu learned `{matched}`")
            else:
                changes.append(f"Jutsu already known `{matched}`")

        elif give_type_clean in ["alljutsu", "all jutsu"]:
            learned = 0
            for name, data in JUTSU.items():
                if data.get("requirements", {}).get("hidden"):
                    continue
                if name not in player["known_jutsu"]:
                    player["known_jutsu"].append(name)
                    learned += 1
            changes.append(f"Learned `{learned}` jutsu")

        elif give_type_clean in ["nature", "chakra nature", "natures"]:
            if dev_normalize(value) == "all":
                for nature in NATURE_TYPES:
                    if nature not in player["chakra_natures"]:
                        player["chakra_natures"].append(nature)
                changes.append("Unlocked all chakra natures")
            else:
                matched = dev_find_key(NATURE_TYPES, value)
                if not matched:
                    await send_notice(ctx, "Nature Not Found", f"Could not find chakra nature `{value}`.", "warning")
                    return
                if matched not in player["chakra_natures"]:
                    player["chakra_natures"].append(matched)
                changes.append(f"Nature unlocked `{matched}`")

        elif give_type_clean in ["allnatures", "all nature", "all natures"]:
            for nature in NATURE_TYPES:
                if nature not in player["chakra_natures"]:
                    player["chakra_natures"].append(nature)
            changes.append("Unlocked all chakra natures")

        elif give_type_clean in ["kekkei", "kekkei genkai", "bloodline", "dojutsu"]:
            matched = dev_find_key(CONFIG.get("TRAIT_MODIFIERS", {}).get("kekkei_genkai", {}), value) or dev_find_key(KEKKEI_GENKAI, value)
            if not matched:
                await send_notice(ctx, "Bloodline Not Found", f"Could not find bloodline/dojutsu `{value}`.", "warning")
                return
            added = add_player_bloodline(player, matched, apply_modifiers=True)
            if added:
                changes.append(f"Kekkei/Dojutsu added `{matched}`")
            else:
                changes.append(f"Kekkei/Dojutsu `{matched}` already owned")

        elif give_type_clean == "clan":
            matched = dev_find_key(CONFIG.get("CLANS", {}), value)
            if not matched:
                await send_notice(ctx, "Clan Not Found", f"Could not find clan `{value}`.", "warning")
                return
            player["clan"] = matched
            changes.append(f"Clan set to `{matched}`")

        elif give_type_clean == "village":
            matched = dev_find_key(VILLAGES, value)
            if not matched:
                await send_notice(ctx, "Village Not Found", f"Could not find village `{value}`.", "warning")
                return
            player["village"] = matched
            changes.append(f"Village set to `{matched}`")

        elif give_type_clean in ["title", "settitle"]:
            player["title"] = value
            changes.append(f"Title set to `{value}`")

        elif give_type_clean in ["stat", "skill", "addstat", "addskill", "setstat", "setskill"]:
            trait_text, amount = dev_split_name_amount(value, 0)
            if amount == 0:
                await send_notice(ctx, "Missing Amount", "Use a trait name followed by a number. Example: `$devgive @user setskill Chakra Control 12000`", "warning")
                return

            trait_kind, trait_name = dev_resolve_trait_name(trait_text)
            if give_type_clean in ["stat", "addstat", "setstat"] and trait_kind != "stat":
                await send_notice(ctx, "Stat Not Found", f"Could not find base stat `{trait_text}`.", "warning")
                return
            if give_type_clean in ["skill", "addskill", "setskill"] and trait_kind != "skill":
                await send_notice(ctx, "Skill Not Found", f"Could not find skill `{trait_text}`.", "warning")
                return

            if trait_kind == "stat":
                if give_type_clean.startswith("set"):
                    player["stats"][trait_name] = max(1, amount)
                    changes.append(f"Stat `{trait_name}` set to `{player['stats'][trait_name]:,}`")
                else:
                    player["stats"][trait_name] = max(1, int(player["stats"].get(trait_name, 0)) + amount)
                    changes.append(f"Stat `{trait_name}` `{amount:+,}` → `{player['stats'][trait_name]:,}`")
            else:
                if give_type_clean.startswith("set"):
                    player["affinities"][trait_name] = max(1, amount)
                    changes.append(f"Skill `{trait_name}` set to `{player['affinities'][trait_name]:,}`")
                else:
                    player["affinities"][trait_name] = max(1, int(player["affinities"].get(trait_name, 0)) + amount)
                    changes.append(f"Skill `{trait_name}` `{amount:+,}` → `{player['affinities'][trait_name]:,}`")

        else:
            embed = ui_embed("Unknown Devgive Type", f"`{give_type}` is not a valid devgive type.", "warning")
            add_field(embed, "Valid Types", dev_available_options_text(), False)
            await ctx.send(embed=embed)
            return

    except ValueError:
        await send_notice(ctx, "Invalid Amount", "That command needs a valid number amount.", "warning")
        return

    player["hidden_skill_score"] = calculate_hidden_skill_score(player)
    players[user_id] = player
    save_players(players)

    embed = ui_embed("Devgive Complete", f"Updated {member.mention}'s profile.", "success")
    add_field(embed, "Changes", "\n".join(f"- {line}" for line in changes) or "No changes made.", False)
    await ctx.send(embed=embed)


@bot.command(name="devscorefix", aliases=["scorefix", "fixscore", "devfixscore"])
async def devscorefix(ctx, member: discord.Member = None):
    """Developer command that recalculates hidden skill score after manual/dev edits.

    Usage:
    $devscorefix @player  -> fixes one player
    $devscorefix          -> fixes every saved player
    """
    if ctx.author.id not in DEV_USER_IDS:
        await send_notice(ctx, "Developer Only", "You do not have permission to use this command.", "danger")
        return

    players = migrate_all_players(load_players())

    if not players:
        await send_notice(ctx, "No Players Found", "No player profiles exist yet.", "warning")
        return

    # Fix one specific player when mentioned.
    if member is not None:
        user_id = str(member.id)

        if user_id not in players:
            await send_notice(ctx, "Profile Not Found", f"{member.mention} does not have a profile.", "warning")
            return

        player = players[user_id]
        old_score = int(player.get("hidden_skill_score", 0) or 0)

        sync_player_bloodlines(player)
        if player.get("level"):
            player["rank"] = get_ninja_rank(player.get("level", 1))
        player["hidden_skill_score"] = calculate_hidden_skill_score(player)
        new_score = int(player.get("hidden_skill_score", 0) or 0)

        players[user_id] = player
        save_players(players)

        embed = ui_embed("Skill Score Fixed", f"Recalculated {member.mention}'s hidden skill score.", "success")
        add_field(embed, "Result", "\n".join([
            kv_line("Old Score", fmt_num(old_score)),
            kv_line("New Score", fmt_num(new_score)),
            kv_line("Change", f"{new_score - old_score:+,}"),
            kv_line("Combat Rating", fmt_num(calculate_combat_rating(player))),
        ]), False)
        await ctx.send(embed=embed)
        return

    # No mention = fix everyone.
    fixed_count = 0
    changed_count = 0
    biggest_changes = []

    for user_id, player in players.items():
        old_score = int(player.get("hidden_skill_score", 0) or 0)

        sync_player_bloodlines(player)
        if player.get("level"):
            player["rank"] = get_ninja_rank(player.get("level", 1))
        player["hidden_skill_score"] = calculate_hidden_skill_score(player)

        new_score = int(player.get("hidden_skill_score", 0) or 0)
        delta = new_score - old_score
        fixed_count += 1

        if delta != 0:
            changed_count += 1
            biggest_changes.append((abs(delta), user_id, player.get("name", "Unknown"), old_score, new_score, delta))

    save_players(players)
    biggest_changes.sort(reverse=True, key=lambda item: item[0])

    embed = ui_embed("Skill Scores Fixed", "Recalculated hidden skill scores for all saved players.", "success")
    add_field(embed, "Summary", "\n".join([
        kv_line("Players Checked", fmt_num(fixed_count)),
        kv_line("Scores Changed", fmt_num(changed_count)),
    ]), False)

    if biggest_changes:
        lines = []
        for _, user_id, name, old_score, new_score, delta in biggest_changes[:8]:
            lines.append(f"**{name}** `<@{user_id}>`: {fmt_num(old_score)} → {fmt_num(new_score)} ({delta:+,})")
        add_field(embed, "Largest Changes", "\n".join(lines), False)

    await ctx.send(embed=embed)

@bot.command(name="devreloadconfig")
async def devreloadconfig(ctx):
    global CONFIG, TRAINING_COOLDOWN_MINUTES, DUEL_DURATION_SECONDS, DUEL_ROUND_DELAY_SECONDS
    global BASE_STATS, AFFINITIES, NATURE_TYPES, KEKKEI_GENKAI, ITEMS, JUTSU, VILLAGES, RANKS
    global LEVELS_PER_RANK, XP_BASE, XP_EXPONENT, DEV_USER_IDS, REROLLS_PER_PLAYER

    if ctx.author.id not in DEV_USER_IDS:
        await send_notice(ctx, "Developer Only", "You do not have permission to use this command.", "danger")
        return

    CONFIG = load_config()
    TRAINING_COOLDOWN_MINUTES = CONFIG.get("TRAINING_COOLDOWN_MINUTES", 30)
    DUEL_DURATION_SECONDS = CONFIG.get("DUEL_DURATION_SECONDS", 30)
    DUEL_ROUND_DELAY_SECONDS = CONFIG.get("DUEL_ROUND_DELAY_SECONDS", 1.5)
    BASE_STATS = CONFIG.get("BASE_STATS", {})
    AFFINITIES = CONFIG.get("AFFINITIES", [])
    NATURE_TYPES = CONFIG.get("NATURE_TYPES", [])
    KEKKEI_GENKAI = CONFIG.get("KEKKEI_GENKAI", [])
    ITEMS = CONFIG.get("ITEMS", {})
    JUTSU = CONFIG.get("JUTSU", {})
    VILLAGES = CONFIG.get("VILLAGES", {})
    RANKS = CONFIG.get("RANKS", [])
    LEVELS_PER_RANK = CONFIG.get("LEVELS_PER_RANK", 10)
    XP_BASE = CONFIG.get("XP_BASE", 100)
    XP_EXPONENT = CONFIG.get("XP_EXPONENT", 1.35)
    DEV_USER_IDS = set(CONFIG.get("DEVELOPER_IDS", []))
    REROLLS_PER_PLAYER = int(CONFIG.get("REROLLS_PER_PLAYER", 3))

    await send_notice(ctx, "Config Reloaded", "Config has been reloaded from `config.json`.", "success")

@bot.command(name="jiawen")
async def jiawen(ctx):
    await send_notice(ctx, "Jiawen", "Jiawen is amazing, and she loves Laen.", "success")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await send_notice(ctx, "Missing Input", f"Missing input: `{error.param.name}`. Use `$help` for examples.", "warning")
        return
    if isinstance(error, commands.BadArgument):
        await send_notice(ctx, "Invalid Command Value", "I could not understand one of the values you entered. Check `$help` for the command format.", "warning")
        return
    print(f"Command error in {getattr(ctx.command, 'name', 'unknown')}: {error}")
    await send_notice(ctx, "Command Error", "Something went wrong while running that command. The error was logged in the console.", "danger")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing. Add it to your environment variables or a local .env file.")

bot.run(TOKEN)
