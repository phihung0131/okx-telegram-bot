import os
import re
import asyncio
from dotenv import load_dotenv
from telethon import TelegramClient, events
import ccxt
import time
import requests
from datetime import datetime

load_dotenv()

BOT_TOKEN = os.getenv("BOT_NOTIFY_TOKEN")
CHAT_ID = os.getenv("NOTIFY_CHAT_ID")
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")

OKX_SANDBOX_MODE = os.getenv("OKX_SANDBOX_MODE", "True").lower() == "true"

TARGET_GROUP_ID = int(os.getenv("TARGET_GROUP_ID") if not OKX_SANDBOX_MODE else os.getenv("TARGET_GROUP_ID_DEMO"))
OKX_API_KEY = os.getenv("OKX_API_KEY") if not OKX_SANDBOX_MODE else os.getenv("OKX_API_KEY_DEMO")
OKX_SECRET = os.getenv("OKX_SECRET") if not OKX_SANDBOX_MODE else os.getenv("OKX_SECRET_DEMO")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE") if not OKX_SANDBOX_MODE else os.getenv("OKX_PASSPHRASE_DEMO")

USDT_BUDGET = float(os.getenv("USDT_BUDGET", 5.0))
LEVERAGE = int(os.getenv("LEVERAGE", 5))

def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def send_telegram(text: str):
    """Gửi tin nhắn về Telegram cá nhân."""
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        resp = requests.post(url, json=payload, timeout=10)
        if not resp.ok:
            print(f"⚠️ Telegram notify lỗi: {resp.text}")
    except Exception as e:
        print(f"⚠️ Telegram notify exception: {e}")

# ====================== CCXT SETUP ======================
exchange = ccxt.okx({
    'apiKey': OKX_API_KEY,
    'secret': OKX_SECRET,
    'password': OKX_PASSPHRASE,
    'enableRateLimit': True,
    'options': {
        'defaultType': 'swap',
        'fetchMarkets': {'type': 'swap'},
    }
})
exchange.set_sandbox_mode(OKX_SANDBOX_MODE)

try:
    exchange.load_markets()
    print(f"✅ Loaded {len(exchange.markets)} pairs.")
    send_telegram(
        f"🤖 <b>BOT KHỞI ĐỘNG</b>\n"
        f"⏰ <code>{now()}</code>\n"
        f"🌐 Mode: <b>{'DEMO 🧪' if OKX_SANDBOX_MODE else 'REAL 💰'}</b>\n"
        f"💵 Budget/lệnh: <b>{USDT_BUDGET} USDT</b> | Đòn bẩy: <b>x{LEVERAGE}</b>\n"
        f"📡 Loaded <b>{len(exchange.markets)}</b> pairs"
    )
except Exception as e:
    print(f"⚠️ Load markets warning: {e}")

# ====================== PARSE SIGNAL ======================
def parse_signal(text):
    try:
        coin_match = re.search(r'#(\w+)', text)
        if not coin_match:
            return None
        coin = coin_match.group(1).upper()
        symbol = f"{coin}/USDT:USDT"

        side = 'buy' if any(x in text for x in ['tăng', 'long', '🔼']) else 'sell'

        entry = float(re.search(r'Vùng tham chiếu[:\s]*([\d.]+)', text).group(1))
        sl = float(re.search(r'Ngưỡng rủi ro[:\s]*([\d.]+)', text).group(1))

        tp_match = re.search(r'(Kháng cự 1|Hỗ trợ 1)[:\s]*([\d.]+)', text)
        tp = float(tp_match.group(2)) if tp_match else None

        return {'symbol': symbol, 'side': side, 'entry': entry, 'sl': sl, 'tp': tp, 'coin': coin}
    except Exception as e:
        return None

def resolve_symbol(coin: str) -> str | None:
    """Tìm symbol đúng trên OKX cho coin, reload markets nếu cần."""
    candidates = [
        f"{coin}/USDT:USDT",
        f"{coin}/USDT",
    ]
    for sym in candidates:
        if sym in exchange.markets:
            return sym

    # Reload markets rồi thử lại (đề phòng listing mới)
    try:
        exchange.load_markets(reload=True)
    except:
        pass

    for sym in candidates:
        if sym in exchange.markets:
            return sym

    # Tìm fuzzy theo tên coin
    for key in exchange.markets:
        if key.startswith(f"{coin}/") and ":USDT" in key:
            return key

    return None

# ====================== EXECUTE TRADE ======================
def execute_trade(signal, raw_text):
    symbol   = signal['symbol']
    side     = signal['side']
    sl_price = signal['sl']
    tp_price = signal['tp']
    entry    = signal['entry']
    coin     = signal['coin']
    mode_tag = "🧪 DEMO" if OKX_SANDBOX_MODE else "💰 REAL"
    side_tag = "🟢 LONG" if side == "buy" else "🔴 SHORT"

    print(f"\n🚀 Market + TP/SL: {side.upper()} {symbol}")

    resolved = resolve_symbol(coin)
    if not resolved:
        send_telegram(
            f"❌ <b>MISS LỆNH — KHÔNG TÌM THẤY MARKET</b>\n"
            f"⏰ <code>{now()}</code>\n"
            f"🪙 Coin: <b>{coin}</b>\n"
            f"🔍 Đã thử: <code>{coin}/USDT:USDT</code>, <code>{coin}/USDT</code>\n"
            f"📋 Lý do: Coin chưa có future/swap USDT trên OKX\n\n"
            f"📩 <b>Tín hiệu gốc:</b>\n<pre>{raw_text[:800]}</pre>"
        )
        return

    if resolved != symbol:
        print(f"   🔄 Symbol remapped: {symbol} → {resolved}")
    symbol = resolved

    try:
        market     = exchange.market(symbol)
        notional   = USDT_BUDGET * LEVERAGE
        raw_amount = notional / entry
        min_amount = market.get('limits', {}).get('amount', {}).get('min', 1)

        if raw_amount < min_amount:
            send_telegram(
                f"⚠️ <b>MISS LỆNH — AMOUNT QUÁ NHỎ</b>\n"
                f"⏰ <code>{now()}</code>\n"
                f"🪙 Symbol: <b>{symbol}</b>  {side_tag}\n"
                f"💵 Budget: {USDT_BUDGET} USDT × x{LEVERAGE} = {notional} USDT\n"
                f"📐 raw_amount: {raw_amount:.6f} | min_amount: {min_amount}\n\n"
                f"📩 <b>Tín hiệu gốc:</b>\n<pre>{raw_text[:800]}</pre>"
            )
            return

        amount = float(exchange.amount_to_precision(symbol, raw_amount))

        # ✅ Fetch giá thị trường thực tế
        ticker       = exchange.fetch_ticker(symbol)
        market_price = ticker['last']
        print(f"   📊 Giá thị trường: {market_price} | Entry signal: {entry}")

        # ✅ Validate SL/TP theo giá thực, không theo entry signal
        if side == 'buy':
            if sl_price >= market_price:
                send_telegram(
                    f"⚠️ <b>MISS LỆNH — SL KHÔNG HỢP LỆ</b>\n"
                    f"⏰ <code>{now()}</code>\n"
                    f"🪙 <b>{symbol}</b>  {side_tag}\n"
                    f"📋 Lý do: LONG nhưng SL ({sl_price}) ≥ giá TT ({market_price})\n"
                    f"💡 SL phải thấp hơn giá thị trường hiện tại\n\n"
                    f"📩 <b>Tín hiệu gốc:</b>\n<pre>{raw_text[:800]}</pre>"
                )
                print(f"   ⚠️ SL không hợp lệ: {sl_price} >= {market_price}")
                return
            if tp_price and tp_price <= market_price:
                send_telegram(
                    f"⚠️ <b>MISS LỆNH — TP KHÔNG HỢP LỆ</b>\n"
                    f"⏰ <code>{now()}</code>\n"
                    f"🪙 <b>{symbol}</b>  {side_tag}\n"
                    f"📋 Lý do: LONG nhưng TP ({tp_price}) ≤ giá TT ({market_price})\n"
                    f"💡 TP phải cao hơn giá thị trường hiện tại\n\n"
                    f"📩 <b>Tín hiệu gốc:</b>\n<pre>{raw_text[:800]}</pre>"
                )
                print(f"   ⚠️ TP không hợp lệ: {tp_price} <= {market_price}")
                return
        else:  # sell/short
            if sl_price <= market_price:
                send_telegram(
                    f"⚠️ <b>MISS LỆNH — SL KHÔNG HỢP LỆ</b>\n"
                    f"⏰ <code>{now()}</code>\n"
                    f"🪙 <b>{symbol}</b>  {side_tag}\n"
                    f"📋 Lý do: SHORT nhưng SL ({sl_price}) ≤ giá TT ({market_price})\n"
                    f"💡 SL phải cao hơn giá thị trường hiện tại\n\n"
                    f"📩 <b>Tín hiệu gốc:</b>\n<pre>{raw_text[:800]}</pre>"
                )
                print(f"   ⚠️ SL không hợp lệ: {sl_price} <= {market_price}")
                return
            if tp_price and tp_price >= market_price:
                send_telegram(
                    f"⚠️ <b>MISS LỆNH — TP KHÔNG HỢP LỆ</b>\n"
                    f"⏰ <code>{now()}</code>\n"
                    f"🪙 <b>{symbol}</b>  {side_tag}\b"
                    f"📋 Lý do: SHORT nhưng TP ({tp_price}) ≥ giá TT ({market_price})\n"
                    f"💡 TP phải thấp hơn giá thị trường hiện tại\n\n"
                    f"📩 <b>Tín hiệu gốc:</b>\n<pre>{raw_text[:800]}</pre>"
                )
                print(f"   ⚠️ TP không hợp lệ: {tp_price} >= {market_price}")
                return

        try:
            exchange.set_leverage(LEVERAGE, symbol, {'marginMode': 'cross'})
        except Exception as lev_err:
            print(f"   ⚠️ set_leverage warning: {lev_err}")

        attach_algo = []
        attach_algo.append({
            "attachAlgoOrdType": "conditional",
            "slTriggerPx": str(sl_price),
            "slOrdPx": "-1",
            "slTriggerPxType": "last",
        })
        if tp_price:
            attach_algo.append({
                "attachAlgoOrdType": "conditional",
                "tpTriggerPx": str(tp_price),
                "tpOrdPx": "-1",
                "tpTriggerPxType": "last",
            })

        params = {
            "tdMode": "cross",
            "attachAlgoOrds": attach_algo
        }

        order = exchange.create_order(
            symbol=symbol,
            type='market',
            side=side,
            amount=amount,
            params=params
        )

        order_id     = order.get('id', 'N/A')
        filled_price = order.get('average') or order.get('price') or market_price
        filled_qty   = order.get('filled') or amount
        fee_info     = order.get('fee') or {}
        fee_cost     = fee_info.get('cost', 'N/A')
        fee_curr     = fee_info.get('currency', '')
        status       = order.get('status', 'N/A')

        sl_pct  = abs(filled_price - sl_price) / filled_price * 100
        tp_pct  = abs(tp_price - filled_price) / filled_price * 100 if tp_price else 0
        rr_str  = f"{tp_pct/sl_pct:.2f}" if tp_price and sl_pct else "N/A"
        est_loss = USDT_BUDGET * (sl_pct / 100) * LEVERAGE
        est_gain = USDT_BUDGET * (tp_pct / 100) * LEVERAGE if tp_price else 0

        send_telegram(
            f"✅ <b>VÀO LỆNH THÀNH CÔNG</b>  {mode_tag}\n"
            f"{'─'*20}\n"
            f"⏰ <code>{now()}</code>\n"
            f"🪙 <b>{coin}/USDT</b>  {side_tag}\n"
            f"🆔 Order ID: <code>{order_id}</code>\n"
            f"📊 Status: <b>{status}</b>\n"
            f"{'─'*20}\n"
            f"<b>📌 TÍN HIỆU GỐC</b>\n"
            f"  Entry tham chiếu : <code>{entry}</code>\n"
            f"  Giá TT lúc vào   : <code>{market_price}</code>\n"
            f"  Stop Loss        : <code>{sl_price}</code>  (-{sl_pct:.2f}%)\n"
            f"  Take Profit      : <code>{tp_price if tp_price else 'N/A'}</code>  (+{tp_pct:.2f}%)\n"
            f"{'─'*20}\n"
            f"<b>📈 THỰC TẾ ĐÃ VÀO</b>\n"
            f"  Filled price     : <code>{filled_price}</code>\n"
            f"  Amount           : <code>{filled_qty}</code> {coin}\n"
            f"  Notional         : <code>{notional:.2f} USDT</code>  (x{LEVERAGE})\n"
            f"  Phí giao dịch    : <code>{fee_cost} {fee_curr}</code>\n"
            f"{'─'*20}\n"
            f"<b>⚖️ RỦI RO / LỢI NHUẬN ƯỚC TÍNH</b>\n"
            f"  R:R Ratio        : <b>{rr_str}</b>\n"
            f"  Max Loss (SL hit): <code>-{est_loss:.2f} USDT</code>\n"
            f"  Max Gain (TP hit): <code>+{est_gain:.2f} USDT</code>\n"
        )
        print(f"   ✅ Lệnh thành công! ID: {order_id}")

    except Exception as e:
        import traceback
        tb  = traceback.format_exc()
        print(f"❌ Lỗi: {type(e).__name__} - {e}\n{tb}")
        send_telegram(
            f"❌ <b>LỖI KHI VÀO LỆNH</b>  {mode_tag}\n"
            f"⏰ <code>{now()}</code>\n"
            f"🪙 Symbol: <b>{symbol}</b>  {side_tag}\n"
            f"💥 Lỗi: <code>{str(e)[:500]}</code>\n\n"
            f"📩 <b>Tín hiệu gốc:</b>\n<pre>{raw_text[:600]}</pre>"
        )

# ====================== TELEGRAM LISTENER ======================
client = TelegramClient('session_crypto_bot', TELEGRAM_API_ID, TELEGRAM_API_HASH)

@client.on(events.NewMessage(chats=TARGET_GROUP_ID))
async def handler(event):
    text = event.raw_text
    if "GÓC NHÌN CÁ NHÂN" not in text:
        return

    signal = parse_signal(text)

    if not signal:
        # Parse thất bại → báo miss
        send_telegram(
            f"⚠️ <b>MISS LỆNH — PARSE THẤT BẠI</b>\n"
            f"⏰ <code>{now()}</code>\n"
            f"📋 Lý do: Không đọc được tín hiệu (thiếu entry/SL/coin?)\n\n"
            f"📩 <b>Tin nhắn gốc:</b>\n<pre>{text[:800]}</pre>"
        )
        print("⚠️ Parse thất bại.")
        return

    execute_trade(signal, text)

async def main():
    mode = "DEMO 🧪" if OKX_SANDBOX_MODE else "REAL 💰"
    print(f"⚡ Bot đã chạy - Mode: {mode}")
    await client.start()
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())

# async def find_chat_ids():
#     await client.start()
#     async for dialog in client.iter_dialogs():
#         print(f"{dialog.id:<25} | {type(dialog.entity).__name__:<20} | {dialog.name}")

# if __name__ == '__main__':
#     asyncio.run(find_chat_ids())