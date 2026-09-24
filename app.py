from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from datetime import datetime
import json
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

# --- CANLI FİYAT ÇEKİCİ MOTOR (API) ---
def fetch_live_price(symbol, asset_type):
    """yfinance üzerinden anlık fiyat çeker; hata durumunda None döner."""
    try:
        clean_sym = symbol.strip().upper()
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
        elif asset_type == 'emtia_altin':
            # Gram altın tahmini: Ons Altın (GC=F) * USDTRY / 31.1035
            ticker_gold = yf.Ticker('GC=F')
            ticker_usd = yf.Ticker('USDTRY=X')
            p_gold = ticker_gold.fast_info.get('lastPrice')
            p_usd = ticker_usd.fast_info.get('lastPrice')
            if p_gold and p_usd:
                return round((p_gold * p_usd) / 31.1035, 2)
            return None

        ticker = yf.Ticker(ticker_str)
        price = ticker.fast_info.get('lastPrice')
        if price:
            return round(float(price), 2)
    except Exception:
        pass
    return None

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

    # 1. Finansal Özet: Gelir, Nakit Gider, Kredi Kartı Gideri (Ekstre)
    cursor.execute("""
        SELECT 
            ISNULL(SUM(CASE WHEN c.type = 'gelir' THEN t.amount ELSE 0 END), 0) AS total_income,
            ISNULL(SUM(CASE WHEN c.type = 'gider' AND ISNULL(t.payment_method, 'nakit') = 'nakit' THEN t.amount ELSE 0 END), 0) AS cash_expense,
            ISNULL(SUM(CASE WHEN c.type = 'gider' AND t.payment_method = 'kredi_karti' THEN t.amount ELSE 0 END), 0) AS cc_expense
        FROM transactions t
        JOIN categories c ON t.category_id = c.id
        WHERE t.user_id = ? 
          AND MONTH(t.transaction_date) = MONTH(GETDATE()) 
          AND YEAR(t.transaction_date) = YEAR(GETDATE())
    """, (user_id,))
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
             AND MONTH(t.transaction_date) = MONTH(GETDATE()) 
             AND YEAR(t.transaction_date) = YEAR(GETDATE())
        WHERE c.user_id = ? AND c.type = 'gider'
        GROUP BY c.id, c.name, c.monthly_budget
        ORDER BY spent DESC
    """, (user_id,))
    budgets_raw = cursor.fetchall()
    
    category_budgets = []
    chart_category_labels = []
    chart_category_values = []

    for b in budgets_raw:
        cat_name = b[1]
        budget = float(b[2])
        spent = float(b[3])
        pct = round((spent / budget * 100), 1) if budget > 0 else 0.0
        
        category_budgets.append({
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

        live_price = fetch_live_price(symbol, atype)
        if live_price is None:
            live_price = avg_cost  # Fiyat çekilemezse maliyet varsayılır

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
            'cur_val': cur_val,
            'pnl': pnl,
            'pnl_pct': pnl_pct
        })

    total_portfolio_pnl = total_portfolio_value - total_portfolio_cost
    total_portfolio_pnl_pct = round((total_portfolio_pnl / total_portfolio_cost * 100), 2) if total_portfolio_cost > 0 else 0.0

    # 6. Son İşlem Hareketleri
    cursor.execute("""
        SELECT TOP 8 t.id, t.amount, t.transaction_date, t.description, c.name, c.type, t.payment_method
        FROM transactions t
        JOIN categories c ON t.category_id = c.id
        WHERE t.user_id = ?
        ORDER BY t.transaction_date DESC, t.id DESC
    """, (user_id,))
    recent_transactions = cursor.fetchall()

    # Kategori Listesi (İşlem modalı için)
    cursor.execute("SELECT id, name, type FROM categories WHERE user_id = ? ORDER BY type, name", (user_id,))
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
        recent_transactions=recent_transactions,
        categories=categories,
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

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO transactions (user_id, category_id, amount, transaction_date, description, payment_method)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, category_id, float(amount), transaction_date, description, payment_method))
        conn.commit()
        flash("İşlem kaydedildi.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Hata: {e}", "danger")
    finally:
        cursor.close()
        conn.close()
    return redirect(url_for('dashboard'))

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

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE savings_goals 
        SET current_amount = current_amount + ?
        WHERE id = ? AND user_id = ?
    """, (amount, goal_id, user_id))
    conn.commit()
    cursor.close()
    conn.close()
    flash("Kumbaraya para eklendi!", "success")
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

    if quantity <= 0 or unit_price <= 0:
        flash("Geçerli bir adet ve fiyat girin.", "danger")
        return redirect(url_for('dashboard'))

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # Varlık mevcut mu?
        cursor.execute("SELECT id, total_quantity, average_cost FROM portfolio_assets WHERE user_id = ? AND asset_symbol = ?", (user_id, symbol))
        existing = cursor.fetchone()

        if existing:
            asset_id, cur_qty, cur_avg = existing[0], float(existing[1]), float(existing[2])
            new_qty = cur_qty + quantity
            # Ağırlıklı Ortalama Maliyet Formülü (WACC)
            new_avg_cost = ((cur_qty * cur_avg) + (quantity * unit_price)) / new_qty

            cursor.execute("""
                UPDATE portfolio_assets 
                SET total_quantity = ?, average_cost = ?
                WHERE id = ?
            """, (new_qty, new_avg_cost, asset_id))
        else:
            cursor.execute("""
                INSERT INTO portfolio_assets (user_id, asset_symbol, asset_type, total_quantity, average_cost)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, symbol, asset_type, quantity, unit_price))
            cursor.execute("SELECT @@IDENTITY")
            asset_id = cursor.fetchone()[0]

        # Alım hareketini kaydet
        cursor.execute("""
            INSERT INTO asset_transactions (asset_id, user_id, transaction_type, quantity, unit_price, total_amount, transaction_date)
            VALUES (?, ?, 'alis', ?, ?, ?, CAST(GETDATE() AS DATE))
        """, (asset_id, user_id, quantity, unit_price, (quantity * unit_price)))

        conn.commit()
        flash(f"{symbol} alımı portföye eklendi.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Alım hatası: {e}", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('dashboard'))

if __name__ == '__main__':
    app.run(debug=True, port=5000)