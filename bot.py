import os
import logging
import requests
import statistics
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, MenuButtonCommands
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
signal_history = []  # Win/Loss tracking

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
    if len(prices) < 35:
        return 0, 0
    ema12 = calculate_ema(prices, 12)
    ema26 = calculate_ema(prices, 26)
    macd_line = ema12 - ema26
    macd_values = []
    for i in range(9):
        subset = prices[i:]
        e12 = calculate_ema(subset, 12)
        e26 = calculate_ema(subset, 26)
        macd_values.append(e12 - e26)
    signal_line = sum(macd_values) / len(macd_values)
    return round(macd_line, 6), round(signal_line, 6)

def calculate_bollinger(prices, period=20):
    if len(prices) < period:
        return prices[0], prices[0], prices[0]
    sl = prices[:period]
    mean = sum(sl) / period
    std = statistics.stdev(sl)
    return round(mean + 2*std, 2), round(mean, 2), round(mean - 2*std, 2)

def get_signal(pair):
    data = get_forex_data(pair)
    if not data or len(data) < 35:
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
        reasons.append(f"RSI {rsi} — Heavily Oversold 🔥")
    elif rsi < 40:
        score += 2
        reasons.append(f"RSI {rsi} — Oversold Zone")
    elif rsi > 70:
        score -= 3
        reasons.append(f"RSI {rsi} — Heavily Overbought 🔥")
    elif rsi > 60:
        score -= 2
        reasons.append(f"RSI {rsi} — Overbought Zone")
    else:
        reasons.append(f"RSI {rsi} — Neutral")
    if macd > signal_line and macd > 0:
        score += 2
        reasons.append("MACD — Strong Bullish 📈")
    elif macd > signal_line:
        score += 1
        reasons.append("MACD — Bullish Crossover")
    elif macd < signal_line and macd < 0:
        score -= 2
        reasons.append("MACD — Strong Bearish 📉")
    else:
        score -= 1
        reasons.append("MACD — Bearish Crossover")
    if current > ma20 > ma50:
        score += 2
        reasons.append("MA — Strong Uptrend ⬆️")
    elif current > ma20:
        score += 1
        reasons.append("MA — Mild Uptrend")
    elif current < ma20 < ma50:
        score -= 2
        reasons.append("MA — Strong Downtrend ⬇️")
    else:
        score -= 1
        reasons.append("MA — Mild Downtrend")
    if current <= bb_lower:
        score += 2
        reasons.append("Bollinger — Strong Support 💪")
    elif current >= bb_upper:
        score -= 2
        reasons.append("Bollinger — Strong Resistance 🛑")
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
    now = datetime.now().strftime('%d %b %Y | %H:%M UTC')
    confidence = signal['confidence']
    if confidence >= 80:
        conf_bar = "🟩🟩🟩🟩🟩"
        conf_text = "Very Strong"
    elif confidence >= 70:
        conf_bar = "🟩🟩🟩🟩⬜"
        conf_text = "Strong"
    elif confidence >= 60:
        conf_bar = "🟩🟩🟩⬜⬜"
        conf_text = "Moderate"
    else:
        conf_bar = "🟩🟩⬜⬜⬜"
        conf_text = "Weak"

    emoji = "🟢" if signal['direction'] == "BUY" else "🔴"
    action = "📈 LONG" if signal['direction'] == "BUY" else "📉 SHORT"

    if user_level == "beginner":
        guide = "📌 <b>Beginner Guide:</b>\n• Entry price pe order lagao\n• Stop Loss zaroor set karo\n• Max 1-2% capital use karo"
    elif user_level == "expert":
        guide = f"📌 <b>Expert Data:</b>\n• Score: {signal['score']}/10\n• RSI: {signal['rsi']}\n• Risk:Reward = 1:1.5"
    else:
        guide = "📌 Always use Stop Loss. Max 1-2% risk per trade."

    msg = f"""
{emoji} <b>PipAlert Pro Signal</b> {emoji}
━━━━━━━━━━━━━━━━━━━━━━
{action} — {asset_info['emoji']} <b>{asset_info['name']}</b>
━━━━━━━━━━━━━━━━━━━━━━
💰 <b>Entry Price:</b>  <code>${signal['price']}</code>
🛑 <b>Stop Loss:</b>    <code>${signal['sl']}</code>
🎯 <b>Target 1:</b>     <code>${signal['tp1']}</code>
🎯 <b>Target 2:</b>     <code>${signal['tp2']}</code>
━━━━━━━━━━━━━━━━━━━━━━
📊 <b>Technical Analysis:</b>
{chr(10).join(['  • ' + r for r in signal['reasons']])}
━━━━━━━━━━━━━━━━━━━━━━
💪 <b>Signal Strength:</b> {conf_bar}
📈 <b>Confidence:</b> {confidence}% — {conf_text}
{guide}
━━━━━━━━━━━━━━━━━━━━━━
🕐 {now}
⚠️ <i>For educational purposes only. Trade at your own risk.</i>
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
                signal_history.append({
                    "pair": pair,
                    "direction": signal['direction'],
                    "confidence": signal['confidence'],
                    "time": datetime.now().strftime('%d %b | %H:%M')
                })
                sent += 1
            time.sleep(2)
        time.sleep(1)
    if sent == 0:
        print("No strong signals this round.")

async def setup_commands(app):
    commands = [
        BotCommand("start", "🚀 Bot start karo"),
        BotCommand("signals", "📊 Latest signals dekho"),
        BotCommand("performance", "🏆 Signal performance"),
        BotCommand("stats", "📈 Bot statistics"),
        BotCommand("help", "❓ Help & Guide"),
        BotCommand("about", "ℹ️ Bot ke baare mein"),
    ]
    await app.bot.set_my_commands(commands)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name
    user_id = update.effective_user.id
    if user_id not in user_profiles:
        user_profiles[user_id] = {}

    keyboard = [
        [InlineKeyboardButton("📊 Live Signals", callback_data="get_signals"),
         InlineKeyboardButton("🏆 Performance", callback_data="show_performance")],
        [InlineKeyboardButton("🟢 Beginner", callback_data="level_beginner"),
         InlineKeyboardButton("🔵 Intermediate", callback_data="level_intermediate"),
         InlineKeyboardButton("🔴 Expert", callback_data="level_expert")],
        [InlineKeyboardButton("📢 Join Channel", url="https://t.me/PipAlertProSignals")],
        [InlineKeyboardButton("❓ Help", callback_data="show_help")]
    ]

    welcome = f"""
🚀 <b>Welcome to PipAlert Pro!</b>
━━━━━━━━━━━━━━━━━━━━━━
👋 Hello <b>{name}</b>!

💹 <b>Kya milega aapko:</b>
  • 🥇 Gold (XAU/USD) Signals
  • ₿ Bitcoin (BTC/USD) Signals
  • 💎 Ethereum (ETH/USD) Signals
  • 📊 RSI + MACD + Bollinger Analysis
  • 🎯 Entry, Stop Loss & Take Profit

⚡ <b>Signals:</b> Har 15 minute mein scan
💪 <b>Accuracy:</b> Multi-indicator system
━━━━━━━━━━━━━━━━━━━━━━
👇 <b>Apna level choose karo:</b>"""

    await update.message.reply_text(
        welcome,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total = len(signal_history)
    buy_signals = len([s for s in signal_history if s['direction'] == 'BUY'])
    sell_signals = len([s for s in signal_history if s['direction'] == 'SELL'])
    avg_conf = round(sum([s['confidence'] for s in signal_history]) / total, 1) if total > 0 else 0

    msg = f"""
📈 <b>PipAlert Pro — Statistics</b>
━━━━━━━━━━━━━━━━━━━━━━
📊 <b>Total Signals:</b> {total}
🟢 <b>BUY Signals:</b> {buy_signals}
🔴 <b>SELL Signals:</b> {sell_signals}
💪 <b>Avg Confidence:</b> {avg_conf}%
━━━━━━━━━━━━━━━━━━━━━━
⏰ <b>Scan Frequency:</b> Every 15 min
📡 <b>Assets:</b> Gold, BTC, ETH
🔬 <b>Indicators:</b> RSI, MACD, MA, Bollinger
━━━━━━━━━━━━━━━━━━━━━━
🚀 @PipAlertProSignals"""

    await update.message.reply_text(msg, parse_mode="HTML")

async def performance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total = len(signal_history)
    if total == 0:
        await update.message.reply_text(
            "📊 <b>Abhi koi signal history nahi hai.</b>\n\nPehle /signals try karo!\n\n🚀 @PipAlertProSignals",
            parse_mode="HTML")
        return

    recent = signal_history[-5:] if len(signal_history) >= 5 else signal_history
    history_text = ""
    for s in reversed(recent):
        emoji = "🟢" if s['direction'] == "BUY" else "🔴"
        history_text += f"  {emoji} {s['pair']} {s['direction']} — {s['confidence']}% — {s['time']}\n"

    msg = f"""
🏆 <b>PipAlert Pro — Performance</b>
━━━━━━━━━━━━━━━━━━━━━━
📊 <b>Total Signals Today:</b> {total}

📋 <b>Recent Signals:</b>
{history_text}
━━━━━━━━━━━━━━━━━━━━━━
⚠️ <i>Past signals for reference only.</i>
🚀 @PipAlertProSignals"""

    await update.message.reply_text(msg, parse_mode="HTML")

async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = """
ℹ️ <b>PipAlert Pro — About</b>
━━━━━━━━━━━━━━━━━━━━━━
🤖 <b>Bot:</b> @PipAlert_Pro_bot
📢 <b>Channel:</b> @PipAlertProSignals

💹 <b>Supported Assets:</b>
  • 🥇 Gold (XAU/USD)
  • ₿ Bitcoin (BTC/USD)
  • 💎 Ethereum (ETH/USD)

🔬 <b>Analysis Method:</b>
  • RSI (Relative Strength Index)
  • MACD (Moving Avg Convergence)
  • Bollinger Bands
  • Moving Averages (MA20, MA50)

⚡ <b>Signal Frequency:</b> Every 15 minutes
📊 <b>Data Source:</b> TwelveData API
━━━━━━━━━━━━━━━━━━━━━━
⚠️ <i>Educational purposes only. Not financial advice.</i>
🚀 @PipAlertProSignals"""

    await update.message.reply_text(msg, parse_mode="HTML")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if user_id not in user_profiles:
        user_profiles[user_id] = {}

    if data == "get_signals":
        await query.edit_message_text("⏳ <b>Signals check ho rahe hain...</b>", parse_mode="HTML")
        found = False
        for pair, asset_info in ASSETS.items():
            signal = get_signal(pair)
            if signal:
                profile = user_profiles.get(user_id, {})
                level = profile.get("level", "intermediate")
                msg = format_signal(signal, asset_info, level)
                await query.message.reply_text(msg, parse_mode="HTML")
                found = True
                time.sleep(1)
        if not found:
            await query.message.reply_text(
                "📊 <b>Abhi koi strong signal nahi hai.</b>\n\n15 minute mein check karo!\n\n🚀 @PipAlertProSignals",
                parse_mode="HTML")

    elif data == "show_performance":
        total = len(signal_history)
        if total == 0:
            await query.edit_message_text(
                "📊 <b>Abhi koi signal history nahi hai.</b>\n\nPehle signals check karo!\n\n🚀 @PipAlertProSignals",
                parse_mode="HTML")
            return
        recent = signal_history[-5:] if len(signal_history) >= 5 else signal_history
        history_text = ""
        for s in reversed(recent):
            emoji = "🟢" if s['direction'] == "BUY" else "🔴"
            history_text += f"  {emoji} {s['pair']} — {s['confidence']}% — {s['time']}\n"
        msg = f"🏆 <b>Recent Signals:</b>\n\n{history_text}\n🚀 @PipAlertProSignals"
        await query.edit_message_text(msg, parse_mode="HTML")

    elif data == "show_help":
        help_msg = """
❓ <b>PipAlert Pro — Help</b>
━━━━━━━━━━━━━━━━━━━━━━
<b>Commands:</b>
/start — Bot start karo
/signals — Live signals dekho
/performance — Signal history
/stats — Bot statistics
/about — Bot info
/help — Yeh message

<b>Signal Guide:</b>
🟢 BUY — Khareeedo (Long)
🔴 SELL — Becho (Short)
🎯 TP1/TP2 — Take Profit targets
🛑 SL — Stop Loss

⚠️ <b>Risk Management:</b>
• Max 1-2% capital per trade
• Stop Loss zaroor lagao
• Kabhi bhi over-trade mat karo
━━━━━━━━━━━━━━━━━━━━━━
🚀 @PipAlertProSignals"""
        await query.edit_message_text(help_msg, parse_mode="HTML")

    elif data.startswith("level_"):
        level = data.replace("level_", "")
        user_profiles[user_id]["level"] = level
        keyboard = [
            [InlineKeyboardButton("😊 Low Risk", callback_data="risk_low"),
             InlineKeyboardButton("⚡ Medium Risk", callback_data="risk_medium"),
             InlineKeyboardButton("🔥 High Risk", callback_data="risk_high")]
        ]
        await query.edit_message_text(
            f"✅ Level set: <b>{level.capitalize()}</b>\n\n📊 Risk tolerance choose karo:",
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
            f"✅ Risk set: <b>{risk.capitalize()}</b>\n\n💰 Investment amount?",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("invest_"):
        invest = data.replace("invest_", "")
        user_profiles[user_id]["invest"] = invest
        profile = user_profiles[user_id]
        keyboard = [
            [InlineKeyboardButton("📊 Get Signals Now!", callback_data="get_signals")],
            [InlineKeyboardButton("📢 Join Channel", url="https://t.me/PipAlertProSignals")]
        ]
        await query.edit_message_text(
            f"""✅ <b>Profile Complete!</b>
━━━━━━━━━━━━━━━━━━━━━━
👤 Level: <b>{profile.get('level','').capitalize()}</b>
⚡ Risk: <b>{profile.get('risk','').capitalize()}</b>
💰 Investment: <b>{invest.capitalize()}</b>
━━━━━━━━━━━━━━━━━━━━━━
🎉 Setup complete! Ab signals milenge.

👇 Abhi signals dekho:""",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard))

async def signals_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    profile = user_profiles.get(user_id, {})
    level = profile.get("level", "intermediate")
    await update.message.reply_text("⏳ <b>Scanning markets...</b>", parse_mode="HTML")
    found = False
    for pair, asset_info in ASSETS.items():
        signal = get_signal(pair)
        if signal:
            msg = format_signal(signal, asset_info, level)
            await update.message.reply_text(msg, parse_mode="HTML")
            found = True
            time.sleep(1)
    if not found:
        keyboard = [[InlineKeyboardButton("🔄 15 min baad try karo", callback_data="get_signals")]]
        await update.message.reply_text(
            "📊 <b>Abhi koi strong signal nahi hai.</b>\n\n⏰ Market scan har 15 minute mein hota hai.\n\n🚀 @PipAlertProSignals",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📊 Get Signals", callback_data="get_signals"),
         InlineKeyboardButton("🏆 Performance", callback_data="show_performance")],
        [InlineKeyboardButton("📢 Join Channel", url="https://t.me/PipAlertProSignals")]
    ]
    await update.message.reply_text(
        """❓ <b>PipAlert Pro — Help</b>
━━━━━━━━━━━━━━━━━━━━━━
/start — Bot start karo
/signals — Live signals
/performance — Signal history
/stats — Statistics
/about — Bot info
/help — Help

🟢 BUY = Khareeedo | 🔴 SELL = Becho
🎯 TP = Target | 🛑 SL = Stop Loss

⚠️ Max 1-2% risk per trade!
━━━━━━━━━━━━━━━━━━━━━━
🚀 @PipAlertProSignals""",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard))

def run_scheduler():
    schedule.every(15).minutes.do(check_and_send_signals)
    while True:
        schedule.run_pending()
        time.sleep(30)

def main():
    print("PipAlert Pro — Interactive Bot v4.0")
    check_and_send_signals()
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    app = Application.builder().token(TELEGRAM_TOKEN).job_queue(None).build()

    import asyncio
    async def post_init(application):
        await setup_commands(application)

    app.post_init = post_init

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("signals", signals_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("performance", performance_command))
    app.add_handler(CommandHandler("about", about_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    print("Bot running! Press Ctrl+C to stop")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
