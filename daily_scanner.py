import yfinance as yf
import pandas as pd
import pandas_ta as ta
import datetime
import logging
import os
import requests

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger("ProductionScannerV6")

# ==============================================================================
# 🚀 V6 PRODUCTION SCANNER: MACRO SWING ENGINE
# ==============================================================================
PORTFOLIO_SIZE = 5000.0
BASE_RISK_PCT = 0.02         # 2% standard risk
VOL_SCALE_THRESHOLD = 3.5    # Halve risk if ATR% > 3.5%
TARGET_MULT = 4.5
BREAKEVEN_MULT = 2.0

TICKERS = [
    "SPY", "QQQ", "IWM", "^VIX",
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AVGO", 
    "PEP", "COST", "CSCO", "TMUS", "ADBE", "TXN", "NFLX", "AMD", 
    "QCOM", "INTC", "INTU", "AMAT", "CMCSA", "HON", "AMGN", "IBM",
    "NOW", "CRM", "ORCL", "UBER", "ABNB", "PLTR", "SNOW", "CRWD",
    "PANW", "SMCI", "MU", "LRCX", "KLAC", "SNPS", "CDNS", "MELI"
]

def fetch_and_prep_data(ticker):
    df = yf.download(ticker, period="60d", interval="1d", progress=False)
    if df.empty: return None
    
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)
        
    df = df[['Open', 'High', 'Low', 'Close', 'Volume']]
    
    df['EMA_20'] = ta.ema(df['Close'], length=20)
    df['EMA_20_Slope'] = df['EMA_20'].diff()
    df['ATR_14'] = ta.atr(df['High'], df['Low'], df['Close'], length=14)
    df['AvgVol_20'] = df['Volume'].rolling(20).mean()
    df['RVOL'] = df['Volume'] / df['AvgVol_20']
    df['Prior_10d_High'] = df['High'].rolling(10).max().shift(1)
    df['ATR_Pct'] = (df['ATR_14'] / df['Close']) * 100
    df['Dollar_Vol'] = df['AvgVol_20'] * df['Close']
    
    return df.dropna()

def send_telegram_alert(message):
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    
    if not bot_token or not chat_id:
        logger.warning("⚠️ Telegram credentials not found in Environment Variables. Printing to console only.")
        return
        
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    }
    
    try:
        response = requests.post(url, json=payload)
        response.raise_for_status()
    except Exception as e:
        logger.error(f"Failed to send Telegram alert: {e}")

def run_daily_scan():
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    alert_msg = f"===========================\n🛡️ **V6 MACRO SCANNER** | {today_str}\n===========================\n\n"
    
    logger.info(f"=======================================================")
    logger.info(f" 🛡️ V6 PRODUCTION SCANNER | Date: {today_str}")
    logger.info(f"=======================================================\n")
    
    logger.info("⏳ Fetching live market data...")
    market_data = {}
    for ticker in TICKERS:
        df = fetch_and_prep_data(ticker)
        if df is not None:
            market_data[ticker] = df
            
    spy_df = market_data.get("SPY")
    if spy_df is None:
        err_msg = "❌ Critical Error: Could not fetch SPY data. Aborting."
        logger.error(err_msg)
        send_telegram_alert(alert_msg + err_msg)
        return

    # 1. MACRO REGIME & CRASH SHIELD
    spy_today = spy_df.iloc[-1]
    spy_yesterday = spy_df.iloc[-2]
    
    macro_is_bullish = spy_today['Close'] > spy_today['EMA_20']
    
    spy_daily_ret = (spy_today['Close'] - spy_yesterday['Close']) / spy_yesterday['Close']
    macro_no_crash = spy_daily_ret >= -0.01 
    
    if not macro_is_bullish:
        logger.warning(f"🛑 MACRO REGIME RED: SPY is below 20 EMA. System is grounded. No new longs permitted tomorrow.\n")
        alert_msg += "🛑 **MACRO REGIME RED**: SPY is below 20 EMA.\nSystem is grounded. No new longs permitted tomorrow."
        send_telegram_alert(alert_msg)
        return
    elif not macro_no_crash:
        logger.warning(f"🛑 MACRO CRASH SHIELD ACTIVE: SPY dropped {spy_daily_ret*100:.2f}% today. System is grounded to avoid overnight contagion.\n")
        alert_msg += f"🛑 **MACRO CRASH SHIELD ACTIVE**: SPY dropped {spy_daily_ret*100:.2f}% today.\nSystem is grounded."
        send_telegram_alert(alert_msg)
        return
    else:
        logger.info(f"✅ MACRO REGIME GREEN. Scanning for V6 institutional breakouts...\n")
        alert_msg += "✅ **MACRO REGIME GREEN**.\n\n"

    # 2. SCAN FOR SETUPS
    candidate_orders = []
    
    for ticker, df in market_data.items():
        if ticker in ['SPY', 'QQQ', 'IWM', '^VIX']: continue
        
        t0 = df.iloc[-1]  
        t1 = df.iloc[-2]  
        
        is_breakout = (t0['Close'] > t0['EMA_20']) and \
                      (t0['EMA_20_Slope'] > 0) and \
                      (t1['Close'] <= t1['EMA_20'])
                      
        if is_breakout and (t0['RVOL'] > 1.0) and (t0['Close'] > t0['Prior_10d_High']) and \
           (t0['Close'] >= 10) and (t0['Dollar_Vol'] >= 20_000_000) and (t0['ATR_Pct'] <= 5.0):
            
            risk_pct = BASE_RISK_PCT * 0.5 if t0['ATR_Pct'] > VOL_SCALE_THRESHOLD else BASE_RISK_PCT
            risk_amount = PORTFOLIO_SIZE * risk_pct
            
            stop_dist = t0['ATR_14'] * 1.5
            shares = int(risk_amount / stop_dist) if stop_dist > 0 else 0
            
            max_capital = PORTFOLIO_SIZE * 0.20
            if (shares * t0['Close']) > max_capital:
                shares = int(max_capital / t0['Close'])
                
            gap_limit = t0['Close'] - (0.5 * t0['ATR_14'])
                
            candidate_orders.append({
                'ticker': ticker, 'close': t0['Close'], 'shares': shares,
                'stop_loss': t0['Close'] - stop_dist,
                'target': t0['Close'] + (t0['ATR_14'] * TARGET_MULT),
                'breakeven_trigger': t0['Close'] + (t0['ATR_14'] * BREAKEVEN_MULT),
                'gap_limit': gap_limit, 'rvol': t0['RVOL'], 'atr_pct': t0['ATR_Pct'],
                'risk_note': "HALVED (High Vol)" if t0['ATR_Pct'] > VOL_SCALE_THRESHOLD else "STANDARD"
            })

    # 3. COMPILE AND SEND ACTIONABLE ORDERS
    if not candidate_orders:
        logger.info("📡 Scan Complete. 0 valid setups found for tomorrow.")
        alert_msg += "📡 Scan Complete. **0 valid setups** found for tomorrow."
    else:
        logger.info(f"🔥 FOUND {len(candidate_orders)} INSTITUTIONAL BREAKOUT(S) 🔥\n")
        alert_msg += f"🔥 **FOUND {len(candidate_orders)} INSTITUTIONAL BREAKOUT(S)** 🔥\n\n"
        
        for order in candidate_orders:
            # Console Output
            logger.info(f"[{order['ticker']}] - ATR: {order['atr_pct']:.1f}% | RVOL: {order['rvol']:.2f}x | Risk: {order['risk_note']}")
            logger.info(f"   ➤ ACTION:      BUY {order['shares']} shares at Tomorrow's Open")
            logger.info(f"   ➤ GAP SHIELD:  ABORT trade if tomorrow's Open is below £{order['gap_limit']:.2f}")
            logger.info(f"   ➤ HARD STOP:   £{order['stop_loss']:.2f}")
            logger.info(f"   ➤ TARGET:      £{order['target']:.2f} (4.5x ATR)")
            logger.info(f"   ➤ BE TRIGGER:  £{order['breakeven_trigger']:.2f} (Move stop to entry if hit)")
            logger.info(f"   ➤ TIME EXIT:   Sell at EOD on Day 15 if target not hit.\n")
            
            # Telegram Output
            alert_msg += f"**[{order['ticker']}]** - Risk: {order['risk_note']}\n"
            alert_msg += f"➤ **ACTION:** BUY {order['shares']} shares at Open\n"
            alert_msg += f"➤ **GAP SHIELD:** Abort if Open < £{order['gap_limit']:.2f}\n"
            alert_msg += f"➤ **HARD STOP:** £{order['stop_loss']:.2f}\n"
            alert_msg += f"➤ **TARGET:** £{order['target']:.2f} (4.5x)\n"
            alert_msg += f"➤ **BE TRIGGER:** £{order['breakeven_trigger']:.2f}\n\n"

    send_telegram_alert(alert_msg)

if __name__ == "__main__":
    run_daily_scan()