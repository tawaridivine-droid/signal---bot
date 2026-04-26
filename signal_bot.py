import os
import requests
import json
import logging
import asyncio
from datetime import datetime, timedelta
from telegram import Bot

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

MIN_VOLUME_1H       = 5000
MIN_LIQUIDITY       = 5000
MIN_PRICE_CHANGE    = 1
MAX_PRICE_CHANGE    = 200
MIN_RUGCHECK_SCORE  = 20
MOONBAG_PERCENT     = 20
MIN_BUY_SELL_RATIO  = 0.8
MAX_TOKEN_AGE_HOURS = 168
MIN_AI_CONFIDENCE   = 40

stats = {
    "signals_sent": 0,
    "scams_filtered": 0,
    "ai_rejections": 0,
    "start_time": datetime.now()
}


def get_trending_solana_tokens():
    candidates = []
    try:
        for query in ["new", "pump", "sol", "meme"]:
            try:
                url = f"https://api.dexscreener.com/latest/dex/search?q={query}"
                resp = requests.get(url, timeout=15)
                if resp.status_code == 200:
                    for pair in resp.json().get("pairs", []):
                        if pair.get("chainId") == "solana":
                            addr = pair.get("baseToken", {}).get("address")
                            if addr:
                                candidates.append(addr)
            except:
                continue

        for url in [
            "https://api.dexscreener.com/token-boosts/latest/v1",
            "https://api.dexscreener.com/token-boosts/top/v1"
        ]:
            try:
                resp = requests.get(url, timeout=15)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list):
                        for item in data:
                            if item.get("chainId") == "solana":
                                addr = item.get("tokenAddress")
                                if addr:
                                    candidates.append(addr)
            except:
                continue

    except Exception as e:
        logger.error(f"DexScreener error: {e}")
    return list(set(filter(None, candidates)))[:60]


def get_token_data(token_address):
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            pairs = resp.json().get("pairs", [])
            sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
            if not sol_pairs:
                return None
            return max(sol_pairs, key=lambda x: float(x.get("liquidity", {}).get("usd", 0) or 0))
    except Exception as e:
        logger.error(f"Token data error: {e}")
    return None


def check_rugcheck(token_address):
    try:
        url = f"https://api.rugcheck.xyz/v1/tokens/{token_address}/report/summary"
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            score = data.get("score", 0)
            risks = data.get("risks", [])
            critical = [r for r in risks if r.get("level") == "danger"]
            return {"score": score, "safe": score >= MIN_RUGCHECK_SCORE and len(critical) == 0}
    except:
        pass
    return {"score": 0, "safe": False}


def check_honeypot(token_address):
    try:
        url = f"https://api.honeypot.is/v2/IsHoneypot?address={token_address}&chainID=solana"
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            return not resp.json().get("honeypotResult", {}).get("isHoneypot", True)
    except:
        pass
    return True


def check_buy_sell_ratio(token_data):
    try:
        txns  = token_data.get("txns", {}).get("h1", {})
        buys  = int(txns.get("buys", 0))
        sells = int(txns.get("sells", 1))
        ratio = buys / sells if sells > 0 else float(buys)
        return ratio >= MIN_BUY_SELL_RATIO, round(ratio, 2)
    except:
        return False, 0


def check_token_age(token_data):
    try:
        created_at = token_data.get("pairCreatedAt")
        if not created_at:
            return True, "Unknown"
        hours = (datetime.now() - datetime.fromtimestamp(created_at / 1000)).total_seconds() / 3600
        if hours > MAX_TOKEN_AGE_HOURS:
            return False, f"{int(hours)}h old"
        return True, f"{int(hours)}h old"
    except:
        return True, "Unknown"


def validate_with_deepseek(token_data, rugcheck_data, buy_sell_ratio):
    if not DEEPSEEK_API_KEY:
        price_change = float(token_data.get("priceChange", {}).get("h1", 0) or 0)
        volume       = float(token_data.get("volume", {}).get("h1", 0) or 0)
        liquidity    = float(token_data.get("liquidity", {}).get("usd", 0) or 0)
        score = 0
        if price_change > 5:   score += 25
        if price_change > 15:  score += 15
        if volume > 30000:     score += 25
        if liquidity > 15000:  score += 20
        if buy_sell_ratio > 1.2: score += 15
        return {
            "valid": score >= 50,
            "confidence": score,
            "reason": f"Momentum score {score}/100",
            "risk_level": "medium",
            "entry_advice": "buy now",
            "mode": "momentum"
        }

    try:
        symbol           = token_data.get("baseToken", {}).get("symbol", "?")
        name             = token_data.get("baseToken", {}).get("name", "Unknown")
        price_usd        = token_data.get("priceUsd", "0")
        price_change_5m  = token_data.get("priceChange", {}).get("m5", 0)
        price_change_1h  = token_data.get("priceChange", {}).get("h1", 0)
        price_change_24h = token_data.get("priceChange", {}).get("h24", 0)
        volume_1h        = token_data.get("volume", {}).get("h1", 0)
        liquidity        = token_data.get("liquidity", {}).get("usd", 0)
        market_cap       = token_data.get("marketCap", 0)
        txns             = token_data.get("txns", {}).get("h1", {})
        buys             = txns.get("buys", 0)
        sells            = txns.get("sells", 0)
        rugcheck_score   = rugcheck_data.get("score", 0)

        prompt = f"""You are an expert Solana meme coin early entry signal validator.

Token: {symbol} ({name})
Price: ${price_usd}
Market Cap: ${market_cap:,.0f}
5m Change: {price_change_5m}%
1h Change: {price_change_1h}%
24h Change: {price_change_24h}%
1h Volume: ${volume_1h:,.0f}
Liquidity: ${liquidity:,.0f}
1h Buys/Sells: {buys}/{sells} (ratio: {buy_sell_ratio})
RugCheck Score: {rugcheck_score}/100

Respond ONLY in this exact JSON with no extra text:
{{
  "valid": true or false,
  "confidence": 0-100,
  "reason": "one sentence explanation",
  "risk_level": "low or medium or high",
  "entry_advice": "buy now or wait for dip"
}}"""

        resp = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            json={"model": "deepseek-chat", "max_tokens": 250, "messages": [{"role": "user", "content": prompt}]},
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
            timeout=20
        )
        if resp.status_code == 200:
            content = resp.json()["choices"][0]["message"]["content"]
            content = content.replace("```json", "").replace("```", "").strip()
            result  = json.loads(content)
            result["mode"] = "deepseek"
            return result
    except Exception as e:
        logger.error(f"DeepSeek error: {e}")

    return {"valid": False, "confidence": 0, "reason": "AI check failed", "risk_level": "high", "entry_advice": "skip", "mode": "error"}


def calculate_levels(price, price_change_1h):
    price    = float(price)
    momentum = float(price_change_1h or 0)
    if momentum <= 15:
        tp1_pct, tp2_pct, tp3_pct, sl_pct = 0.50, 1.00, 2.50, 0.15
    elif momentum <= 25:
        tp1_pct, tp2_pct, tp3_pct, sl_pct = 0.40, 0.80, 1.80, 0.15
    else:
        tp1_pct, tp2_pct, tp3_pct, sl_pct = 0.30, 0.60, 1.20, 0.12
    return {
        "entry":   price,
        "tp1":     round(price * (1 + tp1_pct), 10),
        "tp2":     round(price * (1 + tp2_pct), 10),
        "tp3":     round(price * (1 + tp3_pct), 10),
        "sl":      round(price * (1 - sl_pct), 10),
        "tp1_pct": int(tp1_pct * 100),
        "tp2_pct": int(tp2_pct * 100),
        "tp3_pct": int(tp3_pct * 100),
        "sl_pct":  int(sl_pct * 100),
    }


def format_signal(token_data, levels, ai_result, rugcheck_data, buy_sell_ratio, token_age):
    symbol           = token_data.get("baseToken", {}).get("symbol", "?")
    name             = token_data.get("baseToken", {}).get("name", "Unknown")
    address          = token_data.get("baseToken", {}).get("address", "")
    price_change_5m  = token_data.get("priceChange", {}).get("m5", 0)
    price_change_1h  = token_data.get("priceChange", {}).get("h1", 0)
    price_change_24h = token_data.get("priceChange", {}).get("h24", 0)
    volume_1h        = float(token_data.get("volume", {}).get("h1", 0) or 0)
    vol_5m           = float(token_data.get("volume", {}).get("m5", 0) or 0)
    liquidity        = float(token_data.get("liquidity", {}).get("usd", 0) or 0)
    market_cap       = float(token_data.get("marketCap", 0) or 0)
    confidence       = ai_result.get("confidence", 0)
    reason           = ai_result.get("reason", "")
    risk_level       = ai_result.get("risk_level", "medium")
    entry_advice     = ai_result.get("entry_advice", "buy now")
    rugcheck_score   = rugcheck_data.get("score", 0)
    mode             = ai_result.get("mode", "momentum")
    txns             = token_data.get("txns", {}).get("h1", {})
    buys             = txns.get("buys", 0)
    sells            = txns.get("sells", 0)

    conf_emoji = "🟢" if confidence >= 80 else "🟡" if confidence >= 60 else "🔴"
    conf_label = "HIGH" if confidence >= 80 else "MEDIUM" if confidence >= 60 else "LOW"
    risk_emoji = {"low": "🟢", "medium": "🟡", "high": "🔴"}.get(risk_level, "🟡")
    ai_badge   = "🤖 DeepSeek AI" if mode == "deepseek" else "📊 Momentum Score"

    return f"""
🚀 *SIGNAL ALERT — {symbol}* 🚀
━━━━━━━━━━━━━━━━━━━━━━━━
🪙 *{symbol}* | {name}
⛓ Solana | 🕐 Age: {token_age}

{conf_emoji} *AI Confidence: {confidence}% ({conf_label})*
{risk_emoji} Risk: {risk_level.upper()} | {ai_badge}
🛡 RugCheck: {rugcheck_score}/100

📈 *Price Action*
- 5M Change: `{price_change_5m:+.1f}%`
- 1H Change: `{price_change_1h:+.1f}%`
- 24H Change: `{price_change_24h:+.1f}%`
- 5M Volume: `${vol_5m:,.0f}`
- 1H Volume: `${volume_1h:,.0f}`
- Liquidity: `${liquidity:,.0f}`
- Market Cap: `${market_cap:,.0f}`
- Buys/Sells: `{buys}/{sells}` ({buy_sell_ratio}x)

📌 *AI says: {entry_advice.upper()}*

💰 *Trade Levels (for $1 = 0.007 SOL)*
- 🎯 Entry: `${levels['entry']}`
- 🟢 TP1 (+{levels['tp1_pct']}%): `${levels['tp1']}` → Sell 40%
- 🟢 TP2 (+{levels['tp2_pct']}%): `${levels['tp2']}` → Sell 40%
- 🌙 TP3 (+{levels['tp3_pct']}%): `${levels['tp3']}` → Sell 80%
- 🔴 SL (-{levels['sl_pct']}%): `${levels['sl']}`

🎒 *Keep {MOONBAG_PERCENT}% moonbag after TP2*

🧠 *AI Analysis*
_{reason}_

📋 *Contract — Paste into GMGN:*
`{address}`

🔗 *Links*
- [🟢 Trade on GMGN](https://gmgn.ai/sol/token/{address})
- [📊 DexScreener](https://dexscreener.com/solana/{address})
- [🛡 RugCheck](https://rugcheck.xyz/tokens/{address})
- [🐦 Birdeye](https://birdeye.so/token/{address}?chain=solana)

⚠️ _DYOR — Max $1 per signal. Not financial advice._
⏰ {datetime.now().strftime('%H:%M:%S UTC')}
━━━━━━━━━━━━━━━━━━━━━━━━
""".strip()


async def scan_and_signal(bot):
    logger.info("🔍 Scanning for early gems...")
    addresses    = get_trending_solana_tokens()
    logger.info(f"Candidates: {len(addresses)}")
    signals_sent = 0

    for addr in addresses:
        if not addr:
            continue

        token_data = get_token_data(addr)
        if not token_data:
            continue

        try:
            liquidity    = float(token_data.get("liquidity", {}).get("usd", 0) or 0)
            volume_1h    = float(token_data.get("volume", {}).get("h1", 0) or 0)
            price_change = float(token_data.get("priceChange", {}).get("h1", 0) or 0)
            price_usd    = float(token_data.get("priceUsd", 0) or 0)
        except:
            continue

        if liquidity < MIN_LIQUIDITY:   continue
        if volume_1h < MIN_VOLUME_1H:   continue
        if price_usd <= 0:              continue
        if not (MIN_PRICE_CHANGE <= price_change <= MAX_PRICE_CHANGE): continue

        age_ok, token_age = check_token_age(token_data)
        if not age_ok:
            logger.info(f"⏳ Too old: {addr} ({token_age})")
            continue

        ratio_ok, buy_sell_ratio = check_buy_sell_ratio(token_data)
        if not ratio_ok:
            logger.info(f"📉 Bad ratio: {addr} ({buy_sell_ratio}x)")
            continue

        rugcheck = check_rugcheck(addr)
        if not rugcheck["safe"]:
            stats["scams_filtered"] += 1
            logger.info(f"❌ RugCheck fail: {addr}")
            continue

        honeypot_safe = check_honeypot(addr)
        if not honeypot_safe:
            stats["scams_filtered"] += 1
            logger.info(f"❌ Honeypot: {addr}")
            continue

        ai_result = validate_with_deepseek(token_data, rugcheck, buy_sell_ratio)
        if not ai_result.get("valid") or ai_result.get("confidence", 0) < MIN_AI_CONFIDENCE:
            stats["ai_rejections"] += 1
            logger.info(f"🤖 AI rejected: {addr} ({ai_result.get('confidence', 0)}%)")
            continue

        levels  = calculate_levels(price_usd, price_change)
        message = format_signal(token_data, levels, ai_result, rugcheck, buy_sell_ratio, token_age)

        try:
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=message,
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
            stats["signals_sent"] += 1
            signals_sent += 1
            symbol = token_data.get("baseToken", {}).get("symbol", addr)
            logger.info(f"✅ Signal sent: {symbol} | Confidence: {ai_result.get('confidence')}%")

            if signals_sent >= 3:
                break

        except Exception as e:
            logger.error(f"Send error: {e}")

    if signals_sent == 0:
        logger.info("No qualifying gems this cycle")
    else:
        logger.info(f"✅ {signals_sent} signal(s) sent")


async def main():
    bot = Bot(token=TELEGRAM_TOKEN)
    await bot.initialize()
    await scan_and_signal(bot)
    await bot.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
