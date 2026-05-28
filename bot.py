import logging
import requests
import statistics
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import schedule
import time
import threading

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHANNEL_ID = "@PipAlertProSignals"
TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ASSETS = {
    "XAU/USD": {"name": "Gold", "emoji": "🥇"},
    "BTC/USD": {"name": "Bitcoin", "emoji": "₿"},
    "ETH/USD": {"name": "Ethereum", "emoji": "💎"},
}

user_profiles = {}

def get_forex_data(pair):
    url = "https://api.twelvedata.com/time_series"
    params = {"symbol": pair, "interval": "15min", "outputsize": 60, "apikey": TWELVEDATA_API_KEY}
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
    return round(macd, 6), round(macd * 0.9, 6)

def calculate_bollinger(prices, period=20):
    if len(prices) < period:
        return prices[0], prices[0], prices[0]
    sl = prices[:period]
    mean = sum(sl) / period
    std = statistics.stdev(sl)
    return round(mean + 2*std, 2), round(mean, 2), round(mean - 2*std, 2)

def get_signal(pair):
    data = get_forex_data(pair)
    if not data or len(data) < 30:
        return None
    closes = [float(d["close"]) for d in data]
    current = closes[0]
    rsi = calculate_rsi(closes)
    macd, signal_line = calculate_macd(closes)
    ma20 = sum(closes[:20]) / 20
    ma50 = sum(closes[:50]) / 50 if len(closes) >= 50 else sum(closes) / len(closes)
    bb_upper, bb_mid, bb_lower = calculate_bollinger(closes)
    score = 0
    reasons = []
    if rsi < 30:
        score += 3
        reasons.append(f"RSI {rsi} — Heavily Oversold")
    elif rsi < 40:
        score += 2
        reasons.append(f"RSI {rsi} — Oversold Zone")
    elif rsi > 70:
        score -= 3
        reasons.append(f"RSI {rsi} — Heavily Overbought")
    elif rsi > 60:
        score -= 2
        reasons.append(f"RSI {rsi} — Overbought Zone")
    else:
        reasons.append(f"RSI {rsi} — Neutral")
    if macd > signal_line and macd > 0:
        score += 2
        reasons.append("MACD — Strong Bullish")
    elif macd > signal_line:
        score += 1
        reasons.append("MACD — Bullish Crossover")
    elif macd < signal_line and macd < 0:
        score -= 2
        reasons.append("MACD — Strong Bearish")
    else:
        score -= 1
        reasons.append("MACD — Bearish Crossover")
    if current > ma20 > ma50:
        score += 2
        reasons.append("MA — Strong Uptrend")
    elif current > ma20:
        score += 1
        reasons.append("MA — Mild Uptrend")
    elif current < ma20 < ma50:
        score -= 2
        reasons.append("MA — Strong Downtrend")
    else:
        score -= 1
        reasons.append("MA — Mild Downtrend")
    if current <= bb_lower:
        score += 2
        reasons.append("Bollinger — Support Level")
    elif current >= bb_upper:
        score -= 2
        reasons.append("Bollinger — Resistance Level")
    if score >= 2:
        direction = "BUY"
        confidence = min(92, 55 + score * 5)
        pip = current * 0.003
        sl = round(current - pip, 2)
        tp1 = round(current + pip * 1.5, 2)
        tp2 = round(current + pip * 3, 2)
    elif score <= -2:
        direction = "SELL"
        confidence = min(92, 55 + abs(score) * 5)
        pip = current * 0.003
        sl = round(current + pip, 2)
        tp1 = round(current - pip * 1.5, 2)
        tp2 = round(current - pip * 3, 2)
    else:
        return None
    return {"pair": pair, "direction": direction, "price": round(current, 2),
            "sl": sl, "tp1": tp1, "tp2": tp2, "rsi": rsi,
            "confidence": confidence, "reasons": reasons[:4], "score": score}

def format_signal(signal, asset_info, user_level="intermediate"):
    now = datetime.now().strftime('%d %b | %H:%M')
    stars = "⭐" * (signal['confidence'] // 20)
    emoji = "🟢" if signal['direction'] == "BUY" else "🔴"
    if user_level == "beginner":
        explanation = "📝 Simple Guide:\n- Entry pe buy/sell karo\n- Stop Loss zaroor lagao\n- Max 1-2% capital lagao"
    elif user_level == "expert":
        explanation = f"📊 Technical:\n- Score: {signal['score']}/10\n- RSI: {signal['rsi']}"
    else:
        explanation = "Always use Stop Loss. Max 1-2% risk per trade."
    msg = f"""{emoji} <b>{signal['direction']} — {asset_info['emoji']} {asset_info['name']}</b>
━━━━━━━━━━━━━━━━━━━━
💰 <b>Entry:</b> <code>${signal['price']}</code>
🛑 <b>Stop Loss:</b> <code>${signal['sl']}</code>
🎯 <b>TP1:</b> <code>${signal['tp1']}</code>
🎯 <b>TP2:</b> <code>${signal['tp2']}</code>
📊 <b>Analysis:</b>
{chr(10).join(['- ' + r for r in signal['reasons']])}
{explanation}
💪 <b>Confidence:</b> {signal['confidence']}% {stars}
🕐 <b>Time:</b> {now}
━━━━━━━━━━━━━━━━━━━━
🚀 @PipAlertProSignals"""
    return msg

def send_to_channel(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHANNEL_ID, "text": message, "parse_mode": "HTML"}
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.json()
    except:
        return None

last_signals = {}

def check_and_send_signals():
    print(f"[{datetime.now().strftime('%H:%M')}] Scanning assets...")
    sent = 0
    for pair, asset_info in ASSETS.items():
        signal = get_signal(pair)
        if signal:
            key = f"{pair}_{signal['direction']}"
            if time.time() - last_signals.get(key, 0) < 3600:
                continue
            msg = format_signal(signal, asset_info)
            result = send_to_channel(msg)
            if result and result.get("ok"):
                print(f"Signal sent: {asset_info['name']} {signal['direction']}")
                last_signals[key] = time.time()
                sent += 1
            time.sleep(2)
        time.sleep(1)
    if sent == 0:
        print("No strong signals this round.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    name = update.effective_user.first_name
    keyboard = [
        [InlineKeyboardButton("🟢 Beginner", callback_data="level_beginner"),
         InlineKeyboardButton("🔵 Intermediate", callback_data="level_intermediate"),
         InlineKeyboardButton("🔴 Expert", callback_data="level_expert")]
    ]
    await update.message.reply_text(
        f"👋 <b>Welcome {name}!</b>\n\n🚀 <b>PipAlert Pro</b>\n\nApna experience batao:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    if user_id not in user_profiles:
        user_profiles[user_id] = {}
    if data.startswith("level_"):
        level = data.replace("level_", "")
        user_profiles[user_id]["level"] = level
        keyboard = [
            [InlineKeyboardButton("😊 Low", callback_data="risk_low"),
             InlineKeyboardButton("⚡ Medium", callback_data="risk_medium"),
             InlineKeyboardButton("🔥 High", callback_data="risk_high")]
        ]
        await query.edit_message_text(
            f"✅ Level: <b>{level.capitalize()}</b>\n\nRisk tolerance?",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data.startswith("risk_"):
        risk = data.replace("risk_", "")
        user_profiles[user_id]["risk"] = risk
        keyboard = [
            [InlineKeyboardButton("💵 $100-500", callback_data="invest_small"),
             InlineKeyboardButton("💰 $500-2000", callback_data="invest_medium"),
             InlineKeyboardButton("🏦 $2000+", callback_data="invest_large")]
        ]
        await query.edit_message_text(
            f"✅ Risk: <b>{risk.capitalize()}</b>\n\nInvestment amount?",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data.startswith("invest_"):
        invest = data.replace("invest_", "")
        user_profiles[user_id]["invest"] = invest
        keyboard = [
            [InlineKeyboardButton("🥇 Gold", callback_data="asset_gold"),
             InlineKeyboardButton("₿ Bitcoin", callback_data="asset_btc"),
             InlineKeyboardButton("💎 Ethereum", callback_data="asset_eth")],
            [InlineKeyboardButton("📊 Sab Assets", callback_data="asset_all")]
        ]
        await query.edit_message_text(
            f"✅ Investment: <b>{invest.capitalize()}</b>\n\nAsset choose karo:",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data.startswith("asset_"):
        asset = data.replace("asset_", "")
        user_profiles[user_id]["asset"] = asset
        profile = user_profiles[user_id]
        await query.edit_message_text(
            f"🎉 <b>Profile Complete!</b>\n\n"
            f"Level: {profile.get('level','').capitalize()}\n"
            f"Risk: {profile.get('risk','').capitalize()}\n"
            f"Asset: {asset.capitalize()}\n\n"
            f"✅ Signals shuru!\n\n"
            f"/signals — Latest signals\n"
            f"/help — Help\n\n"
            f"🚀 @PipAlertProSignals",
            parse_mode="HTML")

async def signals_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    profile = user_profiles.get(user_id, {})
    level = profile.get("level", "intermediate")
    await update.message.reply_text("⏳ Checking signals...", parse_mode="HTML")
    found = False
    for pair, asset_info in ASSETS.items():
        signal = get_signal(pair)
        if signal:
            msg = format_signal(signal, asset_info, level)
            await update.message.reply_text(msg, parse_mode="HTML")
            found = True
            time.sleep(1)
    if not found:
        await update.message.reply_text(
            "📊 <b>Abhi koi strong signal nahi hai.</b>\n\n15 minute mein check karo!\n\n🚀 @PipAlertProSignals",
            parse_mode="HTML")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 <b>PipAlert Pro Help</b>\n\n/start — Setup\n/signals — Signals\n/help — Help\n\n"
        "🟢 BUY — Khareeedo\n🔴 SELL — Becho\n🎯 TP — Target\n🛑 SL — Stop Loss\n\n"
        "⚠️ Max 1-2% risk per trade!\n\n🚀 @PipAlertProSignals",
        parse_mode="HTML")

def run_scheduler():
    schedule.every(15).minutes.do(check_and_send_signals)
    while True:
        schedule.run_pending()
        time.sleep(30)

def main():
    print("PipAlert Pro — Interactive Bot v3.0")
    check_and_send_signals()
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    app = Application.builder().token(TELEGRAM_TOKEN).job_queue(None).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signals", signals_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    print("Bot running! Press Ctrl+C to stop")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
