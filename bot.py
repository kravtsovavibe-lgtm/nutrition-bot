import os
import json
import sqlite3
import logging
import base64
from datetime import datetime, date
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import google.generativeai as genai

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

USER_PROFILE = {
    "name": "Кравцова",
    "weight_start": 78.4,
    "fat_start": 39.6,
    "height": 162,
    "age": 35,
    "goal_weight": 68.4,
    "calories_target": 1420,
    "protein_target": 110,
    "water_target": 2.5,
    "no_eat": ["варёные овощи", "лук"],
}

SYSTEM_PROMPT = f"""Ты личный нутрициолог и фитнес-тренер для {USER_PROFILE['name']}.

Параметры:
- Рост: {USER_PROFILE['height']} см, возраст: {USER_PROFILE['age']} лет
- Стартовый вес: {USER_PROFILE['weight_start']} кг, жир: {USER_PROFILE['fat_start']}%
- Цель: похудеть на 10 кг за 2 месяца
- Дневная норма: {USER_PROFILE['calories_target']} ккал, белок {USER_PROFILE['protein_target']}г, вода {USER_PROFILE['water_target']}л
- Не ест: {', '.join(USER_PROFILE['no_eat'])}

Твои задачи:
1. Анализировать фото еды — определять калории и белок
2. Читать фото с весов Picooc — записывать все показатели
3. Анализировать состав продуктов — говорить подходит/нет
4. По содержимому холодильника — предлагать рецепты
5. Корректировать питание — говорить если мало белка или много калорий
6. Отслеживать динамику веса и жира

Стиль: дружелюбно, кратко, по делу. Отвечай на русском.
"""


def init_db():
    conn = sqlite3.connect("/app/nutrition.db")
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS weight_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, weight REAL, fat REAL,
        muscle REAL, water REAL, created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS food_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, description TEXT,
        calories INTEGER, protein REAL, created_at TEXT
    )""")
    conn.commit()
    conn.close()


def get_today_stats():
    try:
        conn = sqlite3.connect("/app/nutrition.db")
        c = conn.cursor()
        today = date.today().isoformat()
        c.execute("SELECT SUM(calories), SUM(protein) FROM food_log WHERE date=?", (today,))
        row = c.fetchone()
        calories = row[0] or 0
        protein = row[1] or 0.0
        c.execute("SELECT description FROM food_log WHERE date=? ORDER BY created_at", (today,))
        meals = [r[0] for r in c.fetchall()]
        conn.close()
        return {"calories": calories, "protein": protein, "meals": meals}
    except Exception:
        return {"calories": 0, "protein": 0, "meals": []}


def get_last_weight():
    try:
        conn = sqlite3.connect("/app/nutrition.db")
        c = conn.cursor()
        c.execute("SELECT weight, fat, muscle, water, date FROM weight_log ORDER BY created_at DESC LIMIT 1")
        row = c.fetchone()
        conn.close()
        if row:
            return {"weight": row[0], "fat": row[1], "muscle": row[2], "water": row[3], "date": row[4]}
    except Exception:
        pass
    return None


def save_weight(weight, fat, muscle, water):
    try:
        conn = sqlite3.connect("/app/nutrition.db")
        c = conn.cursor()
        today = date.today().isoformat()
        c.execute("INSERT INTO weight_log (date, weight, fat, muscle, water, created_at) VALUES (?,?,?,?,?,?)",
                  (today, weight, fat, muscle, water, datetime.now().isoformat()))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"save_weight error: {e}")


def save_food(description, calories, protein):
    try:
        conn = sqlite3.connect("/app/nutrition.db")
        c = conn.cursor()
        today = date.today().isoformat()
        c.execute("INSERT INTO food_log (date, description, calories, protein, created_at) VALUES (?,?,?,?,?)",
                  (today, description, calories, protein, datetime.now().isoformat()))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"save_food error: {e}")


def main_keyboard():
    keyboard = [
        [KeyboardButton("📊 Статистика"), KeyboardButton("📸 Фото весов")],
        [KeyboardButton("🍽 Записать еду"), KeyboardButton("🛒 Список покупок")],
        [KeyboardButton("📅 Отчёт за неделю"), KeyboardButton("💪 Тренировки")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я твой личный нутрициолог 🌿\n\n"
        "Что умею:\n"
        "📸 Читать фото с весов Picooc\n"
        "🍽 Анализировать фото еды\n"
        "🏪 Проверять состав продуктов\n"
        "🥗 Составлять рецепты из холодильника\n"
        "📊 Следить за динамикой\n\n"
        "Просто скинь фото или напиши что съела!",
        reply_markup=main_keyboard()
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    today_stats = get_today_stats()
    last_weight = get_last_weight()

    if text == "📊 Статистика":
        calories_left = USER_PROFILE["calories_target"] - today_stats["calories"]
        protein_left = USER_PROFILE["protein_target"] - today_stats["protein"]
        msg = f"📊 Сегодня {date.today().strftime('%d.%m')}\n\n"
        msg += f"🔥 Калории: {today_stats['calories']} / {USER_PROFILE['calories_target']} ккал\n"
        msg += f"Осталось: {max(0, calories_left)} ккал\n\n"
        msg += f"💪 Белок: {today_stats['protein']:.0f} / {USER_PROFILE['protein_target']}г\n"
        if protein_left > 0:
            msg += f"Нужно ещё: {protein_left:.0f}г\n"
        if today_stats["meals"]:
            msg += "\n🍽 Съедено:\n" + "\n".join(f"• {m}" for m in today_stats["meals"])
        if last_weight:
            diff = last_weight["weight"] - USER_PROFILE["weight_start"]
            sign = "−" if diff < 0 else "+"
            msg += f"\n\n⚖️ Последний вес: {last_weight['weight']} кг ({sign}{abs(diff):.1f} от старта)"
        await update.message.reply_text(msg)
        return

    if text == "📅 Отчёт за неделю":
        try:
            conn = sqlite3.connect("/app/nutrition.db")
            c = conn.cursor()
            c.execute("SELECT date, weight, fat FROM weight_log ORDER BY created_at DESC LIMIT 7")
            weights = c.fetchall()
            conn.close()
            msg = "📅 Динамика за неделю\n\n"
            if weights:
                msg += "⚖️ Вес:\n"
                for w in weights:
                    msg += f"• {w[0]}: {w[1]} кг, жир {w[2]}%\n"
            else:
                msg += "Замеров пока нет. Скинь фото с весов!\n"
            await update.message.reply_text(msg)
        except Exception:
            await update.message.reply_text("Не могу загрузить данные.")
        return

    context_info = f"""
Сегодня {date.today().strftime('%d.%m.%Y')}.
Съедено: {today_stats['calories']} ккал, белок {today_stats['protein']:.0f}г.
Осталось: {USER_PROFILE['calories_target'] - today_stats['calories']} ккал, белок {USER_PROFILE['protein_target'] - today_stats['protein']:.0f}г.
"""
    if last_weight:
        context_info += f"Последний вес: {last_weight['weight']} кг, жир {last_weight['fat']}%.\n"

    prompt = SYSTEM_PROMPT + context_info + f"\nСообщение: {text}"

    try:
        response = model.generate_content(prompt)
        reply = response.text

        food_words = ["съела", "поела", "завтрак", "обед", "ужин", "перекус", "выпила", "ем", "пью"]
        if any(word in text.lower() for word in food_words):
            try:
                parse_prompt = f"Из текста '{text}' определи калории и белок. Ответь ТОЛЬКО JSON без markdown: {{\"calories\": число, \"protein\": число, \"description\": \"краткое название\"}}"
                parse_response = model.generate_content(parse_prompt)
                parse_text = parse_response.text.strip().replace("```json", "").replace("```", "").strip()
                data = json.loads(parse_text)
                if data.get("calories", 0) > 0:
                    save_food(data.get("description", text), data.get("calories", 0), data.get("protein", 0))
            except Exception:
                pass

        await update.message.reply_text(reply)
    except Exception as e:
        await update.message.reply_text("Что-то пошло не так, попробуй ещё раз.")
        logger.error(f"Text error: {e}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = update.message.caption or ""
    photo = update.message.photo[-1]

    try:
        file = await context.bot.get_file(photo.file_id)
       import io
buf = io.BytesIO()
await file.download_to_memory(buf)
file_bytes = buf.getvalue()
        image_data = base64.b64encode(file_bytes).decode("utf-8")
    except Exception as e:
        await update.message.reply_text("Не могу загрузить фото, попробуй ещё раз.")
        logger.error(f"Photo download error: {e}")
        return

    today_stats = get_today_stats()
    context_info = f"Сегодня {date.today().strftime('%d.%m.%Y')}. Съедено: {today_stats['calories']} ккал. Осталось: {USER_PROFILE['calories_target'] - today_stats['calories']} ккал."

    scale_words = ["весы", "picooc", "пикок", "вес", "замер", "взвесилась"]
    is_scale = any(word in caption.lower() for word in scale_words)

    if is_scale or not caption:
        prompt = SYSTEM_PROMPT + context_info + "\nНа фото весы Picooc. Прочитай все показатели: вес кг, % жира, % мышц, % воды. Если это не весы — проанализируй как еду. Дай короткий комментарий."
    else:
        prompt = SYSTEM_PROMPT + context_info + f"\nФото с подписью: '{caption}'. Если еда — калории и белок, сколько осталось на день. Если состав продукта — подходит/нет. Если тело — опиши изменения. Если холодильник — предложи рецепт."

    try:
        image_part = {"mime_type": "image/jpeg", "data": image_data}
        response = model.generate_content([prompt, image_part])
        reply = response.text

        if is_scale or not caption:
            try:
                parse_prompt = f"Из текста '{reply}' извлеки числа. Ответь ТОЛЬКО JSON без markdown: {{\"weight\": число, \"fat\": число, \"muscle\": число, \"water\": число}}"
                parse_resp = model.generate_content(parse_prompt)
                parse_text = parse_resp.text.strip().replace("```json", "").replace("```", "").strip()
                data = json.loads(parse_text)
                if data.get("weight", 0) > 0:
                    save_weight(data.get("weight", 0), data.get("fat", 0), data.get("muscle", 0), data.get("water", 0))
            except Exception:
                pass
        else:
            try:
                parse_prompt = f"Из текста '{reply}' извлеки калории и белок. Ответь ТОЛЬКО JSON без markdown: {{\"calories\": число, \"protein\": число, \"description\": \"название блюда\"}}"
                parse_resp = model.generate_content(parse_prompt)
                parse_text = parse_resp.text.strip().replace("```json", "").replace("```", "").strip()
                data = json.loads(parse_text)
                if data.get("calories", 0) > 0:
                    save_food(data.get("description", "блюдо"), data.get("calories", 0), data.get("protein", 0))
            except Exception:
                pass

        await update.message.reply_text(reply)
    except Exception as e:
        await update.message.reply_text("Не могу обработать фото, попробуй ещё раз.")
        logger.error(f"Photo error: {e}")


async def post_init(application: Application):
    init_db()
    logger.info("DB initialized")


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Bot started!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
