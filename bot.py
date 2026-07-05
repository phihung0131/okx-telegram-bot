import json
import os
import re
import sys
import asyncio
import logging
import logging.handlers
from dotenv import load_dotenv
from telethon import TelegramClient, events
import ccxt
import requests
from datetime import datetime

load_dotenv()

# ====================== LOGGING SETUP ======================
LOG_DIR = os.getenv("LOG_DIR", os.path.dirname(os.path.abspath(__file__)) + "/logs")
LOG_FILE = os.path.join(LOG_DIR, "bot.log")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logger = logging.getLogger("okx_bot")
logger.setLevel(LOG_LEVEL)
logger.propagate = False

_fmt = logging.Formatter(
    fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Console handler -> stdout, line-buffered/flushed explicitly so systemd/journalctl
# sees lines immediately instead of waiting for the internal buffer to fill.
_console_handler = logging.StreamHandler(stream=sys.stdout)
_console_handler.setFormatter(_fmt)
_console_handler.setLevel(LOG_LEVEL)
logger.addHandler(_console_handler)

# Rotating file handler -> bot.log, 5MB x 5 backups, useful for offline debugging
_file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(_fmt)
_file_handler.setLevel(LOG_LEVEL)
logger.addHandler(_file_handler)


class _FlushStreamHandler(logging.StreamHandler):
    """Forces an explicit flush after every emit so logs show up immediately
    even when stdout is being piped to a non-interactive consumer (systemd)."""

    def emit(self, record):
        super().emit(record)
        self.flush()


_console_handler.__class__ = _FlushStreamHandler

# Quiet down noisy third-party libraries unless we actually need their debug spam
logging.getLogger("telethon").setLevel(os.getenv("TELETHON_LOG_LEVEL", "WARNING"))
logging.getLogger("ccxt").setLevel(os.getenv("CCXT_LOG_LEVEL", "WARNING"))
logging.getLogger("urllib3").setLevel("WARNING")

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

HEALTHCHECK_CHANNEL_ID = int(os.getenv("HEALTHCHECK_CHANNEL_ID")) if os.getenv("HEALTHCHECK_CHANNEL_ID") else None
CLOSE_ON_REPLY_ENABLED = os.getenv("CLOSE_ON_REPLY_ENABLED", "True").lower() == "true"

processed_message_ids = set()
BOT_START_TIME = datetime.now()
last_signal_message_at = None

# ====================== GLOBALS ======================
POSITIONS_FILE = "open_positions.json"


def load_positions():
    try:
        with open(POSITIONS_FILE, "r") as f:
            # JSON key luôn là string, convert lại sang int
            data = {int(k): v for k, v in json.load(f).items()}
            logger.info(f"Loaded {len(data)} vị thế đang mở từ {POSITIONS_FILE}")
            return data
    except FileNotFoundError:
        logger.info(f"{POSITIONS_FILE} chưa tồn tại, khởi tạo rỗng")
        return {}
    except json.JSONDecodeError as e:
        logger.warning(f"{POSITIONS_FILE} lỗi JSON ({e}), khởi tạo rỗng")
        return {}


def save_positions(positions):
    try:
        with open(POSITIONS_FILE, "w") as f:
            json.dump(positions, f, indent=2)
        logger.debug(f"Đã lưu {len(positions)} vị thế vào {POSITIONS_FILE}")
    except Exception:
        logger.exception(f"Lỗi khi ghi {POSITIONS_FILE}")


open_positions = load_positions()


def format_price(price):
    """Format giá tránh scientific notation (lỗi với OKX khi giá quá nhỏ, vd SHIB/PEPE)."""
    return f"{price:.10f}".rstrip('0').rstrip('.') if '.' in f"{price:.10f}" else f"{price:.10f}"


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
            logger.warning(f"Telegram notify lỗi (status={resp.status_code}): {resp.text}")
        else:
            logger.debug("Telegram notify gửi thành công")
    except Exception:
        logger.exception("Telegram notify exception")


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
    logger.info(f"Loaded {len(exchange.markets)} pairs từ OKX")
    send_telegram(
        f"🤖 <b>BOT KHỞI ĐỘNG</b>\n"
        f"⏰ <code>{now()}</code>\n"
        f"🌐 Mode: <b>{'DEMO 🧪' if OKX_SANDBOX_MODE else 'REAL 💰'}</b>\n"
        f"💵 Budget/lệnh: <b>{USDT_BUDGET} USDT</b> | Đòn bẩy: <b>x{LEVERAGE}</b>\n"
        f"📡 Loaded <b>{len(exchange.markets)}</b> pairs"
    )
except Exception:
    logger.exception("Load markets thất bại")

# ====================== PARSE SIGNAL ======================
def parse_signal(text):
    try:
        coin_match = re.search(r'#(\w+)', text)
        if not coin_match:
            logger.debug("parse_signal: không tìm thấy mã coin (#XXX)")
            return None
        coin = coin_match.group(1).upper()
        symbol = f"{coin}/USDT:USDT"

        side = 'buy' if any(x in text for x in ['tăng', 'long', '🔼']) else 'sell'

        entry_match = re.search(r'Vùng tham chiếu[:\s]*([\d.]+)', text)
        sl_match = re.search(r'Ngưỡng rủi ro[:\s]*([\d.]+)', text)
        if not entry_match or not sl_match:
            logger.warning(f"parse_signal: thiếu Entry hoặc SL trong tin nhắn cho coin={coin}")
            return None

        entry = float(entry_match.group(1))
        sl = float(sl_match.group(1))

        tp_match = re.search(r'(Kháng cự 1|Hỗ trợ 1)[:\s]*([\d.]+)', text)
        tp = float(tp_match.group(2)) if tp_match else None

        logger.info(f"parse_signal OK: coin={coin} side={side} entry={entry} sl={sl} tp={tp}")
        return {'symbol': symbol, 'side': side, 'entry': entry, 'sl': sl, 'tp': tp, 'coin': coin}
    except Exception:
        logger.exception("parse_signal: lỗi không xác định khi parse tín hiệu")
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

    logger.info(f"resolve_symbol: không thấy {coin} trong markets cache, reload...")
    try:
        exchange.load_markets(reload=True)
    except Exception:
        logger.exception("resolve_symbol: load_markets(reload=True) lỗi")

    for sym in candidates:
        if sym in exchange.markets:
            return sym

    for key in exchange.markets:
        if key.startswith(f"{coin}/") and ":USDT" in key:
            logger.info(f"resolve_symbol: fuzzy match {coin} -> {key}")
            return key

    logger.warning(f"resolve_symbol: không tìm thấy market nào cho coin={coin}")
    return None


# ====================== EXECUTE TRADE ======================
def execute_trade(signal, raw_text, source_message_id):
    symbol   = signal['symbol']
    side     = signal['side']
    sl_price = signal['sl']
    tp_price = signal['tp']
    entry    = signal['entry']
    coin     = signal['coin']
    mode_tag = "🧪 DEMO" if OKX_SANDBOX_MODE else "💰 REAL"
    side_tag = "🟢 LONG" if side == "buy" else "🔴 SHORT"

    logger.info(f"[{source_message_id}] execute_trade bắt đầu: {side.upper()} {symbol} entry={entry} sl={sl_price} tp={tp_price}")

    resolved = resolve_symbol(coin)
    if not resolved:
        logger.warning(f"[{source_message_id}] MISS: không tìm thấy market cho {coin}")
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
        logger.info(f"[{source_message_id}] Symbol remapped: {symbol} → {resolved}")
    symbol = resolved

    try:
        market        = exchange.market(symbol)
        contract_size = market.get('contractSize') or 1
        notional      = USDT_BUDGET * LEVERAGE
        raw_amount    = (notional / entry) / contract_size

        min_amount = market.get('limits', {}).get('amount', {}).get('min', 1)
        logger.debug(f"[{source_message_id}] contract_size={contract_size} notional={notional} raw_amount={raw_amount} min_amount={min_amount}")

        if raw_amount < min_amount:
            logger.warning(f"[{source_message_id}] MISS: amount {raw_amount:.6f} < min {min_amount}")
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

        ticker       = exchange.fetch_ticker(symbol)
        market_price = ticker['last']
        logger.info(f"[{source_message_id}] Giá thị trường: {market_price} | Entry signal: {entry}")

        # ✅ Validate SL/TP theo giá thực, không theo entry signal
        if side == 'buy':
            if sl_price >= market_price:
                logger.warning(f"[{source_message_id}] MISS: SL {sl_price} >= market {market_price} (LONG)")
                send_telegram(
                    f"⚠️ <b>MISS LỆNH — SL KHÔNG HỢP LỆ</b>\n"
                    f"⏰ <code>{now()}</code>\n"
                    f"🪙 <b>{symbol}</b>  {side_tag}\n"
                    f"📋 Lý do: LONG nhưng SL ({sl_price}) ≥ giá TT ({market_price})\n"
                    f"💡 SL phải thấp hơn giá thị trường hiện tại\n\n"
                    f"📩 <b>Tín hiệu gốc:</b>\n<pre>{raw_text[:800]}</pre>"
                )
                return
            if tp_price and tp_price <= market_price:
                logger.warning(f"[{source_message_id}] MISS: TP {tp_price} <= market {market_price} (LONG)")
                send_telegram(
                    f"⚠️ <b>MISS LỆNH — TP KHÔNG HỢP LỆ</b>\n"
                    f"⏰ <code>{now()}</code>\n"
                    f"🪙 <b>{symbol}</b>  {side_tag}\n"
                    f"📋 Lý do: LONG nhưng TP ({tp_price}) ≤ giá TT ({market_price})\n"
                    f"💡 TP phải cao hơn giá thị trường hiện tại\n\n"
                    f"📩 <b>Tín hiệu gốc:</b>\n<pre>{raw_text[:800]}</pre>"
                )
                return
        else:  # sell/short
            if sl_price <= market_price:
                logger.warning(f"[{source_message_id}] MISS: SL {sl_price} <= market {market_price} (SHORT)")
                send_telegram(
                    f"⚠️ <b>MISS LỆNH — SL KHÔNG HỢP LỆ</b>\n"
                    f"⏰ <code>{now()}</code>\n"
                    f"🪙 <b>{symbol}</b>  {side_tag}\n"
                    f"📋 Lý do: SHORT nhưng SL ({sl_price}) ≤ giá TT ({market_price})\n"
                    f"💡 SL phải cao hơn giá thị trường hiện tại\n\n"
                    f"📩 <b>Tín hiệu gốc:</b>\n<pre>{raw_text[:800]}</pre>"
                )
                return
            if tp_price and tp_price >= market_price:
                logger.warning(f"[{source_message_id}] MISS: TP {tp_price} >= market {market_price} (SHORT)")
                send_telegram(
                    f"⚠️ <b>MISS LỆNH — TP KHÔNG HỢP LỆ</b>\n"
                    f"⏰ <code>{now()}</code>\n"
                    f"🪙 <b>{symbol}</b>  {side_tag}\n"
                    f"📋 Lý do: SHORT nhưng TP ({tp_price}) ≥ giá TT ({market_price})\n"
                    f"💡 TP phải thấp hơn giá thị trường hiện tại\n\n"
                    f"📩 <b>Tín hiệu gốc:</b>\n<pre>{raw_text[:800]}</pre>"
                )
                return

        try:
            exchange.set_leverage(LEVERAGE, symbol, {'marginMode': 'cross'})
            logger.debug(f"[{source_message_id}] set_leverage x{LEVERAGE} OK cho {symbol}")
        except Exception:
            logger.warning(f"[{source_message_id}] set_leverage warning", exc_info=True)

        attach_algo = [{
            "attachAlgoOrdType": "conditional",
            "slTriggerPx": format_price(sl_price),
            "slOrdPx": "-1",
            "slTriggerPxType": "last",
        }]
        if tp_price:
            attach_algo.append({
                "attachAlgoOrdType": "conditional",
                "tpTriggerPx": format_price(tp_price),
                "tpOrdPx": "-1",
                "tpTriggerPxType": "last",
            })

        params = {
            "tdMode": "cross",
            "attachAlgoOrds": attach_algo
        }

        logger.info(f"[{source_message_id}] Gửi order: market {side} {amount} {symbol} | params={params}")
        order = exchange.create_order(
            symbol=symbol,
            type='market',
            side=side,
            amount=amount,
            params=params
        )
        logger.debug(f"[{source_message_id}] Raw order response: {order}")

        order_id     = order.get('id', 'N/A')
        filled_price = order.get('average') or order.get('price') or market_price
        filled_qty_contracts = order.get('filled') or amount
        filled_qty_coin = filled_qty_contracts * contract_size
        fee_info     = order.get('fee') or {}
        fee_cost     = fee_info.get('cost', 'N/A')
        fee_curr     = fee_info.get('currency', '')
        status       = order.get('status', 'N/A')

        sl_pct  = abs(filled_price - sl_price) / filled_price * 100
        tp_pct  = abs(tp_price - filled_price) / filled_price * 100 if tp_price else 0
        rr_str  = f"{tp_pct/sl_pct:.2f}" if tp_price and sl_pct else "N/A"
        est_loss = USDT_BUDGET * (sl_pct / 100) * LEVERAGE
        est_gain = USDT_BUDGET * (tp_pct / 100) * LEVERAGE if tp_price else 0

        logger.info(f"[{source_message_id}] Order thành công: id={order_id} status={status} filled_price={filled_price}")

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
            f"  Amount           : <code>{filled_qty_coin}</code> {coin}  ({filled_qty_contracts} contracts)\n"
            f"  Notional         : <code>{notional:.2f} USDT</code>  (x{LEVERAGE})\n"
            f"  Phí giao dịch    : <code>{fee_cost} {fee_curr}</code>\n"
            f"{'─'*20}\n"
            f"<b>⚖️ RỦI RO / LỢI NHUẬN ƯỚC TÍNH</b>\n"
            f"  R:R Ratio        : <b>{rr_str}</b>\n"
            f"  Max Loss (SL hit): <code>-{est_loss:.2f} USDT</code>\n"
            f"  Max Gain (TP hit): <code>+{est_gain:.2f} USDT</code>\n"
        )
        open_positions[source_message_id] = {
            'symbol': symbol,
            'side': side,
            'coin': coin,
            'order_id': order_id,
            'entry': filled_price,
        }
        save_positions(open_positions)
        logger.info(f"[{source_message_id}] Đã lưu position vào file")

    except Exception as e:
        logger.exception(f"[{source_message_id}] Lỗi khi vào lệnh: {type(e).__name__} - {e}")
        send_telegram(
            f"❌ <b>LỖI KHI VÀO LỆNH</b>  {mode_tag}\n"
            f"⏰ <code>{now()}</code>\n"
            f"🪙 Symbol: <b>{symbol}</b>  {side_tag}\n"
            f"💥 Lỗi: <code>{str(e)[:500]}</code>\n\n"
            f"📩 <b>Tín hiệu gốc:</b>\n<pre>{raw_text[:600]}</pre>"
        )


# ====================== CLOSE POSITION ======================
def close_position(symbol, side, coin, raw_text):
    """Đóng lệnh theo giá thị trường."""
    mode_tag = "🧪 DEMO" if OKX_SANDBOX_MODE else "💰 REAL"
    close_side = 'sell' if side == 'buy' else 'buy'
    side_tag = "🟢 LONG" if side == "buy" else "🔴 SHORT"

    logger.info(f"close_position bắt đầu: {symbol} {side_tag} -> đóng bằng {close_side}")

    try:
        positions = exchange.fetch_positions([symbol])
        pos = next((p for p in positions if p['symbol'] == symbol and float(p['contracts'] or 0) > 0), None)

        if not pos:
            logger.info(f"close_position: không có vị thế đang mở cho {symbol} (có thể đã tự đóng)")
            send_telegram(
                f"ℹ️ <b>KHÔNG CÓ POSITION ĐỂ ĐÓNG</b>\n"
                f"⏰ <code>{now()}</code>\n"
                f"🪙 <b>{symbol}</b>  {side_tag}\n"
                f"📋 Có thể lệnh đã tự đóng bởi SL/TP\n\n"
                f"📩 <b>Tin reply:</b>\n<pre>{raw_text[:400]}</pre>"
            )
            return

        amount = float(pos['contracts'])
        ticker = exchange.fetch_ticker(symbol)
        market_price = ticker['last']

        logger.info(f"close_position: đóng {amount} contracts {symbol} bằng order {close_side} @ market~{market_price}")
        order = exchange.create_order(
            symbol=symbol,
            type='market',
            side=close_side,
            amount=amount,
            params={
                "tdMode": "cross",
                "reduceOnly": True,
            }
        )
        logger.debug(f"close_position: raw order response: {order}")

        order_id     = order.get('id', 'N/A')
        filled_price = order.get('average') or order.get('price') or market_price
        status       = order.get('status', 'N/A')

        logger.info(f"close_position: đã đóng {symbol} | order_id={order_id} filled_price={filled_price} status={status}")
        send_telegram(
            f"🔒 <b>ĐÓNG LỆNH THEO TÍN HIỆU REPLY</b>  {mode_tag}\n"
            f"{'─'*20}\n"
            f"⏰ <code>{now()}</code>\n"
            f"🪙 <b>{coin}/USDT</b>  {side_tag}\n"
            f"🆔 Close Order ID: <code>{order_id}</code>\n"
            f"📊 Status: <b>{status}</b>\n"
            f"💰 Giá đóng: <code>{filled_price}</code>\n"
            f"📦 Amount: <code>{amount}</code> {coin}\n\n"
            f"📩 <b>Tin reply:</b>\n<pre>{raw_text[:400]}</pre>"
        )

    except Exception as e:
        logger.exception(f"close_position: lỗi khi đóng lệnh {symbol}")
        send_telegram(
            f"❌ <b>LỖI KHI ĐÓNG LỆNH</b>  {mode_tag}\n"
            f"⏰ <code>{now()}</code>\n"
            f"🪙 <b>{symbol}</b>\n"
            f"💥 Lỗi: <code>{str(e)[:500]}</code>"
        )


# ====================== SYNC POSITIONS LOOP ======================
async def sync_positions_loop():
    """Định kỳ 300s check position thực tế, dọn dẹp JSON nếu đã đóng."""
    logger.info("sync_positions_loop đã khởi động (chu kỳ 300s)")
    while True:
        await asyncio.sleep(300)
        logger.debug("sync_positions_loop: tick")

        if len(processed_message_ids) > 100:
            processed_message_ids.clear()
            logger.info("Đã dọn processed_message_ids (>100 entries)")

        if not open_positions:
            continue

        try:
            actual_positions = exchange.fetch_positions()
            active_symbols = {
                p['symbol'] for p in actual_positions
                if float(p.get('contracts') or 0) > 0
            }
            logger.debug(f"sync_positions_loop: {len(active_symbols)} positions đang active trên sàn")

            closed = []
            for msg_id, pos in list(open_positions.items()):
                if pos['symbol'] not in active_symbols:
                    closed.append((msg_id, pos))

            if closed:
                for msg_id, pos in closed:
                    del open_positions[msg_id]
                    logger.info(f"Lệnh {pos['symbol']} (msg_id={msg_id}) đã tự đóng, xóa khỏi tracking")
                    send_telegram(
                        f"🔕 <b>LỆNH ĐÃ TỰ ĐÓNG</b> (SL/TP)\n"
                        f"⏰ <code>{now()}</code>\n"
                        f"🪙 <b>{pos['coin']}/USDT</b>\n"
                        f"📋 Đã xóa khỏi tracking"
                    )
                save_positions(open_positions)

        except Exception:
            logger.exception("sync_positions_loop: lỗi khi đồng bộ positions")


# ====================== TELEGRAM LISTENER ======================
client = TelegramClient('session_crypto_bot', TELEGRAM_API_ID, TELEGRAM_API_HASH)

# Lưu message_id lớn nhất đã xử lý để poll chỉ lấy tin mới hơn
_last_seen_msg_id: int = 0

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 10))  # giây, mặc định 10s


async def process_message(msg) -> None:
    """Xử lý 1 message object (dùng chung cho cả poll loop và event handler)."""
    global last_signal_message_at
    msg_id = msg.id

    if msg_id in processed_message_ids:
        logger.debug(f"Message {msg_id} đã xử lý rồi, bỏ qua")
        return
    processed_message_ids.add(msg_id)

    last_signal_message_at = datetime.now()
    text = msg.raw_text or ""
    logger.info(f"Xử lý msg_id={msg_id}: {text[:120].replace(chr(10), ' ')}...")
    send_telegram(f"📩 <b>Nhận tin mới</b> từ nhóm\n⏰ <code>{now()}</code>\n<pre>{text[:800]}</pre>")

    # ── Trường hợp 1: Tin reply → kiểm tra đóng lệnh ──
    if msg.reply_to:
        if not CLOSE_ON_REPLY_ENABLED:
            logger.debug(f"msg_id={msg_id}: là reply nhưng CLOSE_ON_REPLY_ENABLED=False, bỏ qua (để lệnh chạy tới TP/SL)")
            return  # Reply thì không xử lý tiếp

        replied_id = msg.reply_to.reply_to_msg_id
        if replied_id in open_positions:
            pos = open_positions[replied_id]
            logger.info(f"Reply cho message_id={replied_id} → Đóng {pos['symbol']}")
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None, close_position, pos['symbol'], pos['side'], pos['coin'], text
                )
            finally:
                del open_positions[replied_id]
                save_positions(open_positions)
        else:
            logger.debug(f"Reply tới msg_id={replied_id} không có trong open_positions, bỏ qua")
        return

    # ── Trường hợp 2: Tin vào lệnh gốc ──
    if "GÓC NHÌN CÁ NHÂN" not in text:
        logger.debug(f"msg_id={msg_id}: không chứa từ khóa tín hiệu, bỏ qua")
        return

    signal = parse_signal(text)
    if not signal or signal['entry'] <= 0 or signal['sl'] <= 0:
        logger.warning(f"msg_id={msg_id}: MISS LỆNH — parse thất bại hoặc Entry/SL = 0")
        send_telegram("⚠️ MISS LỆNH — Entry/SL = 0, có thể lỗi định dạng số (dấu phẩy thay vì chấm)")
        return

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, execute_trade, signal, text, msg_id)


async def poll_channel_loop():
    """
    Chủ động poll channel mỗi POLL_INTERVAL giây thay vì chờ Telegram đẩy event.

    Lý do cần poll: Telegram server KHÔNG đẩy NewMessage update về user client
    thường (non-admin) trong các channel lớn — dù client đang connected, join đủ,
    và resolve được entity. Poll là cách đáng tin cậy duy nhất trong trường hợp này.
    """
    global _last_seen_msg_id
    logger.info(f"poll_channel_loop khởi động — interval={POLL_INTERVAL}s, target={TARGET_GROUP_ID}")

    # Lần đầu: lấy message mới nhất để làm baseline, không xử lý tin cũ
    try:
        msgs = await client.get_messages(TARGET_GROUP_ID, limit=1)
        if msgs:
            _last_seen_msg_id = msgs[0].id
            logger.info(f"Baseline msg_id={_last_seen_msg_id} (tin cũ trước thời điểm start sẽ bị bỏ qua)")
    except Exception:
        logger.exception("poll_channel_loop: lỗi khi lấy baseline")

    while True:
        await asyncio.sleep(POLL_INTERVAL)
        try:
            # Lấy tối đa 10 tin mới hơn _last_seen_msg_id (đủ dùng, không spam API)
            msgs = await client.get_messages(
                TARGET_GROUP_ID,
                limit=10,
                min_id=_last_seen_msg_id,
            )
            if not msgs:
                continue

            # get_messages trả về mới nhất trước — đảo lại để xử lý theo thứ tự thời gian
            for msg in reversed(msgs):
                if msg.id > _last_seen_msg_id:
                    _last_seen_msg_id = msg.id
                    logger.debug(f"poll: phát hiện tin mới msg_id={msg.id}")
                    await process_message(msg)

        except Exception:
            logger.exception("poll_channel_loop: lỗi khi poll, sẽ thử lại sau")


# Giữ lại event handler làm fallback phòng khi Telegram bất ngờ đẩy update
# (ví dụ nếu account được promote lên admin sau này).
# processed_message_ids đảm bảo không xử lý trùng dù cả 2 đều kích hoạt.
@client.on(events.NewMessage(chats=TARGET_GROUP_ID))
async def handler(event):
    logger.debug(f"[event-push] Nhận NewMessage push msg_id={event.message.id} (fallback handler)")
    await process_message(event.message)


# ====================== HEALTHCHECK HANDLER ======================
# Đăng ký riêng, chỉ khi có cấu hình HEALTHCHECK_CHANNEL_ID. Mục đích: bất kỳ tin nhắn nào
# gửi vào channel test này (không liên quan tới logic giao dịch / sandbox mode) đều khiến
# bot trả lời ngay lập tức -> xác nhận tiến trình bot + kết nối Telegram vẫn còn sống.
# Cố tình KHÔNG gọi tới OKX exchange ở đây để không bị block/chậm bởi rate-limit hay mất
# kết nối sàn — chỉ cần Telethon trả lời được là coi như VPS + bot process còn sống.
if HEALTHCHECK_CHANNEL_ID:
    @client.on(events.NewMessage(chats=HEALTHCHECK_CHANNEL_ID))
    async def healthcheck_handler(event):
        text = event.raw_text.strip()

        # ── Lệnh /dialogs — liệt kê tất cả chat bot đang tham gia để so sánh ID ──
        if text.lower() == "/dialogs":
            try:
                lines = ["🗂 <b>DANH SÁCH CHAT BOT ĐANG THAM GIA</b>\n"]
                async for dialog in client.iter_dialogs(limit=40):
                    entity = dialog.entity
                    etype  = type(entity).__name__
                    lines.append(f"<code>{dialog.id}</code>  [{etype}]  {dialog.name}")
                lines.append(f"\n📌 TARGET_GROUP_ID hiện tại: <code>{TARGET_GROUP_ID}</code>")
                await event.reply("\n".join(lines), parse_mode="html")
                logger.info("Healthcheck /dialogs: đã trả lời danh sách dialogs")
            except Exception:
                logger.exception("healthcheck_handler /dialogs: lỗi")
                await event.reply("❌ Lỗi khi lấy danh sách dialogs, xem log.")
            return

        # ── Lệnh /target — kiểm tra bot có resolve được TARGET_GROUP_ID không ──
        if text.lower() == "/target":
            try:
                entity = await client.get_entity(TARGET_GROUP_ID)
                etype  = type(entity).__name__
                title  = getattr(entity, "title", None) or getattr(entity, "username", "N/A")
                await event.reply(
                    f"🎯 <b>TARGET_GROUP_ID CHECK</b>\n"
                    f"ID    : <code>{TARGET_GROUP_ID}</code>\n"
                    f"Type  : <code>{etype}</code>\n"
                    f"Name  : <b>{title}</b>\n"
                    f"✅ Bot resolve được entity này — nếu vẫn không nhận event thì tài khoản chưa join hoặc ID bị âm/dương sai.",
                    parse_mode="html"
                )
                logger.info(f"Healthcheck /target: resolve OK → {etype} '{title}'")
            except Exception as e:
                await event.reply(
                    f"❌ <b>Không resolve được TARGET_GROUP_ID</b>\n"
                    f"ID: <code>{TARGET_GROUP_ID}</code>\n"
                    f"Lỗi: <code>{e}</code>\n\n"
                    f"👉 Gửi /dialogs để lấy ID đúng rồi cập nhật .env",
                    parse_mode="html"
                )
                logger.warning(f"Healthcheck /target: resolve FAILED: {e}")
            return

        # ── Mặc định: status ping ──
        try:
            uptime = datetime.now() - BOT_START_TIME
            mode = "DEMO 🧪" if OKX_SANDBOX_MODE else "REAL 💰"

            if last_signal_message_at:
                idle = datetime.now() - last_signal_message_at
                idle_str = f"{int(idle.total_seconds() // 60)} phút trước"
            else:
                idle_str = "chưa nhận tin nào kể từ khi khởi động"

            reply_text = (
                f"✅ <b>BOT ĐANG SỐNG</b>\n"
                f"⏰ <code>{now()}</code>\n"
                f"🌐 Mode hiện tại: <b>{mode}</b>\n"
                f"⏳ Uptime: <b>{str(uptime).split('.')[0]}</b>\n"
                f"📡 Telegram client: <b>{'connected' if client.is_connected() else 'DISCONNECTED ⚠️'}</b>\n"
                f"📨 Tin nhắn gần nhất ở channel chính: <b>{idle_str}</b>\n"
                f"📂 Vị thế đang track: <b>{len(open_positions)}</b>\n\n"
                f"<i>Lệnh chẩn đoán: /dialogs  /target</i>"
            )

            await event.reply(reply_text, parse_mode="html")
            logger.info(f"Healthcheck ping nhận và trả lời thành công (uptime={uptime})")
        except Exception:
            logger.exception("healthcheck_handler: lỗi khi trả lời ping")
            send_telegram(
                f"⚠️ <b>HEALTHCHECK LỖI</b>\n"
                f"⏰ <code>{now()}</code>\n"
                f"📋 Bot vẫn chạy nhưng không reply được ping trong channel test, xem log để biết chi tiết."
            )
else:
    logger.info("HEALTHCHECK_CHANNEL_ID chưa được cấu hình trong .env, bỏ qua healthcheck handler")


async def main():
    mode = "DEMO 🧪" if OKX_SANDBOX_MODE else "REAL 💰"
    logger.info(f"Bot đang khởi động - Mode: {mode}")
    try:
        await client.start()
        logger.info("Telegram client đã kết nối thành công")

        # ── QUAN TRỌNG: warm-up dialog cache ──────────────────────────────────
        # Telethon user client chỉ nhận update/event từ những chat đã nằm trong
        # dialog cache của session. Nếu bỏ bước này, channel dù đã join vẫn im
        # lặng hoàn toàn — không có lỗi, không có log, event đơn giản là không
        # bao giờ được đẩy về.
        logger.info("Đang warm-up dialog cache (get_dialogs)...")
        dialogs = await client.get_dialogs()
        logger.info(f"Warm-up xong: {len(dialogs)} dialogs đã vào cache")

        # catch_up() yêu cầu Telegram gửi lại các update bị bỏ lỡ kể từ lần
        # disconnect trước — hữu ích khi VPS restart mà channel vẫn có tin mới
        await client.catch_up()
        logger.info("catch_up() hoàn tất — đã đồng bộ update bị bỏ lỡ")
        # ──────────────────────────────────────────────────────────────────────

        # Xác nhận bot có thể resolve TARGET_GROUP_ID và log tên channel
        try:
            target_entity = await client.get_entity(TARGET_GROUP_ID)
            target_name   = getattr(target_entity, "title", str(TARGET_GROUP_ID))
            logger.info(f"Đang lắng nghe channel: '{target_name}' (id={TARGET_GROUP_ID})")
            send_telegram(
                f"👂 <b>Đang lắng nghe</b>: <b>{target_name}</b>\n"
                f"🆔 <code>{TARGET_GROUP_ID}</code>"
            )
        except Exception:
            logger.exception(f"Không resolve được TARGET_GROUP_ID={TARGET_GROUP_ID}, kiểm tra lại .env")

        asyncio.create_task(sync_positions_loop())
        asyncio.create_task(poll_channel_loop())
        await client.run_until_disconnected()
    except Exception:
        logger.exception("main(): lỗi nghiêm trọng khiến bot dừng")
        raise


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot dừng bởi KeyboardInterrupt")
    except Exception:
        logger.exception("Bot crash ở top-level")
        raise