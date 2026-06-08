import asyncio
import json
import os
import requests
import websockets
import time
from dotenv import load_dotenv
from goal_messages import GOAL_MESSAGES

load_dotenv()

AP_SERVER = os.getenv("AP_SERVER", "ws://PUT IP AND PORT HERE")
AP_SLOT_NAME = os.getenv("AP_SLOT_NAME", "PUT SLOT NAME HERE")
AP_PASSWORD = os.getenv("AP_PASSWORD", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "PUT WEBHOOK ADDRESS HERE")

item_id_to_name = {}
location_id_to_name = {}
player_id_to_name = {}
player_id_to_game = {}
slot_info_by_slot = {}

GOAL_RELEASE_IDLE_SECONDS = 10
GOAL_RELEASE_MAX_SECONDS = 90

goal_release_buffers = {}
goal_release_tasks = {}

def should_send(text):
    if not text:
        return False

    lower = text.lower()

    ignore_patterns = [
        "has joined. client(",
        "has left the game",
        "has started tracking the game",
        "has stopped tracking the game",
        "now that you are connected",
        "you can use !help",
        "you may have additional local commands",
        "you can list with /help",
        "poptracker",
    ]

    return not any(pattern in lower for pattern in ignore_patterns)

def get_embed_color(text):
    lower = text.lower()

    if "(trap)" in lower:
        return 0xFF0000
    if "(important)" in lower or "(priority)" in lower:
        return 0xFFD700
    if "(useful)" in lower:
        return 0x3498DB

    return 0xAAAAAA

def build_embed(text):
    return {
        "embeds": [
            {
                "color": get_embed_color(text),
                "description": text[:4000],
            }
        ]
    }

def extract_goal_player(text):
    if "has completed their goal" not in text.lower():
        return None

    marker = " (Team #"

    if marker in text:
        return text.split(marker)[0].strip()

    return None


def get_player_game(player_name):
    for slot, name in player_id_to_name.items():
        if name == player_name:
            return player_id_to_game.get(slot, "")

    return ""


def build_goal_embed(player_name):
    game_name = get_player_game(player_name)

    template = GOAL_MESSAGES.get(
        game_name,
        {
            "title": "🏆 Goal Completed!",
            "description": "🎉 **{player}** has completed their Archipelago goal!"
        }
    )

    return {
        "embeds": [
            {
                "title": template["title"],
                "description": template["description"].format(
                    player=player_name
                ),
                "color": 0x00FF00
            }
        ]
    }


def get_player_slot_by_name(player_name):
    for slot, name in player_id_to_name.items():
        if name == player_name:
            return str(slot)

    return None


def get_item_send_metadata(packet):
    if packet.get("type") != "ItemSend":
        return None

    item = packet.get("item", {})

    sender_slot = item.get("player")
    receiver_slot = packet.get("receiving")
    flags = item.get("flags", 0)

    if sender_slot is None or receiver_slot is None:
        return None

    return {
        "sender_slot": str(sender_slot),
        "receiver_slot": str(receiver_slot),
        "flags": int(flags),
    }

async def flush_goal_release_report(sender_slot):
    buffer = goal_release_buffers.pop(sender_slot, None)

    task = goal_release_tasks.pop(sender_slot, None)
    if task and not task.done():
        task.cancel()

    if not buffer:
        return

    player_name = buffer["player_name"]
    items = buffer["items"]

    if not items:
        return

    external_items = [
        item for item in items
        if item["sender_slot"] != item["receiver_slot"]
    ]


    external_progression = [item for item in external_items if item["flags"] & 0x01]
    external_useful = [item for item in external_items if item["flags"] & 0x02]
    external_traps = [item for item in external_items if item["flags"] & 0x04]
    external_filler = [
        item for item in external_items
        if not (item["flags"] & 0x01 or item["flags"] & 0x02 or item["flags"] & 0x04)
    ]

    important_external = external_progression + external_useful

    if important_external:
        external_lines = "\n".join(item["text"] for item in important_external[:10])

        if len(important_external) > 10:
            external_lines += f"\n_...and {len(important_external) - 10} more progression/useful checks to other players._"
    else:
        external_lines = "_No progression/useful checks were sent to other players._"

    report = (
        f"📜 **{player_name}'s Goal Release Report**\n\n"
        f"**Sent to Other Players:**\n"
        f"⭐ Progression: **{len(external_progression)}**\n"
        f"💡 Useful: **{len(external_useful)}**\n"
        f"🎁 Filler: **{len(external_filler)}**\n"
        f"💀 Traps: **{len(external_traps)}**\n\n"
        f"**Important Releases to Others:**\n"
        f"{external_lines}\n\n"

    )

    await send_discord(report)

async def goal_release_idle_timer(sender_slot):
    try:
        await asyncio.sleep(GOAL_RELEASE_IDLE_SECONDS)
        await flush_goal_release_report(sender_slot)
    except asyncio.CancelledError:
        pass


def reset_goal_release_timer(sender_slot):
    old_task = goal_release_tasks.get(sender_slot)

    if old_task and not old_task.done():
        old_task.cancel()

    goal_release_tasks[sender_slot] = asyncio.create_task(
        goal_release_idle_timer(sender_slot)
    )


async def start_goal_release_buffer(player_name):
    sender_slot = get_player_slot_by_name(player_name)

    if not sender_slot:
        return

    goal_release_buffers[sender_slot] = {
        "player_name": player_name,
        "started": time.time(),
        "items": [],
    }

    reset_goal_release_timer(sender_slot)

async def handle_printjson_packet(packet):
    text = render_print_json(packet)

    goal_player = extract_goal_player(text)

    if goal_player:
        await send_discord(text)

        await send_discord(
            "📦 **Processing released checks...**\n"
            "A summary report is being generated!"
        )

        await start_goal_release_buffer(goal_player)
        return

    metadata = get_item_send_metadata(packet)

    if metadata:
        sender_slot = metadata["sender_slot"]
        receiver_slot = metadata["receiver_slot"]

        # Goal player sent an item to someone else:
        # buffer it for the report.
        if sender_slot in goal_release_buffers:
            buffer = goal_release_buffers[sender_slot]

            if sender_slot != receiver_slot:
                buffer["items"].append({
                    "text": text,
                    "flags": metadata["flags"],
                    "sender_slot": sender_slot,
                    "receiver_slot": receiver_slot,
                })

            if time.time() - buffer["started"] >= GOAL_RELEASE_MAX_SECONDS:
                await flush_goal_release_report(sender_slot)
            else:
                reset_goal_release_timer(sender_slot)

            return

        # Someone sent an item to the goal player:
        # suppress it entirely.
        if receiver_slot in goal_release_buffers:
            reset_goal_release_timer(receiver_slot)
            return

    await send_discord(text)

async def send_discord(text):
    if not should_send(text):
        return

    if not DISCORD_WEBHOOK_URL.startswith("https://discord.com/api/webhooks/"):
        raise RuntimeError("DISCORD_WEBHOOK_URL is missing or invalid. Check your .env file.")

    goal_player = extract_goal_player(text)

    if goal_player:
        requests.post(
            DISCORD_WEBHOOK_URL,
            json=build_goal_embed(goal_player)
        )
    else:
        requests.post(
            DISCORD_WEBHOOK_URL,
            json=build_embed(text)
        )

def load_data_package(packet):
    games = packet.get("data", {}).get("games", {})

    for game_name, game_data in games.items():
        for name, item_id in game_data.get("item_name_to_id", {}).items():
            item_id_to_name[(game_name, str(item_id))] = name

        for name, location_id in game_data.get("location_name_to_id", {}).items():
            location_id_to_name[(game_name, str(location_id))] = name

def discord_escape(text):
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace("*", "\\*")
        .replace("_", "\\_")
        .replace("`", "\\`")
        .replace("~", "\\~")
    )

def resolve_item_name(item_id, player_slot=None):
    item_id = str(item_id)
    slot = str(player_slot) if player_slot is not None else None
    game = player_id_to_game.get(slot) if slot else None

    if game:
        result = item_id_to_name.get((game, item_id))
        if result:
            return result

    matches = [
        name
        for (game_name, stored_id), name in item_id_to_name.items()
        if stored_id == item_id
    ]

    if len(matches) == 1:
        return matches[0]

    print(f"[ITEM LOOKUP FAIL] item_id={item_id}, slot={slot}, game={game}, matches={len(matches)}")
    return item_id

def resolve_location_name(location_id, player_slot=None):
    location_id = str(location_id)
    slot = str(player_slot) if player_slot is not None else None
    game = player_id_to_game.get(slot) if slot else None

    if game:
        result = location_id_to_name.get((game, location_id))
        if result:
            return result

    matches = [
        name
        for (game_name, stored_id), name in location_id_to_name.items()
        if stored_id == location_id
    ]

    if len(matches) == 1:
        return matches[0]

    print(f"[LOCATION LOOKUP FAIL] location_id={location_id}, slot={slot}, game={game}, matches={len(matches)}")
    return location_id


def render_print_json(packet):
    rendered = []

    player_slots = [
        str(part.get("text"))
        for part in packet.get("data", [])
        if isinstance(part, dict) and part.get("type") == "player_id"
    ]

    first_player_slot = player_slots[0] if len(player_slots) > 0 else None
    second_player_slot = player_slots[1] if len(player_slots) > 1 else None
    current_player_slot = None

    for part in packet.get("data", []):
        if not isinstance(part, dict):
            rendered.append(discord_escape(part))
            continue

        part_type = part.get("type")
        text = str(part.get("text", ""))

        if part_type == "player_id":
            current_player_slot = text
            player_name = player_id_to_name.get(text, text)
            rendered.append(f"👤 **{discord_escape(player_name)}**")

        elif part_type == "item_id":
            # Portable rule:
            # 1. Trust Archipelago's item owner field first.
            # 2. If missing, use receiver slot for item sends.
            player_slot = part.get("player") or second_player_slot or current_player_slot
            item_name = resolve_item_name(text, player_slot)
            flags = int(part.get("flags", 0))

            if flags & 0x04:
                icon = "💀"
                label = "Trap"
            elif flags & 0x01:
                icon = "⭐"
                label = "Progression"
            elif flags & 0x02:
                icon = "💡"
                label = "Useful"
            else:
                icon = "🎁"
                label = "Filler"

            rendered.append(f"{icon} **{discord_escape(item_name)}** `({label})`")

        elif part_type == "location_id":
            # Portable rule:
            # 1. Trust Archipelago's location owner field first.
            # 2. If missing, use sender/finder slot.
            player_slot = part.get("player") or first_player_slot or current_player_slot
            location_name = resolve_location_name(text, player_slot)
            rendered.append(f"\n\n📍 *{discord_escape(location_name)}*")

        else:
            if text.strip() in ("(", ")"):
                continue

            rendered.append(discord_escape(text))

    return "".join(rendered)

async def main():
    async with websockets.connect(AP_SERVER) as ws:
        room_info_raw = await ws.recv()
        room_info_packets = json.loads(room_info_raw)

        games = []

        for packet in room_info_packets:
            if packet.get("cmd") == "RoomInfo":
                games = packet.get("games", [])

                for slot, info in packet.get("slot_info", {}).items():
                    slot = str(slot)
                    slot_info_by_slot[slot] = info

                    game = info.get("game", "")
                    if game:
                        player_id_to_game[slot] = game

        print("\n=== SLOT GAME MAP AFTER ROOMINFO ===")
        print(json.dumps(player_id_to_game, indent=2))
        print("===================================\n")

        await ws.send(json.dumps([{
            "cmd": "Connect",
            "password": AP_PASSWORD,
            "name": AP_SLOT_NAME,
            "version": {"major": 0, "minor": 6, "build": 0, "class": "Version"},
            "tags": ["TextOnly"],
            "items_handling": 0,
            "uuid": "ap-discord-webhook",
            "game": "",
            "slot_data": True,
        }]))

        if games:
            await ws.send(json.dumps([{
                "cmd": "GetDataPackage",
                "games": games,
            }]))

        async for msg in ws:
            for packet in json.loads(msg):
                cmd = packet.get("cmd")

                if cmd == "Connected":
                    for slot, info in packet.get("slot_info", {}).items():
                        slot = str(slot)
                        slot_info_by_slot[slot] = info

                        game = info.get("game", "")
                        if game:
                            player_id_to_game[slot] = game
                    for player in packet.get("players", []):
                        slot = str(player.get("slot"))
                        player_id_to_name[slot] = player.get("name", slot)

                        game = player.get("game", "")
                        if game:
                            player_id_to_game[slot] = game
                        elif slot not in player_id_to_game:
                            player_id_to_game[slot] = slot_info_by_slot.get(slot, {}).get("game", "")

                    print("\n=== SLOT GAME MAP AFTER CONNECTED ===")
                    print(json.dumps(player_id_to_game, indent=2))
                    print("====================================\n")
                    continue

                if cmd == "DataPackage":
                    load_data_package(packet)
                    continue

                if cmd == "PrintJSON":
                    await handle_printjson_packet(packet)

                elif cmd == "Print":
                    await send_discord(packet.get("text", ""))

try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("\nBot stopped by user.")
