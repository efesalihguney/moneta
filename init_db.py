"""
Moneta - İdempotent Veritabanı Kurulum ve Doğrulama Motoru (init_db.py)
-----------------------------------------------------------------------
Bu betik, projeyi sıfırdan klonlayan bir geliştiricinin veya akademik jürinin
tek komutla (`python init_db.py`):
1. Akıllı SQL Server keşfi (Auto-Discovery) ile yerel MSSQL sunucusuna bağlanmasını,
2. `wealth_tracker_db` veritabanını oluşturmasını,
3. 8 ilişkisel tabloyu, yabancı anahtarları (FK) ve performans indekslerini kurmasını,
4. `INFORMATION_SCHEMA.COLUMNS` üzerinden tüm kritik sütunları doğrulamasını,
5. Kurumsal `vw_user_financial_summary` (View) ve `sp_get_user_budget_health` (Stored Procedure)
   nesnelerini tanımlamasını,
6. Eğer sistemde hiç kullanıcı yoksa varsayılan başlangıç kullanıcısını ve temel kategorileri
   (mevcut kullanıcı ve verilere kesinlikle dokunmadan) oluşturmasını sağlar.
"""

import os
from werkzeug.security import generate_password_hash
from config import Config
from db import get_db_connection, is_postgres


REQUIRED_SCHEMA_COLUMNS = {
    'users': {'id', 'username', 'email', 'password_hash', 'initial_cash_balance', 'credit_card_limit'},
    'categories': {'id', 'user_id', 'name', 'type', 'monthly_budget'},
    'transactions': {'id', 'user_id', 'category_id', 'amount', 'transaction_date', 'description', 'payment_method', 'is_paid', 'receipt_path'},
    'portfolio_assets': {'id', 'user_id', 'asset_symbol', 'asset_type', 'total_quantity', 'average_cost', 'updated_at'},
    'asset_transactions': {'id', 'asset_id', 'user_id', 'transaction_type', 'quantity', 'unit_price', 'total_amount', 'realized_pnl', 'transaction_date'},
    'savings_goals': {'id', 'user_id', 'title', 'target_amount', 'current_amount', 'target_date', 'status'},
    'subscriptions': {'id', 'user_id', 'title', 'amount', 'billing_cycle', 'next_due_date', 'is_active', 'payment_method', 'auto_apply', 'last_synced_period'},
    'recurring_transactions': {'id', 'user_id', 'category_id', 'title', 'amount', 'type', 'payment_method', 'day_of_month', 'is_active', 'last_synced_period'},
}


def connect_master_and_ensure_database():
    """Veritabanı sunucusunu doğrular; MSSQL'de wealth_tracker_db yoksa oluşturur."""
    if is_postgres():
        print("[OK] PostgreSQL (Supabase) modu aktif: DATABASE_URL üzerinden bağlanılıyor.")
        return
    db_name = Config.DB_DATABASE
    conn = get_db_connection(database='master', autocommit=True)
    try:
        cursor = conn.cursor()
        cursor.execute(f"IF DB_ID('{db_name}') IS NULL CREATE DATABASE [{db_name}];")
        cursor.close()
        resolved_srv = Config._resolved_server or Config.DB_SERVER
        print(f"[OK] Veritabanı ve sunucu doğrulandı: {db_name} (Sunucu: {resolved_srv})")
    finally:
        conn.close()


def _ensure_postgres_schema_and_objects():
    """schema_postgres.sql dosyasını çalıştırarak Supabase / PostgreSQL tablolarını, View ve fonksiyonunu kurar."""
    schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'schema_postgres.sql')
    with open(schema_path, 'r', encoding='utf-8') as f:
        schema_sql = f.read()

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # Ham psycopg2 imleci çoklu DDL deyimlerini tek seferde güvenle yürütür
        raw_cursor = conn._conn.cursor() if hasattr(conn, '_conn') else cursor
        raw_cursor.execute(schema_sql)
        if raw_cursor is not cursor:
            raw_cursor.close()
        conn.commit()
        print("[OK] schema_postgres.sql başarıyla uygulandı (8 tablo, indeksler, View ve PostgreSQL fonksiyonu).")

        # information_schema.columns doğrulaması
        for tbl_name, req_cols in REQUIRED_SCHEMA_COLUMNS.items():
            cursor.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = ?",
                (tbl_name,),
            )
            existing_cols = {str(r[0]).lower() for r in cursor.fetchall()}
            missing = req_cols - existing_cols
            if missing:
                raise RuntimeError(f"PostgreSQL şema doğrulama hatası: '{tbl_name}' tablosunda eksik sütunlar: {missing}")
        print("[OK] PostgreSQL information_schema.columns doğrulaması %100 başarılı.")

        ensure_default_seed_data(cursor=cursor, conn=conn)
    finally:
        cursor.close()
        conn.close()


def ensure_tables_and_objects():
    """Tüm tabloları, sütun migrasyonlarını, indeksleri, View ve SP nesnelerini oluşturur ve doğrular."""
    if is_postgres():
        _ensure_postgres_schema_and_objects()
        return

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # 1. 8 İlişkisel Tablonun Oluşturulması (Canlı Şema & app.py ile %100 Birebir)
        cursor.execute("""
            IF OBJECT_ID('dbo.users', 'U') IS NULL
            BEGIN
                CREATE TABLE dbo.users (
                    id INT IDENTITY(1,1) PRIMARY KEY,
                    username NVARCHAR(100) NOT NULL UNIQUE,
                    email NVARCHAR(150) NOT NULL UNIQUE,
                    password_hash NVARCHAR(255) NOT NULL,
                    first_name NVARCHAR(100) NULL,
                    last_name NVARCHAR(100) NULL,
                    created_at DATETIME2 NOT NULL DEFAULT GETDATE(),
                    telegram_bot_token NVARCHAR(255) NULL,
                    telegram_chat_id NVARCHAR(100) NULL,
                    email_notifications BIT NOT NULL DEFAULT 1,
                    initial_cash_balance DECIMAL(18,2) NOT NULL DEFAULT 0.00,
                    credit_card_limit DECIMAL(18,2) NOT NULL DEFAULT 50000.00
                );
            END

            IF OBJECT_ID('dbo.categories', 'U') IS NULL
            BEGIN
                CREATE TABLE dbo.categories (
                    id INT IDENTITY(1,1) PRIMARY KEY,
                    user_id INT NOT NULL,
                    name NVARCHAR(100) NOT NULL,
                    type NVARCHAR(20) NOT NULL,
                    monthly_budget DECIMAL(18,2) NOT NULL DEFAULT 0.00,
                    created_at DATETIME2 NOT NULL DEFAULT GETDATE(),
                    CONSTRAINT FK_categories_users FOREIGN KEY (user_id) REFERENCES dbo.users(id) ON DELETE CASCADE
                );
            END

            IF OBJECT_ID('dbo.transactions', 'U') IS NULL
            BEGIN
                CREATE TABLE dbo.transactions (
                    id INT IDENTITY(1,1) PRIMARY KEY,
                    user_id INT NOT NULL,
                    category_id INT NOT NULL,
                    amount DECIMAL(18,2) NOT NULL,
                    transaction_date DATE NOT NULL DEFAULT CAST(GETDATE() AS DATE),
                    description NVARCHAR(500) NULL,
                    created_at DATETIME2 NOT NULL DEFAULT GETDATE(),
                    payment_method NVARCHAR(20) NOT NULL DEFAULT 'nakit',
                    is_paid BIT NOT NULL DEFAULT 1,
                    receipt_path NVARCHAR(500) NULL,
                    CONSTRAINT FK_transactions_users FOREIGN KEY (user_id) REFERENCES dbo.users(id),
                    CONSTRAINT FK_transactions_categories FOREIGN KEY (category_id) REFERENCES dbo.categories(id)
                );
            END

            IF OBJECT_ID('dbo.portfolio_assets', 'U') IS NULL
            BEGIN
                CREATE TABLE dbo.portfolio_assets (
                    id INT IDENTITY(1,1) PRIMARY KEY,
                    user_id INT NOT NULL,
                    asset_symbol NVARCHAR(30) NOT NULL,
                    asset_type NVARCHAR(30) NOT NULL,
                    total_quantity DECIMAL(18,6) NOT NULL DEFAULT 0,
                    average_cost DECIMAL(18,6) NOT NULL DEFAULT 0,
                    updated_at DATETIME2 NOT NULL DEFAULT GETDATE(),
                    CONSTRAINT FK_portfolio_assets_users FOREIGN KEY (user_id) REFERENCES dbo.users(id) ON DELETE CASCADE
                );
            END

            IF OBJECT_ID('dbo.asset_transactions', 'U') IS NULL
            BEGIN
                CREATE TABLE dbo.asset_transactions (
                    id INT IDENTITY(1,1) PRIMARY KEY,
                    asset_id INT NOT NULL,
                    user_id INT NOT NULL,
                    transaction_type NVARCHAR(20) NOT NULL,
                    quantity DECIMAL(18,6) NOT NULL,
                    unit_price DECIMAL(18,6) NOT NULL,
                    total_amount DECIMAL(18,2) NOT NULL DEFAULT 0.00,
                    realized_pnl DECIMAL(18,2) NOT NULL DEFAULT 0.00,
                    transaction_date DATE NOT NULL DEFAULT CAST(GETDATE() AS DATE),
                    created_at DATETIME2 NOT NULL DEFAULT GETDATE(),
                    CONSTRAINT FK_asset_tx_portfolio FOREIGN KEY (asset_id) REFERENCES dbo.portfolio_assets(id),
                    CONSTRAINT FK_asset_tx_users FOREIGN KEY (user_id) REFERENCES dbo.users(id)
                );
            END

            IF OBJECT_ID('dbo.savings_goals', 'U') IS NULL
            BEGIN
                CREATE TABLE dbo.savings_goals (
                    id INT IDENTITY(1,1) PRIMARY KEY,
                    user_id INT NOT NULL,
                    title NVARCHAR(150) NOT NULL,
                    target_amount DECIMAL(18,2) NOT NULL,
                    current_amount DECIMAL(18,2) NOT NULL DEFAULT 0.00,
                    target_date DATE NULL,
                    status NVARCHAR(30) NOT NULL DEFAULT 'devam_ediyor',
                    created_at DATETIME2 NOT NULL DEFAULT GETDATE(),
                    CONSTRAINT FK_savings_goals_users FOREIGN KEY (user_id) REFERENCES dbo.users(id) ON DELETE CASCADE
                );
            END

            IF OBJECT_ID('dbo.subscriptions', 'U') IS NULL
            BEGIN
                CREATE TABLE dbo.subscriptions (
                    id INT IDENTITY(1,1) PRIMARY KEY,
                    user_id INT NOT NULL,
                    title NVARCHAR(150) NOT NULL,
                    amount DECIMAL(18,2) NOT NULL,
                    billing_cycle NVARCHAR(50) NOT NULL DEFAULT 'aylik',
                    next_due_date DATE NULL,
                    is_active BIT NOT NULL DEFAULT 1,
                    created_at DATETIME2 NOT NULL DEFAULT GETDATE(),
                    payment_method NVARCHAR(50) NOT NULL DEFAULT 'nakit',
                    auto_apply BIT NOT NULL DEFAULT 1,
                    last_synced_period NVARCHAR(7) NULL,
                    CONSTRAINT FK_subscriptions_users FOREIGN KEY (user_id) REFERENCES dbo.users(id) ON DELETE CASCADE
                );
            END

            IF OBJECT_ID('dbo.recurring_transactions', 'U') IS NULL
            BEGIN
                CREATE TABLE dbo.recurring_transactions (
                    id INT IDENTITY(1,1) PRIMARY KEY,
                    user_id INT NOT NULL,
                    category_id INT NULL,
                    title NVARCHAR(150) NOT NULL,
                    amount DECIMAL(18,2) NOT NULL,
                    type NVARCHAR(20) NOT NULL,
                    payment_method NVARCHAR(20) NOT NULL DEFAULT 'nakit',
                    day_of_month INT NOT NULL DEFAULT 1,
                    is_active BIT NOT NULL DEFAULT 1,
                    created_at DATETIME2 NOT NULL DEFAULT GETDATE(),
                    last_synced_period NVARCHAR(7) NULL,
                    CONSTRAINT FK_recurring_tx_users FOREIGN KEY (user_id) REFERENCES dbo.users(id) ON DELETE CASCADE,
                    CONSTRAINT FK_recurring_tx_categories FOREIGN KEY (category_id) REFERENCES dbo.categories(id)
                );
            END
        """)
        conn.commit()
        print("[OK] 8 ilişkisel tablo (users, categories, transactions, portfolio_assets, asset_transactions, savings_goals, subscriptions, recurring_transactions) doğrulandı.")

        # 2. Sütun Migrasyonları ve Performans İndeksleri
        cursor.execute("""
            IF COL_LENGTH('users', 'initial_cash_balance') IS NULL
                ALTER TABLE users ADD initial_cash_balance DECIMAL(18,2) NOT NULL DEFAULT 0.00;
            IF COL_LENGTH('users', 'credit_card_limit') IS NULL
                ALTER TABLE users ADD credit_card_limit DECIMAL(18,2) NOT NULL DEFAULT 50000.00;
            IF COL_LENGTH('transactions', 'receipt_path') IS NULL
                ALTER TABLE transactions ADD receipt_path NVARCHAR(500) NULL;
            IF COL_LENGTH('portfolio_assets', 'updated_at') IS NULL
                ALTER TABLE portfolio_assets ADD updated_at DATETIME2 NOT NULL DEFAULT GETDATE();
            IF COL_LENGTH('subscriptions', 'payment_method') IS NULL
                ALTER TABLE subscriptions ADD payment_method NVARCHAR(50) NOT NULL DEFAULT 'nakit';
            IF COL_LENGTH('subscriptions', 'auto_apply') IS NULL
                ALTER TABLE subscriptions ADD auto_apply BIT NOT NULL DEFAULT 1;
            IF COL_LENGTH('subscriptions', 'last_synced_period') IS NULL
                ALTER TABLE subscriptions ADD last_synced_period NVARCHAR(7) NULL;
            IF COL_LENGTH('recurring_transactions', 'last_synced_period') IS NULL
                ALTER TABLE recurring_transactions ADD last_synced_period NVARCHAR(7) NULL;

            IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_transactions_user_date' AND object_id = OBJECT_ID('transactions'))
                CREATE INDEX IX_transactions_user_date ON transactions(user_id, transaction_date DESC);

            IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_portfolio_assets_user_sym' AND object_id = OBJECT_ID('portfolio_assets'))
                CREATE INDEX IX_portfolio_assets_user_sym ON portfolio_assets(user_id, asset_symbol);
        """)
        conn.commit()

        # 2.1 INFORMATION_SCHEMA.COLUMNS Doğrulaması
        cursor.execute("SELECT TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS")
        live_cols = {}
        for t_name, c_name in cursor.fetchall():
            live_cols.setdefault(t_name.lower(), set()).add(c_name.lower())

        for req_table, req_cols in REQUIRED_SCHEMA_COLUMNS.items():
            actual = live_cols.get(req_table, set())
            missing = req_cols - actual
            if missing:
                raise RuntimeError(f"Şema doğrulama hatası! '{req_table}' tablosunda eksik sütunlar: {missing}")

        print("[OK] INFORMATION_SCHEMA.COLUMNS şema bütünlüğü ve performans indeksleri (%100 uyum) doğrulandı.")

        # 3. View (vw_user_financial_summary)
        cursor.execute("""
            CREATE OR ALTER VIEW vw_user_financial_summary AS
            SELECT 
                u.id AS user_id,
                u.username,
                ISNULL(u.initial_cash_balance, 0.00) AS initial_cash_balance,
                ISNULL(u.credit_card_limit, 50000.00) AS credit_card_limit,
                ISNULL(inc.total_income, 0.00) AS total_income,
                ISNULL(exp.total_cash_expense, 0.00) AS total_cash_expense,
                ISNULL(cc.unpaid_cc_debt, 0.00) AS unpaid_cc_debt,
                ISNULL(sav.total_savings, 0.00) AS total_savings,
                ISNULL(pa.active_asset_count, 0) AS active_asset_count
            FROM users u
            LEFT JOIN (
                SELECT t.user_id, SUM(t.amount) AS total_income
                FROM transactions t
                JOIN categories c ON t.category_id = c.id
                WHERE c.type = 'gelir'
                GROUP BY t.user_id
            ) inc ON u.id = inc.user_id
            LEFT JOIN (
                SELECT t.user_id, SUM(t.amount) AS total_cash_expense
                FROM transactions t
                JOIN categories c ON t.category_id = c.id
                WHERE c.type = 'gider' AND ISNULL(t.payment_method, 'nakit') = 'nakit'
                GROUP BY t.user_id
            ) exp ON u.id = exp.user_id
            LEFT JOIN (
                SELECT t.user_id, SUM(t.amount) AS unpaid_cc_debt
                FROM transactions t
                WHERE t.payment_method = 'kredi_karti' AND ISNULL(t.is_paid, 0) = 0
                GROUP BY t.user_id
            ) cc ON u.id = cc.user_id
            LEFT JOIN (
                SELECT sg.user_id, SUM(sg.current_amount) AS total_savings
                FROM savings_goals sg
                WHERE sg.status = 'devam_ediyor'
                GROUP BY sg.user_id
            ) sav ON u.id = sav.user_id
            LEFT JOIN (
                SELECT pa.user_id, COUNT(*) AS active_asset_count
                FROM portfolio_assets pa
                WHERE pa.total_quantity > 0
                GROUP BY pa.user_id
            ) pa ON u.id = pa.user_id
        """)
        conn.commit()

        # 4. Stored Procedure (sp_get_user_budget_health)
        cursor.execute("""
            CREATE OR ALTER PROCEDURE sp_get_user_budget_health
                @user_id INT,
                @month INT,
                @year INT
            AS
            BEGIN
                SET NOCOUNT ON;

                SELECT 
                    c.id AS category_id,
                    c.name AS category_name,
                    ISNULL(c.monthly_budget, 0.00) AS monthly_budget,
                    ISNULL(spent.total_spent, 0.00) AS total_spent,
                    CASE 
                        WHEN ISNULL(c.monthly_budget, 0.00) > 0 
                        THEN c.monthly_budget - ISNULL(spent.total_spent, 0.00)
                        ELSE 0.00 
                    END AS remaining_budget,
                    CASE 
                        WHEN ISNULL(c.monthly_budget, 0.00) > 0 
                        THEN ROUND((ISNULL(spent.total_spent, 0.00) / c.monthly_budget) * 100.0, 2)
                        ELSE 0.00 
                    END AS budget_usage_pct,
                    CASE 
                        WHEN ISNULL(c.monthly_budget, 0.00) <= 0 THEN 'TANIMSIZ'
                        WHEN ISNULL(spent.total_spent, 0.00) > c.monthly_budget THEN 'ASILDI'
                        WHEN (ISNULL(spent.total_spent, 0.00) / c.monthly_budget) >= 0.85 THEN 'KRITIK'
                        ELSE 'NORMAL'
                    END AS budget_status
                FROM categories c
                LEFT JOIN (
                    SELECT 
                        t.category_id,
                        SUM(t.amount) AS total_spent
                    FROM transactions t
                    WHERE t.user_id = @user_id 
                      AND MONTH(t.transaction_date) = @month 
                      AND YEAR(t.transaction_date) = @year
                    GROUP BY t.category_id
                ) spent ON c.id = spent.category_id
                WHERE c.user_id = @user_id AND c.type = 'gider'
                ORDER BY total_spent DESC, c.name ASC;
            END
        """)
        conn.commit()
        print("[OK] Kurumsal SQL View (vw_user_financial_summary) ve Stored Procedure (sp_get_user_budget_health) oluşturuldu.")

        ensure_default_seed_data(cursor=cursor, conn=conn)

    finally:
        cursor.close()
        conn.close()


def ensure_default_seed_data(cursor=None, conn=None):
    """İlk kurulum (Zero-User) kontrolü: Hiç kullanıcı yoksa varsayılan kullanıcı ve kategorileri oluşturur."""
    own_conn = False
    if conn is None or cursor is None:
        conn = get_db_connection()
        cursor = conn.cursor()
        own_conn = True

    try:
        cursor.execute("SELECT COUNT(*) FROM users")
        user_count = cursor.fetchone()[0]
        if user_count == 0:
            pwd_hash = generate_password_hash("123456")
            cursor.execute("""
                INSERT INTO users (username, email, password_hash, first_name, last_name, initial_cash_balance, credit_card_limit)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, ("efoss13", "efoss13@moneta.local", pwd_hash, "Efe", "Moneta", 0.00, 50000.00))
            conn.commit()

            cursor.execute("SELECT id FROM users WHERE username = 'efoss13'")
            new_uid = cursor.fetchone()[0]

            default_categories = [
                (new_uid, 'Maaş & Gelir', 'gelir', 0.0),
                (new_uid, 'Ek Gelir & Prim', 'gelir', 0.0),
                (new_uid, 'Yatırım & Portföy', 'gelir', 0.0),
                (new_uid, 'Market & Gıda', 'gider', 5000.0),
                (new_uid, 'Kira & Barınma', 'gider', 10000.0),
                (new_uid, 'Ulaşım & Yakıt', 'gider', 3000.0),
                (new_uid, 'Eğlence & Sosyal', 'gider', 2500.0),
                (new_uid, 'Faturalar', 'gider', 1500.0),
                (new_uid, 'Sağlık & Kişisel Bakım', 'gider', 1500.0),
                (new_uid, 'Eğitim & Gelişim', 'gider', 2000.0),
            ]
            cursor.executemany("""
                INSERT INTO categories (user_id, name, type, monthly_budget)
                VALUES (?, ?, ?, ?)
            """, default_categories)
            conn.commit()
            print("[OK] İlk kurulum tespit edildi: Varsayılan başlangıç kullanıcısı (efoss13 / 123456) ve temel kategoriler oluşturuldu.")
        else:
            print(f"[OK] Mevcut kullanıcı verileri korundu ({user_count} aktif kullanıcı kayıtlı).")
    finally:
        if own_conn:
            cursor.close()
            conn.close()


if __name__ == "__main__":
    print("=" * 72)
    print("  MONETA - Veritabanı Kurulum ve Şema Doğrulama Motoru Başlatılıyor...")
    print("=" * 72)
    connect_master_and_ensure_database()
    ensure_tables_and_objects()
    print("=" * 72)
    print("  [BAŞARILI] Moneta veritabanı kullanıma hazır! (`python app.py`)")
    print("=" * 72)
