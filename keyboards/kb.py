from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Мои продукты"),  KeyboardButton(text="📈 Продажи")],
            [KeyboardButton(text="📉 Остатки"),        KeyboardButton(text="💰 Бонусы и план")],
            [KeyboardButton(text="🏪 Мои аптеки"),     KeyboardButton(text="🏢 Сети (ИНН)")],
            [KeyboardButton(text="🔔 Алерты"),         KeyboardButton(text="📅 История")],
            [KeyboardButton(text="📄 Отчёты"),         KeyboardButton(text="ℹ️ Инфо")],
        ],
        resize_keyboard=True
    )


def admin_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📤 Загрузить данные")],
            [KeyboardButton(text="🏭 Производители"), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="⚙️ Настройки")],
            [KeyboardButton(text="🔙 Выйти из админки")],
        ],
        resize_keyboard=True
    )


def share_phone_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📞 Поделиться номером", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )


def remove_kb() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def products_kb(products: list) -> InlineKeyboardMarkup:
    buttons = []
    for p in products:
        buttons.append([InlineKeyboardButton(
            text=f"💊 {p['name']}",
            callback_data=f"product:{p['id']}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def product_detail_kb(product_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📈 Продажи", callback_data=f"prod_sales:{product_id}"),
            InlineKeyboardButton(text="📉 Остатки", callback_data=f"prod_stock:{product_id}"),
        ],
        [
            InlineKeyboardButton(text="💰 Бонус", callback_data=f"prod_bonus:{product_id}"),
            InlineKeyboardButton(text="🏪 Аптеки", callback_data=f"prod_pharma:{product_id}"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back:products")],
    ])


def regions_kb(regions: list, prefix: str = "region") -> InlineKeyboardMarkup:
    buttons = []
    for r in regions:
        name = r["region"] or "Без региона"
        buttons.append([InlineKeyboardButton(
            text=f"📍 {name}",
            callback_data=f"{prefix}:{name}"
        )])
    buttons.append([InlineKeyboardButton(text="🌍 Все регионы", callback_data=f"{prefix}:all")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def confirm_upload_kb(period_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_upload:{period_id}"),
        InlineKeyboardButton(text="❌ Отмена",      callback_data="cancel_upload"),
    ]])


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")
    ]])


def alert_settings_producer_kb(producers: list) -> InlineKeyboardMarkup:
    buttons = []
    for p in producers:
        buttons.append([InlineKeyboardButton(
            text=f"⚙️ {p['name']}",
            callback_data=f"alertset_producer:{p['id']}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)
