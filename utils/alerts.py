"""
Alert engine for DATFO bot.

Builds a structured list of alerts for a producer/period combination:
  - 🔴 Дефектура (низкий остаток)
  - 🟡 Отставание от плана
  - 📉 Падение продаж (сравнение с предыдущим периодом обновления)
  - 📈 Рост спроса / «волна рынка» (сравнение с предыдущим периодом)

Thresholds come from `alert_settings` (per producer, editable by admin via
"⚙️ Настройки" -> not hardcoded, per ТЗ п.3.5).
"""

from utils.db import (
    get_alert_settings, get_low_stock, get_plan_summary,
    get_sales_summary_by_product, get_previous_period,
)
from utils.excel import fmt_money, fmt_qty


async def build_alerts(producer_id: int, period_id: int) -> list[dict]:
    """
    Returns a list of alert dicts: {"icon": str, "title": str, "lines": [str, ...]}
    Empty list means "all clear".
    """
    settings = await get_alert_settings(producer_id)
    min_stock = settings["min_stock_qty"] if settings else 0
    drop_pct  = settings["sales_drop_pct"] if settings else 30
    surge_pct = settings["sales_surge_pct"] if settings else 50
    lag_pct   = settings["plan_lag_pct"] if settings else 20

    alerts: list[dict] = []

    # ── 1. Дефектура ────────────────────────────────────────────────────────
    low_stock = await get_low_stock(producer_id, period_id, min_stock)
    if low_stock:
        lines = [
            f"{r['product_name']} — {r['pharmacy_name']} ({r['region'] or '—'}): {fmt_qty(r['qty'])} уп."
            for r in low_stock[:5]
        ]
        if len(low_stock) > 5:
            lines.append(f"…и ещё {len(low_stock) - 5}")
        alerts.append({
            "icon": "🔴",
            "title": f"Дефектура — {len(low_stock)} позиций с остатком ≤ {fmt_qty(min_stock)} уп.",
            "lines": lines,
        })

    # ── 2. Отставание от плана ──────────────────────────────────────────────
    summary = await get_plan_summary(producer_id, period_id)
    if summary and summary["total_plan"]:
        pct = (summary["total_fact"] or 0) / summary["total_plan"] * 100
        if pct < (100 - lag_pct):
            gap = (summary["total_plan"] or 0) - (summary["total_fact"] or 0)
            alerts.append({
                "icon": "🟡",
                "title": f"Отставание от плана — выполнено {round(pct)}%",
                "lines": [f"Не хватает до плана: {fmt_money(gap)}"],
            })

    # ── 3 & 4. Падение продаж / Рост спроса (vs предыдущий период) ─────────
    prev_period = await get_previous_period(period_id)
    if prev_period:
        curr = await get_sales_summary_by_product(producer_id, period_id)
        prev = await get_sales_summary_by_product(producer_id, prev_period["id"])
        prev_map = {r["product_name"]: (r["total_amount"] or 0) for r in prev}

        drops, surges = [], []
        for r in curr:
            name = r["product_name"]
            cur_amt = r["total_amount"] or 0
            prev_amt = prev_map.get(name, 0)
            if prev_amt <= 0:
                continue  # нет базы для сравнения
            change = (cur_amt - prev_amt) / prev_amt * 100
            if change <= -drop_pct:
                drops.append((name, change, cur_amt, prev_amt))
            elif change >= surge_pct:
                surges.append((name, change, cur_amt, prev_amt))

        if drops:
            drops.sort(key=lambda x: x[1])  # самое сильное падение первым
            lines = [
                f"{n}: {round(chg)}% ({fmt_money(cur)} ← {fmt_money(prv)})"
                for n, chg, cur, prv in drops[:5]
            ]
            if len(drops) > 5:
                lines.append(f"…и ещё {len(drops) - 5}")
            alerts.append({
                "icon": "📉",
                "title": f"Падение продаж — {len(drops)} продукт(ов) (порог {round(drop_pct)}%)",
                "lines": lines,
            })

        if surges:
            surges.sort(key=lambda x: -x[1])  # самый сильный рост первым
            lines = [
                f"{n}: +{round(chg)}% ({fmt_money(cur)} ← {fmt_money(prv)})"
                for n, chg, cur, prv in surges[:5]
            ]
            if len(surges) > 5:
                lines.append(f"…и ещё {len(surges) - 5}")
            alerts.append({
                "icon": "📈",
                "title": f"Рост спроса / волна рынка — {len(surges)} продукт(ов) (порог {round(surge_pct)}%)",
                "lines": lines,
            })

    return alerts


def format_alerts_text(alerts: list[dict], header: str = "🔔 *Алерты и предупреждения*") -> str:
    """Render alerts list into a Markdown message."""
    if not alerts:
        return "✅ *Алерты*\n\nВсё в норме — критических отклонений не обнаружено."

    lines = [header, ""]
    for a in alerts:
        lines.append(f"{a['icon']} *{a['title']}*")
        for l in a["lines"]:
            lines.append(f"  • {l}")
        lines.append("")

    return "\n".join(lines).strip()
