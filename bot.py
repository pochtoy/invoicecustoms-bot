import os
import json
import base64
import logging
from io import BytesIO
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
import anthropic
import httpx

# ─── Config ───
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
# Optional: restrict bot to specific user IDs (comma-separated)
ALLOWED_USERS = os.environ.get("ALLOWED_USERS", "")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ─── Session storage ───
# user_id -> { "images": [base64_list], "shipments": [...], "phase": "..." }
sessions = {}


def get_session(user_id):
    if user_id not in sessions:
        sessions[user_id] = {"images": [], "phase": "collecting", "shipments": []}
    return sessions[user_id]


def clear_session(user_id):
    sessions[user_id] = {"images": [], "phase": "collecting", "shipments": []}


def is_allowed(user_id):
    if not ALLOWED_USERS:
        return True
    allowed = [int(x.strip()) for x in ALLOWED_USERS.split(",") if x.strip()]
    return user_id in allowed


# ─── Ticket generation ───
def generate_ticket(data, approved):
    order = data.get("orderNumber", "______")
    shipper = data.get("shipper", "N/A")
    country = data.get("shipperCountry", "")
    goods = data.get("goodsDescription", "N/A")
    declared = data.get("declaredValue", "N/A")
    duty = data.get("dutyAmount", "N/A")
    fee = data.get("entryPrepFee", "N/A")
    total = data.get("totalCharges", "N/A")

    header = (
        f"Здравствуйте!\n\n"
        f"По вашему заказу № {order} "
        f"(посылка от отправителя {shipper}, {country}) "
        f"была начислена таможенная пошлина."
    )
    details = (
        f"\n\nДетали:\n"
        f"- Описание товара: {goods}\n"
        f"- Объявленная стоимость: ${declared}\n"
        f"- Пошлина (Duty): ${duty}\n"
        f"- Сбор за оформление (Entry Prep Fee): ${fee}\n"
        f"- Итого {'оплачено' if approved else 'к оплате'}: ${total} USD"
    )
    if approved:
        footer = "\n\nСумма была списана с вашего баланса.\n\nЕсли у вас есть вопросы — напишите нам."
    else:
        footer = (
            "\n\nСписание средств не производилось, так как оплата пошлины не была согласована."
            "\nПожалуйста, подтвердите оплату или свяжитесь с нами для уточнения."
            "\n\nЕсли у вас есть вопросы — напишите нам."
        )
    return header + details + footer


# ─── AI Processing ───
async def process_invoices(images_b64):
    content = []
    for i, img_b64 in enumerate(images_b64):
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64},
        })
        content.append({"type": "text", "text": f"[Фото {i+1} из {len(images_b64)}]"})

    content.append({
        "type": "text",
        "text": f"""Ты — система извлечения данных из инвойсов на таможенную пошлину (UPS, FedEx, DHL и др.).

Тебе предоставлены {len(images_b64)} фото. Это могут быть страницы РАЗНЫХ инвойсов по РАЗНЫМ посылкам, или несколько страниц одного инвойса.

ЗАДАЧА:
1. Определи сколько УНИКАЛЬНЫХ посылок/отправлений здесь есть (по трек-номерам, Shipment ID, или номерам инвойсов)
2. Сгруппируй страницы по посылкам
3. Для каждой посылки извлеки полные данные

Верни ТОЛЬКО JSON-массив (без markdown, без backticks, без пояснений):

[
  {{
    "shipmentIndex": 1,
    "pages": "какие фото относятся к этой посылке",
    "trackingNumber": "трек-номер",
    "shipmentId": "ID отправления если есть",
    "shipper": "название отправителя",
    "shipperCountry": "страна отправителя",
    "recipient": "ФИО получателя",
    "recipientAddress": "адрес получателя",
    "goodsDescription": "описание товара",
    "declaredValue": "объявленная стоимость (только число)",
    "dutyAmount": "сумма пошлины (только число)",
    "entryPrepFee": "сбор за оформление (только число)",
    "totalCharges": "итого к оплате ФИНАЛЬНАЯ сумма (только число)",
    "invoiceNumber": "номер инвойса",
    "invoiceDate": "дата инвойса",
    "carrier": "перевозчик (UPS/FedEx/DHL/другой)",
    "paymentUrl": "URL для оплаты если указан, иначе N/A",
    "notes": "замечания если есть"
  }}
]

Правила:
- Если несколько страниц имеют одинаковый трек-номер или shipment ID — это ОДНА посылка
- Итоговую сумму бери оттуда, где указан финальный Total Charges
- ОБЯЗАТЕЛЬНО найди ссылку/URL для оплаты
- Числовые поля — только цифры с точкой, без знака доллара
- Если поле не найдено — "N/A"
""",
    })

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        messages=[{"role": "user", "content": content}],
    )

    text = ""
    for block in message.content:
        if hasattr(block, "text"):
            text += block.text

    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    parsed = json.loads(text)
    return parsed if isinstance(parsed, list) else [parsed]


# ─── Handlers ───
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ У вас нет доступа к этому боту.")
        return

    clear_session(update.effective_user.id)
    await update.message.reply_text(
        "📦 *Invoice Processor Bot*\n\n"
        "Отправьте мне фото инвойсов \\(можно несколько\\)\\.\n"
        "Когда все фото загружены — нажмите /done\n\n"
        "Команды:\n"
        "/done — распознать загруженные фото\n"
        "/clear — очистить и начать заново\n"
        "/help — справка",
        parse_mode="MarkdownV2",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Как пользоваться:*\n\n"
        "1\\. Отправьте фото инвойсов \\(все страницы всех посылок\\)\n"
        "2\\. Нажмите /done\n"
        "3\\. Бот сгруппирует по посылкам и покажет:\n"
        "   💳 Данные для оплаты\n"
        "   📝 Готовый тикет\n\n"
        "Для каждой посылки можно указать номер заказа "
        "и выбрать согласована ли оплата\\.",
        parse_mode="MarkdownV2",
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_session(update.effective_user.id)
    await update.message.reply_text("🗑 Очищено. Отправляйте новые фото инвойсов.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    session = get_session(update.effective_user.id)

    if session["phase"] != "collecting":
        session["phase"] = "collecting"
        session["images"] = []
        session["shipments"] = []

    photo = update.message.photo[-1]  # highest resolution
    file = await context.bot.get_file(photo.file_id)

    # Download photo
    bio = BytesIO()
    await file.download_to_memory(bio)
    bio.seek(0)
    img_b64 = base64.b64encode(bio.read()).decode("utf-8")

    session["images"].append(img_b64)
    count = len(session["images"])

    await update.message.reply_text(
        f"✅ Фото {count} загружено.\n"
        f"Отправьте ещё фото или нажмите /done для распознавания."
    )


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    session = get_session(update.effective_user.id)

    if not session["images"]:
        await update.message.reply_text("❌ Нет загруженных фото. Отправьте фото инвойсов.")
        return

    count = len(session["images"])
    msg = await update.message.reply_text(f"⏳ Анализирую {count} фото... Подождите.")

    try:
        shipments = await process_invoices(session["images"])
        session["shipments"] = []

        for s in shipments:
            s["orderNumber"] = ""
            s["paymentApproved"] = True
            session["shipments"].append(s)

        session["phase"] = "review"

        # Send results
        for i, s in enumerate(session["shipments"]):
            await send_shipment_card(update, context, session, i)

        if len(session["shipments"]) > 1:
            await update.message.reply_text(
                f"✅ Найдено посылок: {len(session['shipments'])}\n\n"
                "Для каждой посылки укажите номер заказа командой:\n"
                "`/order 1 ABC123`\n"
                "где 1 — номер посылки, ABC123 — номер заказа\n\n"
                "Для генерации тикетов: /tickets",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                "✅ Найдена 1 посылка\n\n"
                "Укажите номер заказа: `/order 1 ABC123`\n"
                "Сгенерировать тикет: /tickets",
                parse_mode="Markdown",
            )

    except Exception as e:
        logger.error(f"Processing error: {e}")
        await context.bot.edit_message_text(
            "❌ Ошибка при распознавании. Проверьте качество фото и попробуйте снова.",
            chat_id=msg.chat_id,
            message_id=msg.message_id,
        )


async def send_shipment_card(update, context, session, idx):
    s = session["shipments"][idx]
    num = idx + 1

    # Payment data
    payment_text = (
        f"📦 *Посылка {num}* — {s.get('shipper', 'N/A')}\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"💳 *ДАННЫЕ ДЛЯ ОПЛАТЫ:*\n"
        f"├ Инвойс: `{s.get('invoiceNumber', 'N/A')}`\n"
        f"├ Сумма: *${s.get('totalCharges', 'N/A')} USD*\n"
        f"├ Трек: `{s.get('trackingNumber', 'N/A')}`\n"
    )

    if s.get("shipmentId") and s["shipmentId"] != "N/A":
        payment_text += f"├ Shipment ID: `{s['shipmentId']}`\n"

    payment_text += (
        f"└ Перевозчик: {s.get('carrier', 'N/A')}\n\n"
        f"📋 *ДЕТАЛИ:*\n"
        f"├ Отправитель: {s.get('shipper', 'N/A')}, {s.get('shipperCountry', '')}\n"
        f"├ Товар: {s.get('goodsDescription', 'N/A')}\n"
        f"├ Стоимость: ${s.get('declaredValue', 'N/A')}\n"
        f"├ Пошлина: ${s.get('dutyAmount', 'N/A')}\n"
        f"├ Сбор: ${s.get('entryPrepFee', 'N/A')}\n"
        f"└ *Итого: ${s.get('totalCharges', 'N/A')} USD*\n"
    )

    if s.get("notes") and s["notes"] != "N/A" and s["notes"]:
        payment_text += f"\n⚠️ {s['notes']}\n"

    # Buttons
    buttons = []
    url = s.get("paymentUrl", "N/A")
    if url and url != "N/A":
        if not url.startswith("http"):
            url = "https://" + url
        buttons.append([InlineKeyboardButton("🌐 Перейти к оплате", url=url)])

    buttons.append([
        InlineKeyboardButton("✅ Оплата согласована", callback_data=f"approve_{idx}"),
        InlineKeyboardButton("❌ Не согласована", callback_data=f"reject_{idx}"),
    ])
    buttons.append([
        InlineKeyboardButton("📝 Сгенерировать тикет", callback_data=f"ticket_{idx}"),
    ])

    await update.message.reply_text(
        payment_text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    session = get_session(user_id)
    data = query.data

    if data.startswith("approve_"):
        idx = int(data.split("_")[1])
        if idx < len(session["shipments"]):
            session["shipments"][idx]["paymentApproved"] = True
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(f"✅ Посылка {idx+1}: оплата отмечена как согласованная")

    elif data.startswith("reject_"):
        idx = int(data.split("_")[1])
        if idx < len(session["shipments"]):
            session["shipments"][idx]["paymentApproved"] = False
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(f"❌ Посылка {idx+1}: оплата отмечена как НЕ согласованная")

    elif data.startswith("ticket_"):
        idx = int(data.split("_")[1])
        if idx < len(session["shipments"]):
            s = session["shipments"][idx]
            ticket = generate_ticket(s, s["paymentApproved"])
            await query.message.reply_text(
                f"📝 *Тикет — Посылка {idx+1}:*\n\n`{ticket}`",
                parse_mode="Markdown",
            )


async def cmd_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    session = get_session(update.effective_user.id)

    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Формат: `/order 1 ABC123`\n"
            "где 1 — номер посылки, ABC123 — номер заказа",
            parse_mode="Markdown",
        )
        return

    try:
        idx = int(context.args[0]) - 1
        order_num = " ".join(context.args[1:])

        if 0 <= idx < len(session["shipments"]):
            session["shipments"][idx]["orderNumber"] = order_num
            await update.message.reply_text(
                f"✅ Посылка {idx+1}: номер заказа установлен → `{order_num}`",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(f"❌ Посылка с номером {idx+1} не найдена.")
    except ValueError:
        await update.message.reply_text("❌ Неверный формат. Используйте: `/order 1 ABC123`", parse_mode="Markdown")


async def cmd_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    session = get_session(update.effective_user.id)

    if not session["shipments"]:
        await update.message.reply_text("❌ Нет распознанных посылок. Отправьте фото и нажмите /done")
        return

    for i, s in enumerate(session["shipments"]):
        ticket = generate_ticket(s, s["paymentApproved"])
        await update.message.reply_text(
            f"📝 *Тикет — Посылка {i+1} \\({s.get('shipper', 'N/A')}\\):*\n\n"
            f"`{ticket}`",
            parse_mode="MarkdownV2",
        )


# ─── Main ───
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("order", cmd_order))
    app.add_handler(CommandHandler("tickets", cmd_tickets))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("Bot started!")
    app.run_polling()


if __name__ == "__main__":
    main()
