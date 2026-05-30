import os
import io
import json
import sqlite3
import logging
import base64
import asyncio
from datetime import datetime, date
import httpx
import anthropic

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

USER_PROFILE = {
    "weight_start": 78.4,
    "fat_start": 39.6,
    "calories_target": 1420,
    "protein_target": 110,
}

SYSTEM_PROMPT = """Ты личный нутрициолог для Кравцовой Анастасии.
Параметры: рост 162см, возраст 35 лет, стартовый вес 78.4кг, жир 39.6%.
Цель: минус 10кг за 2 месяца. Норма: 1420 ккал, белок 110г, вода 2.5л.
Не ест: варёные овощи, лук.
Задачи: анализировать фото еды, читать весы Picooc, проверять состав продуктов, предлагать рецепты из холодильника, корректировать питание, анализировать фото тела.
Стиль: дружелюбно, кратко, по делу. Всегда на русском."""

DB_PATH = "/tmp/nutrition.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
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


def get_today_stats():
    try:
        conn = sqlite3.connect(DB_PATH)
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
        conn = sqlite3.connect(DB_PATH)
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
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO weight_log (date, weight, fat, muscle, water, created_at) VALUES (?,?,?,?,?,?)",
                  (date.today().isoformat(), weight, fat, muscle, water, datetime.now().isoformat()))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"save_weight: {e}")


def save_food(description, calories, protein):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO food_log (date, description, calories, protein, created_at) VALUES (?,?,?,?,?)",
                  (date.today().isoformat(), description, calories, protein, datetime.now().isoformat()))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"save_food: {e}")


def ask_claude(prompt, image_data=None):
    content = []
    if image_data:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": image_data}
        })
    content.append({"type": "text", "text": prompt})
    response = claude.messages.create(
        model="claude-opus-4-5",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}]
    )
    return response.content[0].text


async def send_message(chat_id, text, reply_markup=None):
    async with httpx.AsyncClient() as client:
        data = {"chat_id": chat_id, "text": text}
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        await client.post(f"{API_URL}/sendMessage", json=data)


def main_keyboard():
    return {
        "keyboard": [
            [{"text": "📊 Статистика"}, {"text": "📸 Фото весов"}],
            [{"text": "🍽 Записать еду"}, {"text": "🛒 Список покупок"}],
            [{"text": "📅 Отчёт за неделю"}, {"text": "💪 Тренировки"}],
        ],
        "resize_keyboard": True
    }


async def handle_update(update):
    message = update.get("message", {})
    if not message:
        return

    chat_id = message["chat"]["id"]
    text = message.get("text", "")
    photo = message.get("photo")
    caption = message.get("caption", "")

    if text == "/start":
        await send_message(chat_id,
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
        return

    if text == "📊 Статистика":
        today = get_today_stats()
        last_w = get_last_weight()
        cal_left = USER_PROFILE["calories_target"] - today["calories"]
        prot_left = USER_PROFILE["protein_target"] - today["protein"]
        msg = f"📊 {date.today().strftime('%d.%m')}\n\n"
        msg += f"🔥 {today['calories']} / {USER_PROFILE['calories_target']} ккал (осталось {max(0, cal_left)})\n"
        msg += f"💪 {today['protein']:.0f} / {USER_PROFILE['protein_target']}г белка"
        if prot_left > 0:
            msg += f" (ещё {prot_left:.0f}г)"
        if today["meals"]:
            msg += "\n\n🍽 " + "\n• ".join([""] + today["meals"])
        if last_w:
            diff = last_w["weight"] - USER_PROFILE["weight_start"]
            sign = "−" if diff < 0 else "+"
            msg += f"\n\n⚖️ Вес: {last_w['weight']} кг ({sign}{abs(diff):.1f} от старта)"
        await send_message(chat_id, msg)
        return

    if text == "📅 Отчёт за неделю":
        try:
            conn = sqlite3.connect(DB_PATH)
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
            await send_message(chat_id, msg)
        except Exception:
            await send_message(chat_id, "Не могу загрузить данные.")
        return

    if text == "💪 Тренировки":
        msg = "💪 План на сегодня:\n\n• Зарядка — 10 мин\n• Тазовое дно (Сабина Филина)\n• Тренировка с резинкой — 25 мин\n• Диафрагмальное дыхание — 5 мин"
        await send_message(chat_id, msg)
        return

    if photo:
        await handle_photo(chat_id, photo, caption)
        return

    if text:
        await handle_text_msg(chat_id, text)


async def handle_photo(chat_id, photo, caption):
    today = get_today_stats()
    try:
        file_id = photo[-1]["file_id"]
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{API_URL}/getFile", params={"file_id": file_id})
            file_path = r.json()["result"]["file_path"]
            r2 = await client.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}")
            image_data = base64.b64encode(r2.content).decode("utf-8")
    except Exception as e:
        await send_message(chat_id, "Не могу загрузить фото, попробуй ещё раз.")
        logger.error(f"Photo download: {e}")
        return

    ctx = f"Сегодня {date.today().strftime('%d.%m')}. Съедено: {today['calories']} ккал. Осталось: {USER_PROFILE['calories_target']-today['calories']} ккал."
    scale_words = ["весы", "picooc", "пикок", "вес", "замер", "взвесилась"]
    is_scale = any(w in caption.lower() for w in scale_words) or not caption

    if is_scale:
        prompt = ctx + "\nНа фото весы Picooc. Прочитай показатели: вес кг, % жира, % мышц, % воды. Дай комментарий."
    else:
        prompt = ctx + f"\nФото с подписью: '{caption}'. Если еда — калории и белок. Если состав — подходит/нет. Если тело — опиши изменения. Если холодильник — предложи рецепт."

    try:
        reply = ask_claude(prompt, image_data=image_data)

        if is_scale:
            try:
                parse_text = ask_claude(f"Из текста '{reply}' извлеки числа. ТОЛЬКО JSON: {{\"weight\": число, \"fat\": число, \"muscle\": число, \"water\": число}}")
                data = json.loads(parse_text.strip().replace("```json","").replace("```","").strip())
                if data.get("weight", 0) > 0:
                    save_weight(data["weight"], data.get("fat",0), data.get("muscle",0), data.get("water",0))
            except Exception:
                pass
        else:
            try:
                parse_text = ask_claude(f"Из текста '{reply}' извлеки калории. ТОЛЬКО JSON: {{\"calories\": число, \"protein\": число, \"description\": \"название\"}}")
                data = json.loads(parse_text.strip().replace("```json","").replace("```","").strip())
                if data.get("calories", 0) > 0:
                    save_food(data.get("description","блюдо"), data["calories"], data.get("protein",0))
            except Exception:
                pass

        await send_message(chat_id, reply)
    except Exception as e:
        await send_message(chat_id, "Не могу обработать фото, попробуй ещё раз.")
        logger.error(f"Photo error: {e}")


async def handle_text_msg(chat_id, text):
    today = get_today_stats()
    last_w = get_last_weight()
    ctx = f"\nСегодня {date.today().strftime('%d.%m')}. Съедено: {today['calories']} ккал, белок {today['protein']:.0f}г. Осталось: {USER_PROFILE['calories_target']-today['calories']} ккал."
    if last_w:
        ctx += f" Последний вес: {last_w['weight']} кг."

    try:
        reply = ask_claude(ctx + f"\nСообщение: {text}")

        food_words = ["съела","поела","завтрак","обед","ужин","перекус","выпила","ем","пью","скушала"]
        if any(w in text.lower() for w in food_words):
            try:
                parse_text = ask_claude(f"Из текста '{text}' определи калории. ТОЛЬКО JSON: {{\"calories\": число, \"protein\": число, \"description\": \"название\"}}")
                data = json.loads(parse_text.strip().replace("```json","").replace("```","").strip())
                if data.get("calories", 0) > 0:
                    save_food(data.get("description", text), data.get("calories",0), data.get("protein",0))
            except Exception:
                pass

        await send_message(chat_id, reply)
    except Exception as e:
        await send_message(chat_id, "Что-то пошло не так, попробуй ещё раз.")
        logger.error(f"Text error: {e}")


async def main():
    init_db()
    logger.info("Bot started!")
    offset = 0
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                r = await client.get(f"{API_URL}/getUpdates", params={"offset": offset, "timeout": 25})
                updates = r.json().get("result", [])
                for update in updates:
                    offset = update["update_id"] + 1
                    await handle_update(update)
            except Exception as e:
                logger.error(f"Polling error: {e}")
                await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
