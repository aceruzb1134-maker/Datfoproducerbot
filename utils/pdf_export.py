"""
PDF-выгрузка (п.3.2 ТЗ: "Детальный отчёт (выгрузка в Excel/PDF)").

Реализовано через matplotlib.backends.backend_pdf.PdfPages — тот же движок,
что и для графиков (utils/charts.py). Сознательно НЕ используем reportlab/fpdf2:
у них нет встроенной поддержки кириллицы без подключения отдельного TTF-шрифта,
а на PaaS-хостинге (Railway и т.п.) нет гарантии, что нужный шрифт будет в
образе — это источник ещё одного "тихого" падения при деплое. DejaVu Sans,
которым matplotlib пользуется по умолчанию, кириллицу поддерживает "из коробки".

Итоговый Excel-отчёт (utils/export.py) остаётся основным источником полной
детализации; PDF — компактная сводка на 1-2 страницы для печати/пересылки.
"""

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from io import BytesIO

from utils.excel import fmt_money, fmt_qty


def _new_page(figsize=(8.27, 11.69)):  # A4 в дюймах
    fig, ax = plt.subplots(figsize=figsize, dpi=150)
    ax.axis("off")
    return fig, ax


def _draw_text_block(ax, lines: list[str], y_start: float = 0.97, line_height: float = 0.026,
                      font_size: int = 10.5):
    y = y_start
    for line in lines:
        weight = "bold" if line.startswith("§") else "normal"
        size = font_size + 2 if line.startswith("§") else font_size
        ax.text(0.04, y, line.lstrip("§"), va="top", fontsize=size, fontweight=weight,
                 family="DejaVu Sans", transform=ax.transAxes)
        y -= line_height
    return y


async def build_bonus_pdf(producer_name: str, period_name: str, summary: dict,
                           by_product: list, entities: list | None = None) -> BytesIO:
    """
    Компактный PDF-отчёт: план/факт/бонус по производителю за период,
    разбивка по продуктам и (если есть) по сетям-ИНН.
    """
    buf = BytesIO()

    with PdfPages(buf) as pdf:
        fig, ax = _new_page()

        total_plan = summary["total_plan"] or 0 if summary else 0
        total_fact = summary["total_fact"] or 0 if summary else 0
        pct = round(total_fact / total_plan * 100, 1) if total_plan else 0

        lines = [
            f"§DATFO — Отчёт по бонусам и плану",
            f"{producer_name}",
            f"Период: {period_name}",
            "",
            f"§Сводка",
            f"План: {fmt_money(total_plan)}",
            f"Факт: {fmt_money(total_fact)}   ({pct}% от плана)",
        ]
        if summary and summary["total_bonus"] is not None:
            lines.append(f"Бонус: {fmt_money(summary['total_bonus'])}")
        lines.append("")

        if by_product:
            lines.append("§По продуктам")
            for r in by_product[:25]:
                p_plan = r["plan_amount"] or 0
                p_fact = r["fact_amount"] or 0
                p_pct = round(p_fact / p_plan * 100, 1) if p_plan else 0
                lines.append(f"{r['product_name']}: план {fmt_money(p_plan)}, факт {fmt_money(p_fact)} ({p_pct}%)")
            if len(by_product) > 25:
                lines.append(f"...и ещё {len(by_product) - 25} (полный список — в Excel-отчёте)")
            lines.append("")

        if entities:
            lines.append("§Топ сетей по объёму закупки")
            for e in entities[:20]:
                e_pct = e["pct"]
                lines.append(
                    f"{e['org_name'] or e['inn']} ({e['region'] or '—'}): "
                    f"план {fmt_money(e['plan_amount'])}, факт {fmt_money(e['fact_amount'])} ({e_pct}%)"
                )
            if len(entities) > 20:
                lines.append(f"...и ещё {len(entities) - 20} (полный список — в Excel-отчёте)")

        _draw_text_block(ax, lines)
        pdf.savefig(fig)
        plt.close(fig)

    buf.seek(0)
    return buf
