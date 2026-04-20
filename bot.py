import os
import yaml
import asyncio
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import aiohttp

load_dotenv()
logging.basicConfig(level=logging.INFO)

def load_config():
    with open("config.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

CONFIG = load_config()
PROXY_URL = f"http://{os.getenv('SQUID_PROXY_USER')}:{os.getenv('SQUID_PROXY_PASSWORD')}@{os.getenv('SQUID_PROXY_HOST')}:{os.getenv('SQUID_PROXY_PORT')}"

# Хранилище состояния
state_storage = {"projects": {}}
session = AiohttpSession(proxy=PROXY_URL)
bot = Bot(token=os.getenv("TELEGRAM_BOT_TOKEN"), session=session)
dp = Dispatcher()


# --- Логика API (без изменений) ---
async def fetch_openai_usage(project_id, models_config):
    now_utc = datetime.now(timezone.utc)
    start_of_day = int(now_utc.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    url = f"https://api.openai.com/v1/organization/usage/completions?start_time={start_of_day}&project_ids={project_id}&group_by=project_id,model"
    headers = {"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}", "Content-Type": "application/json"}

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, proxy=PROXY_URL) as response:
                if response.status != 200:
                    return None
                data = await response.json()
                total_cost = 0.0
                for bucket in data.get('data', []):
                    for res in bucket.get('results', []):
                        model_name = res.get('model')
                        pricing = models_config.get(model_name, CONFIG.get('default_pricing'))
                        u_in = res.get('input_uncached_tokens', 0)
                        c_in = res.get('input_cached_tokens', 0)
                        out = res.get('output_tokens', 0)
                        cost = ((u_in * pricing['input'] / 1_000_000) + 
                                (c_in * pricing['cached'] / 1_000_000) + 
                                (out * pricing['output'] / 1_000_000))
                        total_cost += cost
                return round(total_cost, 4)
    except:
        return None

# --- Клавиатуры ---

def get_main_menu_kb():
    builder = InlineKeyboardBuilder()
    for p in CONFIG['projects']:
        builder.row(InlineKeyboardButton(text=f"📂 {p['name']}", callback_data=f"view_project:{p['id']}"))
    builder.row(InlineKeyboardButton(text="🔄 Обновить всё", callback_data="refresh_main"))
    return builder.as_markup()

def get_back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="main_menu")]
    ])

# --- Вспомогательные функции ---

def is_authorized(user_id: int):
    allowed = []
    for p in CONFIG['projects']:
        allowed.extend(p['responsible_ids'])
    return user_id in allowed

def get_project_by_id(p_id):
    return next((p for p in CONFIG['projects'] if p['id'] == p_id), None)

# --- Обработчики интерфейса ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not is_authorized(message.from_user.id): return
    
    await message.answer(
        "👋 <b>Система контроля расходов OpenAI</b>\n\n"
        "Выберите проект из списка ниже для получения детальной статистики:",
        reply_markup=get_main_menu_kb(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "main_menu")
async def callback_main_menu(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "👋 <b>Система контроля расходов OpenAI</b>\n\n"
        "Выберите проект из списка ниже для получения детальной статистики:",
        reply_markup=get_main_menu_kb(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data.startswith("view_project:"))
async def callback_view_project(callback: types.CallbackQuery):
    p_id = callback.data.split(":")[1]
    p = get_project_by_id(p_id)
    
    if not p:
        await callback.answer("Проект не найден")
        return

    # Берем данные из кэша (state_storage)
    data = state_storage["projects"].get(p_id, {"cost": 0, "alerts_sent": 0})
    
    # Формируем список моделей и тарифов
    models_info = ""
    models_config = p.get('models', {})
    for m_name, prices in models_config.items():
        models_info += f"  • <code>{m_name}</code>\n"
        models_info += f"    In: ${prices['input']} | Out: ${prices['output']} (за 1M)\n"

    # Формируем список ответственных
    resp_links = ", ".join([f"<a href='tg://user?id={uid}'>{uid}</a>" for uid in p['responsible_ids']])

    text = (
        f"📊 <b>Проект: {p['name']}</b>\n"
        f"<code>{p['id']}</code>\n\n"
        f"👤 <b>Ответственные:</b> {resp_links}\n"
        f"💰 <b>Лимит:</b> ${p['threshold_usd']}\n"
        f"📈 <b>Расход сегодня:</b> <u>${data['cost']}</u>\n"
        f"🔔 <b>Алерты:</b> {data['alerts_sent']}/3 за сегодня\n\n"
        f"🤖 <b>Тарифы моделей:</b>\n{models_info}"
    )

    await callback.message.edit_text(
        text,
        reply_markup=get_back_kb(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "refresh_main")
async def callback_refresh(callback: types.CallbackQuery):
    await callback.answer("Обновление данных...")
    await check_expenses_job(bot)
    await callback.message.edit_text(
        "✅ Данные обновлены!\nВыберите проект:",
        reply_markup=get_main_menu_kb()
    )

# --- Фоновая задача (Alerts) ---

async def check_expenses_job(bot: Bot):
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    for project in CONFIG['projects']:
        p_id = project['id']
        current_cost = await fetch_openai_usage(p_id, project.get('models', {}))
        
        if current_cost is None: continue

        if p_id not in state_storage["projects"]:
            state_storage["projects"][p_id] = {"cost": 0, "alerts_sent": 0, "last_date": today_str}
        
        # Сброс даты
        if state_storage["projects"][p_id]["last_date"] != today_str:
            state_storage["projects"][p_id] = {"cost": current_cost, "alerts_sent": 0, "last_date": today_str}
        else:
            state_storage["projects"][p_id]["cost"] = current_cost

        # Алерты
        if current_cost >= project['threshold_usd'] and state_storage["projects"][p_id]["alerts_sent"] < 3:
            state_storage["projects"][p_id]["alerts_sent"] += 1
            alert_text = (
                f"🚨 <b>LIMIT EXCEEDED</b> 🚨\n\n"
                f"Проект: <b>{project['name']}</b>\n"
                f"Расход: <b>${current_cost}</b> / ${project['threshold_usd']}\n"
                f"Уведомление {state_storage['projects'][p_id]['alerts_sent']} из 3"
            )
            for user_id in project['responsible_ids']:
                try:
                    await bot.send_message(user_id, alert_text, parse_mode="HTML", reply_markup=get_back_kb())
                except: pass




async def main():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_expenses_job, 'interval', minutes=5, args=[bot])
    scheduler.start()
    asyncio.create_task(check_expenses_job(bot))
    
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    
    asyncio.run(main())