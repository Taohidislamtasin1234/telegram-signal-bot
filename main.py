import asyncio
import os
import sqlite3
import time
import threading
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta
from telegram import Bot
from telegram.request import HTTPXRequest

# ======================================
# DUMMY HTTP SERVER FOR RENDER PORT CHECK
# ======================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is running 24/7 successfully!")

    def log_message(self, format, *args):
        return

def run_health_check():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    print(f"🌐 Health Check Web Server started on port {port}")
    server.serve_forever()

threading.Thread(target=run_health_check, daemon=True).start()

# ======================================
# CONFIG & TELEGRAM SETTINGS
# ======================================
BOT_TOKEN = "8943363652:AAHta2mpz7EQYxeVd1vwtvW7ZiqhH0F17B0"  # আপনার আসল Bot Token দিন
CHAT_ID = "-1004379065547"              # আপনার Telegram Chat ID দিন

PAIRS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT"]
TIMEFRAME = "1m"
LIMIT = 150
SIGNAL_COOLDOWN = 45  # কুলডাউন কমিয়ে ৪৫ সেকেন্ড করা হয়েছে

request = HTTPXRequest(connect_timeout=60, read_timeout=60)
bot = Bot(token=BOT_TOKEN, request=request)

# ======================================
# DATABASE SYSTEM
# ======================================
conn = sqlite3.connect("signals.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair TEXT,
    signal TEXT,
    confidence INTEGER,
    result TEXT DEFAULT 'PENDING',
    price REAL,
    time TEXT
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS stats (
    id INTEGER PRIMARY KEY,
    total_profit REAL DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0
)
""")
cursor.execute("INSERT OR IGNORE INTO stats (id) VALUES (1)")
conn.commit()

# ======================================
# MARKET DATA & INDICATORS
# ======================================
def get_market_data(symbol):
    urls = [
        f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={TIMEFRAME}&limit={LIMIT}",
        f"https://api1.binance.com/api/v3/klines?symbol={symbol}&interval={TIMEFRAME}&limit={LIMIT}",
        f"https://api2.binance.com/api/v3/klines?symbol={symbol}&interval={TIMEFRAME}&limit={LIMIT}",
        f"https://data-api.binance.vision/api/v3/klines?symbol={symbol}&interval={TIMEFRAME}&limit={LIMIT}"
    ]
    
    headers = {'User-Agent': 'Mozilla/5.0'}

    for url in urls:
        try:
            res = requests.get(url, headers=headers, timeout=8)
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, list) and len(data) > 0:
                    opens = [float(c[1]) for c in data]
                    highs = [float(c[2]) for c in data]
                    lows = [float(c[3]) for c in data]
                    closes = [float(c[4]) for c in data]
                    return opens, highs, lows, closes
        except Exception:
            continue

    return None, None, None, None

def calculate_ema(prices, period):
    if len(prices) < period: return 0
    multiplier = 2 / (period + 1)
    ema_value = sum(prices[:period]) / period
    for price in prices[period:]:
        ema_value = (price - ema_value) * multiplier + ema_value
    return ema_value

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1: return 50
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = prices[-i] - prices[-i - 1]
        if diff >= 0: gains.append(diff)
        else: losses.append(abs(diff))
    avg_gain = sum(gains) / period if gains else 0.01
    avg_loss = sum(losses) / period if losses else 0.01
    return 100 - (100 / (1 + (avg_gain / avg_loss)))

def calculate_stochastic(closes):
    if len(closes) < 14: return 50
    recent = closes[-14:]
    highest, lowest = max(recent), min(recent)
    if highest == lowest: return 50
    return ((closes[-1] - lowest) / (highest - lowest)) * 100

def calculate_momentum(closes):
    return closes[-1] - closes[-10] if len(closes) >= 10 else 0

def get_support_resistance(highs, lows):
    return (min(lows[-20:]), max(highs[-20:])) if len(highs) >= 20 else (0, 0)

def get_candle_pattern(opens, closes, highs, lows):
    if not opens: return "NEUTRAL"
    o, c, h, l = opens[-1], closes[-1], highs[-1], lows[-1]
    body, wick = abs(c - o), h - l
    if c > o and wick > body * 1.2: return "BULLISH"
    if o > c and wick > body * 1.2: return "BEARISH"
    return "NEUTRAL"

# ======================================
# DB HELPERS & STATS
# ======================================
def save_signal(pair, signal, confidence, price):
    cursor.execute(
        "INSERT INTO signals (pair, signal, confidence, price, time) VALUES (?, ?, ?, ?, ?)",
        (pair, signal, confidence, price, str(datetime.now()))
    )
    conn.commit()
    return cursor.lastrowid

def update_result(signal_id, is_win):
    cursor.execute("SELECT total_profit, wins, losses FROM stats WHERE id=1")
    row = cursor.fetchone()
    profit, wins, losses = row[0], row[1], row[2]

    if is_win:
        wins += 1
        profit += 0.85
        res_str = "WIN"
    else:
        losses += 1
        profit -= 1.0
        res_str = "LOSS"

    cursor.execute("UPDATE signals SET result=? WHERE id=?", (res_str, signal_id))
    cursor.execute("UPDATE stats SET total_profit=?, wins=?, losses=? WHERE id=1", (profit, wins, losses))
    conn.commit()

def get_stats():
    cursor.execute("SELECT total_profit, wins, losses FROM stats WHERE id=1")
    row = cursor.fetchone()
    return {"profit": round(row[0], 2), "wins": row[1], "losses": row[2]}

async def check_trade_result(pair, signal, entry_price, signal_id):
    await asyncio.sleep(62)
    _, _, _, closes = get_market_data(pair)
    if closes:
        exit_price = closes[-1]
        is_win = (("UP" in signal and exit_price > entry_price) or 
                  ("DOWN" in signal and exit_price < entry_price))
        update_result(signal_id, is_win)

# ======================================
# FAST / RAW SIGNAL STRATEGY
# ======================================
def analyze_market(pair):
    opens, highs, lows, closes = get_market_data(pair)
    if closes is None or len(closes) < 50:
        return None

    price = closes[-1]
    ema20 = calculate_ema(closes, 20)
    rsi = calculate_rsi(closes)
    stoch = calculate_stochastic(closes)
    mom = calculate_momentum(closes)
    sup, res = get_support_resistance(highs, lows)
    pattern = get_candle_pattern(opens, closes, highs, lows)

    up_points, down_points = 0, 0

    # UP Conditions (সহজ করা হয়েছে)
    if price > ema20: up_points += 1
    if rsi < 50: up_points += 1
    if stoch < 45: up_points += 1
    if mom > 0: up_points += 1

    # DOWN Conditions (সহজ করা হয়েছে)
    if price < ema20: down_points += 1
    if rsi > 50: down_points += 1
    if stoch > 55: down_points += 1
    if mom < 0: down_points += 1

    signal = None
    confidence = 0

    # মাত্র ২টি পয়েন্ট মিললেই দ্রুত সিগন্যাল পাঠাবে
    if up_points >= 2:
        signal = "UP 🟢"
        confidence = 80
    elif down_points >= 2:
        signal = "DOWN 🔴"
        confidence = 80

    if not signal:
        return None

    return {
        "pair": pair, "signal": signal, "confidence": confidence,
        "rsi": round(rsi, 2), "stochastic": round(stoch, 2),
        "momentum": round(mom, 4 if "DOGE" in pair or "ADA" in pair else 2),
        "pattern": pattern, "support": round(sup, 4), "resistance": round(res, 4), "price": price
    }

# ======================================
# TELEGRAM NOTIFIER
# ======================================
async def send_telegram_signal(data, signal_id):
    now = datetime.now()
    entry_time = now + timedelta(minutes=1)
    stats = get_stats()

    msg = f"""📊 ADVANCED QUOTEX SIGNAL

🆔 Signal ID: {signal_id}
💹 Pair: {data['pair']}
🚀 Signal: {data['signal']}
🎯 Confidence: {data['confidence']}%

📈 RSI: {data['rsi']}
📉 Stochastic: {data['stochastic']}
⚡ Momentum: {data['momentum']}
🕯 Pattern: {data['pattern']}

🟢 Support: {data['support']}
🔴 Resistance: {data['resistance']}

💰 Martingale Amount: $1
📊 Total Profit: ${stats['profit']}
🏆 Wins: {stats['wins']} | ❌ Losses: {stats['losses']}

⏰ Timeframe: 1 Minute
🕒 Entry Time: {entry_time.strftime("%I:%M %p")}

👨‍💻 Developer: Taohid Islam Tahosin"""

    try:
        await bot.send_message(chat_id=CHAT_ID, text=msg)
        print(f"🚀 FAST SIGNAL: Sent {data['pair']} -> {data['signal']}")
    except Exception as e:
        print(f"❌ Telegram Send Error: {e}")

# ======================================
# MAIN LOOP
# ======================================
async def main():
    print("🤖 Bot Scanning Markets Active (Fast Raw Mode)...")
    last_signal_time = {}

    while True:
        try:
            for pair in PAIRS:
                data = analyze_market(pair)
                if data:
                    curr_time = time.time()
                    if curr_time - last_signal_time.get(pair, 0) >= SIGNAL_COOLDOWN:
                        signal_id = save_signal(pair, data['signal'], data['confidence'], data['price'])
                        await send_telegram_signal(data, signal_id)
                        asyncio.create_task(check_trade_result(pair, data['signal'], data['price'], signal_id))
                        last_signal_time[pair] = curr_time
                        await asyncio.sleep(1)
        except Exception as e:
            print(f"Main Loop Error: {e}")

        await asyncio.sleep(5)  # প্রতি ৫ সেকেন্ডে স্ক্যান করবে

if __name__ == "__main__":
    asyncio.run(main())
