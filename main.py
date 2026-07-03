import asyncio
import os

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from utils.db import init_db
from handlers import admin, auth, producer

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()]


async def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не задан")
    if not ADMIN_IDS:
        raise ValueError("ADMIN_IDS не задан")

    await init_db()
    print("✅ База данных инициализирована")

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    dp["admin_ids"] = ADMIN_IDS

    # Порядок важен: admin первым (перехватывает /start для админов)
    dp.include_router(admin.router)
    dp.include_router(auth.router)
    dp.include_router(producer.router)

    print(f"🤖 Бот запущен. Администраторы: {ADMIN_IDS}")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "contact"])


if __name__ == "__main__":
    asyncio.run(main())
