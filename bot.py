import requests
import schedule
import time
from datetime import datetime
import statistics

# ============================================
# APNI DETAILS
# ============================================
TELEGRAM_TOKEN = "8696123868:AAGeNnJY9jZjm1FFgwzfQoAVTL8XP4hN1V8"
CHANNEL_ID = "@PipAlertProSignals"
TWELVEDATA_API_KEY = "2cdbcf9285b4490eb9cc69a0db45fe9c"
# ============================================

PAIRS = ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD", "NZD/USD", "USD/CHF"]

last_signals = {}

def get_market_session():
    hour = datetime.utcnow().hour
    if 8 <= hour < 12:
        return "🇬🇧 London Session", "HIGH"
    elif 13 <= hour < 17:
        return "🇺🇸 New York Session", "HIGH"
    elif 12 <= hour < 13:
        return "⚡ London-NY Overlap", "VERY HIGH"
    elif 0 <= hour < 8:
        return "🇯🇵 Tokyo Session", "MEDIUM"
    else:
        return "🌙 Off Session", "LOW"

def get_forex_data(pair):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": pair,
        "interval": "15min",
        "outputsize": 60,
        "apikey": TWELVEDATA_API_KEY
    }
    try:
        response = requests.get(url, params=params, timeout=10)
        data = response.json()
        if "values" in data:
            return data["values"]
        return None
    except:
        return None

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = prices[i-1] - prices[i]
        if diff > 0:
            gains.append(diff)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(diff))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def calculate_ema(prices, period):
    if len(prices) < period:
        return prices[0]
    multiplier = 2 / (period + 1)
    ema = sum(prices[-period:]) / period
    for price in reversed(prices[:-period]):
        ema = (price - ema) * multiplier + ema
    return round(ema, 5)

def calculate_macd(prices):
    if len(prices) < 26:
        return 0, 0
    ema12 = calculate_ema(prices, 12)
    ema26 = calculate_ema(prices, 26)
    macd = ema12 - ema26
    signal = macd * 0.9
    return round(macd, 6), round(signal, 6)

def calculate_bollinger(prices, period=20):
    if len(prices) < period:
        return prices[0], prices[0], prices[0]
    slice_prices = prices[:period]
    mean = sum(slice_prices) / period
    std = statistics.stdev(slice_prices)
    upper = mean + (2 * std)
    lower = mean - (2 * std)
    return round(upper, 5), round(mean, 5), round(lower, 5)

def get_signal(pair):
    data = get_forex_data(pair)
    if not data or len(data) < 30:
        return None

    closes = [float(d["close"]) for d in data]
    highs = [float(d["high"]) for d in data]
    lows = [float(d["low"]) for d in data]
    current_price = closes[0]

    rsi = calculate_rsi(closes)
    macd, signal_line = calculate_macd(closes)
    ma20 = sum(closes[:20]) / 20
    ma50 = sum(closes[:50]) / 50
    ema9 = calculate_ema(closes[:20], 9)
    bb_upper, bb_mid, bb_lower = calculate_bollinger(closes)

    score = 0
    reasons = []

    # RSI Analysis
    if rsi < 30:
        score += 3
        reasons.append(f"RSI {rsi} 🔥 Heavily Oversold")
    elif rsi < 40:
        score += 2
        reasons.append(f"RSI {rsi} — Oversold Zone")
    elif rsi > 70:
        score -= 3
        reasons.append(f"RSI {rsi} 🔥 Heavily Overbought")
    elif rsi > 60:
        score -= 2
        reasons.append(f"RSI {rsi} — Overbought Zone")
    else:
        reasons.append(f"RSI {rsi} — Neutral")

    # MACD Analysis
    if macd > signal_line and macd > 0:
        score += 2
        reasons.append("MACD ✅ Strong Bullish")
    elif macd > signal_line:
        score += 1
        reasons.append("MACD ↗️ Bullish Crossover")
    elif macd < signal_line and macd < 0:
        score -= 2
        reasons.append("MACD ❌ Strong Bearish")
    else:
        score -= 1
        reasons.append("MACD ↘️ Bearish Crossover")

    # Moving Average
    if current_price > ma20 > ma50:
        score += 2
        reasons.append("MA ✅ Strong Uptrend")
    elif current_price > ma20:
        score += 1
        reasons.append("MA ↗️ Mild Uptrend")
    elif current_price < ma20 < ma50:
        score -= 2
        reasons.append("MA ❌ Strong Downtrend")
    else:
        score -= 1
        reasons.append("MA ↘️ Mild Downtrend")

    # Bollinger Bands
    if current_price <= bb_lower:
        score += 2
        reasons.append("BB 🎯 Price at Lower Band (BUY Zone)")
    elif current_price >= bb_upper:
        score -= 2
        reasons.append("BB 🎯 Price at Upper Band (SELL Zone)")
    else:
        reasons.append(f"BB — Price in Middle Zone")

    # EMA9
    if current_price > ema9:
        score += 1
        reasons.append("EMA9 ✅ Bullish")
    else:
        score -= 1
        reasons.append("EMA9 ❌ Bearish")

    # Determine signal
    if score >= 4:
        direction = "BUY"
        emoji = "🟢"
        confidence = min(92, 55 + score * 5)
        pip_distance = current_price * 0.0025
        stop_loss = round(current_price - pip_distance, 5)
        take_profit1 = round(current_price + pip_distance * 1.5, 5)
        take_profit2 = round(current_price + pip_distance * 3, 5)
    elif score <= -4:
        direction = "SELL"
        emoji = "🔴"
        confidence = min(92, 55 + abs(score) * 5)
        pip_distance = current_price * 0.0025
        stop_loss = round(current_price + pip_distance, 5)
        take_profit1 = round(current_price - pip_distance * 1.5, 5)
        take_profit2 = round(current_price - pip_distance * 3, 5)
    else:
        return None

    return {
        "pair": pair,
        "direction": direction,
        "emoji": emoji,
        "price": round(current_price, 5),
        "stop_loss": stop_loss,
        "take_profit1": take_profit1,
        "take_profit2": take_profit2,
        "rsi": rsi,
        "confidence": confidence,
        "reasons": reasons[:4],
        "score": score
    }

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHANNEL_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.json()
    except Exception as e:
        print(f"Error: {e}")
        return None

def format_signal_message(signal):
    session_name, session_strength = get_market_session()
    now = datetime.now().strftime('%d %b %Y | %H:%M')

    stars = "⭐" * (signal['confidence'] // 20)

    msg = f"""
╔══════════════════════╗
   {signal['emoji']} <b>{signal['direction']} SIGNAL</b>
╚══════════════════════╝

💱 <b>Pair:</b> {signal['pair']}
💰 <b>Entry Price:</b> <code>{signal['price']}</code>
🛑 <b>Stop Loss:</b> <code>{signal['stop_loss']}</code>
🎯 <b>TP 1:</b> <code>{signal['take_profit1']}</code>
🎯 <b>TP 2:</b> <code>{signal['take_profit2']}</code>

📊 <b>Analysis:</b>
{chr(10).join(['▫️ ' + r for r in signal['reasons']])}

💪 <b>Confidence:</b> {signal['confidence']}% {stars}
📍 <b>Session:</b> {session_name}
⚡ <b>Volatility:</b> {session_strength}
⏰ <b>Timeframe:</b> 15 Minutes
🕐 <b>Time:</b> {now}

⚠️ <i>Always use proper risk management. Never risk more than 1-2% per trade.</i>
━━━━━━━━━━━━━━━━━━━━━
🚀 <b>@PipAlertProSignals</b>
"""
    return msg

def send_signals():
    print(f"\n[{datetime.now().strftime('%H:%M')}] Scanning {len(PAIRS)} pairs...")
    session_name, _ = get_market_session()
    print(f"Session: {session_name}")
    signals_sent = 0

    for pair in PAIRS:
        signal = get_signal(pair)
        if signal:
            pair_key = f"{pair}_{signal['direction']}"
            last_time = last_signals.get(pair_key, 0)
            if time.time() - last_time < 3600:
                print(f"{pair}: Signal already sent recently, skipping...")
                continue

            print(f"🔥 SIGNAL FOUND: {pair} — {signal['direction']} (Score: {signal['score']}, Confidence: {signal['confidence']}%)")
            message = format_signal_message(signal)
            result = send_telegram_message(message)
            if result and result.get("ok"):
                print(f"✅ Signal sent for {pair}!")
                last_signals[pair_key] = time.time()
                signals_sent += 1
            time.sleep(2)
        else:
            print(f"{pair}: No strong signal")
        time.sleep(1)

    if signals_sent == 0:
        print("No strong signals this round — waiting...")
    else:
        print(f"\n✅ {signals_sent} signal(s) sent!")

def send_startup_message():
    session_name, session_strength = get_market_session()
    msg = f"""
🚀 <b>PipAlert Pro Forex Signals</b>
<b>━━━━━━━━━━━━━━━━━━━━━</b>

✅ <b>Bot is LIVE and Running!</b>

📊 <b>Monitoring Pairs:</b>
EUR/USD | GBP/USD | USD/JPY
AUD/USD | USD/CAD | NZD/USD | USD/CHF

⚙️ <b>Analysis Method:</b>
▫️ RSI (Relative Strength Index)
▫️ MACD (Moving Average Convergence)
▫️ Bollinger Bands
▫️ EMA 9, MA 20, MA 50

⏰ <b>Signal Frequency:</b> Every 15 Minutes
📍 <b>Current Session:</b> {session_name}
⚡ <b>Market Activity:</b> {session_strength}

⚠️ <i>Trade responsibly. Past signals do not guarantee future results.</i>
━━━━━━━━━━━━━━━━━━━━━
🚀 <b>@PipAlertProSignals</b>
"""
    send_telegram_message(msg)
    print("✅ Startup message sent!")

if __name__ == "__main__":
    print("=" * 50)
    print("  PipAlert Pro — Professional Forex Bot v2.0")
    print("=" * 50)
    print("Bot starting...")

    send_startup_message()
    send_signals()

    schedule.every(15).minutes.do(send_signals)

    print("\n✅ Bot is running! Signals every 15 minutes.")
    print("Press Ctrl+C to stop.\n")

    while True:
        schedule.run_pending()
        time.sleep(30)
