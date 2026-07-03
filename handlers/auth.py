from aiogram import Router, F
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from utils.db import get_user_by_tg, get_user_by_code, link_user_telegram, log_action
from keyboards.kb import share_phone_kb, remove_kb, main_menu_kb

router = Router()


class AuthState(StatesGroup):
    waiting_for_code  = State()
    waiting_for_phone = State()


@router.message(Command("start"))
async def start(message: Message, state: FSMContext, admin_ids: list):
    if message.from_user.id in admin_ids:
        return

    user = await get_user_by_tg(message.from_user.id)
    if user:
        await _send_dashboard(message, user, greeting="👋 С возвращением!")
        return

    await state.set_state(AuthState.waiting_for_code)
    await message.answer(
        "👋 Добро пожаловать в аналитическую платформу *DATFO*!\n\n"
        "Введите ваш *персональный код доступа*, выданный менеджером DATFO:",
        parse_mode="Markdown"
    )


async def _send_dashboard(message: Message, user, greeting: str):
    """Сводка 'по запросу' (п.1 ТЗ): открыл бота — сразу видишь актуальные цифры,
    без необходимости лезть в подменю."""
    from utils.db import get_producer_by_id, get_latest_period, get_plan_summary
    from utils.alerts import build_alerts

    producer = await get_producer_by_id(user["producer_id"])
    period = await get_latest_period()
    producer_name = producer["name"] if producer else "Производитель"

    if not period:
        await message.answer(
            f"{greeting}\n\n"
            f"🏭 *{producer_name}*\n"
            f"👤 {user['name'] or 'Менеджер'}\n\n"
            f"📭 Данные ещё не загружены DATFO. Загляните позже — как только появится "
            f"первое обновление, вы получите уведомление автоматически.",
            parse_mode="Markdown",
            reply_markup=main_menu_kb()
        )
        return

    summary = await get_plan_summary(user["producer_id"], period["id"])
    alerts = await build_alerts(user["producer_id"], period["id"])

    lines = [
        f"{greeting}",
        "",
        f"🏭 *{producer_name}*",
        f"📅 Обновлено: *{period['period_name']}* ({period['updated_at'].strftime('%d.%m.%Y')})",
        "",
    ]

    if summary and summary["total_plan"]:
        pct = round((summary["total_fact"] or 0) / summary["total_plan"] * 100, 1)
        emoji = "🟢" if pct >= 90 else ("🟡" if pct >= 70 else "🔴")
        lines.append(f"{emoji} План выполнен на *{pct}%*")

    if alerts:
        lines.append(f"🔔 Активных алертов: *{len(alerts)}* — см. «🔔 Алерты»")
    else:
        lines.append("✅ Критических отклонений не обнаружено")

    lines.append("")
    lines.append("Выберите раздел ниже 👇  (или наберите /help — что есть в боте)")

    await message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=main_menu_kb())


@router.message(Command("help"))
async def help_cmd(message: Message, admin_ids: list):
    if message.from_user.id in admin_ids:
        return  # у админки свои команды/меню
    await message.answer(_ONBOARDING_TEXT, parse_mode="Markdown")


@router.message(AuthState.waiting_for_code)
async def handle_code(message: Message, state: FSMContext):
    code = message.text.strip()
    user = await get_user_by_code(code)

    if not user:
        await message.answer(
            "❌ Код не найден или недействителен.\n\n"
            "Проверьте код или обратитесь к менеджеру DATFO."
        )
        return

    if user["telegram_id"]:
        await message.answer(
            "⚠️ Этот код уже привязан к другому аккаунту.\n"
            "Обратитесь к менеджеру DATFO."
        )
        return

    await state.update_data(code=code)
    await state.set_state(AuthState.waiting_for_phone)
    await message.answer(
        f"✅ Код принят!\n\n"
        f"Поделитесь номером телефона для завершения регистрации:",
        reply_markup=share_phone_kb()
    )


@router.message(AuthState.waiting_for_phone, F.contact)
async def handle_phone(message: Message, state: FSMContext):
    phone = message.contact.phone_number
    if not phone.startswith("+"):
        phone = "+" + phone

    data = await state.get_data()
    code = data["code"]
    name = message.from_user.full_name or ""

    await link_user_telegram(code, message.from_user.id, phone, name)
    await state.clear()

    user = await get_user_by_code(code)
    from utils.db import get_producer_by_id
    producer = await get_producer_by_id(user["producer_id"])

    await log_action(user["id"], "register", f"phone={phone}")

    await message.answer(
        f"✅ *Регистрация завершена!*\n\n"
        f"🏭 {producer['name'] if producer else ''}\n"
        f"👤 {name}\n"
        f"📞 {phone}",
        parse_mode="Markdown",
        reply_markup=remove_kb()
    )
    await message.answer(_ONBOARDING_TEXT, parse_mode="Markdown")
    await _send_dashboard(message, user, greeting="Вот что у вас сейчас:")


_ONBOARDING_TEXT = (
    "🧭 *Коротко о разделах бота:*\n\n"
    "📊 *Мои продукты* — список ваших продуктов, у каждого есть детали "
    "(продажи/остатки/бонус/аптеки по отдельности)\n"
    "📈 *Продажи* / 📉 *Остатки* — сводка по всем продуктам сразу\n"
    "💰 *Бонусы и план* — % выполнения плана и сумма бонуса\n"
    "🏪 *Мои аптеки* — точки продаж с фильтром по региону\n"
    "🏢 *Сети (ИНН)* — сводка по сетям-партнёрам (если есть)\n"
    "🔔 *Алерты* — дефектура, отклонения от плана и продаж\n"
    "📄 *Отчёты* — выгрузка в Excel/PDF для детального анализа\n"
    "📅 *История* — динамика по периодам, с графиками\n\n"
    "В любой момент — команда /help."
)
