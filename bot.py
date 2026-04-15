# bot.py

import os
import yaml
import asyncio
import logging
import time
from datetime import datetime, timezone
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.client.session.aiohttp import AiohttpSession
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import aiohttp

load_dotenv()

# Настройка логирования
logging.basicConfig(level=logging.INFO)

def load_config():
    with open("config.yaml", "r") as f:
        return yaml.safe_load(f)

CONFIG = load_config()

# Формируем URL прокси
PROXY_URL = f"http://{os.getenv('SQUID_PROXY_USER')}:{os.getenv('SQUID_PROXY_PASSWORD')}@{os.getenv('SQUID_PROXY_HOST')}:{os.getenv('SQUID_PROXY_PORT')}"

# Хранилище состояния в памяти (для алертов и кэша последних данных)
# В продакшене лучше использовать Redis или JSON файл
state_storage = {
    "projects": {}, # 'project_id': {'cost': 0, 'alerts_sent': 0, 'last_update': 'date'}
}


async def fetch_openai_usage(project_id, models_config):
    # Используем UTC для консистентности с API OpenAI
    now_utc = datetime.now(timezone.utc)
    start_of_day = int(now_utc.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    
    url = f"https://api.openai.com/v1/organization/usage/completions?start_time={start_of_day}&project_ids={project_id}&group_by=project_id,model"
    headers = {
        "Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}",
        "Content-Type": "application/json"
    }

    try:
        # Важно: создаем сессию с таймаутом
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, proxy=PROXY_URL) as response:
                if response.status != 200:
                    err_text = await response.text()
                    logging.error(f"OpenAI API Error {response.status}: {err_text}")
                    return None
                
                data = await response.json()
                total_cost = 0.0
                
                for bucket in data.get('data', []):
                    for res in bucket.get('results', []):
                        model_full_name = res.get('model')
                        # Ищем тариф: сначала в проекте, потом дефолтный
                        pricing = models_config.get(model_full_name, CONFIG.get('default_pricing'))
                        
                        # Если вдруг и в дефолте нет (мало ли), берем gpt-4o-mini тариф
                        if not pricing:
                            pricing = {"input": 0.15, "cached": 0.075, "output": 0.60}

                        u_in = res.get('input_uncached_tokens', 0)
                        c_in = res.get('input_cached_tokens', 0)
                        out = res.get('output_tokens', 0)
                        
                        cost = (
                            (u_in * pricing['input'] / 1_000_000) +
                            (c_in * pricing['cached'] / 1_000_000) +
                            (out * pricing['output'] / 1_000_000)
                        )
                        total_cost += cost
                
                return round(total_cost, 4)
    except Exception as e:
        logging.error(f"Fetch failed for {project_id}: {e}")
        return None



async def check_expenses_job(bot: Bot):
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    for project in CONFIG['projects']:
        p_id = project['id']
        p_name = project['name']
        threshold = project['threshold_usd']
        responsibles = project['responsible_ids']
        
        current_cost = await fetch_openai_usage(p_id, project.get('models', {}))
        
        if current_cost is None:
            continue

        # Инициализация хранилища для проекта
        if p_id not in state_storage["projects"]:
            state_storage["projects"][p_id] = {"cost": 0, "alerts_sent": 0, "last_date": today_str}
        
        # Сброс счетчика алертов, если наступил новый день
        if state_storage["projects"][p_id]["last_date"] != today_str:
            state_storage["projects"][p_id] = {"cost": current_cost, "alerts_sent": 0, "last_date": today_str}
        else:
            state_storage["projects"][p_id]["cost"] = current_cost

        # Проверка порога и отправка уведомлений (макс 3 раза)
        if current_cost >= threshold and state_storage["projects"][p_id]["alerts_sent"] < 3:
            state_storage["projects"][p_id]["alerts_sent"] += 1
            alert_text = (
                f"⚠️ <b>ВНИМАНИЕ: Превышен лимит!</b>\n\n"
                f"Проект: {p_name}\n"
                f"Текущий расход: <b>${current_cost}</b>\n"
                f"Порог: ${threshold}\n"
                f"Уведомление {state_storage['projects'][p_id]['alerts_sent']} из 3"
            )
            
            for user_id in responsibles:
                try:
                    await bot.send_message(user_id, alert_text, parse_mode="HTML")
                except Exception as e:
                    logging.error(f"Could not send alert to {user_id}: {e}")



# Настройка бота через прокси
session = AiohttpSession(proxy=PROXY_URL)
bot = Bot(token=os.getenv("TELEGRAM_BOT_TOKEN"), session=session)
dp = Dispatcher()

# Middleware для проверки доступа
def is_authorized(user_id: int):
    all_allowed = []
    for p in CONFIG['projects']:
        all_allowed.extend(p['responsible_ids'])
    return user_id in all_allowed

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not is_authorized(message.from_user.id):
        return
    await message.answer("Бот мониторинга расходов OpenAI запущен.\n/projects - список проектов\n/status - текущие расходы")

@dp.message(Command("projects"))
async def cmd_projects(message: types.Message):
    if not is_authorized(message.from_user.id):
        return
    
    text = "<b>Подключенные проекты:</b>\n\n"
    for p in CONFIG['projects']:
        text += f"• {p['name']} (ID: <code>{p['id']}</code>)\n  Порог: ${p['threshold_usd']}\n"
    
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    if not is_authorized(message.from_user.id):
        return
    
    text = "<b>Текущие расходы за сегодня:</b>\n\n"
    for p in CONFIG['projects']:
        p_id = p['id']
        data = state_storage["projects"].get(p_id, {"cost": "Нет данных", "alerts_sent": 0})
        cost = data['cost']
        text += f"• {p['name']}: <b>${cost}</b> (Алертов: {data['alerts_sent']}/3)\n"
    
    await message.answer(text, parse_mode="HTML")

async def main():
    # Запуск планировщика
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_expenses_job, 'interval', minutes=5, args=[bot])
    scheduler.start()

    # Сразу запускаем проверку при старте
    asyncio.create_task(check_expenses_job(bot))

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())