import os
import logging
import requests
import statistics
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd
from io import BytesIO
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
    "XAU/USD": {"name": "Gold",     "emoji": "🥇", "tv_symbol": "TVC:GOLD"},
    "BTC/USD": {"name": "Bitcoin",  "emoji": "₿",  "tv_symbol": "BINANCE:BTCUSDT"},
    "ETH/USD": {"name": "Ethereum", "emoji": "💎", "tv_symbol": "BINANCE:ETHUSDT"},
}

TIMEFRAMES = {
    "1min":  {"label": "1 Min",  "desc": "Scalping",   "tv": "1"},
    "2min":  {"label": "2 Min",  "desc": "Scalping",   "tv": "2"},
    "3min":  {"label": "3 Min",  "desc": "Scalping",   "tv": "3"},
    "5min":  {"label": "5 Min",  "desc": "Short Term", "tv": "5"},
    "15min": {"label": "15 Min", "desc": "Short Term", "tv": "15"},
    "30min": {"label": "30 Min", "desc": "Swing",      "tv": "30"},
    "1h":    {"label": "1 Hour", "desc": "Swing",      "tv": "60"},
    "4h":    {"label": "4 Hour", "desc": "Position",   "tv": "240"},
    "1day":  {"label": "1 Day",  "desc": "Long Term",  "tv": "D"},
}

user_profiles  = {}
user_states    = {}
signal_history = []
daily_stats    = {"date": str(date.today()), "total": 0, "wins": 0, "losses": 0}

# ─── TRADINGVIEW LINK ────────────────────────────────────────────

def get_tv_link(pair, interval):
    tv_sym = ASSETS.get(pair, {}).get("tv_symbol", pair.replace("/",""))
    tv_tf  = TIMEFRAMES.get(interval, {}).get("tv", "15")
    return f"https://www.tradingview.com/chart/?symbol={tv_sym}&interval={tv_tf}"

# ─── CHART GENERATOR ─────────────────────────────────────────────

def generate_chart(pair, interval, signal):
    try:
        url    = "https://api.twelvedata.com/time_series"
        params = {"symbol": pair, "interval": interval,
                  "outputsize": 50, "apikey": TWELVEDATA_API_KEY}
        r    = requests.get(url, params=params, timeout=15)
        data = r.json()
        if "values" not in data:
            return None

        vals = list(reversed(data["values"][:40]))
        df   = pd.DataFrame(vals)
        df["datetime"] = pd.to_datetime(df["datetime"])
        df.set_index("datetime", inplace=True)

        # Convert OHLC only (no volume — not always available)
        for col in ["open", "high", "low", "close"]:
            if col in df.columns:
                df[col] = df[col].astype(float)

        # Bollinger Bands
        if len(df) >= 20:
            df["ma20"]  = df["close"].rolling(20).mean()
            df["std"]   = df["close"].rolling(20).std()
            df["bb_up"] = df["ma20"] + 2 * df["std"]
            df["bb_lo"] = df["ma20"] - 2 * df["std"]

        # RSI
        delta      = df["close"].diff()
        gain       = delta.clip(lower=0).rolling(14).mean()
        loss       = (-delta.clip(upper=0)).rolling(14).mean()
        df["rsi"]  = 100 - (100 / (1 + gain / loss))

        # ── PLOT ──────────────────────────────────────────────────
        fig = plt.figure(figsize=(12, 7), facecolor="#0d1117")
        gs  = fig.add_gridspec(2, 1, height_ratios=[3, 1], hspace=0.06)
        ax1 = fig.add_subplot(gs[0])
        ax2 = fig.add_subplot(gs[1])

        for ax in [ax1, ax2]:
            ax.set_facecolor("#0d1117")
            ax.tick_params(colors="#8b949e", labelsize=7)
            for spine in ax.spines.values():
                spine.set_color("#21262d")

        x = list(range(len(df)))

        # ── Candlesticks ──
        for i, (_, row) in enumerate(df.iterrows()):
            color = "#26a641" if row["close"] >= row["open"] else "#f85149"
            ax1.plot([i, i], [row["low"], row["high"]], color=color, linewidth=0.8, zorder=2)
            ax1.bar(i, abs(row["close"] - row["open"]),
                    bottom=min(row["open"], row["close"]),
                    color=color, width=0.6, alpha=0.9, zorder=3)

        # ── Bollinger Bands ──
        if "bb_up" in df.columns:
            ax1.plot(x, df["bb_up"], color="#58a6ff", linewidth=0.8,
                     linestyle="--", alpha=0.7, label="BB Upper")
            ax1.plot(x, df["ma20"],  color="#d29922", linewidth=0.9,
                     alpha=0.8,  label="MA20")
            ax1.plot(x, df["bb_lo"], color="#58a6ff", linewidth=0.8,
                     linestyle="--", alpha=0.7, label="BB Lower")
            ax1.fill_between(x, df["bb_up"], df["bb_lo"],
                             alpha=0.04, color="#58a6ff")

        # ── Entry / SL / TP Lines ──
        last = len(df) - 1
        ax1.axhline(signal["price"], color="#3fb950", linewidth=1.2, linestyle="-",  alpha=0.9, zorder=4)
        ax1.axhline(signal["sl"],    color="#f85149", linewidth=1.0, linestyle="--", alpha=0.8, zorder=4)
        ax1.axhline(signal["tp1"],   color="#58a6ff", linewidth=1.0, linestyle=":",  alpha=0.8, zorder=4)
        ax1.axhline(signal["tp2"],   color="#58a6ff", linewidth=0.8, linestyle=":",  alpha=0.6, zorder=4)

        offset = (df["high"].max() - df["low"].min()) * 0.005
        ax1.text(last + 0.5, signal["price"] + offset, f" Entry  ${signal['price']}",  color="#3fb950", fontsize=7, va="bottom")
        ax1.text(last + 0.5, signal["sl"]    - offset, f" SL     ${signal['sl']}",     color="#f85149", fontsize=7, va="top")
        ax1.text(last + 0.5, signal["tp1"]   + offset, f" TP1   ${signal['tp1']}",    color="#58a6ff", fontsize=7, va="bottom")
        ax1.text(last + 0.5, signal["tp2"]   + offset, f" TP2   ${signal['tp2']}",    color="#58a6ff", fontsize=7, va="bottom", alpha=0.8)

        tf_label  = TIMEFRAMES.get(interval, {}).get("label", interval)
        dir_color = "#3fb950" if signal["direction"] == "BUY" else "#f85149"
        ax1.set_title(
            f"  {ASSETS[pair]['emoji']} {ASSETS[pair]['name']} ({pair})  •  {tf_label}  •  ▶ {signal['direction']}",
            color=dir_color, fontsize=11, fontweight="bold", loc="left", pad=8)
        ax1.legend(fontsize=7, loc="upper left",
                   facecolor="#161b22", edgecolor="#21262d", labelcolor="#8b949e")
        ax1.set_xticks([])
        ax1.set_ylabel("Price", color="#8b949e", fontsize=8)
        ax1.yaxis.set_label_position("right")
        ax1.yaxis.tick_right()

        # ── RSI ──
        ax2.plot(x, df["rsi"], color="#d2a8ff", linewidth=1.0)
        ax2.axhline(70, color="#f85149", linewidth=0.5, linestyle="--", alpha=0.5)
        ax2.axhline(30, color="#3fb950", linewidth=0.5, linestyle="--", alpha=0.5)
        ax2.axhline(50, color="#8b949e", linewidth=0.4, linestyle=":",  alpha=0.4)
        ax2.fill_between(x, df["rsi"], 70, where=df["rsi"] >= 70, alpha=0.12, color="#f85149")
        ax2.fill_between(x, df["rsi"], 30, where=df["rsi"] <= 30, alpha=0.12, color="#3fb950")
        ax2.set_ylim(0, 100)
        ax2.set_yticks([30, 50, 70])
        ax2.set_ylabel("RSI", color="#8b949e", fontsize=8)
        ax2.yaxis.set_label_position("right")
        ax2.yaxis.tick_right()

        step = max(1, len(df) // 6)
        ax2.set_xticks(range(0, len(df), step))
        ax2.set_xticklabels(
            [df.index[i].strftime("%H:%M") for i in range(0, len(df), step)],
            fontsize=6, color="#8b949e")

        # Watermark
        fig.text(0.99, 0.01, "@PipAlertProSignals", ha="right", va="bottom",
                 color="#21262d", fontsize=8, style="italic")

        plt.tight_layout(pad=0.4)
        buf = BytesIO()
        plt.savefig(buf, format="png", dpi=120, bbox_inches="tight", facecolor="#0d1117")
        buf.seek(0)
        plt.close(fig)
        return buf

    except Exception as e:
        logger.error(f"Chart error: {e}")
        return None

# ─── DATA & INDICATORS ───────────────────────────────────────────

def get_forex_data(pair, interval="15min"):
    url    = "https://api.twelvedata.com/time_series"
    params = {"symbol": pair, "interval": interval, "outputsize": 60, "apikey": TWELVEDATA_API_KEY}
    try:
        r = requests.get(url, params=params, timeout=10)
        d = r.json()
        return d["values"] if "values" in d else None
    except:
        return None

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1: return 50
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = prices[i-1] - prices[i]
        gains.append(diff if diff > 0 else 0)
        losses.append(abs(diff) if diff < 0 else 0)
    ag = sum(gains) / period
    al = sum(losses) / period
    if al == 0: return 100
    return round(100 - (100 / (1 + ag / al)), 2)

def calculate_ema(prices, period):
    if len(prices) < period: return prices[0]
    mult = 2 / (period + 1)
    ema  = sum(prices[-period:]) / period
    for p in reversed(prices[:-period]):
        ema = (p - ema) * mult + ema
    return round(ema, 5)

def calculate_macd(prices):
    if len(prices) < 35: return 0, 0
    macd = calculate_ema(prices, 12) - calculate_ema(prices, 26)
    sig  = sum([calculate_ema(prices[i:], 12) - calculate_ema(prices[i:], 26) for i in range(9)]) / 9
    return round(macd, 6), round(sig, 6)

def calculate_bollinger(prices, period=20):
    if len(prices) < period: return prices[0], prices[0], prices[0]
    sl   = prices[:period]
    mean = sum(sl) / period
    std  = statistics.stdev(sl)
    return round(mean + 2*std, 2), round(mean, 2), round(mean - 2*std, 2)

def get_signal(pair, interval="15min"):
    data = get_forex_data(pair, interval)
    if not data or len(data) < 35: return None
    closes  = [float(d["close"]) for d in data]
    current = closes[0]
    rsi     = calculate_rsi(closes)
    macd, sig_line = calculate_macd(closes)
    ma20 = sum(closes[:20]) / 20
    ma50 = sum(closes[:50]) / 50 if len(closes) >= 50 else sum(closes) / len(closes)
    bb_up, _, bb_low = calculate_bollinger(closes)
    score = 0
    rsi_txt = macd_txt = ma_txt = bb_txt = ""

    if   rsi < 30:  score += 3; rsi_txt = f"RSI {rsi} — Heavily Oversold 🔥"
    elif rsi < 40:  score += 2; rsi_txt = f"RSI {rsi} — Oversold Zone 📉"
    elif rsi > 70:  score -= 3; rsi_txt = f"RSI {rsi} — Heavily Overbought 🔥"
    elif rsi > 60:  score -= 2; rsi_txt = f"RSI {rsi} — Overbought Zone 📈"
    else:                        rsi_txt = f"RSI {rsi} — Neutral Zone"

    if   macd > sig_line and macd > 0:   score += 2; macd_txt = "Strong Bullish Momentum 📈"
    elif macd > sig_line:                score += 1; macd_txt = "Bullish Crossover ↗️"
    elif macd < sig_line and macd < 0:   score -= 2; macd_txt = "Strong Bearish Momentum 📉"
    else:                                score -= 1; macd_txt = "Bearish Crossover ↘️"

    if   current > ma20 > ma50:  score += 2; ma_txt = "Strong Uptrend ⬆️"
    elif current > ma20:         score += 1; ma_txt = "Mild Uptrend ↗️"
    elif current < ma20 < ma50:  score -= 2; ma_txt = "Strong Downtrend ⬇️"
    else:                        score -= 1; ma_txt = "Mild Downtrend ↘️"

    if   current <= bb_low:  score += 2; bb_txt = "At Support Level 💪"
    elif current >= bb_up:   score -= 2; bb_txt = "At Resistance Level 🛑"
    else:                    bb_txt = "Mid Range ↔️"

    if score >= 2:
        direction  = "BUY";  confidence = min(92, 55 + score * 5)
        pip = current * 0.003
        sl  = round(current - pip, 2); tp1 = round(current + pip*1.5, 2); tp2 = round(current + pip*3, 2)
    elif score <= -2:
        direction  = "SELL"; confidence = min(92, 55 + abs(score) * 5)
        pip = current * 0.003
        sl  = round(current + pip, 2); tp1 = round(current - pip*1.5, 2); tp2 = round(current - pip*3, 2)
    else:
        return None

    return {"pair": pair, "direction": direction, "price": round(current,2),
            "sl": sl, "tp1": tp1, "tp2": tp2, "rsi": rsi,
            "confidence": confidence, "score": score, "interval": interval,
            "rsi_txt": rsi_txt, "macd_txt": macd_txt, "ma_txt": ma_txt, "bb_txt": bb_txt}

# ─── FORMATTERS ──────────────────────────────────────────────────

def format_signal(signal, asset_info, interval="15min", account=None, risk_pct=None, rr_ratio=None):
    now  = datetime.now().strftime('%d %b %Y  •  %H:%M UTC')
    conf = signal['confidence']
    bar  = "█" * int(conf/10) + "░" * (10 - int(conf/10))
    tf   = TIMEFRAMES.get(interval, {})
    if conf >= 85: pwr = "EXTREMELY STRONG 🔥🔥"
    elif conf >= 75: pwr = "VERY STRONG 💪"
    elif conf >= 65: pwr = "STRONG ⚡"
    else:            pwr = "MODERATE 📊"
    hdr = "🟢 BUY  —  LONG  📈" if signal['direction'] == "BUY" else "🔴 SELL  —  SHORT  📉"

    risk_block = ""
    if account and risk_pct and rr_ratio:
        r_amt  = round(account * risk_pct / 100, 2)
        rw_amt = round(r_amt * rr_ratio, 2)
        risk_block = f"""▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
💼 <b>RISK MANAGEMENT</b>
├ 💰 Account  ➤ <code>${account:,.2f}</code>
├ ⚡ Risk ({risk_pct}%) ➤ <code>-${r_amt:,.2f}</code>
├ 🎯 Reward 1:{rr_ratio} ➤ <code>+${rw_amt:,.2f}</code>
└ 📐 R:R     ➤ 1:{rr_ratio}"""

    return f"""『 👑 <b>PIPALERT PRO</b> 👑 』
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
🚨 <b>{hdr}</b>
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
📍 <b>Asset</b>     ➤  {asset_info['emoji']} <b>{asset_info['name']}</b>
📍 <b>Pair</b>      ➤  <code>{signal['pair']}</code>
⏱️ <b>Timeframe</b> ➤  {tf.get('label','15 Min')} ({tf.get('desc','')})
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
🔹 <b>Entry Zone</b> ➤  <code>${signal['price']}</code>
🔹 <b>Stop Loss</b>  ➤  <code>${signal['sl']}</code>
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
📊 <b>Confidence:</b> {conf}%
<code>{bar}</code>
🏆 {pwr}
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
🕐 {now}
⚠️ <i>Risk 0.5–1% only. Not financial advice.</i>
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
🚀 <b>@PipAlertProSignals</b>"""

def chart_caption(signal, asset_info, interval):
    tf  = TIMEFRAMES.get(interval, {})
    dir = "📈 BUY" if signal['direction'] == "BUY" else "📉 SELL"
    return (f"📊 <b>{asset_info['name']} Chart</b>  •  {tf.get('label')}  •  {dir}\n"
            f"Entry: <code>${signal['price']}</code>  SL: <code>${signal['sl']}</code>  "
            f"TP1: <code>${signal['tp1']}</code>  TP2: <code>${signal['tp2']}</code>\n"
            f"🚀 @PipAlertProSignals")

# ─── CHANNEL AUTO-SEND ───────────────────────────────────────────

def send_to_channel(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHANNEL_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
        return r.json()
    except:
        return None

last_signals = {}

def check_and_send_signals():
    print(f"[{datetime.now().strftime('%H:%M')}] Scanning...")
    sent = 0
    for pair, asset_info in ASSETS.items():
        sig = get_signal(pair, "15min")
        if sig:
            key = f"{pair}_{sig['direction']}"
            if time.time() - last_signals.get(key, 0) < 3600:
                continue
            result = send_to_channel(format_signal(sig, asset_info, "15min"))
            if result and result.get("ok"):
                print(f"Signal sent: {asset_info['name']} {sig['direction']}")
                last_signals[key] = time.time()
                signal_history.append({"pair": pair, "direction": sig['direction'],
                    "confidence": sig['confidence'], "time": datetime.now().strftime('%d %b | %H:%M')})
                daily_stats["total"] += 1
                sent += 1
            time.sleep(2)
        time.sleep(1)
    if sent == 0: print("No strong signals.")

# ─── KEYBOARDS ───────────────────────────────────────────────────

def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Get Signals",     callback_data="get_signals"),
         InlineKeyboardButton("💰 Risk Calculator", callback_data="risk_calc")],
        [InlineKeyboardButton("👤 My Dashboard",    callback_data="dashboard"),
         InlineKeyboardButton("📈 Statistics",      callback_data="show_stats")],
        [InlineKeyboardButton("🏆 Performance",     callback_data="show_perf"),
         InlineKeyboardButton("❓ Help",            callback_data="show_help")],
        [InlineKeyboardButton("📢 Join VIP Channel",url="https://t.me/PipAlertProSignals")]
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
        [InlineKeyboardButton("🔙 Back",   callback_data="go_back")]
    ])

def asset_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🥇 Gold Only",     callback_data="asset_XAU/USD"),
         InlineKeyboardButton("₿ Bitcoin Only",  callback_data="asset_BTC/USD")],
        [InlineKeyboardButton("💎 Ethereum Only", callback_data="asset_ETH/USD"),
         InlineKeyboardButton("📊 All Assets",   callback_data="asset_ALL")],
        [InlineKeyboardButton("🔙 Back",          callback_data="dashboard")]
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
         InlineKeyboardButton("1:10",callback_data="rr_10"),
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
        BotCommand("dashboard",   "👤 My dashboard"),
        BotCommand("performance", "🏆 Performance report"),
        BotCommand("stats",       "📈 Bot statistics"),
        BotCommand("help",        "❓ Help & Guide"),
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name
    uid  = update.effective_user.id
    user_profiles.setdefault(uid, {"interval": "15min", "assets": "ALL"})
    await update.message.reply_text(f"""『 👑 <b>PIPALERT PRO</b> 👑 』
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
👋 Welcome, <b>{name}</b>!
<b>Your Smart AI-Powered Trading Signals</b>
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
✅ 🥇 Gold (XAU/USD)
✅ ₿  Bitcoin (BTC/USD)
✅ 💎 Ethereum (ETH/USD)
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
📊 RSI | MACD | Bollinger | MA
📊 Python Charts + TradingView Links
⏱️ Timeframes: 1Min → 1Day
💰 Custom Risk Calculator
👤 Personal Dashboard
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
⚠️ <i>Educational only. Not financial advice.</i>
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
👇 Get Started:""", parse_mode="HTML", reply_markup=main_kb())

async def dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    name = update.effective_user.first_name
    user_profiles.setdefault(uid, {"interval": "15min", "assets": "ALL"})
    profile = user_profiles[uid]
    asset_label = profile.get("assets", "ALL")
    if asset_label == "ALL":   asset_show = "📊 All Assets"
    elif asset_label == "XAU/USD": asset_show = "🥇 Gold Only"
    elif asset_label == "BTC/USD": asset_show = "₿ Bitcoin Only"
    else:                          asset_show = "💎 Ethereum Only"

    acc = profile.get("account")
    rp  = profile.get("risk_pct")
    rr  = profile.get("rr_ratio")
    tf  = TIMEFRAMES.get(profile.get("interval","15min"),{}).get("label","15 Min")

    acc_line = f"<code>${acc:,.2f}</code>" if acc else "Not set"
    rp_line  = f"{rp}%"                   if rp  else "Not set"
    rr_line  = f"1:{rr}"                  if rr  else "Not set"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 Change Asset",     callback_data="change_asset"),
         InlineKeyboardButton("💰 Set Account",      callback_data="risk_calc")],
        [InlineKeyboardButton("📊 Get My Signals",   callback_data="get_signals")],
        [InlineKeyboardButton("🔙 Back",             callback_data="go_back")]
    ])

    await update.message.reply_text(f"""『 👤 <b>MY DASHBOARD</b> 👤 』
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
👋 <b>{name}</b>
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
⚙️ <b>My Settings:</b>
├ 🎯 Asset      ➤  {asset_show}
├ ⏱️ Timeframe  ➤  {tf}
├ 💰 Account    ➤  {acc_line}
├ ⚡ Risk %     ➤  {rp_line}
└ 📐 R:R Ratio  ➤  {rr_line}
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
📊 Total Signals: {len(signal_history)}
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
👇 Manage your settings:""", parse_mode="HTML", reply_markup=kb)

async def signals_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("『 ⏱️ <b>SELECT TIMEFRAME</b> ⏱️ 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\nWhich timeframe do you want to trade on?\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰", parse_mode="HTML", reply_markup=tf_kb())

async def calculator_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_states[uid] = "waiting_account"
    await update.message.reply_text("『 💰 <b>RISK CALCULATOR</b> 💰 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\nEnter your account balance:\n💡 Example: <code>1000</code>\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n👇 Type amount:", parse_mode="HTML")

async def performance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total = len(signal_history)
    if total == 0:
        await update.message.reply_text("📊 No history yet!\n\n🚀 @PipAlertProSignals")
        return
    recent = signal_history[-5:]
    hist = "".join(f"  {'🟢' if s['direction']=='BUY' else '🔴'} {s['pair']}  •  {s['direction']}  •  {s['confidence']}%  •  {s['time']}\n" for s in reversed(recent))
    await update.message.reply_text(f"『 🏆 <b>PERFORMANCE</b> 🏆 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n📅 {str(date.today())}\nTotal: {daily_stats['total']} | Wins: {daily_stats['wins']} | Loss: {daily_stats['losses']}\n\n🔥 Recent:\n{hist}\n🚀 @PipAlertProSignals", parse_mode="HTML")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total = len(signal_history)
    buys  = len([s for s in signal_history if s['direction'] == 'BUY'])
    sells = len([s for s in signal_history if s['direction'] == 'SELL'])
    avg   = round(sum(s['confidence'] for s in signal_history)/total,1) if total else 0
    await update.message.reply_text(f"『 📈 <b>STATISTICS</b> 📈 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n◆ Total ➤ {total}\n◆ BUY  ➤ 🟢 {buys}\n◆ SELL ➤ 🔴 {sells}\n◆ Avg  ➤ {avg}%\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n🚀 @PipAlertProSignals", parse_mode="HTML")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("『 ❓ <b>HELP</b> ❓ 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n/start /signals /calculator\n/dashboard /performance /stats /help\n\n🟢 BUY = Long 📈\n🔴 SELL = Short 📉\n🎯 TP = Take Profit\n🛑 SL = Stop Loss\n\n⚠️ Risk 0.5–1% only!\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n🚀 @PipAlertProSignals", parse_mode="HTML", reply_markup=main_kb())

# ─── TEXT HANDLER ────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid   = update.effective_user.id
    text  = update.message.text.strip().replace("$","").replace(",","")
    state = user_states.get(uid, "")

    if state == "waiting_account":
        try:
            amt = float(text)
            if amt <= 0: raise ValueError
            user_profiles.setdefault(uid, {})["account"] = amt
            user_states[uid] = "waiting_rp"
            await update.message.reply_text(f"『 ⚡ <b>RISK %</b> ⚡ 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n✅ Account: <code>${amt:,.2f}</code>\n\nHow much % risk per trade?\n💡 Recommended: 0.5–1%\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰", parse_mode="HTML", reply_markup=risk_kb())
        except:
            await update.message.reply_text("❌ Invalid! Example: <code>1000</code>", parse_mode="HTML")

    elif state == "waiting_custom_rp":
        try:
            rp = float(text)
            if rp <= 0 or rp > 100: raise ValueError
            user_profiles[uid]["risk_pct"] = rp
            user_states[uid] = "waiting_rr"
            acc = user_profiles[uid].get("account", 0)
            await update.message.reply_text(f"『 📐 <b>R:R RATIO</b> 📐 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n✅ Account: <code>${acc:,.2f}</code>\n✅ Risk: {rp}%\n\nSelect R:R ratio:\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰", parse_mode="HTML", reply_markup=rr_kb())
        except:
            await update.message.reply_text("❌ Invalid! Example: <code>2</code>", parse_mode="HTML")

    elif state == "waiting_custom_rr":
        try:
            rr = float(text)
            if rr <= 0: raise ValueError
            user_profiles[uid]["rr_ratio"] = rr
            user_states[uid] = ""
            acc = user_profiles[uid].get("account", 0)
            rp  = user_profiles[uid].get("risk_pct", 1)
            await update.message.reply_text(f"『 ⏱️ <b>SELECT TIMEFRAME</b> ⏱️ 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n✅ Account: <code>${acc:,.2f}</code>\n✅ Risk: {rp}%\n✅ R:R: 1:{rr}\n\nSelect timeframe:\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰", parse_mode="HTML", reply_markup=tf_kb())
        except:
            await update.message.reply_text("❌ Invalid! Example: <code>3</code>", parse_mode="HTML")

# ─── BUTTON HANDLER ──────────────────────────────────────────────

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    data = query.data
    user_profiles.setdefault(uid, {"interval": "15min", "assets": "ALL"})

    # ── TIMEFRAME ──
    if data.startswith("tf_"):
        interval = data.replace("tf_", "")
        user_profiles[uid]["interval"] = interval
        tf  = TIMEFRAMES.get(interval, {})
        acc = user_profiles[uid].get("account")
        rp  = user_profiles[uid].get("risk_pct")
        rr  = user_profiles[uid].get("rr_ratio")
        selected_asset = user_profiles[uid].get("assets", "ALL")

        await query.edit_message_text(
            f"『 ⏳ <b>SCANNING MARKETS</b> ⏳ 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n🔍 Analyzing <b>{tf.get('label')}</b>...\n📊 Generating charts...\n⚡ Please wait...",
            parse_mode="HTML")

        # Determine which assets to scan
        if selected_asset == "ALL":
            pairs_to_scan = list(ASSETS.items())
        else:
            pairs_to_scan = [(selected_asset, ASSETS[selected_asset])] if selected_asset in ASSETS else list(ASSETS.items())

        found = False
        for pair, asset_info in pairs_to_scan:
            sig = get_signal(pair, interval)
            if sig:
                msg = format_signal(sig, asset_info, interval, acc, rp, rr)
                tv_link = get_tv_link(pair, interval)
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("📈 Live TradingView Chart", url=tv_link)]])
                await query.message.reply_text(msg, parse_mode="HTML", reply_markup=kb)

                # Python chart
                chart_buf = generate_chart(pair, interval, sig)
                if chart_buf:
                    await query.message.reply_photo(photo=chart_buf, caption=chart_caption(sig, asset_info, interval), parse_mode="HTML")

                signal_history.append({"pair": pair, "direction": sig['direction'],
                    "confidence": sig['confidence'], "time": datetime.now().strftime('%d %b | %H:%M')})
                daily_stats["total"] += 1
                found = True
                time.sleep(1)

        if not found:
            await query.message.reply_text(
                f"『 📊 <b>NO SIGNAL</b> 📊 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n⚠️ No strong signals on <b>{tf.get('label')}</b> now.\n💡 Try another timeframe!\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n🚀 @PipAlertProSignals",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Try Again", callback_data="get_signals"), InlineKeyboardButton("🏠 Menu", callback_data="go_back")]]))
        user_states.pop(uid, None)

    # ── ASSET SELECTION ──
    elif data.startswith("asset_"):
        asset = data.replace("asset_", "")
        user_profiles[uid]["assets"] = asset
        if asset == "ALL":   label = "📊 All Assets"
        elif asset == "XAU/USD": label = "🥇 Gold Only"
        elif asset == "BTC/USD": label = "₿ Bitcoin Only"
        else:                    label = "💎 Ethereum Only"

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎯 Change Asset",   callback_data="change_asset"),
             InlineKeyboardButton("💰 Set Account",    callback_data="risk_calc")],
            [InlineKeyboardButton("📊 Get My Signals", callback_data="get_signals")],
            [InlineKeyboardButton("🔙 Back",           callback_data="go_back")]
        ])
        await query.edit_message_text(
            f"『 👤 <b>DASHBOARD UPDATED</b> 👤 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n✅ Asset set to: <b>{label}</b>\n\nYou will now receive signals for this asset only.\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰",
            parse_mode="HTML", reply_markup=kb)

    elif data == "change_asset":
        await query.edit_message_text(
            "『 🎯 <b>SELECT ASSET</b> 🎯 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\nWhich asset do you want signals for?\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰",
            parse_mode="HTML", reply_markup=asset_kb())

    elif data == "dashboard":
        uid  = query.from_user.id
        name = query.from_user.first_name
        profile = user_profiles.get(uid, {})
        asset_label = profile.get("assets", "ALL")
        if asset_label == "ALL":       asset_show = "📊 All Assets"
        elif asset_label == "XAU/USD": asset_show = "🥇 Gold Only"
        elif asset_label == "BTC/USD": asset_show = "₿ Bitcoin Only"
        else:                          asset_show = "💎 Ethereum Only"
        acc = profile.get("account")
        rp  = profile.get("risk_pct")
        rr  = profile.get("rr_ratio")
        tf  = TIMEFRAMES.get(profile.get("interval","15min"),{}).get("label","15 Min")
        acc_line = f"<code>${acc:,.2f}</code>" if acc else "Not set"
        rp_line  = f"{rp}%"                   if rp  else "Not set"
        rr_line  = f"1:{rr}"                  if rr  else "Not set"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎯 Change Asset",   callback_data="change_asset"),
             InlineKeyboardButton("💰 Set Account",    callback_data="risk_calc")],
            [InlineKeyboardButton("📊 Get My Signals", callback_data="get_signals")],
            [InlineKeyboardButton("🔙 Back",           callback_data="go_back")]
        ])
        await query.edit_message_text(
            f"『 👤 <b>MY DASHBOARD</b> 👤 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n👋 <b>{name}</b>\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n⚙️ <b>My Settings:</b>\n├ 🎯 Asset     ➤  {asset_show}\n├ ⏱️ Timeframe ➤  {tf}\n├ 💰 Account   ➤  {acc_line}\n├ ⚡ Risk %    ➤  {rp_line}\n└ 📐 R:R       ➤  {rr_line}\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n📊 Total Signals: {len(signal_history)}\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰",
            parse_mode="HTML", reply_markup=kb)

    elif data == "get_signals":
        await query.edit_message_text("『 ⏱️ <b>SELECT TIMEFRAME</b> ⏱️ 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\nWhich timeframe do you want to trade on?\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰", parse_mode="HTML", reply_markup=tf_kb())

    elif data == "go_back":
        user_states.pop(uid, None)
        name = query.from_user.first_name
        await query.edit_message_text(f"『 👑 <b>PIPALERT PRO</b> 👑 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\nWelcome back, <b>{name}</b>!\n👇 Choose an option:", parse_mode="HTML", reply_markup=main_kb())

    elif data == "risk_calc":
        user_states[uid] = "waiting_account"
        await query.edit_message_text("『 💰 <b>RISK CALCULATOR</b> 💰 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\nEnter your account balance:\n💡 Example: <code>1000</code>\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n👇 Type in chat:", parse_mode="HTML")

    elif data.startswith("rp_"):
        if data == "rp_custom":
            user_states[uid] = "waiting_custom_rp"
            await query.edit_message_text("『 ✏️ <b>CUSTOM %</b> ✏️ 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\nType custom risk %:\n💡 Example: <code>1.5</code>", parse_mode="HTML")
        else:
            rp = int(data.replace("rp_", ""))
            user_profiles[uid]["risk_pct"] = rp
            acc = user_profiles[uid].get("account", 0)
            await query.edit_message_text(f"『 📐 <b>R:R RATIO</b> 📐 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n✅ Account: <code>${acc:,.2f}</code>\n✅ Risk: {rp}%\n\nSelect R:R ratio:\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰", parse_mode="HTML", reply_markup=rr_kb())

    elif data.startswith("rr_"):
        if data == "rr_custom":
            user_states[uid] = "waiting_custom_rr"
            await query.edit_message_text("『 ✏️ <b>CUSTOM R:R</b> ✏️ 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\nType reward ratio:\n💡 Example: <code>4</code> = 1:4", parse_mode="HTML")
        else:
            rr = int(data.replace("rr_", ""))
            user_profiles[uid]["rr_ratio"] = rr
            acc = user_profiles[uid].get("account", 0)
            rp  = user_profiles[uid].get("risk_pct", 1)
            await query.edit_message_text(f"『 ⏱️ <b>SELECT TIMEFRAME</b> ⏱️ 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n✅ Account: <code>${acc:,.2f}</code>\n✅ Risk: {rp}%\n✅ R:R: 1:{rr}\n\nSelect timeframe:\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰", parse_mode="HTML", reply_markup=tf_kb())

    elif data == "show_perf":
        total = len(signal_history)
        if total == 0:
            await query.edit_message_text("📊 No history yet!\n\n🚀 @PipAlertProSignals", parse_mode="HTML", reply_markup=back_kb())
            return
        recent = signal_history[-5:]
        hist = "".join(f"  {'🟢' if s['direction']=='BUY' else '🔴'} {s['pair']}  •  {s['direction']}  •  {s['confidence']}%\n" for s in reversed(recent))
        await query.edit_message_text(f"『 🏆 <b>PERFORMANCE</b> 🏆 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\nTotal: {daily_stats['total']} | Wins: {daily_stats['wins']} | Loss: {daily_stats['losses']}\n\n{hist}\n🚀 @PipAlertProSignals", parse_mode="HTML", reply_markup=back_kb())

    elif data == "show_stats":
        total = len(signal_history)
        buys  = len([s for s in signal_history if s['direction'] == 'BUY'])
        sells = len([s for s in signal_history if s['direction'] == 'SELL'])
        avg   = round(sum(s['confidence'] for s in signal_history)/total,1) if total else 0
        await query.edit_message_text(f"『 📈 <b>STATISTICS</b> 📈 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n◆ Total ➤ {total}\n◆ BUY  ➤ 🟢 {buys}\n◆ SELL ➤ 🔴 {sells}\n◆ Avg  ➤ {avg}%\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n🚀 @PipAlertProSignals", parse_mode="HTML", reply_markup=back_kb())

    elif data == "show_help":
        await query.edit_message_text("『 ❓ <b>HELP</b> ❓ 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n/start /signals /calculator\n/dashboard /performance /stats\n\n🟢 BUY = Long 📈\n🔴 SELL = Short 📉\n🎯 TP = Take Profit\n🛑 SL = Stop Loss\n⚠️ Risk 0.5–1% only!\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n🚀 @PipAlertProSignals", parse_mode="HTML", reply_markup=back_kb())

# ─── SCHEDULER & MAIN ────────────────────────────────────────────

def run_scheduler():
    schedule.every(15).minutes.do(check_and_send_signals)
    while True:
        schedule.run_pending()
        time.sleep(30)

def main():
    print("PipAlert Pro — v10.0 FINAL")
    check_and_send_signals()
    threading.Thread(target=run_scheduler, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).job_queue(None).build()
    async def post_init(application):
        await setup_commands(application)
    app.post_init = post_init
    app.add_handler(CommandHandler("start",       start))
    app.add_handler(CommandHandler("signals",     signals_command))
    app.add_handler(CommandHandler("calculator",  calculator_command))
    app.add_handler(CommandHandler("dashboard",   dashboard_command))
    app.add_handler(CommandHandler("performance", performance_command))
    app.add_handler(CommandHandler("stats",       stats_command))
    app.add_handler(CommandHandler("help",        help_command))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("Bot running!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
