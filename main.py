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
SIGNAL_COOLDOWN = 180  # একই পেয়ারে ৩ মিনিট কুলডাউন (সেফ ট্রেডিংয়ের জন্য)

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
    try:
        urls = [
            f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={TIMEFRAME}&limit={LIMIT}",
            f"https://api1.binance.com/api/v3/klines?symbol={symbol}&interval={TIMEFRAME}&limit={LIMIT}",
            f"https://api2.binance.com/api/v3/klines?symbol={symbol}&interval={TIMEFRAME}&limit={LIMIT}"
        ]
        headers = {'User-Agent': 'Mozilla/5.0'}

        for url in urls:
            try:
                res = requests.get(url, headers=headers, timeout=10).json()
                if isinstance(res, list) and len(res) > 0:
                    opens = [float(c[1]) for c in res]
                    highs = [float(c[2]) for c in res]
                    lows = [float(c[3]) for c in res]
                    closes = [float(c[4]) for c in res]
                    return opens, highs, lows, closes
            except Exception:
                continue
        return None, None, None, None
    except Exception as e:
        print(f"Error fetching {symbol}: {e}")
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
    if c > o and wick > body * 1.8: return "BULLISH"
    if o > c and wick > body * 1.8: return "BEARISH"
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
# HIGH-ACCURACY STRATEGY ANALYSIS
# ======================================
def analyze_market(pair):
    opens, highs, lows, closes = get_market_data(pair)
    if closes is None or len(closes) < 50:
        return None

    price = closes[-1]
    ema20, ema50 = calculate_ema(closes, 20), calculate_ema(closes, 50)
    rsi = calculate_rsi(closes)
    stoch = calculate_stochastic(closes)
    mom = calculate_momentum(closes)
    sup, res = get_support_resistance(highs, lows)
    pattern = get_candle_pattern(opens, closes, highs, lows)

    up_points, down_points = 0, 0

    # UP Strategy Conditions
    if price > ema20: up_points += 1
    if ema20 > ema50: up_points += 1
    if rsi < 38: up_points += 1
    if stoch < 25: up_points += 1
    if mom > 0: up_points += 1
    if pattern == "BULLISH": up_points += 1

    # DOWN Strategy Conditions
    if price < ema20: down_points += 1
    if ema20 < ema50: down_points += 1
    if rsi > 62: down_points += 1
    if stoch > 75: down_points += 1
    if mom < 0: down_points += 1
    if pattern == "BEARISH": down_points += 1

    signal = None
    confidence = 0

    # স্ট্রিক্ট ফিল্টার: ৪ বা তার বেশি কনফর্মেশন থাকলে তবেই সিগন্যাল
    if up_points >= 4:
        signal = "UP 🟢"
        confidence = min(95, 70 + (up_points * 5))
    elif down_points >= 4:
        signal = "DOWN 🔴"
        confidence = min(95, 70 + (down_points * 5))

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
        print(f"🚀 SUCCESS: High Accuracy Signal Sent -> {data['pair']} [{data['signal']}]")
    except Exception as e:
        print(f"❌ Telegram Send Error: {e}")

# ======================================
# MAIN LOOP
# ======================================
async def main():
    print("🤖 Bot Scanning Markets Active (High Accuracy Mode)...")
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
                        await asyncio.sleep(2)
        except Exception as e:
            print(f"Main Loop Error: {e}")

        await asyncio.sleep(12)

if __name__ == "__main__":
    asyncio.run(main())
