import os, logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, MenuButtonWebApp
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s — %(message)s")

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
APP_URL = os.environ.get("MINI_APP_URL", "")


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("⚡ Open AXIFLOW TRADE", web_app=WebAppInfo(url=APP_URL))],
        [InlineKeyboardButton("📊 About", callback_data="about"),
         InlineKeyboardButton("🔑 API Setup", callback_data="api")],
    ]
    await update.message.reply_text(
        "⚡ *AXIFLOW TRADE*\n\n"
        "Professional Smart Money Trading System:\n\n"
        "◆ Scans 20+ pairs continuously\n"
        "◆ OI · Funding · Liquidations · AMD/FVG\n"
        "◆ Dynamic RR — minimum 1:3\n"
        "◆ Targets moves of 2.5%+\n"
        "◆ Bybit · Binance · MEXC\n"
        "◆ Auto-trading + Telegram alerts\n\n"
        "Tap to open 👇",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )


async def cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "about":
        await q.edit_message_text(
            "📊 *How AXIFLOW works:*\n\n"
            "1️⃣ Scans 20+ futures pairs every 30s\n"
            "2️⃣ Analyzes: RSI, EMA, OI, Funding, CVD,\n"
            "   Liquidations, OrderBook, AMD/FVG\n"
            "3️⃣ Filters: only RR ≥ 1:3, move ≥ 2.5%\n"
            "4️⃣ Opens position automatically\n"
            "5️⃣ Sets TP and SL\n"
            "6️⃣ Sends Telegram alert\n\n"
            "💡 *RR = 1:3* means:\n"
            "Risk $1 to potentially earn $3+",
            parse_mode="Markdown"
        )

    elif q.data == "api":
        await q.edit_message_text(
            "🔑 *API Setup Guide:*\n\n"
            "*Bybit:*\n"
            "bybit.com → Profile → API Management\n"
            "Create New Key → System-generated\n"
            "✅ Read  ✅ Trade  ❌ Withdraw\n\n"
            "*Binance:*\n"
            "binance.com → Profile → API Management\n"
            "✅ Enable Futures  ❌ Enable Withdrawals\n\n"
            "*MEXC:*\n"
            "mexc.com → Profile → API Management\n"
            "✅ Trade  ❌ Withdraw\n\n"
            "⚠️ Never enable Withdraw permissions!",
            parse_mode="Markdown"
        )


async def post_init(application):
    if APP_URL:
        try:
            await application.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="⚡ AXIFLOW",
                    web_app=WebAppInfo(url=APP_URL)
                )
            )
        except Exception as e:
            print(f"Menu button: {e}")


def main():
    if not TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set")
        return
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(cb))
    print("✅ AXIFLOW Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
