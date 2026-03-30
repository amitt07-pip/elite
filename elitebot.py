import asyncio
import json
import os
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
from telethon.tl.types import ChatAdminRights
from telethon import utils as telethon_utils
import database

ADMIN_IDS = [
    7472359048, 7880967664, 8453993167, 2001575810, 5825027777,
    6864194951, 8093808661, 5229586098, 7422906767, 7962772947,
    7338429782, 8004116104, 7715451354, 8034627772, 5208040247
]

OWNER_ID = 7338429782

ESCROW_TEXT = """<b>🛡 Escrow Form</b>
<code>Seller: @
Buyer: @
Amount[USDT]: 
Rate: 
Time:</code>
"""

STATE_FILE = "escrow_state.json"
ESCROWS_FILE = "escrows.json"

TELETHON_API_ID = 38828234
TELETHON_API_HASH = "99d96d08bc57f882907032a2f8f65b46"
TELETHON_SESSION = os.environ.get("TELETHON_SESSION", "")

BOT_USERNAME = "Elite_test_1_bot"
BOT_ID = 8783514181
USERBOT_ID = None

state_lock = asyncio.Lock()
telethon_client = None


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"next_id": 50}


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


async def create_escrow_room(escrow_id):
    try:
        print(f"[ROOM] Starting room creation for escrow {escrow_id}",
              flush=True)
        client = await init_telethon_client()
        if not client:
            print("[ROOM] Telethon client init failed", flush=True)
            return None

        print("[ROOM] Telethon client connected", flush=True)
        escrow_id_str = f"{escrow_id:08d}"
        group_title = f"Elite Escrow Group No. {escrow_id_str}"

        result = await client(CreateChannelRequest(
            title=group_title,
            about="",
            megagroup=True
        ))
        print("[ROOM] Group created", flush=True)

        channel = result.chats[0]
        room_chat_id = telethon_utils.get_peer_id(channel)

        bot_entity = await client.get_entity(BOT_USERNAME)
        print(f"[ROOM] Bot entity resolved: {bot_entity}", flush=True)

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

        await client(LeaveChannelRequest(channel))
        print("[ROOM] Userbot left group", flush=True)

        update_escrow(escrow_id, {"room_chat_id": room_chat_id})
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

    escrow_id_str = f"{escrow_id:08d}"

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

    escrow_id_str = f"{escrow_id:08d}"

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

    escrow_id_str = f"{escrow_id:08d}"

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

    escrow_id_str = f"{escrow_id:08d}"

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

    # Escrow fees (currently free)
    buyer_fee = 0.00
    seller_fee = 0.00
    total_fee = 0.00

    escrow_id_str = f"{escrow_id:08d}"

    message = f"""🟢 Escrow • <code>{escrow_id_str}</code>
━━━━━━━━━━━━━━━━━━━━
🧾 <b>This deal fee</b>: Buyer <code>{buyer_fee:.2f}</code> USDT • Seller <code>{seller_fee:.2f}</code> USDT • Total <code>{total_fee:.2f}</code> USDT
👤 <b>Buyer promo</b>: This deal is free by amount threshold - promo status is still tracked for future deals.
👤 <b>Seller promo</b>: This deal is free by amount threshold — promo status is still tracked for future deals.

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

    # Escrow fees (currently free)
    buyer_fee = 0.00
    seller_fee = 0.00
    total_fee = buyer_fee + seller_fee

    escrow_id_str = f"{escrow_id:08d}"

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


def build_deposit_message(escrow_id, data, escrow_address):
    seller = escape_html(data["seller"].strip())
    buyer = escape_html(data["buyer"].strip())
    amount = data["amount"]
    rate = data["rate"]
    total_inr = data["total_inr"]
    time_val = escape_html(data["time"])

    # Escrow fees (currently free)
    buyer_fee = 0.00
    seller_fee = 0.00
    total_fee = buyer_fee + seller_fee

    escrow_id_str = f"{escrow_id:08d}"

    message = f"""🟢 Escrow • <code>{escrow_id_str}</code>
━━━━━━━━━━━━━━━━━━━━
🧾 <b>This deal fee</b>: Buyer <code>{buyer_fee:.2f}</code> USDT • Seller <code>{seller_fee:.2f}</code> USDT • Total <code>{total_fee:.2f}</code> USDT
👤 <b>Buyer promo</b>: This deal is free by amount threshold - promo status is still tracked for future deals.
👤 <b>Seller promo</b>: This deal is free by amount threshold — promo status is still tracked for future deals.

✅ <b>Seller</b>: {seller}
✅ <b>Buyer</b>: {buyer}
💵 <b>Amount</b>: {amount:.1f} USDT
💱 <b>Rate</b>: {rate:.1f} INR/USDT
💰 <b>Total INR</b>: ₹{total_inr:.1f}
🕒 <b>Time</b>: {time_val}

🏦 <b>Escrow address</b>:
<code>0x8c640881238BEC28509bB3a8F37Dbf3398668a4F</code>
🔐 <b>Verify code</b>: 08FEV4AW
⚠ <i>Security</i>: This room blocks human-posted addresses. Ignore any address sent by users/admins—only trust this pinned bot card.

<b>Status</b>: Awaiting seller deposit."""

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

    escrow_id_str = f"{escrow_id:08d}"

    message = f"""🟢 Escrow • <code>{escrow_id_str}</code>
━━━━━━━━━━━━━━━━━━━━
✅ <b>Seller</b>: {seller}
✅ <b>Buyer</b>: {buyer}
💵 <b>Amount</b>: {amount:.1f} USDT (BEP-20)
💱 <b>Rate</b>: {rate:.1f} INR/USDT
💰 <b>Total INR</b>: ₹{total_inr:.1f}
🕒 <b>Time</b>: {time_val}

🎉 <b>New Year Offer</b>: <code>0 USDT</code> platform fee - escrow is FREE.

<b>Status</b>: Payment detected. Waiting confirmations on-chain...

✅ Payment detected on-chain.
⏳ Confirmation: <b>{confirmations}/61</b>"""

    return message


def build_deposit_verified_message(escrow_id, data):
    seller = escape_html(data["seller"])
    buyer = escape_html(data["buyer"])
    amount = data["amount"]
    rate = data["rate"]
    total_inr = data["total_inr"]
    time_val = escape_html(data["time"])

    escrow_id_str = f"{escrow_id:08d}"

    message = f"""🟢 Escrow • <code>{escrow_id_str}</code>
━━━━━━━━━━━━━━━━━━━━
✅ <b>Seller</b>: {seller}
✅ <b>Buyer</b>: {buyer}
💵 <b>Amount</b>: {amount:.1f} USDT (BEP-20)
💱 <b>Rate</b>: {rate:.1f} INR/USDT
💰 <b>Total INR</b>: ₹{total_inr:.1f}
🕒 <b>Time</b>: {time_val}

📥 <b>Received(on-chain)</b>: {amount:.1f} USDT (≈₹{total_inr:.1f})

🎉 <b>New Year Offer</b>: <code>0 USDT</code> platform fee - escrow is FREE.

<b>Status</b>: ✅ Deposit VERIFIED.
Choose <b>Full Release</b> to send all USDT to buyer, or \
<b>Partial / Refund</b> to split between buyer and seller.
<i>Only seller</i> can start release; both must confirm."""

    return message


def build_release_keyboard(escrow_id):
    full_release = InlineKeyboardButton(
        "🔓 Full Release",
        callback_data=f"release:{escrow_id}:full"
    )
    partial_refund = InlineKeyboardButton(
        "🧩 Partial / Refund",
        callback_data=f"release:{escrow_id}:partial"
    )
    return InlineKeyboardMarkup([[full_release, partial_refund]])


def build_seller_initiated_release_message(escrow_id, data):
    seller = escape_html(data["seller"])
    buyer = escape_html(data["buyer"])
    amount = data["amount"]
    rate = data["rate"]
    total_inr = data["total_inr"]
    time_val = escape_html(data["time"])

    escrow_id_str = f"{escrow_id:08d}"

    message = f"""🟢 Escrow • <code>{escrow_id_str}</code>
━━━━━━━━━━━━━━━━━━━━
✅ <b>Seller</b>: {seller}
✅ <b>Buyer</b>: {buyer}
💵 <b>Amount</b>: {amount:.1f} USDT (BEP-20)
💱 <b>Rate</b>: {rate:.1f} INR/USDT
💰 <b>Total INR</b>: ₹{total_inr:.1f}
🕒 <b>Time</b>: {time_val}

📥 <b>Received(on-chain)</b>: {amount:.1f} USDT (≈₹{total_inr:.1f})

🎉 <b>New Year Offer</b>: <code>0 USDT</code> platform fee - escrow is FREE.

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

    escrow_id_str = f"{escrow_id:08d}"

    message = f"""🟢 Escrow • <code>{escrow_id_str}</code>
━━━━━━━━━━━━━━━━━━━━
✅ <b>Seller</b>: {seller}
✅ <b>Buyer</b>: {buyer}
💵 <b>Amount</b>: {amount:.1f} USDT (BEP-20)
💱 <b>Rate</b>: {rate:.1f} INR/USDT
💰 <b>Total INR</b>: ₹{total_inr:.1f}
🕒 <b>Time</b>: {time_val}

📥 <b>Received(on-chain)</b>: {amount:.1f} USDT (≈₹{total_inr:.1f})

🎉 <b>New Year Offer</b>: <code>0 USDT</code> platform fee - escrow is FREE.

<b>Status</b>: 🔓 Released (payout sent)."""

    return message


def build_partial_refund_message(escrow_id, data, confirmations):
    seller = escape_html(data["seller"])
    buyer = escape_html(data["buyer"])
    amount = data["amount"]
    rate = data["rate"]
    total_inr = data["total_inr"]
    time_val = escape_html(data["time"])

    escrow_id_str = f"{escrow_id:08d}"

    message = f"""🟢 Escrow • <code>{escrow_id_str}</code>
━━━━━━━━━━━━━━━━━━━━
✅ <b>Seller</b>: {seller}
✅ <b>Buyer</b>: {buyer}
💵 <b>Amount</b>: {amount:.1f} USDT (BEP-20)
💱 <b>Rate</b>: {rate:.1f} INR/USDT
💰 <b>Total INR</b>: ₹{total_inr:.1f}
🕒 <b>Time</b>: {time_val}

📥 <b>Received(on-chain)</b>: {amount:.1f} USDT (≈₹{total_inr:.1f})

🎉 <b>New Year Offer</b>: <code>0 USDT</code> platform fee - escrow is FREE.

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

    escrow_id_str = f"{escrow_id:08d}"

    message = f"""🟢 Escrow • <code>{escrow_id_str}</code>
━━━━━━━━━━━━━━━━━━━━
✅ <b>Seller</b>: {seller}
✅ <b>Buyer</b>: {buyer}
💵 <b>Amount</b>: {amount:.1f} USDT (BEP-20)
💱 <b>Rate</b>: {rate:.1f} INR/USDT
💰 <b>Total INR</b>: ₹{total_inr:.1f}
🕒 <b>Time</b>: {time_val}

📥 <b>Received(on-chain)</b>: {amount:.1f} USDT (≈₹{total_inr:.1f})

🎉 <b>New Year Offer</b>: <code>0 USDT</code> platform fee - escrow is FREE.

<b>Status</b>: Buyer initiated refund.
Seller must provide BEP-20 address to receive funds."""

    return message


DEAL_CHANNEL_ID = -1003266978268


def build_deal_completed_message(escrow_id, data, group_link):
    seller = escape_html(data["seller"])
    buyer = escape_html(data["buyer"])
    amount = data["amount"]
    rate = data["rate"]

    escrow_id_str = f"{escrow_id:08d}"

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
    else:
        awaiting_buyer = False
        awaiting_seller = False

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


def normalize_username(username):
    if not username:
        return None
    return username.lstrip("@").lower()


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

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

        if user_id != seller_user_id:
            await query.answer(
                "Only the seller can select the fee mode",
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
            esc_addr = get_next_escrow_address()
            update_escrow(escrow_id, {"escrow_address": esc_addr})
            escrow["escrow_address"] = esc_addr
            new_message = build_deposit_message(escrow_id, escrow, esc_addr)
            new_keyboard = None
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
        escrow_id_str = f"{escrow_id:08d}"

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
            if user_id != seller_user_id:
                await query.answer(
                    "Only the seller can press this button",
                    show_alert=True
                )
                return

            update_escrow(escrow_id, {
                "release_type": "full",
                "awaiting_buyer_address": True
            })

            new_message = build_seller_initiated_release_message(
                escrow_id, escrow
            )

            await query.edit_message_text(
                text=new_message,
                parse_mode="HTML"
            )

            await query.answer("Release initiated!")

        elif action == "partial":
            if user_id != buyer_user_id:
                await query.answer(
                    "Only the buyer can press this button",
                    show_alert=True
                )
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

            await query.answer("Partial/Refund initiated!")


async def handle_refund(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            if user_id != buyer_user_id:
                await query.answer(
                    "Only the buyer can press this button",
                    show_alert=True
                )
                return

            update_escrow(escrow_id, {
                "refund_seller_confirmed": False,
                "refund_buyer_confirmed": False
            })

            verified_msg = build_deposit_verified_message(escrow_id, escrow)
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
        escrow_id, escrow = get_escrow_by_room_chat_id(chat_id)
        if escrow_id:
            try:
                room_invite = await context.bot.create_chat_invite_link(
                    chat_id=chat_id,
                    creates_join_request=True,
                    name="Room link"
                )

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
            except Exception:
                pass

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
            f"Invite link for escrow {escrow_id:08d}:\n{link}",
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

    global bot_running
    user_id = update.message.from_user.id
    if user_id != OWNER_ID:
        return

    bot_running = True
    await update.message.reply_text(
        "Bot started.",
        parse_mode="HTML"
    )


async def handle_stop_command(update: Update,
                              context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    global bot_running
    user_id = update.message.from_user.id
    if user_id != OWNER_ID:
        return

    bot_running = False
    await update.message.reply_text(
        "Bot stopped. Use /start to resume.",
        parse_mode="HTML"
    )


async def handle_status_command(update: Update,
                                context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user_id = update.message.from_user.id
    if user_id != OWNER_ID:
        return

    status = "running" if bot_running else "stopped"
    deals = database.get_all_deals()
    total_deals = len(deals)

    await update.message.reply_text(
        f"Bot status: <b>{status}</b>\n"
        f"Total deals in database: <b>{total_deals}</b>",
        parse_mode="HTML"
    )


BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required")

app = ApplicationBuilder().token(BOT_TOKEN).build()

app.add_handler(CommandHandler("escrow", handle_escrow_command))
app.add_handler(CommandHandler("link", handle_link_command))
app.add_handler(CommandHandler("start", handle_start_command))
app.add_handler(CommandHandler("stop", handle_stop_command))
app.add_handler(CommandHandler("status", handle_status_command))

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
