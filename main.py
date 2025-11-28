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

# CONFIGURAÇÕES DE RISCO E LINK
STOP_LOSS_PCT = 0.015  # 1.5% de Stop Loss
SITE_URL = 'https://xandylima1996.github.io/bot-bitcoin/' # <--- SEU LINK NOVO AQUI

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

    # Adiciona o botão para abrir o seu site
    keyboard = {
        "inline_keyboard": [[
            {"text": "📊 Ver Gráfico e Tabela", "url": SITE_URL}
        ]]
    }

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "reply_markup": json.dumps(keyboard)
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
    try:
        docs = db.collection('historico_bitcoin').order_by('timestamp', direction=firestore.Query.DESCENDING).limit(1).stream()
        for doc in docs:
            return doc.to_dict()
    except Exception as e:
        print(f"Erro ao ler Firebase: {e}")
    return None

def analyze_and_act():
    print(f"Starting analysis for {SYMBOL} at {datetime.now()}...")
    
    try:
        df = get_data()
    except Exception as e:
        print(f"Error fetching data: {e}")
        return

    df['rsi'] = ta.rsi(df['close'], length=RSI_PERIOD)
    bb = ta.bbands(df['close'], length=BB_PERIOD, std=BB_STD)
    
    # Pega colunas pelo indice para evitar erro de nome
    bbl_col = bb.columns[0] # Lower
    bbm_col = bb.columns[1] # Middle
    bbu_col = bb.columns[2] # Upper

    df = pd.concat([df, bb], axis=1)

    last_row = df.iloc[-1]
    current_price = last_row['close']
    rsi = last_row['rsi']
    bb_lower = last_row[bbl_col]
    bb_middle = last_row[bbm_col]
    bb_upper = last_row[bbu_col]
    
    print(f"Price: {current_price:.2f} | RSI: {rsi:.2f}")

    db = init_firebase()
    last_order = get_last_position(db)
    
    last_action = 'EXIT' 
    entry_price = 0
    if last_order:
        last_action = last_order.get('action', 'EXIT')
        entry_price = last_order.get('entryPrice', 0)
        # Fallback para dados antigos
        if 'action' not in last_order and 'direction' in last_order:
             last_action = 'ENTRY'

    current_direction = last_order.get('direction') if last_order else None

    new_signal = None
    new_action = None 
    reason = ""
    stop_val = 0
    target_val = 0

    # --- LÓGICA DE ENTRADA ---
    if last_action == 'EXIT' or last_action is None:
        if rsi < 35 and current_price <= bb_lower * 1.005:
            new_signal = 'UP'
            new_action = 'ENTRY'
            reason = "RSI Baixo + Banda Inferior"
            stop_val = current_price * (1 - STOP_LOSS_PCT)
            target_val = bb_middle # Alvo inicial é a média
            
        elif rsi > 65 and current_price >= bb_upper * 0.995:
            new_signal = 'DOWN'
            new_action = 'ENTRY'
            reason = "RSI Alto + Banda Superior"
            stop_val = current_price * (1 + STOP_LOSS_PCT)
            target_val = bb_middle

    # --- LÓGICA DE SAÍDA (LUCRO + STOP LOSS) ---
    elif last_action == 'ENTRY':
        
        # 1. STOP LOSS (Proteção)
        is_stop_loss = False
        if current_direction == 'UP':
            if current_price <= entry_price * (1 - STOP_LOSS_PCT):
                is_stop_loss = True
        elif current_direction == 'DOWN':
            if current_price >= entry_price * (1 + STOP_LOSS_PCT):
                is_stop_loss = True
                
        if is_stop_loss:
            new_signal = 'STOP_LOSS'
            new_action = 'EXIT'
            reason = f"Preço atingiu limite de perda ({STOP_LOSS_PCT*100}%)"

        # 2. TAKE PROFIT (Lucro na Média)
        elif current_direction == 'UP':
            if current_price >= bb_middle:
                new_signal = 'TAKE_PROFIT'
                new_action = 'EXIT'
                reason = "Alvo atingido (Média Central)"
        elif current_direction == 'DOWN':
            if current_price <= bb_middle:
                new_signal = 'TAKE_PROFIT'
                new_action = 'EXIT'
                reason = "Alvo atingido (Média Central)"

    # --- EXECUÇÃO ---
    if new_signal:
        print(f"NOVO SINAL: {new_signal}")
        collection = db.collection('historico_bitcoin')
        
        # Calcula resultado se for saida
        outcome = "PENDING"
        profit_pct = 0
        
        if new_action == 'EXIT':
            profit_pct = ((current_price - entry_price) / entry_price) * 100
            if current_direction == 'DOWN': profit_pct *= -1
            outcome = "WIN" if profit_pct > 0 else "LOSS"

        doc_data = {
            'timestamp': int(last_row['timestamp'].timestamp() * 1000),
            'entryPrice': float(current_price),
            'direction': 'UP' if new_signal == 'UP' else ('DOWN' if new_signal == 'DOWN' else current_direction),
            'action': new_action,
            'signal_type': new_signal,
            'stopLoss': float(stop_val) if new_action == 'ENTRY' else 0,
            'takeProfit': float(target_val) if new_action == 'ENTRY' else 0,
            'outcome': outcome,
            'profit_pct': profit_pct,
            'rsi': float(rsi),
            'source': 'bot'
        }
        
        collection.add(doc_data)
        
        emoji = "🚀" if new_action == 'ENTRY' else ("💰" if outcome == 'WIN' else "🛑")
        titulo = "ENTRADA" if new_action == 'ENTRY' else ("LUCRO" if outcome == 'WIN' else "STOP LOSS")
        
        msg = (
            f"{emoji} *SINAL: {titulo}* {emoji}\n\n"
            f"📢 *Tipo*: {new_signal}\n"
            f"📝 *Motivo*: {reason}\n"
            f"💵 *Preço*: ${current_price:,.2f}\n"
        )
        if new_action == 'EXIT':
             msg += f"📊 *Resultado*: {profit_pct:.2f}% ({outcome})\n"

        msg += f"⏳ *Hora*: {datetime.now().strftime('%H:%M:%S')}"
        
        send_telegram_message(msg)
    else:
        print("Mantendo posição ou aguardando oportunidade.")

if __name__ == "__main__":
    analyze_and_act()
