import os
import logging
import requests
import statistics
from datetime import datetime, date
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import schedule
import time
import threading

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHANNEL_ID = "@PipAlertProSignals"
TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ASSETS = {
    "XAU/USD": {"name": "Gold",     "emoji": "🥇", "pip_multi": 0.1},
    "BTC/USD": {"name": "Bitcoin",  "emoji": "₿",  "pip_multi": 0.003},
    "ETH/USD": {"name": "Ethereum", "emoji": "💎", "pip_multi": 0.003},
}

TIMEFRAMES = {
    "1min":  {"label": "1 Min",  "desc": "Scalping"},
    "2min":  {"label": "2 Min",  "desc": "Scalping"},
    "3min":  {"label": "3 Min",  "desc": "Scalping"},
    "5min":  {"label": "5 Min",  "desc": "Short Term"},
    "15min": {"label": "15 Min", "desc": "Short Term"},
    "30min": {"label": "30 Min", "desc": "Swing"},
    "1h":    {"label": "1 Hour", "desc": "Swing"},
    "4h":    {"label": "4 Hour", "desc": "Position"},
    "1day":  {"label": "1 Day",  "desc": "Long Term"},
}

user_profiles = {}
user_states = {}
signal_history = []
daily_stats = {"date": str(date.today()), "total": 0, "wins": 0, "losses": 0, "pips": 0}

# ─── DATA & INDICATORS ───────────────────────────────────────────

def get_forex_data(pair, interval="15min"):
    url = "https://api.twelvedata.com/time_series"
    params = {"symbol": pair, "interval": interval, "outputsize": 60, "apikey": TWELVEDATA_API_KEY}
    try:
        r = requests.get(url, params=params, timeout=10)
        d = r.json()
        return d["values"] if "values" in d else None
    except:
        return None

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = prices[i-1] - prices[i]
        gains.append(diff if diff > 0 else 0)
        losses.append(abs(diff) if diff < 0 else 0)
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 2)

def calculate_ema(prices, period):
    if len(prices) < period:
        return prices[0]
    mult = 2 / (period + 1)
    ema = sum(prices[-period:]) / period
    for p in reversed(prices[:-period]):
        ema = (p - ema) * mult + ema
    return round(ema, 5)

def calculate_macd(prices):
    if len(prices) < 35:
        return 0, 0
    macd = calculate_ema(prices, 12) - calculate_ema(prices, 26)
    macd_vals = [calculate_ema(prices[i:], 12) - calculate_ema(prices[i:], 26) for i in range(9)]
    return round(macd, 6), round(sum(macd_vals) / 9, 6)

def calculate_bollinger(prices, period=20):
    if len(prices) < period:
        return prices[0], prices[0], prices[0]
    sl = prices[:period]
    mean = sum(sl) / period
    std = statistics.stdev(sl)
    return round(mean + 2*std, 2), round(mean, 2), round(mean - 2*std, 2)

def get_signal(pair, interval="15min"):
    data = get_forex_data(pair, interval)
    if not data or len(data) < 35:
        return None

    closes = [float(d["close"]) for d in data]
    current = closes[0]
    rsi = calculate_rsi(closes)
    macd, sig_line = calculate_macd(closes)
    ma20 = sum(closes[:20]) / 20
    ma50 = sum(closes[:50]) / 50 if len(closes) >= 50 else sum(closes) / len(closes)
    bb_up, bb_mid, bb_low = calculate_bollinger(closes)

    score = 0
    reasons = []
    rsi_txt = macd_txt = ma_txt = bb_txt = ""

    # RSI
    if rsi < 30:   score += 3; rsi_txt = f"RSI {rsi} — Heavily Oversold 🔥"
    elif rsi < 40: score += 2; rsi_txt = f"RSI {rsi} — Oversold Zone 📉"
    elif rsi > 70: score -= 3; rsi_txt = f"RSI {rsi} — Heavily Overbought 🔥"
    elif rsi > 60: score -= 2; rsi_txt = f"RSI {rsi} — Overbought Zone 📈"
    else:          rsi_txt = f"RSI {rsi} — Neutral Zone"

    # MACD
    if macd > sig_line and macd > 0:   score += 2; macd_txt = "Strong Bullish Momentum 📈"
    elif macd > sig_line:              score += 1; macd_txt = "Bullish Crossover ↗️"
    elif macd < sig_line and macd < 0: score -= 2; macd_txt = "Strong Bearish Momentum 📉"
    else:                              score -= 1; macd_txt = "Bearish Crossover ↘️"

    # MA
    if current > ma20 > ma50:   score += 2; ma_txt = "Strong Uptrend ⬆️"
    elif current > ma20:        score += 1; ma_txt = "Mild Uptrend ↗️"
    elif current < ma20 < ma50: score -= 2; ma_txt = "Strong Downtrend ⬇️"
    else:                       score -= 1; ma_txt = "Mild Downtrend ↘️"

    # Bollinger
    if current <= bb_low:   score += 2; bb_txt = "At Support Level 💪"
    elif current >= bb_up:  score -= 2; bb_txt = "At Resistance Level 🛑"
    else:                   bb_txt = "Mid Range ↔️"

    if score >= 2:
        direction = "BUY"
        confidence = min(92, 55 + score * 5)
        pip = current * 0.003
        sl  = round(current - pip, 2)
        tp1 = round(current + pip * 1.5, 2)
        tp2 = round(current + pip * 3.0, 2)
    elif score <= -2:
        direction = "SELL"
        confidence = min(92, 55 + abs(score) * 5)
        pip = current * 0.003
        sl  = round(current + pip, 2)
        tp1 = round(current - pip * 1.5, 2)
        tp2 = round(current - pip * 3.0, 2)
    else:
        return None

    return {
        "pair": pair, "direction": direction, "price": round(current, 2),
        "sl": sl, "tp1": tp1, "tp2": tp2, "rsi": rsi,
        "confidence": confidence, "score": score, "interval": interval,
        "rsi_txt": rsi_txt, "macd_txt": macd_txt,
        "ma_txt": ma_txt, "bb_txt": bb_txt, "pip": round(pip, 2)
    }

# ─── SIGNAL FORMATTER ────────────────────────────────────────────

def format_signal(signal, asset_info, interval="15min", account=None, risk_pct=None, rr_ratio=None):
    now   = datetime.now().strftime('%d %b %Y  •  %H:%M UTC')
    conf  = signal['confidence']
    bar   = "█" * int(conf/10) + "░" * (10 - int(conf/10))
    tf    = TIMEFRAMES.get(interval, {})

    if   conf >= 85: pwr = "EXTREMELY STRONG 🔥🔥"
    elif conf >= 75: pwr = "VERY STRONG 💪"
    elif conf >= 65: pwr = "STRONG ⚡"
    else:            pwr = "MODERATE 📊"

    hdr = "🟢 BUY  —  LONG  📈" if signal['direction'] == "BUY" else "🔴 SELL  —  SHORT  📉"

    # Risk section
    risk_block = ""
    if account and risk_pct and rr_ratio:
        r_amt  = round(account * risk_pct / 100, 2)
        rw_amt = round(r_amt * rr_ratio, 2)
        risk_block = f"""▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
💼 <b>RISK MANAGEMENT</b>
├ 💰 Account Balance ➤ <code>${account:,.2f}</code>
├ ⚡ Risk ({risk_pct}%)      ➤ <code>-${r_amt:,.2f}</code>
├ 🎯 Reward (1:{rr_ratio})   ➤ <code>+${rw_amt:,.2f}</code>
└ 📐 R:R Ratio       ➤ 1:{rr_ratio}"""

    return f"""『 👑 <b>PIPALERT PRO</b> 👑 』
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
🚨 <b>{hdr}</b>
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
📍 <b>Asset</b>      ➤  {asset_info['emoji']} <b>{asset_info['name']}</b>
📍 <b>Pair</b>       ➤  <code>{signal['pair']}</code>
⏱️ <b>Timeframe</b>  ➤  {tf.get('label','15 Min')} ({tf.get('desc','')})
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
🔹 <b>Entry Zone</b>  ➤  <code>${signal['price']}</code>
🔹 <b>Stop Loss</b>   ➤  <code>${signal['sl']}</code>
🔹 <b>Take Profit:</b>
   • TP1 ➤  <code>${signal['tp1']}</code>
   • TP2 ➤  <code>${signal['tp2']}</code>
{risk_block}
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
📊 <b>ANALYSIS REPORT</b>
├ {signal['rsi_txt']}
├ {signal['macd_txt']}
├ {signal['ma_txt']}
└ {signal['bb_txt']}
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
📊 <b>Confidence:</b>  {conf}%
<code>{bar}</code>
🏆 {pwr}
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
🕐 {now}
⚠️ <i>Risk only 0.5–1% of capital. Not financial advice.</i>
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
🚀 <b>@PipAlertProSignals</b>"""

# ─── CHANNEL SENDER ──────────────────────────────────────────────

def send_to_channel(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHANNEL_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
        return r.json()
    except:
        return None

last_signals = {}

def check_and_send_signals():
    print(f"[{datetime.now().strftime('%H:%M')}] Scanning assets...")
    sent = 0
    for pair, asset_info in ASSETS.items():
        signal = get_signal(pair, "15min")
        if signal:
            key = f"{pair}_{signal['direction']}"
            if time.time() - last_signals.get(key, 0) < 3600:
                continue
            msg = format_signal(signal, asset_info, "15min")
            result = send_to_channel(msg)
            if result and result.get("ok"):
                print(f"Signal sent: {asset_info['name']} {signal['direction']}")
                last_signals[key] = time.time()
                signal_history.append({
                    "pair": pair, "direction": signal['direction'],
                    "confidence": signal['confidence'],
                    "time": datetime.now().strftime('%d %b | %H:%M'),
                    "entry": signal['price'], "tp1": signal['tp1'],
                    "tp2": signal['tp2'], "sl": signal['sl']
                })
                daily_stats["total"] += 1
                sent += 1
            time.sleep(2)
        time.sleep(1)
    if sent == 0:
        print("No strong signals this round.")

# ─── KEYBOARDS ───────────────────────────────────────────────────

def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Get Signals", callback_data="get_signals"),
         InlineKeyboardButton("💰 Risk Calculator", callback_data="risk_calc")],
        [InlineKeyboardButton("🏆 Performance", callback_data="show_perf"),
         InlineKeyboardButton("📈 Statistics", callback_data="show_stats")],
        [InlineKeyboardButton("❓ Help", callback_data="show_help"),
         InlineKeyboardButton("ℹ️ About", callback_data="show_about")],
        [InlineKeyboardButton("📢 Join VIP Channel", url="https://t.me/PipAlertProSignals")]
    ])

def tf_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ 1 Min",  callback_data="tf_1min"),
         InlineKeyboardButton("⚡ 2 Min",  callback_data="tf_2min"),
         InlineKeyboardButton("⚡ 3 Min",  callback_data="tf_3min")],
        [InlineKeyboardButton("🔥 5 Min",  callback_data="tf_5min"),
         InlineKeyboardButton("🔥 15 Min", callback_data="tf_15min"),
         InlineKeyboardButton("🔥 30 Min", callback_data="tf_30min")],
        [InlineKeyboardButton("📊 1 Hour", callback_data="tf_1h"),
         InlineKeyboardButton("📊 4 Hour", callback_data="tf_4h"),
         InlineKeyboardButton("📅 1 Day",  callback_data="tf_1day")],
        [InlineKeyboardButton("🔙 Back", callback_data="go_back")]
    ])

def risk_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1% — Safe 🟢",   callback_data="rp_1"),
         InlineKeyboardButton("2% — Normal 🟡", callback_data="rp_2")],
        [InlineKeyboardButton("3% — Medium 🟠", callback_data="rp_3"),
         InlineKeyboardButton("5% — High 🔴",   callback_data="rp_5")],
        [InlineKeyboardButton("✏️ Custom %",    callback_data="rp_custom")],
        [InlineKeyboardButton("🔙 Back",        callback_data="go_back")]
    ])

def rr_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1:2", callback_data="rr_2"),
         InlineKeyboardButton("1:3", callback_data="rr_3"),
         InlineKeyboardButton("1:5", callback_data="rr_5")],
        [InlineKeyboardButton("1:7", callback_data="rr_7"),
         InlineKeyboardButton("1:10", callback_data="rr_10"),
         InlineKeyboardButton("✏️ Custom", callback_data="rr_custom")],
        [InlineKeyboardButton("🔙 Back", callback_data="go_back")]
    ])

def back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Menu", callback_data="go_back")]])

# ─── COMMANDS ────────────────────────────────────────────────────

async def setup_commands(app):
    await app.bot.set_my_commands([
        BotCommand("start",       "🚀 Start the bot"),
        BotCommand("signals",     "📊 Get trading signals"),
        BotCommand("calculator",  "💰 Risk calculator"),
        BotCommand("performance", "🏆 Performance report"),
        BotCommand("stats",       "📈 Bot statistics"),
        BotCommand("help",        "❓ Help & Guide"),
        BotCommand("about",       "ℹ️ About PipAlert Pro"),
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name
    uid  = update.effective_user.id
    user_profiles.setdefault(uid, {"interval": "15min"})
    await update.message.reply_text(f"""『 👑 <b>PIPALERT PRO</b> 👑 』
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
👋 Welcome, <b>{name}</b>!

<b>Your Smart AI-Powered Trading Signals</b>
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
✅ 🥇 Gold (XAU/USD) Signals
✅ ₿  Bitcoin (BTC/USD) Signals
✅ 💎 Ethereum (ETH/USD) Signals
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
📊 <b>Analysis Methods:</b>
  • RSI | MACD | Bollinger Bands
  • Moving Averages (MA20 & MA50)
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
⚡ Signal Frequency  : Every 15 min
⏱️ Timeframes       : 1Min → 1Day
💰 Risk Calculator  : Custom Amount
📐 R:R Ratios       : 1:2, 1:3, 1:5+
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
⚠️ <i>For educational purposes only. Not financial advice. Trade at your own risk.</i>
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
👇 <b>Get Started:</b>""", parse_mode="HTML", reply_markup=main_kb())

async def signals_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("""『 ⏱️ <b>SELECT TIMEFRAME</b> ⏱️ 』
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
Which timeframe do you want to trade on?
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
⚡ <b>1–3 Min</b>  — Scalping (Ultra Fast)
🔥 <b>5–30 Min</b> — Short Term Trading
📊 <b>1–4 Hour</b> — Swing Trading
📅 <b>1 Day</b>   — Position Trading
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
👇 Select your timeframe:""", parse_mode="HTML", reply_markup=tf_kb())

async def calculator_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_states[uid] = "waiting_account"
    await update.message.reply_text("""『 💰 <b>RISK CALCULATOR</b> 💰 』
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
Enter your total account balance:
💡 Example: Type <code>1000</code> for $1,000
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
👇 Type your amount:""", parse_mode="HTML")

async def performance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total = len(signal_history)
    today = str(date.today())
    if total == 0:
        await update.message.reply_text("『 🏆 <b>PERFORMANCE</b> 🏆 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n📊 No signal history yet!\nGet some signals first.\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n🚀 @PipAlertProSignals", parse_mode="HTML")
        return
    recent = signal_history[-5:]
    hist = ""
    for s in reversed(recent):
        e = "🟢" if s['direction'] == "BUY" else "🔴"
        hist += f"  {e} {s['pair']}  •  {s['direction']}  •  {s['confidence']}%  •  {s['time']}\n"
    await update.message.reply_text(f"""『 🏆 <b>PERFORMANCE REPORT</b> 🏆 』
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
📅 Date: {today}

📊 <b>Today's Summary:</b>
  • Total Signals : {daily_stats['total']}
  • Wins          : ✅ {daily_stats['wins']}
  • Losses        : ❌ {daily_stats['losses']}

🔥 <b>Recent Signals:</b>
{hist}
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
⚠️ <i>Past performance is for reference only. Not financial advice.</i>
🚀 @PipAlertProSignals""", parse_mode="HTML")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total = len(signal_history)
    buys  = len([s for s in signal_history if s['direction'] == 'BUY'])
    sells = len([s for s in signal_history if s['direction'] == 'SELL'])
    avg   = round(sum(s['confidence'] for s in signal_history) / total, 1) if total else 0
    await update.message.reply_text(f"""『 📈 <b>BOT STATISTICS</b> 📈 』
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
◆ Total Signals   ➤  {total}
◆ BUY Signals    ➤  🟢 {buys}
◆ SELL Signals   ➤  🔴 {sells}
◆ Avg Confidence ➤  {avg}%
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
◆ Scan Frequency ➤  Every 15 min
◆ Assets         ➤  Gold, BTC, ETH
◆ Indicators     ➤  RSI, MACD, MA, BB
◆ Data Source    ➤  TwelveData API
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
🚀 @PipAlertProSignals""", parse_mode="HTML")

async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("""『 ℹ️ <b>ABOUT PIPALERT PRO</b> ℹ️ 』
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
🤖 <b>Bot:</b> @PipAlert_Pro_bot
📢 <b>Channel:</b> @PipAlertProSignals

Professional trading signals for:
  • Forex: XAU/USD (Gold)
  • Crypto: BTC, ETH

📊 <b>Analysis Methods:</b>
  • RSI | MACD | Bollinger Bands
  • Moving Averages (MA20 & MA50)

🔥 <b>Features:</b>
  • Real-time signals every 15 min
  • 1Min to 1Day timeframes
  • Custom risk calculator
  • Clear Entry, SL & TP levels
  • Daily performance tracking
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
⚠️ <i>For educational purposes only. Not financial advice.</i>
🚀 @PipAlertProSignals""", parse_mode="HTML")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("""『 ❓ <b>HELP & GUIDE</b> ❓ 』
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
<b>Commands:</b>
/start       — Start the bot
/signals     — Get trading signals
/calculator  — Risk calculator
/performance — Performance report
/stats       — Bot statistics
/about       — About
/help        — This guide

<b>Signal Guide:</b>
🟢 BUY  — Go Long (Buy position)
🔴 SELL — Go Short (Sell position)
🔹 Entry Zone — Where to enter
🔹 Stop Loss  — Max loss point
🔹 TP1 / TP2  — Take profit targets

<b>Timeframes:</b>
⚡ 1–3 Min  — Scalping
🔥 5–30 Min — Short Term
📊 1–4 Hour — Swing Trading
📅 1 Day    — Position Trading

⚠️ <b>Risk Rules:</b>
• Risk only 0.5–1% capital per trade
• Always set Stop Loss
• Never overtrade
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
🚀 @PipAlertProSignals""", parse_mode="HTML", reply_markup=main_kb())

# ─── TEXT HANDLER ────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    text = update.message.text.strip().replace("$","").replace(",","")
    state = user_states.get(uid, "")

    if state == "waiting_account":
        try:
            amt = float(text)
            if amt <= 0: raise ValueError
            user_profiles.setdefault(uid, {})["account"] = amt
            user_states[uid] = "waiting_rp"
            await update.message.reply_text(f"""『 ⚡ <b>RISK PERCENTAGE</b> ⚡ 』
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
✅ Account: <code>${amt:,.2f}</code>

How much % risk per trade?
💡 Recommended: 0.5–1% for safety
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰""", parse_mode="HTML", reply_markup=risk_kb())
        except:
            await update.message.reply_text("❌ Invalid! Enter a number.\n💡 Example: <code>1000</code>", parse_mode="HTML")

    elif state == "waiting_custom_rp":
        try:
            rp = float(text)
            if rp <= 0 or rp > 100: raise ValueError
            user_profiles[uid]["risk_pct"] = rp
            user_states[uid] = "waiting_rr"
            acc = user_profiles[uid].get("account", 0)
            await update.message.reply_text(f"""『 📐 <b>R:R RATIO</b> 📐 』
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
✅ Account : <code>${acc:,.2f}</code>
✅ Risk    : {rp}% = <code>${acc*rp/100:,.2f}</code>

Select Risk:Reward ratio:
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰""", parse_mode="HTML", reply_markup=rr_kb())
        except:
            await update.message.reply_text("❌ Invalid! Enter a number like <code>2</code>", parse_mode="HTML")

    elif state == "waiting_custom_rr":
        try:
            rr = float(text)
            if rr <= 0: raise ValueError
            user_profiles[uid]["rr_ratio"] = rr
            user_states[uid] = ""
            acc = user_profiles[uid].get("account", 0)
            rp  = user_profiles[uid].get("risk_pct", 1)
            r_amt = round(acc * rp / 100, 2)
            await update.message.reply_text(f"""『 ⏱️ <b>SELECT TIMEFRAME</b> ⏱️ 』
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
✅ Account  : <code>${acc:,.2f}</code>
✅ Risk     : {rp}% = <code>-${r_amt:,.2f}</code>
✅ Reward   : 1:{rr} = <code>+${round(r_amt*rr,2):,.2f}</code>

Now select timeframe for signal:
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰""", parse_mode="HTML", reply_markup=tf_kb())
        except:
            await update.message.reply_text("❌ Invalid! Enter a number like <code>3</code>", parse_mode="HTML")

# ─── BUTTON HANDLER ──────────────────────────────────────────────

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    data = query.data
    user_profiles.setdefault(uid, {"interval": "15min"})

    # TIMEFRAME
    if data.startswith("tf_"):
        interval = data.replace("tf_", "")
        user_profiles[uid]["interval"] = interval
        tf = TIMEFRAMES.get(interval, {})
        await query.edit_message_text(f"『 ⏳ <b>SCANNING MARKETS</b> ⏳ 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n🔍 Analyzing <b>{tf.get('label')}</b> timeframe...\n📊 Running multi-indicator analysis...\n⚡ Please wait...", parse_mode="HTML")
        acc = user_profiles[uid].get("account")
        rp  = user_profiles[uid].get("risk_pct")
        rr  = user_profiles[uid].get("rr_ratio")
        found = False
        for pair, asset_info in ASSETS.items():
            sig = get_signal(pair, interval)
            if sig:
                await query.message.reply_text(format_signal(sig, asset_info, interval, acc, rp, rr), parse_mode="HTML")
                found = True
                time.sleep(1)
        if not found:
            await query.message.reply_text(f"『 📊 <b>NO SIGNAL</b> 📊 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n⚠️ No strong signals on <b>{tf.get('label')}</b> right now.\n💡 Try another timeframe!\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n🚀 @PipAlertProSignals",
                parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Try Again", callback_data="get_signals"), InlineKeyboardButton("🏠 Menu", callback_data="go_back")]]))
        user_states.pop(uid, None)

    elif data == "get_signals":
        await query.edit_message_text("『 ⏱️ <b>SELECT TIMEFRAME</b> ⏱️ 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\nWhich timeframe do you want to trade on?\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n⚡ 1–3 Min  — Scalping\n🔥 5–30 Min — Short Term\n📊 1–4 Hour — Swing\n📅 1 Day    — Position\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰", parse_mode="HTML", reply_markup=tf_kb())

    elif data == "go_back":
        user_states.pop(uid, None)
        name = query.from_user.first_name
        await query.edit_message_text(f"『 👑 <b>PIPALERT PRO</b> 👑 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\nWelcome back, <b>{name}</b>!\n👇 Choose an option:", parse_mode="HTML", reply_markup=main_kb())

    elif data == "risk_calc":
        user_states[uid] = "waiting_account"
        await query.edit_message_text("『 💰 <b>RISK CALCULATOR</b> 💰 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\nEnter your total account balance:\n💡 Example: Type <code>1000</code> for $1,000\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n👇 Type your amount in chat:", parse_mode="HTML")

    elif data.startswith("rp_"):
        if data == "rp_custom":
            user_states[uid] = "waiting_custom_rp"
            await query.edit_message_text("『 ✏️ <b>CUSTOM RISK %</b> ✏️ 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\nType your custom risk %:\n💡 Example: <code>1.5</code> for 1.5%", parse_mode="HTML")
        else:
            rp = int(data.replace("rp_", ""))
            user_profiles[uid]["risk_pct"] = rp
            user_states[uid] = "waiting_rr"
            acc = user_profiles[uid].get("account", 0)
            await query.edit_message_text(f"『 📐 <b>R:R RATIO</b> 📐 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n✅ Account : <code>${acc:,.2f}</code>\n✅ Risk    : {rp}% = <code>${acc*rp/100:,.2f}</code>\n\nSelect Risk:Reward ratio:\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰", parse_mode="HTML", reply_markup=rr_kb())

    elif data.startswith("rr_"):
        if data == "rr_custom":
            user_states[uid] = "waiting_custom_rr"
            await query.edit_message_text("『 ✏️ <b>CUSTOM R:R</b> ✏️ 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\nType reward ratio:\n💡 Example: <code>4</code> means 1:4", parse_mode="HTML")
        else:
            rr = int(data.replace("rr_", ""))
            user_profiles[uid]["rr_ratio"] = rr
            user_states[uid] = ""
            acc = user_profiles[uid].get("account", 0)
            rp  = user_profiles[uid].get("risk_pct", 1)
            r_amt = round(acc * rp / 100, 2)
            await query.edit_message_text(f"『 ⏱️ <b>SELECT TIMEFRAME</b> ⏱️ 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n✅ Account  : <code>${acc:,.2f}</code>\n✅ Risk     : {rp}% = <code>-${r_amt:,.2f}</code>\n✅ Reward   : 1:{rr} = <code>+${round(r_amt*rr,2):,.2f}</code>\n\nNow select timeframe:\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰", parse_mode="HTML", reply_markup=tf_kb())

    elif data == "show_perf":
        total = len(signal_history)
        if total == 0:
            await query.edit_message_text("📊 No history yet!\n\n🚀 @PipAlertProSignals", parse_mode="HTML", reply_markup=back_kb())
            return
        recent = signal_history[-5:]
        hist = ""
        for s in reversed(recent):
            e = "🟢" if s['direction'] == "BUY" else "🔴"
            hist += f"  {e} {s['pair']}  •  {s['direction']}  •  {s['confidence']}%  •  {s['time']}\n"
        await query.edit_message_text(f"『 🏆 <b>PERFORMANCE</b> 🏆 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n📅 {str(date.today())}\n\n📊 Total: {daily_stats['total']} | Wins: {daily_stats['wins']} | Loss: {daily_stats['losses']}\n\n🔥 Recent:\n{hist}\n⚠️ Not financial advice.\n🚀 @PipAlertProSignals", parse_mode="HTML", reply_markup=back_kb())

    elif data == "show_stats":
        total = len(signal_history)
        buys  = len([s for s in signal_history if s['direction'] == 'BUY'])
        sells = len([s for s in signal_history if s['direction'] == 'SELL'])
        avg   = round(sum(s['confidence'] for s in signal_history)/total,1) if total else 0
        await query.edit_message_text(f"『 📈 <b>STATISTICS</b> 📈 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n◆ Total ➤ {total}\n◆ BUY  ➤ 🟢 {buys}\n◆ SELL ➤ 🔴 {sells}\n◆ Avg  ➤ {avg}%\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n🚀 @PipAlertProSignals", parse_mode="HTML", reply_markup=back_kb())

    elif data == "show_help":
        await query.edit_message_text("『 ❓ <b>HELP</b> ❓ 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n/start /signals /calculator\n/performance /stats /help\n\n🟢 BUY = Long 📈\n🔴 SELL = Short 📉\n🎯 TP = Take Profit\n🛑 SL = Stop Loss\n\n⚠️ Risk only 0.5–1% per trade!\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n🚀 @PipAlertProSignals", parse_mode="HTML", reply_markup=back_kb())

    elif data == "show_about":
        await query.edit_message_text("『 ℹ️ <b>ABOUT</b> ℹ️ 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n🤖 @PipAlert_Pro_bot\n📢 @PipAlertProSignals\n\n💹 Gold, BTC, ETH\n⏱️ 1Min → 1Day\n💰 Risk Calculator\n🔬 RSI, MACD, MA, BB\n📡 TwelveData API\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n⚠️ Educational only.\n🚀 @PipAlertProSignals", parse_mode="HTML", reply_markup=back_kb())

# ─── SCHEDULER & MAIN ────────────────────────────────────────────

def run_scheduler():
    schedule.every(15).minutes.do(check_and_send_signals)
    while True:
        schedule.run_pending()
        time.sleep(30)

def main():
    print("PipAlert Pro — v8.0 FINAL")
    check_and_send_signals()
    threading.Thread(target=run_scheduler, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).job_queue(None).build()
    async def post_init(application):
        await setup_commands(application)
    app.post_init = post_init
    app.add_handler(CommandHandler("start",       start))
    app.add_handler(CommandHandler("signals",     signals_command))
    app.add_handler(CommandHandler("calculator",  calculator_command))
    app.add_handler(CommandHandler("performance", performance_command))
    app.add_handler(CommandHandler("stats",       stats_command))
    app.add_handler(CommandHandler("about",       about_command))
    app.add_handler(CommandHandler("help",        help_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("Bot running!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
