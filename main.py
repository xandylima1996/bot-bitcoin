import os
import json
import time
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

# NOVAS CONFIGURAÇÕES (FILTROS SNIPER)
ADX_PERIOD = 14
ADX_THRESHOLD = 32     # Acima disso, mercado está perigoso (não opera)
EMA_TREND_PERIOD = 200 # Filtro de tendência macro

# CONFIGURAÇÕES DE RISCO E LINK
STOP_LOSS_PCT = 0.015  # 1.5% de Stop Loss
SITE_URL = 'https://xandylima1996.github.io/bot-bitcoin/' # Mude para seu domínio novo depois

# --- Environment Variables ---
FIREBASE_CREDS_JSON = os.environ.get('FIREBASE_CREDENTIALS')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

def init_firebase():
    if not firebase_admin._apps:
        if not FIREBASE_CREDS_JSON:
            print("ERRO CRÍTICO: FIREBASE_CREDENTIALS não encontrado.")
            return None
        try:
            creds_dict = json.loads(FIREBASE_CREDS_JSON)
            cred = credentials.Certificate(creds_dict)
            firebase_admin.initialize_app(cred)
        except Exception as e:
            print(f"Erro ao inicializar Firebase: {e}")
            return None
    return firestore.client()

def send_telegram_message(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials not set.")
        return

    # Teclado com botão para o site/checkout
    keyboard = {
        "inline_keyboard": [[
            {"text": "📊 Ver Gráfico / Assinar VIP", "url": SITE_URL}
        ]]
    }

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML", 
        "reply_markup": json.dumps(keyboard)
    }
    try:
        response = requests.post(url, data=payload)
        if response.status_code != 200:
            print(f"Erro Telegram API: {response.text}")
    except Exception as e:
        print(f"Erro sending Telegram: {e}")

def get_data():
    exchange = ccxt.kraken()
    # AUMENTADO PARA 300 (Necessário para calcular a EMA 200 com precisão)
    bars = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=300)
    df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    return df

def get_last_position(db):
    if db is None: return None
    try:
        docs = db.collection('historico_bitcoin').order_by('timestamp', direction=firestore.Query.DESCENDING).limit(1).stream()
        for doc in docs:
            return doc.to_dict()
    except Exception as e:
        print(f"Erro ao ler Firebase: {e}")
    return None

def analyze_and_act():
    print(f"--- Iniciando análise {datetime.now().strftime('%H:%M:%S')} ---")
    
    try:
        df = get_data()
    except Exception as e:
        print(f"Error fetching data: {e}")
        return

    # 1. Cálculos de Indicadores
    df['rsi'] = ta.rsi(df['close'], length=RSI_PERIOD)
    bb = ta.bbands(df['close'], length=BB_PERIOD, std=BB_STD)
    df = pd.concat([df, bb], axis=1)
    
    # ADX (Filtro de Força)
    adx_df = ta.adx(df['high'], df['low'], df['close'], length=ADX_PERIOD)
    df = pd.concat([df, adx_df], axis=1)

    # EMA 200 (Filtro de Tendência)
    df['ema_trend'] = ta.ema(df['close'], length=EMA_TREND_PERIOD)

    # Nomes das colunas do BB e ADX (variam conforme a lib)
    bbl_col = bb.columns[0] # Lower
    bbm_col = bb.columns[1] # Middle
    bbu_col = bb.columns[2] # Upper
    adx_col = f"ADX_{ADX_PERIOD}" # Geralmente 'ADX_14'

    # Pegando a última linha (Vela Fechada ou Atual)
    last_row = df.iloc[-1]
    
    current_price = last_row['close']
    rsi = last_row['rsi']
    bb_lower = last_row[bbl_col]
    bb_middle = last_row[bbm_col]
    bb_upper = last_row[bbu_col]
    current_adx = last_row[adx_col]
    ema_trend = last_row['ema_trend']

    if pd.isna(ema_trend):
        ema_trend = current_price # Fallback

    print(f"Preço: {current_price:.2f} | RSI: {rsi:.2f} | ADX: {current_adx:.2f}")
    print(f"EMA 200: {ema_trend:.2f} (Tendência: {'ALTA' if current_price > ema_trend else 'BAIXA'})")

    db = init_firebase()
    last_order = get_last_position(db)
    
    last_action = 'EXIT' 
    entry_price = 0
    current_direction = None

    if last_order:
        last_action = last_order.get('action', 'EXIT')
        entry_price = last_order.get('entryPrice', 0)
        current_direction = last_order.get('direction')
        if 'action' not in last_order and 'direction' in last_order:
             last_action = 'ENTRY'

    new_signal = None
    new_action = None 
    reason = ""
    stop_val = 0
    target_val = 0

    # --- LÓGICA DE ENTRADA ---
    if last_action == 'EXIT' or last_action is None:
        
        # 1. FILTRO DE VOLATILIDADE (ADX)
        if current_adx > ADX_THRESHOLD:
            print(f"⚠️ MERCADO PERIGOSO (ADX {current_adx:.1f} > {ADX_THRESHOLD}). Aguardando calmaria.")
        
        else:
            # 2. LÓGICA DE SINAL COM FILTRO DE TENDÊNCIA (EMA 200)
            
            # SINAL DE COMPRA (LONG)
            if rsi < 35 and current_price <= bb_lower * 1.005:
                if current_price > ema_trend:
                    new_signal = 'UP'
                    new_action = 'ENTRY'
                    reason = "Pullback em Tendência de Alta (RSI Baixo + Acima EMA200)"
                    stop_val = current_price * (1 - STOP_LOSS_PCT)
                    target_val = bb_middle
                else:
                    print("Sinal de COMPRA ignorado: Preço abaixo da EMA 200 (Contra tendência).")

            # SINAL DE VENDA (SHORT)
            elif rsi > 65 and current_price >= bb_upper * 0.995:
                if current_price < ema_trend:
                    new_signal = 'DOWN'
                    new_action = 'ENTRY'
                    reason = "Repique em Tendência de Baixa (RSI Alto + Abaixo EMA200)"
                    stop_val = current_price * (1 + STOP_LOSS_PCT)
                    target_val = bb_middle
                else:
                     print("Sinal de VENDA ignorado: Preço acima da EMA 200 (Contra tendência).")

    # --- LÓGICA DE SAÍDA ---
    elif last_action == 'ENTRY':
        is_stop_loss = False
        if current_direction == 'UP':
            if current_price <= entry_price * (1 - STOP_LOSS_PCT): is_stop_loss = True
        elif current_direction == 'DOWN':
            if current_price >= entry_price * (1 + STOP_LOSS_PCT): is_stop_loss = True
                
        if is_stop_loss:
            new_signal = 'STOP_LOSS'
            new_action = 'EXIT'
            reason = f"Stop Loss atingido ({STOP_LOSS_PCT*100}%)"

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

    # --- EXECUÇÃO E ENVIO ---
    if new_signal:
        print(f"!!! NOVO SINAL DETECTADO: {new_signal} !!!")
        
        if db:
            collection = db.collection('historico_bitcoin')
            
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
                'adx': float(current_adx), 
                'source': 'bot_v2_sniper'
            }
            
            collection.add(doc_data)
            
            emoji = "🚀" if new_action == 'ENTRY' else ("💰" if outcome == 'WIN' else "🛑")
            titulo = "ENTRADA CONFIRMADA" if new_action == 'ENTRY' else ("LUCRO REALIZADO" if outcome == 'WIN' else "STOP LOSS")
            reason_safe = reason.replace('<', '&lt;').replace('>', '&gt;')
            
            msg = (
                f"{emoji} <b>SINAL: {titulo}</b> {emoji}\n\n"
                f"📢 <b>Tipo</b>: {new_signal.replace('_', ' ')}\n"
                f"📝 <b>Motivo</b>: {reason_safe}\n"
                f"💵 <b>Preço</b>: ${current_price:,.2f}\n"
            )
            
            if new_action == 'ENTRY':
                 msg += f"📉 <b>Stop Loss</b>: ${stop_val:,.2f}\n"
                 msg += f"🎯 <b>Alvo</b>: ${target_val:,.2f}\n"

            if new_action == 'EXIT':
                 msg += f"📊 <b>Resultado</b>: {profit_pct:.2f}% ({outcome})\n"

            msg += f"⏳ <b>Hora</b>: {datetime.now().strftime('%H:%M:%S')}"
            
            send_telegram_message(msg)
    else:
        print("Nenhuma ação necessária.")

# --- MUDANÇA CRÍTICA AQUI EMBAIXO: REMOVIDO O LOOP WHILE ---
if __name__ == "__main__":
    print(f"🤖 Bot iniciado em modo Cron Job: {datetime.now()}")
    
    # Executa a análise UMA VEZ e encerra
    try:
        analyze_and_act()
        print("Análise concluída com sucesso.")
    except Exception as e:
        print(f"Erro fatal na execução: {e}")
    
    print("Desligando bot para aguardar o próximo agendamento...")
