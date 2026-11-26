/* ============================================================================
   MONETA - Akıllı Bütçe, Portföy ve Servet Yönetim Sistemi
   Veritabanı Şeması (PostgreSQL / Supabase)
   schema.sql (MSSQL) ve app.py ile %100 Birebir Senkronize
   ============================================================================ */

-- 1. Kullanıcılar Tablosu (users)
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(100) NOT NULL UNIQUE,
    email VARCHAR(150) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    first_name VARCHAR(100) NULL,
    last_name VARCHAR(100) NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    telegram_bot_token VARCHAR(255) NULL,
    telegram_chat_id VARCHAR(100) NULL,
    email_notifications BOOLEAN NOT NULL DEFAULT TRUE,
    initial_cash_balance NUMERIC(18,2) NOT NULL DEFAULT 0.00,
    credit_card_limit NUMERIC(18,2) NOT NULL DEFAULT 50000.00
);

-- 2. Harcama ve Gelir Kategorileri Tablosu (categories)
CREATE TABLE IF NOT EXISTS categories (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(100) NOT NULL,
    type VARCHAR(20) NOT NULL, -- 'gelir' veya 'gider'
    monthly_budget NUMERIC(18,2) NOT NULL DEFAULT 0.00,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 3. Finansal İşlemler Tablosu (transactions)
CREATE TABLE IF NOT EXISTS transactions (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id),
    category_id INT NOT NULL REFERENCES categories(id),
    amount NUMERIC(18,2) NOT NULL,
    transaction_date DATE NOT NULL DEFAULT CURRENT_DATE,
    description TEXT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    payment_method VARCHAR(20) NOT NULL DEFAULT 'nakit', -- 'nakit' veya 'kredi_karti'
    is_paid BOOLEAN NOT NULL DEFAULT TRUE,
    receipt_path VARCHAR(500) NULL
);

-- 4. Yatırım Portföyü Varlıkları Tablosu (portfolio_assets)
CREATE TABLE IF NOT EXISTS portfolio_assets (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    asset_symbol VARCHAR(30) NOT NULL,
    asset_type VARCHAR(30) NOT NULL, -- 'hisse', 'abd_hisse', 'emtia_altin', 'doviz', 'tefas_fon', 'befas_fon', 'fon'
    total_quantity NUMERIC(18,6) NOT NULL DEFAULT 0,
    average_cost NUMERIC(18,6) NOT NULL DEFAULT 0,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 5. Portföy Alış/Satış İşlem Geçmişi Tablosu (asset_transactions)
CREATE TABLE IF NOT EXISTS asset_transactions (
    id SERIAL PRIMARY KEY,
    asset_id INT NOT NULL REFERENCES portfolio_assets(id),
    user_id INT NOT NULL REFERENCES users(id),
    transaction_type VARCHAR(20) NOT NULL, -- 'alis' veya 'satis'
    quantity NUMERIC(18,6) NOT NULL,
    unit_price NUMERIC(18,6) NOT NULL,
    total_amount NUMERIC(18,2) NOT NULL DEFAULT 0.00,
    realized_pnl NUMERIC(18,2) NOT NULL DEFAULT 0.00,
    transaction_date DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 6. Birikim Hedefleri ve Kumbara Tablosu (savings_goals)
CREATE TABLE IF NOT EXISTS savings_goals (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title VARCHAR(150) NOT NULL,
    target_amount NUMERIC(18,2) NOT NULL,
    current_amount NUMERIC(18,2) NOT NULL DEFAULT 0.00,
    target_date DATE NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'devam_ediyor', -- 'devam_ediyor' veya 'tamamlandi'
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 7. Sabit Faturalar ve Abonelikler Tablosu (subscriptions)
CREATE TABLE IF NOT EXISTS subscriptions (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title VARCHAR(150) NOT NULL,
    amount NUMERIC(18,2) NOT NULL,
    billing_cycle VARCHAR(50) NOT NULL DEFAULT 'aylik',
    next_due_date DATE NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    payment_method VARCHAR(50) NOT NULL DEFAULT 'nakit',
    auto_apply BOOLEAN NOT NULL DEFAULT TRUE,
    last_synced_period VARCHAR(7) NULL
);

-- 8. Düzenli (Tekrarlayan) Gelir/Gider Şablonları Tablosu (recurring_transactions)
CREATE TABLE IF NOT EXISTS recurring_transactions (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category_id INT NULL REFERENCES categories(id),
    title VARCHAR(150) NOT NULL,
    amount NUMERIC(18,2) NOT NULL,
    type VARCHAR(20) NOT NULL, -- 'gelir' veya 'gider'
    payment_method VARCHAR(20) NOT NULL DEFAULT 'nakit',
    day_of_month INT NOT NULL DEFAULT 1,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_synced_period VARCHAR(7) NULL
);

-- 9. Performans İndeksleri
CREATE INDEX IF NOT EXISTS ix_transactions_user_date
    ON transactions (user_id, transaction_date DESC);

CREATE INDEX IF NOT EXISTS ix_portfolio_assets_user_sym
    ON portfolio_assets (user_id, asset_symbol);

-- 10. Kurumsal SQL View: vw_user_financial_summary
CREATE OR REPLACE VIEW vw_user_financial_summary AS
SELECT
    u.id AS user_id,
    u.username,
    COALESCE(u.initial_cash_balance, 0.00) AS initial_cash_balance,
    COALESCE(u.credit_card_limit, 50000.00) AS credit_card_limit,
    COALESCE(inc.total_income, 0.00) AS total_income,
    COALESCE(exp.total_cash_expense, 0.00) AS total_cash_expense,
    COALESCE(cc.unpaid_cc_debt, 0.00) AS unpaid_cc_debt,
    COALESCE(sav.total_savings, 0.00) AS total_savings,
    COALESCE(pa.active_asset_count, 0) AS active_asset_count
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
    WHERE c.type = 'gider' AND COALESCE(t.payment_method, 'nakit') = 'nakit'
    GROUP BY t.user_id
) exp ON u.id = exp.user_id
LEFT JOIN (
    SELECT t.user_id, SUM(t.amount) AS unpaid_cc_debt
    FROM transactions t
    WHERE t.payment_method = 'kredi_karti' AND COALESCE(t.is_paid, FALSE) = FALSE
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
) pa ON u.id = pa.user_id;

-- 11. Kurumsal PostgreSQL Fonksiyonu: sp_get_user_budget_health
CREATE OR REPLACE FUNCTION sp_get_user_budget_health(
    p_user_id INT,
    p_month INT,
    p_year INT
)
RETURNS TABLE (
    category_id INT,
    category_name VARCHAR(100),
    monthly_budget NUMERIC(18,2),
    total_spent NUMERIC(18,2),
    remaining_budget NUMERIC(18,2),
    budget_usage_pct NUMERIC(10,2),
    budget_status VARCHAR(20)
)
LANGUAGE sql
STABLE
AS $$
    SELECT
        c.id AS category_id,
        c.name AS category_name,
        COALESCE(c.monthly_budget, 0.00)::NUMERIC(18,2) AS monthly_budget,
        COALESCE(spent.total_spent, 0.00)::NUMERIC(18,2) AS total_spent,
        CASE
            WHEN COALESCE(c.monthly_budget, 0.00) > 0
            THEN (c.monthly_budget - COALESCE(spent.total_spent, 0.00))::NUMERIC(18,2)
            ELSE 0.00::NUMERIC(18,2)
        END AS remaining_budget,
        CASE
            WHEN COALESCE(c.monthly_budget, 0.00) > 0
            THEN ROUND((COALESCE(spent.total_spent, 0.00) / c.monthly_budget) * 100.0, 2)::NUMERIC(10,2)
            ELSE 0.00::NUMERIC(10,2)
        END AS budget_usage_pct,
        CASE
            WHEN COALESCE(c.monthly_budget, 0.00) <= 0 THEN 'TANIMSIZ'
            WHEN COALESCE(spent.total_spent, 0.00) > c.monthly_budget THEN 'ASILDI'
            WHEN (COALESCE(spent.total_spent, 0.00) / c.monthly_budget) >= 0.85 THEN 'KRITIK'
            ELSE 'NORMAL'
        END::VARCHAR(20) AS budget_status
    FROM categories c
    LEFT JOIN (
        SELECT
            t.category_id,
            SUM(t.amount) AS total_spent
        FROM transactions t
        WHERE t.user_id = p_user_id
          AND EXTRACT(MONTH FROM t.transaction_date)::INT = p_month
          AND EXTRACT(YEAR FROM t.transaction_date)::INT = p_year
        GROUP BY t.category_id
    ) spent ON c.id = spent.category_id
    WHERE c.user_id = p_user_id AND c.type = 'gider'
    ORDER BY total_spent DESC, c.name ASC;
$$;
