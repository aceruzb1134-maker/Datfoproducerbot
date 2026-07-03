"""
Генерация графиков динамики (п.3.2 ТЗ: "Историческую динамику (график) за
последние периоды обновления"). Используем matplotlib с backend "Agg"
(рендер в память, без дисплея) — шрифт DejaVu Sans по умолчанию поддерживает
кириллицу, поэтому дополнительных шрифтов подключать не нужно.
"""

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from io import BytesIO

COLOR_PRIMARY = "#1F6FEB"
COLOR_SECONDARY = "#8B5CF6"
COLOR_GRID = "#D0D7DE"


def build_sales_trend_chart(history: list, title: str) -> BytesIO:
    """
    history: список словарей с ключами period_name, total_amount, отсортированный
    по возрастанию даты (старые периоды слева, новые справа).
    """
    periods = [h["period_name"] for h in history]
    amounts = [float(h["total_amount"] or 0) for h in history]

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=150)
    x = range(len(periods))

    ax.plot(x, amounts, marker="o", linewidth=2.5, color=COLOR_PRIMARY, markersize=7, zorder=3)
    ax.fill_between(x, amounts, alpha=0.08, color=COLOR_PRIMARY, zorder=1)

    for i, v in enumerate(amounts):
        ax.annotate(f"{v:,.0f}".replace(",", " "), (i, v), textcoords="offset points",
                    xytext=(0, 10), ha="center", fontsize=9, color="#555")

    ax.set_title(title, fontsize=14, fontweight="bold", pad=15)
    ax.set_ylabel("Сумма продаж, сум", fontsize=10)
    ax.set_xticks(list(x))
    ax.set_xticklabels(periods, rotation=25, ha="right", fontsize=9)
    ax.grid(axis="y", linestyle="--", alpha=0.5, color=COLOR_GRID, zorder=0)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.set_ylim(bottom=0)

    fig.tight_layout()
    buf = BytesIO()
    fig.savefig(buf, format="png", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf


def build_plan_vs_fact_chart(history: list, title: str) -> BytesIO:
    """
    history: список словарей period_name, total_plan, total_fact — сравнение
    план/факт по периодам в виде столбчатой диаграммы.
    """
    periods = [h["period_name"] for h in history]
    plans = [float(h["total_plan"] or 0) for h in history]
    facts = [float(h["total_fact"] or 0) for h in history]

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=150)
    x = range(len(periods))
    width = 0.35

    ax.bar([i - width / 2 for i in x], plans, width, label="План", color=COLOR_SECONDARY, alpha=0.55, zorder=3)
    ax.bar([i + width / 2 for i in x], facts, width, label="Факт", color=COLOR_PRIMARY, zorder=3)

    ax.set_title(title, fontsize=14, fontweight="bold", pad=15)
    ax.set_ylabel("Сумма, сум", fontsize=10)
    ax.set_xticks(list(x))
    ax.set_xticklabels(periods, rotation=25, ha="right", fontsize=9)
    ax.grid(axis="y", linestyle="--", alpha=0.5, color=COLOR_GRID, zorder=0)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.legend(frameon=False, fontsize=10)
    ax.set_ylim(bottom=0)

    fig.tight_layout()
    buf = BytesIO()
    fig.savefig(buf, format="png", facecolor="white")
    plt.close(fig)
    buf.seek(0)
    return buf
