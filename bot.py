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
import asyncio

TELEGRAM_TOKEN     = os.environ.get("TELEGRAM_TOKEN")
CHANNEL_ID         = "@PipAlertProSignals"
TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY")
SUPPORT_CHAT_ID    = os.environ.get("SUPPORT_CHAT_ID", "")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── ASSETS & TIMEFRAMES ─────────────────────────────────────────

ASSETS = {
    "XAU/USD": {"name": "Gold",         "emoji": "🥇", "tv_symbol": "TVC:GOLD"},
    "BTC/USD": {"name": "Bitcoin",      "emoji": "₿",  "tv_symbol": "BINANCE:BTCUSDT"},
    "ETH/USD": {"name": "Ethereum",     "emoji": "💎", "tv_symbol": "BINANCE:ETHUSDT"},
    "EUR/USD": {"name": "EUR/USD",      "emoji": "💶", "tv_symbol": "FX:EURUSD"},
    "GBP/USD": {"name": "GBP/USD",      "emoji": "💷", "tv_symbol": "FX:GBPUSD"},
    "USD/JPY": {"name": "USD/JPY",      "emoji": "💴", "tv_symbol": "FX:USDJPY"},
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

# ─── GLOBAL DATA ─────────────────────────────────────────────────

user_profiles  = {}   # uid → settings
user_states    = {}   # uid → state
user_signals   = {}   # uid → [signal records]
pending_checks = []   # [{signal, uids, check_after}] for auto result check
signal_history = []
daily_stats    = {"date": str(date.today()), "total": 0, "wins": 0, "losses": 0}
last_signals   = {}

# Global app reference for sending messages from scheduler
_app = None

def get_user(uid):
    user_profiles.setdefault(uid, {"interval":"15min","assets":"ALL","account":None,"risk_pct":None,"rr_ratio":None})
    user_signals.setdefault(uid, [])
    return user_profiles[uid]

# ─── PRICE FETCHER ───────────────────────────────────────────────

def get_current_price(pair):
    try:
        r = requests.get("https://api.twelvedata.com/price",
                         params={"symbol": pair, "apikey": TWELVEDATA_API_KEY}, timeout=8)
        d = r.json()
        return float(d["price"]) if "price" in d else None
    except: return None

# ─── AUTO WIN/LOSS CHECKER ───────────────────────────────────────

def check_pending_signals():
    """Auto check if TP or SL was hit for pending signals"""
    global pending_checks
    still_pending = []
    results_to_send = []

    for item in pending_checks:
        sig      = item["signal"]
        uids     = item["uids"]
        check_at = item["check_after"]

        if time.time() < check_at:
            still_pending.append(item)
            continue

        current = get_current_price(sig["pair"])
        if current is None:
            still_pending.append(item)
            continue

        result   = None
        result_msg = ""

        if sig["direction"] == "BUY":
            if current >= sig["tp2"]:
                result = "WIN"; result_msg = f"✅ TP2 Hit! +{round(sig['tp2']-sig['price'],2)}"
            elif current >= sig["tp1"]:
                result = "WIN"; result_msg = f"✅ TP1 Hit! +{round(sig['tp1']-sig['price'],2)}"
            elif current <= sig["sl"]:
                result = "LOSS"; result_msg = f"❌ Stop Loss Hit! -{round(sig['price']-sig['sl'],2)}"
        else:  # SELL
            if current <= sig["tp2"]:
                result = "WIN"; result_msg = f"✅ TP2 Hit! +{round(sig['price']-sig['tp2'],2)}"
            elif current <= sig["tp1"]:
                result = "WIN"; result_msg = f"✅ TP1 Hit! +{round(sig['price']-sig['tp1'],2)}"
            elif current >= sig["sl"]:
                result = "LOSS"; result_msg = f"❌ Stop Loss Hit! -{round(sig['sl']-sig['price'],2)}"

        if result:
            # Update daily stats
            if result == "WIN":   daily_stats["wins"] += 1
            elif result == "LOSS": daily_stats["losses"] += 1

            # Update user signal records
            for uid in uids:
                sigs = user_signals.get(uid, [])
                for s in sigs:
                    if (s["pair"] == sig["pair"] and
                        s["entry"] == sig["price"] and
                        s.get("result") == "PENDING"):
                        s["result"] = result
                        break

            results_to_send.append({
                "uids": uids, "signal": sig,
                "result": result, "result_msg": result_msg,
                "current_price": current
            })

            # Check again after more time if TP2 not hit yet
        else:
            # Check again after 15 minutes
            item["check_after"] = time.time() + 900
            if time.time() - item.get("created_at", time.time()) > 86400:
                # Expired after 24 hours
                pass
            else:
                still_pending.append(item)

    pending_checks = still_pending

    # Send results
    if results_to_send and _app:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_send_results(results_to_send))
        finally:
            loop.close()

async def _send_results(results):
    for item in results:
        sig    = item["signal"]
        result = item["result"]
        cur    = item["current_price"]
        rmsg   = item["result_msg"]
        asset  = ASSETS.get(sig["pair"], {})

        if result == "WIN":
            hdr = "🏆 SIGNAL RESULT — WIN! 🏆"
            color_emoji = "🟢"
        else:
            hdr = "📊 SIGNAL RESULT — LOSS 📊"
            color_emoji = "🔴"

        msg = f"""『 {color_emoji} <b>{hdr}</b> {color_emoji} 』
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
📍 <b>Asset:</b>  {asset.get('emoji','')} {asset.get('name',sig['pair'])}
📍 <b>Pair:</b>   <code>{sig['pair']}</code>
📍 <b>Signal:</b> {sig['direction']}
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
💰 Entry Price  ➤  <code>${sig['price']}</code>
📊 Current Price ➤  <code>${cur}</code>
🛑 Stop Loss    ➤  <code>${sig['sl']}</code>
🎯 TP1          ➤  <code>${sig['tp1']}</code>
🎯 TP2          ➤  <code>${sig['tp2']}</code>
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
📈 <b>Result: {rmsg}</b>
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
🕐 {datetime.now().strftime('%d %b %Y  •  %H:%M UTC')}
🚀 <b>@PipAlertProSignals</b>"""

        for uid in item["uids"]:
            try:
                await _app.bot.send_message(chat_id=uid, text=msg, parse_mode="HTML")
                time.sleep(0.3)
            except Exception as e:
                logger.error(f"Send result error: {e}")

        # Also send to channel
        try:
            send_to_channel(msg)
        except: pass

# ─── TRADINGVIEW LINK ────────────────────────────────────────────

def get_tv_link(pair, interval):
    tv_sym = ASSETS.get(pair,{}).get("tv_symbol", pair.replace("/",""))
    tv_tf  = TIMEFRAMES.get(interval,{}).get("tv","15")
    return f"https://www.tradingview.com/chart/?symbol={tv_sym}&interval={tv_tf}"

# ─── CHART GENERATOR ─────────────────────────────────────────────

def generate_chart(pair, interval, signal):
    try:
        params = {"symbol":pair,"interval":interval,"outputsize":50,"apikey":TWELVEDATA_API_KEY}
        r    = requests.get("https://api.twelvedata.com/time_series", params=params, timeout=15)
        data = r.json()
        if "values" not in data: return None
        vals = list(reversed(data["values"][:40]))
        df   = pd.DataFrame(vals)
        df["datetime"] = pd.to_datetime(df["datetime"])
        df.set_index("datetime", inplace=True)
        for col in ["open","high","low","close"]:
            if col in df.columns: df[col] = df[col].astype(float)
        if len(df)>=20:
            df["ma20"]  = df["close"].rolling(20).mean()
            df["std"]   = df["close"].rolling(20).std()
            df["bb_up"] = df["ma20"]+2*df["std"]
            df["bb_lo"] = df["ma20"]-2*df["std"]
        delta=df["close"].diff(); gain=delta.clip(lower=0).rolling(14).mean()
        loss=(-delta.clip(upper=0)).rolling(14).mean(); df["rsi"]=100-(100/(1+gain/loss))

        fig=plt.figure(figsize=(12,7),facecolor="#0d1117")
        gs=fig.add_gridspec(2,1,height_ratios=[3,1],hspace=0.06)
        ax1=fig.add_subplot(gs[0]); ax2=fig.add_subplot(gs[1])
        for ax in [ax1,ax2]:
            ax.set_facecolor("#0d1117"); ax.tick_params(colors="#8b949e",labelsize=7)
            for sp in ax.spines.values(): sp.set_color("#21262d")
        x=list(range(len(df)))
        for i,(_,row) in enumerate(df.iterrows()):
            color="#26a641" if row["close"]>=row["open"] else "#f85149"
            ax1.plot([i,i],[row["low"],row["high"]],color=color,linewidth=0.8,zorder=2)
            ax1.bar(i,abs(row["close"]-row["open"]),bottom=min(row["open"],row["close"]),
                    color=color,width=0.6,alpha=0.9,zorder=3)
        if "bb_up" in df.columns:
            ax1.plot(x,df["bb_up"],color="#58a6ff",linewidth=0.8,linestyle="--",alpha=0.7,label="BB Upper")
            ax1.plot(x,df["ma20"], color="#d29922",linewidth=0.9,alpha=0.8,label="MA20")
            ax1.plot(x,df["bb_lo"],color="#58a6ff",linewidth=0.8,linestyle="--",alpha=0.7,label="BB Lower")
            ax1.fill_between(x,df["bb_up"],df["bb_lo"],alpha=0.04,color="#58a6ff")
        # ── Y-axis: tight around candles ──
        candle_min = df["low"].min()
        candle_max = df["high"].max()
        padding    = (candle_max - candle_min) * 0.15
        y_min      = candle_min - padding
        y_max      = candle_max + padding
        ax1.set_ylim(y_min, y_max)

        rng=candle_max-candle_min; off=rng*0.01; last=len(df)-1

        # Draw Entry/SL/TP only if within visible range
        all_levels = {
            "entry": (signal["price"], "#3fb950", "-",  1.2, f" Entry ${signal['price']}"),
            "sl":    (signal["sl"],    "#f85149", "--", 1.0, f" SL ${signal['sl']}"),
            "tp1":   (signal["tp1"],   "#58a6ff", ":",  1.0, f" TP1 ${signal['tp1']}"),
            "tp2":   (signal["tp2"],   "#58a6ff", ":",  0.8, f" TP2 ${signal['tp2']}"),
        }
        for key,(price,color,ls,lw,label) in all_levels.items():
            ax1.axhline(price,color=color,linewidth=lw,linestyle=ls,alpha=0.85,zorder=4)
            if y_min <= price <= y_max:
                ax1.text(last+0.5,price+off,label,color=color,fontsize=7,va="bottom")
        tf_label=TIMEFRAMES.get(interval,{}).get("label",interval)
        dc="#3fb950" if signal["direction"]=="BUY" else "#f85149"
        ax1.set_title(f"  {ASSETS.get(pair,{}).get('emoji','')} {ASSETS.get(pair,{}).get('name',pair)} ({pair})  •  {tf_label}  •  ▶ {signal['direction']}",
                      color=dc,fontsize=11,fontweight="bold",loc="left",pad=8)
        ax1.legend(fontsize=7,loc="upper left",facecolor="#161b22",edgecolor="#21262d",labelcolor="#8b949e")
        ax1.set_xticks([]); ax1.set_ylabel("Price",color="#8b949e",fontsize=8)
        ax1.yaxis.set_label_position("right"); ax1.yaxis.tick_right()
        ax2.plot(x,df["rsi"],color="#d2a8ff",linewidth=1.0)
        ax2.axhline(70,color="#f85149",linewidth=0.5,linestyle="--",alpha=0.5)
        ax2.axhline(30,color="#3fb950",linewidth=0.5,linestyle="--",alpha=0.5)
        ax2.axhline(50,color="#8b949e",linewidth=0.4,linestyle=":", alpha=0.4)
        ax2.fill_between(x,df["rsi"],70,where=df["rsi"]>=70,alpha=0.12,color="#f85149")
        ax2.fill_between(x,df["rsi"],30,where=df["rsi"]<=30,alpha=0.12,color="#3fb950")
        ax2.set_ylim(0,100); ax2.set_yticks([30,50,70])
        ax2.set_ylabel("RSI",color="#8b949e",fontsize=8)
        ax2.yaxis.set_label_position("right"); ax2.yaxis.tick_right()
        step=max(1,len(df)//6)
        ax2.set_xticks(range(0,len(df),step))
        ax2.set_xticklabels([df.index[i].strftime("%H:%M") for i in range(0,len(df),step)],fontsize=6,color="#8b949e")
        fig.text(0.99,0.01,"@PipAlertProSignals",ha="right",va="bottom",color="#21262d",fontsize=8,style="italic")
        plt.tight_layout(pad=0.4)
        buf=BytesIO(); plt.savefig(buf,format="png",dpi=120,bbox_inches="tight",facecolor="#0d1117")
        buf.seek(0); plt.close(fig); return buf
    except Exception as e:
        logger.error(f"Chart error: {e}"); return None

# ─── INDICATORS ──────────────────────────────────────────────────

def get_forex_data(pair, interval="15min"):
    params={"symbol":pair,"interval":interval,"outputsize":60,"apikey":TWELVEDATA_API_KEY}
    try:
        r=requests.get("https://api.twelvedata.com/time_series",params=params,timeout=10)
        d=r.json(); return d["values"] if "values" in d else None
    except: return None

def calc_rsi(prices,period=14):
    if len(prices)<period+1: return 50
    gains=[max(prices[i-1]-prices[i],0) for i in range(1,period+1)]
    losses=[max(prices[i]-prices[i-1],0) for i in range(1,period+1)]
    ag=sum(gains)/period; al=sum(losses)/period
    return 50 if al==0 else round(100-(100/(1+ag/al)),2)

def calc_ema(prices,period):
    if len(prices)<period: return prices[0]
    mult=2/(period+1); ema=sum(prices[-period:])/period
    for p in reversed(prices[:-period]): ema=(p-ema)*mult+ema
    return round(ema,5)

def calc_macd(prices):
    if len(prices)<35: return 0,0
    macd=calc_ema(prices,12)-calc_ema(prices,26)
    sig=sum([calc_ema(prices[i:],12)-calc_ema(prices[i:],26) for i in range(9)])/9
    return round(macd,6),round(sig,6)

def calc_bb(prices,period=20):
    if len(prices)<period: return prices[0],prices[0],prices[0]
    sl=prices[:period]; mean=sum(sl)/period; std=statistics.stdev(sl)
    return round(mean+2*std,2),round(mean,2),round(mean-2*std,2)

def get_signal(pair,interval="15min"):
    data=get_forex_data(pair,interval)
    if not data or len(data)<35: return None
    closes=[float(d["close"]) for d in data]; cur=closes[0]
    rsi=calc_rsi(closes); macd,sig_line=calc_macd(closes)
    ma20=sum(closes[:20])/20
    ma50=sum(closes[:50])/50 if len(closes)>=50 else sum(closes)/len(closes)
    bb_up,_,bb_low=calc_bb(closes)
    score=0; rsi_txt=macd_txt=ma_txt=bb_txt=""
    if   rsi<30:  score+=3; rsi_txt=f"RSI {rsi} — Heavily Oversold 🔥"
    elif rsi<40:  score+=2; rsi_txt=f"RSI {rsi} — Oversold Zone 📉"
    elif rsi>70:  score-=3; rsi_txt=f"RSI {rsi} — Heavily Overbought 🔥"
    elif rsi>60:  score-=2; rsi_txt=f"RSI {rsi} — Overbought Zone 📈"
    else:          rsi_txt=f"RSI {rsi} — Neutral Zone"
    if   macd>sig_line and macd>0:  score+=2; macd_txt="Strong Bullish 📈"
    elif macd>sig_line:             score+=1; macd_txt="Bullish Crossover ↗️"
    elif macd<sig_line and macd<0:  score-=2; macd_txt="Strong Bearish 📉"
    else:                           score-=1; macd_txt="Bearish Crossover ↘️"
    if   cur>ma20>ma50:  score+=2; ma_txt="Strong Uptrend ⬆️"
    elif cur>ma20:       score+=1; ma_txt="Mild Uptrend ↗️"
    elif cur<ma20<ma50:  score-=2; ma_txt="Strong Downtrend ⬇️"
    else:                score-=1; ma_txt="Mild Downtrend ↘️"
    if   cur<=bb_low:  score+=2; bb_txt="At Support 💪"
    elif cur>=bb_up:   score-=2; bb_txt="At Resistance 🛑"
    else:               bb_txt="Mid Range ↔️"
    if score>=2:
        d="BUY";  conf=min(92,55+score*5)
        pip=cur*0.003; sl=round(cur-pip,2); tp1=round(cur+pip*1.5,2); tp2=round(cur+pip*3,2)
    elif score<=-2:
        d="SELL"; conf=min(92,55+abs(score)*5)
        pip=cur*0.003; sl=round(cur+pip,2); tp1=round(cur-pip*1.5,2); tp2=round(cur-pip*3,2)
    else: return None
    return {"pair":pair,"direction":d,"price":round(cur,2),"sl":sl,"tp1":tp1,"tp2":tp2,
            "rsi":rsi,"confidence":conf,"score":score,"interval":interval,
            "rsi_txt":rsi_txt,"macd_txt":macd_txt,"ma_txt":ma_txt,"bb_txt":bb_txt}

# ─── FORMATTERS ──────────────────────────────────────────────────

def format_signal(sig,asset_info,interval="15min",account=None,risk_pct=None,rr_ratio=None):
    now=datetime.now().strftime('%d %b %Y  •  %H:%M UTC')
    conf=sig['confidence']; bar="█"*int(conf/10)+"░"*(10-int(conf/10))
    tf=TIMEFRAMES.get(interval,{})
    if conf>=85: pwr="EXTREMELY STRONG 🔥🔥"
    elif conf>=75: pwr="VERY STRONG 💪"
    elif conf>=65: pwr="STRONG ⚡"
    else:          pwr="MODERATE 📊"
    hdr="🟢 BUY  —  LONG  📈" if sig['direction']=="BUY" else "🔴 SELL  —  SHORT  📉"
    risk_block=""
    if account and risk_pct and rr_ratio:
        r=round(account*risk_pct/100,2); rw=round(r*rr_ratio,2)
        risk_block=f"▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n💼 <b>RISK MANAGEMENT</b>\n├ 💰 Account  ➤ <code>${account:,.2f}</code>\n├ ⚡ Risk ({risk_pct}%) ➤ <code>-${r:,.2f}</code>\n├ 🎯 Reward 1:{rr_ratio} ➤ <code>+${rw:,.2f}</code>\n└ 📐 R:R     ➤ 1:{rr_ratio}"
    return f"""『 👑 <b>PIPALERT PRO</b> 👑 』
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
🚨 <b>{hdr}</b>
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
📍 <b>Asset</b>     ➤  {asset_info['emoji']} <b>{asset_info['name']}</b>
📍 <b>Pair</b>      ➤  <code>{sig['pair']}</code>
⏱️ <b>Timeframe</b> ➤  {tf.get('label','15 Min')} ({tf.get('desc','')})
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
🔹 <b>Entry Zone</b> ➤  <code>${sig['price']}</code>
🔹 <b>Stop Loss</b>  ➤  <code>${sig['sl']}</code>
🔹 <b>Take Profit:</b>
   • TP1 ➤  <code>${sig['tp1']}</code>
   • TP2 ➤  <code>${sig['tp2']}</code>
{risk_block}
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
📊 <b>ANALYSIS</b>
├ {sig['rsi_txt']}
├ {sig['macd_txt']}
├ {sig['ma_txt']}
└ {sig['bb_txt']}
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
📊 <b>Confidence:</b> {conf}%
<code>{bar}</code>
🏆 {pwr}
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
⏳ <i>Auto result will be sent when TP/SL hits!</i>
🕐 {now}
⚠️ <i>Risk 0.5–1% only. Not financial advice.</i>
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
🚀 <b>@PipAlertProSignals</b>"""

def chart_cap(sig,asset_info,interval):
    tf=TIMEFRAMES.get(interval,{}); d="📈 BUY" if sig['direction']=="BUY" else "📉 SELL"
    return f"📊 <b>{asset_info['name']} Chart</b>  •  {tf.get('label')}  •  {d}\nEntry: <code>${sig['price']}</code>  SL: <code>${sig['sl']}</code>  TP1: <code>${sig['tp1']}</code>  TP2: <code>${sig['tp2']}</code>\n🚀 @PipAlertProSignals"

# ─── USER STATS ──────────────────────────────────────────────────

def get_user_stats(uid):
    sigs=user_signals.get(uid,[])
    total=len(sigs)
    wins=len([s for s in sigs if s.get("result")=="WIN"])
    losses=len([s for s in sigs if s.get("result")=="LOSS"])
    pending=len([s for s in sigs if s.get("result")=="PENDING"])
    winrate=round(wins/total*100,1) if total>0 else 0
    return {"total":total,"wins":wins,"losses":losses,"pending":pending,"winrate":winrate}

def add_user_signal(uid,sig):
    user_signals.setdefault(uid,[])
    user_signals[uid].append({
        "pair":sig["pair"],"direction":sig["direction"],"entry":sig["price"],
        "sl":sig["sl"],"tp1":sig["tp1"],"tp2":sig["tp2"],
        "confidence":sig["confidence"],"time":datetime.now().strftime('%d %b | %H:%M'),
        "result":"PENDING"
    })

def get_bot_accuracy():
    total=daily_stats["wins"]+daily_stats["losses"]
    if total==0: return 0
    return round(daily_stats["wins"]/total*100,1)

# ─── CHANNEL SENDER ──────────────────────────────────────────────

def send_to_channel(message):
    try:
        r=requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                        json={"chat_id":CHANNEL_ID,"text":message,"parse_mode":"HTML"},timeout=10)
        return r.json()
    except: return None

def check_and_send_signals():
    check_pending_signals()
    print(f"[{datetime.now().strftime('%H:%M')}] Scanning...")
    sent=0
    for pair,asset_info in ASSETS.items():
        sig=get_signal(pair,"15min")
        if sig:
            key=f"{pair}_{sig['direction']}"
            if time.time()-last_signals.get(key,0)<3600: continue
            result=send_to_channel(format_signal(sig,asset_info,"15min"))
            if result and result.get("ok"):
                print(f"Signal: {asset_info['name']} {sig['direction']}")
                last_signals[key]=time.time()
                signal_history.append({"pair":pair,"direction":sig['direction'],
                    "confidence":sig['confidence'],"time":datetime.now().strftime('%d %b | %H:%M')})
                daily_stats["total"]+=1
                # Add to pending check — check after 30 min
                uids=list(user_signals.keys())
                pending_checks.append({
                    "signal":sig,"uids":uids,
                    "check_after":time.time()+1800,
                    "created_at":time.time()
                })
                sent+=1
            time.sleep(2)
        time.sleep(1)
    if sent==0: print("No signals.")

# ─── KEYBOARDS ───────────────────────────────────────────────────

def main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Get Signals",     callback_data="get_signals"),
         InlineKeyboardButton("💰 Risk Calculator", callback_data="risk_calc")],
        [InlineKeyboardButton("👤 My Dashboard",    callback_data="dashboard"),
         InlineKeyboardButton("📋 My History",      callback_data="my_history")],
        [InlineKeyboardButton("🏆 Accuracy",        callback_data="show_accuracy"),
         InlineKeyboardButton("🆘 Support",         callback_data="support")],
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
        [InlineKeyboardButton("🥇 Gold",     callback_data="asset_XAU/USD"),
         InlineKeyboardButton("₿ Bitcoin",  callback_data="asset_BTC/USD")],
        [InlineKeyboardButton("💎 Ethereum", callback_data="asset_ETH/USD"),
         InlineKeyboardButton("💶 EUR/USD",  callback_data="asset_EUR/USD")],
        [InlineKeyboardButton("💷 GBP/USD",  callback_data="asset_GBP/USD"),
         InlineKeyboardButton("💴 USD/JPY",  callback_data="asset_USD/JPY")],
        [InlineKeyboardButton("📊 All Assets",callback_data="asset_ALL")],
        [InlineKeyboardButton("🔙 Back",     callback_data="dashboard")]
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
        [InlineKeyboardButton("1:2",callback_data="rr_2"),InlineKeyboardButton("1:3",callback_data="rr_3"),InlineKeyboardButton("1:5",callback_data="rr_5")],
        [InlineKeyboardButton("1:7",callback_data="rr_7"),InlineKeyboardButton("1:10",callback_data="rr_10"),InlineKeyboardButton("✏️ Custom",callback_data="rr_custom")],
        [InlineKeyboardButton("🔙 Back",callback_data="go_back")]
    ])

def back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Menu",callback_data="go_back")]])

# ─── COMMANDS ────────────────────────────────────────────────────

async def setup_commands(app):
    await app.bot.set_my_commands([
        BotCommand("start",      "🚀 Start the bot"),
        BotCommand("signals",    "📊 Get trading signals"),
        BotCommand("calculator", "💰 Risk calculator"),
        BotCommand("dashboard",  "👤 My dashboard"),
        BotCommand("history",    "📋 My signal history"),
        BotCommand("accuracy",   "🏆 Bot accuracy"),
        BotCommand("support",    "🆘 Get help"),
        BotCommand("help",       "❓ Help guide"),
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id; name=update.effective_user.first_name
    get_user(uid)
    await update.message.reply_text(f"""『 👑 <b>PIPALERT PRO</b> 👑 』
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
👋 Welcome, <b>{name}</b>!
<b>Your Smart AI-Powered Trading Signals</b>
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
✅ 🥇 Gold  ✅ ₿ BTC  ✅ 💎 ETH
✅ 💶 EUR/USD  ✅ 💷 GBP/USD  ✅ 💴 USD/JPY
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
🔥 <b>NEW: Auto Win/Loss Detection!</b>
Bot automatically checks if TP or SL hit
and sends you the result instantly! 🤖
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
📊 RSI | MACD | Bollinger | MA
📊 Python Charts + TradingView
⏱️ Timeframes: 1Min → 1Day
💰 Custom Risk Calculator
👤 Personal Win/Loss Dashboard
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
⚠️ <i>Educational only. Not financial advice.</i>
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
👇 Get Started:""", parse_mode="HTML", reply_markup=main_kb())

async def signals_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("『 ⏱️ <b>SELECT TIMEFRAME</b> ⏱️ 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\nWhich timeframe do you want to trade on?\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰", parse_mode="HTML", reply_markup=tf_kb())

async def calculator_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id; user_states[uid]="waiting_account"
    await update.message.reply_text("『 💰 <b>RISK CALCULATOR</b> 💰 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\nEnter your account balance:\n💡 Example: <code>1000</code>\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n👇 Type amount:", parse_mode="HTML")

async def dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id; name=update.effective_user.first_name
    profile=get_user(uid); stats=get_user_stats(uid)
    al=profile.get("assets","ALL")
    labels={"ALL":"📊 All","XAU/USD":"🥇 Gold","BTC/USD":"₿ BTC","ETH/USD":"💎 ETH",
            "EUR/USD":"💶 EUR/USD","GBP/USD":"💷 GBP/USD","USD/JPY":"💴 USD/JPY"}
    asset_show=labels.get(al,"📊 All")
    acc=profile.get("account"); rp=profile.get("risk_pct"); rr=profile.get("rr_ratio")
    tf=TIMEFRAMES.get(profile.get("interval","15min"),{}).get("label","15 Min")
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton("🎯 Change Asset",   callback_data="change_asset"),
         InlineKeyboardButton("💰 Set Account",    callback_data="risk_calc")],
        [InlineKeyboardButton("📋 Signal History", callback_data="my_history"),
         InlineKeyboardButton("📊 Get Signals",    callback_data="get_signals")],
        [InlineKeyboardButton("🔙 Back",           callback_data="go_back")]
    ])
    await update.message.reply_text(f"""『 👤 <b>MY DASHBOARD</b> 👤 』
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
👋 <b>{name}</b>
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
⚙️ <b>Settings:</b>
├ 🎯 Asset     ➤  {asset_show}
├ ⏱️ Timeframe ➤  {tf}
├ 💰 Account   ➤  {'$'+f'{acc:,.2f}' if acc else 'Not set'}
├ ⚡ Risk %    ➤  {str(rp)+'%' if rp else 'Not set'}
└ 📐 R:R       ➤  {'1:'+str(rr) if rr else 'Not set'}
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
📊 <b>My Trading Stats:</b>
├ 📈 Total   ➤  {stats['total']}
├ ✅ Wins    ➤  {stats['wins']}
├ ❌ Losses  ➤  {stats['losses']}
├ ⏳ Pending ➤  {stats['pending']}
└ 🏆 Win Rate ➤  {stats['winrate']}%
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
🤖 Bot Accuracy: {get_bot_accuracy()}%
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰""", parse_mode="HTML", reply_markup=kb)

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    sigs=user_signals.get(uid,[])
    if not sigs:
        await update.message.reply_text("『 📋 <b>HISTORY</b> 📋 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n📊 No history yet!\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n🚀 @PipAlertProSignals", parse_mode="HTML"); return
    recent=sigs[-8:]
    hist=""
    for s in reversed(recent):
        r={"WIN":"✅","LOSS":"❌"}.get(s.get("result",""),"⏳")
        d="🟢" if s['direction']=="BUY" else "🔴"
        hist+=f"  {r} {d} {s['pair']}  •  ${s['entry']}  •  {s['time']}\n"
    stats=get_user_stats(uid)
    await update.message.reply_text(f"""『 📋 <b>MY SIGNAL HISTORY</b> 📋 』
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
✅ {stats['wins']}  ❌ {stats['losses']}  ⏳ {stats['pending']}  🏆 {stats['winrate']}%
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
{hist}
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
⚡ Results auto-update when TP/SL hits!
🚀 @PipAlertProSignals""", parse_mode="HTML", reply_markup=back_kb())

async def accuracy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    acc=get_bot_accuracy()
    total=daily_stats["wins"]+daily_stats["losses"]
    bar="█"*int(acc/10)+"░"*(10-int(acc/10))
    if acc>=70: grade="EXCELLENT 🔥"; color="🟢"
    elif acc>=60: grade="GOOD ⚡"; color="🟡"
    else: grade="MODERATE 📊"; color="🟠"
    await update.message.reply_text(f"""『 🏆 <b>BOT ACCURACY</b> 🏆 』
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
📊 <b>Today's Performance:</b>
├ Total Signals  ➤  {daily_stats['total']}
├ ✅ Wins        ➤  {daily_stats['wins']}
├ ❌ Losses      ➤  {daily_stats['losses']}
└ 🤖 Accuracy    ➤  {acc}%
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
<code>{bar}</code>
{color} {grade}
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
📅 Date: {str(date.today())}
⚡ Auto Win/Loss detection active!
▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰
🚀 @PipAlertProSignals""", parse_mode="HTML", reply_markup=back_kb())

async def support_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id; user_states[uid]="waiting_support"
    await update.message.reply_text("『 🆘 <b>SUPPORT CENTER</b> 🆘 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\nNeed help? We're here!\n\n📝 Type your message below.\nWe'll reply as soon as possible.\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n💡 Common issues:\n• No signal? Try diff timeframe\n• Chart error? Check internet\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n👇 Type your question:", parse_mode="HTML",
    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel",callback_data="go_back")]]))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("『 ❓ <b>HELP & GUIDE</b> ❓ 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n/start /signals /calculator\n/dashboard /history /accuracy\n/support /help\n\n🟢 BUY = Long 📈\n🔴 SELL = Short 📉\n🎯 TP = Take Profit\n🛑 SL = Stop Loss\n\n🤖 <b>Auto Win/Loss:</b>\nBot checks price every 15 min\nand sends result when TP/SL hits!\n\n⚠️ Risk 0.5–1% only!\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n🚀 @PipAlertProSignals", parse_mode="HTML", reply_markup=main_kb())

# ─── TEXT HANDLER ────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id; name=update.effective_user.first_name
    text=update.message.text.strip(); state=user_states.get(uid,"")
    if state=="waiting_support":
        user_states.pop(uid,None)
        if SUPPORT_CHAT_ID:
            try: await context.bot.send_message(chat_id=SUPPORT_CHAT_ID,text=f"🆘 <b>Support</b>\n👤 {name} (ID:{uid})\n\n💬 {text}",parse_mode="HTML")
            except: pass
        await update.message.reply_text("『 ✅ <b>MESSAGE SENT</b> ✅ 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n📨 Your message received!\nWe'll get back to you soon.\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n🚀 @PipAlertProSignals",parse_mode="HTML",reply_markup=main_kb()); return
    tc=text.replace("$","").replace(",","")
    if state=="waiting_account":
        try:
            amt=float(tc)
            if amt<=0: raise ValueError
            get_user(uid)["account"]=amt; user_states[uid]="waiting_rp"
            await update.message.reply_text(f"『 ⚡ <b>RISK %</b> ⚡ 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n✅ Account: <code>${amt:,.2f}</code>\n\nHow much % risk per trade?\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰",parse_mode="HTML",reply_markup=risk_kb())
        except: await update.message.reply_text("❌ Invalid! Example: <code>1000</code>",parse_mode="HTML")
    elif state=="waiting_custom_rp":
        try:
            rp=float(tc)
            if rp<=0 or rp>100: raise ValueError
            get_user(uid)["risk_pct"]=rp; user_states[uid]="waiting_rr"
            acc=user_profiles[uid].get("account",0)
            await update.message.reply_text(f"『 📐 <b>R:R</b> 📐 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n✅ Account: <code>${acc:,.2f}</code>\n✅ Risk: {rp}%\n\nSelect R:R:\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰",parse_mode="HTML",reply_markup=rr_kb())
        except: await update.message.reply_text("❌ Invalid! Example: <code>2</code>",parse_mode="HTML")
    elif state=="waiting_custom_rr":
        try:
            rr=float(tc)
            if rr<=0: raise ValueError
            get_user(uid)["rr_ratio"]=rr; user_states[uid]=""
            await update.message.reply_text("『 ⏱️ <b>SELECT TIMEFRAME</b> ⏱️ 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\nSelect timeframe:\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰",parse_mode="HTML",reply_markup=tf_kb())
        except: await update.message.reply_text("❌ Invalid! Example: <code>3</code>",parse_mode="HTML")

# ─── BUTTON HANDLER ──────────────────────────────────────────────

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query=update.callback_query; await query.answer()
    uid=query.from_user.id; data=query.data; get_user(uid)

    if data.startswith("tf_"):
        interval=data.replace("tf_","")
        user_profiles[uid]["interval"]=interval
        tf=TIMEFRAMES.get(interval,{})
        acc=user_profiles[uid].get("account"); rp=user_profiles[uid].get("risk_pct"); rr=user_profiles[uid].get("rr_ratio")
        selected=user_profiles[uid].get("assets","ALL")
        await query.edit_message_text(f"『 ⏳ <b>SCANNING</b> ⏳ 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n🔍 Analyzing <b>{tf.get('label')}</b>...\n📊 Generating charts...\n⚡ Please wait...",parse_mode="HTML")
        pairs=[(selected,ASSETS[selected])] if selected!="ALL" and selected in ASSETS else list(ASSETS.items())
        found=False
        for pair,asset_info in pairs:
            sig=get_signal(pair,interval)
            if sig:
                msg=format_signal(sig,asset_info,interval,acc,rp,rr)
                tv_link=get_tv_link(pair,interval)
                kb=InlineKeyboardMarkup([[InlineKeyboardButton("📈 Live TradingView Chart",url=tv_link)]])
                await query.message.reply_text(msg,parse_mode="HTML",reply_markup=kb)
                chart_buf=generate_chart(pair,interval,sig)
                if chart_buf: await query.message.reply_photo(photo=chart_buf,caption=chart_cap(sig,asset_info,interval),parse_mode="HTML")
                add_user_signal(uid,sig)
                signal_history.append({"pair":pair,"direction":sig['direction'],"confidence":sig['confidence'],"time":datetime.now().strftime('%d %b | %H:%M')})
                daily_stats["total"]+=1
                # Add to auto-check
                pending_checks.append({"signal":sig,"uids":[uid],"check_after":time.time()+900,"created_at":time.time()})
                found=True; time.sleep(1)
        if not found:
            await query.message.reply_text(f"『 📊 <b>NO SIGNAL</b> 📊 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n⚠️ No strong signals on <b>{tf.get('label')}</b>.\n💡 Try another timeframe!\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n🚀 @PipAlertProSignals",parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Try Again",callback_data="get_signals"),InlineKeyboardButton("🏠 Menu",callback_data="go_back")]]))
        user_states.pop(uid,None)

    elif data.startswith("asset_"):
        asset=data.replace("asset_",""); user_profiles[uid]["assets"]=asset
        labels={"ALL":"📊 All Assets","XAU/USD":"🥇 Gold","BTC/USD":"₿ Bitcoin","ETH/USD":"💎 Ethereum","EUR/USD":"💶 EUR/USD","GBP/USD":"💷 GBP/USD","USD/JPY":"💴 USD/JPY"}
        label=labels.get(asset,"📊 All")
        await query.edit_message_text(f"『 ✅ <b>ASSET SET</b> ✅ 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n✅ Asset: <b>{label}</b>\nYou'll receive signals for this asset!\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰",parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎯 Change Again",callback_data="change_asset"),InlineKeyboardButton("📊 Get Signals",callback_data="get_signals")],[InlineKeyboardButton("🔙 Back",callback_data="go_back")]]))

    elif data=="change_asset":
        await query.edit_message_text("『 🎯 <b>SELECT ASSET</b> 🎯 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\nWhich asset do you want?\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰",parse_mode="HTML",reply_markup=asset_kb())

    elif data=="dashboard":
        profile=user_profiles.get(uid,{}); stats=get_user_stats(uid); name=query.from_user.first_name
        al=profile.get("assets","ALL")
        labels={"ALL":"📊 All","XAU/USD":"🥇 Gold","BTC/USD":"₿ BTC","ETH/USD":"💎 ETH","EUR/USD":"💶 EUR/USD","GBP/USD":"💷 GBP/USD","USD/JPY":"💴 USD/JPY"}
        asset_show=labels.get(al,"📊 All")
        acc=profile.get("account"); rp=profile.get("risk_pct"); rr=profile.get("rr_ratio")
        tf=TIMEFRAMES.get(profile.get("interval","15min"),{}).get("label","15 Min")
        kb=InlineKeyboardMarkup([[InlineKeyboardButton("🎯 Change Asset",callback_data="change_asset"),InlineKeyboardButton("💰 Set Account",callback_data="risk_calc")],[InlineKeyboardButton("📋 History",callback_data="my_history"),InlineKeyboardButton("📊 Get Signals",callback_data="get_signals")],[InlineKeyboardButton("🔙 Back",callback_data="go_back")]])
        await query.edit_message_text(f"『 👤 <b>MY DASHBOARD</b> 👤 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n👋 <b>{name}</b>\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n⚙️ <b>Settings:</b>\n├ 🎯 Asset ➤ {asset_show}\n├ ⏱️ TF ➤ {tf}\n├ 💰 Acc ➤ {'$'+f'{acc:,.2f}' if acc else 'Not set'}\n├ ⚡ Risk ➤ {str(rp)+'%' if rp else 'Not set'}\n└ 📐 R:R ➤ {'1:'+str(rr) if rr else 'Not set'}\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n📊 <b>Stats:</b>\n├ Total ➤ {stats['total']}\n├ ✅ Wins ➤ {stats['wins']}\n├ ❌ Loss ➤ {stats['losses']}\n└ 🏆 Rate ➤ {stats['winrate']}%\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n🤖 Bot Accuracy: {get_bot_accuracy()}%\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰",parse_mode="HTML",reply_markup=kb)

    elif data=="my_history":
        sigs=user_signals.get(uid,[])
        if not sigs:
            await query.edit_message_text("📋 No history yet!\n\n🚀 @PipAlertProSignals",parse_mode="HTML",reply_markup=back_kb()); return
        recent=sigs[-6:]
        hist=""
        for s in reversed(recent):
            r={"WIN":"✅","LOSS":"❌"}.get(s.get("result",""),"⏳"); d="🟢" if s['direction']=="BUY" else "🔴"
            hist+=f"  {r} {d} {s['pair']}  •  ${s['entry']}  •  {s['time']}\n"
        stats=get_user_stats(uid)
        await query.edit_message_text(f"『 📋 <b>HISTORY</b> 📋 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n✅ {stats['wins']}  ❌ {stats['losses']}  ⏳ {stats['pending']}  🏆 {stats['winrate']}%\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n{hist}\n⚡ Results auto-update!\n🚀 @PipAlertProSignals",parse_mode="HTML",reply_markup=back_kb())

    elif data=="show_accuracy":
        acc=get_bot_accuracy(); bar="█"*int(acc/10)+"░"*(10-int(acc/10))
        if acc>=70: grade="EXCELLENT 🔥"; ce="🟢"
        elif acc>=60: grade="GOOD ⚡"; ce="🟡"
        else: grade="MODERATE 📊"; ce="🟠"
        await query.edit_message_text(f"『 🏆 <b>BOT ACCURACY</b> 🏆 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\nTotal: {daily_stats['total']} | ✅ {daily_stats['wins']} | ❌ {daily_stats['losses']}\n\n<code>{bar}</code>\n{ce} Accuracy: {acc}% — {grade}\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n🚀 @PipAlertProSignals",parse_mode="HTML",reply_markup=back_kb())

    elif data=="support":
        user_states[uid]="waiting_support"
        await query.edit_message_text("『 🆘 <b>SUPPORT</b> 🆘 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\nNeed help? Type your message below!\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n👇 Type your question:",parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel",callback_data="go_back")]]))

    elif data=="get_signals":
        await query.edit_message_text("『 ⏱️ <b>SELECT TIMEFRAME</b> ⏱️ 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\nWhich timeframe do you want to trade on?\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰",parse_mode="HTML",reply_markup=tf_kb())

    elif data=="go_back":
        user_states.pop(uid,None); name=query.from_user.first_name
        await query.edit_message_text(f"『 👑 <b>PIPALERT PRO</b> 👑 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\nWelcome back, <b>{name}</b>!\n👇 Choose an option:",parse_mode="HTML",reply_markup=main_kb())

    elif data=="risk_calc":
        user_states[uid]="waiting_account"
        await query.edit_message_text("『 💰 <b>RISK CALCULATOR</b> 💰 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\nEnter account balance:\n💡 Example: <code>1000</code>\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n👇 Type in chat:",parse_mode="HTML")

    elif data.startswith("rp_"):
        if data=="rp_custom":
            user_states[uid]="waiting_custom_rp"
            await query.edit_message_text("『 ✏️ <b>CUSTOM %</b> ✏️ 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\nType custom risk %:\n💡 Example: <code>1.5</code>",parse_mode="HTML")
        else:
            rp=int(data.replace("rp_","")); user_profiles[uid]["risk_pct"]=rp
            acc=user_profiles[uid].get("account",0)
            await query.edit_message_text(f"『 📐 <b>R:R RATIO</b> 📐 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n✅ Account: <code>${acc:,.2f}</code>\n✅ Risk: {rp}%\n\nSelect R:R:\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰",parse_mode="HTML",reply_markup=rr_kb())

    elif data.startswith("rr_"):
        if data=="rr_custom":
            user_states[uid]="waiting_custom_rr"
            await query.edit_message_text("『 ✏️ <b>CUSTOM R:R</b> ✏️ 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\nType reward ratio:\n💡 Example: <code>4</code> = 1:4",parse_mode="HTML")
        else:
            rr=int(data.replace("rr_","")); user_profiles[uid]["rr_ratio"]=rr
            acc=user_profiles[uid].get("account",0); rp=user_profiles[uid].get("risk_pct",1)
            await query.edit_message_text(f"『 ⏱️ <b>SELECT TIMEFRAME</b> ⏱️ 』\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰\n✅ Account: <code>${acc:,.2f}</code>\n✅ Risk: {rp}%\n✅ R:R: 1:{rr}\n\nSelect timeframe:\n▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰▰",parse_mode="HTML",reply_markup=tf_kb())

# ─── SCHEDULER & MAIN ────────────────────────────────────────────

def run_scheduler():
    schedule.every(15).minutes.do(check_and_send_signals)
    schedule.every(10).minutes.do(check_pending_signals)
    while True: schedule.run_pending(); time.sleep(30)

def main():
    global _app
    print("PipAlert Pro — v12.0 AUTO WIN/LOSS")
    _app=Application.builder().token(TELEGRAM_TOKEN).job_queue(None).build()
    async def post_init(application): await setup_commands(application)
    _app.post_init=post_init
    for cmd,func in [("start",start),("signals",signals_command),("calculator",calculator_command),
                     ("dashboard",dashboard_command),("history",history_command),
                     ("accuracy",accuracy_command),("support",support_command),("help",help_command)]:
        _app.add_handler(CommandHandler(cmd,func))
    _app.add_handler(CallbackQueryHandler(button_handler))
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle_text))
    check_and_send_signals()
    threading.Thread(target=run_scheduler,daemon=True).start()
    print("Bot running!")
    _app.run_polling(drop_pending_updates=True)

if __name__=="__main__":
    main()
