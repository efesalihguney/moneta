/* ============================================================================
   MONETA - Akıllı Bütçe, Portföy ve Servet Yönetim Sistemi
   Veritabanı Şeması (T-SQL / Microsoft SQL Server)
   Canlı INFORMATION_SCHEMA.COLUMNS ve app.py ile %100 Birebir Senkronize
   ============================================================================ */

-- 1. Veritabanı Kontrolü ve Oluşturulması
IF DB_ID('wealth_tracker_db') IS NULL
BEGIN
    CREATE DATABASE wealth_tracker_db;
END
GO

USE wealth_tracker_db;
GO

-- 2. Kullanıcılar Tablosu (users)
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
GO

-- 3. Harcama ve Gelir Kategorileri Tablosu (categories)
IF OBJECT_ID('dbo.categories', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.categories (
        id INT IDENTITY(1,1) PRIMARY KEY,
        user_id INT NOT NULL,
        name NVARCHAR(100) NOT NULL,
        type NVARCHAR(20) NOT NULL, -- 'gelir' veya 'gider'
        monthly_budget DECIMAL(18,2) NOT NULL DEFAULT 0.00,
        created_at DATETIME2 NOT NULL DEFAULT GETDATE(),
        CONSTRAINT FK_categories_users FOREIGN KEY (user_id) REFERENCES dbo.users(id) ON DELETE CASCADE
    );
END
GO

-- 4. Finansal İşlemler Tablosu (transactions)
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
        payment_method NVARCHAR(20) NOT NULL DEFAULT 'nakit', -- 'nakit' veya 'kredi_karti'
        is_paid BIT NOT NULL DEFAULT 1,
        receipt_path NVARCHAR(500) NULL,
        CONSTRAINT FK_transactions_users FOREIGN KEY (user_id) REFERENCES dbo.users(id),
        CONSTRAINT FK_transactions_categories FOREIGN KEY (category_id) REFERENCES dbo.categories(id)
    );
END
GO

-- 5. Yatırım Portföyü Varlıkları Tablosu (portfolio_assets)
IF OBJECT_ID('dbo.portfolio_assets', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.portfolio_assets (
        id INT IDENTITY(1,1) PRIMARY KEY,
        user_id INT NOT NULL,
        asset_symbol NVARCHAR(30) NOT NULL,
        asset_type NVARCHAR(30) NOT NULL, -- 'hisse', 'abd_hisse', 'emtia_altin', 'doviz', 'tefas_fon', 'befas_fon', 'fon'
        total_quantity DECIMAL(18,6) NOT NULL DEFAULT 0,
        average_cost DECIMAL(18,6) NOT NULL DEFAULT 0,
        updated_at DATETIME2 NOT NULL DEFAULT GETDATE(),
        CONSTRAINT FK_portfolio_assets_users FOREIGN KEY (user_id) REFERENCES dbo.users(id) ON DELETE CASCADE
    );
END
GO

-- 6. Portföy Alış/Satış İşlem Geçmişi Tablosu (asset_transactions)
IF OBJECT_ID('dbo.asset_transactions', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.asset_transactions (
        id INT IDENTITY(1,1) PRIMARY KEY,
        asset_id INT NOT NULL,
        user_id INT NOT NULL,
        transaction_type NVARCHAR(20) NOT NULL, -- 'alis' veya 'satis'
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
GO

-- 7. Birikim Hedefleri ve Kumbara Tablosu (savings_goals)
IF OBJECT_ID('dbo.savings_goals', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.savings_goals (
        id INT IDENTITY(1,1) PRIMARY KEY,
        user_id INT NOT NULL,
        title NVARCHAR(150) NOT NULL,
        target_amount DECIMAL(18,2) NOT NULL,
        current_amount DECIMAL(18,2) NOT NULL DEFAULT 0.00,
        target_date DATE NULL,
        status NVARCHAR(30) NOT NULL DEFAULT 'devam_ediyor', -- 'devam_ediyor' veya 'tamamlandi'
        created_at DATETIME2 NOT NULL DEFAULT GETDATE(),
        CONSTRAINT FK_savings_goals_users FOREIGN KEY (user_id) REFERENCES dbo.users(id) ON DELETE CASCADE
    );
END
GO

-- 8. Sabit Faturalar ve Abonelikler Tablosu (subscriptions)
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
GO

-- 9. Düzenli (Tekrarlayan) Gelir/Gider Şablonları Tablosu (recurring_transactions)
IF OBJECT_ID('dbo.recurring_transactions', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.recurring_transactions (
        id INT IDENTITY(1,1) PRIMARY KEY,
        user_id INT NOT NULL,
        category_id INT NULL,
        title NVARCHAR(150) NOT NULL,
        amount DECIMAL(18,2) NOT NULL,
        type NVARCHAR(20) NOT NULL, -- 'gelir' veya 'gider'
        payment_method NVARCHAR(20) NOT NULL DEFAULT 'nakit',
        day_of_month INT NOT NULL DEFAULT 1,
        is_active BIT NOT NULL DEFAULT 1,
        created_at DATETIME2 NOT NULL DEFAULT GETDATE(),
        last_synced_period NVARCHAR(7) NULL,
        CONSTRAINT FK_recurring_tx_users FOREIGN KEY (user_id) REFERENCES dbo.users(id) ON DELETE CASCADE,
        CONSTRAINT FK_recurring_tx_categories FOREIGN KEY (category_id) REFERENCES dbo.categories(id)
    );
END
GO

-- 10. Geriye Dönük Şema Migrasyon Korumaları (Mevcut Tablolar İçin)
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
GO

-- 11. Performans İndeksleri
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_transactions_user_date' AND object_id = OBJECT_ID('dbo.transactions'))
BEGIN
    CREATE INDEX IX_transactions_user_date ON dbo.transactions(user_id, transaction_date DESC);
END
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_portfolio_assets_user_sym' AND object_id = OBJECT_ID('dbo.portfolio_assets'))
BEGIN
    CREATE INDEX IX_portfolio_assets_user_sym ON dbo.portfolio_assets(user_id, asset_symbol);
END
GO

-- 12. Kurumsal SQL View: vw_user_financial_summary
CREATE OR ALTER VIEW dbo.vw_user_financial_summary AS
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
FROM dbo.users u
LEFT JOIN (
    SELECT t.user_id, SUM(t.amount) AS total_income
    FROM dbo.transactions t
    JOIN dbo.categories c ON t.category_id = c.id
    WHERE c.type = 'gelir'
    GROUP BY t.user_id
) inc ON u.id = inc.user_id
LEFT JOIN (
    SELECT t.user_id, SUM(t.amount) AS total_cash_expense
    FROM dbo.transactions t
    JOIN dbo.categories c ON t.category_id = c.id
    WHERE c.type = 'gider' AND ISNULL(t.payment_method, 'nakit') = 'nakit'
    GROUP BY t.user_id
) exp ON u.id = exp.user_id
LEFT JOIN (
    SELECT t.user_id, SUM(t.amount) AS unpaid_cc_debt
    FROM dbo.transactions t
    WHERE t.payment_method = 'kredi_karti' AND ISNULL(t.is_paid, 0) = 0
    GROUP BY t.user_id
) cc ON u.id = cc.user_id
LEFT JOIN (
    SELECT sg.user_id, SUM(sg.current_amount) AS total_savings
    FROM dbo.savings_goals sg
    WHERE sg.status = 'devam_ediyor'
    GROUP BY sg.user_id
) sav ON u.id = sav.user_id
LEFT JOIN (
    SELECT pa.user_id, COUNT(*) AS active_asset_count
    FROM dbo.portfolio_assets pa
    WHERE pa.total_quantity > 0
    GROUP BY pa.user_id
) pa ON u.id = pa.user_id;
GO

-- 13. Kurumsal Stored Procedure: sp_get_user_budget_health
CREATE OR ALTER PROCEDURE dbo.sp_get_user_budget_health
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
    FROM dbo.categories c
    LEFT JOIN (
        SELECT 
            t.category_id,
            SUM(t.amount) AS total_spent
        FROM dbo.transactions t
        WHERE t.user_id = @user_id 
          AND MONTH(t.transaction_date) = @month 
          AND YEAR(t.transaction_date) = @year
        GROUP BY t.category_id
    ) spent ON c.id = spent.category_id
    WHERE c.user_id = @user_id AND c.type = 'gider'
    ORDER BY total_spent DESC, c.name ASC;
END
GO
