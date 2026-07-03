import os
import asyncpg

_pool = None


async def get_pool():
    global _pool
    if _pool is None:
        db_url = os.environ.get("DATABASE_PUBLIC_URL") or os.environ.get("DATABASE_URL")
        _pool = await asyncpg.create_pool(db_url, ssl="require")
    return _pool


async def _executemany_with_fallback(db, sql: str, params_list: list, chunk_size: int = 500):
    """
    Выполняет executemany пакетами по chunk_size — для больших загрузок это на
    порядки быстрее, чем один await на строку. НО: если один пакет целиком падает
    из-за одной "плохой" строки (executemany в asyncpg атомарен на уровне пакета),
    откатывается к построчному выполнению ТОЛЬКО внутри этого пакета — чтобы не
    терять корректные строки из-за одной проблемной.
    Возвращает (сколько_строк_успешно, [тексты ошибок, максимум 5]).
    """
    ok = 0
    errors: list[str] = []
    for i in range(0, len(params_list), chunk_size):
        chunk = params_list[i:i + chunk_size]
        try:
            await db.executemany(sql, chunk)
            ok += len(chunk)
        except Exception:
            for params in chunk:
                try:
                    await db.execute(sql, *params)
                    ok += 1
                except Exception as e2:
                    if len(errors) < 5:
                        errors.append(f"{type(e2).__name__}: {e2}")
    return ok, errors


async def init_db():
    pool = await get_pool()
    async with pool.acquire() as db:

        # Производители
        await db.execute("""
            CREATE TABLE IF NOT EXISTS producers (
                id         SERIAL PRIMARY KEY,
                name       TEXT UNIQUE NOT NULL,
                code       TEXT UNIQUE NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)

        # Пользователи (менеджеры производителей)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id          SERIAL PRIMARY KEY,
                telegram_id BIGINT UNIQUE,
                phone       TEXT,
                name        TEXT,
                producer_id INTEGER REFERENCES producers(id),
                role        TEXT DEFAULT 'manager',
                access_code TEXT UNIQUE NOT NULL,
                is_active   BOOLEAN DEFAULT TRUE,
                registered_at TIMESTAMP,
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """)

        # Продукты (SKU производителя)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id          SERIAL PRIMARY KEY,
                producer_id INTEGER REFERENCES producers(id),
                sku_code    TEXT NOT NULL,
                name        TEXT NOT NULL,
                created_at  TIMESTAMP DEFAULT NOW(),
                UNIQUE(producer_id, sku_code)
            )
        """)

        # Аптеки (физические точки)
        # ВАЖНО: ИНН — это юрлицо/сеть (может владеть 20+ точками с разными адресами),
        # НЕ уникальный идентификатор конкретной аптеки. Реальный уникальный ключ точки —
        # fom_id (стабилен между периодами обновления). Для источников данных без fom_id
        # (старый плоский формат) используется синтетический fom_id вида "INN-<инн>".
        await db.execute("""
            CREATE TABLE IF NOT EXISTS pharmacies (
                id      SERIAL PRIMARY KEY,
                fom_id  TEXT,
                inn     TEXT,
                name    TEXT,
                org_name TEXT,
                region  TEXT,
                city    TEXT,
                address TEXT
            )
        """)

        # Полностью защищённая миграция: гарантирует наличие каждой колонки независимо
        # от того, в каком состоянии таблица существовала раньше (ADD COLUMN IF NOT EXISTS
        # безопасно выполнять многократно — Postgres 9.6+).
        for col_sql in [
            "ALTER TABLE pharmacies ADD COLUMN IF NOT EXISTS fom_id TEXT",
            "ALTER TABLE pharmacies ADD COLUMN IF NOT EXISTS inn TEXT",
            "ALTER TABLE pharmacies ADD COLUMN IF NOT EXISTS name TEXT",
            "ALTER TABLE pharmacies ADD COLUMN IF NOT EXISTS org_name TEXT",
            "ALTER TABLE pharmacies ADD COLUMN IF NOT EXISTS region TEXT",
            "ALTER TABLE pharmacies ADD COLUMN IF NOT EXISTS city TEXT",
            "ALTER TABLE pharmacies ADD COLUMN IF NOT EXISTS address TEXT",
        ]:
            await db.execute(col_sql)

        # Бэкфилл fom_id для старых записей (1 ИНН = 1 точка) и NOT NULL/UNIQUE constraint —
        # оборачиваем в DO $$ с проверками, чтобы не упасть, если что-то уже применено.
        await db.execute("""
            DO $$
            BEGIN
                UPDATE pharmacies SET fom_id = 'INN-' || inn
                WHERE fom_id IS NULL AND inn IS NOT NULL;

                UPDATE pharmacies SET inn = '' WHERE inn IS NULL;
                UPDATE pharmacies SET name = COALESCE(name, '') WHERE name IS NULL;

                IF NOT EXISTS (SELECT 1 FROM pharmacies WHERE fom_id IS NULL)
                   AND EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='pharmacies' AND column_name='fom_id' AND is_nullable='YES') THEN
                    ALTER TABLE pharmacies ALTER COLUMN fom_id SET NOT NULL;
                END IF;

                IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'pharmacies_inn_key') THEN
                    ALTER TABLE pharmacies DROP CONSTRAINT pharmacies_inn_key;
                END IF;

                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'pharmacies_fom_id_key') THEN
                    ALTER TABLE pharmacies ADD CONSTRAINT pharmacies_fom_id_key UNIQUE (fom_id);
                END IF;
            END $$;
        """)

        # Индекс по ИНН для агрегации точек одной сети/юрлица
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_pharmacies_inn ON pharmacies(inn)
        """)

        # Периоды обновления данных
        await db.execute("""
            CREATE TABLE IF NOT EXISTS data_periods (
                id          SERIAL PRIMARY KEY,
                period_name TEXT NOT NULL,
                updated_at  TIMESTAMP DEFAULT NOW(),
                uploaded_by BIGINT,
                notes       TEXT
            )
        """)

        # Остатки (stock)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stock (
                id          SERIAL PRIMARY KEY,
                period_id   INTEGER REFERENCES data_periods(id),
                product_id  INTEGER REFERENCES products(id),
                pharmacy_id INTEGER REFERENCES pharmacies(id),
                qty         REAL DEFAULT 0,
                date_on     DATE,
                UNIQUE(period_id, product_id, pharmacy_id)
            )
        """)

        # Продажи (secondary sales)
        # incoming_qty / retail_bonus — необязательные поля из детальных листов
        # производителя (Приход, Bonus для аптек-точек за приход) — сохраняются "как есть",
        # производителю в UI по умолчанию не показываются (это retail-стимулирование аптек).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS sales (
                id            SERIAL PRIMARY KEY,
                period_id     INTEGER REFERENCES data_periods(id),
                product_id    INTEGER REFERENCES products(id),
                pharmacy_id   INTEGER REFERENCES pharmacies(id),
                qty           REAL DEFAULT 0,
                amount        REAL DEFAULT 0,
                incoming_qty  REAL DEFAULT 0,
                retail_bonus  REAL DEFAULT 0,
                date_on       DATE,
                UNIQUE(period_id, product_id, pharmacy_id)
            )
        """)
        await db.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='sales' AND column_name='incoming_qty') THEN
                    ALTER TABLE sales ADD COLUMN incoming_qty REAL DEFAULT 0;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='sales' AND column_name='retail_bonus') THEN
                    ALTER TABLE sales ADD COLUMN retail_bonus REAL DEFAULT 0;
                END IF;
            END $$;
        """)

        # Планы и бонусы
        await db.execute("""
            CREATE TABLE IF NOT EXISTS plans (
                id          SERIAL PRIMARY KEY,
                period_id   INTEGER REFERENCES data_periods(id),
                product_id  INTEGER REFERENCES products(id),
                pharmacy_id INTEGER REFERENCES pharmacies(id),
                plan_qty    REAL DEFAULT 0,
                plan_amount REAL DEFAULT 0,
                fact_qty    REAL DEFAULT 0,
                fact_amount REAL DEFAULT 0,
                bonus       REAL DEFAULT 0,
                UNIQUE(period_id, product_id, pharmacy_id)
            )
        """)

        # План/факт по юрлицу (сети аптек) — источник: сводный лист "свод" в файле производителя.
        # Не привязан к конкретному продукту/точке — только к ИНН сети в рамках периода.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS entity_plans (
                id              SERIAL PRIMARY KEY,
                period_id       INTEGER REFERENCES data_periods(id),
                producer_id     INTEGER REFERENCES producers(id),
                inn             TEXT NOT NULL,
                org_name        TEXT,
                region          TEXT,
                district        TEXT,
                pharmacy_count  INTEGER DEFAULT 0,
                plan_amount     REAL DEFAULT 0,
                fact_amount     REAL DEFAULT 0,
                updated_at      TIMESTAMP DEFAULT NOW(),
                UNIQUE(period_id, producer_id, inn)
            )
        """)

        # Настройки алертов
        await db.execute("""
            CREATE TABLE IF NOT EXISTS alert_settings (
                id              SERIAL PRIMARY KEY,
                producer_id     INTEGER REFERENCES producers(id) UNIQUE,
                min_stock_qty   REAL DEFAULT 0,
                sales_drop_pct  REAL DEFAULT 30,
                sales_surge_pct REAL DEFAULT 50,
                plan_lag_pct    REAL DEFAULT 20,
                updated_at      TIMESTAMP DEFAULT NOW()
            )
        """)

        # Миграция для уже развёрнутых баз, где UNIQUE(producer_id) ещё не было:
        # сначала убираем дубли (оставляем последнюю запись на производителя), потом добавляем constraint.
        await db.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'alert_settings_producer_id_key'
                ) THEN
                    DELETE FROM alert_settings a USING alert_settings b
                    WHERE a.producer_id = b.producer_id AND a.id < b.id;
                    ALTER TABLE alert_settings ADD CONSTRAINT alert_settings_producer_id_key UNIQUE (producer_id);
                END IF;
            END $$;
        """)

        # Лог действий пользователей
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_logs (
                id          SERIAL PRIMARY KEY,
                user_id     INTEGER REFERENCES users(id),
                action      TEXT NOT NULL,
                details     TEXT,
                created_at  TIMESTAMP DEFAULT NOW()
            )
        """)


# ── Producers ─────────────────────────────────────────────────────────────────

async def create_producer(name: str, code: str) -> int:
    pool = await get_pool()
    async with pool.acquire() as db:
        row = await db.fetchrow("""
            INSERT INTO producers (name, code) VALUES ($1, $2)
            ON CONFLICT(code) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
        """, name, code)
        return row["id"]


async def get_all_producers():
    pool = await get_pool()
    async with pool.acquire() as db:
        return await db.fetch("SELECT * FROM producers ORDER BY name")


async def get_producer_by_id(producer_id: int):
    pool = await get_pool()
    async with pool.acquire() as db:
        return await db.fetchrow("SELECT * FROM producers WHERE id = $1", producer_id)


# ── Users ─────────────────────────────────────────────────────────────────────

async def get_user_by_tg(telegram_id: int):
    pool = await get_pool()
    async with pool.acquire() as db:
        return await db.fetchrow("SELECT * FROM users WHERE telegram_id = $1", telegram_id)


async def get_user_by_code(code: str):
    pool = await get_pool()
    async with pool.acquire() as db:
        return await db.fetchrow("SELECT * FROM users WHERE access_code = $1 AND is_active = TRUE", code)


async def link_user_telegram(code: str, telegram_id: int, phone: str = "", name: str = ""):
    pool = await get_pool()
    async with pool.acquire() as db:
        await db.execute("""
            UPDATE users SET telegram_id=$1, phone=$2, name=$3, registered_at=NOW()
            WHERE access_code=$4
        """, telegram_id, phone, name, code)


async def create_user(access_code: str, producer_id: int, name: str = "", role: str = "manager") -> int:
    pool = await get_pool()
    async with pool.acquire() as db:
        row = await db.fetchrow("""
            INSERT INTO users (access_code, producer_id, name, role)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT(access_code) DO UPDATE SET producer_id=EXCLUDED.producer_id, name=EXCLUDED.name
            RETURNING id
        """, access_code, producer_id, name, role)
        return row["id"]


async def get_all_users():
    pool = await get_pool()
    async with pool.acquire() as db:
        return await db.fetch("""
            SELECT u.*, p.name as producer_name
            FROM users u
            LEFT JOIN producers p ON p.id = u.producer_id
            ORDER BY p.name, u.name
        """)


async def deactivate_user(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as db:
        await db.execute("UPDATE users SET is_active=FALSE WHERE id=$1", user_id)


# ── Products ──────────────────────────────────────────────────────────────────

async def upsert_product(producer_id: int, sku_code: str, name: str) -> int:
    pool = await get_pool()
    async with pool.acquire() as db:
        row = await db.fetchrow("""
            INSERT INTO products (producer_id, sku_code, name)
            VALUES ($1, $2, $3)
            ON CONFLICT(producer_id, sku_code) DO UPDATE SET name=EXCLUDED.name
            RETURNING id
        """, producer_id, sku_code, name)
        return row["id"]


async def batch_upsert_products(producer_id: int, items: list[tuple[str, str]]) -> tuple[dict[str, int], list[str]]:
    """
    Пакетная версия upsert_product — для больших загрузок (сотни/тысячи строк).
    items: [(sku_code, name), ...], уже без дублей по sku_code.
    Возвращает ({sku_code: product_id}, [ошибки]).
    """
    if not items:
        return {}, []
    pool = await get_pool()
    async with pool.acquire() as db:
        _, errors = await _executemany_with_fallback(db, """
            INSERT INTO products (producer_id, sku_code, name)
            VALUES ($1, $2, $3)
            ON CONFLICT(producer_id, sku_code) DO UPDATE SET name=EXCLUDED.name
        """, [(producer_id, sku, name) for sku, name in items])

        skus = [sku for sku, _ in items]
        rows = await db.fetch(
            "SELECT id, sku_code FROM products WHERE producer_id=$1 AND sku_code = ANY($2::text[])",
            producer_id, skus
        )
        return {r["sku_code"]: r["id"] for r in rows}, errors


async def get_products_by_producer(producer_id: int):
    pool = await get_pool()
    async with pool.acquire() as db:
        return await db.fetch("SELECT * FROM products WHERE producer_id=$1 ORDER BY name", producer_id)


# ── Pharmacies ────────────────────────────────────────────────────────────────

async def upsert_pharmacy(fom_id: str, inn: str, name: str, org_name: str = "",
                           region: str = "", city: str = "", address: str = "") -> int:
    """
    fom_id — уникальный идентификатор конкретной физической аптечной точки.
    inn — ИНН юрлица/сети, которому принадлежит точка (НЕ уникален — одна сеть = много точек).
    Для источников без fom_id (старый плоский формат) вызывающий код должен передать
    синтетический fom_id вида f"INN-{inn}".
    """
    pool = await get_pool()
    async with pool.acquire() as db:
        row = await db.fetchrow("""
            INSERT INTO pharmacies (fom_id, inn, name, org_name, region, city, address)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT(fom_id) DO UPDATE SET
                inn=EXCLUDED.inn, name=EXCLUDED.name, org_name=EXCLUDED.org_name,
                region=EXCLUDED.region, city=EXCLUDED.city, address=EXCLUDED.address
            RETURNING id
        """, fom_id, inn, name, org_name, region, city, address)
        return row["id"]


async def batch_upsert_pharmacies(items: list[tuple]) -> tuple[dict[str, int], list[str]]:
    """
    Пакетная версия upsert_pharmacy.
    items: [(fom_id, inn, name, org_name, region, city, address), ...], без дублей по fom_id.
    Возвращает ({fom_id: pharmacy_id}, [ошибки]).
    """
    if not items:
        return {}, []
    pool = await get_pool()
    async with pool.acquire() as db:
        _, errors = await _executemany_with_fallback(db, """
            INSERT INTO pharmacies (fom_id, inn, name, org_name, region, city, address)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT(fom_id) DO UPDATE SET
                inn=EXCLUDED.inn, name=EXCLUDED.name, org_name=EXCLUDED.org_name,
                region=EXCLUDED.region, city=EXCLUDED.city, address=EXCLUDED.address
        """, items)

        fom_ids = [it[0] for it in items]
        rows = await db.fetch(
            "SELECT id, fom_id FROM pharmacies WHERE fom_id = ANY($1::text[])", fom_ids
        )
        return {r["fom_id"]: r["id"] for r in rows}, errors


async def get_pharmacies_by_producer(producer_id: int, period_id: int):
    """Get pharmacies that have sales/stock data for this producer in this period."""
    pool = await get_pool()
    async with pool.acquire() as db:
        return await db.fetch("""
            SELECT DISTINCT ph.* FROM pharmacies ph
            JOIN sales s ON s.pharmacy_id = ph.id
            JOIN products p ON p.id = s.product_id
            WHERE p.producer_id = $1 AND s.period_id = $2
            ORDER BY ph.region, ph.name
        """, producer_id, period_id)


async def search_pharmacies(producer_id: int, period_id: int, query: str):
    pool = await get_pool()
    async with pool.acquire() as db:
        return await db.fetch("""
            SELECT DISTINCT ph.* FROM pharmacies ph
            JOIN sales s ON s.pharmacy_id = ph.id
            JOIN products p ON p.id = s.product_id
            WHERE p.producer_id = $1 AND s.period_id = $2
              AND (LOWER(ph.name) LIKE $3 OR LOWER(ph.region) LIKE $3)
            ORDER BY ph.name
            LIMIT 20
        """, producer_id, period_id, f"%{query.lower()}%")


# ── Data periods ──────────────────────────────────────────────────────────────

async def create_period(period_name: str, uploaded_by: int, notes: str = "") -> int:
    pool = await get_pool()
    async with pool.acquire() as db:
        row = await db.fetchrow("""
            INSERT INTO data_periods (period_name, uploaded_by, notes)
            VALUES ($1, $2, $3) RETURNING id
        """, period_name, uploaded_by, notes)
        return row["id"]


async def get_period_by_name(period_name: str):
    pool = await get_pool()
    async with pool.acquire() as db:
        return await db.fetchrow("SELECT * FROM data_periods WHERE period_name=$1", period_name)


async def get_or_create_period(period_name: str, uploaded_by: int) -> int:
    """Idempotent: повторная загрузка того же периода (по имени) обновляет данные, а не плодит дубли."""
    existing = await get_period_by_name(period_name)
    if existing:
        pool = await get_pool()
        async with pool.acquire() as db:
            await db.execute("UPDATE data_periods SET updated_at=NOW(), uploaded_by=$1 WHERE id=$2",
                              uploaded_by, existing["id"])
        return existing["id"]
    return await create_period(period_name, uploaded_by)


async def get_latest_period():
    pool = await get_pool()
    async with pool.acquire() as db:
        return await db.fetchrow(
            "SELECT * FROM data_periods ORDER BY updated_at DESC LIMIT 1"
        )


async def get_all_periods():
    pool = await get_pool()
    async with pool.acquire() as db:
        return await db.fetch("SELECT * FROM data_periods ORDER BY updated_at DESC LIMIT 12")


async def get_previous_period(period_id: int):
    """Period immediately before the given one, by updated_at. Used for drop/surge comparison."""
    pool = await get_pool()
    async with pool.acquire() as db:
        current = await db.fetchrow("SELECT updated_at FROM data_periods WHERE id=$1", period_id)
        if not current:
            return None
        return await db.fetchrow("""
            SELECT * FROM data_periods
            WHERE updated_at < $1
            ORDER BY updated_at DESC
            LIMIT 1
        """, current["updated_at"])


# ── Хранение истории / архивация (п.4, п.5 ТЗ: "минимум 6-12 месяцев") ─────────
# Удаление ручное, через админ-команду /cleanup с подтверждением — НЕ автоматическое,
# чтобы не потерять данные молча.

async def get_old_periods(retention_months: int = 12):
    """Периоды, которым больше retention_months месяцев (по updated_at) — кандидаты на удаление."""
    pool = await get_pool()
    async with pool.acquire() as db:
        return await db.fetch("""
            SELECT * FROM data_periods
            WHERE updated_at < NOW() - ($1 || ' months')::interval
            ORDER BY updated_at
        """, str(retention_months))


async def delete_period_cascade(period_id: int):
    """Полностью и безвозвратно удаляет период и все связанные с ним данные."""
    pool = await get_pool()
    async with pool.acquire() as db:
        async with db.transaction():
            await db.execute("DELETE FROM sales WHERE period_id=$1", period_id)
            await db.execute("DELETE FROM stock WHERE period_id=$1", period_id)
            await db.execute("DELETE FROM plans WHERE period_id=$1", period_id)
            await db.execute("DELETE FROM entity_plans WHERE period_id=$1", period_id)
            await db.execute("DELETE FROM data_periods WHERE id=$1", period_id)


async def get_producer_ids_in_period(period_id: int) -> list[int]:
    """Distinct producer_ids that have any sales/stock/plan data in this period. Used for alert push after upload."""
    pool = await get_pool()
    async with pool.acquire() as db:
        rows = await db.fetch("""
            SELECT DISTINCT p.producer_id FROM products p
            WHERE p.id IN (
                SELECT product_id FROM sales WHERE period_id=$1
                UNION
                SELECT product_id FROM stock WHERE period_id=$1
                UNION
                SELECT product_id FROM plans WHERE period_id=$1
            )
        """, period_id)
        return [r["producer_id"] for r in rows]


async def get_users_by_producer(producer_id: int):
    """Active, registered users of a producer — used to target alert push notifications."""
    pool = await get_pool()
    async with pool.acquire() as db:
        return await db.fetch("""
            SELECT * FROM users
            WHERE producer_id=$1 AND is_active=TRUE AND telegram_id IS NOT NULL
        """, producer_id)


# ── Stock ─────────────────────────────────────────────────────────────────────

async def save_stock(period_id: int, product_id: int, pharmacy_id: int, qty: float, date_on):
    pool = await get_pool()
    async with pool.acquire() as db:
        await db.execute("""
            INSERT INTO stock (period_id, product_id, pharmacy_id, qty, date_on)
            VALUES ($1,$2,$3,$4,$5)
            ON CONFLICT(period_id, product_id, pharmacy_id) DO UPDATE SET qty=EXCLUDED.qty, date_on=EXCLUDED.date_on
        """, period_id, product_id, pharmacy_id, qty, date_on)


async def batch_save_stock(rows: list[tuple]) -> tuple[int, list[str]]:
    """rows: [(period_id, product_id, pharmacy_id, qty, date_on), ...]. Возвращает (сохранено, [ошибки])."""
    if not rows:
        return 0, []
    pool = await get_pool()
    async with pool.acquire() as db:
        return await _executemany_with_fallback(db, """
            INSERT INTO stock (period_id, product_id, pharmacy_id, qty, date_on)
            VALUES ($1,$2,$3,$4,$5)
            ON CONFLICT(period_id, product_id, pharmacy_id) DO UPDATE SET qty=EXCLUDED.qty, date_on=EXCLUDED.date_on
        """, rows)


async def get_stock_by_producer(producer_id: int, period_id: int):
    pool = await get_pool()
    async with pool.acquire() as db:
        return await db.fetch("""
            SELECT s.qty, s.date_on, p.name as product_name, p.sku_code,
                   ph.name as pharmacy_name, ph.region, ph.city
            FROM stock s
            JOIN products p ON p.id = s.product_id
            JOIN pharmacies ph ON ph.id = s.pharmacy_id
            WHERE p.producer_id = $1 AND s.period_id = $2
            ORDER BY p.name, ph.region, ph.name
        """, producer_id, period_id)


async def get_low_stock(producer_id: int, period_id: int, min_qty: float = 0):
    pool = await get_pool()
    async with pool.acquire() as db:
        return await db.fetch("""
            SELECT s.qty, p.name as product_name, ph.name as pharmacy_name, ph.region
            FROM stock s
            JOIN products p ON p.id = s.product_id
            JOIN pharmacies ph ON ph.id = s.pharmacy_id
            WHERE p.producer_id = $1 AND s.period_id = $2 AND s.qty <= $3
            ORDER BY s.qty, p.name
        """, producer_id, period_id, min_qty)


# ── Sales ─────────────────────────────────────────────────────────────────────

async def save_sale(period_id: int, product_id: int, pharmacy_id: int, qty: float, amount: float,
                     date_on, incoming_qty: float = 0, retail_bonus: float = 0):
    pool = await get_pool()
    async with pool.acquire() as db:
        await db.execute("""
            INSERT INTO sales (period_id, product_id, pharmacy_id, qty, amount, incoming_qty, retail_bonus, date_on)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT(period_id, product_id, pharmacy_id) DO UPDATE SET
                qty=EXCLUDED.qty, amount=EXCLUDED.amount, incoming_qty=EXCLUDED.incoming_qty,
                retail_bonus=EXCLUDED.retail_bonus, date_on=EXCLUDED.date_on
        """, period_id, product_id, pharmacy_id, qty, amount, incoming_qty, retail_bonus, date_on)


async def batch_save_sale(rows: list[tuple]) -> tuple[int, list[str]]:
    """rows: [(period_id, product_id, pharmacy_id, qty, amount, incoming_qty, retail_bonus, date_on), ...]"""
    if not rows:
        return 0, []
    pool = await get_pool()
    async with pool.acquire() as db:
        return await _executemany_with_fallback(db, """
            INSERT INTO sales (period_id, product_id, pharmacy_id, qty, amount, incoming_qty, retail_bonus, date_on)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT(period_id, product_id, pharmacy_id) DO UPDATE SET
                qty=EXCLUDED.qty, amount=EXCLUDED.amount, incoming_qty=EXCLUDED.incoming_qty,
                retail_bonus=EXCLUDED.retail_bonus, date_on=EXCLUDED.date_on
        """, rows)


async def get_sales_by_producer(producer_id: int, period_id: int):
    pool = await get_pool()
    async with pool.acquire() as db:
        return await db.fetch("""
            SELECT s.qty, s.amount, s.date_on,
                   p.name as product_name, p.sku_code,
                   ph.name as pharmacy_name, ph.region, ph.city
            FROM sales s
            JOIN products p ON p.id = s.product_id
            JOIN pharmacies ph ON ph.id = s.pharmacy_id
            WHERE p.producer_id = $1 AND s.period_id = $2
            ORDER BY s.amount DESC
        """, producer_id, period_id)


async def get_sales_summary_by_product(producer_id: int, period_id: int):
    pool = await get_pool()
    async with pool.acquire() as db:
        return await db.fetch("""
            SELECT p.name as product_name, p.sku_code,
                   SUM(s.qty) as total_qty, SUM(s.amount) as total_amount,
                   COUNT(DISTINCT s.pharmacy_id) as pharmacy_count
            FROM sales s
            JOIN products p ON p.id = s.product_id
            WHERE p.producer_id = $1 AND s.period_id = $2
            GROUP BY p.id, p.name, p.sku_code
            ORDER BY total_amount DESC
        """, producer_id, period_id)


async def get_sales_summary_by_region(producer_id: int, period_id: int):
    pool = await get_pool()
    async with pool.acquire() as db:
        return await db.fetch("""
            SELECT ph.region, SUM(s.qty) as total_qty, SUM(s.amount) as total_amount,
                   COUNT(DISTINCT s.pharmacy_id) as pharmacy_count
            FROM sales s
            JOIN products p ON p.id = s.product_id
            JOIN pharmacies ph ON ph.id = s.pharmacy_id
            WHERE p.producer_id = $1 AND s.period_id = $2
            GROUP BY ph.region
            ORDER BY total_amount DESC
        """, producer_id, period_id)


async def get_sales_history(producer_id: int, limit: int = 8):
    """Sales totals per period for trend analysis."""
    pool = await get_pool()
    async with pool.acquire() as db:
        return await db.fetch("""
            SELECT dp.period_name, dp.updated_at,
                   SUM(s.qty) as total_qty, SUM(s.amount) as total_amount
            FROM sales s
            JOIN products p ON p.id = s.product_id
            JOIN data_periods dp ON dp.id = s.period_id
            WHERE p.producer_id = $1
            GROUP BY dp.id, dp.period_name, dp.updated_at
            ORDER BY dp.updated_at DESC
            LIMIT $2
        """, producer_id, limit)


# ── Plans & Bonuses ───────────────────────────────────────────────────────────

async def save_plan(period_id: int, product_id: int, pharmacy_id: int,
                    plan_qty: float, plan_amount: float,
                    fact_qty: float, fact_amount: float, bonus: float):
    pool = await get_pool()
    async with pool.acquire() as db:
        await db.execute("""
            INSERT INTO plans (period_id, product_id, pharmacy_id,
                plan_qty, plan_amount, fact_qty, fact_amount, bonus)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT(period_id, product_id, pharmacy_id) DO UPDATE SET
                plan_qty=EXCLUDED.plan_qty, plan_amount=EXCLUDED.plan_amount,
                fact_qty=EXCLUDED.fact_qty, fact_amount=EXCLUDED.fact_amount,
                bonus=EXCLUDED.bonus
        """, period_id, product_id, pharmacy_id,
            plan_qty, plan_amount, fact_qty, fact_amount, bonus)


async def get_plan_summary(producer_id: int, period_id: int):
    pool = await get_pool()
    async with pool.acquire() as db:
        return await db.fetchrow("""
            SELECT SUM(pl.plan_amount) as total_plan,
                   SUM(pl.fact_amount) as total_fact,
                   SUM(pl.bonus) as total_bonus,
                   COUNT(DISTINCT pl.pharmacy_id) as pharmacy_count
            FROM plans pl
            JOIN products p ON p.id = pl.product_id
            WHERE p.producer_id = $1 AND pl.period_id = $2
        """, producer_id, period_id)


async def get_plans_by_producer(producer_id: int, period_id: int):
    """Detailed pharmacy-level plan/fact/bonus rows — used for Excel export."""
    pool = await get_pool()
    async with pool.acquire() as db:
        return await db.fetch("""
            SELECT pl.plan_qty, pl.plan_amount, pl.fact_qty, pl.fact_amount, pl.bonus,
                   p.name as product_name, p.sku_code,
                   ph.name as pharmacy_name, ph.region, ph.city
            FROM plans pl
            JOIN products p ON p.id = pl.product_id
            JOIN pharmacies ph ON ph.id = pl.pharmacy_id
            WHERE p.producer_id = $1 AND pl.period_id = $2
            ORDER BY p.name, ph.region, ph.name
        """, producer_id, period_id)


async def get_plan_by_product(producer_id: int, period_id: int):
    pool = await get_pool()
    async with pool.acquire() as db:
        return await db.fetch("""
            SELECT p.name as product_name,
                   SUM(pl.plan_amount) as plan_amount,
                   SUM(pl.fact_amount) as fact_amount,
                   SUM(pl.bonus) as bonus,
                   CASE WHEN SUM(pl.plan_amount) > 0
                        THEN ROUND((SUM(pl.fact_amount)/SUM(pl.plan_amount)*100)::numeric, 1)
                        ELSE 0 END as pct
            FROM plans pl
            JOIN products p ON p.id = pl.product_id
            WHERE p.producer_id = $1 AND pl.period_id = $2
            GROUP BY p.id, p.name
            ORDER BY fact_amount DESC
        """, producer_id, period_id)


async def get_bonus_history(producer_id: int):
    pool = await get_pool()
    async with pool.acquire() as db:
        return await db.fetch("""
            SELECT dp.period_name, SUM(pl.bonus) as total_bonus,
                   SUM(pl.plan_amount) as total_plan, SUM(pl.fact_amount) as total_fact
            FROM plans pl
            JOIN products p ON p.id = pl.product_id
            JOIN data_periods dp ON dp.id = pl.period_id
            WHERE p.producer_id = $1
            GROUP BY dp.id, dp.period_name, dp.updated_at
            ORDER BY dp.updated_at DESC
            LIMIT 6
        """, producer_id)


# ── Entity plans (план/факт по юрлицу-сети из сводного листа) ──────────────────

async def upsert_entity_plan(period_id: int, producer_id: int, inn: str, org_name: str,
                              region: str, district: str, pharmacy_count: int,
                              plan_amount: float, fact_amount: float):
    pool = await get_pool()
    async with pool.acquire() as db:
        await db.execute("""
            INSERT INTO entity_plans (period_id, producer_id, inn, org_name, region, district,
                pharmacy_count, plan_amount, fact_amount, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9, NOW())
            ON CONFLICT(period_id, producer_id, inn) DO UPDATE SET
                org_name=EXCLUDED.org_name, region=EXCLUDED.region, district=EXCLUDED.district,
                pharmacy_count=EXCLUDED.pharmacy_count, plan_amount=EXCLUDED.plan_amount,
                fact_amount=EXCLUDED.fact_amount, updated_at=NOW()
        """, period_id, producer_id, inn, org_name, region, district,
            pharmacy_count, plan_amount, fact_amount)


async def batch_upsert_entity_plans(rows: list[tuple]) -> tuple[int, list[str]]:
    """rows: [(period_id, producer_id, inn, org_name, region, district, pharmacy_count, plan_amount, fact_amount), ...]"""
    if not rows:
        return 0, []
    pool = await get_pool()
    async with pool.acquire() as db:
        return await _executemany_with_fallback(db, """
            INSERT INTO entity_plans (period_id, producer_id, inn, org_name, region, district,
                pharmacy_count, plan_amount, fact_amount, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9, NOW())
            ON CONFLICT(period_id, producer_id, inn) DO UPDATE SET
                org_name=EXCLUDED.org_name, region=EXCLUDED.region, district=EXCLUDED.district,
                pharmacy_count=EXCLUDED.pharmacy_count, plan_amount=EXCLUDED.plan_amount,
                fact_amount=EXCLUDED.fact_amount, updated_at=NOW()
        """, rows)


async def get_entity_plans(producer_id: int, period_id: int):
    """Все сети/юрлица, закупающие товар производителя в этом периоде: план, факт, % выполнения."""
    pool = await get_pool()
    async with pool.acquire() as db:
        return await db.fetch("""
            SELECT *,
                   CASE WHEN plan_amount > 0
                        THEN ROUND((fact_amount / plan_amount * 100)::numeric, 1)
                        ELSE 0 END as pct
            FROM entity_plans
            WHERE producer_id = $1 AND period_id = $2
            ORDER BY fact_amount DESC
        """, producer_id, period_id)


async def get_entity_plan_summary(producer_id: int, period_id: int):
    pool = await get_pool()
    async with pool.acquire() as db:
        return await db.fetchrow("""
            SELECT SUM(plan_amount) as total_plan, SUM(fact_amount) as total_fact,
                   COUNT(*) as entity_count, SUM(pharmacy_count) as total_pharmacies
            FROM entity_plans
            WHERE producer_id = $1 AND period_id = $2
        """, producer_id, period_id)


async def get_stock_by_entity(producer_id: int, period_id: int):
    """Остаток товара, агрегированный по ИНН сети (сумма по всем точкам сети)."""
    pool = await get_pool()
    async with pool.acquire() as db:
        return await db.fetch("""
            SELECT ph.inn, SUM(s.qty) as stock_qty
            FROM stock s
            JOIN products p ON p.id = s.product_id
            JOIN pharmacies ph ON ph.id = s.pharmacy_id
            WHERE p.producer_id = $1 AND s.period_id = $2
            GROUP BY ph.inn
        """, producer_id, period_id)


# ── Alert settings ────────────────────────────────────────────────────────────

async def get_alert_settings(producer_id: int):
    pool = await get_pool()
    async with pool.acquire() as db:
        row = await db.fetchrow("SELECT * FROM alert_settings WHERE producer_id=$1", producer_id)
        if not row:
            await db.execute("""
                INSERT INTO alert_settings (producer_id) VALUES ($1)
                ON CONFLICT (producer_id) DO NOTHING
            """, producer_id)
            row = await db.fetchrow("SELECT * FROM alert_settings WHERE producer_id=$1", producer_id)
        return row


async def update_alert_settings(producer_id: int, min_stock_qty: float,
                                 sales_drop_pct: float, sales_surge_pct: float,
                                 plan_lag_pct: float):
    pool = await get_pool()
    async with pool.acquire() as db:
        await db.execute("""
            INSERT INTO alert_settings (producer_id, min_stock_qty, sales_drop_pct, sales_surge_pct, plan_lag_pct, updated_at)
            VALUES ($1,$2,$3,$4,$5, NOW())
            ON CONFLICT (producer_id) DO UPDATE SET
                min_stock_qty=EXCLUDED.min_stock_qty,
                sales_drop_pct=EXCLUDED.sales_drop_pct,
                sales_surge_pct=EXCLUDED.sales_surge_pct,
                plan_lag_pct=EXCLUDED.plan_lag_pct,
                updated_at=NOW()
        """, producer_id, min_stock_qty, sales_drop_pct, sales_surge_pct, plan_lag_pct)


# ── Logging ───────────────────────────────────────────────────────────────────

async def log_action(user_id: int, action: str, details: str = ""):
    pool = await get_pool()
    async with pool.acquire() as db:
        await db.execute("""
            INSERT INTO user_logs (user_id, action, details) VALUES ($1,$2,$3)
        """, user_id, action, details)
