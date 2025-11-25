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
SYMBOL = 'BTC/USD' # KRAKEN
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
        print("Telegram credentials not set.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, data=payload)
        print("Telegram message sent.")
    except Exception as e:
        print(f"Error sending Telegram: {e}")

def get_data():
    exchange = ccxt.kraken()
    bars = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=100)
    df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    return df

def get_last_position(db):
    # Busca a última ordem gravada no banco para saber se estamos comprados ou vendidos
    try:
        docs = db.collection('historico_bitcoin').order_by('timestamp', direction=firestore.Query.DESCENDING).limit(1).stream()
        for doc in docs:
            return doc.to_dict()
    except Exception as e:
        print(f"Erro ao ler Firebase: {e}")
    return None

def analyze_and_act():
    print(f"Starting analysis for {SYMBOL} at {datetime.now()}...")
    
    # 1. Dados e Indicadores
    try:
        df = get_data()
    except Exception as e:
        print(f"Error fetching data: {e}")
        return

    df['rsi'] = ta.rsi(df['close'], length=RSI_PERIOD)
    bb = ta.bbands(df['close'], length=BB_PERIOD, std=BB_STD)
    
    # 0=Lower, 1=Middle (Média), 2=Upper
    bbl_col = bb.columns[0]
    bbm_col = bb.columns[1] # NOVA: Linha do Meio
    bbu_col = bb.columns[2]

    df = pd.concat([df, bb], axis=1)

    last_row = df.iloc[-1]
    current_price = last_row['close']
    rsi = last_row['rsi']
    bb_lower = last_row[bbl_col]
    bb_middle = last_row[bbm_col]
    bb_upper = last_row[bbu_col]
    
    print(f"Price: {current_price:.2f} | RSI: {rsi:.2f} | Middle: {bb_middle:.2f}")

    # 2. Verificar Estado Atual (Memória)
    db = init_firebase()
    last_order = get_last_position(db)
    
    # Estado padrão: Neutro (sem posição)
    last_action = 'EXIT' 
    if last_order and 'action' in last_order:
        last_action = last_order['action'] # Pode ser 'ENTRY' ou 'EXIT'
    elif last_order and 'direction' in last_order:
         # Compatibilidade com dados antigos que não tinham o campo 'action'
         # Se o último dado foi um sinal (UP/DOWN) e não teve exit, assumimos que é ENTRY
        last_action = 'ENTRY'

    current_direction = last_order.get('direction') if last_order else None

    # 3. Lógica de Decisão
    new_signal = None
    new_action = None # ENTRY ou EXIT

    # --- CENÁRIO A: Procurar ENTRADA (se estivermos fora) ---
    if last_action == 'EXIT' or last_action is None:
        if rsi < 35 and current_price <= bb_lower * 1.005:
            new_signal = 'UP'
            new_action = 'ENTRY'
        elif rsi > 65 and current_price >= bb_upper * 0.995:
            new_signal = 'DOWN'
            new_action = 'ENTRY'

    # --- CENÁRIO B: Procurar SAÍDA (se estivermos dentro) ---
    elif last_action == 'ENTRY':
        # Se estamos comprados (UP), saímos se tocar na Média ou subir demais
        if current_direction == 'UP':
            if current_price >= bb_middle: # Tocou na média ou passou
                new_signal = 'CLOSE_UP'
                new_action = 'EXIT'
        
        # Se estamos vendidos (DOWN), saímos se tocar na Média ou cair demais
        elif current_direction == 'DOWN':
            if current_price <= bb_middle: # Tocou na média ou passou
                new_signal = 'CLOSE_DOWN'
                new_action = 'EXIT'

    # 4. Execução
    if new_signal:
        print(f"NOVO SINAL: {new_signal}")
        collection = db.collection('historico_bitcoin')
        
        doc_data = {
            'timestamp': int(last_row['timestamp'].timestamp() * 1000),
            'entryPrice': float(current_price),
            'direction': 'UP' if new_signal == 'UP' else ('DOWN' if new_signal == 'DOWN' else current_direction),
            'action': new_action, # ENTRY ou EXIT
            'signal_type': new_signal,
            'rsi': float(rsi),
            'source': 'bot'
        }
        
        collection.add(doc_data)
        
        # Mensagens diferentes para Entrada e Saída
        emoji = "🚀" if new_action == 'ENTRY' else "💰"
        titulo = "SINAL DE ENTRADA" if new_action == 'ENTRY' else "SINAL DE SAÍDA (LUCRO)"
        
        msg = (
            f"{emoji} *{titulo}* {emoji}\n\n"
            f"📢 *Ação*: {new_signal}\n"
            f"💵 *Preço Atual*: ${current_price:,.2f}\n"
            f"📊 *Ref Média*: ${bb_middle:,.2f}\n"
            f"⏳ *Hora*: {datetime.now().strftime('%H:%M:%S')}"
        )
        send_telegram_message(msg)
    else:
        print("Mantendo posição ou aguardando oportunidade.")

if __name__ == "__main__":
    analyze_and_act()
