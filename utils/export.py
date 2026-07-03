"""
Excel export for DATFO bot — п.3.2 ТЗ: "Детальный отчёт (выгрузка в Excel/PDF)
по запросу — для углублённого анализа вне бота."

Builds a multi-sheet workbook (Остатки / Продажи / План и бонусы) scoped to
one producer's own data for a given period — never mixes in other brands'
figures (confidentiality requirement, п.5 ТЗ).
"""

from io import BytesIO
import xlsxwriter

from utils.db import (
    get_stock_by_producer, get_sales_by_producer, get_plans_by_producer,
    get_entity_plans, get_stock_by_entity,
)


async def build_producer_report_xlsx(producer_id: int, producer_name: str, period_id: int, period_name: str) -> BytesIO:
    buf = BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})

    header_fmt = wb.add_format({"bold": True, "bg_color": "#1F6FEB", "font_color": "white", "border": 1})
    money_fmt  = wb.add_format({"num_format": "#,##0"})
    title_fmt  = wb.add_format({"bold": True, "font_size": 13})

    # ── Остатки ──────────────────────────────────────────────────────────────
    stock = await get_stock_by_producer(producer_id, period_id)
    ws = wb.add_worksheet("Остатки")
    ws.write(0, 0, f"{producer_name} — Остатки — {period_name}", title_fmt)
    headers = ["Продукт", "SKU", "Аптека", "Регион", "Город", "Остаток, уп."]
    for c, h in enumerate(headers):
        ws.write(2, c, h, header_fmt)
    for r, row in enumerate(stock, start=3):
        ws.write(r, 0, row["product_name"])
        ws.write(r, 1, row["sku_code"])
        ws.write(r, 2, row["pharmacy_name"])
        ws.write(r, 3, row["region"] or "")
        ws.write(r, 4, row["city"] or "")
        ws.write(r, 5, row["qty"] or 0, money_fmt)
    for c, w in enumerate([28, 12, 30, 16, 16, 14]):
        ws.set_column(c, c, w)

    # ── Продажи ──────────────────────────────────────────────────────────────
    sales = await get_sales_by_producer(producer_id, period_id)
    ws = wb.add_worksheet("Продажи")
    ws.write(0, 0, f"{producer_name} — Продажи — {period_name}", title_fmt)
    headers = ["Продукт", "SKU", "Аптека", "Регион", "Город", "Продажи, шт", "Продажи, сум"]
    for c, h in enumerate(headers):
        ws.write(2, c, h, header_fmt)
    for r, row in enumerate(sales, start=3):
        ws.write(r, 0, row["product_name"])
        ws.write(r, 1, row["sku_code"])
        ws.write(r, 2, row["pharmacy_name"])
        ws.write(r, 3, row["region"] or "")
        ws.write(r, 4, row["city"] or "")
        ws.write(r, 5, row["qty"] or 0, money_fmt)
        ws.write(r, 6, row["amount"] or 0, money_fmt)
    for c, w in enumerate([28, 12, 30, 16, 16, 14, 16]):
        ws.set_column(c, c, w)

    # ── План и бонусы ────────────────────────────────────────────────────────
    plans = await get_plans_by_producer(producer_id, period_id)
    ws = wb.add_worksheet("План и бонусы")
    ws.write(0, 0, f"{producer_name} — План и бонусы — {period_name}", title_fmt)
    headers = ["Продукт", "SKU", "Аптека", "Регион", "План, шт", "План, сум",
               "Факт, шт", "Факт, сум", "% вып.", "Бонус, сум"]
    for c, h in enumerate(headers):
        ws.write(2, c, h, header_fmt)
    for r, row in enumerate(plans, start=3):
        plan_amt = row["plan_amount"] or 0
        fact_amt = row["fact_amount"] or 0
        pct = round(fact_amt / plan_amt * 100, 1) if plan_amt else 0
        ws.write(r, 0, row["product_name"])
        ws.write(r, 1, row["sku_code"])
        ws.write(r, 2, row["pharmacy_name"])
        ws.write(r, 3, row["region"] or "")
        ws.write(r, 4, row["plan_qty"] or 0, money_fmt)
        ws.write(r, 5, plan_amt, money_fmt)
        ws.write(r, 6, row["fact_qty"] or 0, money_fmt)
        ws.write(r, 7, fact_amt, money_fmt)
        ws.write(r, 8, pct)
        ws.write(r, 9, row["bonus"] or 0, money_fmt)
    for c, w in enumerate([28, 12, 30, 16, 12, 14, 12, 14, 10, 14]):
        ws.set_column(c, c, w)

    # ── Сети (ИНН) ───────────────────────────────────────────────────────────
    entities = await get_entity_plans(producer_id, period_id)
    if entities:
        stock_rows = await get_stock_by_entity(producer_id, period_id)
        stock_by_inn = {r["inn"]: r["stock_qty"] or 0 for r in stock_rows}

        ws = wb.add_worksheet("Сети (ИНН)")
        ws.write(0, 0, f"{producer_name} — Сети/юрлица — {period_name}", title_fmt)
        headers = ["ИНН", "Юрлицо", "Регион", "Район", "Точек", "План, сум",
                   "Факт, сум", "% вып.", "Остаток, уп."]
        for c, h in enumerate(headers):
            ws.write(2, c, h, header_fmt)
        for r, row in enumerate(entities, start=3):
            plan_amt = row["plan_amount"] or 0
            fact_amt = row["fact_amount"] or 0
            ws.write(r, 0, row["inn"])
            ws.write(r, 1, row["org_name"])
            ws.write(r, 2, row["region"] or "")
            ws.write(r, 3, row["district"] or "")
            ws.write(r, 4, row["pharmacy_count"] or 0)
            ws.write(r, 5, plan_amt, money_fmt)
            ws.write(r, 6, fact_amt, money_fmt)
            ws.write(r, 7, row["pct"] or 0)
            ws.write(r, 8, stock_by_inn.get(row["inn"], 0), money_fmt)
        for c, w in enumerate([16, 32, 16, 20, 10, 14, 14, 10, 14]):
            ws.set_column(c, c, w)

    wb.close()
    buf.seek(0)
    return buf
