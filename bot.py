import os
import io
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
    "calories_target": 1420,
    "protein_target": 110,
    "no_eat": ["варёные овощи", "лук"],
}

SYSTEM_PROMPT = """Ты личный нутрициолог для Кравцовой.
Параметры: рост 162см, возраст 35 лет, стартовый вес 78.4кг, жир 39.6%.
Цель: минус 10кг за 2 месяца. Норма: 1420 ккал, белок 110г, вода 2.5л.
Не ест: варёные овощи, лук.
Задачи: анализировать фото еды, читать весы Picooc, проверять составы продуктов, предлагать рецепты из холодильника, корректировать питание.
Стиль: дружелюбно, кратко, по делу. Отвечай на русском."""


def init_db():
    db_path = os.environ.get("DB_PATH", "/tmp/nutrition.db")
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS weight_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, weight REAL, fat REAL, muscle REAL, water REAL, created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS food_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, description TEXT, calories INTEGER, protein REAL, created_at TEXT
    )""")
    conn.commit()
    conn.close()


def get_db():
    db_path = os.environ.get("DB_PATH", "/tmp/nutrition.db")
    return sqlite3.connect(db_path)


def get_today_stats():
    try:
        conn = get_db()
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
        conn = get_db()
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
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT INTO weight_log (date, weight, fat, muscle, water, created_at) VALUES (?,?,?,?,?,?)",
                  (date.today().isoformat(), weight, fat, muscle, water, datetime.now().isoformat()))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"save_weight: {e}")


def save_food(description, calories, protein):
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT INTO food_log (date, description, calories, protein, created_at) VALUES (?,?,?,?,?)",
                  (date.today().isoformat(), description, calories, protein, datetime.now().isoformat()))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"save_food: {e}")


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
        "🥗 Рецепты из холодильника\n"
        "📊 Динамика веса и жира\n\n"
        "Скинь фото или напиши что съела!",
        reply_markup=main_keyboard()
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    today = get_today_stats()
    last_w = get_last_weight()

    if text == "📊 Статистика":
        cal_left = USER_PROFILE["calories_target"] - today["calories"]
        prot_left = USER_PROFILE["protein_target"] - today["protein"]
        msg = f"📊 {date.today().strftime('%d.%m')}\n\n"
        msg += f"🔥 {today['calories']} / {USER_PROFILE['calories_target']} ккал (осталось {max(0,cal_left)})\n"
        msg += f"💪 {today['protein']:.0f} / {USER_PROFILE['protein_target']}г белка"
        if prot_left > 0:
            msg += f" (ещё {prot_left:.0f}г)"
        if today["meals"]:
            msg += "\n\n🍽 " + "\n• ".join([""] + today["meals"])
        if last_w:
            diff = last_w["weight"] - USER_PROFILE["weight_start"]
            sign = "−" if diff < 0 else "+"
            msg += f"\n\n⚖️ Вес: {last_w['weight']} кг ({sign}{abs(diff):.1f} от старта)\n"
            msg += f"Жир: {last_w['fat']}% | Мышцы: {last_w['muscle']}%"
        await update.message.reply_text(msg)
        return

    if text == "📅 Отчёт за неделю":
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute("SELECT date, weight, fat FROM weight_log ORDER BY created_at DESC LIMIT 7")
            weights = c.fetchall()
            conn.close()
            msg = "📅 Динамика за неделю\n\n"
            if weights:
                for w in weights:
                    msg += f"• {w[0]}: {w[1]} кг, жир {w[2]}%\n"
            else:
                msg += "Замеров пока нет. Скинь фото с весов!"
            await update.message.reply_text(msg)
        except Exception:
            await update.message.reply_text("Не могу загрузить данные.")
        return

    if text == "💪 Тренировки":
        msg = "💪 План на сегодня:\n\n• Зарядка — 10 мин\n• Тазовое дно (Сабина Филина)\n• Тренировка с резинкой — 25 мин\n• Диафрагмальное дыхание — 5 мин"
        await update.message.reply_text(msg)
        return

    ctx = f"\nСегодня {date.today().strftime('%d.%m')}. Съедено: {today['calories']} ккал, белок {today['protein']:.0f}г. Осталось: {USER_PROFILE['calories_target']-today['calories']} ккал."
    if last_w:
        ctx += f" Последний вес: {last_w['weight']} кг."

    try:
        response = model.generate_content(SYSTEM_PROMPT + ctx + f"\nСообщение: {text}")
        reply = response.text

        food_words = ["съела", "поела", "завтрак", "обед", "ужин", "перекус", "выпила", "ем", "пью", "скушала"]
        if any(w in text.lower() for w in food_words):
            try:
                pr = model.generate_content(f"Из текста '{text}' определи калории и белок. ТОЛЬКО JSON: {{\"calories\": число, \"protein\": число, \"description\": \"название\"}}")
                pt = pr.text.strip().replace("```json","").replace("```","").strip()
                data = json.loads(pt)
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
    today = get_today_stats()

    try:
        photo = update.message.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        image_data = base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:
        await update.message.reply_text("Не могу загрузить фото, попробуй ещё раз.")
        logger.error(f"Download error: {e}")
        return

    ctx = f"Сегодня {date.today().strftime('%d.%m')}. Съедено: {today['calories']} ккал. Осталось: {USER_PROFILE['calories_target']-today['calories']} ккал."
    scale_words = ["весы", "picooc", "пикок", "вес", "замер", "взвесилась", "весы"]
    is_scale = any(w in caption.lower() for w in scale_words) or not caption

    if is_scale:
        prompt = SYSTEM_PROMPT + "\n" + ctx + "\nНа фото весы Picooc или напольные весы. Прочитай показатели: вес кг, % жира, % мышц, % воды. Запиши и дай короткий комментарий по динамике. Если это не весы — скажи что именно на фото и проанализируй."
    else:
        prompt = SYSTEM_PROMPT + "\n" + ctx + f"\nФото с подписью: '{caption}'. Если еда — калории и белок, сколько осталось. Если состав — подходит/нет. Если тело — опиши изменения. Если холодильник — предложи рецепт."

    try:
        image_part = {"mime_type": "image/jpeg", "data": image_data}
        response = model.generate_content([prompt, image_part])
        reply = response.text

        if is_scale:
            try:
                pr = model.generate_content(f"Из текста '{reply}' извлеки числа. ТОЛЬКО JSON: {{\"weight\": число, \"fat\": число, \"muscle\": число, \"water\": число}}")
                pt = pr.text.strip().replace("```json","").replace("```","").strip()
                data = json.loads(pt)
                if data.get("weight", 0) > 0:
                    save_weight(data["weight"], data.get("fat",0), data.get("muscle",0), data.get("water",0))
            except Exception:
                pass
        else:
            try:
                pr = model.generate_content(f"Из текста '{reply}' извлеки калории еды. ТОЛЬКО JSON: {{\"calories\": число, \"protein\": число, \"description\": \"название\"}}")
                pt = pr.text.strip().replace("```json","").replace("```","").strip()
                data = json.loads(pt)
                if data.get("calories", 0) > 0:
                    save_food(data.get("description","блюдо"), data["calories"], data.get("protein",0))
            except Exception:
                pass

        await update.message.reply_text(reply)
    except Exception as e:
        await update.message.reply_text("Не могу обработать фото, попробуй ещё раз.")
        logger.error(f"Photo error: {e}")


async def post_init(application: Application):
    init_db()
    logger.info("DB ready")


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Bot started!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
