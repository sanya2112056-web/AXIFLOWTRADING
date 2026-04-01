"""AXIFLOW — Telegram Bot"""
import os, logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, MenuButtonWebApp
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s — %(message)s")
log = logging.getLogger("axiflow.bot")

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
APP_URL = os.environ.get("MINI_APP_URL", "")


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("⚡ Відкрити AXIFLOW", web_app=WebAppInfo(url=APP_URL))],
        [InlineKeyboardButton("📊 Що таке AXIFLOW?", callback_data="about"),
         InlineKeyboardButton("❓ Як налаштувати?", callback_data="help")],
    ]
    await update.message.reply_text(
        "⚡ *AXIFLOW — Smart Money Trading*\n\n"
        "Система аналізу крипто ринку:\n\n"
        "◆ Аналізує 15+ пар безперервно\n"
        "◆ OI · Funding · Ліквідації · AMD/FVG\n"
        "◆ RR = 1:4 на кожну угоду\n"
        "◆ Bybit + Binance\n"
        "◆ Авто-торгівля + сповіщення сюди\n\n"
        "Натисни щоб відкрити 👇",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb)
    )


async def cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "about":
        await q.edit_message_text(
            "📊 *Що таке AXIFLOW?*\n\n"
            "AXIFLOW — це AI торговий агент який:\n\n"
            "1️⃣ Сканує ринок кожні 30 секунд\n"
            "2️⃣ Аналізує Open Interest (OI)\n"
            "3️⃣ Перевіряє Funding Rate\n"
            "4️⃣ Відстежує ліквідації\n"
            "5️⃣ Виявляє AMD/FVG патерни\n"
            "6️⃣ При сигналі — відкриває угоду\n"
            "7️⃣ Ставить TP та SL автоматично\n"
            "8️⃣ Надсилає сповіщення сюди\n\n"
            "💡 RR = 1:4 означає:\n"
            "ризикуєш $1 щоб заробити $4",
            parse_mode="Markdown"
        )

    elif q.data == "help":
        await q.edit_message_text(
            "❓ *Як налаштувати:*\n\n"
            "1️⃣ Відкрий Mini App\n"
            "2️⃣ Натисни *API* внизу\n"
            "3️⃣ Для демо — просто натисни *Підключити*\n"
            "4️⃣ Для реальної торгівлі — вкажи ключі\n"
            "5️⃣ Натисни *Агент* → Запустити\n\n"
            "🔑 *Bybit API ключі:*\n"
            "bybit.com → Профіль → API Management\n"
            "Create New Key\n"
            "✅ Read  ✅ Trade  ❌ Withdraw\n\n"
            "🔑 *Binance API ключі:*\n"
            "binance.com → Профіль → API Management\n"
            "✅ Enable Futures  ❌ Enable Withdrawals\n\n"
            "⚠️ Починай завжди з Testnet!",
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
            log.info(f"✅ Menu button встановлено: {APP_URL}")
        except Exception as e:
            log.error(f"Menu button: {e}")


def main():
    if not TOKEN:
        log.error("TELEGRAM_BOT_TOKEN не вказано!")
        return
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(cb))
    log.info("✅ AXIFLOW Bot запущено")
    app.run_polling()


if __name__ == "__main__":
    main()
