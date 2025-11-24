import os
import json
import ccxt
import pandas as pd
import pandas_ta as ta
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore
import requests
from datetime import datetime

# --- Configuration ---
SYMBOL = 'BTC/USDT'
TIMEFRAME = '15m'
RSI_PERIOD = 14
BB_PERIOD = 20
BB_STD = 2

# --- Environment Variables ---
# Ensure these are set in your environment (e.g., GitHub Actions Secrets)
FIREBASE_CREDS_JSON = os.environ.get('FIREBASE_CREDENTIALS')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

def init_firebase():
    if not firebase_admin._apps:
        if not FIREBASE_CREDS_JSON:
            raise ValueError("FIREBASE_CREDENTIALS environment variable not set.")
        
        try:
            creds_dict = json.loads(FIREBASE_CREDS_JSON)
            cred = credentials.Certificate(creds_dict)
            firebase_admin.initialize_app(cred)
        except Exception as e:
            raise ValueError(f"Error initializing Firebase: {e}")
    
    return firestore.client()

def send_telegram_message(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials not set. Skipping notification.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, data=payload)
        response.raise_for_status()
        print("Telegram message sent.")
    except Exception as e:
        print(f"Error sending Telegram message: {e}")

def get_data():
    exchange = ccxt.binance()
    bars = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=100)
    df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    return df

def analyze_and_act():
    print(f"Starting analysis for {SYMBOL} at {datetime.now()}...")
    
    # 1. Get Data
    try:
        df = get_data()
    except Exception as e:
        print(f"Error fetching data: {e}")
        return

    # 2. Calculate Indicators
    # RSI
    df['rsi'] = ta.rsi(df['close'], length=RSI_PERIOD)
    
    # Bollinger Bands
    bb = ta.bbands(df['close'], length=BB_PERIOD, std=BB_STD)
    # pandas_ta returns columns like BBL_20_2.0, BBM_20_2.0, BBU_20_2.0
    # We need to rename or access them dynamically. 
    # Default names: BBL_length_std, BBM_..., BBU_...
    bbl_col = f'BBL_{BB_PERIOD}_{float(BB_STD)}'
    bbu_col = f'BBU_{BB_PERIOD}_{float(BB_STD)}'
    
    # Check if columns exist (pandas_ta naming can vary slightly by version, usually it's reliable)
    if bbl_col not in bb.columns:
        # Fallback or print columns to debug if needed, but standard naming is consistent
        print(f"Error: BB columns not found. Available: {bb.columns}")
        return

    df = pd.concat([df, bb], axis=1)

    # 3. Logic
    # We look at the LAST completed candle or the current one?
    # The prompt implies "check if there is a signal".
    # Using the latest available data point (which might be the still-open candle or the last closed one depending on fetch_ohlcv).
    # ccxt fetch_ohlcv usually returns the latest candle as the last element (often incomplete).
    # However, for a bot running on schedule, we usually want to act on the *last closed* candle to be sure.
    # BUT, the user's JS dashboard uses the *current* price.
    # Let's use the last row of the dataframe.
    
    last_row = df.iloc[-1]
    current_price = last_row['close']
    rsi = last_row['rsi']
    bb_lower = last_row[bbl_col]
    bb_upper = last_row[bbu_col]
    
    print(f"Price: {current_price}, RSI: {rsi}, BB Low: {bb_lower}, BB High: {bb_upper}")

    signal = None
    
    # Logic from dashboard:
    # UP: RSI < 35 AND Price <= BB_Lower * 1.005
    # DOWN: RSI > 65 AND Price >= BB_Upper * 0.995
    
    if rsi < 35 and current_price <= bb_lower * 1.005:
        signal = 'UP'
    elif rsi > 65 and current_price >= bb_upper * 0.995:
        signal = 'DOWN'

    if signal:
        print(f"SIGNAL DETECTED: {signal}")
        
        # 4. Action
        # Save to Firebase
        try:
            db = init_firebase()
            collection = db.collection('historico_bitcoin')
            
            # Check if we recently added a signal to avoid spamming?
            # The prompt says "Se houver sinal: Salve...". It doesn't explicitly ask for cooldown here,
            # but it's good practice. However, I'll stick to the requirements: "Se houver sinal: Salve".
            # The dashboard has a cooldown. The bot might run every 15m, so natural cooldown.
            
            doc_data = {
                'timestamp': int(last_row['timestamp'].timestamp() * 1000), # ms
                'entryPrice': float(current_price),
                'direction': signal,
                'rsi': float(rsi),
                'bbLimit': float(bb_lower if signal == 'UP' else bb_upper),
                'status': 'PENDING',
                'resultPrice': None,
                'outcome': None,
                'source': 'bot' # Tag to identify it came from the python script
            }
            
            collection.add(doc_data)
            print("Saved to Firebase.")
            
            # Send Telegram
            msg = (
                f"🚨 *SINAL BITCOIN DETECTADO* 🚨\n\n"
                f"📈 *Direção*: {signal}\n"
                f"💰 *Preço*: ${current_price:,.2f}\n"
                f"📊 *RSI*: {rsi:.2f}\n"
                f"📉 *Bandas*: {bb_lower:.2f} / {bb_upper:.2f}\n\n"
                f"⏳ *Hora*: {datetime.now().strftime('%H:%M:%S')}"
            )
            send_telegram_message(msg)
            
        except Exception as e:
            print(f"Error executing actions: {e}")
            
    else:
        print("Nada a fazer.")

if __name__ == "__main__":
    analyze_and_act()
