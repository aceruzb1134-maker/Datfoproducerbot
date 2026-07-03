from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command, BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from utils.db import (
    get_all_producers, get_all_users, create_producer, create_user,
    deactivate_user, get_latest_period, get_all_periods,
    create_period, get_or_create_period, upsert_pharmacy, upsert_product,
    save_stock, save_sale, save_plan, get_pool,
    get_producer_ids_in_period, get_users_by_producer,
    get_alert_settings, update_alert_settings, upsert_entity_plan,
    batch_upsert_products, batch_upsert_pharmacies, batch_save_stock,
    batch_save_sale, batch_upsert_entity_plans,
    get_old_periods, delete_period_cascade,
)
from utils.excel import parse_analytics_excel, fmt_money
from utils.excel_producer_import import parse_producer_workbook
from utils.alerts import build_alerts, format_alerts_text
from keyboards.kb import (
    admin_menu_kb, confirm_upload_kb, alert_settings_producer_kb,
)

router = Router()


class IsAdmin(BaseFilter):
    """
    ВАЖНО: используется как фильтр, а не проверка внутри тела хендлера.
    Если бы проверка была только внутри функции (return без обработки),
    aiogram всё равно считал бы апдейт "обработанным" этим роутером и НЕ
    передавал бы его дальше в auth.router — из-за этого /start от не-админа
    просто пропадал в никуда (бот "не реагировал").
    """
    async def __call__(self, message: Message, admin_ids: list) -> bool:
        return message.from_user.id in admin_ids


class UploadState(StatesGroup):
    waiting_for_file        = State()
    waiting_for_period_name = State()
    confirming_upload       = State()


class AddProducerState(StatesGroup):
    waiting_for_name        = State()
    waiting_for_code        = State()
    waiting_for_access_code = State()


class AlertSettingsState(StatesGroup):
    waiting_for_producer = State()
    waiting_for_values   = State()


def is_admin(user_id: int, admin_ids: list) -> bool:
    return user_id in admin_ids


# ── /start for admin ──────────────────────────────────────────────────────────

@router.message(Command("start"), IsAdmin())
async def admin_start(message: Message, admin_ids: list):
    period = await get_latest_period()
    period_info = f"📅 Последнее обновление: *{period['period_name']}*\n🕐 {period['updated_at'].strftime('%d.%m.%Y %H:%M')}" if period else "📭 Данных пока нет"

    await message.answer(
        f"👋 Панель администратора *DATFO*\n\n"
        f"{period_info}\n\n"
        f"Управляйте данными и пользователями:",
        parse_mode="Markdown",
        reply_markup=admin_menu_kb()
    )


@router.message(Command("admin"), IsAdmin())
async def admin_cmd(message: Message, admin_ids: list):
    await admin_start(message, admin_ids)


@router.message(F.text == "🔙 Выйти из админки")
async def exit_admin(message: Message, admin_ids: list):
    if not is_admin(message.from_user.id, admin_ids):
        return
    from keyboards.kb import remove_kb
    await message.answer("Вышли из панели администратора.", reply_markup=remove_kb())


# ── Upload data ───────────────────────────────────────────────────────────────

@router.message(F.text == "📤 Загрузить данные")
async def prompt_upload(message: Message, state: FSMContext, admin_ids: list):
    if not is_admin(message.from_user.id, admin_ids):
        return
    await state.set_state(UploadState.waiting_for_period_name)
    await message.answer(
        "Введите *название периода* для этого обновления.\n\n"
        "Например: `Январь 2026 — обновление 1`\n"
        "или: `15.01.2026`",
        parse_mode="Markdown"
    )


@router.message(UploadState.waiting_for_period_name, F.text)
async def handle_period_name(message: Message, state: FSMContext, admin_ids: list):
    if not is_admin(message.from_user.id, admin_ids):
        return
    await state.update_data(period_name=message.text.strip())
    await state.set_state(UploadState.waiting_for_file)
    await message.answer(
        "📎 Теперь отправьте *Excel-файл* с данными.\n\n"
        "Формат файла:\n"
        "• Лист `Data` или первый лист\n"
        "• Строка 1: заголовки\n"
        "• Колонки: Производитель | Код SKU | Продукт | ИНН | Аптека | Регион | Город | Адрес | "
        "Остаток | Продажи шт | Продажи сум | План шт | План сум | Факт шт | Факт сум | Бонус | Дата",
        parse_mode="Markdown"
    )


@router.message(UploadState.waiting_for_period_name, F.document)
async def handle_period_name_skipped(message: Message, state: FSMContext, admin_ids: list):
    """
    Админ прислал файл сразу, не введя название периода текстом — не роняем сценарий,
    а берём период по умолчанию и сразу обрабатываем файл (для нового формата
    производителя название периода всё равно берётся из листов самого файла).
    """
    if not is_admin(message.from_user.id, admin_ids):
        return
    from datetime import datetime
    default_period_name = f"Обновление {datetime.now().strftime('%d.%m.%Y')}"
    await state.set_state(UploadState.waiting_for_file)
    await state.update_data(period_name=default_period_name)
    await process_uploaded_file(message, state, admin_ids)


@router.message(UploadState.waiting_for_file, F.document)
async def handle_upload(message: Message, state: FSMContext, admin_ids: list):
    if not is_admin(message.from_user.id, admin_ids):
        return
    await process_uploaded_file(message, state, admin_ids)


async def process_uploaded_file(message: Message, state: FSMContext, admin_ids: list):
    doc = message.document
    if not doc.file_name.endswith((".xlsx", ".xls")):
        await message.answer("❌ Нужен файл .xlsx")
        return

    data = await state.get_data()
    period_name = data.get("period_name", "Обновление")
    await state.clear()

    processing = await message.answer("⏳ Обрабатываю файл...")
    file = await message.bot.get_file(doc.file_id)
    content = (await message.bot.download_file(file.file_path)).read()

    # 1) Пробуем формат производителя (свод + листы-месяцы, например Astra Zeneca-Fom-...)
    try:
        new_format = parse_producer_workbook(content, filename=doc.file_name)
    except Exception as e:
        import traceback
        traceback.print_exc()
        await processing.edit_text(
            f"❌ Ошибка при разборе файла (формат производителя): `{e}`\n\n"
            f"Подробности в логах сервера. Проверьте, что файл не повреждён.",
            parse_mode="Markdown"
        )
        return

    if new_format:
        # ВАЖНО: каждая таблица — это данные ОДНОГО конкретного производителя, загружаемые
        # отдельно. Прежде чем что-либо писать в базу, показываем, какого производителя
        # бот распознал из файла, и ждём явного подтверждения — чтобы не записать данные
        # не туда, если формат файла окажется нестандартным.
        existing = await _find_producer_by_name(new_format["producer_name"])
        status_line = (
            "⚠️ Такой производитель уже есть в базе — новые данные *обновят* существующие периоды с теми же названиями."
            if existing else
            "🆕 Такого производителя в базе ещё нет — будет создан новый."
        )
        total_rows = sum(len(p["rows"]) for p in new_format["periods"])
        periods_list = ", ".join(p["period_name"] for p in new_format["periods"])

        await state.update_data(pending_new_format=new_format)
        await state.set_state(UploadState.confirming_upload)

        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да, верно — загрузить", callback_data="confirm_upload"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_upload"),
        ]])

        await processing.edit_text(
            f"📋 *Файл распознан*\n\n"
            f"🏭 Производитель (определён из файла): *{new_format['producer_name']}*\n"
            f"{status_line}\n\n"
            f"📅 Периоды в файле: {periods_list}\n"
            f"📊 Всего строк детализации: {total_rows}\n\n"
            f"Это верно? Все данные из файла будут привязаны именно к этому производителю.",
            parse_mode="Markdown", reply_markup=kb
        )
        return

    # 2) Запасной вариант — старый плоский формат (один лист, фиксированные колонки)
    try:
        parsed = parse_analytics_excel(content)
    except Exception as e:
        import traceback
        traceback.print_exc()
        await processing.edit_text(f"❌ Ошибка при разборе файла: `{e}`", parse_mode="Markdown")
        return

    if not parsed:
        await processing.edit_text(
            "❌ Не удалось распознать файл. Проверьте, что это либо файл производителя "
            "(листы \"свод\" + месяцы с колонкой FOM ID), либо плоская таблица со стандартными колонками."
        )
        return

    await _handle_flat_format_upload(message, processing, parsed, period_name)


async def _find_producer_by_name(name: str):
    producers = await get_all_producers()
    for p in producers:
        if p["name"].strip().lower() == name.strip().lower():
            return p
    return None


@router.callback_query(UploadState.confirming_upload, F.data == "confirm_upload")
async def confirm_upload(callback: CallbackQuery, state: FSMContext, admin_ids: list):
    if not is_admin(callback.from_user.id, admin_ids):
        await callback.answer()
        return

    data = await state.get_data()
    new_format = data.get("pending_new_format")
    await state.clear()

    if not new_format:
        await callback.message.edit_text("❌ Данные загрузки устарели (бот, возможно, перезапускался). Загрузите файл заново.")
        await callback.answer()
        return

    await callback.message.edit_text(f"⏳ Загружаю данные — {new_format['producer_name']}...")
    await callback.answer()

    try:
        await _handle_new_format_upload(callback.bot, callback.from_user.id, callback.message, new_format)
    except Exception as e:
        import traceback
        traceback.print_exc()
        await callback.message.answer(f"❌ Ошибка при сохранении данных: `{e}`", parse_mode="Markdown")


@router.callback_query(UploadState.confirming_upload, F.data == "cancel_upload")
async def cancel_upload(callback: CallbackQuery, state: FSMContext, admin_ids: list):
    if not is_admin(callback.from_user.id, admin_ids):
        await callback.answer()
        return
    await state.clear()
    await callback.message.edit_text("❌ Загрузка отменена. В базу ничего не записано.")
    await callback.answer()


async def _handle_new_format_upload(bot, uploaded_by_id: int, processing, parsed: dict):
    """Загрузка формата производителя: свод (план/факт по ИНН) + листы-месяцы (детализация по FOM ID).
    Использует пакетную вставку (executemany кусками по 500) — на порядки быстрее, чем
    построчные запросы, при этом устойчива к "плохим" строкам (см. _executemany_with_fallback).

    Принимает bot и uploaded_by_id ЯВНО (а не Message), потому что вызывается как из обычного
    message-хендлера, так и из callback после подтверждения — а у callback.message.from_user
    был бы id самого бота, а не админа, если бы мы читали id из message."""
    producer_id = await _ensure_producer(parsed["producer_name"])

    summary_lines = [f"✅ *Данные загружены — {parsed['producer_name']}*\n"]
    total_saved = 0
    total_errors = 0
    last_period_id = None
    last_period_name = None

    for period_data in parsed["periods"]:
        period_name = period_data["period_name"]
        period_id = await get_or_create_period(period_name, uploaded_by_id)
        last_period_id = period_id
        last_period_name = period_name

        rows = period_data["rows"]
        total_rows = len(rows)
        sample_errors: list[str] = []
        errors = 0

        try:
            await processing.edit_text(f"⏳ {period_name}: готовлю {total_rows} строк к пакетной загрузке...")
        except Exception:
            pass

        # 1) Уникальные продукты и аптеки — пакетный upsert одним проходом
        products_map: dict[str, str] = {}
        pharmacies_map: dict[str, tuple] = {}
        for row in rows:
            products_map[row["sku_code"]] = row["product"]
            pharmacies_map[row["fom_id"]] = (
                row["fom_id"], row["inn"], row["pharmacy"], row["org_name"],
                row["region"], "", row["address"]
            )

        product_ids, prod_errors = await batch_upsert_products(producer_id, list(products_map.items()))
        pharmacy_ids, pharm_errors = await batch_upsert_pharmacies(list(pharmacies_map.values()))
        sample_errors.extend(prod_errors[:2])
        sample_errors.extend(pharm_errors[:2])

        try:
            await processing.edit_text(
                f"⏳ {period_name}: {len(product_ids)} продуктов, {len(pharmacy_ids)} точек готово, "
                f"сохраняю остатки и продажи..."
            )
        except Exception:
            pass

        # 2) Строки остатков/продаж, используя полученные id
        stock_rows, sale_rows = [], []
        skipped_missing_id = 0
        for row in rows:
            pid = product_ids.get(row["sku_code"])
            phid = pharmacy_ids.get(row["fom_id"])
            if pid is None or phid is None:
                skipped_missing_id += 1
                continue
            if row["stock_qty"] > 0:
                stock_rows.append((period_id, pid, phid, row["stock_qty"], None))
            if row["sales_qty"] > 0 or row["sales_amount"] > 0 or row["incoming_qty"] > 0:
                sale_rows.append((period_id, pid, phid, row["sales_qty"], row["sales_amount"],
                                   row["incoming_qty"], row["total_bonus"], None))

        stock_saved, stock_errors = await batch_save_stock(stock_rows)
        sale_saved, sale_errors = await batch_save_sale(sale_rows)
        sample_errors.extend(stock_errors[:2])
        sample_errors.extend(sale_errors[:2])

        errors = skipped_missing_id + (len(stock_rows) - stock_saved) + (len(sale_rows) - sale_saved)
        saved = total_rows - skipped_missing_id  # строка "обработана", даже если у неё был только stock ИЛИ только sale

        # 3) Entity-level план/факт (свод по ИНН) — тоже пакетно
        ep_rows = [
            (period_id, producer_id, ep["inn"], ep["org_name"], ep["region"], ep["district"],
             ep["pharmacy_count"], ep["plan_amount"], ep["fact_amount"])
            for ep in period_data["entity_plans"]
        ]
        _, ep_errors = await batch_upsert_entity_plans(ep_rows)
        errors += len(ep_errors)
        sample_errors.extend(ep_errors[:2])

        total_saved += saved
        total_errors += errors
        summary_lines.append(
            f"📅 *{period_name}*: {saved} строк, {len(pharmacy_ids)} точек, "
            f"{len(product_ids)} продуктов, {len(period_data['entity_plans'])} сетей (план/факт)"
        )
        if sample_errors:
            summary_lines.append("   ⚠️ Примеры ошибок:")
            for err in sample_errors[:5]:
                safe_err = err[:200].replace("`", "'")
                summary_lines.append(f"   `{safe_err}`")

    if parsed["warnings"]:
        summary_lines.append("\n⚠️ " + "\n⚠️ ".join(parsed["warnings"]))
    if total_errors:
        summary_lines.append(f"\n❌ Ошибок при сохранении: {total_errors}")

    summary_text = "\n".join(summary_lines)
    try:
        await processing.edit_text(summary_text, parse_mode="Markdown")
    except Exception:
        # Текст ошибки Postgres мог содержать символы, ломающие Markdown — шлём как есть.
        await processing.edit_text(summary_text)

    # Уведомляем и шлём алерты только по последнему (самому свежему) периоду в файле —
    # старые месяцы, идущие в том же файле для истории, не должны спамить пользователей.
    if last_period_id:
        await _notify_users(bot, last_period_name)
        pushed = await _push_alerts(bot, last_period_id)
        if pushed:
            await processing.answer(f"🔔 Алерты разосланы менеджерам {pushed} производителей.")


async def _handle_flat_format_upload(message: Message, processing, parsed: dict, period_name: str):
    """Старый плоский формат — один лист, фиксированные колонки (см. /addproducer -> шаблон)."""
    period_id = await get_or_create_period(period_name, message.from_user.id)

    saved = 0
    errors = 0

    for row in parsed["rows"]:
        try:
            producer_id = await _ensure_producer(row["producer"])
            product_id = await upsert_product(producer_id, row["sku_code"], row["product"])

            # Нет FOM ID в этом формате — считаем, что 1 ИНН = 1 точка (синтетический ключ).
            pharmacy_id = await upsert_pharmacy(
                f"INN-{row['inn']}", row["inn"], row["pharmacy"], "",
                row["region"], row["city"], row["address"]
            )

            if row["stock_qty"] > 0:
                await save_stock(period_id, product_id, pharmacy_id, row["stock_qty"], row["date_on"])

            if row["sales_qty"] > 0 or row["sales_amount"] > 0:
                await save_sale(period_id, product_id, pharmacy_id,
                                row["sales_qty"], row["sales_amount"], row["date_on"])

            if row["plan_amount"] > 0 or row["fact_amount"] > 0:
                await save_plan(period_id, product_id, pharmacy_id,
                                row["plan_qty"], row["plan_amount"],
                                row["fact_qty"], row["fact_amount"], row["bonus"])
            saved += 1

            if saved % 100 == 0:
                try:
                    await processing.edit_text(f"⏳ Сохраняю... ({saved}/{parsed['total_rows']})")
                except Exception:
                    pass

        except Exception as e:
            errors += 1
            print(f"Row error: {e}")

    await processing.edit_text(
        f"✅ *Данные загружены!*\n\n"
        f"📅 Период: *{period_name}*\n"
        f"📊 Строк обработано: *{saved}*\n"
        f"🏭 Производителей: *{parsed['producers']}*\n"
        f"🏪 Аптек: *{parsed['pharmacies']}*\n"
        f"💊 Продуктов: *{parsed['products']}*\n"
        f"❌ Ошибок: *{errors}*\n\n"
        f"Все пользователи получат уведомление.",
        parse_mode="Markdown"
    )

    await _notify_users(message.bot, period_name)
    pushed = await _push_alerts(message.bot, period_id)
    if pushed:
        await message.answer(f"🔔 Алерты разосланы менеджерам {pushed} производителей.")


async def _ensure_producer(name: str) -> int:
    """Get or create producer by name."""
    pool = await get_pool()
    async with pool.acquire() as db:
        row = await db.fetchrow("SELECT id FROM producers WHERE LOWER(name) = LOWER($1)", name)
        if row:
            return row["id"]
        code = name[:10].upper().replace(" ", "_")
        row = await db.fetchrow("""
            INSERT INTO producers (name, code) VALUES ($1, $2)
            ON CONFLICT(code) DO UPDATE SET name=EXCLUDED.name RETURNING id
        """, name, code)
        return row["id"]


async def _notify_users(bot, period_name: str):
    """Send update notification to all active users."""
    pool = await get_pool()
    async with pool.acquire() as db:
        users = await db.fetch(
            "SELECT telegram_id FROM users WHERE is_active=TRUE AND telegram_id IS NOT NULL"
        )
    for user in users:
        try:
            await bot.send_message(
                chat_id=user["telegram_id"],
                text=f"🔄 *Данные обновлены!*\n\n📅 {period_name}\n\nОткройте бота чтобы посмотреть актуальную аналитику.",
                parse_mode="Markdown"
            )
        except Exception:
            pass


async def _push_alerts(bot, period_id: int) -> int:
    """
    For each producer touched by this data upload, compute alerts (дефектура,
    отставание от плана, падение продаж, рост спроса) and, if any are found,
    push a message to that producer's registered managers. Returns the number
    of producers for whom an alert push was sent.
    """
    producer_ids = await get_producer_ids_in_period(period_id)
    pushed = 0
    for producer_id in producer_ids:
        try:
            alerts = await build_alerts(producer_id, period_id)
        except Exception as e:
            print(f"build_alerts error for producer {producer_id}: {e}")
            continue
        if not alerts:
            continue

        text = format_alerts_text(alerts, header="⚠️ *Обнаружены важные изменения по вашим продуктам*")
        users = await get_users_by_producer(producer_id)
        sent_any = False
        for user in users:
            try:
                await bot.send_message(chat_id=user["telegram_id"], text=text, parse_mode="Markdown")
                sent_any = True
            except Exception:
                pass
        if sent_any:
            pushed += 1
    return pushed


# ── Производители и их единственный пользователь (один менеджер = один производитель) ──

@router.message(F.text == "🏭 Производители")
async def show_producers(message: Message, admin_ids: list):
    if not is_admin(message.from_user.id, admin_ids):
        return

    producers = await get_all_producers()
    if not producers:
        await message.answer("Производителей пока нет.\n\n➕ /addproducer — добавить производителя (сразу с пользователем)")
        return

    all_users = await get_all_users()
    users_by_producer = {}
    for u in all_users:
        users_by_producer.setdefault(u["producer_id"], u)  # один производитель — один пользователь

    lines = [f"🏭 *Производители ({len(producers)}):*\n"]
    for p in producers:
        u = users_by_producer.get(p["id"])
        if u:
            status = "✅ зарегистрирован" if u["telegram_id"] else "⏳ ждёт регистрации"
            lines.append(f"• *{p['name']}* (`{p['code']}`)\n   🔑 `{u['access_code']}` — {status}")
        else:
            lines.append(f"• *{p['name']}* (`{p['code']}`)\n   ⚠️ пользователь ещё не создан")
    lines.append(f"\n➕ /addproducer — добавить нового производителя")

    await message.answer("\n".join(lines), parse_mode="Markdown")


@router.message(Command("addproducer"))
async def add_producer_start(message: Message, state: FSMContext, admin_ids: list):
    if not is_admin(message.from_user.id, admin_ids):
        return
    await state.set_state(AddProducerState.waiting_for_name)
    await message.answer("Введите *название производителя*:", parse_mode="Markdown")


@router.message(AddProducerState.waiting_for_name)
async def add_producer_name(message: Message, state: FSMContext, admin_ids: list):
    if not is_admin(message.from_user.id, admin_ids):
        return
    await state.update_data(name=message.text.strip())
    await state.set_state(AddProducerState.waiting_for_code)
    await message.answer(
        "Введите *короткий код* производителя (латиница, без пробелов).\n"
        "Пример: `NOVARTIS`, `PFIZER`, `ASTRAZENECA`",
        parse_mode="Markdown"
    )


@router.message(AddProducerState.waiting_for_code)
async def add_producer_code(message: Message, state: FSMContext, admin_ids: list):
    if not is_admin(message.from_user.id, admin_ids):
        return
    await state.update_data(code=message.text.strip().upper())
    await state.set_state(AddProducerState.waiting_for_access_code)
    await message.answer(
        "Теперь введите *код доступа* — именно его вы передадите представителю "
        "этого производителя, он введёт код при регистрации в боте.\n\n"
        "Пример: `ASTRAZENECA-01`",
        parse_mode="Markdown"
    )


@router.message(AddProducerState.waiting_for_access_code)
async def add_producer_access_code(message: Message, state: FSMContext, admin_ids: list):
    if not is_admin(message.from_user.id, admin_ids):
        return

    data = await state.get_data()
    name = data["name"]
    code = data["code"]
    access_code = message.text.strip()

    try:
        producer_id = await create_producer(name, code)

        # Один производитель — один пользователь: если уже есть, не плодим второго.
        existing_users = await get_users_by_producer(producer_id)
        if existing_users:
            await state.clear()
            u = existing_users[0]
            await message.answer(
                f"ℹ️ У производителя *{name}* уже есть пользователь.\n"
                f"🔑 Код доступа: `{u['access_code']}`\n\n"
                f"Если нужно выдать новый код взамен старого — сначала удалите старого "
                f"пользователя в базе и повторите /addproducer.",
                parse_mode="Markdown"
            )
            return

        await create_user(access_code, producer_id, name)
        await state.clear()
        await message.answer(
            f"✅ *Готово!*\n\n"
            f"🏭 Производитель: {name} (`{code}`)\n"
            f"🔑 Код доступа для входа в бота: `{access_code}`\n\n"
            f"Отправьте этот код представителю производителя — он введёт его при "
            f"регистрации в боте (обычный /start с другого аккаунта).",
            parse_mode="Markdown"
        )
    except Exception as e:
        await state.clear()
        await message.answer(f"❌ Ошибка: {e}\n\nВозможно, такое название или код уже используются.")



# ── Stats ─────────────────────────────────────────────────────────────────────

@router.message(F.text == "📊 Статистика")
async def show_stats(message: Message, admin_ids: list):
    if not is_admin(message.from_user.id, admin_ids):
        return

    pool = await get_pool()
    async with pool.acquire() as db:
        producers_count = await db.fetchval("SELECT COUNT(*) FROM producers")
        users_count     = await db.fetchval("SELECT COUNT(*) FROM users WHERE is_active=TRUE")
        reg_count       = await db.fetchval("SELECT COUNT(*) FROM users WHERE telegram_id IS NOT NULL")
        pharmacies_count= await db.fetchval("SELECT COUNT(*) FROM pharmacies")
        periods_count   = await db.fetchval("SELECT COUNT(*) FROM data_periods")

    period = await get_latest_period()
    period_info = f"📅 {period['period_name']} ({period['updated_at'].strftime('%d.%m.%Y')})" if period else "—"

    await message.answer(
        f"📊 *Статистика платформы*\n\n"
        f"🏭 Производителей: *{producers_count}*\n"
        f"👥 Пользователей: *{users_count}* (из них в боте: *{reg_count}*)\n"
        f"🏪 Аптек в базе: *{pharmacies_count}*\n"
        f"🔄 Обновлений данных: *{periods_count}*\n\n"
        f"Последнее обновление:\n{period_info}",
        parse_mode="Markdown"
    )


# ── Alert settings (пороги алертов, п.3.5 ТЗ — настраиваются, не зашиты в коде) ─

@router.message(F.text == "⚙️ Настройки")
async def alert_settings_start(message: Message, state: FSMContext, admin_ids: list):
    if not is_admin(message.from_user.id, admin_ids):
        return

    producers = await get_all_producers()
    if not producers:
        await message.answer("❌ Сначала добавьте производителя через /addproducer")
        return

    await state.set_state(AlertSettingsState.waiting_for_producer)
    await message.answer(
        "⚙️ *Пороги алертов*\n\nВыберите производителя, для которого настроить пороги:",
        parse_mode="Markdown",
        reply_markup=alert_settings_producer_kb(producers)
    )


@router.callback_query(F.data.startswith("alertset_producer:"))
async def alert_settings_select_producer(callback: CallbackQuery, state: FSMContext, admin_ids: list):
    if not is_admin(callback.from_user.id, admin_ids):
        return

    producer_id = int(callback.data.split(":")[1])
    producer = await get_producer_by_id(producer_id)
    settings = await get_alert_settings(producer_id)

    await state.update_data(producer_id=producer_id)
    await state.set_state(AlertSettingsState.waiting_for_values)

    await callback.message.edit_text(
        f"⚙️ *Пороги алертов — {producer['name']}*\n\n"
        f"Текущие значения:\n"
        f"  📦 Мин. остаток (дефектура): *{settings['min_stock_qty']}* уп.\n"
        f"  📉 Падение продаж от: *{settings['sales_drop_pct']}* %\n"
        f"  📈 Рост спроса от: *{settings['sales_surge_pct']}* %\n"
        f"  🎯 Отставание от плана от: *{settings['plan_lag_pct']}* %\n\n"
        f"Отправьте новые значения одной строкой через запятую в этом порядке:\n"
        f"`мин_остаток, %падения, %роста, %отставания`\n\n"
        f"Например: `20, 30, 50, 20`",
        parse_mode="Markdown"
    )
    await callback.answer()


@router.message(AlertSettingsState.waiting_for_values)
async def alert_settings_apply(message: Message, state: FSMContext, admin_ids: list):
    if not is_admin(message.from_user.id, admin_ids):
        return

    data = await state.get_data()
    producer_id = data.get("producer_id")

    parts = [p.strip() for p in message.text.split(",")]
    if len(parts) != 4:
        await message.answer(
            "❌ Нужно ровно 4 числа через запятую: `мин_остаток, %падения, %роста, %отставания`\n"
            "Например: `20, 30, 50, 20`",
            parse_mode="Markdown"
        )
        return

    try:
        min_stock_qty, drop_pct, surge_pct, lag_pct = (float(p.replace("%", "")) for p in parts)
    except ValueError:
        await message.answer("❌ Не удалось распознать числа. Попробуйте ещё раз, например: `20, 30, 50, 20`", parse_mode="Markdown")
        return

    await update_alert_settings(producer_id, min_stock_qty, drop_pct, surge_pct, lag_pct)
    await state.clear()

    producer = await get_producer_by_id(producer_id)
    await message.answer(
        f"✅ *Пороги алертов обновлены — {producer['name']}*\n\n"
        f"  📦 Мин. остаток: *{min_stock_qty}* уп.\n"
        f"  📉 Падение продаж от: *{drop_pct}* %\n"
        f"  📈 Рост спроса от: *{surge_pct}* %\n"
        f"  🎯 Отставание от плана от: *{lag_pct}* %",
        parse_mode="Markdown"
    )


# ── Архивация старых периодов (п.4/5 ТЗ) — ручная, с подтверждением ────────────

@router.message(Command("cleanup"))
async def cleanup_start(message: Message, admin_ids: list):
    if not is_admin(message.from_user.id, admin_ids):
        return

    old_periods = await get_old_periods(12)
    if not old_periods:
        await message.answer("✅ Периодов старше 12 месяцев нет — архивировать нечего.")
        return

    lines = [f"🗑 *Периодов старше 12 месяцев: {len(old_periods)}*\n"]
    for p in old_periods[:20]:
        lines.append(f"• {p['period_name']} (обновлён {p['updated_at'].strftime('%d.%m.%Y')})")
    if len(old_periods) > 20:
        lines.append(f"...и ещё {len(old_periods) - 20}")
    lines.append(
        "\n⚠️ Это действие *безвозвратно* удалит все продажи, остатки и план/факт "
        "по этим периодам у всех производителей."
    )

    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Удалить безвозвратно", callback_data="cleanup_confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="cleanup_cancel"),
    ]])
    await message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=kb)


@router.callback_query(F.data == "cleanup_confirm")
async def cleanup_confirm(callback: CallbackQuery, admin_ids: list):
    if not is_admin(callback.from_user.id, admin_ids):
        await callback.answer()
        return

    old_periods = await get_old_periods(12)
    if not old_periods:
        await callback.message.edit_text("✅ Периодов старше 12 месяцев уже нет.")
        await callback.answer()
        return

    await callback.message.edit_text(f"⏳ Удаляю {len(old_periods)} периодов...")
    deleted_names = []
    for p in old_periods:
        try:
            await delete_period_cascade(p["id"])
            deleted_names.append(p["period_name"])
        except Exception as e:
            print(f"cleanup error for period {p['id']}: {e}")

    text = f"✅ Удалено периодов: {len(deleted_names)}\n" + "\n".join(f"• {n}" for n in deleted_names[:20])
    if len(deleted_names) > 20:
        text += f"\n...и ещё {len(deleted_names) - 20}"
    await callback.message.edit_text(text)
    await callback.answer()


@router.callback_query(F.data == "cleanup_cancel")
async def cleanup_cancel(callback: CallbackQuery, admin_ids: list):
    if not is_admin(callback.from_user.id, admin_ids):
        await callback.answer()
        return
    await callback.message.edit_text("Отменено — данные не тронуты.")
    await callback.answer()


# ── Cancel ────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "cancel")
async def cancel_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Отменено")


# Fix import
from utils.db import get_producer_by_id
