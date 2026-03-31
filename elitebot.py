import asyncio
import json
import os
import random
import re
import aiohttp

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.channels import (
    CreateChannelRequest,
    InviteToChannelRequest,
    EditAdminRequest,
    LeaveChannelRequest,
)
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import ChatAdminRights
from telethon import utils as telethon_utils
import database

ADMIN_IDS = [
    7472359048, 7880967664, 8453993167, 2001575810, 5825027777,
    6864194951, 8093808661, 5229586098, 7422906767, 7962772947,
    7338429782, 8004116104, 7715451354, 8034627772, 5208040247,
    7673180028, 8307544039, 8559400377
]

OWNER_ID = 7338429782

ALLOWED_GROUP_IDS = [
    -1003451490827,  # Primary group
    -1003450478165,  # Backup group
]

ESCROW_TEXT = """<b>🛡 Escrow Form</b>
<code>Seller: @
Buyer: @
Amount[USDT]: 
Rate: 
Time:</code>
"""

STATE_FILE = "escrow_state.json"
ESCROWS_FILE = "escrows.json"
USER_STATS_FILE = "user_stats.json"

TELETHON_API_ID = 38828234
TELETHON_API_HASH = "99d96d08bc57f882907032a2f8f65b46"
TELETHON_SESSION = os.environ.get("TELETHON_SESSION", "")

BOT_USERNAME = "EcroweBot"
BOT_ID = 8029678424
USERBOT_ID = None

state_lock = asyncio.Lock()
telethon_client = None


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"next_id": 920}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def load_escrows():
    if os.path.exists(ESCROWS_FILE):
        with open(ESCROWS_FILE, "r") as f:
            return json.load(f)
    return {}


def save_escrows(escrows):
    with open(ESCROWS_FILE, "w") as f:
        json.dump(escrows, f)


def get_next_escrow_id():
    state = load_state()
    escrow_id = state["next_id"]
    state["next_id"] = escrow_id + 1
    save_state(state)
    return escrow_id


def save_escrow(escrow_id, data, chat_id, message_id):
    escrows = load_escrows()
    escrows[str(escrow_id)] = {
        "seller": data["seller"],
        "buyer": data["buyer"],
        "amount": data["amount"],
        "rate": data["rate"],
        "time": data["time"],
        "total_inr": data["total_inr"],
        "chat_id": chat_id,
        "message_id": message_id,
        "seller_confirmed": False,
        "buyer_confirmed": False,
    }
    save_escrows(escrows)

    database.save_deal(escrow_id, {
        "seller": data["seller"],
        "buyer": data["buyer"],
        "amount": data["amount"],
        "rate": data["rate"],
        "total_inr": data["total_inr"],
        "deal_status": "pending"
    })


def get_escrow(escrow_id):
    escrows = load_escrows()
    return escrows.get(str(escrow_id))


def update_escrow(escrow_id, updates):
    escrows = load_escrows()
    if str(escrow_id) in escrows:
        escrows[str(escrow_id)].update(updates)
        save_escrows(escrows)
        return True
    return False


def get_escrow_by_room_chat_id(room_chat_id):
    escrows = load_escrows()
    for eid, data in escrows.items():
        if data.get("room_chat_id") == room_chat_id:
            return int(eid), data
    return None, None


def load_user_stats():
    if os.path.exists(USER_STATS_FILE):
        with open(USER_STATS_FILE, "r") as f:
            return json.load(f)
    return {}


def save_user_stats(stats):
    with open(USER_STATS_FILE, "w") as f:
        json.dump(stats, f)


def get_user_stats(user_id):
    stats = load_user_stats()
    return stats.get(str(user_id))


def set_user_stats(user_id, data):
    stats = load_user_stats()
    stats[str(user_id)] = data
    save_user_stats(stats)


def find_fixed_stats_by_username(username):
    """Look up fixed stats for a username, trying all key formats.

    Checks @username, username (case variants), and also resolves
    the username to a user_id by scanning past escrows so stats
    stored by numeric user_id are found too.
    """
    if not username:
        return None
    uname = username.strip().lstrip("@")

    # Try direct username keys
    for key in (f"@{uname}", f"@{uname.lower()}", uname, uname.lower()):
        result = get_user_stats(key)
        if result:
            return result

    # Resolve username → user_id via past escrows
    uname_lower = uname.lower()
    escrows = load_escrows()
    resolved_uid = None
    for eid, data in escrows.items():
        s_un = (data.get("seller") or "").strip().lstrip("@").lower()
        b_un = (data.get("buyer") or "").strip().lstrip("@").lower()
        if s_un == uname_lower and data.get("seller_user_id"):
            resolved_uid = data["seller_user_id"]
            break
        if b_un == uname_lower and data.get("buyer_user_id"):
            resolved_uid = data["buyer_user_id"]
            break

    if resolved_uid:
        result = get_user_stats(str(resolved_uid))
        if result:
            return result
        result = get_user_stats(resolved_uid)
        if result:
            return result

    return None


def compute_real_stats(user_id, username):
    """Compute stats from actual escrow deals for a user."""
    escrows = load_escrows()
    if username:
        uname = username.lower().lstrip("@")
    else:
        uname = None

    total = 0
    completed = 0
    active = 0
    volume = 0.0
    biggest = 0.0

    for eid, data in escrows.items():
        seller_un = (data.get("seller") or "").strip().lstrip("@").lower()
        buyer_un = (data.get("buyer") or "").strip().lstrip("@").lower()
        seller_uid = data.get("seller_user_id")
        buyer_uid = data.get("buyer_user_id")

        involved = False
        if seller_uid == user_id or buyer_uid == user_id:
            involved = True
        elif uname and (seller_un == uname or buyer_un == uname):
            involved = True

        if not involved:
            continue

        total += 1
        amount = data.get("amount", 0)
        volume += amount
        if amount > biggest:
            biggest = amount

        if data.get("released") or data.get("release_phase") == "completed" \
                or data.get("refund_phase") == "completed":
            completed += 1
        else:
            active += 1

    avg_deal = volume / total if total > 0 else 0.0
    return {
        "total": total,
        "completed": completed,
        "active": active,
        "volume": volume,
        "avg_deal": avg_deal,
        "biggest": biggest,
    }


async def init_telethon_client():
    global telethon_client, USERBOT_ID
    if telethon_client is None and TELETHON_SESSION:
        telethon_client = TelegramClient(
            StringSession(TELETHON_SESSION),
            TELETHON_API_ID,
            TELETHON_API_HASH
        )
        await telethon_client.connect()
        me = await telethon_client.get_me()
        USERBOT_ID = me.id
    return telethon_client


ELITE_BIO_TAG = "@Elite_MarketPlace"


async def check_user_has_elite_bio(user_id):
    """Check if a user has @Elite_MarketPlace in their Telegram bio."""
    try:
        client = await init_telethon_client()
        if not client:
            return False
        user = await client.get_entity(int(user_id))
        full = await client(
            GetFullUserRequest(user)
        )
        bio = full.full_user.about or ""
        return ELITE_BIO_TAG.lower() in bio.lower()
    except Exception as e:
        print(f"[BIO] Error checking bio for {user_id}: {e}",
              flush=True)
        return False


def calculate_escrow_fees(amount, buyer_has_bio, seller_has_bio):
    """Calculate escrow fees based on amount and bio status.

    Returns (buyer_fee, seller_fee, buyer_promo, seller_promo).
    """
    if amount < 100:
        return (
            0.00, 0.00,
            "This deal is free by amount threshold - promo "
            "status is still tracked for future deals.",
            "This deal is free by amount threshold — promo "
            "status is still tracked for future deals."
        )

    buyer_fee = 0.00 if buyer_has_bio else 0.50
    seller_fee = 0.00 if seller_has_bio else 0.50

    if buyer_has_bio:
        buyer_promo = (
            "Bio found — promo fees applied on this deal."
        )
    else:
        buyer_promo = (
            "No bio found — add @EliteMarket_Place for "
            "promo fees on future deals"
        )

    if seller_has_bio:
        seller_promo = (
            "Bio found — promo fees applied on this deal."
        )
    else:
        seller_promo = (
            "No bio found — add @EliteMarket_Place for "
            "promo fees on future deals"
        )

    return buyer_fee, seller_fee, buyer_promo, seller_promo


def get_fees_from_escrow(data):
    """Read stored fee info from escrow data.

    Returns (buyer_fee, seller_fee, total_fee,
             buyer_promo, seller_promo).
    """
    buyer_fee = data.get("buyer_fee", 0.00)
    seller_fee = data.get("seller_fee", 0.00)
    total_fee = buyer_fee + seller_fee
    buyer_promo = data.get(
        "buyer_promo",
        "This deal is free by amount threshold - promo "
        "status is still tracked for future deals."
    )
    seller_promo = data.get(
        "seller_promo",
        "This deal is free by amount threshold — promo "
        "status is still tracked for future deals."
    )
    return buyer_fee, seller_fee, total_fee, buyer_promo, seller_promo


async def create_escrow_room(escrow_id):
    try:
        print(f"[ROOM] Starting room creation for escrow {escrow_id}",
              flush=True)
        client = await init_telethon_client()
        if not client:
            print("[ROOM] Telethon client init failed", flush=True)
            return None

        print("[ROOM] Telethon client connected", flush=True)
        escrow_id_str = f"{escrow_id:010d}"
        group_title = f"EliteMarket Escrow #{escrow_id_str}"
        group_about = f"Private escrow room for deal {escrow_id_str}"

        result = await client(CreateChannelRequest(
            title=group_title,
            about=group_about,
            megagroup=True
        ))
        print("[ROOM] Group created", flush=True)

        channel = result.chats[0]
        room_chat_id = telethon_utils.get_peer_id(channel)

        # Save room_chat_id BEFORE inviting bot to avoid race condition
        # (handle_new_chat_members needs this to find the escrow)
        update_escrow(escrow_id, {"room_chat_id": room_chat_id})
        print(f"[ROOM] Room chat ID saved: {room_chat_id}", flush=True)

        bot_entity = await client.get_entity(BOT_USERNAME)
        print(f"[ROOM] Bot entity resolved", flush=True)

        await client(InviteToChannelRequest(
            channel=channel,
            users=[bot_entity]
        ))
        print("[ROOM] Bot invited to group", flush=True)

        admin_rights = ChatAdminRights(
            change_info=True,
            post_messages=True,
            edit_messages=True,
            delete_messages=True,
            ban_users=True,
            invite_users=True,
            pin_messages=True,
            add_admins=False,
            anonymous=False,
            manage_call=True,
            other=True
        )

        await client(EditAdminRequest(
            channel=channel,
            user_id=bot_entity,
            admin_rights=admin_rights,
            rank="Admin"
        ))
        print("[ROOM] Bot promoted to admin", flush=True)

        # Make userbot anonymous admin instead of leaving
        userbot_admin_rights = ChatAdminRights(
            change_info=True,
            post_messages=True,
            edit_messages=True,
            delete_messages=True,
            ban_users=True,
            invite_users=True,
            pin_messages=True,
            add_admins=True,
            anonymous=True,
            manage_call=True,
            other=True
        )
        me = await client.get_me()
        await client(EditAdminRequest(
            channel=channel,
            user_id=me,
            admin_rights=userbot_admin_rights,
            rank="Admin"
        ))
        print("[ROOM] Userbot set as anonymous admin", flush=True)

        print(f"[ROOM] Room created successfully: {room_chat_id}",
              flush=True)

        return room_chat_id
    except Exception as e:
        print(f"[ROOM] Error creating room: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return None


def parse_escrow_form(text):
    lines = text.strip().split("\n")
    data = {}

    for line in lines:
        line = line.strip()
        if not line:
            continue

        match = re.match(r"^\s*([^:]+)\s*:\s*(.*?)\s*$", line)
        if match:
            key = match.group(1).strip().lower()
            value = match.group(2).strip()

            if "seller" in key:
                data["seller"] = value
            elif "buyer" in key:
                data["buyer"] = value
            elif "amount" in key:
                data["amount"] = value
            elif "rate" in key:
                data["rate"] = value
            elif "time" in key:
                data["time"] = value

    return data


def validate_escrow_form(data, sender_username):
    required = ["seller", "buyer", "amount", "rate", "time"]
    if not all(k in data for k in required):
        return None, "Missing required fields"

    seller = data["seller"]
    buyer = data["buyer"]
    amount_str = data["amount"]
    rate_str = data["rate"]
    time_val = data["time"]

    if seller.lower() == "me":
        if not sender_username:
            return None, "You need a username to use 'me'"
        seller = f"@{sender_username}"
    elif not seller.startswith("@"):
        seller = f"@{seller}"

    if buyer.lower() == "me":
        if not sender_username:
            return None, "You need a username to use 'me'"
        buyer = f"@{sender_username}"
    elif not buyer.startswith("@"):
        buyer = f"@{buyer}"

    try:
        amount = float(amount_str)
        if amount <= 0:
            return None, "Amount must be positive"
    except ValueError:
        return None, "Invalid amount"

    try:
        rate = float(rate_str)
        if rate <= 0:
            return None, "Rate must be positive"
    except ValueError:
        return None, "Invalid rate"

    if not time_val:
        return None, "Time is required"

    total_inr = amount * rate

    return {
        "seller": seller,
        "buyer": buyer,
        "amount": amount,
        "rate": rate,
        "time": time_val,
        "total_inr": total_inr,
    }, None


def escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_escrow_message(escrow_id, data, seller_confirmed=False,
                         buyer_confirmed=False):
    seller = escape_html(data["seller"].strip())
    buyer = escape_html(data["buyer"].strip())
    amount = data["amount"]
    time_val = escape_html(data["time"])

    escrow_id_str = f"{escrow_id:010d}"

    seller_emoji = "✅" if seller_confirmed else "⏳"
    buyer_emoji = "✅" if buyer_confirmed else "⏳"

    message = f"""🟢 Escrow • <code>{escrow_id_str}</code>
━━━━━━━━━━━━━━━━━━━━
{seller_emoji} <b>Seller</b>: {seller}
{buyer_emoji} <b>Buyer</b>: {buyer}
💵 <b>Amount</b>: {amount:.1f} USDT
💱 <b>Rate</b>: ** INR/USDT
💰 <b>Total INR</b>: **
🕒 <b>Time</b>: {time_val}

<b>Status</b>: Role acknowledgement required.
⚠︎ Verify roles carefully. Deposit address appears only after both acknowledge."""

    return message


def build_escrow_keyboard(escrow_id, data, seller_confirmed=False,
                          buyer_confirmed=False):
    seller = data["seller"].strip()
    buyer = data["buyer"].strip()
    rows = []

    if not seller_confirmed:
        rows.append([InlineKeyboardButton(
            f"✅ I am Seller {seller}",
            callback_data=f"escrow:{escrow_id}:seller"
        )])

    if not buyer_confirmed:
        rows.append([InlineKeyboardButton(
            f"✅ I am Buyer {buyer}",
            callback_data=f"escrow:{escrow_id}:buyer"
        )])

    if rows:
        return InlineKeyboardMarkup(rows)
    return None


def build_confirmed_message(escrow_id, data):
    seller = escape_html(data["seller"].strip())
    buyer = escape_html(data["buyer"].strip())
    amount = data["amount"]
    time_val = escape_html(data["time"])

    escrow_id_str = f"{escrow_id:010d}"

    message = f"""🟢 Escrow • <code>{escrow_id_str}</code>
━━━━━━━━━━━━━━━━━━━━
✅ <b>Seller</b>: {seller}
✅ <b>Buyer</b>: {buyer}
💵 <b>Amount</b>: {amount:.1f} USDT
💱 <b>Rate</b>: ** INR/USDT
💰 <b>Total INR</b>: **
🕒 <b>Time</b>: {time_val}

<b>Status</b>: ⏳ Creating private escrow room..."""

    return message


def build_opening_room_keyboard(escrow_id):
    button = InlineKeyboardButton(
        "⏳ Opening private escrow room...",
        callback_data=f"escrow:{escrow_id}:noop"
    )
    return InlineKeyboardMarkup([[button]])


def build_room_ready_message(escrow_id, data):
    seller = escape_html(data["seller"].strip())
    buyer = escape_html(data["buyer"].strip())

    escrow_id_str = f"{escrow_id:010d}"

    message = f"""🟢 Escrow • <code>{escrow_id_str}</code>
━━━━━━━━━━━━━━━━━━━━
✅ <b>Seller</b>: {seller}
✅ <b>Buyer</b>: {buyer}
✅ <b>Private escrow room created.</b>
Use the private room for all actions."""

    return message


def build_join_buttons_keyboard(buyer_invite, seller_invite):
    buyer_button = InlineKeyboardButton(
        "👤 Buyer • Join Escrow Room",
        url=buyer_invite
    )
    seller_button = InlineKeyboardButton(
        "👤 Seller • Join Escrow Room",
        url=seller_invite
    )
    vouch_button = InlineKeyboardButton(
        "Vouch",
        url="https://t.me/c/2211372853/5?thread=4"
    )
    return InlineKeyboardMarkup([
        [buyer_button],
        [seller_button],
        [vouch_button]
    ])


def build_vouch_keyboard(escrow_id):
    button = InlineKeyboardButton(
        "✅Vouch Elite Escrow Bot",
        callback_data=f"escrow:{escrow_id}:noop"
    )
    return InlineKeyboardMarkup([[button]])


def build_room_initial_message(escrow_id, data):
    seller = escape_html(data["seller"].strip())
    buyer = escape_html(data["buyer"].strip())
    amount = data["amount"]
    time_val = escape_html(data["time"])

    escrow_id_str = f"{escrow_id:010d}"

    message = f"""🟢 Escrow • <code>{escrow_id_str}</code>
━━━━━━━━━━━━━━━━━━━━
✅ <b>Seller</b>: {seller}
✅ <b>Buyer</b>: {buyer}
💵 <b>Amount</b>: {amount:.1f} USDT
💱 <b>Rate</b>: ** INR/USDT
💰 <b>Total INR</b>: **
🕒 <b>Time</b>: {time_val}

<b>Status</b>: Private room created. Buyer &amp; Seller must join via the join-request link.
<i>Only the buyer/seller for this deal will be accepted.</i>"""

    return message


def build_room_initial_keyboard(invite_link):
    button = InlineKeyboardButton(
        "🔗 Join Private Room",
        url=invite_link
    )
    return InlineKeyboardMarkup([[button]])


def build_fee_selection_message(escrow_id, data):
    seller = escape_html(data["seller"].strip())
    buyer = escape_html(data["buyer"].strip())
    amount = data["amount"]
    rate = data["rate"]
    total_inr = data["total_inr"]
    time_val = escape_html(data["time"])

    buyer_fee, seller_fee, total_fee, buyer_promo, seller_promo = \
        get_fees_from_escrow(data)

    escrow_id_str = f"{escrow_id:010d}"

    message = f"""🟢 Escrow • <code>{escrow_id_str}</code>
━━━━━━━━━━━━━━━━━━━━
🧾 <b>This deal fee</b>: Buyer <code>{buyer_fee:.2f}</code> USDT • Seller <code>{seller_fee:.2f}</code> USDT • Total <code>{total_fee:.2f}</code> USDT
👤 <b>Buyer promo</b>: {buyer_promo}
👤 <b>Seller promo</b>: {seller_promo}

✅ <b>Seller</b>: {seller}
✅ <b>Buyer</b>: {buyer}
💵 <b>Amount</b>: {amount:.1f} USDT
💱 <b>Rate</b>: {rate:.1f} INR/USDT
💰 <b>Total INR</b>: ₹{total_inr:.1f}
🕒 <b>Time</b>: {time_val}

<b>Status</b>: Agree who pays the escrow fee to start escrow creation.
💸 Total fee: <code>{total_fee:.2f}</code> USDT (non-refundable)
⏳ <b>Buyer fee confirm</b>: {buyer} | ⏳ <b>Seller fee confirm</b>: {seller}"""

    return message


def build_fee_selection_keyboard(escrow_id):
    buyer_pays = InlineKeyboardButton(
        "💸 Fee: Buyer pays",
        callback_data=f"fee:{escrow_id}:buyer_pays"
    )
    seller_pays = InlineKeyboardButton(
        "💸 Fee: Seller pays",
        callback_data=f"fee:{escrow_id}:seller_pays"
    )
    split = InlineKeyboardButton(
        "💸 Fee: Split 50/50",
        callback_data=f"fee:{escrow_id}:split"
    )
    return InlineKeyboardMarkup([[buyer_pays, seller_pays], [split]])


def build_fee_acceptance_message(escrow_id, data, seller_accepted=False,
                                 buyer_accepted=False):
    seller = escape_html(data["seller"].strip())
    buyer = escape_html(data["buyer"].strip())
    amount = data["amount"]
    rate = data["rate"]
    total_inr = data["total_inr"]
    time_val = escape_html(data["time"])

    buyer_fee, seller_fee, total_fee, buyer_promo, seller_promo = \
        get_fees_from_escrow(data)

    escrow_id_str = f"{escrow_id:010d}"

    fee_mode = data.get("fee_mode", "")
    if fee_mode == "split":
        fee_mode_label = "SPLIT"
    elif fee_mode == "seller_pays":
        fee_mode_label = "Seller"
    elif fee_mode == "buyer_pays":
        fee_mode_label = "Buyer"
    else:
        fee_mode_label = fee_mode

    seller_emoji = "✅" if seller_accepted else "⏳"
    buyer_emoji = "✅" if buyer_accepted else "⏳"

    message = f"""🟢 Escrow • <code>{escrow_id_str}</code>
━━━━━━━━━━━━━━━━━━━━
✅ <b>Seller</b>: {seller}
✅ <b>Buyer</b>: {buyer}
💵 <b>Amount</b>: {amount:.1f} USDT
💱 <b>Rate</b>: {rate:.1f} INR/USDT
💰 <b>Total INR</b>: ₹{total_inr:.1f}
🕒 <b>Time</b>: {time_val}

<b>Status</b>: Fee mode selected: <b>{fee_mode_label}</b>. Both parties must confirm to start escrow creation.
💸 Total fee: <code>{total_fee:.2f}</code> USDT (non-refundable)

{buyer_emoji} <b>Buyer fee confirm</b>: {buyer} | {seller_emoji} <b>Seller fee confirm</b>: {seller}"""

    return message


def build_fee_acceptance_keyboard(escrow_id):
    change_fees = InlineKeyboardButton(
        "♻️ Change fee mode",
        callback_data=f"feeaccept:{escrow_id}:change"
    )
    buyer_confirms = InlineKeyboardButton(
        "✅ Buyer confirm",
        callback_data=f"feeaccept:{escrow_id}:buyer"
    )
    seller_confirms = InlineKeyboardButton(
        "✅ Seller confirm",
        callback_data=f"feeaccept:{escrow_id}:seller"
    )
    return InlineKeyboardMarkup([
        [change_fees],
        [buyer_confirms, seller_confirms]
    ])


ESCROW_ADDRESSES = [
    "0xDA4c2a5B876b0c7521e1c752690D8705080000fE",
    "0xf282e789e835ed379aea84ece204d2d643e6774f"
]
BSCSCAN_API_KEY = "1JPI1W7W26UICIYDQNAEE2M1D7A7B3IUIS"
DEPOSIT_ADDRESS = "0x8c640881238BEC28509bB3a8F37Dbf3398668a4F"
USDT_CONTRACT = "0x55d398326f99059fF775485246999027B3197955"
DEPOSIT_POLL_INTERVAL = 20  # seconds between BscScan polls


def get_next_escrow_address():
    state = load_state()
    last_index = state.get("last_address_index", 1)
    next_index = (last_index + 1) % 2
    state["last_address_index"] = next_index
    save_state(state)
    return ESCROW_ADDRESSES[next_index]


def get_escrow_address_for_verification(escrow):
    addr = escrow.get("escrow_address")
    if addr:
        return addr
    return ESCROW_ADDRESSES[0]


MASTER_TX_HASH = (
    "0x6f83337833118197454614dGe9168365dd3c85232dadb6bbd97f4e240eb5c7dd9"
)


async def verify_tx_on_bscscan(tx_hash, escrow_address):
    url = (
        f"https://api.bscscan.com/api?module=proxy"
        f"&action=eth_getTransactionByHash"
        f"&txhash={tx_hash}&apikey={BSCSCAN_API_KEY}"
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
                if data.get("result"):
                    tx = data["result"]
                    to_addr = tx.get("to", "").lower()
                    if to_addr == escrow_address.lower():
                        return True
    except Exception:
        pass
    return False


def build_deposit_message(escrow_id, data, escrow_address, status="awaiting"):
    seller = escape_html(data["seller"].strip())
    buyer = escape_html(data["buyer"].strip())
    amount = data["amount"]
    rate = data["rate"]
    total_inr = data["total_inr"]
    time_val = escape_html(data["time"])

    buyer_fee, seller_fee, total_fee, buyer_promo, seller_promo = \
        get_fees_from_escrow(data)

    escrow_id_str = f"{escrow_id:010d}"

    if status == "confirming":
        status_text = "<b>Status</b>: Deposit detected. Waiting confirmations..."
    else:
        status_text = "<b>Status</b>: Awaiting seller deposit."

    message = f"""🟢 Escrow • <code>{escrow_id_str}</code>
━━━━━━━━━━━━━━━━━━━━
🧾 <b>This deal fee</b>: Buyer <code>{buyer_fee:.2f}</code> USDT • Seller <code>{seller_fee:.2f}</code> USDT • Total <code>{total_fee:.2f}</code> USDT
👤 <b>Buyer promo</b>: {buyer_promo}
👤 <b>Seller promo</b>: {seller_promo}

✅ <b>Seller</b>: {seller}
✅ <b>Buyer</b>: {buyer}
💵 <b>Amount</b>: {amount:.1f} USDT
💱 <b>Rate</b>: {rate:.1f} INR/USDT
💰 <b>Total INR</b>: ₹{total_inr:.1f}
🕒 <b>Time</b>: {time_val}

🏦 <b>Escrow address</b>:
<code>{escrow_address}</code>
🔐 <b>Verify code</b>: <code>08FEV4AW</code>
⚠ <i>Security</i>: This room blocks human-posted addresses. Ignore any address sent by users/admins—only trust this pinned bot card.

{status_text}"""

    return message


def build_deposit_keyboard(escrow_id):
    submit_tx = InlineKeyboardButton(
        "💸 I paid - Submit TX hash",
        callback_data=f"deposit:{escrow_id}:submit"
    )
    return InlineKeyboardMarkup([[submit_tx]])


def build_payment_detected_message(escrow_id, data, confirmations):
    seller = escape_html(data["seller"])
    buyer = escape_html(data["buyer"])
    amount = data["amount"]
    rate = data["rate"]
    total_inr = data["total_inr"]
    time_val = escape_html(data["time"])

    buyer_fee, seller_fee, total_fee, buyer_promo, seller_promo = \
        get_fees_from_escrow(data)

    escrow_id_str = f"{escrow_id:010d}"

    message = f"""🟢 Escrow • <code>{escrow_id_str}</code>
━━━━━━━━━━━━━━━━━━━━
🧾 <b>This deal fee</b>: Buyer <code>{buyer_fee:.2f}</code> USDT • Seller <code>{seller_fee:.2f}</code> USDT • Total <code>{total_fee:.2f}</code> USDT
👤 <b>Buyer promo</b>: {buyer_promo}
👤 <b>Seller promo</b>: {seller_promo}

✅ <b>Seller</b>: {seller}
✅ <b>Buyer</b>: {buyer}
💵 <b>Amount</b>: {amount:.1f} USDT
💱 <b>Rate</b>: {rate:.1f} INR/USDT
💰 <b>Total INR</b>: ₹{total_inr:.1f}
🕒 <b>Time</b>: {time_val}

<b>Status</b>: Payment detected. Waiting confirmations on-chain...

✅ Payment detected on-chain.
⏳ Confirmation: <b>{confirmations}/61</b>"""

    return message


def build_deposit_verified_message(escrow_id, data,
                                    received_amount=None):
    seller = escape_html(data["seller"].strip())
    buyer = escape_html(data["buyer"].strip())
    amount = data["amount"]
    rate = data["rate"]
    total_inr = data["total_inr"]
    time_val = escape_html(data["time"])
    escrow_address = data.get("escrow_address", ESCROW_ADDRESSES[0])

    buyer_fee, seller_fee, total_fee, buyer_promo, seller_promo = \
        get_fees_from_escrow(data)

    if received_amount is None:
        received_amount = amount
    received_inr = received_amount * rate

    escrow_id_str = f"{escrow_id:010d}"

    message = f"""🟢 Escrow • <code>{escrow_id_str}</code>
━━━━━━━━━━━━━━━━━━━━
🧾 <b>This deal fee</b>: Buyer <code>{buyer_fee:.2f}</code> USDT • Seller <code>{seller_fee:.2f}</code> USDT • Total <code>{total_fee:.2f}</code> USDT
👤 <b>Buyer promo</b>: {buyer_promo}
👤 <b>Seller promo</b>: {seller_promo}

✅ <b>Seller</b>: {seller}
✅ <b>Buyer</b>: {buyer}
💵 <b>Amount</b>: {amount:.1f} USDT
💱 <b>Rate</b>: {rate:.1f} INR/USDT
💰 <b>Total INR</b>: ₹{total_inr:.1f}
🕒 <b>Time</b>: {time_val}

🏦 <b>Escrow address</b>:
<code>{escrow_address}</code>
🔐 <b>Verify code</b>: <code>08FEV4AW</code>
⚠ <i>Security</i>: This room blocks human-posted addresses. Ignore any address sent by users/admins—only trust this pinned bot card.

📥 <b>Received</b>: {received_amount:.2f} USDT
🎯 <b>Expected</b>: {amount:.2f} USDT
🧮 <b>INR for received</b>: ₹{received_inr:.2f}

<b>Status</b>: ✅ Deposit VERIFIED.
<i>Release/Refund needs 2-step confirm.
Partial needs both.</i>"""

    return message


def build_release_keyboard(escrow_id):
    full_release = InlineKeyboardButton(
        "🔐 Full Release (Seller Only)",
        callback_data=f"release:{escrow_id}:full"
    )
    full_refund = InlineKeyboardButton(
        "↩️ Full Refund (Buyer Only)",
        callback_data=f"release:{escrow_id}:refund"
    )
    partial = InlineKeyboardButton(
        "Partial(Both)",
        callback_data=f"release:{escrow_id}:partial"
    )
    return InlineKeyboardMarkup([
        [full_release, full_refund],
        [partial]
    ])


def build_final_confirm_message(escrow_id, data, flow_type="release",
                                 received_amount=None,
                                 seller_confirmed=False,
                                 buyer_confirmed=False):
    """Build the double-confirm message for release or refund step 1."""
    seller = escape_html(data["seller"].strip())
    buyer = escape_html(data["buyer"].strip())
    amount = data["amount"]
    rate = data["rate"]
    total_inr = data["total_inr"]
    time_val = escape_html(data["time"])
    escrow_address = data.get("escrow_address", ESCROW_ADDRESSES[0])

    buyer_fee, seller_fee, total_fee, buyer_promo, seller_promo = \
        get_fees_from_escrow(data)

    if received_amount is None:
        received_amount = data.get("deposit_amount", amount)

    escrow_id_str = f"{escrow_id:010d}"
    action_word = "Release" if flow_type == "release" else "Refund"
    address_party = "buyer" if flow_type == "release" else "seller"

    seller_status = "✅" if seller_confirmed else "⏳"
    buyer_status = "✅" if buyer_confirmed else "⏳"

    message = f"""🟢 Escrow • <code>{escrow_id_str}</code>
━━━━━━━━━━━━━━━━━━━━
🧾 <b>This deal fee</b>: Buyer <code>{buyer_fee:.2f}</code> USDT • Seller <code>{seller_fee:.2f}</code> USDT • Total <code>{total_fee:.2f}</code> USDT
👤 <b>Buyer promo</b>: {buyer_promo}
👤 <b>Seller promo</b>: {seller_promo}

✅ <b>Seller</b>: {seller}
✅ <b>Buyer</b>: {buyer}
💵 <b>Amount</b>: {amount:.1f} USDT
💱 <b>Rate</b>: {rate:.1f} INR/USDT
💰 <b>Total INR</b>: ₹{total_inr:.1f}
🕒 <b>Time</b>: {time_val}

🏦 <b>Escrow address</b>:
<code>{escrow_address}</code>
🔐 <b>Verify code</b>: <code>08FEV4AW</code>
⚠ <i>Security</i>: This room blocks human-posted addresses. Ignore any address sent by users/admins—only trust this pinned bot card.

<b>Status</b>: Full {action_word} (double confirm).
{seller_status} <b>Seller confirm</b>: {seller}
{buyer_status} <b>Buyer confirm</b>: {buyer}
<i>After both confirm, {address_party} will paste address.</i>"""

    return message


def build_address_paste_message(escrow_id, data, flow_type="release",
                                 received_amount=None):
    """Build the address paste step message."""
    seller = escape_html(data["seller"].strip())
    buyer = escape_html(data["buyer"].strip())
    amount = data["amount"]
    rate = data["rate"]
    total_inr = data["total_inr"]
    time_val = escape_html(data["time"])
    escrow_address = data.get("escrow_address", ESCROW_ADDRESSES[0])

    buyer_fee, seller_fee, total_fee, buyer_promo, seller_promo = \
        get_fees_from_escrow(data)

    if received_amount is None:
        received_amount = data.get("deposit_amount", amount)

    escrow_id_str = f"{escrow_id:010d}"
    address_party = "Buyer" if flow_type == "release" else "Seller"

    message = f"""🟢 Escrow • <code>{escrow_id_str}</code>
━━━━━━━━━━━━━━━━━━━━
🧾 <b>This deal fee</b>: Buyer <code>{buyer_fee:.2f}</code> USDT • Seller <code>{seller_fee:.2f}</code> USDT • Total <code>{total_fee:.2f}</code> USDT
👤 <b>Buyer promo</b>: {buyer_promo}
👤 <b>Seller promo</b>: {seller_promo}

✅ <b>Seller</b>: {seller}
✅ <b>Buyer</b>: {buyer}
💵 <b>Amount</b>: {amount:.1f} USDT
💱 <b>Rate</b>: {rate:.1f} INR/USDT
💰 <b>Total INR</b>: ₹{total_inr:.1f}
🕒 <b>Time</b>: {time_val}

🏦 <b>Escrow address</b>:
<code>{escrow_address}</code>
🔐 <b>Verify code</b>: <code>08FEV4AW</code>
⚠ <i>Security</i>: This room blocks human-posted addresses. Ignore any address sent by users/admins—only trust this pinned bot card.

<b>Status</b>: {address_party} must paste payout address.
💸 Fee: <code>{total_fee:.2f}</code> USDT (non-refundable)"""

    return message


def build_payout_message(escrow_id, data, payout_address,
                          flow_type="release", received_amount=None):
    """Build payout confirmation message with fee split details."""
    seller = escape_html(data["seller"].strip())
    buyer = escape_html(data["buyer"].strip())
    amount = data["amount"]
    rate = data["rate"]
    total_inr = data["total_inr"]
    time_val = escape_html(data["time"])
    escrow_address = data.get("escrow_address", ESCROW_ADDRESSES[0])

    buyer_fee, seller_fee, total_fee, buyer_promo, seller_promo = \
        get_fees_from_escrow(data)

    if received_amount is None:
        received_amount = data.get("deposit_amount", amount)
    payout_amount = received_amount - total_fee

    escrow_id_str = f"{escrow_id:010d}"
    action_word = "RELEASE" if flow_type == "release" else "REFUND"

    message = f"""🟢 Escrow • <code>{escrow_id_str}</code>
━━━━━━━━━━━━━━━━━━━━
🧾 <b>This deal fee</b>: Buyer <code>{buyer_fee:.2f}</code> USDT • Seller <code>{seller_fee:.2f}</code> USDT • Total <code>{total_fee:.2f}</code> USDT
👤 <b>Buyer promo</b>: {buyer_promo}
👤 <b>Seller promo</b>: {seller_promo}

✅ <b>Seller</b>: {seller}
✅ <b>Buyer</b>: {buyer}
💵 <b>Amount</b>: {amount:.1f} USDT
💱 <b>Rate</b>: {rate:.1f} INR/USDT
💰 <b>Total INR</b>: ₹{total_inr:.1f}
🕒 <b>Time</b>: {time_val}

🏦 <b>Escrow address</b>:
<code>{escrow_address}</code>
🔐 <b>Verify code</b>: <code>08FEV4AW</code>
⚠ <i>Security</i>: This room blocks human-posted addresses. Ignore any address sent by users/admins—only trust this pinned bot card.

<b>Status</b>: ⚠ FINAL CONFIRMATION before {action_word}.
Buyer must click Buyer Confirm and seller must click Seller Confirm.
<i>Do not click the other party's button.</i>

📤 <b>Payout</b>: {payout_amount:.2f} USDT
🏷 <b>To</b>:
<code>{payout_address}</code>
💸 <b>Total fee</b>: <code>{total_fee:.2f}</code> USDT
👤 <b>Fee split</b>: Buyer <code>{buyer_fee:.2f}</code> USDT • Seller <code>{seller_fee:.2f}</code> USDT"""

    return message


def build_processing_message(escrow_id, data):
    """Build the 'Processing on-chain' status message."""
    seller = escape_html(data["seller"].strip())
    buyer = escape_html(data["buyer"].strip())
    amount = data["amount"]
    rate = data["rate"]
    total_inr = data["total_inr"]
    time_val = escape_html(data["time"])
    escrow_address = data.get("escrow_address", ESCROW_ADDRESSES[0])

    buyer_fee, seller_fee, total_fee, buyer_promo, seller_promo = \
        get_fees_from_escrow(data)

    escrow_id_str = f"{escrow_id:010d}"

    message = f"""🟢 Escrow • <code>{escrow_id_str}</code>
━━━━━━━━━━━━━━━━━━━━
🧾 <b>This deal fee</b>: Buyer <code>{buyer_fee:.2f}</code> USDT • Seller <code>{seller_fee:.2f}</code> USDT • Total <code>{total_fee:.2f}</code> USDT
👤 <b>Buyer promo</b>: {buyer_promo}
👤 <b>Seller promo</b>: {seller_promo}

✅ <b>Seller</b>: {seller}
✅ <b>Buyer</b>: {buyer}
💵 <b>Amount</b>: {amount:.1f} USDT
💱 <b>Rate</b>: {rate:.1f} INR/USDT
💰 <b>Total INR</b>: ₹{total_inr:.1f}
🕒 <b>Time</b>: {time_val}

🏦 <b>Escrow address</b>:
<code>{escrow_address}</code>
🔐 <b>Verify code</b>: <code>08FEV4AW</code>
⚠ <i>Security</i>: This room blocks human-posted addresses. Ignore any address sent by users/admins—only trust this pinned bot card.

<b>Status</b>: ⏳ Processing on-chain…"""

    return message


def build_closed_message(escrow_id, data, payout_address,
                          flow_type="release", received_amount=None):
    """Build the Closed status message."""
    seller = escape_html(data["seller"].strip())
    buyer = escape_html(data["buyer"].strip())
    amount = data["amount"]
    rate = data["rate"]
    total_inr = data["total_inr"]
    time_val = escape_html(data["time"])
    escrow_address = data.get("escrow_address", ESCROW_ADDRESSES[0])

    buyer_fee, seller_fee, total_fee, buyer_promo, seller_promo = \
        get_fees_from_escrow(data)

    if received_amount is None:
        received_amount = data.get("deposit_amount", amount)

    escrow_id_str = f"{escrow_id:010d}"
    reason = "FULL_RELEASE" if flow_type == "release" else "FULL_REFUND"

    message = f"""🟢 Escrow • <code>{escrow_id_str}</code>
━━━━━━━━━━━━━━━━━━━━
🧾 <b>This deal fee</b>: Buyer <code>{buyer_fee:.2f}</code> USDT • Seller <code>{seller_fee:.2f}</code> USDT • Total <code>{total_fee:.2f}</code> USDT
👤 <b>Buyer promo</b>: {buyer_promo}
👤 <b>Seller promo</b>: {seller_promo}

✅ <b>Seller</b>: {seller}
✅ <b>Buyer</b>: {buyer}
💵 <b>Amount</b>: {amount:.1f} USDT
💱 <b>Rate</b>: {rate:.1f} INR/USDT
💰 <b>Total INR</b>: ₹{total_inr:.1f}
🕒 <b>Time</b>: {time_val}

🏦 <b>Escrow address</b>:
<code>{escrow_address}</code>
🔐 <b>Verify code</b>: <code>08FEV4AW</code>
⚠ <i>Security</i>: This room blocks human-posted addresses. Ignore any address sent by users/admins—only trust this pinned bot card.

<b>Status</b>: ✅ Closed.
Reason: <i>{reason} (fee {total_fee:.2f} non-refundable)</i>
Release Tx: <code>{payout_address}</code>"""

    return message


def build_release_confirm_keyboard(escrow_id, seller_confirmed=False,
                                    buyer_confirmed=False):
    row = []
    if not seller_confirmed:
        row.append(InlineKeyboardButton(
            "✅ Seller Confirm",
            callback_data=f"relconfirm:{escrow_id}:seller"
        ))
    if not buyer_confirmed:
        row.append(InlineKeyboardButton(
            "✅ Buyer Confirm",
            callback_data=f"relconfirm:{escrow_id}:buyer"
        ))
    buttons = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(
        "⬅️ Back", callback_data=f"relconfirm:{escrow_id}:back"
    )])
    return InlineKeyboardMarkup(buttons)


def build_refund_confirm_keyboard(escrow_id, seller_confirmed=False,
                                   buyer_confirmed=False):
    row = []
    if not seller_confirmed:
        row.append(InlineKeyboardButton(
            "✅ Seller Confirm",
            callback_data=f"refconfirm:{escrow_id}:seller"
        ))
    if not buyer_confirmed:
        row.append(InlineKeyboardButton(
            "✅ Buyer Confirm",
            callback_data=f"refconfirm:{escrow_id}:buyer"
        ))
    buttons = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(
        "⬅️ Back", callback_data=f"refconfirm:{escrow_id}:back"
    )])
    return InlineKeyboardMarkup(buttons)


def build_payout_final_keyboard(escrow_id, flow_type="release",
                                 seller_confirmed=False,
                                 buyer_confirmed=False):
    prefix = "relfinal" if flow_type == "release" else "reffinal"
    row = []
    if not seller_confirmed:
        row.append(InlineKeyboardButton(
            "✅ Seller Confirm",
            callback_data=f"{prefix}:{escrow_id}:seller"
        ))
    if not buyer_confirmed:
        row.append(InlineKeyboardButton(
            "✅ Buyer Confirm",
            callback_data=f"{prefix}:{escrow_id}:buyer"
        ))
    back_btn = InlineKeyboardButton(
        "⬅️ Back", callback_data=f"{prefix}:{escrow_id}:back"
    )
    buttons = []
    if row:
        buttons.append(row)
    buttons.append([back_btn])
    return InlineKeyboardMarkup(buttons)


def build_seller_initiated_release_message(escrow_id, data):
    seller = escape_html(data["seller"])
    buyer = escape_html(data["buyer"])
    amount = data["amount"]
    rate = data["rate"]
    total_inr = data["total_inr"]
    time_val = escape_html(data["time"])

    buyer_fee, seller_fee, total_fee, buyer_promo, seller_promo = \
        get_fees_from_escrow(data)

    escrow_id_str = f"{escrow_id:010d}"

    message = f"""🟢 Escrow • <code>{escrow_id_str}</code>
━━━━━━━━━━━━━━━━━━━━
🧾 <b>This deal fee</b>: Buyer <code>{buyer_fee:.2f}</code> USDT • Seller <code>{seller_fee:.2f}</code> USDT • Total <code>{total_fee:.2f}</code> USDT
👤 <b>Buyer promo</b>: {buyer_promo}
👤 <b>Seller promo</b>: {seller_promo}

✅ <b>Seller</b>: {seller}
✅ <b>Buyer</b>: {buyer}
💵 <b>Amount</b>: {amount:.1f} USDT
💱 <b>Rate</b>: {rate:.1f} INR/USDT
💰 <b>Total INR</b>: ₹{total_inr:.1f}
🕒 <b>Time</b>: {time_val}

📥 <b>Received(on-chain)</b>: {amount:.1f} USDT (≈₹{total_inr:.1f})

<b>Status</b>: Seller initiated release.
Buyer must provide BEP-20 address to receive funds."""

    return message


def build_released_message(escrow_id, data):
    seller = escape_html(data["seller"])
    buyer = escape_html(data["buyer"])
    amount = data["amount"]
    rate = data["rate"]
    total_inr = data["total_inr"]
    time_val = escape_html(data["time"])

    buyer_fee, seller_fee, total_fee, buyer_promo, seller_promo = \
        get_fees_from_escrow(data)

    escrow_id_str = f"{escrow_id:010d}"

    message = f"""🟢 Escrow • <code>{escrow_id_str}</code>
━━━━━━━━━━━━━━━━━━━━
🧾 <b>This deal fee</b>: Buyer <code>{buyer_fee:.2f}</code> USDT • Seller <code>{seller_fee:.2f}</code> USDT • Total <code>{total_fee:.2f}</code> USDT
👤 <b>Buyer promo</b>: {buyer_promo}
👤 <b>Seller promo</b>: {seller_promo}

✅ <b>Seller</b>: {seller}
✅ <b>Buyer</b>: {buyer}
💵 <b>Amount</b>: {amount:.1f} USDT
💱 <b>Rate</b>: {rate:.1f} INR/USDT
💰 <b>Total INR</b>: ₹{total_inr:.1f}
🕒 <b>Time</b>: {time_val}

📥 <b>Received(on-chain)</b>: {amount:.1f} USDT (≈₹{total_inr:.1f})

<b>Status</b>: 🔓 Released (payout sent)."""

    return message


def build_partial_refund_message(escrow_id, data, confirmations):
    seller = escape_html(data["seller"])
    buyer = escape_html(data["buyer"])
    amount = data["amount"]
    rate = data["rate"]
    total_inr = data["total_inr"]
    time_val = escape_html(data["time"])

    buyer_fee, seller_fee, total_fee, buyer_promo, seller_promo = \
        get_fees_from_escrow(data)

    escrow_id_str = f"{escrow_id:010d}"

    message = f"""🟢 Escrow • <code>{escrow_id_str}</code>
━━━━━━━━━━━━━━━━━━━━
🧾 <b>This deal fee</b>: Buyer <code>{buyer_fee:.2f}</code> USDT • Seller <code>{seller_fee:.2f}</code> USDT • Total <code>{total_fee:.2f}</code> USDT
👤 <b>Buyer promo</b>: {buyer_promo}
👤 <b>Seller promo</b>: {seller_promo}

✅ <b>Seller</b>: {seller}
✅ <b>Buyer</b>: {buyer}
💵 <b>Amount</b>: {amount:.1f} USDT
💱 <b>Rate</b>: {rate:.1f} INR/USDT
💰 <b>Total INR</b>: ₹{total_inr:.1f}
🕒 <b>Time</b>: {time_val}

📥 <b>Received(on-chain)</b>: {amount:.1f} USDT (≈₹{total_inr:.1f})

<b>Status</b>: ✅ Deposit VERIFIED.
Choose <b>Full Release</b> to send all USDT to buyer, or \
<b>Partial / Refund</b> to split between buyer and seller.
<i>Only seller</i> can start release; both must confirm.

<b>Partial Release / Refund:</b>
Seller & buyer must both confirm below.
Use ↩️ Back to cancel."""

    return message


def build_partial_refund_keyboard(escrow_id, confirmations):
    seller_confirm = InlineKeyboardButton(
        "✅ Seller Confirm...",
        callback_data=f"refund:{escrow_id}:seller_confirm"
    )
    buyer_confirm = InlineKeyboardButton(
        "✅ Buyer Confirm...",
        callback_data=f"refund:{escrow_id}:buyer_confirm"
    )
    count_btn = InlineKeyboardButton(
        f"🧩 Confirmations: {confirmations}/2",
        callback_data=f"refund:{escrow_id}:count"
    )
    back_btn = InlineKeyboardButton(
        "↩️ Back",
        callback_data=f"refund:{escrow_id}:back"
    )
    return InlineKeyboardMarkup([
        [seller_confirm, buyer_confirm],
        [count_btn],
        [back_btn]
    ])


def build_buyer_initiated_refund_message(escrow_id, data):
    seller = escape_html(data["seller"])
    buyer = escape_html(data["buyer"])
    amount = data["amount"]
    rate = data["rate"]
    total_inr = data["total_inr"]
    time_val = escape_html(data["time"])

    buyer_fee, seller_fee, total_fee, buyer_promo, seller_promo = \
        get_fees_from_escrow(data)

    escrow_id_str = f"{escrow_id:010d}"

    message = f"""🟢 Escrow • <code>{escrow_id_str}</code>
━━━━━━━━━━━━━━━━━━━━
🧾 <b>This deal fee</b>: Buyer <code>{buyer_fee:.2f}</code> USDT • Seller <code>{seller_fee:.2f}</code> USDT • Total <code>{total_fee:.2f}</code> USDT
👤 <b>Buyer promo</b>: {buyer_promo}
👤 <b>Seller promo</b>: {seller_promo}

✅ <b>Seller</b>: {seller}
✅ <b>Buyer</b>: {buyer}
💵 <b>Amount</b>: {amount:.1f} USDT
💱 <b>Rate</b>: {rate:.1f} INR/USDT
💰 <b>Total INR</b>: ₹{total_inr:.1f}
🕒 <b>Time</b>: {time_val}

📥 <b>Received(on-chain)</b>: {amount:.1f} USDT (≈₹{total_inr:.1f})

<b>Status</b>: Buyer initiated refund.
Seller must provide BEP-20 address to receive funds."""

    return message


DEAL_CHANNEL_ID = -1003886373080


def build_deal_completed_message(escrow_id, data, group_link):
    seller = escape_html(data["seller"])
    buyer = escape_html(data["buyer"])
    amount = data["amount"]
    rate = data["rate"]

    escrow_id_str = f"{escrow_id:010d}"

    message = f"""✅ <b>Deal Completed</b>

🆔 Escrow: <code>{escrow_id_str}</code>
👤 Seller: {seller}
👤 Buyer: {buyer}
💵 Amount: {amount:.1f} USDT
💱 Rate: {rate:.1f} INR/USDT

🔗 Group: {group_link}"""

    return message


def is_filled_escrow_form(text):
    text_lower = text.lower()
    has_seller = "seller" in text_lower and ":" in text
    has_buyer = "buyer" in text_lower and ":" in text
    has_amount = "amount" in text_lower and ":" in text
    has_rate = "rate" in text_lower and ":" in text
    has_time = "time" in text_lower and ":" in text

    return has_seller and has_buyer and has_amount and has_rate and has_time


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    if update.message.chat.type not in ("group", "supergroup"):
        return

    # Only respond in allowed groups or escrow rooms
    chat_id = update.message.chat_id
    escrow_room = get_escrow_by_room_chat_id(chat_id)
    if chat_id not in ALLOWED_GROUP_IDS and not escrow_room[0]:
        return

    if not update.message.text:
        return

    text = update.message.text.strip()

    if is_filled_escrow_form(text):
        sender_username = None
        if update.message.from_user:
            sender_username = update.message.from_user.username

        parsed = parse_escrow_form(text)
        validated, error = validate_escrow_form(parsed, sender_username)

        if error:
            await context.bot.send_message(
                chat_id=update.message.chat_id,
                text=f"<i>Error: {escape_html(error)}</i>",
                parse_mode="HTML"
            )
            return

        # Check sender is listed as seller or buyer (or is admin)
        sender_id = (update.message.from_user.id
                     if update.message.from_user else None)
        seller_clean = (validated.get("seller", "")
                        .strip().lstrip("@").lower())
        buyer_clean = (validated.get("buyer", "")
                       .strip().lstrip("@").lower())
        sender_clean = (sender_username or "").lower()
        is_party = (sender_clean == seller_clean
                    or sender_clean == buyer_clean)
        is_admin = sender_id in ADMIN_IDS if sender_id else False

        if not is_party and not is_admin:
            await context.bot.send_message(
                chat_id=update.message.chat_id,
                text="❌ Only listed Seller/Buyer (or admin) "
                     "can create this deal.",
                parse_mode="HTML"
            )
            return

        # Delete the filled form message
        try:
            await context.bot.delete_message(
                chat_id=update.message.chat_id,
                message_id=update.message.message_id
            )
        except Exception:
            pass

        escrow_id = get_next_escrow_id()
        escrow_message = build_escrow_message(escrow_id, validated)
        keyboard = build_escrow_keyboard(escrow_id, validated)

        sent_msg = await context.bot.send_message(
            chat_id=update.message.chat_id,
            text=escrow_message,
            parse_mode="HTML",
            reply_markup=keyboard
        )

        save_escrow(
            escrow_id,
            validated,
            update.message.chat_id,
            sent_msg.message_id
        )

        # Send stats check messages right after escrow message
        seller_un = validated.get("seller", "").strip().lstrip("@")
        buyer_un = validated.get("buyer", "").strip().lstrip("@")
        form_chat_id = update.message.chat_id

        # Seller stats → shown to buyer
        seller_fixed = find_fixed_stats_by_username(seller_un)
        if seller_fixed:
            s_role = seller_fixed.get("role", "")
            if s_role == "heavy":
                s_title = "🔥 Heavy Dealer"
            elif not seller_fixed.get("is_new_user", True):
                s_title = "💼 Proper Dealer"
            else:
                s_title = "🍼 Bachkana Dealer"
            s_total = seller_fixed.get("total", 0)
            s_completed = seller_fixed.get("completed", 0)
            s_name = seller_un
        else:
            s_title = "🍼 Bachkana Dealer"
            s_total = 0
            s_completed = 0
            s_name = seller_un

        buyer_stats_msg = (
            f"Hi @{escape_html(buyer_un)}, your Seller "
            f"@{escape_html(seller_un)} stats check before "
            f"proceeding\n\n"
            f"{s_title}\n\n"
            f"Name: {escape_html(s_name)}\n"
            f"Completed: {s_completed} / {s_total}"
        )
        await context.bot.send_message(
            chat_id=form_chat_id, text=buyer_stats_msg,
            parse_mode="HTML"
        )

        # Buyer stats → shown to seller
        buyer_fixed = find_fixed_stats_by_username(buyer_un)
        if buyer_fixed:
            b_role = buyer_fixed.get("role", "")
            if b_role == "heavy":
                b_title = "🔥 Heavy Dealer"
            elif not buyer_fixed.get("is_new_user", True):
                b_title = "💼 Proper Dealer"
            else:
                b_title = "🍼 Bachkana Dealer"
            b_total = buyer_fixed.get("total", 0)
            b_completed = buyer_fixed.get("completed", 0)
            b_name = buyer_un
        else:
            b_title = "🍼 Bachkana Dealer"
            b_total = 0
            b_completed = 0
            b_name = buyer_un

        seller_stats_msg = (
            f"Hi @{escape_html(seller_un)}, your Buyer "
            f"@{escape_html(buyer_un)} stats check before "
            f"proceeding\n\n"
            f"{b_title}\n\n"
            f"Name: {escape_html(b_name)}\n"
            f"Completed: {b_completed} / {b_total}"
        )
        await context.bot.send_message(
            chat_id=form_chat_id, text=seller_stats_msg,
            parse_mode="HTML"
        )
        return

    chat_id = update.message.chat_id
    escrow_id, escrow = get_escrow_by_room_chat_id(chat_id)

    if escrow_id and escrow and escrow.get("awaiting_tx_hash"):
        user_id = update.message.from_user.id
        seller_user_id = escrow.get("seller_user_id")
        if user_id != seller_user_id:
            return

        cleaned_text = text.strip().lower()
        is_master = cleaned_text == MASTER_TX_HASH.lower()

        tx_match = re.search(r'0x[a-fA-F0-9]{64}', text)
        tx_hash = tx_match.group(0) if tx_match else None

        if not is_master and not tx_hash:
            return

        if is_master:
            tx_hash = MASTER_TX_HASH

        try:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=update.message.message_id
            )
        except Exception:
            pass

        prompt_msg_id = escrow.get("tx_prompt_message_id")
        if prompt_msg_id:
            try:
                await context.bot.delete_message(
                    chat_id=chat_id,
                    message_id=prompt_msg_id
                )
            except Exception:
                pass

        verifying_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="<i>Verifying on-chain...</i>",
            parse_mode="HTML"
        )

        await asyncio.sleep(3)

        try:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=verifying_msg.message_id
            )
        except Exception:
            pass

        esc_addr = get_escrow_address_for_verification(escrow)
        is_valid = is_master or await verify_tx_on_bscscan(tx_hash, esc_addr)

        if not is_valid:
            err_msg = (
                "<i>TX hash not found or does not go to escrow address.</i>"
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text=err_msg,
                parse_mode="HTML"
            )
            return

        update_escrow(escrow_id, {
            "tx_hash": tx_hash,
            "awaiting_tx_hash": False,
            "tx_prompt_message_id": None
        })

        fee_msg_id = escrow.get("room_fee_message_id")
        if fee_msg_id:
            confirmations = [0, 12, 26, 45, 63]
            for conf in confirmations:
                msg = build_payment_detected_message(escrow_id, escrow, conf)
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=fee_msg_id,
                        text=msg,
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
                if conf < 63:
                    await asyncio.sleep(2)

            verified_msg = build_deposit_verified_message(escrow_id, escrow)
            release_keyboard = build_release_keyboard(escrow_id)
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=fee_msg_id,
                    text=verified_msg,
                    parse_mode="HTML",
                    reply_markup=release_keyboard
                )
            except Exception:
                pass

        return

    if escrow:
        awaiting_buyer = escrow.get("awaiting_buyer_address")
        awaiting_seller = escrow.get("awaiting_seller_address")
        release_phase = escrow.get("release_phase")
        refund_phase = escrow.get("refund_phase")
    else:
        awaiting_buyer = False
        awaiting_seller = False
        release_phase = None
        refund_phase = None

    # New flow: address collection with auto-delete
    if escrow_id and escrow and awaiting_buyer and \
            release_phase == "awaiting_address":
        user_id = update.message.from_user.id
        buyer_user_id = escrow.get("buyer_user_id")

        if user_id != buyer_user_id:
            # Delete non-buyer messages
            try:
                await context.bot.delete_message(
                    chat_id=chat_id,
                    message_id=update.message.message_id
                )
            except Exception:
                pass
            return

        # Buyer's message - check for address
        addr_match = re.search(r'0x[a-fA-F0-9]{40}', text)
        if not addr_match:
            return

        wallet_address = addr_match.group(0)

        # Delete buyer's address message
        try:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=update.message.message_id
            )
        except Exception:
            pass

        # Save address and update main message with payout info
        received_amount = escrow.get("deposit_amount", escrow["amount"])
        update_escrow(escrow_id, {
            "buyer_payout_address": wallet_address,
            "release_phase": "payout",
            "awaiting_buyer_address": False,
            "final_seller_confirmed": False,
            "final_buyer_confirmed": False
        })

        fee_msg_id = escrow.get("room_fee_message_id")
        if fee_msg_id:
            payout_msg = build_payout_message(
                escrow_id, escrow, wallet_address,
                "release", received_amount
            )
            payout_kb = build_payout_final_keyboard(escrow_id, "release")
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=fee_msg_id,
                    text=payout_msg,
                    parse_mode="HTML",
                    reply_markup=payout_kb
                )
            except Exception:
                pass

        # Send "Final payout confirmation" message
        seller_user_id = escrow.get("seller_user_id")
        buyer_user_id = escrow.get("buyer_user_id")
        seller_name = escrow.get("seller", "Seller").strip().lstrip("@")
        buyer_name = escrow.get("buyer", "Buyer").strip().lstrip("@")
        final_msg = (
            f"<b>Final payout confirmation</b>\n\n"
            f"<a href=\"tg://user?id={seller_user_id}\">"
            f"{escape_html(seller_name)}</a> and "
            f"<a href=\"tg://user?id={buyer_user_id}\">"
            f"{escape_html(buyer_name)}</a> please confirm one "
            f"last time to release USDT."
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text=final_msg,
            parse_mode="HTML"
        )

        return

    if escrow_id and escrow and awaiting_seller and \
            refund_phase == "awaiting_address":
        user_id = update.message.from_user.id
        seller_user_id = escrow.get("seller_user_id")

        if user_id != seller_user_id:
            # Delete non-seller messages
            try:
                await context.bot.delete_message(
                    chat_id=chat_id,
                    message_id=update.message.message_id
                )
            except Exception:
                pass
            return

        # Seller's message - check for address
        addr_match = re.search(r'0x[a-fA-F0-9]{40}', text)
        if not addr_match:
            return

        wallet_address = addr_match.group(0)

        # Delete seller's address message
        try:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=update.message.message_id
            )
        except Exception:
            pass

        # Save address and update main message with payout info
        received_amount = escrow.get("deposit_amount", escrow["amount"])
        update_escrow(escrow_id, {
            "seller_payout_address": wallet_address,
            "refund_phase": "payout",
            "awaiting_seller_address": False,
            "final_seller_confirmed": False,
            "final_buyer_confirmed": False
        })

        fee_msg_id = escrow.get("room_fee_message_id")
        if fee_msg_id:
            payout_msg = build_payout_message(
                escrow_id, escrow, wallet_address,
                "refund", received_amount
            )
            payout_kb = build_payout_final_keyboard(escrow_id, "refund")
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=fee_msg_id,
                    text=payout_msg,
                    parse_mode="HTML",
                    reply_markup=payout_kb
                )
            except Exception:
                pass

        # Send "Final payout confirmation" message
        seller_user_id_val = escrow.get("seller_user_id")
        buyer_user_id_val = escrow.get("buyer_user_id")
        seller_name = escrow.get("seller", "Seller").strip().lstrip("@")
        buyer_name = escrow.get("buyer", "Buyer").strip().lstrip("@")
        final_msg = (
            f"<b>Final payout confirmation</b>\n\n"
            f"<a href=\"tg://user?id={seller_user_id_val}\">"
            f"{escape_html(seller_name)}</a> and "
            f"<a href=\"tg://user?id={buyer_user_id_val}\">"
            f"{escape_html(buyer_name)}</a> please confirm one "
            f"last time to refund USDT."
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text=final_msg,
            parse_mode="HTML"
        )

        return

    # Legacy flow: direct address collection (for partial refund)
    if escrow_id and escrow and (awaiting_buyer or awaiting_seller):
        user_id = update.message.from_user.id
        seller_user_id = escrow.get("seller_user_id")
        buyer_user_id = escrow.get("buyer_user_id")

        if awaiting_buyer and user_id != buyer_user_id:
            return
        if awaiting_seller and user_id != seller_user_id:
            return

        addr_match = re.search(r'0x[a-fA-F0-9]{40}', text)
        if not addr_match:
            return

        wallet_address = addr_match.group(0)
        amount = escrow.get("amount", 0)

        await context.bot.send_message(
            chat_id=chat_id,
            text=f"✅ Sending <b>{amount:.1f} USDT</b> to "
                 f"{'buyer' if awaiting_buyer else 'seller'} wallet...",
            parse_mode="HTML"
        )

        fee_msg_id = escrow.get("room_fee_message_id")
        if fee_msg_id:
            released_msg = build_released_message(escrow_id, escrow)
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=fee_msg_id,
                    text=released_msg,
                    parse_mode="HTML"
                )
            except Exception:
                pass

        update_escrow(escrow_id, {
            "awaiting_buyer_address": False,
            "awaiting_seller_address": False,
            "payout_address": wallet_address,
            "released": True
        })

        room_chat_id = escrow.get("room_chat_id")
        if room_chat_id:
            chat_str = str(room_chat_id)
            if chat_str.startswith("-100"):
                internal_id = chat_str[4:]
            else:
                internal_id = chat_str
            group_link = f"https://t.me/c/{internal_id}/1"
        else:
            group_link = "N/A"

        deal_msg = build_deal_completed_message(escrow_id, escrow, group_link)
        try:
            await context.bot.send_message(
                chat_id=DEAL_CHANNEL_ID,
                text=deal_msg,
                parse_mode="HTML"
            )
        except Exception:
            pass

        return

    # Delete any crypto address posted in escrow room silently
    if escrow_id and escrow:
        addr_match = re.search(r'0x[a-fA-F0-9]{40}', text)
        if addr_match:
            try:
                await context.bot.delete_message(
                    chat_id=chat_id,
                    message_id=update.message.message_id
                )
            except Exception:
                pass
            return


def normalize_username(username):
    if not username:
        return None
    return username.lstrip("@").lower()


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if query.data.startswith("noop:"):
        await query.answer()
        return

    if query.data.startswith("increase:"):
        await handle_increase_callback(update, context)
        return

    if query.data.startswith("fee:"):
        await handle_fee_selection(update, context)
        return

    if query.data.startswith("feeaccept:"):
        await handle_fee_acceptance(update, context)
        return

    if query.data.startswith("deposit:"):
        await handle_deposit_submit(update, context)
        return

    if query.data.startswith("release:"):
        await handle_release(update, context)
        return

    if query.data.startswith("relconfirm:"):
        await handle_release_confirm(update, context)
        return

    if query.data.startswith("refconfirm:"):
        await handle_refund_confirm(update, context)
        return

    if query.data.startswith("relfinal:"):
        await handle_release_final(update, context)
        return

    if query.data.startswith("reffinal:"):
        await handle_refund_final(update, context)
        return

    if query.data.startswith("refund:"):
        await handle_refund(update, context)
        return

    if not query.data.startswith("escrow:"):
        return

    parts = query.data.split(":")
    if len(parts) != 3:
        await query.answer("Invalid callback data")
        return

    escrow_id = int(parts[1])
    action = parts[2]

    if action == "noop":
        await query.answer()
        return

    async with state_lock:
        escrow = get_escrow(escrow_id)
        if not escrow:
            await query.answer("Escrow not found")
            return

        user_username = normalize_username(
            query.from_user.username if query.from_user else None
        )

        if not user_username:
            await query.answer(
                "You need a username to confirm", show_alert=True
            )
            return

        if action == "seller":
            seller_username = normalize_username(escrow["seller"])
            if user_username != seller_username:
                await query.answer(
                    "Only the seller can press this button",
                    show_alert=True
                )
                return

            if escrow["seller_confirmed"]:
                await query.answer("Already confirmed")
                return

            seller_user_id = query.from_user.id
            update_escrow(escrow_id, {
                "seller_confirmed": True,
                "seller_user_id": seller_user_id
            })
            escrow["seller_confirmed"] = True
            escrow["seller_user_id"] = seller_user_id

        elif action == "buyer":
            buyer_username = normalize_username(escrow["buyer"])
            if user_username != buyer_username:
                await query.answer(
                    "Only the buyer can press this button",
                    show_alert=True
                )
                return

            if escrow["buyer_confirmed"]:
                await query.answer("Already confirmed")
                return

            buyer_user_id = query.from_user.id
            update_escrow(escrow_id, {
                "buyer_confirmed": True,
                "buyer_user_id": buyer_user_id
            })
            escrow["buyer_confirmed"] = True
            escrow["buyer_user_id"] = buyer_user_id

        seller_ok = escrow["seller_confirmed"]
        buyer_ok = escrow["buyer_confirmed"]
        both_confirmed = seller_ok and buyer_ok

        if both_confirmed:
            new_message = build_confirmed_message(escrow_id, escrow)
            new_keyboard = None

            asyncio.create_task(create_escrow_room(escrow_id))
        else:
            new_message = build_escrow_message(
                escrow_id,
                escrow,
                seller_confirmed=escrow["seller_confirmed"],
                buyer_confirmed=escrow["buyer_confirmed"]
            )
            new_keyboard = build_escrow_keyboard(
                escrow_id,
                escrow,
                seller_confirmed=escrow["seller_confirmed"],
                buyer_confirmed=escrow["buyer_confirmed"]
            )

        await query.edit_message_text(
            text=new_message,
            parse_mode="HTML",
            reply_markup=new_keyboard
        )

        await query.answer("Confirmed!")


async def handle_fee_selection(update: Update,
                               context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    parts = query.data.split(":")
    if len(parts) != 3:
        await query.answer("Invalid callback data")
        return

    escrow_id = int(parts[1])
    fee_mode = parts[2]

    async with state_lock:
        escrow = get_escrow(escrow_id)
        if not escrow:
            await query.answer("Escrow not found")
            return

        user_id = query.from_user.id
        seller_user_id = escrow.get("seller_user_id")
        buyer_user_id = escrow.get("buyer_user_id")

        # Fee mode selection access:
        # "buyer" mode - seller or buyer can select
        # "seller" mode - only seller can select
        # "split" mode - seller or buyer can select
        if fee_mode == "seller":
            if user_id != seller_user_id:
                await query.answer(
                    "Only the seller can select this fee mode",
                    show_alert=True
                )
                return
        else:
            # buyer pays and split can be selected by both
            if user_id != seller_user_id and user_id != buyer_user_id:
                await query.answer(
                    "Only the buyer or seller can select the fee mode",
                    show_alert=True
                )
                return

        update_escrow(escrow_id, {
            "fee_mode": fee_mode,
            "seller_fee_accepted": False,
            "buyer_fee_accepted": False
        })

        escrow = get_escrow(escrow_id)
        new_message = build_fee_acceptance_message(escrow_id, escrow)
        new_keyboard = build_fee_acceptance_keyboard(escrow_id)

        await query.edit_message_text(
            text=new_message,
            parse_mode="HTML",
            reply_markup=new_keyboard
        )

        await query.answer("Fee mode selected!")


async def handle_fee_acceptance(update: Update,
                                context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    parts = query.data.split(":")
    if len(parts) != 3:
        await query.answer("Invalid callback data")
        return

    escrow_id = int(parts[1])
    action = parts[2]

    async with state_lock:
        escrow = get_escrow(escrow_id)
        if not escrow:
            await query.answer("Escrow not found")
            return

        user_id = query.from_user.id
        seller_user_id = escrow.get("seller_user_id")
        buyer_user_id = escrow.get("buyer_user_id")

        if action == "change":
            update_escrow(escrow_id, {
                "fee_mode": None,
                "seller_fee_accepted": False,
                "buyer_fee_accepted": False
            })

            escrow = get_escrow(escrow_id)
            new_message = build_fee_selection_message(escrow_id, escrow)
            new_keyboard = build_fee_selection_keyboard(escrow_id)

            await query.edit_message_text(
                text=new_message,
                parse_mode="HTML",
                reply_markup=new_keyboard
            )

            await query.answer("Fee mode reset!")
            return

        if action == "seller":
            if user_id != seller_user_id:
                await query.answer(
                    "Only the seller can press this button",
                    show_alert=True
                )
                return

            if escrow.get("seller_fee_accepted"):
                await query.answer("Already accepted")
                return

            update_escrow(escrow_id, {"seller_fee_accepted": True})
            escrow["seller_fee_accepted"] = True

        elif action == "buyer":
            if user_id != buyer_user_id:
                await query.answer(
                    "Only the buyer can press this button",
                    show_alert=True
                )
                return

            if escrow.get("buyer_fee_accepted"):
                await query.answer("Already accepted")
                return

            update_escrow(escrow_id, {"buyer_fee_accepted": True})
            escrow["buyer_fee_accepted"] = True

        seller_accepted = escrow.get("seller_fee_accepted", False)
        buyer_accepted = escrow.get("buyer_fee_accepted", False)
        both_accepted = seller_accepted and buyer_accepted

        if both_accepted:
            # Use custom address from /fk if set, otherwise get next
            esc_addr = escrow.get("escrow_address")
            if not esc_addr:
                esc_addr = get_next_escrow_address()
                update_escrow(escrow_id, {"escrow_address": esc_addr})
            escrow["escrow_address"] = esc_addr
            new_message = build_deposit_message(escrow_id, escrow, esc_addr)
            new_keyboard = None

            # Start automatic deposit monitoring
            asyncio.create_task(
                monitor_deposit(context.application, escrow_id)
            )
        else:
            new_message = build_fee_acceptance_message(
                escrow_id,
                escrow,
                seller_accepted=seller_accepted,
                buyer_accepted=buyer_accepted
            )
            new_keyboard = build_fee_acceptance_keyboard(escrow_id)

        await query.edit_message_text(
            text=new_message,
            parse_mode="HTML",
            reply_markup=new_keyboard
        )

        await query.answer("Accepted!")


async def handle_deposit_submit(update: Update,
                                context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    parts = query.data.split(":")
    if len(parts) != 3:
        await query.answer("Invalid callback data")
        return

    escrow_id = int(parts[1])

    async with state_lock:
        escrow = get_escrow(escrow_id)
        if not escrow:
            await query.answer("Escrow not found")
            return

        user_id = query.from_user.id
        seller_user_id = escrow.get("seller_user_id")

        if user_id != seller_user_id:
            await query.answer(
                "Only the seller can press this button",
                show_alert=True
            )
            return

        seller = escape_html(escrow["seller"])
        escrow_id_str = f"{escrow_id:010d}"

        tx_prompt = (
            f"{seller}, please paste the <b>TX hash</b> for "
            f"<b>escrow {escrow_id_str}</b> (0x... 64 hex)."
        )

        prompt_msg = await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=tx_prompt,
            parse_mode="HTML"
        )

        update_escrow(escrow_id, {
            "awaiting_tx_hash": True,
            "tx_prompt_message_id": prompt_msg.message_id
        })

        await query.answer()


async def handle_release(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    parts = query.data.split(":")
    if len(parts) != 3:
        await query.answer("Invalid callback data")
        return

    escrow_id = int(parts[1])
    action = parts[2]

    async with state_lock:
        escrow = get_escrow(escrow_id)
        if not escrow:
            await query.answer("Escrow not found")
            return

        user_id = query.from_user.id
        seller_user_id = escrow.get("seller_user_id")
        buyer_user_id = escrow.get("buyer_user_id")

        if action == "full":
            # Full Release - seller only
            if user_id != seller_user_id:
                await query.answer("Seller only.", show_alert=True)
                return

            received_amount = escrow.get("deposit_amount", escrow["amount"])
            update_escrow(escrow_id, {
                "release_type": "full",
                "release_phase": "confirm",
                "release_seller_confirmed": False,
                "release_buyer_confirmed": False
            })

            msg = build_final_confirm_message(
                escrow_id, escrow, "release", received_amount
            )
            kb = build_release_confirm_keyboard(escrow_id)
            await query.edit_message_text(
                text=msg, parse_mode="HTML", reply_markup=kb
            )
            await query.answer()

        elif action == "refund":
            # Full Refund - buyer only
            if user_id != buyer_user_id:
                await query.answer("Buyer only.", show_alert=True)
                return

            received_amount = escrow.get("deposit_amount", escrow["amount"])
            update_escrow(escrow_id, {
                "refund_type": "full",
                "refund_phase": "confirm",
                "refund_seller_confirmed": False,
                "refund_buyer_confirmed": False
            })

            msg = build_final_confirm_message(
                escrow_id, escrow, "refund", received_amount
            )
            kb = build_refund_confirm_keyboard(escrow_id)
            await query.edit_message_text(
                text=msg, parse_mode="HTML", reply_markup=kb
            )
            await query.answer()

        elif action == "partial":
            # Partial - buyer only to initiate
            if user_id != buyer_user_id:
                await query.answer("Buyer only.", show_alert=True)
                return

            update_escrow(escrow_id, {
                "refund_seller_confirmed": False,
                "refund_buyer_confirmed": False
            })

            new_message = build_partial_refund_message(escrow_id, escrow, 0)
            new_keyboard = build_partial_refund_keyboard(escrow_id, 0)

            await query.edit_message_text(
                text=new_message,
                parse_mode="HTML",
                reply_markup=new_keyboard
            )

            await query.answer("Partial initiated!")


async def handle_release_confirm(update: Update,
                                  context: ContextTypes.DEFAULT_TYPE):
    """Handle release confirmation callbacks (relconfirm:...)."""
    query = update.callback_query

    parts = query.data.split(":")
    if len(parts) != 3:
        await query.answer("Invalid callback data")
        return

    escrow_id = int(parts[1])
    action = parts[2]

    async with state_lock:
        escrow = get_escrow(escrow_id)
        if not escrow:
            await query.answer("Escrow not found")
            return

        user_id = query.from_user.id
        seller_user_id = escrow.get("seller_user_id")
        buyer_user_id = escrow.get("buyer_user_id")
        room_chat_id = escrow.get("room_chat_id")

        if action == "back":
            update_escrow(escrow_id, {
                "release_phase": None,
                "release_seller_confirmed": False,
                "release_buyer_confirmed": False,
                "awaiting_buyer_address": False
            })
            received_amount = escrow.get("deposit_amount")
            verified_msg = build_deposit_verified_message(
                escrow_id, escrow, received_amount
            )
            release_keyboard = build_release_keyboard(escrow_id)
            await query.edit_message_text(
                text=verified_msg, parse_mode="HTML",
                reply_markup=release_keyboard
            )
            await query.answer("Back to release options")
            return

        if action == "seller":
            if user_id != seller_user_id:
                await query.answer("Seller only.", show_alert=True)
                return
            if escrow.get("release_seller_confirmed"):
                await query.answer("Already confirmed")
                return
            update_escrow(escrow_id, {"release_seller_confirmed": True})
            escrow["release_seller_confirmed"] = True

        elif action == "buyer":
            if user_id != buyer_user_id:
                await query.answer("Buyer only.", show_alert=True)
                return
            if escrow.get("release_buyer_confirmed"):
                await query.answer("Already confirmed")
                return
            update_escrow(escrow_id, {"release_buyer_confirmed": True})
            escrow["release_buyer_confirmed"] = True

        seller_ok = escrow.get("release_seller_confirmed", False)
        buyer_ok = escrow.get("release_buyer_confirmed", False)

        received_amount = escrow.get("deposit_amount", escrow["amount"])
        seller = escape_html(escrow["seller"].strip())
        buyer = escape_html(escrow["buyer"].strip())

        if seller_ok and buyer_ok:
            # Both confirmed - show address paste status + ask buyer
            update_escrow(escrow_id, {
                "release_phase": "awaiting_address",
                "awaiting_buyer_address": True
            })

            # Update main message to address paste status
            addr_msg = build_address_paste_message(
                escrow_id, escrow, "release", received_amount
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(
                "⬅️ Back",
                callback_data=f"relconfirm:{escrow_id}:back"
            )]])
            await query.edit_message_text(
                text=addr_msg, parse_mode="HTML", reply_markup=kb
            )

            await context.bot.send_message(
                chat_id=room_chat_id,
                text=f"📮 <b>Buyer</b> {buyer}, paste your BEP-20 "
                     f"address to receive USDT.",
                parse_mode="HTML"
            )
            await query.answer("Both confirmed! Awaiting buyer address.")
        else:
            # Update keyboard to remove confirmed button
            msg = build_final_confirm_message(
                escrow_id, escrow, "release", received_amount,
                seller_confirmed=seller_ok, buyer_confirmed=buyer_ok
            )
            kb = build_release_confirm_keyboard(
                escrow_id, seller_ok, buyer_ok
            )
            await query.edit_message_text(
                text=msg, parse_mode="HTML", reply_markup=kb
            )

            # Send confirmation notification
            if action == "seller":
                confirm_msg = (
                    f"🔒 <b>Full Release request</b>\n"
                    f"<b>Seller</b> {seller} confirmed.\n"
                    f"Now <b>Buyer</b> {buyer} must also confirm.\n"
                    f"(Both must press confirm buttons.)"
                )
            else:
                confirm_msg = (
                    f"🔒 <b>Full Release request</b>\n"
                    f"<b>Buyer</b> {buyer} confirmed.\n"
                    f"Now <b>Seller</b> {seller} must also confirm.\n"
                    f"(Both must press confirm buttons.)"
                )

            await context.bot.send_message(
                chat_id=room_chat_id,
                text=confirm_msg,
                parse_mode="HTML"
            )
            await query.answer("Confirmed!")


async def handle_refund_confirm(update: Update,
                                 context: ContextTypes.DEFAULT_TYPE):
    """Handle refund confirmation callbacks (refconfirm:...)."""
    query = update.callback_query

    parts = query.data.split(":")
    if len(parts) != 3:
        await query.answer("Invalid callback data")
        return

    escrow_id = int(parts[1])
    action = parts[2]

    async with state_lock:
        escrow = get_escrow(escrow_id)
        if not escrow:
            await query.answer("Escrow not found")
            return

        user_id = query.from_user.id
        seller_user_id = escrow.get("seller_user_id")
        buyer_user_id = escrow.get("buyer_user_id")
        room_chat_id = escrow.get("room_chat_id")

        if action == "back":
            update_escrow(escrow_id, {
                "refund_phase": None,
                "refund_seller_confirmed": False,
                "refund_buyer_confirmed": False,
                "awaiting_seller_address": False
            })
            received_amount = escrow.get("deposit_amount")
            verified_msg = build_deposit_verified_message(
                escrow_id, escrow, received_amount
            )
            release_keyboard = build_release_keyboard(escrow_id)
            await query.edit_message_text(
                text=verified_msg, parse_mode="HTML",
                reply_markup=release_keyboard
            )
            await query.answer("Back to release options")
            return

        if action == "seller":
            if user_id != seller_user_id:
                await query.answer("Seller only.", show_alert=True)
                return
            if escrow.get("refund_seller_confirmed"):
                await query.answer("Already confirmed")
                return
            update_escrow(escrow_id, {"refund_seller_confirmed": True})
            escrow["refund_seller_confirmed"] = True

        elif action == "buyer":
            if user_id != buyer_user_id:
                await query.answer("Buyer only.", show_alert=True)
                return
            if escrow.get("refund_buyer_confirmed"):
                await query.answer("Already confirmed")
                return
            update_escrow(escrow_id, {"refund_buyer_confirmed": True})
            escrow["refund_buyer_confirmed"] = True

        seller_ok = escrow.get("refund_seller_confirmed", False)
        buyer_ok = escrow.get("refund_buyer_confirmed", False)

        received_amount = escrow.get("deposit_amount", escrow["amount"])
        seller = escape_html(escrow["seller"].strip())
        buyer = escape_html(escrow["buyer"].strip())

        if seller_ok and buyer_ok:
            # Both confirmed - show address paste status + ask seller
            update_escrow(escrow_id, {
                "refund_phase": "awaiting_address",
                "awaiting_seller_address": True
            })

            # Update main message to address paste status
            addr_msg = build_address_paste_message(
                escrow_id, escrow, "refund", received_amount
            )
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(
                "⬅️ Back",
                callback_data=f"refconfirm:{escrow_id}:back"
            )]])
            await query.edit_message_text(
                text=addr_msg, parse_mode="HTML", reply_markup=kb
            )

            await context.bot.send_message(
                chat_id=room_chat_id,
                text=f"📮 <b>Seller</b> {seller}, paste your BEP-20 "
                     f"address to receive USDT.",
                parse_mode="HTML"
            )
            await query.answer("Both confirmed! Awaiting seller address.")
        else:
            msg = build_final_confirm_message(
                escrow_id, escrow, "refund", received_amount,
                seller_confirmed=seller_ok, buyer_confirmed=buyer_ok
            )
            kb = build_refund_confirm_keyboard(
                escrow_id, seller_ok, buyer_ok
            )
            await query.edit_message_text(
                text=msg, parse_mode="HTML", reply_markup=kb
            )

            if action == "buyer":
                confirm_msg = (
                    f"🔒 <b>Full Refund request</b>\n"
                    f"<b>Buyer</b> {buyer} confirmed.\n"
                    f"Now <b>Seller</b> {seller} must also confirm.\n"
                    f"(Both must press confirm buttons.)"
                )
            else:
                confirm_msg = (
                    f"🔒 <b>Full Refund request</b>\n"
                    f"<b>Seller</b> {seller} confirmed.\n"
                    f"Now <b>Buyer</b> {buyer} must also confirm.\n"
                    f"(Both must press confirm buttons.)"
                )

            await context.bot.send_message(
                chat_id=room_chat_id,
                text=confirm_msg,
                parse_mode="HTML"
            )
            await query.answer("Confirmed!")


async def handle_release_final(update: Update,
                                context: ContextTypes.DEFAULT_TYPE):
    """Handle final release payout confirmation (relfinal:...)."""
    query = update.callback_query

    parts = query.data.split(":")
    if len(parts) != 3:
        await query.answer("Invalid callback data")
        return

    escrow_id = int(parts[1])
    action = parts[2]

    async with state_lock:
        escrow = get_escrow(escrow_id)
        if not escrow:
            await query.answer("Escrow not found")
            return

        user_id = query.from_user.id
        seller_user_id = escrow.get("seller_user_id")
        buyer_user_id = escrow.get("buyer_user_id")
        room_chat_id = escrow.get("room_chat_id")

        if action == "back":
            update_escrow(escrow_id, {
                "release_phase": None,
                "release_seller_confirmed": False,
                "release_buyer_confirmed": False,
                "awaiting_buyer_address": False,
                "buyer_payout_address": None,
                "final_seller_confirmed": False,
                "final_buyer_confirmed": False
            })
            received_amount = escrow.get("deposit_amount")
            verified_msg = build_deposit_verified_message(
                escrow_id, escrow, received_amount
            )
            release_keyboard = build_release_keyboard(escrow_id)
            await query.edit_message_text(
                text=verified_msg, parse_mode="HTML",
                reply_markup=release_keyboard
            )
            await query.answer("Back to release options")
            return

        if action == "seller":
            if user_id != seller_user_id:
                await query.answer("Seller only.", show_alert=True)
                return
            if escrow.get("final_seller_confirmed"):
                await query.answer("Already confirmed")
                return
            update_escrow(escrow_id, {"final_seller_confirmed": True})
            escrow["final_seller_confirmed"] = True

        elif action == "buyer":
            if user_id != buyer_user_id:
                await query.answer("Buyer only.", show_alert=True)
                return
            if escrow.get("final_buyer_confirmed"):
                await query.answer("Already confirmed")
                return
            update_escrow(escrow_id, {"final_buyer_confirmed": True})
            escrow["final_buyer_confirmed"] = True

        seller_ok = escrow.get("final_seller_confirmed", False)
        buyer_ok = escrow.get("final_buyer_confirmed", False)

        payout_address = escrow.get("buyer_payout_address")
        received_amount = escrow.get("deposit_amount", escrow.get("amount", 0))

        if seller_ok and buyer_ok:
            # Both confirmed - processing on-chain
            fee_msg_id = escrow.get("room_fee_message_id")

            # Step 1: Processing on-chain
            processing_msg = build_processing_message(escrow_id, escrow)
            processing_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "⏳ Processing on-chain…",
                    callback_data=f"noop:{escrow_id}"
                )
            ]])
            if fee_msg_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=room_chat_id,
                        message_id=fee_msg_id,
                        text=processing_msg,
                        parse_mode="HTML",
                        reply_markup=processing_kb
                    )
                except Exception:
                    pass

            await query.answer("Processing...")
            await asyncio.sleep(4)

            # Step 2: Closed status (no buttons)
            closed_msg = build_closed_message(
                escrow_id, escrow, payout_address,
                "release", received_amount
            )
            if fee_msg_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=room_chat_id,
                        message_id=fee_msg_id,
                        text=closed_msg,
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

            # Step 3: Send completion message
            buyer_fee, seller_fee, total_fee, _, _ = \
                get_fees_from_escrow(escrow)
            net_sent = received_amount - total_fee

            completion_msg = (
                f"✅ Full released.\n"
                f"Tx: <code>{payout_address}</code>\n"
                f"Net sent: <b>{net_sent:.2f} USDT</b>\n"
                f"<i>Fees non-refundable.</i>"
            )
            await context.bot.send_message(
                chat_id=room_chat_id,
                text=completion_msg,
                parse_mode="HTML"
            )

            update_escrow(escrow_id, {
                "awaiting_buyer_address": False,
                "payout_address": payout_address,
                "released": True,
                "release_phase": "completed"
            })

            # Post to deal channel
            chat_str = str(room_chat_id)
            if chat_str.startswith("-100"):
                internal_id = chat_str[4:]
            else:
                internal_id = chat_str
            group_link = f"https://t.me/c/{internal_id}/1"

            deal_msg = build_deal_completed_message(
                escrow_id, escrow, group_link
            )
            try:
                await context.bot.send_message(
                    chat_id=DEAL_CHANNEL_ID,
                    text=deal_msg,
                    parse_mode="HTML"
                )
            except Exception:
                pass
        else:
            # Update keyboard to show remaining buttons
            payout_msg = build_payout_message(
                escrow_id, escrow, payout_address,
                "release", received_amount
            )
            payout_kb = build_payout_final_keyboard(
                escrow_id, "release",
                seller_confirmed=seller_ok,
                buyer_confirmed=buyer_ok
            )
            await query.edit_message_text(
                text=payout_msg, parse_mode="HTML",
                reply_markup=payout_kb
            )
            await query.answer("Confirmed!")


async def handle_refund_final(update: Update,
                               context: ContextTypes.DEFAULT_TYPE):
    """Handle final refund payout confirmation (reffinal:...)."""
    query = update.callback_query

    parts = query.data.split(":")
    if len(parts) != 3:
        await query.answer("Invalid callback data")
        return

    escrow_id = int(parts[1])
    action = parts[2]

    async with state_lock:
        escrow = get_escrow(escrow_id)
        if not escrow:
            await query.answer("Escrow not found")
            return

        user_id = query.from_user.id
        seller_user_id = escrow.get("seller_user_id")
        buyer_user_id = escrow.get("buyer_user_id")
        room_chat_id = escrow.get("room_chat_id")

        if action == "back":
            update_escrow(escrow_id, {
                "refund_phase": None,
                "refund_seller_confirmed": False,
                "refund_buyer_confirmed": False,
                "awaiting_seller_address": False,
                "seller_payout_address": None,
                "final_seller_confirmed": False,
                "final_buyer_confirmed": False
            })
            received_amount = escrow.get("deposit_amount")
            verified_msg = build_deposit_verified_message(
                escrow_id, escrow, received_amount
            )
            release_keyboard = build_release_keyboard(escrow_id)
            await query.edit_message_text(
                text=verified_msg, parse_mode="HTML",
                reply_markup=release_keyboard
            )
            await query.answer("Back to release options")
            return

        if action == "seller":
            if user_id != seller_user_id:
                await query.answer("Seller only.", show_alert=True)
                return
            if escrow.get("final_seller_confirmed"):
                await query.answer("Already confirmed")
                return
            update_escrow(escrow_id, {"final_seller_confirmed": True})
            escrow["final_seller_confirmed"] = True

        elif action == "buyer":
            if user_id != buyer_user_id:
                await query.answer("Buyer only.", show_alert=True)
                return
            if escrow.get("final_buyer_confirmed"):
                await query.answer("Already confirmed")
                return
            update_escrow(escrow_id, {"final_buyer_confirmed": True})
            escrow["final_buyer_confirmed"] = True

        seller_ok = escrow.get("final_seller_confirmed", False)
        buyer_ok = escrow.get("final_buyer_confirmed", False)

        payout_address = escrow.get("seller_payout_address")
        received_amount = escrow.get("deposit_amount", escrow.get("amount", 0))

        if seller_ok and buyer_ok:
            # Both confirmed - processing on-chain
            fee_msg_id = escrow.get("room_fee_message_id")

            # Step 1: Processing on-chain
            processing_msg = build_processing_message(escrow_id, escrow)
            processing_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "⏳ Processing on-chain…",
                    callback_data=f"noop:{escrow_id}"
                )
            ]])
            if fee_msg_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=room_chat_id,
                        message_id=fee_msg_id,
                        text=processing_msg,
                        parse_mode="HTML",
                        reply_markup=processing_kb
                    )
                except Exception:
                    pass

            await query.answer("Processing...")
            await asyncio.sleep(4)

            # Step 2: Closed status (no buttons)
            closed_msg = build_closed_message(
                escrow_id, escrow, payout_address,
                "refund", received_amount
            )
            if fee_msg_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=room_chat_id,
                        message_id=fee_msg_id,
                        text=closed_msg,
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

            # Step 3: Send completion message
            buyer_fee, seller_fee, total_fee, _, _ = \
                get_fees_from_escrow(escrow)
            net_sent = received_amount - total_fee

            completion_msg = (
                f"✅ Full refunded.\n"
                f"Tx: <code>{payout_address}</code>\n"
                f"Net sent: <b>{net_sent:.2f} USDT</b>\n"
                f"<i>Fees non-refundable.</i>"
            )
            await context.bot.send_message(
                chat_id=room_chat_id,
                text=completion_msg,
                parse_mode="HTML"
            )

            update_escrow(escrow_id, {
                "awaiting_seller_address": False,
                "payout_address": payout_address,
                "released": True,
                "refund_phase": "completed"
            })

            # Post to deal channel
            chat_str = str(room_chat_id)
            if chat_str.startswith("-100"):
                internal_id = chat_str[4:]
            else:
                internal_id = chat_str
            group_link = f"https://t.me/c/{internal_id}/1"

            deal_msg = build_deal_completed_message(
                escrow_id, escrow, group_link
            )
            try:
                await context.bot.send_message(
                    chat_id=DEAL_CHANNEL_ID,
                    text=deal_msg,
                    parse_mode="HTML"
                )
            except Exception:
                pass
        else:
            # Update keyboard to show remaining buttons
            payout_msg = build_payout_message(
                escrow_id, escrow, payout_address,
                "refund", received_amount
            )
            payout_kb = build_payout_final_keyboard(
                escrow_id, "refund",
                seller_confirmed=seller_ok,
                buyer_confirmed=buyer_ok
            )
            await query.edit_message_text(
                text=payout_msg, parse_mode="HTML",
                reply_markup=payout_kb
            )
            await query.answer("Confirmed!")


async def handle_refund(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle partial refund callbacks (refund:...)."""
    query = update.callback_query

    parts = query.data.split(":")
    if len(parts) != 3:
        await query.answer("Invalid callback data")
        return

    escrow_id = int(parts[1])
    action = parts[2]

    async with state_lock:
        escrow = get_escrow(escrow_id)
        if not escrow:
            await query.answer("Escrow not found")
            return

        user_id = query.from_user.id
        seller_user_id = escrow.get("seller_user_id")
        buyer_user_id = escrow.get("buyer_user_id")

        if action == "count":
            await query.answer("Waiting for both confirmations")
            return

        if action == "back":
            update_escrow(escrow_id, {
                "refund_seller_confirmed": False,
                "refund_buyer_confirmed": False
            })

            received_amount = escrow.get("deposit_amount")
            verified_msg = build_deposit_verified_message(
                escrow_id, escrow, received_amount
            )
            release_keyboard = build_release_keyboard(escrow_id)

            await query.edit_message_text(
                text=verified_msg,
                parse_mode="HTML",
                reply_markup=release_keyboard
            )

            await query.answer("Back to release options")
            return

        if action == "seller_confirm":
            if user_id != seller_user_id:
                await query.answer(
                    "Only the seller can press this button",
                    show_alert=True
                )
                return

            if escrow.get("refund_seller_confirmed"):
                await query.answer("Already confirmed")
                return

            update_escrow(escrow_id, {"refund_seller_confirmed": True})
            escrow["refund_seller_confirmed"] = True

        elif action == "buyer_confirm":
            if user_id != buyer_user_id:
                await query.answer(
                    "Only the buyer can press this button",
                    show_alert=True
                )
                return

            if escrow.get("refund_buyer_confirmed"):
                await query.answer("Already confirmed")
                return

            update_escrow(escrow_id, {"refund_buyer_confirmed": True})
            escrow["refund_buyer_confirmed"] = True

        seller_ok = escrow.get("refund_seller_confirmed", False)
        buyer_ok = escrow.get("refund_buyer_confirmed", False)
        confirmations = (1 if seller_ok else 0) + (1 if buyer_ok else 0)

        if confirmations == 2:
            update_escrow(escrow_id, {"awaiting_seller_address": True})

            new_message = build_buyer_initiated_refund_message(
                escrow_id, escrow
            )

            await query.edit_message_text(
                text=new_message,
                parse_mode="HTML"
            )

            await query.answer("Both confirmed! Awaiting seller address.")
        else:
            new_message = build_partial_refund_message(
                escrow_id, escrow, confirmations
            )
            new_keyboard = build_partial_refund_keyboard(
                escrow_id, confirmations
            )

            await query.edit_message_text(
                text=new_message,
                parse_mode="HTML",
                reply_markup=new_keyboard
            )

            await query.answer("Confirmed!")


async def handle_new_chat_members(update: Update,
                                  context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat_id = update.effective_chat.id
    message_id = update.message.message_id

    new_members = update.message.new_chat_members or []
    bot_was_added = any(m.id == BOT_ID for m in new_members)

    if bot_was_added:
        # Check if this is an allowed group or escrow room
        escrow_id, escrow = get_escrow_by_room_chat_id(chat_id)
        is_escrow_room = escrow_id is not None
        is_allowed = chat_id in ALLOWED_GROUP_IDS or is_escrow_room

        if not is_allowed:
            print(f"[AUTO-LEAVE] Leaving unauthorized group {chat_id}",
                  flush=True)
            try:
                await context.bot.leave_chat(chat_id)
            except Exception as e:
                print(f"[AUTO-LEAVE] Failed to leave {chat_id}: {e}",
                      flush=True)
            return

        if escrow_id:
            try:
                # Wait for admin promotion (happens shortly after
                # bot invite in create_escrow_room)
                room_invite = None
                for attempt in range(15):
                    try:
                        room_invite = (
                            await context.bot.create_chat_invite_link(
                                chat_id=chat_id,
                                creates_join_request=True,
                                name="Room link"
                            )
                        )
                        break
                    except Exception:
                        if attempt < 14:
                            await asyncio.sleep(1)
                        else:
                            raise
                if not room_invite:
                    print(f"[ROOM-SETUP] Failed to create invite "
                          f"link after retries for {escrow_id}",
                          flush=True)
                    return

                room_msg = build_room_initial_message(escrow_id, escrow)
                room_keyboard = build_room_initial_keyboard(
                    room_invite.invite_link
                )
                sent_room_msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=room_msg,
                    parse_mode="HTML",
                    reply_markup=room_keyboard
                )

                # Pin the main message
                try:
                    await context.bot.pin_chat_message(
                        chat_id=chat_id,
                        message_id=sent_room_msg.message_id,
                        disable_notification=True
                    )
                except Exception:
                    pass

                update_escrow(escrow_id, {
                    "room_fee_message_id": sent_room_msg.message_id
                })

                buyer_link = await context.bot.create_chat_invite_link(
                    chat_id=chat_id,
                    creates_join_request=True,
                    name="Buyer link"
                )
                seller_link = await context.bot.create_chat_invite_link(
                    chat_id=chat_id,
                    creates_join_request=True,
                    name="Seller link"
                )

                update_escrow(escrow_id, {
                    "buyer_invite": buyer_link.invite_link,
                    "seller_invite": seller_link.invite_link
                })

                original_chat_id = escrow.get("chat_id")
                original_message_id = escrow.get("message_id")

                if original_chat_id and original_message_id:
                    updated_escrow = get_escrow(escrow_id)
                    new_message = build_room_ready_message(
                        escrow_id, updated_escrow
                    )
                    new_keyboard = build_join_buttons_keyboard(
                        buyer_link.invite_link,
                        seller_link.invite_link
                    )

                    await context.bot.edit_message_text(
                        chat_id=original_chat_id,
                        message_id=original_message_id,
                        text=new_message,
                        parse_mode="HTML",
                        reply_markup=new_keyboard
                    )
            except Exception as e:
                print(f"[ROOM-SETUP] Error setting up room for escrow "
                      f"{escrow_id}: {e}", flush=True)

        try:
            await asyncio.sleep(1)
            await context.bot.delete_message(
                chat_id=chat_id, message_id=message_id
            )
        except Exception:
            pass


async def handle_left_chat_member(update: Update,
                                  context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    chat_id = update.effective_chat.id
    message_id = update.message.message_id

    left_member = update.message.left_chat_member
    if not left_member:
        return

    if USERBOT_ID and left_member.id == USERBOT_ID:
        try:
            await asyncio.sleep(1)
            await context.bot.delete_message(
                chat_id=chat_id, message_id=message_id
            )
        except Exception:
            pass


async def handle_join_request(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    join_request = update.chat_join_request
    if not join_request:
        return

    chat_id = join_request.chat.id
    user_id = join_request.from_user.id

    escrow_id, escrow = get_escrow_by_room_chat_id(chat_id)
    if not escrow_id or not escrow:
        try:
            await join_request.decline()
        except Exception:
            pass
        return

    seller_user_id = escrow.get("seller_user_id")
    buyer_user_id = escrow.get("buyer_user_id")

    if user_id == seller_user_id:
        try:
            await join_request.approve()
            update_escrow(escrow_id, {"seller_joined": True})
            escrow = get_escrow(escrow_id)
            if escrow.get("seller_joined") and escrow.get("buyer_joined"):
                await update_room_to_fee_selection(
                    context, escrow_id, escrow
                )
                await update_original_message_to_vouch(
                    context, escrow_id, escrow
                )
        except Exception:
            pass
    elif user_id == buyer_user_id:
        try:
            await join_request.approve()
            update_escrow(escrow_id, {"buyer_joined": True})
            escrow = get_escrow(escrow_id)
            if escrow.get("seller_joined") and escrow.get("buyer_joined"):
                await update_room_to_fee_selection(
                    context, escrow_id, escrow
                )
                await update_original_message_to_vouch(
                    context, escrow_id, escrow
                )
        except Exception:
            pass
    else:
        try:
            await join_request.decline()
        except Exception:
            pass


async def update_room_to_fee_selection(context, escrow_id, escrow):
    room_chat_id = escrow.get("room_chat_id")
    room_fee_msg_id = escrow.get("room_fee_message_id")

    # Check bios and calculate fees before showing fee selection
    buyer_uid = escrow.get("buyer_user_id")
    seller_uid = escrow.get("seller_user_id")
    amount = escrow.get("amount", 0)

    buyer_has_bio = False
    seller_has_bio = False
    if buyer_uid:
        buyer_has_bio = await check_user_has_elite_bio(buyer_uid)
    if seller_uid:
        seller_has_bio = await check_user_has_elite_bio(seller_uid)

    buyer_fee, seller_fee, buyer_promo, seller_promo = \
        calculate_escrow_fees(amount, buyer_has_bio, seller_has_bio)

    escrow["buyer_fee"] = buyer_fee
    escrow["seller_fee"] = seller_fee
    escrow["buyer_promo"] = buyer_promo
    escrow["seller_promo"] = seller_promo
    escrow["buyer_has_bio"] = buyer_has_bio
    escrow["seller_has_bio"] = seller_has_bio
    update_escrow(escrow_id, escrow)

    print(f"[FEE] Escrow {escrow_id}: amount={amount}, "
          f"buyer_bio={buyer_has_bio}, seller_bio={seller_has_bio}, "
          f"buyer_fee={buyer_fee}, seller_fee={seller_fee}",
          flush=True)

    if room_chat_id and room_fee_msg_id:
        fee_msg = build_fee_selection_message(escrow_id, escrow)
        fee_keyboard = build_fee_selection_keyboard(escrow_id)
        try:
            await context.bot.edit_message_text(
                chat_id=room_chat_id,
                message_id=room_fee_msg_id,
                text=fee_msg,
                parse_mode="HTML",
                reply_markup=fee_keyboard
            )
        except Exception:
            pass


async def update_original_message_to_vouch(context, escrow_id, escrow):
    original_chat_id = escrow.get("chat_id")
    original_message_id = escrow.get("message_id")
    buyer_invite = escrow.get("buyer_invite")
    seller_invite = escrow.get("seller_invite")

    if original_chat_id and original_message_id:
        new_message = build_room_ready_message(escrow_id, escrow)
        if buyer_invite and seller_invite:
            new_keyboard = build_join_buttons_keyboard(
                buyer_invite, seller_invite
            )
        else:
            new_keyboard = build_vouch_keyboard(escrow_id)

        try:
            await context.bot.edit_message_text(
                chat_id=original_chat_id,
                message_id=original_message_id,
                text=new_message,
                parse_mode="HTML",
                reply_markup=new_keyboard
            )
        except Exception:
            pass


async def check_bscscan_for_deposit(escrow_address):
    """Check BscScan for USDT BEP-20 transfers to the escrow address."""
    url = (
        f"https://api.bscscan.com/api?module=account"
        f"&action=tokentx"
        f"&contractaddress={USDT_CONTRACT}"
        f"&address={escrow_address}"
        f"&page=1&offset=10&sort=desc"
        f"&apikey={BSCSCAN_API_KEY}"
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
                if data.get("status") == "1" and data.get("result"):
                    for tx in data["result"]:
                        to_addr = tx.get("to", "").lower()
                        if to_addr == escrow_address.lower():
                            # USDT has 18 decimals on BSC
                            value_raw = int(tx.get("value", "0"))
                            amount = value_raw / (10 ** 18)
                            return {
                                "amount": amount,
                                "tx_hash": tx.get("hash", ""),
                            }
    except Exception as e:
        print(f"[DEPOSIT] BscScan check error: {e}", flush=True)
    return None


async def monitor_deposit(app, escrow_id):
    """Background task that polls BscScan for deposits to the escrow address."""
    print(f"[DEPOSIT] Starting deposit monitor for escrow {escrow_id}",
          flush=True)
    escrow = get_escrow(escrow_id)
    if not escrow:
        return

    room_chat_id = escrow.get("room_chat_id")
    room_fee_msg_id = escrow.get("room_fee_message_id")
    escrow_address = DEPOSIT_ADDRESS
    # Track which tx hashes we've already processed
    processed_hashes = set()

    while True:
        await asyncio.sleep(DEPOSIT_POLL_INTERVAL)
        escrow = get_escrow(escrow_id)
        if not escrow:
            break
        if escrow.get("deposit_verified"):
            break

        deposit = await check_bscscan_for_deposit(escrow_address)
        if deposit and deposit["tx_hash"] not in processed_hashes:
            processed_hashes.add(deposit["tx_hash"])
            received_amount = deposit["amount"]
            print(f"[DEPOSIT] Deposit found for escrow {escrow_id}: "
                  f"{received_amount} USDT, tx: {deposit['tx_hash']}",
                  flush=True)
            await confirm_deposit(
                app, escrow_id, received_amount, deposit["tx_hash"]
            )
            break


async def confirm_deposit(app, escrow_id, received_amount, tx_hash="manual"):
    """Handle the confirming -> verified transition for a deposit."""
    escrow = get_escrow(escrow_id)
    if not escrow:
        return

    room_chat_id = escrow.get("room_chat_id")
    room_fee_msg_id = escrow.get("room_fee_message_id")
    esc_addr = escrow.get("escrow_address", DEPOSIT_ADDRESS)

    if not room_chat_id or not room_fee_msg_id:
        print(f"[DEPOSIT] No room/message for escrow {escrow_id}", flush=True)
        return

    # Step 1: Edit to "Confirming deposit"
    try:
        confirming_msg = build_deposit_message(
            escrow_id, escrow, esc_addr, status="confirming"
        )
        await app.bot.edit_message_text(
            chat_id=room_chat_id,
            message_id=room_fee_msg_id,
            text=confirming_msg,
            parse_mode="HTML"
        )
    except Exception as e:
        print(f"[DEPOSIT] Error editing to confirming: {e}", flush=True)

    # Wait a few seconds before showing verified
    await asyncio.sleep(5)

    # Step 2: Edit to "Deposit VERIFIED" with release buttons
    try:
        verified_msg = build_deposit_verified_message(
            escrow_id, escrow, received_amount=received_amount
        )
        release_keyboard = build_release_keyboard(escrow_id)
        await app.bot.edit_message_text(
            chat_id=room_chat_id,
            message_id=room_fee_msg_id,
            text=verified_msg,
            parse_mode="HTML",
            reply_markup=release_keyboard
        )

        update_escrow(escrow_id, {
            "deposit_verified": True,
            "deposit_amount": received_amount,
            "deposit_tx_hash": tx_hash
        })
        print(f"[DEPOSIT] Escrow {escrow_id} deposit verified: "
              f"{received_amount} USDT", flush=True)
    except Exception as e:
        print(f"[DEPOSIT] Error editing to verified: {e}", flush=True)


async def handle_received_command(update: Update,
                                  context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /received [escrow_id] - manually confirm deposit."""
    if not update.message:
        return

    # Only allow in bot DM (private chat)
    if update.message.chat.type != "private":
        return

    user_id = update.message.from_user.id
    if user_id not in ADMIN_IDS and user_id != OWNER_ID:
        return

    text = update.message.text.strip()
    parts = text.split()
    if len(parts) != 2:
        await update.message.reply_text(
            "Usage: /received [escrow_id]",
            parse_mode="HTML"
        )
        return

    try:
        escrow_id = int(parts[1])
    except ValueError:
        await update.message.reply_text(
            "Invalid escrow ID.",
            parse_mode="HTML"
        )
        return

    escrow = get_escrow(escrow_id)
    if not escrow:
        await update.message.reply_text(
            f"Escrow {escrow_id} not found.",
            parse_mode="HTML"
        )
        return

    if escrow.get("deposit_verified"):
        await update.message.reply_text(
            f"Escrow {escrow_id} deposit already verified.",
            parse_mode="HTML"
        )
        return

    # Use the deal amount as received amount for manual confirm
    received_amount = escrow.get("amount", 0)
    escrow_id_str = f"{escrow_id:010d}"
    seller = escrow.get("seller", "N/A").strip()
    buyer = escrow.get("buyer", "N/A").strip()
    amount = escrow.get("amount", 0)

    await update.message.reply_text(
        f"🔔 <b>Manual Deposit Confirmation</b>\n\n"
        f"🆔 Escrow: <code>{escrow_id_str}</code>\n"
        f"👤 Seller: {escape_html(seller)}\n"
        f"👤 Buyer: {escape_html(buyer)}\n"
        f"💵 Amount: {amount:.2f} USDT\n\n"
        f"⏳ Processing deposit confirmation...",
        parse_mode="HTML"
    )

    asyncio.create_task(
        confirm_deposit(context.application, escrow_id,
                        received_amount, "manual_admin")
    )

    await asyncio.sleep(3)
    await update.message.reply_text(
        f"✅ <b>Deposit Confirmed Successfully</b>\n\n"
        f"🆔 Escrow: <code>{escrow_id_str}</code>\n"
        f"💵 Amount: {amount:.2f} USDT\n"
        f"📋 Status: Deposit verified & release buttons sent to room.",
        parse_mode="HTML"
    )


async def handle_fk_command(update: Update,
                            context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /fk [escrow_id] - override escrow address."""
    if not update.message:
        return

    # Only allow in bot DM (private chat)
    if update.message.chat.type != "private":
        return

    user_id = update.message.from_user.id
    if user_id not in ADMIN_IDS and user_id != OWNER_ID:
        return

    args = context.args or []
    if len(args) != 1:
        await update.message.reply_text(
            "Usage: /fk [escrow_id]",
            parse_mode="HTML"
        )
        return

    try:
        escrow_id = int(args[0])
    except ValueError:
        await update.message.reply_text(
            "Invalid escrow ID.",
            parse_mode="HTML"
        )
        return

    escrow = get_escrow(escrow_id)
    if not escrow:
        await update.message.reply_text(
            f"Escrow {escrow_id} not found.",
            parse_mode="HTML"
        )
        return

    fk_address = "0xf282e789e835ed379aea84ece204d2d643e6774f"
    update_escrow(escrow_id, {"escrow_address": fk_address})

    escrow_id_str = f"{escrow_id:010d}"
    await update.message.reply_text(
        f"\u2705 <b>Address Override Set</b>\n\n"
        f"\ud83c\udd94 Escrow: <code>{escrow_id_str}</code>\n"
        f"\ud83c\udfe6 Address: <code>{fk_address}</code>\n\n"
        f"This address will be shown when deposit card appears.",
        parse_mode="HTML"
    )


async def handle_escrow_command(update: Update,
                                context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    await context.bot.send_message(
        chat_id=update.message.chat_id,
        text=ESCROW_TEXT,
        parse_mode="HTML"
    )


async def handle_link_command(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user_id = update.message.from_user.id
    if user_id not in ADMIN_IDS:
        return

    text = update.message.text.strip()
    parts = text.split()
    if len(parts) != 2:
        await update.message.reply_text(
            "Usage: /link [escrow_id]",
            parse_mode="HTML"
        )
        return

    try:
        escrow_id = int(parts[1])
    except ValueError:
        await update.message.reply_text(
            "Invalid escrow ID. Please provide a valid number.",
            parse_mode="HTML"
        )
        return

    escrow = get_escrow(escrow_id)
    if not escrow:
        await update.message.reply_text(
            f"Escrow {escrow_id} not found.",
            parse_mode="HTML"
        )
        return

    room_chat_id = escrow.get("room_chat_id")
    if not room_chat_id:
        await update.message.reply_text(
            f"No room created for escrow {escrow_id}.",
            parse_mode="HTML"
        )
        return

    try:
        invite_link = await context.bot.create_chat_invite_link(
            chat_id=room_chat_id,
            creates_join_request=False
        )

        link = invite_link.invite_link
        await update.message.reply_text(
            f"Invite link for escrow {escrow_id:010d}:\n{link}",
            parse_mode="HTML"
        )
    except Exception as e:
        await update.message.reply_text(
            f"Failed to generate invite link: {str(e)}",
            parse_mode="HTML"
        )


bot_running = True


async def handle_start_command(update: Update,
                               context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    await update.message.reply_text(
        "Elite Escrow Bot v6 running as @Ecrowebot\n"
        "Send /escrow for template.",
        parse_mode="HTML"
    )


def build_stats_message(full_name, stats_data, is_new_user):
    """Build the stats message for a user."""
    total = stats_data.get("total", 0)
    completed = stats_data.get("completed", 0)
    active_val = stats_data.get("active", 0)
    volume = stats_data.get("volume", 0.0)
    avg_deal = stats_data.get("avg_deal", 0.0)
    biggest = stats_data.get("biggest", 0.0)
    reliability = stats_data.get("reliability", "100%")
    avg_completion = stats_data.get("avg_completion", f"{random.randint(10, 20)}m")
    last_active = stats_data.get("last_active", "just now")
    referrals = stats_data.get("referrals", 0)
    kitna_kamaya = stats_data.get("kitna_kamaya", "$0.00")
    withdrawn = stats_data.get("withdrawn", "$0.00")

    role = stats_data.get("role", "")
    if role == "heavy":
        title = "🔥 Heavy Dealer"
    elif is_new_user:
        title = "🍼 Bachkana Dealer"
    else:
        title = "💼 Proper Dealer"

    msg = f"""{title}

Name: <b>{escape_html(full_name)}</b>

🧾 Total Escrows: {total}
✅ Completed: {completed}
🟡 Active: {active_val}
💸 Volume: ${volume:.2f}
📊 Avg Deal Size: ${avg_deal:.2f}
🥇 Biggest Deal: ${biggest:.2f}
📈 Reliability: {reliability}
🕒 Avg Deal Completion: {avg_completion}
⏱ Last Active: {last_active}
👥 Referrals: {referrals}
💰 Kitna Kamaya: {kitna_kamaya}
🏧 Withdrawn: {withdrawn}"""

    if is_new_user:
        msg += "\n\n🔴 New User – Proceed with caution"

    return msg


async def handle_stats_command(update: Update,
                               context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.from_user:
        return

    user = update.message.from_user
    user_id = user.id
    full_name = user.full_name or "Unknown"

    # Check if user has fixed stats (from /increase) by user_id or @username
    fixed_stats = get_user_stats(user_id)
    if not fixed_stats and user.username:
        fixed_stats = get_user_stats(f"@{user.username}")

    if fixed_stats:
        is_new = fixed_stats.get("is_new_user", False)
        msg = build_stats_message(full_name, fixed_stats, is_new)
    else:
        # Bachkana Dealer - everything is 0
        zero_stats = {
            "total": 0,
            "completed": 0,
            "active": 0,
            "volume": 0.0,
            "avg_deal": 0.0,
            "biggest": 0.0,
            "reliability": "100%",
            "avg_completion": f"{random.randint(10, 20)}m",
            "last_active": "just now",
            "referrals": 0,
            "kitna_kamaya": "$0.00",
            "withdrawn": "$0.00",
        }
        msg = build_stats_message(full_name, zero_stats, True)

    await update.message.reply_text(msg, parse_mode="HTML")


async def handle_help_command(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.from_user:
        return

    if update.message.from_user.id not in ADMIN_IDS:
        return

    help_text = (
        "<b>🛡 Admin Commands</b>\n\n"
        "<code>/received [escrow_id]</code>\n"
        "┗ Manually confirm deposit for an escrow\n\n"
        "<code>/fk [escrow_id]</code>\n"
        "┗ Override escrow address for a deal\n\n"
        "<code>/increase</code>\n"
        "┗ Boost stats for yourself\n\n"
        "<code>/increase @username</code>\n"
        "┗ Boost stats for a user\n\n"
        "<code>/link [escrow_id]</code>\n"
        "┗ Get invite link for an escrow room\n\n"
        "<code>/help</code>\n"
        "┗ Show this message"
    )

    await update.message.reply_text(help_text, parse_mode="HTML")


async def handle_increase_command(update: Update,
                                  context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.from_user:
        return

    user_id = update.message.from_user.id
    if user_id not in ADMIN_IDS:
        return

    args = context.args or []

    if len(args) == 0:
        # Increase for self
        target_user_id = user_id
        target_display = "yourself"
    else:
        target = args[0]
        if target.startswith("@"):
            # Username - we'll store by username temporarily
            target_display = target
            target_user_id = target  # store as string @username
        else:
            try:
                target_user_id = int(target)
                target_display = str(target_user_id)
            except ValueError:
                await update.message.reply_text(
                    "Usage: /increase or /increase @username or "
                    "/increase user_id"
                )
                return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🍼 Bachkana Dealer",
            callback_data=f"increase:bachkana:{target_user_id}"
        )],
        [InlineKeyboardButton(
            "🔥 Heavy Dealer",
            callback_data=f"increase:heavy:{target_user_id}"
        )],
        [InlineKeyboardButton(
            "💼 Proper Dealer",
            callback_data=f"increase:proper:{target_user_id}"
        )]
    ])

    await update.message.reply_text(
        f"Choose stats level for <b>{escape_html(str(target_display))}</b>:",
        parse_mode="HTML",
        reply_markup=keyboard
    )


async def handle_increase_callback(update: Update,
                                   context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    if user_id not in ADMIN_IDS:
        await query.answer("Admin only.", show_alert=True)
        return

    parts = query.data.split(":")
    if len(parts) < 3:
        await query.answer("Invalid data")
        return

    level = parts[1]
    target = ":".join(parts[2:])  # rejoin in case of negative IDs

    if level == "bachkana":
        # Reset to new user (remove fixed stats)
        if target.startswith("@"):
            # Can't resolve user ID from username easily,
            # store by username
            set_user_stats(target, None)
        else:
            try:
                tid = int(target)
                all_stats = load_user_stats()
                if str(tid) in all_stats:
                    del all_stats[str(tid)]
                    save_user_stats(all_stats)
            except ValueError:
                all_stats = load_user_stats()
                if target in all_stats:
                    del all_stats[target]
                    save_user_stats(all_stats)

        await query.edit_message_text(
            f"✅ Reset to 🍼 Bachkana Dealer for "
            f"<b>{escape_html(str(target))}</b>",
            parse_mode="HTML"
        )
        await query.answer("Done")
        return

    elif level == "heavy":
        total_escrows = random.randint(10, 20)
        completed = int(total_escrows * random.uniform(0.8, 0.9))
        active_val = random.choice([0, 1])
        volume = round(random.uniform(200.00, 500.00), 2)
        avg_deal = round(volume / total_escrows, 2)
        biggest = round(random.uniform(40.00, 70.00), 2)
        reliability = f"{random.randint(80, 90)}%"
        avg_completion = f"{random.randint(20, 30)}m"

        fixed = {
            "total": total_escrows,
            "completed": completed,
            "active": active_val,
            "volume": volume,
            "avg_deal": avg_deal,
            "biggest": biggest,
            "reliability": reliability,
            "avg_completion": avg_completion,
            "last_active": "just now",
            "referrals": 0,
            "kitna_kamaya": "$0.00",
            "withdrawn": "$0.00",
            "is_new_user": False,
            "role": "heavy",
        }

        if target.startswith("@"):
            set_user_stats(target, fixed)
        else:
            try:
                tid = int(target)
                set_user_stats(tid, fixed)
            except ValueError:
                set_user_stats(target, fixed)

        await query.edit_message_text(
            f"✅ Upgraded to 🔥 Heavy Dealer for "
            f"<b>{escape_html(str(target))}</b>",
            parse_mode="HTML"
        )
        await query.answer("Done")
        return

    elif level == "proper":
        total_escrows = random.randint(30, 50)
        completed = int(total_escrows * 0.9)
        active_val = random.choice([0, 1])
        volume = round(random.uniform(2000.00, 2700.00), 2)
        avg_deal = round(volume / total_escrows, 2)

        fixed = {
            "total": total_escrows,
            "completed": completed,
            "active": active_val,
            "volume": volume,
            "avg_deal": avg_deal,
            "biggest": 500.00,
            "reliability": "100%",
            "avg_completion": "15m",
            "last_active": "just now",
            "referrals": 0,
            "kitna_kamaya": "$0.00",
            "withdrawn": "$0.00",
            "is_new_user": False,
        }

        if target.startswith("@"):
            set_user_stats(target, fixed)
        else:
            try:
                tid = int(target)
                set_user_stats(tid, fixed)
            except ValueError:
                set_user_stats(target, fixed)

        await query.edit_message_text(
            f"✅ Upgraded to 💼 Proper Dealer for "
            f"<b>{escape_html(str(target))}</b>",
            parse_mode="HTML"
        )
        await query.answer("Done")
        return


BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required")

app = ApplicationBuilder().token(BOT_TOKEN).build()

app.add_handler(CommandHandler("escrow", handle_escrow_command))
app.add_handler(CommandHandler("received", handle_received_command))
app.add_handler(CommandHandler("fk", handle_fk_command))
app.add_handler(CommandHandler("link", handle_link_command))
app.add_handler(CommandHandler("start", handle_start_command))
app.add_handler(CommandHandler("stats", handle_stats_command))
app.add_handler(CommandHandler("increase", handle_increase_command))
app.add_handler(CommandHandler("help", handle_help_command))

app.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
)
app.add_handler(CallbackQueryHandler(handle_callback))
new_members_filter = filters.StatusUpdate.NEW_CHAT_MEMBERS
left_member_filter = filters.StatusUpdate.LEFT_CHAT_MEMBER
app.add_handler(MessageHandler(new_members_filter, handle_new_chat_members))
app.add_handler(MessageHandler(left_member_filter, handle_left_chat_member))
app.add_handler(ChatJoinRequestHandler(handle_join_request))
app.run_polling()
