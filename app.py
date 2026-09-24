from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from datetime import datetime
import json
import time
import urllib.request
import re
import yfinance as yf
from db import get_db_connection

app = Flask(__name__)
app.secret_key = "super_secret_wealth_tracker_key_change_in_production"

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash("Lütfen önce giriş yapın.", "warning")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# --- CANLI FİYAT ÇEKİCİ MOTOR & ÖNBELLEK (CACHE) ---
PRICE_CACHE = {}  # { (clean_sym, asset_type): (data, timestamp) }
CACHE_TTL = 300   # 5 dakika (300 saniye)

def get_usdtry_rate():
    """USDTRY kurunu döner; önbellek desteklidir."""
    now = time.time()
    usd_key = ('USDTRY=X', 'doviz')
    if usd_key in PRICE_CACHE and (now - PRICE_CACHE[usd_key][1] < CACHE_TTL):
        return PRICE_CACHE[usd_key][0]
    try:
        t_usd = yf.Ticker('USDTRY=X')
        p_usd = t_usd.fast_info.get('lastPrice')
        if p_usd:
            rate = float(p_usd)
            PRICE_CACHE[usd_key] = (rate, now)
            return rate
    except Exception:
        pass
    return None

def fetch_live_price(symbol, asset_type):
    """
    BIST, ABD Borsası, TEFAS/BEFAS Fonları, Altın ve Döviz için anlık fiyat çeker.
    5 dakikalık önbellek kullanır.
    """
    try:
        clean_sym = symbol.strip().upper()
        cache_key = (clean_sym, asset_type)
        now = time.time()
        if cache_key in PRICE_CACHE:
            cached_data, cached_time = PRICE_CACHE[cache_key]
            if now - cached_time < CACHE_TTL:
                return cached_data

        # 1. TEFAS & BEFAS Fonları (Örn: TI2, TTE, MAC, YAY, AFT, ALR, CHH)
        if asset_type in ['tefas_fon', 'befas_fon', 'fon']:
            url = f"https://www.tefas.gov.tr/FonAnaliz.aspx?FonKod={clean_sym}"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
            html = urllib.request.urlopen(req, timeout=8).read().decode('utf-8', errors='ignore')
            m = re.search(r'Son Fiyat \(TL\).*?>\s*([0-9]+,[0-9]+)\s*</p>', html, re.DOTALL)
            if m:
                val = round(float(m.group(1).replace(',', '.')), 6)
                PRICE_CACHE[cache_key] = (val, now)
                return val
            return None

        # 2. ABD Borsası (US Stocks / ETFs: AAPL, MSFT, NVDA, TSLA, SPY, QQQ)
        if asset_type == 'abd_hisse':
            ticker = yf.Ticker(clean_sym)
            usd_price = ticker.fast_info.get('lastPrice')
            if usd_price:
                usd_val = round(float(usd_price), 2)
                usdtry = get_usdtry_rate() or 1.0
                tl_val = round(usd_val * usdtry, 2)
                data = {
                    'tl_price': tl_val,
                    'orig_price': usd_val,
                    'currency': 'USD'
                }
                PRICE_CACHE[cache_key] = (data, now)
                return data
            return None

        # 3. Altın (Gram Altın: Ons GC=F * USDTRY / 31.1035)
        if asset_type == 'emtia_altin':
            ticker_gold = yf.Ticker('GC=F')
            p_gold = ticker_gold.fast_info.get('lastPrice')
            usdtry = get_usdtry_rate()
            if p_gold and usdtry:
                price = round((float(p_gold) * usdtry) / 31.1035, 2)
                PRICE_CACHE[cache_key] = (price, now)
                return price
            return None

        # 4. Borsa İstanbul ve Döviz Kurları
        ticker_str = clean_sym
        if asset_type == 'hisse':
            if not ticker_str.endswith('.IS'):
                ticker_str = f"{clean_sym}.IS"
        elif asset_type == 'doviz':
            if clean_sym in ['USD', 'DOLAR']:
                ticker_str = 'USDTRY=X'
            elif clean_sym in ['EUR', 'EURO']:
                ticker_str = 'EURTRY=X'
            elif clean_sym in ['GBP']:
                ticker_str = 'GBPTRY=X'

        ticker = yf.Ticker(ticker_str)
        price = ticker.fast_info.get('lastPrice')
        if price:
            val = round(float(price), 2)
            PRICE_CACHE[cache_key] = (val, now)
            return val
    except Exception:
        pass
    return None

def get_or_create_category(cursor, user_id, name, cat_type, default_budget=0.0):
    """Kategori varsa ID'sini döner, yoksa otomatik oluşturur."""
    cursor.execute("SELECT id FROM categories WHERE user_id = ? AND name = ? AND type = ?", (user_id, name, cat_type))
    row = cursor.fetchone()
    if row:
        return row[0]
    cursor.execute("""
        INSERT INTO categories (user_id, name, type, monthly_budget)
        VALUES (?, ?, ?, ?)
    """, (user_id, name, cat_type, default_budget))
    cursor.execute("SELECT @@IDENTITY")
    return cursor.fetchone()[0]

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

# --- AUTH ---
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        first_name = request.form.get('first_name', '').strip()
        last_name = request.form.get('last_name', '').strip()
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')

        if not all([first_name, last_name, username, email, password]):
            flash("Tüm alanları doldurun.", "danger")
            return render_template('register.html')

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE username = ? OR email = ?", (username, email))
        if cursor.fetchone():
            flash("Bu kullanıcı adı veya e-posta zaten kullanımda.", "danger")
            cursor.close()
            conn.close()
            return render_template('register.html')

        password_hash = generate_password_hash(password)
        try:
            cursor.execute("""
                INSERT INTO users (username, email, password_hash, first_name, last_name)
                VALUES (?, ?, ?, ?, ?)
            """, (username, email, password_hash, first_name, last_name))
            conn.commit()

            cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
            new_user_id = cursor.fetchone()[0]

            default_categories = [
                (new_user_id, 'Maaş & Gelir', 'gelir', 0.0),
                (new_user_id, 'Market & Gıda', 'gider', 5000.0),
                (new_user_id, 'Kira & Barınma', 'gider', 10000.0),
                (new_user_id, 'Ulaşım & Yakıt', 'gider', 3000.0),
                (new_user_id, 'Eğlence & Sosyal', 'gider', 2500.0),
                (new_user_id, 'Faturalar', 'gider', 1500.0)
            ]
            cursor.executemany("""
                INSERT INTO categories (user_id, name, type, monthly_budget)
                VALUES (?, ?, ?, ?)
            """, default_categories)
            conn.commit()

            flash("Kayıt başarılı! Giriş yapabilirsiniz.", "success")
            return redirect(url_for('login'))
        except Exception as e:
            conn.rollback()
            flash(f"Hata: {e}", "danger")
        finally:
            cursor.close()
            conn.close()

    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        identifier = request.form.get('login_identifier', '').strip()
        password = request.form.get('password', '')

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, username, first_name, password_hash 
            FROM users WHERE username = ? OR email = ?
        """, (identifier, identifier))
        user = cursor.fetchone()
        cursor.close()
        conn.close()

        if user and check_password_hash(user[3], password):
            session['user_id'] = user[0]
            session['username'] = user[1]
            session['first_name'] = user[2]
            return redirect(url_for('dashboard'))
        flash("Giriş bilgileri hatalı.", "danger")

    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash("Çıkış yapıldı.", "info")
    return redirect(url_for('login'))

# --- ANA DASHBOARD ---
@app.route('/dashboard')
@login_required
def dashboard():
    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor()

    # Tarih Filtresi (Varsayılan: Mevcut Ay ve Yıl)
    now = datetime.today()
    selected_year = request.args.get('year', type=int) or now.year
    selected_month = request.args.get('month', type=int) or now.month
    if selected_month < 1 or selected_month > 12:
        selected_month = now.month

    prev_month = 12 if selected_month == 1 else selected_month - 1
    prev_year = selected_year - 1 if selected_month == 1 else selected_year
    next_month = 1 if selected_month == 12 else selected_month + 1
    next_year = selected_year + 1 if selected_month == 12 else selected_year

    month_names = {
        1: 'Ocak', 2: 'Şubat', 3: 'Mart', 4: 'Nisan', 5: 'Mayıs', 6: 'Haziran',
        7: 'Temmuz', 8: 'Ağustos', 9: 'Eylül', 10: 'Ekim', 11: 'Kasım', 12: 'Aralık'
    }
    selected_month_name = month_names.get(selected_month, '')

    # 1. Finansal Özet: Gelir, Nakit Gider, Kredi Kartı Gideri (Ödenmemiş Ekstre)
    cursor.execute("""
        SELECT 
            ISNULL(SUM(CASE WHEN c.type = 'gelir' THEN t.amount ELSE 0 END), 0) AS total_income,
            ISNULL(SUM(CASE WHEN c.type = 'gider' AND ISNULL(t.payment_method, 'nakit') = 'nakit' THEN t.amount ELSE 0 END), 0) AS cash_expense,
            ISNULL(SUM(CASE WHEN c.type = 'gider' AND t.payment_method = 'kredi_karti' AND ISNULL(t.is_paid, 0) = 0 THEN t.amount ELSE 0 END), 0) AS cc_expense
        FROM transactions t
        JOIN categories c ON t.category_id = c.id
        WHERE t.user_id = ? 
          AND MONTH(t.transaction_date) = ? 
          AND YEAR(t.transaction_date) = ?
    """, (user_id, selected_month, selected_year))
    row = cursor.fetchone()
    total_income = float(row[0])
    cash_expense = float(row[1])
    cc_expense = float(row[2])
    total_expense = cash_expense + cc_expense
    net_savings = total_income - total_expense

    # 2. Kategori Bütçe Limitleri ve Harcama İlerlemeleri
    cursor.execute("""
        SELECT 
            c.id, 
            c.name, 
            c.monthly_budget,
            ISNULL(SUM(t.amount), 0) AS spent
        FROM categories c
        LEFT JOIN transactions t ON c.id = t.category_id 
             AND MONTH(t.transaction_date) = ? 
             AND YEAR(t.transaction_date) = ?
        WHERE c.user_id = ? AND c.type = 'gider'
        GROUP BY c.id, c.name, c.monthly_budget
        ORDER BY spent DESC
    """, (selected_month, selected_year, user_id))
    budgets_raw = cursor.fetchall()
    
    category_budgets = []
    chart_category_labels = []
    chart_category_values = []

    for b in budgets_raw:
        cat_id = b[0]
        cat_name = b[1]
        budget = float(b[2])
        spent = float(b[3])
        pct = round((spent / budget * 100), 1) if budget > 0 else 0.0
        
        category_budgets.append({
            'id': cat_id,
            'name': cat_name,
            'budget': budget,
            'spent': spent,
            'percent': pct
        })

        if spent > 0:
            chart_category_labels.append(cat_name)
            chart_category_values.append(spent)

    # 3. Yaklaşan Faturalar & Kart Ödemeleri
    cursor.execute("""
        SELECT id, title, amount, billing_cycle, next_due_date,
               DATEDIFF(day, CAST(GETDATE() AS DATE), next_due_date) AS days_left
        FROM subscriptions
        WHERE user_id = ? AND is_active = 1
        ORDER BY next_due_date ASC
    """, (user_id,))
    subscriptions = cursor.fetchall()

    # 4. Tasarruf Hedefleri
    cursor.execute("""
        SELECT id, title, target_amount, current_amount, target_date
        FROM savings_goals
        WHERE user_id = ? AND status = 'devam_ediyor'
    """, (user_id,))
    goals_raw = cursor.fetchall()
    savings_goals = []
    for g in goals_raw:
        t_amt = float(g[2])
        c_amt = float(g[3])
        pct = round((c_amt / t_amt * 100), 1) if t_amt > 0 else 0.0
        savings_goals.append({
            'id': g[0],
            'title': g[1],
            'target_amount': t_amt,
            'current_amount': c_amt,
            'target_date': g[4],
            'percent': min(pct, 100.0)
        })

    # 5. Canlı Yatırım Portföyü
    cursor.execute("""
        SELECT id, asset_symbol, asset_type, total_quantity, average_cost
        FROM portfolio_assets
        WHERE user_id = ? AND total_quantity > 0
    """, (user_id,))
    assets_raw = cursor.fetchall()

    portfolio_items = []
    total_portfolio_value = 0.0
    total_portfolio_cost = 0.0

    for a in assets_raw:
        asset_id = a[0]
        symbol = a[1]
        atype = a[2]
        qty = float(a[3])
        avg_cost = float(a[4])

        price_info = fetch_live_price(symbol, atype)
        if price_info is None:
            live_price = avg_cost
            orig_price = None
            currency = 'TL'
        elif isinstance(price_info, dict):
            live_price = float(price_info['tl_price'])
            orig_price = float(price_info['orig_price'])
            currency = price_info.get('currency', 'TL')
        else:
            live_price = float(price_info)
            orig_price = None
            currency = 'TL'

        cost_val = qty * avg_cost
        cur_val = qty * live_price
        pnl = cur_val - cost_val
        pnl_pct = round((pnl / cost_val * 100), 2) if cost_val > 0 else 0.0

        total_portfolio_value += cur_val
        total_portfolio_cost += cost_val

        portfolio_items.append({
            'id': asset_id,
            'symbol': symbol,
            'type': atype,
            'qty': qty,
            'avg_cost': avg_cost,
            'live_price': live_price,
            'orig_price': orig_price,
            'currency': currency,
            'cur_val': cur_val,
            'pnl': pnl,
            'pnl_pct': pnl_pct
        })

    total_portfolio_pnl = total_portfolio_value - total_portfolio_cost
    total_portfolio_pnl_pct = round((total_portfolio_pnl / total_portfolio_cost * 100), 2) if total_portfolio_cost > 0 else 0.0

    # Toplam Realize Edilen Kâr / Zarar
    cursor.execute("""
        SELECT ISNULL(SUM(realized_pnl), 0) 
        FROM asset_transactions 
        WHERE user_id = ? AND transaction_type = 'satis'
    """, (user_id,))
    total_realized_pnl = float(cursor.fetchone()[0])

    # 6. İşlem Hareketleri (Seçilen Dönem)
    cursor.execute("""
        SELECT TOP 20 t.id, t.amount, t.transaction_date, t.description, c.name, c.type, t.payment_method, c.id, t.is_paid
        FROM transactions t
        JOIN categories c ON t.category_id = c.id
        WHERE t.user_id = ? 
          AND MONTH(t.transaction_date) = ? 
          AND YEAR(t.transaction_date) = ?
        ORDER BY t.transaction_date DESC, t.id DESC
    """, (user_id, selected_month, selected_year))
    recent_transactions = cursor.fetchall()

    # Kategori Listesi (İşlem modalı ve Kategori Yönetimi için)
    cursor.execute("SELECT id, name, type, monthly_budget FROM categories WHERE user_id = ? ORDER BY type, name", (user_id,))
    categories = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template(
        'dashboard.html',
        total_income=total_income,
        cash_expense=cash_expense,
        cc_expense=cc_expense,
        total_expense=total_expense,
        net_savings=net_savings,
        category_budgets=category_budgets,
        subscriptions=subscriptions,
        savings_goals=savings_goals,
        portfolio_items=portfolio_items,
        total_portfolio_value=total_portfolio_value,
        total_portfolio_pnl=total_portfolio_pnl,
        total_portfolio_pnl_pct=total_portfolio_pnl_pct,
        total_realized_pnl=total_realized_pnl,
        recent_transactions=recent_transactions,
        categories=categories,
        selected_month=selected_month,
        selected_year=selected_year,
        selected_month_name=selected_month_name,
        prev_month=prev_month,
        prev_year=prev_year,
        next_month=next_month,
        next_year=next_year,
        today_str=datetime.today().strftime('%Y-%m-%d'),
        chart_cat_labels=json.dumps(chart_category_labels),
        chart_cat_values=json.dumps(chart_category_values),
        chart_payment_values=json.dumps([cash_expense, cc_expense])
    )

# --- İŞLEMLER (ACTIONS) ---

@app.route('/add_transaction', methods=['POST'])
@login_required
def add_transaction():
    user_id = session['user_id']
    category_id = request.form.get('category_id')
    amount = request.form.get('amount')
    transaction_date = request.form.get('transaction_date')
    description = request.form.get('description', '').strip()
    payment_method = request.form.get('payment_method', 'nakit')
    is_paid = 0 if payment_method == 'kredi_karti' else 1

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO transactions (user_id, category_id, amount, transaction_date, description, payment_method, is_paid)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, category_id, float(amount), transaction_date, description, payment_method, is_paid))
        conn.commit()
        flash("İşlem kaydedildi.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Hata: {e}", "danger")
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for('dashboard'))

@app.route('/edit_transaction/<int:tx_id>', methods=['POST'])
@login_required
def edit_transaction(tx_id):
    user_id = session['user_id']
    category_id = request.form.get('category_id')
    amount = request.form.get('amount')
    transaction_date = request.form.get('transaction_date')
    description = request.form.get('description', '').strip()
    payment_method = request.form.get('payment_method', 'nakit')

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE transactions 
            SET category_id = ?, amount = ?, transaction_date = ?, description = ?, payment_method = ?
            WHERE id = ? AND user_id = ?
        """, (category_id, float(amount), transaction_date, description, payment_method, tx_id, user_id))
        conn.commit()
        flash("İşlem güncellendi.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Güncelleme hatası: {e}", "danger")
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for('dashboard'))

@app.route('/delete_transaction/<int:tx_id>', methods=['POST'])
@login_required
def delete_transaction(tx_id):
    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM transactions WHERE id = ? AND user_id = ?", (tx_id, user_id))
        conn.commit()
        flash("İşlem başarıyla silindi.", "info")
    except Exception as e:
        conn.rollback()
        flash(f"Silme hatası: {e}", "danger")
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for('dashboard'))

@app.route('/pay_credit_card', methods=['POST'])
@login_required
def pay_credit_card():
    user_id = session['user_id']
    month = request.form.get('month', type=int) or datetime.today().month
    year = request.form.get('year', type=int) or datetime.today().year

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT ISNULL(SUM(amount), 0) 
            FROM transactions 
            WHERE user_id = ? AND payment_method = 'kredi_karti' AND ISNULL(is_paid, 0) = 0
              AND MONTH(transaction_date) = ? AND YEAR(transaction_date) = ?
        """, (user_id, month, year))
        unpaid_cc = float(cursor.fetchone()[0])

        if unpaid_cc <= 0:
            flash("Bu döneme ait ödenecek kredi kartı ekstre borcu bulunmamaktadır.", "info")
            return redirect(url_for('dashboard', month=month, year=year))

        # 1. Kredi kartı harcamalarını ödendi olarak işaretle
        cursor.execute("""
            UPDATE transactions 
            SET is_paid = 1 
            WHERE user_id = ? AND payment_method = 'kredi_karti' AND ISNULL(is_paid, 0) = 0
              AND MONTH(transaction_date) = ? AND YEAR(transaction_date) = ?
        """, (user_id, month, year))

        # 2. Nakit akışından kredi kartı borç ödemesi olarak çıkış kaydet
        cat_id = get_or_create_category(cursor, user_id, 'Kredi Kartı Borç Ödemesi', 'gider')
        cursor.execute("""
            INSERT INTO transactions (user_id, category_id, amount, transaction_date, description, payment_method, is_paid)
            VALUES (?, ?, ?, CAST(GETDATE() AS DATE), ?, 'nakit', 1)
        """, (user_id, cat_id, unpaid_cc, f"{month}/{year} Kredi Kartı Ekstresi Ödemesi"))

        conn.commit()
        flash(f"{month}/{year} dönemi için {unpaid_cc:.2f} TL tutarındaki kredi kartı ekstresi nakit bakiyenizden başarıyla ödendi.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Ekstre ödeme hatası: {e}", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('dashboard', month=month, year=year))

@app.route('/add_subscription', methods=['POST'])
@login_required
def add_subscription():
    user_id = session['user_id']
    title = request.form.get('title', '').strip()
    amount = request.form.get('amount')
    next_due_date = request.form.get('next_due_date')

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO subscriptions (user_id, title, amount, billing_cycle, next_due_date, is_active)
            VALUES (?, ?, ?, 'aylik', ?, 1)
        """, (user_id, title, float(amount), next_due_date))
        conn.commit()
        flash("Ödeme / Fatura eklendi.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Hata: {e}", "danger")
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for('dashboard'))

@app.route('/delete_subscription/<int:sub_id>', methods=['POST'])
@login_required
def delete_subscription(sub_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM subscriptions WHERE id = ? AND user_id = ?", (sub_id, session['user_id']))
    conn.commit()
    cursor.close()
    conn.close()
    flash("Fatura silindi.", "info")
    return redirect(url_for('dashboard'))

# --- TASARRUF HEDEFİ İŞLEMLERİ ---
@app.route('/add_savings_goal', methods=['POST'])
@login_required
def add_savings_goal():
    user_id = session['user_id']
    title = request.form.get('title', '').strip()
    target_amount = request.form.get('target_amount')
    target_date = request.form.get('target_date') or None

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO savings_goals (user_id, title, target_amount, current_amount, target_date, status)
        VALUES (?, ?, ?, 0.00, ?, 'devam_ediyor')
    """, (user_id, title, float(target_amount), target_date))
    conn.commit()
    cursor.close()
    conn.close()
    flash("Tasarruf hedefi oluşturuldu.", "success")
    return redirect(url_for('dashboard'))

@app.route('/deposit_savings_goal', methods=['POST'])
@login_required
def deposit_savings_goal():
    user_id = session['user_id']
    goal_id = request.form.get('goal_id')
    amount = float(request.form.get('amount', 0))

    if amount <= 0:
        flash("Geçerli bir tutar girin.", "warning")
        return redirect(url_for('dashboard'))

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT title FROM savings_goals WHERE id = ? AND user_id = ?", (goal_id, user_id))
        goal = cursor.fetchone()
        if not goal:
            flash("Hedef bulunamadı.", "danger")
            return redirect(url_for('dashboard'))
        goal_title = goal[0]

        cursor.execute("""
            UPDATE savings_goals 
            SET current_amount = current_amount + ?
            WHERE id = ? AND user_id = ?
        """, (amount, goal_id, user_id))

        # Nakit akışından kumbara transferi olarak harcama kaydı düş
        cat_id = get_or_create_category(cursor, user_id, 'Tasarruf & Kumbara', 'gider')
        cursor.execute("""
            INSERT INTO transactions (user_id, category_id, amount, transaction_date, description, payment_method, is_paid)
            VALUES (?, ?, ?, CAST(GETDATE() AS DATE), ?, 'nakit', 1)
        """, (user_id, cat_id, amount, f"Kumbaraya Ekleme: {goal_title}"))

        conn.commit()
        flash(f"Kumbaraya {amount:.2f} TL eklendi ve nakit harcamanıza işlendi!", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Hata: {e}", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('dashboard'))

@app.route('/delete_savings_goal/<int:goal_id>', methods=['POST'])
@login_required
def delete_savings_goal(goal_id):
    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM savings_goals WHERE id = ? AND user_id = ?", (goal_id, user_id))
        conn.commit()
        flash("Tasarruf hedefi kaldırıldı.", "info")
    except Exception as e:
        conn.rollback()
        flash(f"Hata: {e}", "danger")
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for('dashboard'))

@app.route('/complete_savings_goal/<int:goal_id>', methods=['POST'])
@login_required
def complete_savings_goal(goal_id):
    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE savings_goals SET status = 'tamamlandi' WHERE id = ? AND user_id = ?", (goal_id, user_id))
        conn.commit()
        flash("Tebrikler! Tasarruf hedefini başarıyla tamamladınız.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Hata: {e}", "danger")
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for('dashboard'))

# --- YATIRIM ALIM / SATIM İŞLEMLERİ (WACC MANTIĞI) ---
@app.route('/buy_asset', methods=['POST'])
@login_required
def buy_asset():
    user_id = session['user_id']
    symbol = request.form.get('symbol', '').strip().upper()
    asset_type = request.form.get('asset_type')
    quantity = float(request.form.get('quantity', 0))
    unit_price = float(request.form.get('unit_price', 0))
    price_currency = request.form.get('price_currency', 'TRY')

    if quantity <= 0 or unit_price <= 0:
        flash("Geçerli bir adet ve fiyat girin.", "danger")
        return redirect(url_for('dashboard'))

    # ABD hissesi için USD girildiyse TL'ye dönüştür
    unit_price_tl = unit_price
    if asset_type == 'abd_hisse' and price_currency == 'USD':
        rate = get_usdtry_rate() or 1.0
        unit_price_tl = round(unit_price * rate, 2)

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # Varlık mevcut mu?
        cursor.execute("SELECT id, total_quantity, average_cost FROM portfolio_assets WHERE user_id = ? AND asset_symbol = ?", (user_id, symbol))
        existing = cursor.fetchone()

        if existing:
            asset_id, cur_qty, cur_avg = existing[0], float(existing[1]), float(existing[2])
            new_qty = cur_qty + quantity
            # Ağırlıklı Ortalama Maliyet Formülü (WACC) - TL bazında
            new_avg_cost = ((cur_qty * cur_avg) + (quantity * unit_price_tl)) / new_qty

            cursor.execute("""
                UPDATE portfolio_assets 
                SET total_quantity = ?, average_cost = ?
                WHERE id = ?
            """, (new_qty, new_avg_cost, asset_id))
        else:
            cursor.execute("""
                INSERT INTO portfolio_assets (user_id, asset_symbol, asset_type, total_quantity, average_cost)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, symbol, asset_type, quantity, unit_price_tl))
            cursor.execute("SELECT @@IDENTITY")
            asset_id = cursor.fetchone()[0]

        total_cost_tl = quantity * unit_price_tl

        # Alım hareketini kaydet
        cursor.execute("""
            INSERT INTO asset_transactions (asset_id, user_id, transaction_type, quantity, unit_price, total_amount, transaction_date)
            VALUES (?, ?, 'alis', ?, ?, ?, CAST(GETDATE() AS DATE))
        """, (asset_id, user_id, quantity, unit_price_tl, total_cost_tl))

        # Nakit akışından alım tutarını harcama olarak düş
        cat_id = get_or_create_category(cursor, user_id, 'Yatırım & Portföy', 'gider')
        currency_info = f" (${unit_price:.2f})" if price_currency == 'USD' else ""
        cursor.execute("""
            INSERT INTO transactions (user_id, category_id, amount, transaction_date, description, payment_method, is_paid)
            VALUES (?, ?, ?, CAST(GETDATE() AS DATE), ?, 'nakit', 1)
        """, (user_id, cat_id, total_cost_tl, f"Varlık Alımı: {quantity:.2f} adet {symbol}{currency_info}"))

        conn.commit()
        flash(f"{symbol} alımı portföye eklendi ve nakit harcamanıza işlendi.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Alım hatası: {e}", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('dashboard'))

@app.route('/sell_asset', methods=['POST'])
@login_required
def sell_asset():
    user_id = session['user_id']
    asset_id = request.form.get('asset_id')
    quantity = float(request.form.get('quantity', 0))
    unit_price = float(request.form.get('unit_price', 0))
    sell_currency = request.form.get('sell_currency', 'TRY')

    if quantity <= 0 or unit_price <= 0:
        flash("Geçerli bir adet ve satış fiyatı girin.", "danger")
        return redirect(url_for('dashboard'))

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT id, asset_symbol, asset_type, total_quantity, average_cost 
            FROM portfolio_assets 
            WHERE id = ? AND user_id = ?
        """, (asset_id, user_id))
        asset = cursor.fetchone()

        if not asset:
            flash("Satılacak varlık bulunamadı.", "danger")
            return redirect(url_for('dashboard'))

        symbol = asset[1]
        asset_type = asset[2]
        cur_qty = float(asset[3])
        avg_cost = float(asset[4])

        if quantity > cur_qty:
            flash(f"Yetersiz bakiye! Portföyünüzde {cur_qty:.2f} adet {symbol} bulunmaktadır.", "warning")
            return redirect(url_for('dashboard'))

        unit_price_tl = unit_price
        if asset_type == 'abd_hisse' and sell_currency == 'USD':
            rate = get_usdtry_rate() or 1.0
            unit_price_tl = round(unit_price * rate, 2)

        new_qty = cur_qty - quantity
        total_amount_tl = quantity * unit_price_tl
        realized_pnl = (unit_price_tl - avg_cost) * quantity

        cursor.execute("""
            UPDATE portfolio_assets 
            SET total_quantity = ? 
            WHERE id = ?
        """, (new_qty, asset_id))

        cursor.execute("""
            INSERT INTO asset_transactions (asset_id, user_id, transaction_type, quantity, unit_price, total_amount, realized_pnl, transaction_date)
            VALUES (?, ?, 'satis', ?, ?, ?, ?, CAST(GETDATE() AS DATE))
        """, (asset_id, user_id, quantity, unit_price_tl, total_amount_tl, realized_pnl))

        pnl_text = f"+{realized_pnl:.2f} TL Kâr" if realized_pnl >= 0 else f"{realized_pnl:.2f} TL Zarar"

        # Satıştan gelen nakit tutarını gelirlere kaydet
        cat_id = get_or_create_category(cursor, user_id, 'Yatırım Satış Geliri', 'gelir')
        currency_info = f" (${unit_price:.2f})" if sell_currency == 'USD' else ""
        cursor.execute("""
            INSERT INTO transactions (user_id, category_id, amount, transaction_date, description, payment_method, is_paid)
            VALUES (?, ?, ?, CAST(GETDATE() AS DATE), ?, 'nakit', 1)
        """, (user_id, cat_id, total_amount_tl, f"Varlık Satışı: {quantity:.2f} adet {symbol}{currency_info} ({pnl_text})"))

        conn.commit()
        flash(f"{symbol} satışı yapıldı ({quantity} adet, {pnl_text}) ve nakit gelirinize eklendi.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Satış işlemi sırasında hata: {e}", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('dashboard'))

# --- KATEGORİ VE BÜTÇE YÖNETİMİ ---
@app.route('/add_category', methods=['POST'])
@login_required
def add_category():
    user_id = session['user_id']
    name = request.form.get('name', '').strip()
    cat_type = request.form.get('type', 'gider')
    monthly_budget = float(request.form.get('monthly_budget', 0) or 0)

    if not name:
        flash("Kategori adı boş bırakılamaz.", "warning")
        return redirect(url_for('dashboard'))

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM categories WHERE user_id = ? AND name = ? AND type = ?", (user_id, name, cat_type))
        if cursor.fetchone():
            flash("Bu kategori zaten mevcut.", "warning")
            return redirect(url_for('dashboard'))

        cursor.execute("""
            INSERT INTO categories (user_id, name, type, monthly_budget)
            VALUES (?, ?, ?, ?)
        """, (user_id, name, cat_type, monthly_budget))
        conn.commit()
        flash(f"'{name}' kategorisi başarıyla eklendi.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Kategori eklenirken hata: {e}", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('dashboard'))

@app.route('/update_category_budget', methods=['POST'])
@login_required
def update_category_budget():
    user_id = session['user_id']
    category_id = request.form.get('category_id')
    monthly_budget = float(request.form.get('monthly_budget', 0) or 0)

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE categories 
            SET monthly_budget = ? 
            WHERE id = ? AND user_id = ?
        """, (monthly_budget, category_id, user_id))
        conn.commit()
        flash("Kategori bütçesi güncellendi.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Güncelleme hatası: {e}", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('dashboard'))

@app.route('/delete_category/<int:cat_id>', methods=['POST'])
@login_required
def delete_category(cat_id):
    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM transactions WHERE category_id = ? AND user_id = ?", (cat_id, user_id))
        tx_count = cursor.fetchone()[0]
        if tx_count > 0:
            flash("Bu kategoriye bağlı harcama/gelir kayıtları bulunduğu için kategori silinemez.", "warning")
        else:
            cursor.execute("DELETE FROM categories WHERE id = ? AND user_id = ?", (cat_id, user_id))
            conn.commit()
            flash("Kategori başarıyla silindi.", "info")
    except Exception as e:
        conn.rollback()
        flash(f"Silme hatası: {e}", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('dashboard'))

if __name__ == '__main__':
    app.run(debug=True, port=5000)