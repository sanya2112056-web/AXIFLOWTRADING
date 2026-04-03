"""AXIFLOW TRADE - Telegram Bot - Pure ASCII"""
import os, logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, MenuButtonWebApp
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
APP_URL = os.environ.get("MINI_APP_URL", "")


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("Open AXIFLOW TRADE", web_app=WebAppInfo(url=APP_URL))],
        [InlineKeyboardButton("How it works", callback_data="about"),
         InlineKeyboardButton("API setup", callback_data="api")],
    ]
    await update.message.reply_text(
        "AXIFLOW TRADE\n\n"
        "Pure Smart Money trading system:\n\n"
        "- Scans 20 pairs every 30 seconds\n"
        "- OI / Funding / Liquidations / CVD / FVG / OB\n"
        "- Confidence 70%+ required to fire signal\n"
        "- Max 1 signal per symbol per 15 min\n"
        "- Bybit / Binance / MEXC futures\n"
        "- Auto-trading + alerts in this chat\n\n"
        "Tap to open the app:",
        reply_markup=InlineKeyboardMarkup(kb)
    )


async def cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "about":
        await q.edit_message_text(
            "How AXIFLOW works:\n\n"
            "1. Scans 20+ futures pairs every 30s\n"
            "2. 4-pillar confidence scoring (25pts each):\n"
            "   - Derivatives: OI, Funding, Liquidations, L/S ratio\n"
            "   - Order Flow: CVD, absorption, OB imbalance\n"
            "   - Liquidity: sweeps, FVG, Order Blocks, clusters\n"
            "   - Structure: HH/HL, market phase, 24H position\n"
            "3. Signal only if confidence >= 70%\n"
            "4. Max 1 signal per symbol per 15 minutes\n"
            "5. Opens position automatically\n"
            "6. Sets TP1 / TP2 / TP3 and SL\n"
            "7. Sends alert in this chat\n\n"
            "Rule: Better no signal than a weak one."
        )
    elif q.data == "api":
        await q.edit_message_text(
            "API Key Setup:\n\n"
            "Bybit:\n"
            "bybit.com > Profile > API Management\n"
            "Create New Key\n"
            "Enable: Read + Trade Futures\n"
            "NEVER enable Withdraw!\n\n"
            "Binance:\n"
            "binance.com > Profile > API Management\n"
            "Enable Futures, disable Withdrawals\n\n"
            "MEXC:\n"
            "mexc.com > Profile > API Management\n"
            "Enable Trade, disable Withdraw\n\n"
            "Then paste your keys in the API tab of the app."
        )


async def post_init(application):
    if APP_URL:
        try:
            await application.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="AXIFLOW",
                    web_app=WebAppInfo(url=APP_URL)
                )
            )
        except Exception as e:
            print(f"Menu button error: {e}")


def main():
    if not TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set")
        return
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(cb))
    print("AXIFLOW Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
