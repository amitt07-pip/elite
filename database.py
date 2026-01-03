import json
import os
import hashlib
from cryptography.fernet import Fernet
from datetime import datetime

DATABASE_FILE = "deals_database.enc"
DATABASE_PASSWORD = "AMIT7678Q"


def _get_encryption_key():
    key_bytes = hashlib.sha256(DATABASE_PASSWORD.encode()).digest()
    return Fernet(key_bytes[:32].hex().encode()[:44] + b'=')


def _derive_fernet_key(password: str) -> bytes:
    digest = hashlib.sha256(password.encode()).digest()
    import base64
    return base64.urlsafe_b64encode(digest)


def get_fernet():
    key = _derive_fernet_key(DATABASE_PASSWORD)
    return Fernet(key)


def load_deals():
    if not os.path.exists(DATABASE_FILE):
        return {}

    try:
        fernet = get_fernet()
        with open(DATABASE_FILE, "rb") as f:
            encrypted_data = f.read()
        decrypted_data = fernet.decrypt(encrypted_data)
        return json.loads(decrypted_data.decode())
    except Exception:
        return {}


def save_deals(deals):
    fernet = get_fernet()
    data = json.dumps(deals, indent=2)
    encrypted_data = fernet.encrypt(data.encode())
    with open(DATABASE_FILE, "wb") as f:
        f.write(encrypted_data)


def save_deal(escrow_id, data):
    deals = load_deals()
    escrow_id_str = str(escrow_id)

    deal_record = {
        "deal_id": escrow_id,
        "buyer_username": data.get("buyer", ""),
        "buyer_user_id": data.get("buyer_user_id"),
        "seller_username": data.get("seller", ""),
        "seller_user_id": data.get("seller_user_id"),
        "amount": data.get("amount", 0),
        "rate": data.get("rate", 0),
        "total_inr": data.get("total_inr", 0),
        "deal_date": data.get("deal_date", datetime.now().isoformat()),
        "deal_status": data.get("deal_status", "pending"),
        "room_chat_id": data.get("room_chat_id"),
        "tx_hash": data.get("tx_hash"),
        "payout_address": data.get("payout_address"),
        "released": data.get("released", False)
    }

    deals[escrow_id_str] = deal_record
    save_deals(deals)
    return deal_record


def get_deal(escrow_id):
    deals = load_deals()
    return deals.get(str(escrow_id))


def update_deal(escrow_id, updates):
    deals = load_deals()
    escrow_id_str = str(escrow_id)

    if escrow_id_str in deals:
        deals[escrow_id_str].update(updates)
        save_deals(deals)
        return deals[escrow_id_str]
    return None


def update_deal_status(escrow_id, status):
    return update_deal(escrow_id, {"deal_status": status})


def get_all_deals():
    return load_deals()


def get_deal_by_room_chat_id(room_chat_id):
    deals = load_deals()
    for deal_id, deal in deals.items():
        if deal.get("room_chat_id") == room_chat_id:
            return int(deal_id), deal
    return None, None
