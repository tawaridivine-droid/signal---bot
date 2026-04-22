import os
import asyncio
import aiohttp
import json
import logging
from datetime import datetime, timedelta
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")
DEEPSEEK_API_KEY  = os.getenv("DEEPSEEK_API_KEY")

SCAN_INTERVAL        = 300   # every 5 minutes
MIN_VOLUME_1H        = 50000
MIN_LIQUIDITY        = 20000
MIN_PRICE_CHANGE     = 10
MAX_PRICE_CHANGE     = 200
MIN_RUGCHECK_SCORE   = 60
MOONBAG_PERCENT      = 20
MIN_BUY_SELL_RATIO   = 1.2   # more buys than sells
MAX_TOKEN_AGE_HOURS  = 72    # ignore tokens older than 3 days

sent_signals = {}

stats = {
    "signals_sent": 0,
    "scams_filtered": 0,
    "ai_rejections": 0,
    "ratio_filtered": 0,
    "start_time": datetime.now()
}


# ── DexScreener ──────────────────────────────────────────────────────────────
async def get_trending_solana_tokens(session):
    candidates = []
    try:
        urls = [
            "https://api.dexscreener.com/token-boosts/top/v1",
            "https://api.dexscreener.com/token-boosts/latest/v1"
        ]
        for url in urls:
            async with session.get(url, timeout=15) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list):
                        for item in data:
                            if item.get("chainId") == "solana":
                                candidates.append(item.get("tokenAddress"))

        search_url = "https://api.dexscreener.com/latest/dex/search?q=solana"
        async with session.get(search_url, timeout=15) as resp:
            if resp.status == 200:
                data = await resp.json()
                for pair in data.get("pairs", []):
                    if pair.get("chainId") == "solana":
                        addr = pair.get("baseToken", {}).get("address")
                        if addr:
                            candidates.append(addr)
    except Exception as e:
        logger.error(f"DexScreener trending error: {e}")
    return list(set(candidates))[:30]


async def get_token_data(session, token_address):
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        async with session.get(url, timeout=15) as resp:
            if resp.status == 200:
                data = await resp.json()
                pairs = data.get("pairs", [])
                sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
                if not sol_pairs:
                    return None
                best = max(sol_pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0))
                return best
    except Exception as e:
        logger.error(f"Token data error {token_address}: {e}")
    return None


# ── Safety Checks ─────────────────────────────────────────────────────────────
async def check_rugcheck(session, token_address):
    try:
        url = f"https://api.rugcheck.xyz/v1/tokens/{token_address}/report/summary"
        async with session.get(url, timeout=15) as resp:
            if resp.status == 200:
                data = await resp.json()
                score = data.get("score", 0)
                risks = data.get("risks", [])
                critical = [r for r in risks if r.get("level") in ["danger", "warn"]]
                return {
                    "score": score,
                    "risks": critical,
                    "safe": score >= MIN_RUGCHECK_SCORE and len(critical) == 0
                }
    except Exception as e:
        logger.error(f"RugCheck error: {e}")
    return {"score": 0, "risks": [], "safe": False}


async def check_honeypot(session, token_address):
    try:
        url = f"https://api.honeypot.is/v2/IsHoneypot?address={token_address}&chainID=solana"
        async with session.get(url, timeout=15) as resp:
            if resp.status == 200:
                data = await resp.json()
                is_honeypot = data.get("honeypotResult", {}).get("isHoneypot", True)
                return not is_honeypot
    except:
        pass
    return False


# ── Buy/Sell Ratio Filter ─────────────────────────────────────────────────────
def check_buy_sell_ratio(token_data):
    """More buyers than sellers = bullish pressure"""
    try:
        txns = token_data.get("txns", {}).get("h1", {})
        buys = int(txns.get("buys", 0))
        sells = int(txns.get("sells", 1))
        ratio = buys / sells if sells > 0 else buys
        return ratio >= MIN_BUY_SELL_RATIO, round(ratio, 2)
    except:
        return False, 0


# ── Token Age Check ───────────────────────────────────────────────────────────
def check_token_age(token_data):
    """Prefer newer tokens with fresh momentum"""
    try:
        created_at = token_data.get("pairCreatedAt")
        if not created_at:
            return True, "Unknown"
        created = datetime.fromtimestamp(created_at / 1000)
        age = datetime.now() - created
        hours = age.total_seconds() / 3600
        if hours > MAX_TOKEN_AGE_HOURS:
            return False, f"{int(hours)}h old"
        return True, f"{int(hours)}h old"
    except:
        return True, "Unknown"


# ── DeepSeek AI Validation ────────────────────────────────────────────────────
async def validate_with_deepseek(session, token_data, rugcheck_data, buy_sell_ratio):
    if not DEEPSEEK_API_KEY:
        price_change = float(token_data.get("priceChange", {}).get("h1", 0) or 0)
        volume = float(token_data.get("volume", {}).get("h1", 0) or 0)
        liquidity = float(token_data.get("liquidity", {}).get("usd", 0) or 0)
        score = 0
        if price_change > 20: score += 25
        if price_change > 50: score += 15
        if volume > 100000: score += 25
        if liquidity > 50000: score += 20
        if buy_sell_ratio > 1.5: score += 15
        return {
            "valid": score >= 60,
            "confidence": score,
            "reason": f"Momentum score {score}/100 (no DeepSeek key)",
            "mode": "momentum"
        }

    try:
        symbol = token_data.get("baseToken", {}).get("symbol", "?")
        name = token_data.get("baseToken", {}).get("name", "Unknown")
        price_usd = token_data.get("priceUsd", "0")
        price_change_1h = token_data.get("priceChange", {}).get("h1", 0)
        price_change_24h = token_data.get("priceChange", {}).get("h24", 0)
        volume_1h = token_data.get("volume", {}).get("h1", 0)
        volume_24h = token_data.get("volume", {}).get("h24", 0)
        liquidity = token_data.get("liquidity", {}).get("usd", 0)
        market_cap = token_data.get("marketCap", 0)
        txns = token_data.get("txns", {}).get("h1", {})
        buys = txns.get("buys", 0)
        sells = txns.get("sells", 0)
        rugcheck_score = rugcheck_data.get("score", 0)

        prompt = f"""You are a professional crypto trading signal validator specializing in Solana meme coins. Analyze this token and decide if it's a strong buy signal.

Token: {symbol} ({name})
Price: ${price_usd}
Market Cap: ${market_cap:,.0f}
1h Change: {price_change_1h}%
24h Change: {price_change_24h}%
1h Volume: ${volume_1h:,.0f}
24h Volume: ${volume_24h:,.0f}
Liquidity: ${liquidity:,.0f}
1h Buys: {buys} | 1h Sells: {sells}
Buy/Sell Ratio: {buy_sell_ratio}
RugCheck Score: {rugcheck_score}/100

Be strict. Only approve tokens with genuine momentum and low rug risk.
Respond ONLY in this exact JSON format with no extra text:
{{
  "valid": true or false,
  "confidence": 0-100,
  "reason": "one sentence explanation",
  "risk_level": "low or medium or high"
}}"""

        payload = {
            "model": "deepseek-chat",
            "max_tokens": 200,
            "messages": [{"role": "user", "content": prompt}]
        }
        headers = {
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json"
        }

        async with session.post(
            "https://api.deepseek.com/v1/chat/completions",
            json=payload, headers=headers, timeout=20
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                content = data["choices"][0]["message"]["content"]
                content = content.replace("```json", "").replace("```", "").strip()
                result = json.loads(content)
                result["mode"] = "deepseek"
                return result
    except Exception as e:
        logger.error(f"DeepSeek error: {e}")

    return {"valid": False, "confidence": 0, "reason": "AI check failed", "mode": "error"}


# ── Signal Formatter ──────────────────────────────────────────────────────────
def calculate_levels(price, price_change_1h):
    price = float(price)
    momentum = float(price_change_1h or 0)

    if momentum > 50:
        tp1_pct, tp2_pct, tp3_pct, sl_pct = 0.25, 0.45, 0.80, 0.12
    elif momentum > 20:
        tp1_pct, tp2_pct, tp3_pct, sl_pct = 0.35, 0.65, 1.20, 0.15
    else:
        tp1_pct, tp2_pct, tp3_pct, sl_pct = 0.50, 1.00, 2.00, 0.20

    return {
        "entry": price,
        "tp1": round(price * (1 + tp1_pct), 8),
        "tp2": round(price * (1 + tp2_pct), 8),
        "tp3": round(price * (1 + tp3_pct), 8),
        "sl":  round(price * (1 - sl_pct), 8),
        "tp1_pct": int(tp1_pct * 100),
        "tp2_pct": int(tp2_pct * 100),
        "tp3_pct": int(tp3_pct * 100),
        "sl_pct":  int(sl_pct * 100),
    }


def format_signal_message(token_data, levels, ai_result, rugcheck_data, buy_sell_ratio, token_age):
    symbol        = token_data.get("baseToken", {}).get("symbol", "?")
    name          = token_data.get("baseToken", {}).get("name", "Unknown")
    address       = token_data.get("baseToken", {}).get("address", "")
    price_change_1h  = token_data.get("priceChange", {}).get("h1", 0)
    price_change_24h = token_data.get("priceChange", {}).get("h24", 0)
    volume_1h     = float(token_data.get("volume", {}).get("h1", 0) or 0)
    liquidity     = float(token_data.get("liquidity", {}).get("usd", 0) or 0)
    market_cap    = float(token_data.get("marketCap", 0) or 0)
    confidence    = ai_result.get("confidence", 0)
    reason        = ai_result.get("reason", "")
    risk_level    = ai_result.get("risk_level", "medium")
    rugcheck_score = rugcheck_data.get("score", 0)
    mode          = ai_result.get("mode", "momentum")

    txns = token_data.get("txns", {}).get("h1", {})
    buys  = txns.get("buys", 0)
    sells = txns.get("sells", 0)

    # Confidence emoji
    if confidence >= 80:
        conf_emoji, conf_label = "🟢", "HIGH"
    elif confidence >= 60:
        conf_emoji, conf_label = "🟡", "MEDIUM"
    else:
        conf_emoji, conf_label = "🔴", "LOW"

    # Risk emoji
    risk_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(risk_level, "🟡")

    ai_badge = "🤖 DeepSeek AI" if mode == "deepseek" else "📊 Momentum Score"

    msg = f"""
🚀 *SIGNAL ALERT — {symbol}* 🚀
━━━━━━━━━━━━━━━━━━━━━━━━
🪙 *{symbol}* | {name}
⛓ Chain: Solana | 🕐 Age: {token_age}

{conf_emoji} *AI Confidence: {confidence}% ({conf_label})*
{risk_emoji} Risk Level: {risk_level.upper()}
{ai_badge} | 🛡 RugCheck: {rugcheck_score}/100

📈 *Price Action*
- 1H Change: `+{price_change_1h}%`
- 24H Change: `{price_change_24h:+.1f}%`
- 1H Volume: `${volume_1h:,.0f}`
- Liquidity: `${liquidity:,.0f}`
- Market Cap: `${market_cap:,.0f}`
- 1H Buys/Sells: `{buys} / {sells}` (ratio: {buy_sell_ratio}x)

💰 *Trade Levels (for $1 position)*
- 🎯 Entry: `${levels['entry']}`
- 🟢 TP1 (+{levels['tp1_pct']}%): `${levels['tp1']}` → *Sell 40%*
- 🟢 TP2 (+{levels['tp2_pct']}%): `${levels['tp2']}` → *Sell 40%*
- 🌙 TP3 (+{levels['tp3_pct']}%): `${levels['tp3']}` → *Sell {100 - MOONBAG_PERCENT}% of rest*
- 🔴 Stop Loss (-{levels['sl_pct']}%): `${levels['sl']}`

🎒 *Moonbag: Keep {MOONBAG_PERCENT}% running after TP2*
_Exit moonbag only if -30% from TP2 or TP3 is hit_

🧠 *AI Analysis*
_{reason}_

📋 *Contract Address — Copy & Paste into GMGN:*
`{address}`

🔗 *Quick Links*
- [🟢 Trade on GMGN](https://gmgn.ai/sol/token/{address})
- [📊 DexScreener](https://dexscreener.com/solana/{address})
- [🛡 RugCheck](https://rugcheck.xyz/tokens/{address})
- [🐦 Birdeye](https://birdeye.so/token/{address}?chain=solana)

⚠️ _DYOR — Not financial advice. Use $1 max per signal._
⏰ {datetime.now().strftime('%H:%M:%S UTC')}
━━━━━━━━━━━━━━━━━━━━━━━━
"""
    return msg.strip()


# ── Main Scanner ──────────────────────────────────────────────────────────────
async def scan_and_signal(bot):
    logger.info("🔍 Scanning...")

    async with aiohttp.ClientSession() as session:
        addresses = await get_trending_solana_tokens(session)
        logger.info(f"Candidates: {len(addresses)}")
        signals_this_cycle = 0

        for addr in addresses:
            if addr in sent_signals:
                if datetime.now() - sent_signals[addr] < timedelta(hours=2):
                    continue

            token_data = await get_token_data(session, addr)
            if not token_data:
                continue

            try:
                liquidity     = float(token_data.get("liquidity", {}).get("usd", 0) or 0)
                volume_1h     = float(token_data.get("volume", {}).get("h1", 0) or 0)
                price_change  = float(token_data.get("priceChange", {}).get("h1", 0) or 0)
                price_usd     = float(token_data.get("priceUsd", 0) or 0)
            except:
                continue

            # Basic filters
            if liquidity < MIN_LIQUIDITY: continue
            if volume_1h < MIN_VOLUME_1H: continue
            if price_change < MIN_PRICE_CHANGE or price_change > MAX_PRICE_CHANGE: continue
            if price_usd <= 0: continue

            # Age check
            age_ok, token_age = check_token_age(token_data)
            if not age_ok:
                logger.info(f"⏳ Too old: {addr} ({token_age})")
                continue

            # Buy/sell ratio
            ratio_ok, buy_sell_ratio = check_buy_sell_ratio(token_data)
            if not ratio_ok:
                stats["ratio_filtered"] += 1
                logger.info(f"📉 Bad ratio: {addr} ({buy_sell_ratio}x)")
                continue

            # Safety checks
            rugcheck = await check_rugcheck(session, addr)
            if not rugcheck["safe"]:
                stats["scams_filtered"] += 1
                logger.info(f"❌ RugCheck fail: {addr}")
                continue

            honeypot_safe = await check_honeypot(session, addr)
            if not honeypot_safe:
                stats["scams_filtered"] += 1
                logger.info(f"❌ Honeypot: {addr}")
                continue

            # AI validation
            ai_result = await validate_with_deepseek(session, token_data, rugcheck, buy_sell_ratio)
            if not ai_result.get("valid") or ai_result.get("confidence", 0) < 60:
                stats["ai_rejections"] += 1
                logger.info(f"🤖 AI rejected: {addr}")
                continue

            # Send signal
            levels  = calculate_levels(price_usd, price_change)
            message = format_signal_message(token_data, levels, ai_result, rugcheck, buy_sell_ratio, token_age)

            try:
                await bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=message,
                    parse_mode="Markdown",
                    disable_web_page_preview=True
                )
                sent_signals[addr] = datetime.now()
                stats["signals_sent"] += 1
                signals_this_cycle += 1
                symbol = token_data.get("baseToken", {}).get("symbol", addr)
                logger.info(f"✅ Signal: {symbol}")

                if signals_this_cycle >= 3:
                    break

            except Exception as e:
                logger.error(f"Send error: {e}")

            await asyncio.sleep(2)

        if signals_this_cycle == 0:
            logger.info("No signals this cycle")


# ── Commands ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = """
🤖 *JunLuisify Signal Bot is LIVE!* 🇳🇬

I scan Solana meme coins 24/7 and only send you signals that pass:
✅ Volume & Liquidity filter
✅ Buy/Sell ratio check
✅ Token age filter
✅ RugCheck safety scan
✅ Honeypot.is check
✅ DeepSeek AI validation

*Every signal includes:*
- Contract address (copy & paste into GMGN)
- Entry price
- TP1 / TP2 / TP3 levels
- Stop Loss
- Moonbag strategy

*Commands:*
/status — Bot health & stats
/signals — Last 24h signals
/scan — Trigger manual scan
/help — How to trade signals
/about — How the bot works
"""
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uptime = datetime.now() - stats["start_time"]
    hours  = int(uptime.total_seconds() // 3600)
    mins   = int((uptime.total_seconds() % 3600) // 60)
    ai_status = "🟢 DeepSeek AI Active" if DEEPSEEK_API_KEY else "🟡 Momentum Mode (add DEEPSEEK_API_KEY)"

    msg = f"""
📊 *Bot Status*
━━━━━━━━━━━━━━
🟢 Status: Running
⏱ Uptime: {hours}h {mins}m
🔍 Scan: Every {SCAN_INTERVAL//60} mins
{ai_status}

📈 *Session Stats*
- ✅ Signals Sent: {stats['signals_sent']}
- ❌ Scams Filtered: {stats['scams_filtered']}
- 📉 Bad Ratio Filtered: {stats['ratio_filtered']}
- 🤖 AI Rejections: {stats['ai_rejections']}

⚙️ *Active Filters*
- Min Liquidity: ${MIN_LIQUIDITY:,}
- Min 1H Volume: ${MIN_VOLUME_1H:,}
- Price Change: {MIN_PRICE_CHANGE}% – {MAX_PRICE_CHANGE}%
- Min Buy/Sell Ratio: {MIN_BUY_SELL_RATIO}x
- Max Token Age: {MAX_TOKEN_AGE_HOURS}h
- RugCheck Min: {MIN_RUGCHECK_SCORE}/100
"""
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_signals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    recent = [(addr, t) for addr, t in sent_signals.items()
              if datetime.now() - t < timedelta(hours=24)]
    if not recent:
        await update.message.reply_text("📭 No signals in the last 24 hours.")
        return
    msg = f"📋 *{len(recent)} signal(s) in last 24h*\n\n"
    for addr, t in sorted(recent, key=lambda x: x[1], reverse=True)[:5]:
        msg += f"• `{addr[:12]}...` — {t.strftime('%H:%M UTC')}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Manual scan triggered! Results coming shortly...")
    await scan_and_signal(context.application.bot)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = """
📖 *How to Trade Signals*
━━━━━━━━━━━━━━━━━━━━

When a signal arrives:

1️⃣ Copy the *Contract Address*
2️⃣ Open *GMGN* → paste address → search
3️⃣ Set slippage to *10–15%*
4️⃣ Enable *Anti-MEV* in GMGN settings
5️⃣ Buy with *$1 max* per signal

💰 *Managing your trade:*
- At TP1 → sell 40% of position
- At TP2 → sell another 40%
- Keep 20% as moonbag

🔴 *Stop Loss rule:*
Price hits SL → EXIT everything immediately. No hoping.

🌙 *Moonbag rule:*
After TP2, only exit if:
- Price drops -30% from TP2, OR
- TP3 is reached

💡 *$1 Compounding Plan:*
Win 1 → reinvest profit into next signal
3–4 wins → increase to $2 per signal
Keep stacking 📈
"""
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = """
🧠 *How This Bot Works*
━━━━━━━━━━━━━━━━━━━━

*Layer 1 — Discovery*
DexScreener scans boosted + trending Solana tokens

*Layer 2 — Basic Filters*
Liquidity, Volume, Price Change, Buy/Sell Ratio, Token Age

*Layer 3 — Safety*
RugCheck score + Honeypot.is scan

*Layer 4 — AI Validation*
DeepSeek AI gives final confidence score

*Only if ALL 4 layers pass → Signal sent* ✅

Built by JunLuisify 🇳🇬🚀
"""
    await update.message.reply_text(msg, parse_mode="Markdown")


# ── Background Loop ───────────────────────────────────────────────────────────
async def background_scanner(bot):
    await asyncio.sleep(10)
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text="🟢 *JunLuisify Signal Bot ONLINE*\nScanning Solana every 5 mins...\n\nType /help to get started.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Startup msg error: {e}")

    while True:
        try:
            await scan_and_signal(bot)
        except Exception as e:
            logger.error(f"Scanner loop error: {e}")
        await asyncio.sleep(SCAN_INTERVAL)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("signals", cmd_signals))
    app.add_handler(CommandHandler("scan",    cmd_scan))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("about",   cmd_about))

    async def post_init(app):
        asyncio.create_task(background_scanner(app.bot))

    app.post_init = post_init

    logger.info("🚀 JunLuisify Signal Bot starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
