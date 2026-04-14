"""Shared lock for shop_products.json (Flask API + Telegram bot thread)."""
import threading

shop_products_lock = threading.Lock()
