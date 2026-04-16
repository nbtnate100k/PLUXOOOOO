"""
PLUXO API server — shop, balances, games (Flask).
"""

import json
import os
import logging
import threading
import re
import random
import string
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from flask_cors import CORS
from shop_lock import shop_products_lock

# ==================== CONFIGURATION ====================
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "pluxo_secret_2024")
PORT = int(os.getenv("PORT", 5000))

# Data files
DATA_DIR = "bot_data"
BALANCES_FILE = os.path.join(DATA_DIR, "balances.json")
PURCHASES_FILE = os.path.join(DATA_DIR, "purchases.json")
LOGS_FILE = os.path.join(DATA_DIR, "action_logs.json")
SHOP_PRODUCTS_FILE = "shop_products.json"
GAMES_FILE = os.path.join(DATA_DIR, "games.json")

SYSTEM_LOCKED = False
GAMES_LOCK = threading.Lock()

# ==================== LOGGING ====================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== DATA FUNCTIONS ====================
def ensure_data_dir():
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)

def load_json(filepath, default=None):
    if default is None:
        default = {}
    try:
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading {filepath}: {e}")
    return default

def save_json(filepath, data):
    ensure_data_dir()
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error(f"Error saving {filepath}: {e}")
        return False

def load_balances():
    return load_json(BALANCES_FILE, {})

def save_balances(balances):
    save_json(BALANCES_FILE, balances)

def load_purchases():
    return load_json(PURCHASES_FILE, {"purchases": []})

def save_purchases(purchases):
    save_json(PURCHASES_FILE, purchases)

def log_action(admin_id, admin_name, action, details=""):
    logs = load_json(LOGS_FILE, {"logs": []})
    logs["logs"].append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "admin_id": admin_id,
        "admin_name": admin_name,
        "action": action,
        "details": details
    })
    logs["logs"] = logs["logs"][-1000:]
    save_json(LOGS_FILE, logs)

def generate_key(length=16):
    """Generate a random alphanumeric key"""
    chars = string.ascii_uppercase + string.digits
    return ''.join(random.choice(chars) for _ in range(length))

def parse_bulk_cards(text: str):
    """Parse bulk pipe-delimited cards (card|mm|yyyy|cvv)"""
    cards = []
    lines = text.strip().split('\n')
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Match: 5355851164846467|02|2026|358
        match = re.match(r'^(\d{15,16})\|(\d{1,2})\|(\d{4})\|(\d{3,4})$', line)
        if match:
            card_number = match.group(1)
            exp_month = match.group(2).zfill(2)
            exp_year = match.group(3)
            cvv = match.group(4)
            
            if len(card_number) == 15:
                card_number = '0' + card_number
            
            cards.append({
                'card_number': card_number,
                'exp_month': exp_month,
                'exp_year': exp_year,
                'cvv': cvv,
                'full_text': f"{card_number}|{exp_month}|{exp_year}|{cvv}",
                'name': '',
                'address': '',
                'city_state_zip': '',
                'country': ''
            })
    
    return cards

def parse_multiline_cards(text: str):
    """Parse multi-line cards with address info (card exp cvv + 4 lines of info)"""
    cards = []
    lines = text.strip().split('\n')
    
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        
        # Match: 4145670692391812 01/29 651
        match = re.match(r'^(\d{15,16})\s+(\d{2})/(\d{2})\s+(\d{3,4})$', line)
        if match:
            card_number = match.group(1)
            exp_month = match.group(2)
            exp_year_short = match.group(3)
            cvv = match.group(4)
            
            # Convert 2-digit year to 4-digit
            exp_year = '20' + exp_year_short
            
            if len(card_number) == 15:
                card_number = '0' + card_number
            
            # Get next 4 lines for address info
            name = lines[i + 1].strip() if i + 1 < len(lines) else ''
            address = lines[i + 2].strip() if i + 2 < len(lines) else ''
            city_state_zip = lines[i + 3].strip() if i + 3 < len(lines) else ''
            country = lines[i + 4].strip() if i + 4 < len(lines) else ''
            
            full_text = f"{card_number} {exp_month}/{exp_year_short} {cvv}\n{name}\n{address}\n{city_state_zip}\n{country}"
            
            cards.append({
                'card_number': card_number,
                'exp_month': exp_month,
                'exp_year': exp_year,
                'cvv': cvv,
                'full_text': full_text,
                'name': name,
                'address': address,
                'city_state_zip': city_state_zip,
                'country': country
            })
            
            i += 5  # Skip to next card block
            continue
        
        i += 1
    
    return cards

def parse_all_formats(text: str):
    """Try both card formats and return parsed cards"""
    # Try pipe format first
    cards = parse_bulk_cards(text)
    if cards:
        return cards
    
    # Try multi-line format
    cards = parse_multiline_cards(text)
    if cards:
        return cards
    
    return []

def get_brand_from_bin(bin_str):
    """Determine card brand from BIN"""
    if not bin_str or len(bin_str) < 6:
        return "VISA"
    first_digit = bin_str[0]
    if first_digit == "4":
        return "VISA"
    elif first_digit == "5":
        return "MASTERCARD"
    elif first_digit == "3":
        return "AMEX"
    return "VISA"

def get_shop_products():
    """Load shop products as a normalized list."""
    with shop_products_lock:
        shop_products = load_json(SHOP_PRODUCTS_FILE, [])
        return list(shop_products) if isinstance(shop_products, list) else []

def save_shop_products(shop_products):
    """Persist shop products to shared storage."""
    with shop_products_lock:
        save_json(SHOP_PRODUCTS_FILE, shop_products)

def clear_shop_products():
    """Clear all shop stock and return removed count."""
    existing_products = get_shop_products()
    removed_count = len(existing_products)
    save_shop_products([])
    return removed_count

def remove_shop_products_by_ids(product_ids):
    """Remove products by id, return removed products and missing ids."""
    if not isinstance(product_ids, list):
        return [], []
    normalized_ids = []
    for pid in product_ids:
        pid_str = str(pid).strip()
        if pid_str:
            normalized_ids.append(pid_str)
    if not normalized_ids:
        return [], []

    existing_products = get_shop_products()
    id_set = set(normalized_ids)

    removed_products = []
    remaining_products = []
    for product in existing_products:
        product_id_str = str(product.get("id", "")).strip()
        if product_id_str in id_set:
            removed_products.append(product)
        else:
            remaining_products.append(product)

    removed_ids = {str(p.get("id", "")).strip() for p in removed_products}
    missing_ids = [pid for pid in normalized_ids if pid not in removed_ids]

    if removed_products:
        save_shop_products(remaining_products)

    return removed_products, missing_ids

def remove_shop_products_by_slots(slot_numbers):
    """Remove products by 1-based slot positions in current stock list."""
    if not isinstance(slot_numbers, list):
        return [], []

    normalized_slots = []
    for slot in slot_numbers:
        try:
            slot_int = int(slot)
            if slot_int not in normalized_slots:
                normalized_slots.append(slot_int)
        except (TypeError, ValueError):
            continue

    if not normalized_slots:
        return [], []

    shop_products = get_shop_products()
    total_products = len(shop_products)
    if total_products == 0:
        return [], normalized_slots

    valid_slots = [slot for slot in normalized_slots if 1 <= slot <= total_products]
    invalid_slots = [slot for slot in normalized_slots if slot < 1 or slot > total_products]
    if not valid_slots:
        return [], invalid_slots

    slot_set = set(valid_slots)
    removed_entries = []
    remaining_products = []

    for idx, product in enumerate(shop_products, start=1):
        if idx in slot_set:
            removed_entries.append({
                "slot": idx,
                "product": product
            })
        else:
            remaining_products.append(product)

    if removed_entries:
        save_shop_products(remaining_products)

    return removed_entries, invalid_slots

def default_games_state():
    return {
        "dice_bets": [],
        "dice_history": [],
        "blackjack_matches": [],
        "blackjack_history": []
    }

def load_games_state():
    state = load_json(GAMES_FILE, default_games_state())
    if not isinstance(state, dict):
        state = default_games_state()
    defaults = default_games_state()
    for key, fallback in defaults.items():
        if not isinstance(state.get(key), list):
            state[key] = fallback
    return state

def save_games_state(state):
    save_json(GAMES_FILE, state)

def valid_secret():
    return request.headers.get('X-Webhook-Secret', '') == WEBHOOK_SECRET

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def make_id(prefix):
    return f"{prefix}_{int(datetime.now(timezone.utc).timestamp() * 1000)}_{random.randint(1000, 9999)}"

def as_money(value, default=0.0):
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return round(float(default), 2)

def ensure_balance_user(balances, username):
    username = str(username or "").lower().strip()
    if username not in balances or not isinstance(balances.get(username), dict):
        balances[username] = {"balance": 0.0, "totalRecharge": 0.0}
    balances[username]["balance"] = as_money(balances[username].get("balance", 0.0))
    balances[username]["totalRecharge"] = as_money(balances[username].get("totalRecharge", 0.0))
    return balances[username]

def log_purchase_notification(username, item_count, total_amount):
    """Log purchase for server logs."""
    logger.info(
        "Purchase: user=%s items=%s total=$%.2f",
        username, item_count, total_amount,
    )

# ==================== FLASK APP ====================
app = Flask(__name__)
# Allow CORS from any origin (needed for GitHub Pages -> Railway)
CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=False)

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "ok", "server": "PLUXO API"})

@app.route('/api/products', methods=['GET', 'OPTIONS'])
def get_products():
    """Serve shop products for the website"""
    if request.method == 'OPTIONS':
        return '', 204
    try:
        shop_products = get_shop_products()
        return jsonify(shop_products)
    except Exception as e:
        logger.error(f"Products API error: {e}")
        return jsonify([]), 200  # Return empty array on error

@app.route('/api/register', methods=['POST', 'OPTIONS'])
def webhook_register():
    if request.method == 'OPTIONS':
        return '', 204
    try:
        secret = request.headers.get('X-Webhook-Secret', '')
        if secret != WEBHOOK_SECRET:
            return jsonify({"error": "Unauthorized"}), 401
        
        data = request.json
        username = data.get('username', '').lower().strip()
        email = data.get('email', '')
        
        if not username:
            return jsonify({"error": "Username required"}), 400
        
        balances = load_balances()
        is_new = username not in balances
        
        if is_new:
            balances[username] = {
                "balance": 0,
                "totalRecharge": 0,
                "email": email,
                "registeredAt": datetime.now(timezone.utc).isoformat()
            }
            save_balances(balances)
            log_action(0, "WEBSITE", "NEW_USER", f"User registered: {username}")
            logger.info(f"New user registered: {username}")
        
        return jsonify({"success": True, "username": username, "isNew": is_new})
    except Exception as e:
        logger.error(f"Register error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/balance/<username>', methods=['GET', 'OPTIONS'])
def get_user_balance(username):
    if request.method == 'OPTIONS':
        return '', 204
    try:
        secret = request.headers.get('X-Webhook-Secret', '')
        if secret != WEBHOOK_SECRET:
            return jsonify({"error": "Unauthorized"}), 401
        
        username = username.lower().strip()
        balances = load_balances()
        
        if username in balances:
            user_data = balances[username]
            return jsonify({
                "success": True,
                "username": username,
                "balance": user_data.get("balance", 0),
                "totalRecharge": user_data.get("totalRecharge", 0)
            })
        else:
            return jsonify({"success": True, "username": username, "balance": 0, "totalRecharge": 0})
    except Exception as e:
        logger.error(f"Balance API error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/balance/update', methods=['POST', 'OPTIONS'])
def update_user_balance():
    if request.method == 'OPTIONS':
        return '', 204
    try:
        secret = request.headers.get('X-Webhook-Secret', '')
        if secret != WEBHOOK_SECRET:
            return jsonify({"error": "Unauthorized"}), 401
        
        data = request.json
        username = data.get('username', '').lower().strip()
        action = data.get('action', '')
        amount = float(data.get('amount', 0))
        
        if not username or not action or amount <= 0:
            return jsonify({"error": "Invalid parameters"}), 400
        
        balances = load_balances()
        if username not in balances:
            balances[username] = {"balance": 0, "totalRecharge": 0}
        
        old_balance = balances[username].get("balance", 0)
        
        if action == 'subtract':
            if old_balance < amount:
                return jsonify({"error": "Insufficient balance"}), 400
            new_balance = old_balance - amount
        elif action == 'add':
            new_balance = old_balance + amount
        else:
            return jsonify({"error": "Invalid action"}), 400
        
        balances[username]["balance"] = new_balance
        save_balances(balances)
        
        return jsonify({"success": True, "username": username, "oldBalance": old_balance, "newBalance": new_balance})
    except Exception as e:
        logger.error(f"Balance update error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/purchase/notify', methods=['POST', 'OPTIONS'])
def notify_purchase():
    """Notify admins about a purchase made on the website"""
    if request.method == 'OPTIONS':
        return '', 204
    try:
        secret = request.headers.get('X-Webhook-Secret', '')
        if secret != WEBHOOK_SECRET:
            return jsonify({"error": "Unauthorized"}), 401
        
        data = request.json
        username = data.get('username', '').lower().strip()
        item_count = data.get('item_count', 1)
        total_amount = float(data.get('total_amount', 0))
        product_ids = data.get('product_ids', [])
        stock_slots = data.get('stock_slots', [])
        
        if not username:
            return jsonify({"error": "Username required"}), 400

        removed_count = 0
        missing_ids = []
        invalid_slots = []

        # Auto-remove purchased stock (single or multiple) when checkout notifies backend.
        if isinstance(product_ids, list) and product_ids:
            removed_products, missing_ids = remove_shop_products_by_ids(product_ids)
            removed_count = len(removed_products)
        elif isinstance(stock_slots, list) and stock_slots:
            removed_entries, invalid_slots = remove_shop_products_by_slots(stock_slots)
            removed_count = len(removed_entries)
        
        log_purchase_notification(username, item_count, total_amount)

        return jsonify({
            "success": True,
            "message": "Notification sent",
            "removed_count": removed_count,
            "missing_ids": missing_ids,
            "invalid_slots": invalid_slots
        })
    except Exception as e:
        logger.error(f"Purchase notification error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/purchase/checkout', methods=['POST', 'OPTIONS'])
def purchase_checkout():
    """Atomically charge balance and remove purchased products from stock."""
    if request.method == 'OPTIONS':
        return '', 204
    try:
        secret = request.headers.get('X-Webhook-Secret', '')
        if secret != WEBHOOK_SECRET:
            return jsonify({"error": "Unauthorized"}), 401

        data = request.json or {}
        username = str(data.get('username', '')).lower().strip()
        items = data.get('items', [])

        if not username or not isinstance(items, list) or len(items) == 0:
            return jsonify({"error": "Invalid parameters"}), 400

        product_ids = []
        total_amount = 0.0
        for item in items:
            if not isinstance(item, dict):
                continue
            product_id = item.get("productId")
            price = float(item.get("price", 0))
            if product_id is None or price <= 0:
                continue
            product_ids.append(product_id)
            total_amount += price

        if not product_ids or total_amount <= 0:
            return jsonify({"error": "Invalid checkout items"}), 400

        # 1) Validate user balance
        balances = load_balances()
        if username not in balances:
            balances[username] = {"balance": 0, "totalRecharge": 0}
        old_balance = float(balances[username].get("balance", 0))
        if old_balance < total_amount:
            return jsonify({"error": "Insufficient balance"}), 400

        # 2) Remove products from shared stock first (so they are not sellable anymore)
        removed_products, missing_ids = remove_shop_products_by_ids(product_ids)
        if missing_ids:
            return jsonify({"error": "Some items are no longer available", "missing_ids": missing_ids}), 409
        if len(removed_products) != len(product_ids):
            return jsonify({"error": "Checkout failed: product mismatch"}), 409

        # 3) Charge balance after stock lock-in
        new_balance = old_balance - total_amount
        balances[username]["balance"] = new_balance
        save_balances(balances)

        # 4) Return purchased product payload with keys/full info
        purchased_items = []
        removed_by_id = {str(p.get("id", "")).strip(): p for p in removed_products}
        for item in items:
            pid_str = str(item.get("productId", "")).strip()
            product = removed_by_id.get(pid_str)
            if not product:
                continue
            purchased_items.append({
                "productId": product.get("id"),
                "bin": product.get("bin", ""),
                "brand": product.get("brand", ""),
                "bank": product.get("bank", "BANK"),
                "base": product.get("base", "2026_US_Base"),
                "price": float(product.get("price", item.get("price", 0))),
                "refundable": bool(product.get("refundable", True)),
                "full_info": product.get("full_info", "")
            })

        log_action(
            0,
            "WEBSITE",
            "CHECKOUT",
            f"User {username} purchased {len(purchased_items)} item(s), total ${total_amount:.2f}"
        )

        return jsonify({
            "success": True,
            "username": username,
            "oldBalance": old_balance,
            "newBalance": new_balance,
            "totalAmount": total_amount,
            "itemCount": len(purchased_items),
            "items": purchased_items
        })
    except Exception as e:
        logger.error(f"Purchase checkout error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/games/dice/bets', methods=['GET', 'OPTIONS'])
def api_get_dice_bets():
    if request.method == 'OPTIONS':
        return '', 204
    if not valid_secret():
        return jsonify({"error": "Unauthorized"}), 401
    with GAMES_LOCK:
        state = load_games_state()
        return jsonify({"success": True, "bets": state["dice_bets"]})

@app.route('/api/games/dice/history', methods=['GET', 'OPTIONS'])
def api_get_dice_history():
    if request.method == 'OPTIONS':
        return '', 204
    if not valid_secret():
        return jsonify({"error": "Unauthorized"}), 401
    with GAMES_LOCK:
        state = load_games_state()
        return jsonify({"success": True, "history": state["dice_history"]})

@app.route('/api/games/dice/create', methods=['POST', 'OPTIONS'])
def api_create_dice_bet():
    if request.method == 'OPTIONS':
        return '', 204
    if not valid_secret():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        data = request.json or {}
        creator = str(data.get("creator", "")).strip().lower()
        creator_name = str(data.get("creatorName", "")).strip()
        amount = as_money(data.get("amount", 0))
        if not creator or not creator_name or amount < 1 or amount > 25:
            return jsonify({"error": "Invalid parameters"}), 400

        with GAMES_LOCK:
            state = load_games_state()
            balances = load_balances()
            creator_data = ensure_balance_user(balances, creator)
            for bet in state["dice_bets"]:
                if bet.get("status") == "waiting" and (bet.get("creator") == creator or bet.get("opponent") == creator):
                    return jsonify({"error": "You already have an active waiting bet"}), 400

            if creator_data["balance"] < amount:
                return jsonify({"error": "Insufficient balance"}), 400

            creator_data["balance"] = as_money(creator_data["balance"] - amount)

            new_bet = {
                "id": make_id("DICE"),
                "creator": creator,
                "creatorName": creator_name,
                "opponent": None,
                "opponentName": None,
                "amount": f"{amount:.2f}",
                "status": "waiting",
                "creatorDebited": True,
                "creatorRoll": None,
                "opponentRoll": None,
                "winner": None,
                "winnerName": None,
                "createdAt": now_iso(),
                "completedAt": None
            }
            state["dice_bets"].append(new_bet)
            save_balances(balances)
            save_games_state(state)
            return jsonify({"success": True, "bet": new_bet, "newBalance": creator_data["balance"]})
    except Exception as e:
        logger.error(f"Create dice bet error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/games/dice/cancel', methods=['POST', 'OPTIONS'])
def api_cancel_dice_bet():
    if request.method == 'OPTIONS':
        return '', 204
    if not valid_secret():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        data = request.json or {}
        bet_id = str(data.get("betId", "")).strip()
        username = str(data.get("username", "")).strip().lower()
        if not bet_id or not username:
            return jsonify({"error": "Invalid parameters"}), 400

        with GAMES_LOCK:
            state = load_games_state()
            balances = load_balances()
            idx = next((i for i, b in enumerate(state["dice_bets"]) if b.get("id") == bet_id), -1)
            if idx < 0:
                return jsonify({"error": "Bet not found"}), 404
            bet = state["dice_bets"][idx]
            if bet.get("creator") != username:
                return jsonify({"error": "Only creator can cancel"}), 403
            if bet.get("status") != "waiting":
                return jsonify({"error": "Bet cannot be cancelled"}), 400
            state["dice_bets"].pop(idx)
            amount = as_money(bet.get("amount", 0))
            creator_data = ensure_balance_user(balances, username)
            refunded = 0.0
            if bet.get("creatorDebited", False):
                creator_data["balance"] = as_money(creator_data["balance"] + amount)
                refunded = amount
            save_balances(balances)
            save_games_state(state)
            return jsonify({"success": True, "amount": refunded, "newBalance": creator_data["balance"]})
    except Exception as e:
        logger.error(f"Cancel dice bet error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/games/dice/accept', methods=['POST', 'OPTIONS'])
def api_accept_dice_bet():
    if request.method == 'OPTIONS':
        return '', 204
    if not valid_secret():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        data = request.json or {}
        bet_id = str(data.get("betId", "")).strip()
        opponent = str(data.get("opponent", "")).strip().lower()
        opponent_name = str(data.get("opponentName", "")).strip()
        if not bet_id or not opponent or not opponent_name:
            return jsonify({"error": "Invalid parameters"}), 400

        with GAMES_LOCK:
            state = load_games_state()
            balances = load_balances()
            idx = next((i for i, b in enumerate(state["dice_bets"]) if b.get("id") == bet_id), -1)
            if idx < 0:
                return jsonify({"error": "Bet not found"}), 404
            bet = state["dice_bets"][idx]
            if bet.get("status") != "waiting":
                return jsonify({"error": "Bet is no longer available"}), 400
            if bet.get("creator") == opponent:
                return jsonify({"error": "Cannot accept your own bet"}), 400

            amount = as_money(bet.get("amount", 0))
            creator_username = str(bet.get("creator", "")).lower().strip()
            creator_data = ensure_balance_user(balances, creator_username)
            opponent_data = ensure_balance_user(balances, opponent)

            # Backward-compatible safety: older matches may not have creator stake debited.
            if not bet.get("creatorDebited", False):
                if creator_data["balance"] < amount:
                    state["dice_bets"].pop(idx)
                    save_games_state(state)
                    return jsonify({"error": "Creator has insufficient balance. Match cancelled."}), 409
                creator_data["balance"] = as_money(creator_data["balance"] - amount)
                bet["creatorDebited"] = True

            if opponent_data["balance"] < amount:
                return jsonify({"error": "Insufficient balance"}), 400
            opponent_data["balance"] = as_money(opponent_data["balance"] - amount)

            creator_roll = random.randint(1, 6)
            opponent_roll = random.randint(1, 6)
            winner = "tie"
            winner_name = "Tie"
            if creator_roll > opponent_roll:
                winner = bet.get("creator")
                winner_name = bet.get("creatorName")
            elif opponent_roll > creator_roll:
                winner = opponent
                winner_name = opponent_name

            creator_payout = 0.0
            opponent_payout = 0.0
            if winner == creator_username:
                creator_payout = as_money(amount * 2)
                creator_data["balance"] = as_money(creator_data["balance"] + creator_payout)
            elif winner == opponent:
                opponent_payout = as_money(amount * 2)
                opponent_data["balance"] = as_money(opponent_data["balance"] + opponent_payout)
            else:
                # Tie: return 50% to each side (house keeps 50% total)
                creator_payout = as_money(amount * 0.5)
                opponent_payout = as_money(amount * 0.5)
                creator_data["balance"] = as_money(creator_data["balance"] + creator_payout)
                opponent_data["balance"] = as_money(opponent_data["balance"] + opponent_payout)

            completed = {
                **bet,
                "opponent": opponent,
                "opponentName": opponent_name,
                "status": "completed",
                "creatorRoll": creator_roll,
                "opponentRoll": opponent_roll,
                "winner": winner,
                "winnerName": winner_name,
                "creatorPayout": creator_payout,
                "opponentPayout": opponent_payout,
                "creatorBalanceAfter": creator_data["balance"],
                "opponentBalanceAfter": opponent_data["balance"],
                "completedAt": now_iso()
            }

            state["dice_bets"].pop(idx)
            state["dice_history"].insert(0, completed)
            state["dice_history"] = state["dice_history"][:100]
            save_balances(balances)
            save_games_state(state)
            return jsonify({
                "success": True,
                "result": completed,
                "balances": {
                    "creator": creator_data["balance"],
                    "opponent": opponent_data["balance"]
                },
                "viewerBalance": opponent_data["balance"]
            })
    except Exception as e:
        logger.error(f"Accept dice bet error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/games/blackjack/matches', methods=['GET', 'OPTIONS'])
def api_get_blackjack_matches():
    if request.method == 'OPTIONS':
        return '', 204
    if not valid_secret():
        return jsonify({"error": "Unauthorized"}), 401
    with GAMES_LOCK:
        state = load_games_state()
        return jsonify({"success": True, "matches": state["blackjack_matches"]})

@app.route('/api/games/blackjack/history', methods=['GET', 'OPTIONS'])
def api_get_blackjack_history():
    if request.method == 'OPTIONS':
        return '', 204
    if not valid_secret():
        return jsonify({"error": "Unauthorized"}), 401
    with GAMES_LOCK:
        state = load_games_state()
        return jsonify({"success": True, "history": state["blackjack_history"]})

@app.route('/api/games/blackjack/create', methods=['POST', 'OPTIONS'])
def api_create_blackjack_match():
    if request.method == 'OPTIONS':
        return '', 204
    if not valid_secret():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        data = request.json or {}
        creator = str(data.get("creator", "")).strip().lower()
        creator_name = str(data.get("creatorName", "")).strip()
        amount = as_money(data.get("amount", 0))
        if not creator or not creator_name or amount < 1 or amount > 25:
            return jsonify({"error": "Invalid parameters"}), 400

        with GAMES_LOCK:
            state = load_games_state()
            balances = load_balances()
            creator_data = ensure_balance_user(balances, creator)
            existing = next((m for m in state["blackjack_matches"] if m.get("creator") == creator and m.get("status") == "waiting"), None)
            if existing:
                return jsonify({"error": "You already have an open match"}), 400

            if creator_data["balance"] < amount:
                return jsonify({"error": "Insufficient balance"}), 400
            creator_data["balance"] = as_money(creator_data["balance"] - amount)

            match = {
                "id": make_id("BJ"),
                "creator": creator,
                "creatorName": creator_name,
                "opponent": None,
                "opponentName": None,
                "amount": f"{amount:.2f}",
                "status": "waiting",
                "creatorDebited": True,
                "createdAt": now_iso()
            }
            state["blackjack_matches"].append(match)
            save_balances(balances)
            save_games_state(state)
            return jsonify({"success": True, "match": match, "newBalance": creator_data["balance"]})
    except Exception as e:
        logger.error(f"Create blackjack match error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/games/blackjack/cancel', methods=['POST', 'OPTIONS'])
def api_cancel_blackjack_match():
    if request.method == 'OPTIONS':
        return '', 204
    if not valid_secret():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        data = request.json or {}
        match_id = str(data.get("matchId", "")).strip()
        username = str(data.get("username", "")).strip().lower()
        if not match_id or not username:
            return jsonify({"error": "Invalid parameters"}), 400

        with GAMES_LOCK:
            state = load_games_state()
            balances = load_balances()
            idx = next((i for i, m in enumerate(state["blackjack_matches"]) if m.get("id") == match_id), -1)
            if idx < 0:
                return jsonify({"error": "Match not found"}), 404
            match = state["blackjack_matches"][idx]
            if match.get("creator") != username:
                return jsonify({"error": "Only creator can cancel"}), 403
            if match.get("status") != "waiting":
                return jsonify({"error": "Match cannot be cancelled"}), 400

            state["blackjack_matches"].pop(idx)
            amount = as_money(match.get("amount", 0))
            creator_data = ensure_balance_user(balances, username)
            refunded = 0.0
            if match.get("creatorDebited", False):
                creator_data["balance"] = as_money(creator_data["balance"] + amount)
                refunded = amount
            save_balances(balances)
            save_games_state(state)
            return jsonify({"success": True, "amount": refunded, "newBalance": creator_data["balance"]})
    except Exception as e:
        logger.error(f"Cancel blackjack match error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/games/blackjack/join', methods=['POST', 'OPTIONS'])
def api_join_blackjack_match():
    if request.method == 'OPTIONS':
        return '', 204
    if not valid_secret():
        return jsonify({"error": "Unauthorized"}), 401
    try:
        data = request.json or {}
        match_id = str(data.get("matchId", "")).strip()
        opponent = str(data.get("opponent", "")).strip().lower()
        opponent_name = str(data.get("opponentName", "")).strip()
        if not match_id or not opponent or not opponent_name:
            return jsonify({"error": "Invalid parameters"}), 400

        with GAMES_LOCK:
            state = load_games_state()
            balances = load_balances()
            idx = next((i for i, m in enumerate(state["blackjack_matches"]) if m.get("id") == match_id), -1)
            if idx < 0:
                return jsonify({"error": "Match not found"}), 404
            match = state["blackjack_matches"][idx]
            if match.get("status") != "waiting":
                return jsonify({"error": "Match not available"}), 400
            if match.get("creator") == opponent:
                return jsonify({"error": "Cannot join your own match"}), 400

            amount = as_money(match.get("amount", 0))
            creator_username = str(match.get("creator", "")).lower().strip()
            creator_data = ensure_balance_user(balances, creator_username)
            opponent_data = ensure_balance_user(balances, opponent)

            # Backward-compatible safety for old waiting matches.
            if not match.get("creatorDebited", False):
                if creator_data["balance"] < amount:
                    state["blackjack_matches"].pop(idx)
                    save_games_state(state)
                    return jsonify({"error": "Creator has insufficient balance. Match cancelled."}), 409
                creator_data["balance"] = as_money(creator_data["balance"] - amount)
                match["creatorDebited"] = True

            if opponent_data["balance"] < amount:
                return jsonify({"error": "Insufficient balance"}), 400
            opponent_data["balance"] = as_money(opponent_data["balance"] - amount)

            creator_score = random.randint(12, 22)
            opponent_score = random.randint(12, 22)
            creator_bust = creator_score > 21
            opponent_bust = opponent_score > 21

            winner = "tie"
            winner_name = "Tie"
            if creator_bust and opponent_bust:
                winner = "tie"
                winner_name = "Tie"
            elif creator_bust:
                winner = opponent
                winner_name = opponent_name
            elif opponent_bust:
                winner = match.get("creator")
                winner_name = match.get("creatorName")
            elif creator_score > opponent_score:
                winner = match.get("creator")
                winner_name = match.get("creatorName")
            elif opponent_score > creator_score:
                winner = opponent
                winner_name = opponent_name

            creator_payout = 0.0
            opponent_payout = 0.0
            if winner == creator_username:
                creator_payout = as_money(amount * 2)
                creator_data["balance"] = as_money(creator_data["balance"] + creator_payout)
            elif winner == opponent:
                opponent_payout = as_money(amount * 2)
                opponent_data["balance"] = as_money(opponent_data["balance"] + opponent_payout)
            else:
                # Tie: return 50% to each side (house keeps 50% total)
                creator_payout = as_money(amount * 0.5)
                opponent_payout = as_money(amount * 0.5)
                creator_data["balance"] = as_money(creator_data["balance"] + creator_payout)
                opponent_data["balance"] = as_money(opponent_data["balance"] + opponent_payout)

            completed = {
                **match,
                "opponent": opponent,
                "opponentName": opponent_name,
                "creatorScore": creator_score,
                "opponentScore": opponent_score,
                "winner": winner,
                "winnerName": winner_name,
                "creatorPayout": creator_payout,
                "opponentPayout": opponent_payout,
                "creatorBalanceAfter": creator_data["balance"],
                "opponentBalanceAfter": opponent_data["balance"],
                "status": "completed",
                "completedAt": now_iso()
            }

            state["blackjack_matches"].pop(idx)
            state["blackjack_history"].insert(0, completed)
            state["blackjack_history"] = state["blackjack_history"][:100]
            save_balances(balances)
            save_games_state(state)
            return jsonify({
                "success": True,
                "result": completed,
                "balances": {
                    "creator": creator_data["balance"],
                    "opponent": opponent_data["balance"]
                },
                "viewerBalance": opponent_data["balance"]
            })
    except Exception as e:
        logger.error(f"Join blackjack match error: {e}")
        return jsonify({"error": str(e)}), 500

# ==================== MAIN ====================
ensure_data_dir()


try:
    import shop_bot

    shop_bot.launch_shop_bot_if_enabled()
except Exception:
    logger.exception("Failed to launch shop Telegram bot")

if __name__ == "__main__":
    # Run Flask server (for local development)
    logger.info(f"Starting PLUXO API Server on port {PORT}...")
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
