from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command

from utils.db import (
    get_user_by_tg, get_producer_by_id, get_latest_period,
    get_products_by_producer, get_sales_summary_by_product,
    get_sales_summary_by_region, get_sales_by_producer,
    get_stock_by_producer, get_low_stock,
    get_plan_summary, get_plan_by_product,
    get_bonus_history, get_sales_history,
    get_pharmacies_by_producer, search_pharmacies,
    get_alert_settings, log_action,
    get_entity_plans, get_entity_plan_summary, get_stock_by_entity,
)
from utils.excel import fmt_money, fmt_qty, fmt_pct, make_progress_bar
from keyboards.kb import (
    main_menu_kb, products_kb, product_detail_kb,
    regions_kb, cancel_kb
)

router = Router()


async def get_user_and_period(message_or_callback):
    """Helper to get current user and latest period."""
    if hasattr(message_or_callback, 'from_user'):
        tg_id = message_or_callback.from_user.id
    else:
        tg_id = message_or_callback.from_user.id

    user = await get_user_by_tg(tg_id)
    if not user:
        return None, None, None

    period = await get_latest_period()
    producer = await get_producer_by_id(user["producer_id"])
    return user, period, producer


# ── Главное меню ──────────────────────────────────────────────────────────────

@router.message(F.text == "ℹ️ Инфо")
async def show_info(message: Message):
    user, period, producer = await get_user_and_period(message)
    if not user:
        await message.answer("❌ Вы не авторизованы. Напишите /start")
        return

    period_info = f"📅 *{period['period_name']}*\n🕐 {period['updated_at'].strftime('%d.%m.%Y %H:%M')}" if period else "Данных пока нет"
    await message.answer(
        f"ℹ️ *Информация*\n\n"
        f"🏭 Производитель: *{producer['name']}*\n"
        f"👤 {user['name'] or 'Менеджер'}\n"
        f"📞 {user['phone'] or '—'}\n\n"
        f"Последнее обновление данных:\n{period_info}",
        parse_mode="Markdown"
    )
    await log_action(user["id"], "info")


# ── Мои продукты ──────────────────────────────────────────────────────────────

@router.message(F.text == "📊 Мои продукты")
async def show_products(message: Message):
    user, period, producer = await get_user_and_period(message)
    if not user:
        await message.answer("❌ Вы не авторизованы. Напишите /start")
        return

    products = await get_products_by_producer(user["producer_id"])
    if not products:
        await message.answer("📭 Продукты не найдены. Обратитесь к менеджеру DATFO.")
        return

    await log_action(user["id"], "view_products")
    await message.answer(
        f"💊 *Ваши продукты* ({len(products)} позиций)\n\nВыберите продукт для детализации:",
        parse_mode="Markdown",
        reply_markup=products_kb(products)
    )


@router.callback_query(F.data.startswith("product:"))
async def show_product_detail(callback: CallbackQuery):
    product_id = int(callback.data.split(":")[1])
    user = await get_user_by_tg(callback.from_user.id)
    if not user:
        await callback.answer("Не авторизован", show_alert=True)
        return

    period = await get_latest_period()
    if not period:
        await callback.answer("Данных пока нет", show_alert=True)
        return

    # Get sales for this product
    from utils.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as db:
        product = await db.fetchrow("SELECT * FROM products WHERE id=$1", product_id)
        if not product or product["producer_id"] != user["producer_id"]:
            await callback.answer("Доступ запрещён", show_alert=True)
            return

        sales = await db.fetchrow("""
            SELECT SUM(s.qty) as qty, SUM(s.amount) as amount,
                   COUNT(DISTINCT s.pharmacy_id) as pharmacies
            FROM sales s WHERE s.product_id=$1 AND s.period_id=$2
        """, product_id, period["id"])

        stock = await db.fetchrow("""
            SELECT SUM(st.qty) as qty FROM stock st
            WHERE st.product_id=$1 AND st.period_id=$2
        """, product_id, period["id"])

        plan = await db.fetchrow("""
            SELECT SUM(pl.plan_amount) as plan, SUM(pl.fact_amount) as fact, SUM(pl.bonus) as bonus
            FROM plans pl WHERE pl.product_id=$1 AND pl.period_id=$2
        """, product_id, period["id"])

    sales_qty = sales["qty"] or 0
    sales_amt = sales["amount"] or 0
    pharmacies = sales["pharmacies"] or 0
    stock_qty = stock["qty"] or 0 if stock else 0
    plan_amt = plan["plan"] or 0 if plan else 0
    fact_amt = plan["fact"] or 0 if plan else 0
    bonus = plan["bonus"] or 0 if plan else 0

    text = (
        f"💊 *{product['name']}*\n"
        f"📅 {period['period_name']}\n\n"
        f"📈 *Продажи:*\n"
        f"  • Штук: {fmt_qty(sales_qty)}\n"
        f"  • Сумма: {fmt_money(sales_amt)}\n"
        f"  • Аптек: {pharmacies}\n\n"
        f"📦 *Остаток:* {fmt_qty(stock_qty)} уп.\n\n"
        f"🎯 *Выполнение плана:*\n"
        f"  {make_progress_bar(fact_amt, plan_amt)}\n"
        f"  План: {fmt_money(plan_amt)}\n"
        f"  Факт: {fmt_money(fact_amt)}\n\n"
        f"💰 *Бонус:* {fmt_money(bonus)}"
    )

    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=product_detail_kb(product_id))
    await callback.answer()
    await log_action(user["id"], "view_product", str(product_id))


async def _check_product_access(callback: CallbackQuery, product_id: int):
    """Проверяет авторизацию и что продукт принадлежит производителю пользователя.
    Возвращает (user, period, product) или (None, None, None) если доступ запрещён."""
    user = await get_user_by_tg(callback.from_user.id)
    if not user:
        await callback.answer("Не авторизован", show_alert=True)
        return None, None, None

    period = await get_latest_period()
    if not period:
        await callback.answer("Данных пока нет", show_alert=True)
        return None, None, None

    from utils.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as db:
        product = await db.fetchrow("SELECT * FROM products WHERE id=$1", product_id)

    if not product or product["producer_id"] != user["producer_id"]:
        await callback.answer("Доступ запрещён", show_alert=True)
        return None, None, None

    return user, period, product


def _back_to_product_kb(product_id: int):
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад к продукту", callback_data=f"product:{product_id}")]
    ])


@router.callback_query(F.data.startswith("prod_sales:"))
async def show_product_sales_detail(callback: CallbackQuery):
    product_id = int(callback.data.split(":")[1])
    user, period, product = await _check_product_access(callback, product_id)
    if not user:
        return

    from utils.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as db:
        rows = await db.fetch("""
            SELECT s.qty, s.amount, ph.name as pharmacy_name, ph.region
            FROM sales s
            JOIN pharmacies ph ON ph.id = s.pharmacy_id
            WHERE s.product_id=$1 AND s.period_id=$2 AND (s.qty > 0 OR s.amount > 0)
            ORDER BY s.amount DESC
        """, product_id, period["id"])

    if not rows:
        await callback.message.edit_text(
            f"📈 *Продажи — {product['name']}*\n📅 {period['period_name']}\n\nПродаж за этот период нет.",
            parse_mode="Markdown", reply_markup=_back_to_product_kb(product_id)
        )
        await callback.answer()
        return

    lines = [f"📈 *Продажи — {product['name']}*", f"📅 {period['period_name']}\n"]
    for r in rows[:20]:
        lines.append(f"• {r['pharmacy_name']} ({r['region'] or '—'}): {fmt_qty(r['qty'])} уп. / {fmt_money(r['amount'])}")
    if len(rows) > 20:
        lines.append(f"\n_...и ещё {len(rows) - 20} аптек — полный список в 📄 Отчёты_")

    await callback.message.edit_text("\n".join(lines), parse_mode="Markdown", reply_markup=_back_to_product_kb(product_id))
    await callback.answer()
    await log_action(user["id"], "view_product_sales", str(product_id))


@router.callback_query(F.data.startswith("prod_stock:"))
async def show_product_stock_detail(callback: CallbackQuery):
    product_id = int(callback.data.split(":")[1])
    user, period, product = await _check_product_access(callback, product_id)
    if not user:
        return

    from utils.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as db:
        rows = await db.fetch("""
            SELECT st.qty, ph.name as pharmacy_name, ph.region
            FROM stock st
            JOIN pharmacies ph ON ph.id = st.pharmacy_id
            WHERE st.product_id=$1 AND st.period_id=$2 AND st.qty > 0
            ORDER BY st.qty DESC
        """, product_id, period["id"])

    if not rows:
        await callback.message.edit_text(
            f"📉 *Остатки — {product['name']}*\n📅 {period['period_name']}\n\nОстатков за этот период нет.",
            parse_mode="Markdown", reply_markup=_back_to_product_kb(product_id)
        )
        await callback.answer()
        return

    lines = [f"📉 *Остатки — {product['name']}*", f"📅 {period['period_name']}\n"]
    for r in rows[:20]:
        lines.append(f"• {r['pharmacy_name']} ({r['region'] or '—'}): {fmt_qty(r['qty'])} уп.")
    if len(rows) > 20:
        lines.append(f"\n_...и ещё {len(rows) - 20} аптек — полный список в 📄 Отчёты_")

    await callback.message.edit_text("\n".join(lines), parse_mode="Markdown", reply_markup=_back_to_product_kb(product_id))
    await callback.answer()
    await log_action(user["id"], "view_product_stock", str(product_id))


@router.callback_query(F.data.startswith("prod_bonus:"))
async def show_product_bonus_detail(callback: CallbackQuery):
    product_id = int(callback.data.split(":")[1])
    user, period, product = await _check_product_access(callback, product_id)
    if not user:
        return

    from utils.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as db:
        rows = await db.fetch("""
            SELECT pl.plan_amount, pl.fact_amount, pl.bonus, ph.name as pharmacy_name, ph.region
            FROM plans pl
            JOIN pharmacies ph ON ph.id = pl.pharmacy_id
            WHERE pl.product_id=$1 AND pl.period_id=$2 AND (pl.plan_amount > 0 OR pl.fact_amount > 0)
            ORDER BY pl.fact_amount DESC
        """, product_id, period["id"])

    if not rows:
        await callback.message.edit_text(
            f"💰 *Бонус — {product['name']}*\n📅 {period['period_name']}\n\n"
            f"Детализация плана по аптекам для этого продукта не загружена.\n"
            f"Если у вашего производителя план/факт по сетям (ИНН) — смотрите раздел «🏢 Сети (ИНН)».",
            parse_mode="Markdown", reply_markup=_back_to_product_kb(product_id)
        )
        await callback.answer()
        return

    lines = [f"💰 *Бонус — {product['name']}*", f"📅 {period['period_name']}\n"]
    for r in rows[:20]:
        pct = round((r["fact_amount"] or 0) / r["plan_amount"] * 100, 1) if r["plan_amount"] else 0
        lines.append(
            f"• {r['pharmacy_name']} ({r['region'] or '—'}): план {fmt_money(r['plan_amount'])}, "
            f"факт {fmt_money(r['fact_amount'])} ({pct}%), бонус {fmt_money(r['bonus'])}"
        )
    if len(rows) > 20:
        lines.append(f"\n_...и ещё {len(rows) - 20} аптек — полный список в 📄 Отчёты_")

    await callback.message.edit_text("\n".join(lines), parse_mode="Markdown", reply_markup=_back_to_product_kb(product_id))
    await callback.answer()
    await log_action(user["id"], "view_product_bonus", str(product_id))


@router.callback_query(F.data.startswith("prod_pharma:"))
async def show_product_pharmacies_detail(callback: CallbackQuery):
    product_id = int(callback.data.split(":")[1])
    user, period, product = await _check_product_access(callback, product_id)
    if not user:
        return

    from utils.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as db:
        rows = await db.fetch("""
            SELECT DISTINCT ph.name as pharmacy_name, ph.region, ph.city, ph.address
            FROM pharmacies ph
            WHERE ph.id IN (
                SELECT pharmacy_id FROM stock WHERE product_id=$1 AND period_id=$2 AND qty > 0
                UNION
                SELECT pharmacy_id FROM sales WHERE product_id=$1 AND period_id=$2 AND (qty > 0 OR amount > 0)
            )
            ORDER BY ph.region, ph.name
        """, product_id, period["id"])

    if not rows:
        await callback.message.edit_text(
            f"🏪 *Аптеки — {product['name']}*\n📅 {period['period_name']}\n\n"
            f"Аптек с этим продуктом за период не найдено.",
            parse_mode="Markdown", reply_markup=_back_to_product_kb(product_id)
        )
        await callback.answer()
        return

    lines = [f"🏪 *Аптеки с этим продуктом — {product['name']}*", f"📅 {period['period_name']} ({len(rows)} аптек)\n"]
    for r in rows[:25]:
        lines.append(f"• {r['pharmacy_name']} ({r['region'] or '—'}, {r['city'] or ''})")
    if len(rows) > 25:
        lines.append(f"\n_...и ещё {len(rows) - 25} аптек — полный список в 📄 Отчёты_")

    await callback.message.edit_text("\n".join(lines), parse_mode="Markdown", reply_markup=_back_to_product_kb(product_id))
    await callback.answer()
    await log_action(user["id"], "view_product_pharmacies", str(product_id))


# ── Продажи ───────────────────────────────────────────────────────────────────

@router.message(F.text == "📈 Продажи")
async def show_sales(message: Message):
    user, period, producer = await get_user_and_period(message)
    if not user:
        await message.answer("❌ Вы не авторизованы.")
        return
    if not period:
        await message.answer("📭 Данных пока нет.")
        return

    by_product = await get_sales_summary_by_product(user["producer_id"], period["id"])
    by_region  = await get_sales_summary_by_region(user["producer_id"], period["id"])

    if not by_product:
        await message.answer("📭 Данных по продажам нет.")
        return

    total_amount = sum(r["total_amount"] or 0 for r in by_product)
    total_qty    = sum(r["total_qty"] or 0 for r in by_product)

    lines = [
        f"📈 *Продажи* — {period['period_name']}\n",
        f"💰 Итого: *{fmt_money(total_amount)}*",
        f"📦 Штук: *{fmt_qty(total_qty)}*\n",
        "🔹 *По продуктам:*"
    ]
    for r in by_product[:10]:
        lines.append(f"  • {r['product_name']}: {fmt_money(r['total_amount'])} ({fmt_qty(r['total_qty'])} уп.)")

    if by_region:
        lines.append("\n🔹 *По регионам:*")
        for r in by_region[:8]:
            region = r["region"] or "Не указан"
            lines.append(f"  • {region}: {fmt_money(r['total_amount'])} ({r['pharmacy_count']} аптек)")

    await message.answer("\n".join(lines), parse_mode="Markdown")
    await log_action(user["id"], "view_sales")


# ── Остатки ───────────────────────────────────────────────────────────────────

@router.message(F.text == "📉 Остатки")
async def show_stock(message: Message):
    user, period, producer = await get_user_and_period(message)
    if not user:
        await message.answer("❌ Вы не авторизованы.")
        return
    if not period:
        await message.answer("📭 Данных пока нет.")
        return

    settings = await get_alert_settings(user["producer_id"])
    min_qty = settings["min_stock_qty"] if settings else 0

    stock = await get_stock_by_producer(user["producer_id"], period["id"])
    low   = await get_low_stock(user["producer_id"], period["id"], min_qty)

    if not stock:
        await message.answer("📭 Данных по остаткам нет.")
        return

    # Group by product
    by_product = {}
    for row in stock:
        name = row["product_name"]
        if name not in by_product:
            by_product[name] = {"qty": 0, "pharmacies": 0}
        by_product[name]["qty"] += row["qty"] or 0
        by_product[name]["pharmacies"] += 1

    lines = [f"📉 *Остатки* — {period['period_name']}\n"]
    for name, data in sorted(by_product.items(), key=lambda x: -x[1]["qty"])[:10]:
        lines.append(f"  • {name}: *{fmt_qty(data['qty'])}* уп. ({data['pharmacies']} аптек)")

    if low:
        lines.append(f"\n⚠️ *Дефектура* (ниже {fmt_qty(min_qty)} уп.):")
        for row in low[:5]:
            lines.append(f"  🔴 {row['product_name']} — {row['pharmacy_name']}: {fmt_qty(row['qty'])} уп.")
        if len(low) > 5:
            lines.append(f"  _...и ещё {len(low)-5} позиций_")

    await message.answer("\n".join(lines), parse_mode="Markdown")
    await log_action(user["id"], "view_stock")


# ── Бонусы и план ─────────────────────────────────────────────────────────────

@router.message(F.text == "💰 Бонусы и план")
async def show_bonuses(message: Message):
    user, period, producer = await get_user_and_period(message)
    if not user:
        await message.answer("❌ Вы не авторизованы.")
        return
    if not period:
        await message.answer("📭 Данных пока нет.")
        return

    summary   = await get_plan_summary(user["producer_id"], period["id"])
    by_product = await get_plan_by_product(user["producer_id"], period["id"])
    history   = await get_bonus_history(user["producer_id"])

    if not summary or not summary["total_plan"]:
        await message.answer("📭 Данных по плану нет.")
        return

    total_plan  = summary["total_plan"] or 0
    total_fact  = summary["total_fact"] or 0
    total_bonus = summary["total_bonus"] or 0

    lines = [
        f"💰 *Бонусы и выполнение плана*\n",
        f"📅 {period['period_name']}\n",
        f"🎯 *Выполнение:*",
        f"{make_progress_bar(total_fact, total_plan)}",
        f"  План: {fmt_money(total_plan)}",
        f"  Факт: {fmt_money(total_fact)}",
        f"\n💰 *Заработанный бонус: {fmt_money(total_bonus)}*\n",
    ]

    if by_product:
        lines.append("🔹 *По продуктам:*")
        for r in by_product[:8]:
            pct = fmt_pct(r["fact_amount"] or 0, r["plan_amount"] or 0)
            lines.append(f"  • {r['product_name']}: {pct} — бонус {fmt_money(r['bonus'] or 0)}")

    if history and len(history) > 1:
        lines.append("\n📊 *История бонусов:*")
        for h in history[:5]:
            lines.append(f"  • {h['period_name']}: {fmt_money(h['total_bonus'] or 0)}")

    await message.answer("\n".join(lines), parse_mode="Markdown")
    await log_action(user["id"], "view_bonuses")


# ── Мои аптеки ────────────────────────────────────────────────────────────────

@router.message(F.text == "🏪 Мои аптеки")
async def show_pharmacies(message: Message):
    user, period, producer = await get_user_and_period(message)
    if not user:
        await message.answer("❌ Вы не авторизованы.")
        return
    if not period:
        await message.answer("📭 Данных пока нет.")
        return

    pharmacies = await get_pharmacies_by_producer(user["producer_id"], period["id"])
    if not pharmacies:
        await message.answer("📭 Аптек не найдено.")
        return

    # Регионы с количеством точек — для фильтра
    region_counts = {}
    for ph in pharmacies:
        region = ph["region"] or "Без региона"
        region_counts[region] = region_counts.get(region, 0) + 1
    regions = [{"region": r} for r in sorted(region_counts.keys())]

    lines = [f"🏪 *Ваши аптеки* ({len(pharmacies)} точек)\n", "Выберите регион для списка аптек:"]
    for region, count in sorted(region_counts.items()):
        lines.append(f"  📍 {region}: {count}")
    lines.append("\n🔍 Для поиска аптеки по названию используйте /search")

    await message.answer("\n".join(lines), parse_mode="Markdown", reply_markup=regions_kb(regions, prefix="pharma_region"))
    await log_action(user["id"], "view_pharmacies")


@router.callback_query(F.data.startswith("pharma_region:"))
async def show_pharmacies_by_region(callback: CallbackQuery):
    region_filter = callback.data.split(":", 1)[1]

    user = await get_user_by_tg(callback.from_user.id)
    if not user:
        await callback.answer("Не авторизован", show_alert=True)
        return
    period = await get_latest_period()
    if not period:
        await callback.answer("Данных пока нет", show_alert=True)
        return

    pharmacies = await get_pharmacies_by_producer(user["producer_id"], period["id"])
    if region_filter != "all":
        pharmacies = [ph for ph in pharmacies if (ph["region"] or "Без региона") == region_filter]

    title = "🏪 *Все аптеки*" if region_filter == "all" else f"🏪 *Аптеки — {region_filter}*"
    lines = [title, f"Найдено: {len(pharmacies)}\n"]
    for ph in pharmacies[:30]:
        city = f", {ph['city']}" if ph["city"] else ""
        lines.append(f"• {ph['name']}{city}")
    if len(pharmacies) > 30:
        lines.append(f"\n_...и ещё {len(pharmacies) - 30} — полный список в 📄 Отчёты_")

    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ К списку регионов", callback_data="back:pharma_regions")]
    ])

    await callback.message.edit_text("\n".join(lines), parse_mode="Markdown", reply_markup=back_kb)
    await callback.answer()
    await log_action(user["id"], "view_pharmacies_filtered", region_filter)


@router.callback_query(F.data == "back:pharma_regions")
async def back_to_pharma_regions(callback: CallbackQuery):
    user = await get_user_by_tg(callback.from_user.id)
    if not user:
        await callback.answer("Не авторизован", show_alert=True)
        return
    period = await get_latest_period()
    if not period:
        await callback.answer("Данных пока нет", show_alert=True)
        return

    pharmacies = await get_pharmacies_by_producer(user["producer_id"], period["id"])
    region_counts = {}
    for ph in pharmacies:
        region = ph["region"] or "Без региона"
        region_counts[region] = region_counts.get(region, 0) + 1
    regions = [{"region": r} for r in sorted(region_counts.keys())]

    lines = [f"🏪 *Ваши аптеки* ({len(pharmacies)} точек)\n", "Выберите регион для списка аптек:"]
    for region, count in sorted(region_counts.items()):
        lines.append(f"  📍 {region}: {count}")

    await callback.message.edit_text("\n".join(lines), parse_mode="Markdown", reply_markup=regions_kb(regions, prefix="pharma_region"))
    await callback.answer()


# ── Сети (ИНН) — сводная картина по всем юрлицам-сетям, закупающим товар ──────

@router.message(F.text == "🏢 Сети (ИНН)")
async def show_entities(message: Message):
    user, period, producer = await get_user_and_period(message)
    if not user:
        await message.answer("❌ Вы не авторизованы.")
        return
    if not period:
        await message.answer("📭 Данных пока нет.")
        return

    entities = await get_entity_plans(user["producer_id"], period["id"])
    if not entities:
        await message.answer(
            "📭 Данных по сетям/юрлицам за этот период нет.\n"
            "Возможно, для этого периода загружена только детализация по точкам, без сводки план/факт."
        )
        return

    summary = await get_entity_plan_summary(user["producer_id"], period["id"])
    stock_rows = await get_stock_by_entity(user["producer_id"], period["id"])
    stock_by_inn = {r["inn"]: r["stock_qty"] or 0 for r in stock_rows}

    total_plan = summary["total_plan"] or 0
    total_fact = summary["total_fact"] or 0

    lines = [
        f"🏢 *Сети/юрлица — {producer['name'] if producer else ''}*",
        f"📅 {period['period_name']}\n",
        f"Всего сетей: *{summary['entity_count']}*, аптечных точек: *{summary['total_pharmacies']}*",
        f"{make_progress_bar(total_fact, total_plan)}",
        f"  План: {fmt_money(total_plan)}  Факт: {fmt_money(total_fact)}\n",
        "🔝 *Топ-15 по объёму закупки:*",
    ]
    for e in entities[:15]:
        stock = stock_by_inn.get(e["inn"], 0)
        lines.append(
            f"  • {e['org_name'] or e['inn']} ({e['region'] or '—'})\n"
            f"     точек: {e['pharmacy_count']} | план {fmt_money(e['plan_amount'])} | "
            f"факт {fmt_money(e['fact_amount'])} | {e['pct']}% | остаток {fmt_qty(stock)} уп."
        )
    if len(entities) > 15:
        lines.append(f"\n_...и ещё {len(entities) - 15} сетей — полный список в 📄 Отчёты_")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n\n_...список сокращён_"

    await message.answer(text, parse_mode="Markdown")
    await log_action(user["id"], "view_entities")


@router.message(Command("search"))
async def search_pharmacy(message: Message):
    user, period, producer = await get_user_and_period(message)
    if not user:
        await message.answer("❌ Вы не авторизованы.")
        return

    query = message.text.replace("/search", "").strip()
    if not query:
        await message.answer("Введите: `/search название аптеки`", parse_mode="Markdown")
        return

    if not period:
        await message.answer("📭 Данных пока нет.")
        return

    results = await search_pharmacies(user["producer_id"], period["id"], query)
    if not results:
        await message.answer(f"❌ По запросу «{query}» ничего не найдено.")
        return

    lines = [f"🔍 Результаты поиска «{query}»:\n"]
    for ph in results:
        lines.append(f"🏪 *{ph['name']}*")
        if ph["region"]:
            lines.append(f"   📍 {ph['region']}, {ph['city'] or ''}")
        if ph["address"]:
            lines.append(f"   🏠 {ph['address']}")
        lines.append("")

    await message.answer("\n".join(lines), parse_mode="Markdown")
    await log_action(user["id"], "search_pharmacy", query)


# ── Отчёты (Excel/PDF) ───────────────────────────────────────────────────────

@router.message(F.text == "📄 Отчёты")
async def reports_menu(message: Message):
    user, period, producer = await get_user_and_period(message)
    if not user:
        await message.answer("❌ Вы не авторизованы.")
        return
    if not period:
        await message.answer("📭 Данных пока нет.")
        return

    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📊 Excel — вся детализация", callback_data="report:excel"),
        InlineKeyboardButton(text="📑 PDF — краткая сводка", callback_data="report:pdf"),
    ]])
    await message.answer(
        f"📄 *Отчёт за {period['period_name']}*\n\n"
        f"📊 *Excel* — полная детализация по остаткам, продажам, плану и сетям (для анализа вне бота)\n"
        f"📑 *PDF* — сводка на 1 страницу (план/факт/бонус, удобно распечатать или переслать)",
        parse_mode="Markdown", reply_markup=kb
    )


@router.callback_query(F.data == "report:excel")
async def report_excel_cb(callback: CallbackQuery):
    await callback.answer()
    await _export_excel_impl(callback, callback.message)


@router.callback_query(F.data == "report:pdf")
async def report_pdf_cb(callback: CallbackQuery):
    await callback.answer()
    await _export_pdf_impl(callback, callback.message)


async def _export_excel_impl(identity, sender):
    user, period, producer = await get_user_and_period(identity)
    if not user:
        await sender.answer("❌ Вы не авторизованы.")
        return
    if not period:
        await sender.answer("📭 Данных пока нет.")
        return

    from aiogram.types import BufferedInputFile
    from utils.export import build_producer_report_xlsx

    wait_msg = await sender.answer("⏳ Формирую отчёт...")

    try:
        buf = await build_producer_report_xlsx(
            user["producer_id"], producer["name"] if producer else "Отчёт",
            period["id"], period["period_name"]
        )
    except Exception as e:
        await wait_msg.edit_text(f"❌ Не удалось сформировать отчёт: {e}")
        return

    safe_name = (producer["name"] if producer else "report").replace(" ", "_")
    filename = f"DATFO_{safe_name}_{period['period_name']}.xlsx".replace("/", "-")

    await sender.answer_document(
        BufferedInputFile(buf.read(), filename=filename),
        caption=f"📄 Детальный отчёт — {producer['name'] if producer else ''}\n📅 {period['period_name']}"
    )
    await wait_msg.delete()
    await log_action(user["id"], "export_excel")


async def _export_pdf_impl(identity, sender):
    user, period, producer = await get_user_and_period(identity)
    if not user:
        await sender.answer("❌ Вы не авторизованы.")
        return
    if not period:
        await sender.answer("📭 Данных пока нет.")
        return

    from aiogram.types import BufferedInputFile
    from utils.pdf_export import build_bonus_pdf

    wait_msg = await sender.answer("⏳ Формирую PDF...")

    try:
        summary = await get_plan_summary(user["producer_id"], period["id"])
        by_product = await get_plan_by_product(user["producer_id"], period["id"])
        entities = await get_entity_plans(user["producer_id"], period["id"])

        buf = await build_bonus_pdf(
            producer["name"] if producer else "Отчёт", period["period_name"],
            summary, by_product, entities
        )
    except Exception as e:
        await wait_msg.edit_text(f"❌ Не удалось сформировать PDF: {e}")
        return

    safe_name = (producer["name"] if producer else "report").replace(" ", "_")
    filename = f"DATFO_{safe_name}_{period['period_name']}.pdf".replace("/", "-")

    await sender.answer_document(
        BufferedInputFile(buf.read(), filename=filename),
        caption=f"📑 Сводный отчёт — {producer['name'] if producer else ''}\n📅 {period['period_name']}"
    )
    await wait_msg.delete()
    await log_action(user["id"], "export_pdf")


# ── Алерты ────────────────────────────────────────────────────────────────────

@router.message(F.text == "🔔 Алерты")
async def show_alerts(message: Message):
    user, period, producer = await get_user_and_period(message)
    if not user:
        await message.answer("❌ Вы не авторизованы.")
        return
    if not period:
        await message.answer("📭 Данных пока нет.")
        return

    from utils.alerts import build_alerts, format_alerts_text

    alerts = await build_alerts(user["producer_id"], period["id"])
    await message.answer(format_alerts_text(alerts), parse_mode="Markdown")
    await log_action(user["id"], "view_alerts")


# ── История ───────────────────────────────────────────────────────────────────

@router.message(F.text == "📅 История")
async def show_history(message: Message):
    user, period, producer = await get_user_and_period(message)
    if not user:
        await message.answer("❌ Вы не авторизованы.")
        return

    history = await get_sales_history(user["producer_id"])
    bonus_h  = await get_bonus_history(user["producer_id"])

    if not history:
        await message.answer("📭 История данных пока недоступна.")
        return

    lines = ["📅 *История по периодам*\n"]
    for i, h in enumerate(history):
        lines.append(f"*{h['period_name']}*")
        lines.append(f"  💰 Продажи: {fmt_money(h['total_amount'] or 0)}")
        lines.append(f"  📦 Штук: {fmt_qty(h['total_qty'] or 0)}")
        if bonus_h and i < len(bonus_h):
            lines.append(f"  🎁 Бонус: {fmt_money(bonus_h[i]['total_bonus'] or 0)}")
        lines.append("")

    await message.answer("\n".join(lines), parse_mode="Markdown")
    await log_action(user["id"], "view_history")

    # График динамики (п.3.2 ТЗ) — строим только если есть хотя бы 2 периода для сравнения
    if len(history) >= 2:
        from aiogram.types import BufferedInputFile
        from utils.charts import build_sales_trend_chart, build_plan_vs_fact_chart

        history_asc = list(reversed(history))  # старые периоды слева, новые справа
        producer_name = producer["name"] if producer else ""

        try:
            trend_buf = build_sales_trend_chart(history_asc, f"Динамика продаж — {producer_name}")
            await message.answer_photo(
                BufferedInputFile(trend_buf.read(), filename="sales_trend.png"),
                caption="📈 Динамика продаж по периодам"
            )
        except Exception as e:
            print(f"chart error (trend): {e}")

        bonus_h_asc = list(reversed(bonus_h)) if bonus_h else []
        if len(bonus_h_asc) >= 2 and any(h["total_plan"] for h in bonus_h_asc):
            try:
                pf_buf = build_plan_vs_fact_chart(bonus_h_asc, f"План vs Факт — {producer_name}")
                await message.answer_photo(
                    BufferedInputFile(pf_buf.read(), filename="plan_vs_fact.png"),
                    caption="🎯 План и факт по периодам"
                )
            except Exception as e:
                print(f"chart error (plan_vs_fact): {e}")


# ── Callbacks ─────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "back:main")
async def back_to_main(callback: CallbackQuery):
    await callback.message.delete()
    await callback.answer()


@router.callback_query(F.data == "back:products")
async def back_to_products(callback: CallbackQuery):
    user = await get_user_by_tg(callback.from_user.id)
    if not user:
        await callback.answer("Не авторизован", show_alert=True)
        return
    products = await get_products_by_producer(user["producer_id"])
    await callback.message.edit_text(
        f"💊 *Ваши продукты* ({len(products)} позиций)\n\nВыберите продукт:",
        parse_mode="Markdown",
        reply_markup=products_kb(products)
    )
    await callback.answer()
