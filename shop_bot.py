"""
Pluxo shop Telegram bot — add/remove listings (no redemption keys; site sells by checkout).
Set SHOP_BOT_ADMIN_IDS to your numeric Telegram user id(s), comma-separated.
"""
import logging
import os
import re
from typing import Optional, Tuple

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv(
    "BOT_TOKEN",
    "8595954246:AAE6pvDyNj8sM9E1WIDD5_VH4M8RrJwFrEw",
)

# Used when /upload is sent with only the pipe line (no leading price)
DEFAULT_UPLOAD_PRICE = float(os.getenv("DEFAULT_UPLOAD_PRICE", "15"))


def _admin_id_set():
    raw = os.getenv("SHOP_BOT_ADMIN_IDS", "")
    out = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


def is_admin(user_id: int) -> bool:
    admins = _admin_id_set()
    if not admins:
        return False
    return user_id in admins


def _import_main():
    import main as main_mod

    return main_mod


def get_brand_from_bin(bin_str: str) -> str:
    if not bin_str or len(bin_str) < 1:
        return "VISA"
    d = bin_str[0]
    if d == "4":
        return "VISA"
    if d == "5":
        return "MASTERCARD"
    if d == "3":
        return "AMEX"
    return "VISA"


def make_product_entry(main_mod, bin_str: str, price: float, seller_id: str, full_info: str = ""):
    products = main_mod.get_shop_products()
    next_id = max([int(p.get("id", 0) or 0) for p in products], default=0) + 1
    b = re.sub(r"\D", "", bin_str)[:6].ljust(6, "0")[:6]
    brand = get_brand_from_bin(b)
    return {
        "id": next_id,
        "bin": b,
        "brand": brand,
        "type": "CREDIT",
        "country": {
            "flag": "🇺🇸",
            "flagClass": "fi-us",
            "code": "US",
            "name": "USA",
        },
        "hasName": True,
        "hasAddress": True,
        "hasZip": True,
        "hasPhone": True,
        "hasMail": True,
        "hasSSN": True,
        "hasDOB": True,
        "bank": "BANK",
        "base": "2026_US_Base",
        "refundable": True,
        "price": str(round(float(price), 2)),
        "key": "",
        "seller_id": str(seller_id),
        "full_info": full_info or "",
    }


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    admins = _admin_id_set()
    if not admins:
        await update.message.reply_text(
            "🔧 **Shop bot** is not configured yet.\n\n"
            "Add env **`SHOP_BOT_ADMIN_IDS`** on the server with your Telegram user id "
            f"(yours looks like: `{uid}`).\n\n"
            "Then use /add, /upload, /list, /remove.",
            parse_mode="Markdown",
        )
        return
    if not is_admin(uid):
        await update.message.reply_text(
            f"⛔ Not authorized. Your Telegram id: `{uid}`\n"
            "Ask the owner to add it to **SHOP_BOT_ADMIN_IDS**.",
            parse_mode="Markdown",
        )
        return
    await update.message.reply_text(
        "📦 **Pluxo shop bot**\n\n"
        "`/upload <price> <pipe line>` — full record (see below)\n"
        "`/upload <pipe line>` — same, price = "
        f"{DEFAULT_UPLOAD_PRICE:.2f} (set **DEFAULT_UPLOAD_PRICE** on server to change)\n"
        "`/add <price> <bin>` — BIN-only listing\n"
        "`/list` · `/remove <id>`\n"
        "📎 **.txt** + caption `price=15.50` — one row per line\n\n"
        "**Upload format** (example):\n"
        "`123456789000|00|00|123|\"Name\"|\"Addr\"|City|ST|ZIP|phone|`",
        parse_mode="Markdown",
    )


def _parse_upload_text(message_text: str) -> Tuple[Optional[float], str, str]:
    """
    Returns (price, line, error). error non-empty means invalid.
    """
    m = re.match(r"^/upload(?:@\S+)?\s*(.*)$", message_text.strip(), re.DOTALL | re.IGNORECASE)
    if not m:
        return None, "", "not_upload"
    rest = (m.group(1) or "").strip()
    if not rest:
        return None, "", "empty"
    # Optional leading price, then rest of line (pipe data)
    pm = re.match(r"^(\d+(?:\.\d+)?)\s+(.+)$", rest, re.DOTALL)
    if pm:
        try:
            price = float(pm.group(1))
        except ValueError:
            return None, "", "bad_price"
        line = pm.group(2).strip()
    else:
        price = DEFAULT_UPLOAD_PRICE
        line = rest
    line = line.rstrip("|").strip()
    if "|" not in line:
        return None, "", "no_pipe"
    first = line.split("|", 1)[0].strip().strip('"')
    digits = re.sub(r"\D", "", first)
    if len(digits) < 6:
        return None, "", "short_bin"
    if price <= 0:
        return None, "", "bad_price"
    return price, line, ""


async def cmd_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("⛔ Not authorized.")
        return
    text = update.message.text or ""
    price, line, err = _parse_upload_text(text)
    if err == "not_upload":
        return
    if err == "empty":
        await update.message.reply_text(
            "Usage:\n"
            "`/upload 12.50 123456789000|00|00|123|...`\n"
            "or `/upload 123456789000|...` (uses default price)",
            parse_mode="Markdown",
        )
        return
    if err == "no_pipe":
        await update.message.reply_text("Line must contain `|` fields (pipe format).")
        return
    if err == "short_bin":
        await update.message.reply_text("First field must contain at least 6 digits (BIN/PAN prefix).")
        return
    if err == "bad_price":
        await update.message.reply_text("Invalid price.")
        return

    main_mod = _import_main()
    products = main_mod.get_shop_products()
    first_field = line.split("|", 1)[0]
    entry = make_product_entry(main_mod, first_field, price, seller_id=uid, full_info=line)
    products.append(entry)
    main_mod.save_shop_products(products)
    await update.message.reply_text(
        f"✅ **Uploaded** id **{entry['id']}** · BIN `{entry['bin']}` · ${entry['price']}\n"
        f"Sells on the site; stock removes automatically when someone checks out.",
        parse_mode="Markdown",
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("⛔ Not authorized.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: `/add <price> <bin>`", parse_mode="Markdown")
        return
    try:
        price = float(str(context.args[0]).replace("$", "").replace(",", ""))
    except ValueError:
        await update.message.reply_text("Invalid price.")
        return
    bin_raw = str(context.args[1]).strip()
    if not re.sub(r"\D", "", bin_raw):
        await update.message.reply_text("Invalid BIN.")
        return

    main_mod = _import_main()
    entry = make_product_entry(main_mod, bin_raw, price, seller_id=uid)
    products = main_mod.get_shop_products()
    products.append(entry)
    main_mod.save_shop_products(products)
    await update.message.reply_text(
        f"✅ Listed **#{entry['id']}** · BIN `{entry['bin']}` · ${entry['price']}\n"
        "No key — buyers get **full_info** after checkout on the site.",
        parse_mode="Markdown",
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("⛔ Not authorized.")
        return
    main_mod = _import_main()
    products = main_mod.get_shop_products()
    if not products:
        await update.message.reply_text("Shop is empty.")
        return
    lines = []
    for i, p in enumerate(products[:40], start=1):
        lines.append(f"{i}. id={p.get('id')} · {p.get('bin')} · ${p.get('price', '0')}")
    extra = f"\n… +{len(products) - 40} more" if len(products) > 40 else ""
    await update.message.reply_text("**Listings**\n" + "\n".join(lines) + extra, parse_mode="Markdown")


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("⛔ Not authorized.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/remove <id>` (product **id** from /list)")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("id must be a number.")
        return
    main_mod = _import_main()
    products = main_mod.get_shop_products()
    removed = None
    kept = []
    for p in products:
        if int(p.get("id", -1)) == target_id:
            removed = p
        else:
            kept.append(p)
    if removed is None:
        await update.message.reply_text("No listing with that id.")
        return
    main_mod.save_shop_products(kept)
    await update.message.reply_text(
        f"🗑 Removed id **{target_id}** · BIN `{removed.get('bin')}`",
        parse_mode="Markdown",
    )


def _parse_price_caption(caption: str) -> Optional[float]:
    if not caption:
        return None
    m = re.search(r"price\s*[=:]\s*([\d.]+)", caption, re.I)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    try:
        return float(caption.strip().replace("$", ""))
    except ValueError:
        return None


async def on_document_txt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    doc = update.message.document
    if not doc or not (doc.file_name or "").lower().endswith(".txt"):
        return
    price = _parse_price_caption(update.message.caption or "")
    if price is None or price <= 0:
        await update.message.reply_text(
            "Set price in the **caption**, e.g. `price=15.50` or `15.50`"
        )
        return
    file = await doc.get_file()
    data = await file.download_as_bytearray()
    text = data.decode("utf-8", errors="replace")
    main_mod = _import_main()
    products = main_mod.get_shop_products()
    max_id = max([int(p.get("id", 0) or 0) for p in products], default=0)
    added = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        first = line.split("|", 1)[0]
        digits = re.sub(r"\D", "", first)
        if len(digits) < 6:
            continue
        max_id += 1
        b = digits[:6].ljust(6, "0")[:6]
        entry = {
            "id": max_id,
            "bin": b,
            "brand": get_brand_from_bin(b),
            "type": "CREDIT",
            "country": {
                "flag": "🇺🇸",
                "flagClass": "fi-us",
                "code": "US",
                "name": "USA",
            },
            "hasName": True,
            "hasAddress": True,
            "hasZip": True,
            "hasPhone": True,
            "hasMail": True,
            "hasSSN": True,
            "hasDOB": True,
            "bank": "BANK",
            "base": "2026_US_Base",
            "refundable": True,
            "price": str(round(float(price), 2)),
            "key": "",
            "seller_id": str(uid),
            "full_info": line,
        }
        products.append(entry)
        added += 1
    if added == 0:
        await update.message.reply_text("No valid lines (need at least 6 digits in first field).")
        return
    main_mod.save_shop_products(products)
    await update.message.reply_text(f"✅ Added **{added}** listing(s) at ${price:.2f} each.", parse_mode="Markdown")


def run_bot():
    """Blocking; run in a background thread from main."""
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_start))
    application.add_handler(CommandHandler("upload", cmd_upload))
    application.add_handler(CommandHandler("add", cmd_add))
    application.add_handler(CommandHandler("list", cmd_list))
    application.add_handler(CommandHandler("remove", cmd_remove))
    application.add_handler(
        MessageHandler(filters.Document.FileExtension("txt"), on_document_txt)
    )
    logger.info("Shop Telegram bot polling…")
    application.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


def run_bot_thread():
    t = __import__("threading").Thread(target=run_bot, daemon=True, name="shop-bot")
    t.start()
    return t
