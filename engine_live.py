import yfinance as yf
import pandas as pd
import pandas_ta as ta
import datetime
import logging
import os
import json
import requests

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger("V6_Live_Engine")

# ==============================================================================
# 🚀 V6 PRODUCTION ENGINE: STATE MANAGEMENT + SCANNER
# ==============================================================================
PORTFOLIO_SIZE_GBP = 5000.0
GBP_USD_RATE = 1.25          
BASE_RISK_PCT = 0.02
VOL_SCALE_THRESHOLD = 3.5
TARGET_MULT = 4.5
BREAKEVEN_MULT = 2.0
STOP_MULT = 1.5
MAX_POSITIONS = 10
MAX_HOLD_DAYS = 15

STATE_FILE = "portfolio.json"

TICKERS = [
    "SPY", "QQQ", "IWM", "^VIX", "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", 
    "TSLA", "AVGO", "PEP", "COST", "CSCO", "TMUS", "ADBE", "TXN", "NFLX", "AMD", 
    "QCOM", "INTC", "INTU", "AMAT", "CMCSA", "HON", "AMGN", "IBM", "NOW", "CRM", 
    "ORCL", "UBER", "ABNB", "PLTR", "SNOW", "CRWD", "PANW", "SMCI", "MU", "LRCX", 
    "KLAC", "SNPS", "CDNS", "MELI"
]

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            try: return json.load(f)
            except: return {}
    # Create empty state file template if it doesn't exist
    with open(STATE_FILE, 'w') as f:
        json.dump({}, f, indent=4)
    return {}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=4)

def fetch_and_prep_data(ticker):
    df = yf.download(ticker, period="60d", interval="1d", progress=False)
    if df.empty: return None
    if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.droplevel(1)
    df = df[['Open', 'High', 'Low', 'Close', 'Volume']]
    
    df['EMA_20'] = ta.ema(df['Close'], length=20)
    df['EMA_20_Slope'] = df['EMA_20'].diff()
    df['ATR_14'] = ta.atr(df['High'], df['Low'], df['Close'], length=14)
    df['AvgVol_20'] = df['Volume'].rolling(20).mean()
    df['RVOL'] = df['Volume'] / df['AvgVol_20']
    df['Prior_10d_High'] = df['High'].rolling(10).max().shift(1)
    df['ATR_Pct'] = (df['ATR_14'] / df['Close']) * 100
    df['Dollar_Vol'] = df['AvgVol_20'] * df['Close']
    df['5d_Ret'] = df['Close'].pct_change(5)
    return df.dropna()

def send_telegram_alert(message):
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot_token or not chat_id: return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try: requests.post(url, json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"})
    except Exception as e: logger.error(f"Telegram failed: {e}")

def run_engine():
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    alert_msg = f"===========================\n🛡️ **V6 MACRO ENGINE** | {today_str}\n===========================\n\n"
    logger.info(alert_msg)
    
    # 1. LOAD STATE & MARKET DATA
    portfolio = load_state()
    market_data = {t: fetch_and_prep_data(t) for t in TICKERS}
    spy_df = market_data.get("SPY")
    if spy_df is None: return
    
    # 2. MANAGE EXISTING POSITIONS
    alert_msg += "📂 **PORTFOLIO MANAGEMENT**\n"
    if not portfolio: alert_msg += "No open positions.\n\n"
    
    positions_to_close = []
    
    for ticker, pos in portfolio.items():
        if ticker not in market_data or market_data[ticker] is None: continue
        t0 = market_data[ticker].iloc[-1]
        
        # Increment hold time
        pos['days_held'] += 1
        
        logger.info(f"Checking {ticker}: Day {pos['days_held']}/15 | Close: ${t0['Close']:.2f}")
        
        # Check Time Stop
        if pos['days_held'] >= MAX_HOLD_DAYS:
            alert_msg += f"🚨 **TIME STOP:** Sell `{ticker}` tomorrow at Open (15 days reached).\n"
            positions_to_close.append(ticker)
            continue
            
        # Check Breakeven Trigger
        if not pos.get('breakeven_triggered', False):
            if t0['High'] >= pos['breakeven_price']:
                pos['breakeven_triggered'] = True
                alert_msg += f"🛡️ **FREE RIDE SECURED:** `{ticker}` hit BE trigger. Move broker stop to entry (${pos['entry_price']:.2f}).\n"
        
        # Check if stopped out intraday (For accounting purposes only, you should rely on your broker stop)
        if t0['Low'] <= pos['stop_loss']:
            alert_msg += f"⚠️ **STOP OUT WARNING:** `{ticker}` dropped below your stop loss today.\n"
            
    # Save updated days_held and BE triggers
    save_state(portfolio)
    if portfolio: alert_msg += "\n"

    # 3. MACRO REGIME SHIELD
    spy_today, spy_yesterday = spy_df.iloc[-1], spy_df.iloc[-2]
    macro_is_bullish = spy_today['Close'] > spy_today['EMA_20']
    macro_no_crash = ((spy_today['Close'] - spy_yesterday['Close']) / spy_yesterday['Close']) >= -0.01 
    
    if not macro_is_bullish:
        alert_msg += "🛑 **MACRO REGIME RED**: SPY < 20 EMA. System grounded.\n"
        send_telegram_alert(alert_msg); logger.info("MACRO RED. Aborting scan.")
        return
    elif not macro_no_crash:
        alert_msg += "🛑 **MACRO CRASH SHIELD ACTIVE**. System grounded.\n"
        send_telegram_alert(alert_msg); logger.info("CRASH SHIELD ACTIVE. Aborting scan.")
        return

    # 4. CAPACITY CHECK
    open_slots = MAX_POSITIONS - len(portfolio)
    if open_slots <= 0:
        alert_msg += "🔒 **PORTFOLIO FULL:** 10/10 positions active. Skipping new scans.\n"
        send_telegram_alert(alert_msg); logger.info("Portfolio full. Aborting scan.")
        return

    # 5. SCAN & RANK NEW SETUPS
    candidates = []
    for ticker, df in market_data.items():
        if ticker in ['SPY', 'QQQ', 'IWM', '^VIX'] or ticker in portfolio: continue
        
        t0, t1 = df.iloc[-1], df.iloc[-2]
        is_breakout = (t0['Close'] > t0['EMA_20']) and (t0['EMA_20_Slope'] > 0) and (t1['Close'] <= t1['EMA_20'])
                      
        if is_breakout and (t0['RVOL'] > 1.0) and (t0['Close'] > t0['Prior_10d_High']) and \
           (t0['Close'] >= 10) and (t0['Dollar_Vol'] >= 20_000_000) and (t0['ATR_Pct'] <= 5.0):
            
            rs_score = t0['5d_Ret'] - spy_today['5d_Ret']
            portfolio_usd = PORTFOLIO_SIZE_GBP * GBP_USD_RATE
            risk_pct = BASE_RISK_PCT * 0.5 if t0['ATR_Pct'] > VOL_SCALE_THRESHOLD else BASE_RISK_PCT
            stop_dist = t0['ATR_14'] * STOP_MULT
            shares = int((portfolio_usd * risk_pct) / stop_dist) if stop_dist > 0 else 0
            
            candidates.append({
                'ticker': ticker, 'shares': shares, 'atr': t0['ATR_14'],
                'gap_limit': t0['Close'] - (0.5 * t0['ATR_14']),
                'rs_score': rs_score, 'close': t0['Close']
            })

    # Sort by Relative Strength and trim to available slots
    candidates = sorted(candidates, key=lambda x: x['rs_score'], reverse=True)[:open_slots]

    # 6. OUTPUT SIGNALS
    if not candidates:
        alert_msg += "📡 Scan Complete. **0 new setups** found."
    else:
        alert_msg += f"🔥 **NEW SIGNALS ({len(candidates)} needed to fill {open_slots} slots)** 🔥\n\n"
        for o in candidates:
            alert_msg += f"**[{o['ticker']}]** (RS: {o['rs_score']:.3f})\n"
            alert_msg += f"➤ BUY ~{o['shares']} shares tomorrow.\n"
            alert_msg += f"➤ GAP ABORT: Below ${o['gap_limit']:.2f}\n"
            alert_msg += f"➤ STOP: Fill Price - (${o['atr']:.2f} * {STOP_MULT})\n"
            alert_msg += f"➤ BE: Fill Price + (${o['atr']:.2f} * {BREAKEVEN_MULT})\n"
            alert_msg += f"➤ TARGET: Fill Price + (${o['atr']:.2f} * {TARGET_MULT})\n\n"

    send_telegram_alert(alert_msg)
    logger.info("Engine run complete. Telegram pushed.")

if __name__ == "__main__":
    run_engine()