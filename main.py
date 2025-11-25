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
SYMBOL = 'BTC/USD' # KRAKEN usa USD
TIMEFRAME = '15m'
RSI_PERIOD = 14
BB_PERIOD = 20
BB_STD = 2

# --- Environment Variables ---
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
    # Usando Kraken para evitar bloqueio nos EUA (GitHub Actions)
    exchange = ccxt.kraken()
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
    
    # Bollinger Bands (A LINHA QUE FALTAVA ESTÁ AQUI ABAIXO)
    bb = ta.bbands(df['close'], length=BB_PERIOD, std=BB_STD)
    
    # Solução Blindada: Pega pelo índice (0=Lower, 2=Upper)
    bbl_col = bb.columns[0]
    bbu_col = bb.columns[2]

    df = pd.concat([df, bb], axis=1)

    # 3. Logic
    last_row = df.iloc[-1]
    current_price = last_row['close']
    rsi = last_row['rsi']
    bb_lower = last_row[bbl_col]
    bb_upper = last_row[bbu_col]
    
    print(f"Price: {current_price}, RSI: {rsi}, BB Low: {bb_lower}, BB High: {bb_upper}")

    signal = None
    
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
            
            doc_data = {
                'timestamp': int(last_row['timestamp'].timestamp() * 1000), # ms
                'entryPrice': float(current_price),
                'direction': signal,
                'rsi': float(rsi),
                'bbLimit': float(bb_lower if signal == 'UP' else bb_upper),
                'status': 'PENDING',
                'resultPrice': None,
                'outcome': None,
                'source': 'bot'
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
