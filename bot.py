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
    "workouts": ["зарядка", "тазовое дно", "резинка"],
}

SYSTEM_PROMPT = f"""Ты личный нутрициолог и фитнес-тренер для {USER_PROFILE['name']}.

Параметры:
- Рост: {USER_PROFILE['height']} см, возраст: {USER_PROFILE['age']} лет
- Стартовый вес: {USER_PROFILE['weight_start']} кг, жир: {USER_PROFILE['fat_start']}%
- Цель: похудеть на 10 кг за 2 месяца
- Дневная норма: {USER_PROFILE['calories_target']} ккал, белок {USER_PROFILE['protein_target']}г, вода {USER_PROFILE['water_target']}л
- Не ест: {', '.join(USER_PROFILE['no_eat'])}
- Тренировки: {', '.join(USER_PROFILE['workouts'])}

Твои задачи:
1. Анализировать фото еды — определять калории и белок, добавлять в дневник
2. Читать фото с весов Picooc — записывать вес, жир, мышцы, воду
3. Анализировать состав продуктов — говорить подходит/нет
4. По содержимому холодильника — предлагать рецепты под оставшиеся калории
5. Корректировать: говорить если мало белка, мало воды, много калорий
6. Отслеживать динамику — сравнивать с предыдущими замерами
7. Анализировать фото тела — описывать изменения

Стиль общения: дружелюбно, кратко, по делу. Без лишних слов.
Отвечай на русском языке.
"""


def init_db():
    conn = sqlite3.connect("nutrition.db")
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS weight_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, weight REAL, fat REAL,
        muscle REAL, water REAL, bmr REAL,
        visceral INTEGER, created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS food_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, meal TEXT, description TEXT,
        calories INTEGER, protein REAL, created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS body_photos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, file_id TEXT, notes TEXT, created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS sleep_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, bedtime TEXT, wakeup TEXT,
        hours REAL, quality TEXT, created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS cycle_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, day INTEGER, phase TEXT, notes TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS challenges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, start_date TEXT, end_date TEXT,
        days_completed INTEGER, active INTEGER
    )""")
    conn.commit()
    conn.close()


def get_today_stats():
    conn = sqlite3.connect("nutrition.db")
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


def get_last_weight():
    conn = sqlite3.connect("nutrition.db")
    c = conn.cursor()
    c.execute("SELECT weight, fat, muscle, water, date FROM weight_log ORDER BY created_at DESC LIMIT 1")
    row = c.fetchone()
    conn.close()
    if row:
        return {"weight": row[0], "fat": row[1], "muscle": row[2], "water": row[3], "date": row[4]}
    return None


def save_weight(weight, fat, muscle, water, bmr=None, visceral=None):
    conn = sqlite3.connect("nutrition.db")
    c = conn.cursor()
    today = date.today().isoformat()
    c.execute("INSERT INTO weight_log (date, weight, fat, muscle, water, bmr, visceral, created_at) VALUES (?,?,?,?,?,?,?,?)",
              (today, weight, fat, muscle, water, bmr, visceral, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def save_food(description, calories, protein, meal=""):
    conn = sqlite3.connect("nutrition.db")
    c = conn.cursor()
    today = date.today().isoformat()
    c.execute("INSERT INTO food_log (date, meal, description, calories, protein, created_at) VALUES (?,?,?,?,?,?)",
              (today, meal, description, calories, protein, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def get_weekly_report():
    conn = sqlite3.connect("nutrition.db")
    c = conn.cursor()
    c.execute("SELECT date, weight, fat, muscle FROM weight_log ORDER BY created_at DESC LIMIT 7")
    weights = c.fetchall()
    c.execute("SELECT date, SUM(calories), SUM(protein) FROM food_log GROUP BY date ORDER BY date DESC LIMIT 7")
    foods = c.fetchall()
    conn.close()
    return {"weights": weights, "foods": foods}


def main_keyboard():
    keyboard = [
        [KeyboardButton("📊 Статистика"), KeyboardButton("📸 Фото весов")],
        [KeyboardButton("🍽 Записать еду"), KeyboardButton("🛒 Что купить")],
        [KeyboardButton("💪 Тренировки"), KeyboardButton("📅 Еженедельный отчёт")],
        [KeyboardButton("🌙 Сон"), KeyboardButton("🔄 Цикл")],
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
            msg += f"Добавь ещё {protein_left:.0f}г белка\n\n"
        if today_stats["meals"]:
            msg += "🍽 Съедено:\n" + "\n".join(f"• {m}" for m in today_stats["meals"])
        if last_weight:
            diff = last_weight["weight"] - USER_PROFILE["weight_start"]
            msg += f"\n\n⚖️ Последний вес: {last_weight['weight']} кг"
            msg += f" ({'−' if diff < 0 else '+'}{abs(diff):.1f} кг от старта)"
        await update.message.reply_text(msg)
        return

    if text == "📅 Еженедельный отчёт":
        report = get_weekly_report()
        msg = "📅 Отчёт за неделю\n\n"
        if report["weights"]:
            msg += "⚖️ Динамика веса:\n"
            for w in report["weights"]:
                msg += f"• {w[0]}: {w[1]} кг, жир {w[2]}%\n"
        if report["foods"]:
            msg += "\n🍽 Питание:\n"
            for f in report["foods"]:
                msg += f"• {f[0]}: {f[1] or 0} ккал, белок {f[2] or 0:.0f}г\n"
        await update.message.reply_text(msg)
        return

    context_info = f"""
Сегодня {date.today().strftime('%d.%m.%Y')}.
Съедено: {today_stats['calories']} ккал, белок {today_stats['protein']:.0f}г.
Осталось: {USER_PROFILE['calories_target'] - today_stats['calories']} ккал, белок {USER_PROFILE['protein_target'] - today_stats['protein']:.0f}г.
"""
    if last_weight:
        context_info += f"Последний вес: {last_weight['weight']} кг, жир {last_weight['fat']}%.\n"

    prompt = SYSTEM_PROMPT + context_info + f"\nСообщение пользователя: {text}"

    try:
        response = model.generate_content(prompt)
        reply = response.text

        if any(word in text.lower() for word in ["съела", "поела", "завтрак", "обед", "ужин", "перекус", "выпила"]):
            try:
                parse_prompt = f"Из текста '{text}' определи калории и белок. Ответь ТОЛЬКО в формате JSON: {{\"calories\": число, \"protein\": число, \"description\": \"краткое описание\"}}"
                parse_response = model.generate_content(parse_prompt)
                parse_text = parse_response.text.strip()
                if "```" in parse_text:
                    parse_text = parse_text.split("```")[1].replace("json", "").strip()
                data = json.loads(parse_text)
                save_food(data.get("description", text), data.get("calories", 0), data.get("protein", 0))
            except Exception:
                pass

        await update.message.reply_text(reply)
    except Exception as e:
        await update.message.reply_text("Что-то пошло не так, попробуй ещё раз.")
        logger.error(f"Error: {e}")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = update.message.caption or ""
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    file_bytes = await file.download_as_bytearray()
    image_data = base64.b64encode(file_bytes).decode("utf-8")

    today_stats = get_today_stats()
    context_info = f"""
Сегодня {date.today().strftime('%d.%m.%Y')}.
Съедено: {today_stats['calories']} ккал, белок {today_stats['protein']:.0f}г.
Осталось: {USER_PROFILE['calories_target'] - today_stats['calories']} ккал.
"""

    is_scale = any(word in caption.lower() for word in ["весы", "picooc", "вес", "замер"]) or not caption

    if is_scale:
        prompt = SYSTEM_PROMPT + context_info + """
На фото весы Picooc. Прочитай все показатели: вес, % жира, % мышц, % воды, СООВ, висцеральный жир.
Запиши данные и сравни с предыдущими показателями если они есть.
Дай короткий комментарий по динамике.
Если это не весы — проанализируй фото как еду.
"""
    else:
        prompt = SYSTEM_PROMPT + context_info + f"""
Пользователь прислал фото. Подпись: "{caption}".
Если это еда — определи блюдо, калории, белок, скажи что записала и сколько осталось на день.
Если это состав продукта — проанализируй и скажи подходит ли.
Если это тело — опиши изменения по сравнению с предыдущим фото.
Если это холодильник — предложи рецепт из того что видишь.
"""

    try:
        image_part = {"mime_type": "image/jpeg", "data": image_data}
        response = model.generate_content([prompt, image_part])
        reply = response.text

        if is_scale and any(c.isdigit() for c in reply):
            try:
                parse_prompt = f"Из текста '{reply}' извлеки числа. Ответь ТОЛЬКО JSON: {{\"weight\": число, \"fat\": число, \"muscle\": число, \"water\": число}}"
                parse_resp = model.generate_content(parse_prompt)
                parse_text = parse_resp.text.strip()
                if "```" in parse_text:
                    parse_text = parse_text.split("```")[1].replace("json", "").strip()
                data = json.loads(parse_text)
                save_weight(data.get("weight", 0), data.get("fat", 0), data.get("muscle", 0), data.get("water", 0))
            except Exception:
                pass
        elif not is_scale:
            try:
                parse_prompt = f"Из текста '{reply}' извлеки калории и белок еды. Ответь ТОЛЬКО JSON: {{\"calories\": число, \"protein\": число, \"description\": \"краткое название блюда\"}}"
                parse_resp = model.generate_content(parse_prompt)
                parse_text = parse_resp.text.strip()
                if "```" in parse_text:
                    parse_text = parse_text.split("```")[1].replace("json", "").strip()
                data = json.loads(parse_text)
                if data.get("calories", 0) > 0:
                    save_food(data.get("description", "блюдо"), data.get("calories", 0), data.get("protein", 0))
            except Exception:
                pass

        await update.message.reply_text(reply)
    except Exception as e:
        await update.message.reply_text("Не могу обработать фото, попробуй ещё раз.")
        logger.error(f"Photo error: {e}")


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Bot started!")
    app.run_polling()


if __name__ == "__main__":
    main()
