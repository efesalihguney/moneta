from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file, Response, send_from_directory, abort
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename, safe_join
from functools import wraps
from datetime import datetime
import json
import time
import urllib.request
import re
import io
import calendar
import os
import hmac
import secrets
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor

def _ensure_ascii_ca_bundle():
    """Windows'ta Unicode klasör yollarındaki certifi paketinin curl_cffi SSL (curl 77) hatasına yol açmasını önler."""
    try:
        import shutil
        import tempfile
        import certifi
        ca_path = certifi.where()
        if ca_path and ca_path.isascii():
            return
        candidates = [
            tempfile.gettempdir(),
            os.path.join(os.environ.get('PUBLIC', r'C:\Users\Public'), 'moneta_certs'),
            os.path.join(os.environ.get('ProgramData', r'C:\ProgramData'), 'moneta_certs'),
        ]
        for base_dir in candidates:
            if not base_dir or not base_dir.isascii():
                continue
            try:
                os.makedirs(base_dir, exist_ok=True)
                target = os.path.join(base_dir, 'moneta_cacert.pem')
                if (not os.path.exists(target)) or os.path.getsize(target) != os.path.getsize(ca_path):
                    shutil.copyfile(ca_path, target)
                certifi.where = lambda _p=target: _p
                os.environ['CURL_CA_BUNDLE'] = target
                os.environ['SSL_CERT_FILE'] = target
                return
            except Exception:
                continue
    except Exception:
        pass

_ensure_ascii_ca_bundle()

import requests
import pandas as pd
import yfinance as yf
import xml.etree.ElementTree as ET
from db import get_db_connection, is_postgres
from config import Config
from services.finance_engine import (
    calculate_fisher_real_return,
    evaluate_credit_utilization,
    analyze_portfolio_concentration,
    calculate_wac_and_pnl,
    calculate_realized_pnl,
    parse_safe_amount
)
from services.receipt_scanner import (
    scan_receipt,
    is_allowed_scan_file,
    MAX_RECEIPT_SCAN_BYTES
)

app = Flask(__name__)
app.secret_key = Config.get_secret_key()
app.config['SESSION_COOKIE_HTTPONLY'] = Config.SESSION_COOKIE_HTTPONLY
app.config['SESSION_COOKIE_SAMESITE'] = Config.SESSION_COOKIE_SAMESITE
app.config['SESSION_COOKIE_SECURE'] = Config.SESSION_COOKIE_SECURE
app.config['MAX_CONTENT_LENGTH'] = Config.MAX_CONTENT_LENGTH

if Config.is_production() and app.secret_key == 'super_secret_moneta_key_change_in_production':
    print("[UYARI] Canlı ortamda (production) varsayılan SECRET_KEY kullanılıyor! Güvenlik için ortam değişkeni üzerinden güçlü bir anahtar tanımlayın.")


def generate_csrf_token():
    """Oturum bazlı CSRF doğrulama belirteci üretir ve döndürür."""
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['_csrf_token'] = token
    return token


app.jinja_env.globals['csrf_token'] = generate_csrf_token


@app.before_request
def verify_csrf_token():
    """Tüm durum değiştiren isteklerde (POST/PUT/PATCH/DELETE) CSRF token doğrular."""
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        if app.config.get('TESTING') and not app.config.get('CSRF_ENABLED_IN_TESTS'):
            return
        expected = session.get('_csrf_token')
        submitted = request.form.get('csrf_token') or request.headers.get('X-CSRF-Token')
        if not expected or not submitted or not hmac.compare_digest(str(expected), str(submitted)):
            abort(403)

def ensure_schema_migrations():
    """Veritabanı şema güncellemelerini, indeksleri, kurumsal VIEW ve STORED PROCEDURE nesnelerini güvenli bir şekilde yapar."""
    if is_postgres():
        # PostgreSQL / Supabase şeması schema_postgres.sql ve init_db.py üzerinden yönetilir
        return
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 1. Kredi kartı limit sütunu, abonelik/düzenli işlem otomasyon sütunları ve performans indeksleri
        cursor.execute("""
            IF COL_LENGTH('users', 'credit_card_limit') IS NULL
            BEGIN
                ALTER TABLE users ADD credit_card_limit DECIMAL(18,2) NOT NULL DEFAULT 50000.00;
            END
            IF COL_LENGTH('subscriptions', 'payment_method') IS NULL
            BEGIN
                ALTER TABLE subscriptions ADD payment_method NVARCHAR(20) NOT NULL DEFAULT 'nakit';
            END
            IF COL_LENGTH('subscriptions', 'auto_apply') IS NULL
            BEGIN
                ALTER TABLE subscriptions ADD auto_apply BIT NOT NULL DEFAULT 1;
            END
            IF COL_LENGTH('subscriptions', 'last_synced_period') IS NULL
            BEGIN
                ALTER TABLE subscriptions ADD last_synced_period NVARCHAR(7) NULL;
            END
            IF COL_LENGTH('recurring_transactions', 'last_synced_period') IS NULL
            BEGIN
                ALTER TABLE recurring_transactions ADD last_synced_period NVARCHAR(7) NULL;
            END
            IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_transactions_user_date' AND object_id = OBJECT_ID('transactions'))
            BEGIN
                CREATE INDEX IX_transactions_user_date ON transactions(user_id, transaction_date DESC);
            END
            IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_portfolio_assets_user_sym' AND object_id = OBJECT_ID('portfolio_assets'))
            BEGIN
                CREATE INDEX IX_portfolio_assets_user_sym ON portfolio_assets(user_id, asset_symbol);
            END
        """)
        conn.commit()

        # 2. Kurumsal SQL View (vw_user_financial_summary)
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

        # 3. Kurumsal Stored Procedure (sp_get_user_budget_health)
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
    except Exception as e:
        print(f"Schema migration note: {e}")
    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass
        if conn:
            try:
                conn.close()
            except Exception:
                pass

try:
    ensure_schema_migrations()
except Exception:
    pass

@app.template_filter('currency_tr')
def currency_tr_filter(value, decimals=2):
    """Sayıları Türk Lirası standardında (1.234,56) formatlar."""
    if value is None or value == '':
        return "0," + ("0" * decimals) if decimals > 0 else "0"
    try:
        val = float(value)
        formatted = f"{val:,.{decimals}f}"
        return formatted.replace(",", "X").replace(".", ",").replace("X", ".")
    except (ValueError, TypeError):
        return str(value)

# --- HTTP GÜVENLİK BAŞLIKLARI (SECURITY HEADERS) ---
@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    return response

# --- ÖZEL HATA YÖNETİCİLERİ (403, 404, 413 & 500) ---
@app.errorhandler(403)
def forbidden_error(e):
    return render_template('403.html'), 403


@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404


@app.errorhandler(413)
def payload_too_large(e):
    if request.path.startswith('/api/'):
        return {
            'success': False,
            'error': 'Yüklenen dosya boyutu çok büyük (maksimum 5 MB).'
        }, 413
    flash("Yüklenen dosya boyutu çok büyük (maksimum 5 MB).", "warning")
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.errorhandler(500)
def internal_server_error(e):
    print(f"[Moneta Safe Isolation 500]: {e}")
    return render_template('500.html'), 500


# --- FİŞ / MAKBUZ YÜKLEME AYARLARI (static/ dışında güvenli ve Serverless/Read-Only uyumlu depolama) ---
UPLOAD_FOLDER = Config.resolve_upload_folder(app.root_path)
try:
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
except OSError:
    UPLOAD_FOLDER = Config.TEMP_UPLOAD_FOLDER
    try:
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    except OSError:
        pass
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'pdf'}


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def _save_receipt_if_valid(receipt_file):
    """
    Yüklenen fişi uzantı kontrolünden geçirip diske kaydeder; (filename, err_msg) döner.
    Vercel Serverless / salt-okunur dosya sisteminde diske yazma başarısız olursa (OSError),
    geçici /tmp dizinini dener; yine yazılamasa bile veritabanı işlemini kesmeden güvenli dosya adını döner.
    """
    if not receipt_file or not receipt_file.filename:
        return None, None
    if not allowed_file(receipt_file.filename):
        return None, "İzin verilmeyen fiş dosya türü. Lütfen PNG, JPG, GIF, WEBP veya PDF yükleyin."
    safe_name = secure_filename(receipt_file.filename)
    if not safe_name:
        return None, "Geçersiz dosya adı."
    receipt_filename = f"{uuid.uuid4().hex[:12]}_{safe_name}"

    target_dirs = list(dict.fromkeys([
        Config.resolve_upload_folder(app.root_path) if Config.is_vercel() else UPLOAD_FOLDER,
        Config.TEMP_UPLOAD_FOLDER,
    ]))
    last_os_error = None
    for target_dir in target_dirs:
        if not target_dir:
            continue
        try:
            os.makedirs(target_dir, exist_ok=True)
            receipt_file.save(os.path.join(target_dir, receipt_filename))
            return receipt_filename, None
        except OSError as exc:
            last_os_error = exc
            continue

    if last_os_error is not None:
        print(f"[Receipt Upload Warning]: Dosya diske yazılamadı ({last_os_error}); yalnızca dosya adı kaydediliyor.")
    return receipt_filename, None

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash("Lütfen önce giriş yapın.", "warning")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# --- CANLI FİYAT ÇEKİCİ MOTOR & İŞ PARÇACIĞI GÜVENLİ ÖNBELLEK (THREAD-SAFE CACHE) ---
PRICE_CACHE = {}  # { (clean_sym, asset_type): (data, timestamp) }
_price_cache_lock = threading.Lock()
_LAST_KNOWN_USDTRY = {'rate': Config.DEFAULT_USD_TRY_FALLBACK}
CACHE_TTL = 300   # 5 dakika (300 saniye)

BENCHMARK_CACHE = {}  # { 'data': (results, timestamp) }
_benchmark_cache_lock = threading.Lock()


def get_benchmark_returns():
    """Son 1, 3 ve 6 aylık BIST100, Dolar, Gram Altın getirilerini ve TÜFE enflasyonunu döner (1 saatlik önbellek)."""
    now = time.time()
    tufe_defaults = Config.TUFE_RATES

    with _benchmark_cache_lock:
        cached = BENCHMARK_CACHE.get('data')
    if cached and (now - cached[1] < 3600):
        res = {k: dict(v) for k, v in cached[0].items()}
        for p, t_val in tufe_defaults.items():
            if p in res:
                res[p]['tufe'] = t_val
        return res

    results = {
        '1M': {'bist': 0.0, 'usd': 0.0, 'gold': 0.0, 'tufe': tufe_defaults['1M']},
        '3M': {'bist': 0.0, 'usd': 0.0, 'gold': 0.0, 'tufe': tufe_defaults['3M']},
        '6M': {'bist': 0.0, 'usd': 0.0, 'gold': 0.0, 'tufe': tufe_defaults['6M']}
    }
    try:
        bist_hist = yf.Ticker('XU100.IS').history(period='6mo')
        usd_hist = yf.Ticker('USDTRY=X').history(period='6mo')
        gold_hist = yf.Ticker('GC=F').history(period='6mo')

        if not bist_hist.empty and len(bist_hist) > 5:
            b_now = bist_hist['Close'].iloc[-1]
            if len(bist_hist) >= 21:
                results['1M']['bist'] = round(float((b_now - bist_hist['Close'].iloc[-21]) / bist_hist['Close'].iloc[-21] * 100), 1)
            if len(bist_hist) >= 63:
                results['3M']['bist'] = round(float((b_now - bist_hist['Close'].iloc[-63]) / bist_hist['Close'].iloc[-63] * 100), 1)
            results['6M']['bist'] = round(float((b_now - bist_hist['Close'].iloc[0]) / bist_hist['Close'].iloc[0] * 100), 1)

        if not usd_hist.empty and len(usd_hist) > 5:
            u_now = usd_hist['Close'].iloc[-1]
            if len(usd_hist) >= 21:
                results['1M']['usd'] = round(float((u_now - usd_hist['Close'].iloc[-21]) / usd_hist['Close'].iloc[-21] * 100), 1)
            if len(usd_hist) >= 63:
                results['3M']['usd'] = round(float((u_now - usd_hist['Close'].iloc[-63]) / usd_hist['Close'].iloc[-63] * 100), 1)
            results['6M']['usd'] = round(float((u_now - usd_hist['Close'].iloc[0]) / usd_hist['Close'].iloc[0] * 100), 1)

        if not gold_hist.empty and not usd_hist.empty and len(gold_hist) > 5:
            g_now = (gold_hist['Close'].iloc[-1] * usd_hist['Close'].iloc[-1]) / 31.1035
            if len(gold_hist) >= 21 and len(usd_hist) >= 21:
                g_1m = (gold_hist['Close'].iloc[-21] * usd_hist['Close'].iloc[-21]) / 31.1035
                results['1M']['gold'] = round(float((g_now - g_1m) / g_1m * 100), 1)
            if len(gold_hist) >= 63 and len(usd_hist) >= 63:
                g_3m = (gold_hist['Close'].iloc[-63] * usd_hist['Close'].iloc[-63]) / 31.1035
                results['3M']['gold'] = round(float((g_now - g_3m) / g_3m * 100), 1)
            g_6m = (gold_hist['Close'].iloc[0] * usd_hist['Close'].iloc[0]) / 31.1035
            results['6M']['gold'] = round(float((g_now - g_6m) / g_6m * 100), 1)

        with _benchmark_cache_lock:
            BENCHMARK_CACHE['data'] = ({k: dict(v) for k, v in results.items()}, now)
        return results
    except Exception:
        fallback = {
            '1M': {'bist': -12.0, 'usd': 2.0, 'gold': -5.7, 'tufe': tufe_defaults['1M']},
            '3M': {'bist': -8.8, 'usd': 4.9, 'gold': 12.0, 'tufe': tufe_defaults['3M']},
            '6M': {'bist': -0.6, 'usd': 10.4, 'gold': 4.6, 'tufe': tufe_defaults['6M']}
        }
        return fallback


NEWS_CACHE = {'timestamp': 0, 'data': []}
_news_cache_lock = threading.Lock()


def get_economy_news(force_refresh=False):
    """Günün ekonomi ve piyasa haber başlıklarını çeker ve 15 dakika önbelleğe alır."""
    now = time.time()
    with _news_cache_lock:
        cached_news = list(NEWS_CACHE['data']) if NEWS_CACHE['data'] else []
        cached_ts = NEWS_CACHE['timestamp']
    if not force_refresh and cached_news and (now - cached_ts < 900):
        return cached_news

    url = 'https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=tr&gl=TR&ceid=TR:tr'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
    news_items = []
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            root = ET.fromstring(resp.read())
            for item in root.findall('.//item')[:12]:
                raw_title = item.find('title').text if item.find('title') is not None else ''
                link = item.find('link').text if item.find('link') is not None else '#'
                pub_date = item.find('pubDate').text if item.find('pubDate') is not None else ''

                source = 'Ekonomi Gündemi'
                title = raw_title
                if ' - ' in raw_title:
                    parts = raw_title.rsplit(' - ', 1)
                    title = parts[0].strip()
                    source = parts[1].strip()

                lower_title = title.lower()
                if any(k in lower_title for k in ['bist', 'borsa', 'hisse', 'endeks', 'halka arz', 'spk']):
                    category = 'Borsa & BIST'
                    badge_class = 'bg-info-subtle text-info border border-info'
                    icon = 'bi-graph-up'
                elif any(k in lower_title for k in ['faiz', 'enflasyon', 'tcmb', 'merkez bankası', 'fed', 'politika faizi', 'tüfe', 'üfe']):
                    category = 'Merkez & Faiz'
                    badge_class = 'bg-primary-subtle text-primary border border-primary'
                    icon = 'bi-bank'
                elif any(k in lower_title for k in ['dolar', 'euro', 'döviz', 'kur', 'sterlin', 'parite', 'tl']):
                    category = 'Döviz & Kur'
                    badge_class = 'bg-success-subtle text-success border border-success'
                    icon = 'bi-currency-exchange'
                elif any(k in lower_title for k in ['altın', 'emtia', 'petrol', 'ons', 'gümüş', 'brent', 'varil']):
                    category = 'Altın & Emtia'
                    badge_class = 'bg-warning-subtle text-warning-emphasis border border-warning'
                    icon = 'bi-gem'
                else:
                    category = 'Ekonomi'
                    badge_class = 'bg-secondary-subtle text-secondary border'
                    icon = 'bi-newspaper'

                time_str = pub_date[:16] if pub_date else 'Bugün'
                if pub_date:
                    try:
                        dt = datetime.strptime(pub_date[:25].strip(), "%a, %d %b %Y %H:%M:%S")
                        time_str = dt.strftime("%H:%M")
                    except Exception:
                        time_str = pub_date[:16]

                news_items.append({
                    'title': title,
                    'source': source,
                    'link': link,
                    'category': category,
                    'badge_class': badge_class,
                    'icon': icon,
                    'time': time_str
                })

        if news_items:
            with _news_cache_lock:
                NEWS_CACHE['data'] = news_items
                NEWS_CACHE['timestamp'] = now
            return news_items
    except Exception as e:
        print("Ekonomi haberleri çekilirken hata oluştu:", e)

    with _news_cache_lock:
        if NEWS_CACHE['data']:
            return list(NEWS_CACHE['data'])

    # Ağ hatası veya çevrimdışı durum için güncel yedek haberler
    fallback_news = [
        {
            'title': 'BIST 100 Endeksi haftayı hareketli kapatıyor: Sanayi ve bankacılık hisselerinde hacim arttı',
            'source': 'Bloomberg HT',
            'link': 'https://www.bloomberght.com',
            'category': 'Borsa & BIST',
            'badge_class': 'bg-info-subtle text-info border border-info',
            'icon': 'bi-graph-up',
            'time': 'Bugün'
        },
        {
            'title': 'TCMB Piyasa Katılımcıları Anketi: Yıl sonu enflasyon ve faiz beklentileri açıklandı',
            'source': 'AA Finans',
            'link': 'https://www.aa.com.tr/tr/ekonomi',
            'category': 'Merkez & Faiz',
            'badge_class': 'bg-primary-subtle text-primary border border-primary',
            'icon': 'bi-bank',
            'time': 'Bugün'
        },
        {
            'title': 'Döviz kurlarında son durum: Dolar/TL ve Euro yatay seyirde güçleniyor',
            'source': 'Dünya Gazetesi',
            'link': 'https://www.dunya.com',
            'category': 'Döviz & Kur',
            'badge_class': 'bg-success-subtle text-success border border-success',
            'icon': 'bi-currency-exchange',
            'time': 'Bugün'
        },
        {
            'title': 'Gram altın rekor serisini sürdürüyor: Küresel ons ve jeopolitik gelişmeler fiyatı destekliyor',
            'source': 'Ekonomim',
            'link': 'https://www.ekonomim.com',
            'category': 'Altın & Emtia',
            'badge_class': 'bg-warning-subtle text-warning-emphasis border border-warning',
            'icon': 'bi-gem',
            'time': 'Bugün'
        },
        {
            'title': 'Küresel piyasalarda Fed ve merkez bankalarından gelecek faiz indirim sinyalleri izleniyor',
            'source': 'Foreks Haber',
            'link': 'https://www.foreks.com',
            'category': 'Ekonomi',
            'badge_class': 'bg-secondary-subtle text-secondary border',
            'icon': 'bi-newspaper',
            'time': 'Bugün'
        }
    ]
    return fallback_news

def generate_financial_insights(total_income, total_expense, net_savings, savings_rate, category_budgets, cc_expense, cash_expense, portfolio_pnl_pct, benchmark_data):
    """Kullanıcının finansal durumunu analiz edip akıllı tavsiye ve içgörüler üretir."""
    insights = []
    
    # 1. Bütçe ve 50/30/20 Tasarruf Kuralı Analizi
    if total_income > 0:
        if savings_rate >= 30:
            insights.append({
                'type': 'success',
                'icon': 'bi-trophy-fill',
                'title': 'Üstün Tasarruf Performansı',
                'text': f"Bu ay gelirinizin %{savings_rate:.1f}'ini biriktirdiniz. 50/30/20 finansal kuralındaki %20 tasarruf hedefini başarıyla geride bıraktınız!"
            })
        elif savings_rate >= 20:
            insights.append({
                'type': 'success',
                'icon': 'bi-check-circle-fill',
                'title': 'İdeal Tasarruf Dengesi',
                'text': f"Gelirinizin %{savings_rate:.1f}'ini tasarrufa ayırdınız. Sağlıklı ve dengeli bir birikim patikasındasınız."
            })
        elif savings_rate >= 5:
            insights.append({
                'type': 'info',
                'icon': 'bi-lightbulb-fill',
                'title': 'Tasarruf Potansiyeli',
                'text': f"Tasarruf oranınız %{savings_rate:.1f}. Zorunlu olmayan abonelik ve harcamalarınızı optimize ederek %20 hedefine yaklaşabilirsiniz."
            })
        elif savings_rate < 0:
            insights.append({
                'type': 'danger',
                'icon': 'bi-exclamation-octagon-fill',
                'title': 'Bütçe Açığı Uyarısı',
                'text': f"Bu ay harcamalarınız gelirinizi {currency_tr_filter(abs(net_savings))} TL aştı. Birikimlerinizin erimemesi için harcamalarınızı acilen sınırlandırın."
            })

    # 2. Bütçe Aşım ve Risk Tespiti
    overbudget_cats = [b for b in category_budgets if b['spent'] > b['budget'] and b['budget'] > 0]
    if overbudget_cats:
        for b in overbudget_cats[:2]:
            over_pct = ((b['spent'] - b['budget']) / b['budget']) * 100
            insights.append({
                'type': 'warning',
                'icon': 'bi-exclamation-triangle-fill',
                'title': f"{b['name']} Bütçesi Aşıldı",
                'text': f"Bu kategoride {currency_tr_filter(b['budget'])} TL planlanan bütçeye karşın {currency_tr_filter(b['spent'])} TL harcandı (%{over_pct:.0f} aşım)."
            })
    elif category_budgets and total_expense > 0:
        insights.append({
            'type': 'success',
            'icon': 'bi-shield-check',
            'title': 'Kusursuz Bütçe Disiplini',
            'text': "Bu ay belirlenen hiçbir kategori bütçe tavanı aşılmadı. Harcama kontrolünüz harika!"
        })

    # 3. Kredi Kartı vs Nakit Akışı Riski
    if total_expense > 0:
        cc_pct = (cc_expense / total_expense) * 100
        if cc_pct > 65:
            insights.append({
                'type': 'warning',
                'icon': 'bi-credit-card-2-front-fill',
                'title': 'Yüksek Kredi Kartı Kullanımı',
                'text': f"Aylık giderlerinizin %{cc_pct:.0f}'i kredi kartıyla yapıldı ({currency_tr_filter(cc_expense)} TL). Ekstre kesiminde nakit sıkışıklığı yaşamamak için vadesiz bakiyenizi hazır tutun."
            })

    # 4. Portföy & Benchmark Kıyaslaması
    if benchmark_data and '1M' in benchmark_data:
        bist_1m = benchmark_data['1M']['bist']
        if portfolio_pnl_pct > bist_1m and portfolio_pnl_pct != 0:
            insights.append({
                'type': 'primary',
                'icon': 'bi-rocket-takeoff-fill',
                'title': 'Piyasanın Üzerinde Portföy Getirisi',
                'text': f"Portföyünüz (%{portfolio_pnl_pct:.1f}), son 1 aylık BIST 100 getirisini (%{bist_1m:.1f}) geride bırakarak yüksek alfa üretti!"
            })

    # 5. Boş/Yeni Hesap İçin Karşılama İpucu
    if not insights:
        insights.append({
            'type': 'primary',
            'icon': 'bi-sparkles',
            'title': 'Finansal Asistanınız Hazır!',
            'text': 'Bu döneme ait harcama ve gelirlerinizi ekledikçe; bütçe aşım uyarıları, 50/30/20 kuralı analizi ve tasarruf tavsiyeleriniz burada otomatik oluşturulacaktır.'
        })

    return insights

def get_in_app_notifications(subscriptions, category_budgets, total_unpaid_cc, savings_goals, total_portfolio_pnl_pct):
    """Kullanıcının faturalarını, bütçe limitlerini, kart borcunu ve birikimlerini inceleyerek uygulama içi bildirimler üretir."""
    notifications = []
    
    # 1. Yaklaşan / Vadesi Dolmuş Faturalar
    if subscriptions:
        for sub in subscriptions:
            sub_id, sub_name, sub_amount = sub[0], sub[1], float(sub[2])
            days_left = sub[5]
            if days_left <= 0:
                notifications.append({
                    'id': f'sub_{sub_id}',
                    'severity': 'danger',
                    'badge': 'Acil Ödeme',
                    'icon': 'bi-exclamation-octagon-fill',
                    'title': f'{sub_name} Vadesi Doldu!',
                    'message': f'{currency_tr_filter(sub_amount)} TL tutarındaki {sub_name} faturasının son ödeme günü geldi/geçti.',
                    'target_tab': 'goals-tab'
                })
            elif days_left <= 3:
                notifications.append({
                    'id': f'sub_{sub_id}',
                    'severity': 'danger',
                    'badge': f'{days_left} Gün Kaldı',
                    'icon': 'bi-alarm-fill',
                    'title': f'{sub_name} Yaklaşıyor',
                    'message': f'{currency_tr_filter(sub_amount)} TL tutarındaki fatura için son {days_left} gün kaldı.',
                    'target_tab': 'goals-tab'
                })
            elif days_left <= 7:
                notifications.append({
                    'id': f'sub_{sub_id}',
                    'severity': 'warning',
                    'badge': f'{days_left} Gün Kaldı',
                    'icon': 'bi-calendar-event',
                    'title': f'{sub_name} Hatırlatması',
                    'message': f'{sub_name} ödemesine 1 haftadan az zaman kaldı ({currency_tr_filter(sub_amount)} TL).',
                    'target_tab': 'goals-tab'
                })

    # 2. Bütçe Aşımları ve Kritik Limitler
    if category_budgets:
        for b in category_budgets:
            if b.get('budget', 0) > 0:
                if b['spent'] > b['budget']:
                    diff = b['spent'] - b['budget']
                    notifications.append({
                        'id': f"cat_over_{b['id']}",
                        'severity': 'danger',
                        'badge': 'Bütçe Aşımı',
                        'icon': 'bi-exclamation-triangle-fill',
                        'title': f"{b['name']} Limiti Aşıldı!",
                        'message': f"{b['name']} bütçesi {currency_tr_filter(diff)} TL aşıldı (%{b['percent']:.0f} harcandı).",
                        'target_tab': 'transactions-tab'
                    })
                elif b['percent'] >= 85:
                    rem = b['budget'] - b['spent']
                    notifications.append({
                        'id': f"cat_warn_{b['id']}",
                        'severity': 'warning',
                        'badge': 'Kritik Seviye',
                        'icon': 'bi-speedometer2',
                        'title': f"{b['name']} Limite Yaklaştı",
                        'message': f"Bütçenizin %{b['percent']:.0f}'i kullanıldı, kalan harcama limiti: {currency_tr_filter(rem)} TL.",
                        'target_tab': 'transactions-tab'
                    })

    # 3. Kredi Kartı Ekstre Borcu
    if total_unpaid_cc > 0:
        notifications.append({
            'id': 'cc_unpaid',
            'severity': 'warning',
            'badge': 'Kredi Kartı',
            'icon': 'bi-credit-card-2-front-fill',
            'title': 'Ödenmemiş Kredi Kartı Ekstresi',
            'message': f'Toplam {currency_tr_filter(total_unpaid_cc)} TL ekstre borcunuz bulunmaktadır.',
            'target_tab': 'overview-tab'
        })

    # 4. Kumbara Hedefleri
    if savings_goals:
        for g in savings_goals:
            pct = g.get('percent', 0)
            if pct >= 100:
                notifications.append({
                    'id': f"goal_comp_{g['id']}",
                    'severity': 'success',
                    'badge': 'Tebrikler',
                    'icon': 'bi-trophy-fill',
                    'title': f"🎉 {g['title']} Hedefi Tamamlandı!",
                    'message': f"{g['title']} kumbaranızda hedef tutara ({currency_tr_filter(g['target_amount'])} TL) ulaşıldı!",
                    'target_tab': 'goals-tab'
                })
            elif pct >= 80:
                rem = g['target_amount'] - g['current_amount']
                notifications.append({
                    'id': f"goal_near_{g['id']}",
                    'severity': 'info',
                    'badge': f'%{pct:.0f} Ulaşıldı',
                    'icon': 'bi-piggy-bank-fill',
                    'title': f"{g['title']} Hedefine Çok Az Kaldı",
                    'message': f"Hedefe ulaşmak için son {currency_tr_filter(rem)} TL kaldı.",
                    'target_tab': 'goals-tab'
                })

    # 5. Portföy Durumu
    if total_portfolio_pnl_pct >= 10.0:
        notifications.append({
            'id': 'port_high',
            'severity': 'success',
            'badge': 'Portföy',
            'icon': 'bi-graph-up-arrow',
            'title': 'Yüksek Portföy Kârı',
            'message': f'Yatırımlarınız toplamda +%{total_portfolio_pnl_pct:.1f} getiri sağladı.',
            'target_tab': 'portfolio-tab'
        })
    elif total_portfolio_pnl_pct <= -10.0:
        notifications.append({
            'id': 'port_low',
            'severity': 'warning',
            'badge': 'Portföy',
            'icon': 'bi-graph-down-arrow',
            'title': 'Portföyde Geri Çekilme',
            'message': f'Yatırımlarınızda %{total_portfolio_pnl_pct:.1f} oranında düzeltme var.',
            'target_tab': 'portfolio-tab'
        })

    return notifications

def get_usdtry_rate():
    """USDTRY kurunu döner; iş parçacığı güvenli önbellek desteklidir."""
    now = time.time()
    usd_key = ('USDTRY=X', 'doviz')
    with _price_cache_lock:
        cached = PRICE_CACHE.get(usd_key)
    if cached and (now - cached[1] < CACHE_TTL):
        return cached[0]
    try:
        t_usd = yf.Ticker('USDTRY=X')
        p_usd = t_usd.fast_info.get('lastPrice')
        if p_usd and float(p_usd) > 0:
            rate = float(p_usd)
            with _price_cache_lock:
                PRICE_CACHE[usd_key] = (rate, now)
                _LAST_KNOWN_USDTRY['rate'] = rate
            return rate
    except Exception:
        pass
    return None


def get_effective_usdtry_rate():
    """Canlı USDTRY kuru alınamazsa son bilinen veya yapılandırılmış güvenli yedek kuru döner."""
    live = get_usdtry_rate()
    if live and live > 0:
        return live
    with _price_cache_lock:
        return _LAST_KNOWN_USDTRY.get('rate') or Config.DEFAULT_USD_TRY_FALLBACK


def fetch_live_price(symbol, asset_type):
    """
    BIST, ABD Borsası, TEFAS/BEFAS Fonları, Altın ve Döviz için anlık fiyat çeker.
    5 dakikalık iş parçacığı güvenli önbellek kullanır.
    """
    try:
        clean_sym = symbol.strip().upper()
        cache_key = (clean_sym, asset_type)
        now = time.time()
        with _price_cache_lock:
            cached = PRICE_CACHE.get(cache_key)
        if cached and (now - cached[1] < CACHE_TTL):
            return cached[0]

        # 1. TEFAS & BEFAS Fonları (Örn: TI2, TTE, MAC, YAY, AFT, ALR, CHH)
        if asset_type in ['tefas_fon', 'befas_fon', 'fon']:
            url = f"https://www.tefas.gov.tr/FonAnaliz.aspx?FonKod={clean_sym}"
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
            html = urllib.request.urlopen(req, timeout=8).read().decode('utf-8', errors='ignore')
            m = re.search(r'Son Fiyat \(TL\).*?>\s*([0-9]+,[0-9]+)\s*</p>', html, re.DOTALL)
            if m:
                val = round(float(m.group(1).replace(',', '.')), 6)
                with _price_cache_lock:
                    PRICE_CACHE[cache_key] = (val, now)
                return val
            return None

        # 2. ABD Borsası (US Stocks / ETFs: AAPL, MSFT, NVDA, TSLA, SPY, QQQ)
        if asset_type == 'abd_hisse':
            ticker = yf.Ticker(clean_sym)
            usd_price = ticker.fast_info.get('lastPrice')
            if usd_price:
                usd_val = round(float(usd_price), 2)
                usdtry = get_effective_usdtry_rate()
                tl_val = round(usd_val * usdtry, 2)
                data = {
                    'tl_price': tl_val,
                    'orig_price': usd_val,
                    'currency': 'USD'
                }
                with _price_cache_lock:
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
                with _price_cache_lock:
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
            with _price_cache_lock:
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
        OUTPUT INSERTED.id
        VALUES (?, ?, ?, ?)
    """, (user_id, name, cat_type, default_budget))
    return int(cursor.fetchone()[0])


_login_lock = threading.Lock()
_login_attempts = {}  # { ip: [timestamps] }
LOGIN_RATE_LIMIT_MAX = 10
LOGIN_RATE_LIMIT_WINDOW = 300  # 5 dakika


def _is_auto_login_allowed():
    """Hızlı test girişinin yalnızca debug/test ortamında veya açık izinle aktif olmasını sağlar."""
    if Config.is_production() and not app.config.get('TESTING'):
        return False
    return bool(app.debug or app.config.get('TESTING') or Config.is_auto_login_enabled())


def _get_client_ip():
    return request.headers.get('X-Forwarded-For', request.remote_addr or 'local').split(',')[0].strip()


def _is_login_rate_limited(ip):
    if app.config.get('TESTING'):
        return False
    now = time.time()
    with _login_lock:
        recent = [t for t in _login_attempts.get(ip, []) if now - t < LOGIN_RATE_LIMIT_WINDOW]
        if recent:
            _login_attempts[ip] = recent
        else:
            _login_attempts.pop(ip, None)
        return len(recent) >= LOGIN_RATE_LIMIT_MAX


def _record_login_failure(ip):
    if app.config.get('TESTING'):
        return
    now = time.time()
    with _login_lock:
        recent = [t for t in _login_attempts.get(ip, []) if now - t < LOGIN_RATE_LIMIT_WINDOW]
        recent.append(now)
        _login_attempts[ip] = recent


def _clear_login_failures(ip):
    with _login_lock:
        _login_attempts.pop(ip, None)


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

        if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
            flash("Lütfen geçerli bir e-posta adresi girin.", "danger")
            return render_template('register.html')

        if len(password) < 8:
            flash("Güvenliğiniz için şifreniz en az 8 karakter olmalıdır.", "danger")
            return render_template('register.html')

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT id FROM users WHERE username = ? OR email = ?", (username, email))
            if cursor.fetchone():
                flash("Bu kullanıcı adı veya e-posta zaten kullanımda.", "danger")
                return render_template('register.html')

            password_hash = generate_password_hash(password)
            cursor.execute("""
                INSERT INTO users (username, email, password_hash, first_name, last_name)
                OUTPUT INSERTED.id
                VALUES (?, ?, ?, ?, ?)
            """, (username, email, password_hash, first_name, last_name))
            new_user_id = int(cursor.fetchone()[0])

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
            print(f"[Register Error]: {e}")
            flash("Kayıt sırasında beklenmeyen bir hata oluştu. Lütfen tekrar deneyin.", "danger")
        finally:
            cursor.close()
            conn.close()

    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    allow_auto_login = _is_auto_login_allowed()
    if request.method == 'POST':
        client_ip = _get_client_ip()
        if _is_login_rate_limited(client_ip):
            flash("Çok fazla başarısız giriş denemesi. Lütfen birkaç dakika bekleyip tekrar deneyin.", "danger")
            return render_template('login.html', allow_auto_login=allow_auto_login), 429

        identifier = request.form.get('login_identifier', '').strip()
        password = request.form.get('password', '')

        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT id, username, first_name, password_hash 
                FROM users WHERE username = ? OR email = ?
            """, (identifier, identifier))
            user = cursor.fetchone()
        finally:
            cursor.close()
            conn.close()

        if user and check_password_hash(user[3], password):
            _clear_login_failures(client_ip)
            session.clear()
            session['user_id'] = user[0]
            session['username'] = user[1]
            session['first_name'] = user[2]
            session['_csrf_token'] = secrets.token_urlsafe(32)
            return redirect(url_for('dashboard'))

        _record_login_failure(client_ip)
        flash("Giriş bilgileri hatalı.", "danger")

    return render_template('login.html', allow_auto_login=allow_auto_login)


@app.route('/logout')
def logout():
    session.clear()
    flash("Çıkış yapıldı.", "info")
    return redirect(url_for('login'))


@app.route('/auto-login')
def auto_login():
    if not _is_auto_login_allowed():
        flash("Otomatik test girişi yalnızca geliştirme ortamında aktiftir.", "warning")
        return redirect(url_for('login'))

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT TOP 1 id, username, first_name FROM users ORDER BY id ASC")
        user = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()

    if user:
        session.clear()
        session['user_id'] = user[0]
        session['username'] = user[1]
        session['first_name'] = user[2]
        session['_csrf_token'] = secrets.token_urlsafe(32)
        flash(f"Hoş geldiniz {user[2]}, test oturumunuz başarıyla aktif edildi.", "success")
        return redirect(url_for('dashboard'))
    flash("Sistemde kayıtlı kullanıcı bulunamadı.", "warning")
    return redirect(url_for('login'))

# --- NAKİT BAKİYE VE AYLIK OTOMASYON YARDIMCILARI ---
INTERNAL_TRANSFER_CATEGORIES = (
    'Yatırım & Portföy',
    'Tasarruf & Kumbara',
    'Kumbara İadesi',
    'Tamamlanan Kumbara',
    'Kredi Kartı Borç Ödemesi'
)

def get_user_cash_balance(cursor, user_id):
    """Kullanıcının güncel kullanılabilir Vadesiz Nakit Bakiyesini hesaplar."""
    cursor.execute("SELECT ISNULL(initial_cash_balance, 0.0) FROM users WHERE id = ?", (user_id,))
    u_row = cursor.fetchone()
    user_init_cash = float(u_row[0] or 0.0) if u_row else 0.0

    cursor.execute("""
        SELECT ISNULL(SUM(CASE 
            WHEN c.type = 'gelir' THEN t.amount 
            ELSE (CASE WHEN ISNULL(t.payment_method, 'nakit') = 'nakit' THEN -t.amount ELSE 0 END) 
        END), 0)
        FROM transactions t
        JOIN categories c ON t.category_id = c.id
        WHERE t.user_id = ?
    """, (user_id,))
    net_cash_tx = float(cursor.fetchone()[0] or 0.0)
    return round(user_init_cash + net_cash_tx, 2)


_sync_locks_guard = threading.Lock()
_user_sync_locks = {}


def _get_user_sync_lock(user_id):
    with _sync_locks_guard:
        if user_id not in _user_sync_locks:
            _user_sync_locks[user_id] = threading.Lock()
        return _user_sync_locks[user_id]


def auto_sync_monthly_obligations(user_id, target_month=None, target_year=None, cursor=None, conn=None, force_manual=False):
    """
    Kullanıcının tanımladığı aylık düzenli işlemleri (recurring_transactions) ve
    otomatik yansıtma açık sabit abonelik/faturalarını (subscriptions) ilgili ay için
    idempotent ve kullanıcı kilidiyle eşzamanlılık-güvenli (race-condition safe) olarak
    transactions tablosuna yansıtır.
    """
    with _get_user_sync_lock(user_id):
        own_conn = False
        if conn is None or cursor is None:
            conn = get_db_connection()
            cursor = conn.cursor()
            own_conn = True

        now = datetime.today()
        month = target_month or now.month
        year = target_year or now.year
        last_day = calendar.monthrange(year, month)[1]
        period_key = f"{year:04d}-{month:02d}"
        synced_count = 0
        meta_updated = False

        try:
            # 1. Tekrarlayan İşlemler (recurring_transactions)
            cursor.execute("""
                SELECT id, category_id, title, amount, type, payment_method, day_of_month, last_synced_period
                FROM recurring_transactions
                WHERE user_id = ? AND is_active = 1
            """, (user_id,))
            rec_rows = cursor.fetchall()

            for r in rec_rows:
                rec_id, cat_id, title, amt, r_type, p_method, day, last_synced = (
                    r[0], r[1], r[2], float(r[3]), r[4], (r[5] or 'nakit'), int(r[6] or 1), r[7]
                )
                if not force_manual and last_synced == period_key:
                    continue

                actual_day = min(max(day, 1), last_day)
                tx_date = f"{year:04d}-{month:02d}-{actual_day:02d}"
                desc_tag = f"[Düzenli] {title}"

                cursor.execute("""
                    SELECT id FROM transactions
                    WHERE user_id = ? AND MONTH(transaction_date) = ? AND YEAR(transaction_date) = ?
                      AND (description = ? OR description = ?)
                """, (user_id, month, year, desc_tag, title))
                if cursor.fetchone():
                    if last_synced != period_key:
                        cursor.execute("UPDATE recurring_transactions SET last_synced_period = ? WHERE id = ?", (period_key, rec_id))
                        meta_updated = True
                    continue

                is_paid = 0 if p_method == 'kredi_karti' else 1
                cursor.execute("""
                    INSERT INTO transactions (user_id, category_id, amount, transaction_date, description, payment_method, is_paid)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (user_id, cat_id, amt, tx_date, desc_tag, p_method, is_paid))
                cursor.execute("UPDATE recurring_transactions SET last_synced_period = ? WHERE id = ?", (period_key, rec_id))
                synced_count += 1

            # 2. Sabit Abonelikler ve Faturalar (subscriptions)
            cursor.execute("""
                SELECT id, title, amount, billing_cycle, next_due_date,
                       ISNULL(payment_method, 'nakit') AS payment_method,
                       ISNULL(auto_apply, 1) AS auto_apply,
                       last_synced_period
                FROM subscriptions
                WHERE user_id = ? AND is_active = 1 AND ISNULL(auto_apply, 1) = 1
            """, (user_id,))
            sub_rows = cursor.fetchall()

            for s in sub_rows:
                sub_id, title, amt, cycle, due_date, p_method, auto_apply, last_synced = (
                    s[0], s[1], float(s[2]), s[3], s[4], (s[5] or 'nakit'), int(s[6]), s[7]
                )
                due_day = due_date.day if due_date else 1

                # Vadesi geçmiş aylarda kalan next_due_date'i güncel aya ilerlet
                if due_date and (due_date.year, due_date.month) < (now.year, now.month):
                    new_last_day = calendar.monthrange(now.year, now.month)[1]
                    new_due = f"{now.year:04d}-{now.month:02d}-{min(due_day, new_last_day):02d}"
                    cursor.execute("UPDATE subscriptions SET next_due_date = ? WHERE id = ?", (new_due, sub_id))
                    meta_updated = True

                if not force_manual and last_synced == period_key:
                    continue

                actual_day = min(max(due_day, 1), last_day)
                tx_date = f"{year:04d}-{month:02d}-{actual_day:02d}"
                desc_sub = f"[Fatura/Abonelik] {title}"
                desc_rec = f"[Düzenli] {title}"

                cursor.execute("""
                    SELECT id FROM transactions
                    WHERE user_id = ? AND MONTH(transaction_date) = ? AND YEAR(transaction_date) = ?
                      AND (description = ? OR description = ? OR description = ?)
                """, (user_id, month, year, desc_sub, desc_rec, title))
                if cursor.fetchone():
                    if last_synced != period_key:
                        cursor.execute("UPDATE subscriptions SET last_synced_period = ? WHERE id = ?", (period_key, sub_id))
                        meta_updated = True
                    continue

                cat_id = get_or_create_category(cursor, user_id, 'Faturalar', 'gider')
                is_paid = 0 if p_method == 'kredi_karti' else 1
                cursor.execute("""
                    INSERT INTO transactions (user_id, category_id, amount, transaction_date, description, payment_method, is_paid)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (user_id, cat_id, amt, tx_date, desc_sub, p_method, is_paid))
                cursor.execute("UPDATE subscriptions SET last_synced_period = ? WHERE id = ?", (period_key, sub_id))
                synced_count += 1

            if synced_count > 0 or meta_updated:
                conn.commit()
        except Exception as e:
            print(f"[auto_sync_monthly_obligations] Hata: {e}")
            conn.rollback()
        finally:
            if own_conn:
                cursor.close()
                conn.close()

        return synced_count


# --- ANA DASHBOARD ---
@app.route('/dashboard')
@login_required
def dashboard():
    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # Tarih Filtresi (Varsayılan: Mevcut Ay ve Yıl)
        now = datetime.today()
        selected_year = request.args.get('year', type=int) or now.year
        selected_month = request.args.get('month', type=int) or now.month
        if selected_month < 1 or selected_month > 12:
            selected_month = now.month

        # Aylık düzenli fatura, abonelik ve tekrarlayan işlemleri otomatik senkronize et
        if (selected_year, selected_month) <= (now.year, now.month):
            synced_count = auto_sync_monthly_obligations(user_id, selected_month, selected_year, cursor=cursor, conn=conn)
            if synced_count > 0:
                flash(f"Bu aya ait {synced_count} adet düzenli fatura/işlem otomatik olarak hesaplarınıza yansıtıldı.", "info")

        prev_month = 12 if selected_month == 1 else selected_month - 1
        prev_year = selected_year - 1 if selected_month == 1 else selected_year
        next_month = 1 if selected_month == 12 else selected_month + 1
        next_year = selected_year + 1 if selected_month == 12 else selected_year

        month_names = {
            1: 'Ocak', 2: 'Şubat', 3: 'Mart', 4: 'Nisan', 5: 'Mayıs', 6: 'Haziran',
            7: 'Temmuz', 8: 'Ağustos', 9: 'Eylül', 10: 'Ekim', 11: 'Kasım', 12: 'Aralık'
        }
        selected_month_name = month_names.get(selected_month, '')

        # 1. Finansal Özet: Operasyonel Gelir, Tüketim Nakit Gideri, Kredi Kartı Gideri
        # (Yatırım alım/satım, kumbara aktarım ve ekstre borç kapama gibi bilanço içi transferler aylık tüketim giderini şişirmez)
        cursor.execute("""
            SELECT 
                ISNULL(SUM(CASE 
                    WHEN c.type = 'gelir' AND c.name NOT IN ('Yatırım & Portföy', 'Kumbara İadesi', 'Tamamlanan Kumbara') 
                    THEN t.amount ELSE 0 END), 0) AS total_income,
                ISNULL(SUM(CASE 
                    WHEN c.type = 'gider' AND ISNULL(t.payment_method, 'nakit') = 'nakit' 
                         AND c.name NOT IN ('Yatırım & Portföy', 'Tasarruf & Kumbara', 'Kredi Kartı Borç Ödemesi') 
                    THEN t.amount ELSE 0 END), 0) AS cash_expense,
                ISNULL(SUM(CASE 
                    WHEN c.type = 'gider' AND t.payment_method = 'kredi_karti' AND ISNULL(t.is_paid, 0) = 0 
                    THEN t.amount ELSE 0 END), 0) AS cc_expense
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

        # 2. Kategori Bütçe Limitleri ve Harcama İlerlemeleri (Doğrudan Kurumsal Stored Procedure ile)
        cursor.execute(
            "EXEC sp_get_user_budget_health @user_id = ?, @month = ?, @year = ?",
            (user_id, selected_month, selected_year)
        )
        budgets_raw = cursor.fetchall()
        
        category_budgets = []
        chart_category_labels = []
        chart_category_values = []

        for b in budgets_raw:
            cat_id = b[0]
            cat_name = b[1]
            budget = float(b[2] or 0.0)
            spent = float(b[3] or 0.0)
            pct = round(float(b[5] or 0.0), 1) if len(b) > 5 else (round((spent / budget * 100), 1) if budget > 0 else 0.0)
            
            # Bütçe limiti olmayan iç transfer kategorilerini bütçe listesinde ve tüketim grafiğinde gösterme
            if cat_name in INTERNAL_TRANSFER_CATEGORIES and budget <= 0:
                continue

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
                   DATEDIFF(day, CAST(GETDATE() AS DATE), next_due_date) AS days_left,
                   ISNULL(payment_method, 'nakit') AS payment_method,
                   ISNULL(auto_apply, 1) AS auto_apply
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

        unique_pairs = list({(a[1], a[2]) for a in assets_raw})
        if len(unique_pairs) > 1:
            with ThreadPoolExecutor(max_workers=min(6, len(unique_pairs))) as pool:
                price_map = dict(zip(unique_pairs, pool.map(lambda p: fetch_live_price(p[0], p[1]), unique_pairs)))
        elif len(unique_pairs) == 1:
            p0 = unique_pairs[0]
            price_map = {p0: fetch_live_price(p0[0], p0[1])}
        else:
            price_map = {}

        for a in assets_raw:
            asset_id = a[0]
            symbol = a[1]
            atype = a[2]
            qty = float(a[3])
            avg_cost = float(a[4])

            price_info = price_map.get((symbol, atype))
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

        # 6. Kümülatif Net Servet (Net Worth) Bileşenleri (Tam Kapasite MSSQL View Destekli - Mükerrer Sorgusuz)
        cursor.execute("""
            SELECT initial_cash_balance, credit_card_limit, total_income, total_cash_expense, unpaid_cc_debt, total_savings
            FROM vw_user_financial_summary 
            WHERE user_id = ?
        """, (user_id,))
        vw_row = cursor.fetchone()
        if vw_row:
            user_initial_cash = float(vw_row[0] or 0.0)
            credit_card_limit = float(vw_row[1] or 50000.0)
            vw_total_income = float(vw_row[2] or 0.0)
            vw_total_cash_exp = float(vw_row[3] or 0.0)
            total_unpaid_cc = float(vw_row[4] or 0.0)
            total_savings_amount = float(vw_row[5] or 0.0)
        else:
            user_initial_cash = 0.0
            credit_card_limit = 50000.0
            vw_total_income = 0.0
            vw_total_cash_exp = 0.0
            total_unpaid_cc = 0.0
            total_savings_amount = 0.0

        cash_balance = round(user_initial_cash + vw_total_income - vw_total_cash_exp, 2)

        # Kredi Kartı Limit Doluluk Oranı (Finance Engine Modüler Servisi)
        cc_eval = evaluate_credit_utilization(total_unpaid_cc, credit_card_limit)
        available_card_limit = cc_eval['available_card_limit']
        cc_utilization_pct = cc_eval['cc_utilization_pct']
        cc_risk_status = cc_eval['cc_risk_status']
        cc_risk_level = cc_eval['cc_risk_level']
        cc_risk_badge_class = cc_eval['cc_risk_badge_class']
        cc_risk_bar_class = cc_eval['cc_risk_bar_class']
        cc_risk_icon = cc_eval['cc_risk_icon']
        cc_risk_advice = cc_eval['cc_risk_advice']

        total_net_worth = cash_balance + total_portfolio_value + total_savings_amount - total_unpaid_cc

        # 7. Portföy Varlık Dağılımı (Asset Allocation) & Çeşitlendirme (Diversification) Analizi
        PALETTE_COLORS = [
            '#4f46e5',  # Indigo
            '#06b6d4',  # Cyan
            '#10b981',  # Emerald
            '#f59e0b',  # Amber
            '#ec4899',  # Pink
            '#8b5cf6',  # Purple
            '#3b82f6',  # Blue
            '#14b8a6',  # Teal
            '#f97316',  # Orange
            '#64748b'   # Slate
        ]

        type_display_names = {
            'hisse': 'BIST Hisseleri',
            'abd_hisse': 'ABD Borsası ($)',
            'tefas_fon': 'TEFAS / BEFAS',
            'befas_fon': 'TEFAS / BEFAS',
            'fon': 'Yatırım Fonları',
            'doviz': 'Döviz',
            'emtia_altin': 'Altın & Emtia',
            'diger': 'Diğer'
        }

        # Sınıf bazlı kırılım (allocation_by_type)
        type_dict = {}
        for item in portfolio_items:
            raw_type = item.get('type', 'diger')
            display_type = type_display_names.get(raw_type, raw_type.title())
            cur_val = float(item.get('cur_val', 0.0))
            if display_type not in type_dict:
                type_dict[display_type] = {'value': 0.0, 'count': 0, 'symbols': []}
            type_dict[display_type]['value'] += cur_val
            type_dict[display_type]['count'] += 1
            type_dict[display_type]['symbols'].append(item.get('symbol', ''))

        allocation_by_type = []
        sorted_types = sorted(type_dict.items(), key=lambda x: x[1]['value'], reverse=True)
        for idx, (tname, info) in enumerate(sorted_types):
            val = round(info['value'], 2)
            pct = round((val / total_portfolio_value * 100), 2) if total_portfolio_value > 0 else 0.0
            color = PALETTE_COLORS[idx % len(PALETTE_COLORS)]
            allocation_by_type.append({
                'name': tname,
                'label': tname,
                'value': val,
                'pct': pct,
                'color': color,
                'count': info['count'],
                'symbols': info['symbols']
            })

        # Sembol / Varlık bazlı kırılım (allocation_by_symbol)
        symbol_dict = {}
        for item in portfolio_items:
            sym = item.get('symbol', 'Bilinmeyen')
            cur_val = float(item.get('cur_val', 0.0))
            raw_type = item.get('type', 'diger')
            display_type = type_display_names.get(raw_type, raw_type)
            if sym not in symbol_dict:
                symbol_dict[sym] = {'value': 0.0, 'type_name': display_type}
            symbol_dict[sym]['value'] += cur_val

        allocation_by_symbol = []
        sorted_symbols = sorted(symbol_dict.items(), key=lambda x: x[1]['value'], reverse=True)
        for idx, (sym, info) in enumerate(sorted_symbols):
            val = round(info['value'], 2)
            pct = round((val / total_portfolio_value * 100), 2) if total_portfolio_value > 0 else 0.0
            color = PALETTE_COLORS[idx % len(PALETTE_COLORS)]
            allocation_by_symbol.append({
                'name': sym,
                'label': sym,
                'value': val,
                'pct': pct,
                'color': color,
                'type_name': info['type_name']
            })

        # Konsantrasyon & Çeşitlendirme Skoru Analizi (Finance Engine Modüler Servisi)
        diversification = analyze_portfolio_concentration(
            allocations_by_symbol=allocation_by_symbol,
            allocations_by_type=allocation_by_type,
            total_portfolio_value=total_portfolio_value
        )

        # Eski genel bakış grafiği uyumluluğu
        chart_asset_labels = [item['label'] for item in allocation_by_type]
        chart_asset_values = [item['value'] for item in allocation_by_type]

        # 8. Son 6 Ayın Tarihsel Finansal Trendi (Tek Sorgu ile N+1 Optimizasyonu)
        past_months = []
        for i in range(5, -1, -1):
            m = selected_month - i
            y = selected_year
            while m <= 0:
                m += 12
                y -= 1
            past_months.append((y, m))

        start_y, start_m = past_months[0]
        end_y, end_m = past_months[-1]
        end_last_day = calendar.monthrange(end_y, end_m)[1]
        start_date_str = f"{start_y:04d}-{start_m:02d}-01"
        end_date_str = f"{end_y:04d}-{end_m:02d}-{end_last_day:02d}"

        cursor.execute("""
            SELECT 
                YEAR(t.transaction_date) AS yr,
                MONTH(t.transaction_date) AS mo,
                ISNULL(SUM(CASE 
                    WHEN c.type = 'gelir' AND c.name NOT IN ('Yatırım & Portföy', 'Kumbara İadesi', 'Tamamlanan Kumbara') 
                    THEN t.amount ELSE 0 END), 0) AS inc,
                ISNULL(SUM(CASE 
                    WHEN c.type = 'gider' AND c.name NOT IN ('Yatırım & Portföy', 'Tasarruf & Kumbara', 'Kredi Kartı Borç Ödemesi') 
                    THEN t.amount ELSE 0 END), 0) AS exp
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            WHERE t.user_id = ? AND t.transaction_date >= ? AND t.transaction_date <= ?
            GROUP BY YEAR(t.transaction_date), MONTH(t.transaction_date)
        """, (user_id, start_date_str, end_date_str))
        trend_map = {(int(r[0]), int(r[1])): (float(r[2]), float(r[3])) for r in cursor.fetchall()}

        trend_labels = []
        trend_income = []
        trend_expense = []
        trend_net = []

        for y, m in past_months:
            inc_val, exp_val = trend_map.get((y, m), (0.0, 0.0))
            net_val = inc_val - exp_val
            m_name = month_names.get(m, '')[:3]
            trend_labels.append(f"{m_name} '{str(y)[2:]}")
            trend_income.append(round(inc_val, 2))
            trend_expense.append(round(exp_val, 2))
            trend_net.append(round(net_val, 2))

        # 9. Akıllı Bildirimler (Fatura günleri, Kredi Kartı, Bütçe Aşımları)
        smart_alerts = []
        for sub in subscriptions:
            days_left = sub[5]
            sub_title = sub[1]
            sub_amt = float(sub[2])
            formatted_sub_amt = currency_tr_filter(sub_amt)
            if days_left < 0:
                smart_alerts.append({
                    'type': 'danger',
                    'icon': 'bi-exclamation-octagon-fill',
                    'title': 'Günü Geçmiş Ödeme',
                    'message': f"'{sub_title}' ({formatted_sub_amt} TL) son ödeme tarihi {abs(days_left)} gün önce geçti.",
                    'sub_id': sub[0]
                })
            elif days_left <= 3:
                smart_alerts.append({
                    'type': 'warning',
                    'icon': 'bi-alarm-fill',
                    'title': 'Yaklaşan Fatura',
                    'message': f"'{sub_title}' ({formatted_sub_amt} TL) için son {days_left} gün kaldı.",
                    'sub_id': sub[0]
                })

        if cc_expense > 0:
            smart_alerts.append({
                'type': 'warning',
                'icon': 'bi-credit-card-2-front-fill',
                'title': 'Ödenmemiş Kredi Kartı Ekstresi',
                'message': f"{selected_month_name} dönemine ait {currency_tr_filter(cc_expense)} TL tutarında ekstre borcunuz bulunmaktadır.",
                'action_pay_cc': True
            })
        elif total_unpaid_cc > 0:
            smart_alerts.append({
                'type': 'warning',
                'icon': 'bi-credit-card-2-front-fill',
                'title': 'Devreden Kredi Kartı Borcu',
                'message': f"Önceki dönemlerden devreden toplam {currency_tr_filter(total_unpaid_cc)} TL ödenmemiş kredi kartı borcunuz bulunmaktadır.",
                'action_pay_cc': True
            })

        for b in category_budgets:
            if b['budget'] > 0 and b['spent'] > b['budget']:
                over = b['spent'] - b['budget']
                smart_alerts.append({
                    'type': 'danger',
                    'icon': 'bi-lightning-charge-fill',
                    'title': f"Bütçe Aşımı ({b['name']})",
                    'message': f"Bu ayki bütçe sınırı {currency_tr_filter(over)} TL tutarında aşıldı (%{b['percent']}).",
                    'cat_id': b['id']
                })

        # 10. Düzenli & Tekrarlayan İşlemler (Maaş, Kira vb.)
        cursor.execute("""
            SELECT r.id, r.title, r.amount, r.type, r.payment_method, r.day_of_month, c.name, r.category_id 
            FROM recurring_transactions r 
            JOIN categories c ON r.category_id = c.id 
            WHERE r.user_id = ? AND r.is_active = 1 
            ORDER BY r.day_of_month ASC
        """, (user_id,))
        recurring_transactions = cursor.fetchall()

        # 11. İşlem Hareketleri (Seçilen Dönem - En Son 50 İşlem)
        cursor.execute("""
            SELECT TOP 50 t.id, t.amount, t.transaction_date, t.description, c.name, c.type, t.payment_method, c.id, t.is_paid, t.receipt_path
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
    finally:
        cursor.close()
        conn.close()

    savings_rate = round((net_savings / total_income * 100), 1) if total_income > 0 else 0.0

    # 12. Piyasa Benchmark & Fisher Denklemi ile Enflasyona Karşı Reel Getiri Analizi
    benchmark_data = get_benchmark_returns()
    tufe_rates = Config.TUFE_RATES
    period_labels = {'1M': '1 Aylık', '3M': '3 Aylık', '6M': '6 Aylık'}
    nom = total_portfolio_pnl_pct

    for p in ['1M', '3M', '6M']:
        if p not in benchmark_data:
            benchmark_data[p] = {'bist': 0.0, 'usd': 0.0, 'gold': 0.0}
        t_rate = tufe_rates.get(p, tufe_rates['1M'])
        benchmark_data[p]['tufe'] = t_rate
        benchmark_data[p]['period_label'] = period_labels.get(p, p)
        benchmark_data[p]['nominal_return'] = nom

        # Fisher Denklemi ile Enflasyona Karşı Reel Getiri (Finance Engine Modüler Servisi)
        fisher_res = calculate_fisher_real_return(nom, t_rate)
        benchmark_data[p]['real_return'] = fisher_res['real_return']
        benchmark_data[p]['real_status'] = {
            'label': fisher_res['status_label'],
            'badge_class': fisher_res['badge_class'],
            'icon': fisher_res['icon'],
            'desc': fisher_res['desc']
        }

    # 13. Akıllı Finansal Asistan & Harcama İçgörüleri (AI Insights)
    ai_insights = generate_financial_insights(
        total_income, total_expense, net_savings, savings_rate, 
        category_budgets, cc_expense, cash_expense, 
        total_portfolio_pnl_pct, benchmark_data
    )

    # 14. Uygulama İçi Bildirim ve Uyarılar
    notifications = get_in_app_notifications(subscriptions, category_budgets, total_unpaid_cc, savings_goals, total_portfolio_pnl_pct)
    notification_count = len(notifications)

    usd_try_rate = round(get_effective_usdtry_rate(), 2)

    return render_template(
        'dashboard.html',
        usd_try_rate=usd_try_rate,
        available_cash=cash_balance,
        savings_rate=savings_rate,
        total_income=total_income,
        cash_expense=cash_expense,
        cc_expense=cc_expense,
        total_expense=total_expense,
        net_savings=net_savings,
        total_net_worth=total_net_worth,
        cash_balance=cash_balance,
        initial_cash_balance=user_initial_cash,
        total_savings_amount=total_savings_amount,
        total_unpaid_cc=total_unpaid_cc,
        credit_card_limit=credit_card_limit,
        available_card_limit=available_card_limit,
        cc_utilization_pct=cc_utilization_pct,
        cc_risk_status=cc_risk_status,
        cc_risk_level=cc_risk_level,
        cc_risk_badge_class=cc_risk_badge_class,
        cc_risk_bar_class=cc_risk_bar_class,
        cc_risk_icon=cc_risk_icon,
        cc_risk_advice=cc_risk_advice,
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
        chart_payment_values=json.dumps([cash_expense, cc_expense]),
        chart_asset_labels=json.dumps(chart_asset_labels),
        chart_asset_values=json.dumps(chart_asset_values),
        allocation_by_type=allocation_by_type,
        allocation_by_symbol=allocation_by_symbol,
        diversification=diversification,
        allocation_by_type_json=json.dumps(allocation_by_type),
        allocation_by_symbol_json=json.dumps(allocation_by_symbol),
        chart_trend_labels=json.dumps(trend_labels),
        chart_trend_income=json.dumps(trend_income),
        chart_trend_expense=json.dumps(trend_expense),
        chart_trend_net=json.dumps(trend_net),
        smart_alerts=smart_alerts,
        recurring_transactions=recurring_transactions,
        benchmark_data=benchmark_data,
        ai_insights=ai_insights,
        notifications=notifications,
        notification_count=notification_count
    )

# --- EKONOMİ HABERLERİ API ---
@app.route('/api/economy-news')
@login_required
def api_economy_news():
    force = request.args.get('refresh', '0') == '1'
    news = get_economy_news(force_refresh=force)
    return {'success': True, 'news': news}

# --- CANLI PİYASA GÖSTERGELERİ (BIST 100, NASDAQ, GRAM ALTIN, GRAM GÜMÜŞ) ---
_market_summary_cache = {"data": None, "timestamp": 0}
_market_summary_lock = threading.Lock()
MARKET_CACHE_TTL = 300        # 5 dakika
MARKET_MIN_REFRESH = 30       # Manuel yenilemede yfinance'i korumak için minimum aralık (sn)
TROY_OUNCE_GRAMS = 31.1035


def _fetch_last_two_closes(symbol):
    """Bir sembolün son iki geçerli kapanışını (son fiyat, önceki kapanış) döner; hata halinde None."""
    try:
        hist = yf.Ticker(symbol).history(period='5d', interval='1d', timeout=8)
        closes = hist['Close'].dropna() if hist is not None and not hist.empty else None
        if closes is None or len(closes) < 2:
            return None
        return float(closes.iloc[-1]), float(closes.iloc[-2])
    except Exception as e:
        print(f"Piyasa verisi alınamadı ({symbol}):", e)
        return None


def _pct_change(last, prev):
    return round((last - prev) / prev * 100, 2) if prev else 0.0


def build_market_summary():
    """yfinance üzerinden BIST 100, Nasdaq, Gram Altın ve Gram Gümüş özetini üretir."""
    symbols = ['XU100.IS', '^IXIC', 'TRY=X', 'GC=F', 'SI=F']
    with ThreadPoolExecutor(max_workers=len(symbols)) as pool:
        quotes = dict(zip(symbols, pool.map(_fetch_last_two_closes, symbols)))

    # TRY=X boş dönerse alternatif USD/TRY sembolünü dene
    usd = quotes.get('TRY=X') or _fetch_last_two_closes('USDTRY=X')

    def item(code, name, currency, pair):
        if not pair:
            return {"code": code, "name": name, "price_fmt": "—", "change_pct": None,
                    "currency": currency, "available": False}
        last, prev = pair
        return {"code": code, "name": name, "price_fmt": currency_tr_filter(last, 2),
                "change_pct": _pct_change(last, prev), "currency": currency, "available": True}

    def gram_try(ounce_pair):
        if not ounce_pair or not usd:
            return None
        return ((ounce_pair[0] * usd[0]) / TROY_OUNCE_GRAMS,
                (ounce_pair[1] * usd[1]) / TROY_OUNCE_GRAMS)

    items = [
        item("XU100", "BIST 100", "TL", quotes.get('XU100.IS')),
        item("IXIC", "Nasdaq", "$", quotes.get('^IXIC')),
        item("GAU_TRY", "Gram Altın", "TL", gram_try(quotes.get('GC=F'))),
        item("XAG_TRY", "Gram Gümüş", "TL", gram_try(quotes.get('SI=F'))),
    ]
    return items


@app.route('/api/market-summary', methods=['GET'])
@login_required
def api_market_summary():
    now = time.time()
    force = request.args.get('refresh', '0') == '1'
    cached = _market_summary_cache["data"]
    age = now - _market_summary_cache["timestamp"]

    if cached and (age < MARKET_CACHE_TTL) and not (force and age >= MARKET_MIN_REFRESH):
        return {**cached, "cached": True}

    with _market_summary_lock:
        # Kilidi beklerken başka bir istek önbelleği tazelemiş olabilir
        cached = _market_summary_cache["data"]
        if cached and (time.time() - _market_summary_cache["timestamp"]) < MARKET_MIN_REFRESH:
            return {**cached, "cached": True}
        try:
            items = build_market_summary()
        except Exception as e:
            print("Piyasa özeti oluşturulamadı:", e)
            items = []

        if any(i.get("available") for i in items):
            payload = {"status": "success", "last_updated": datetime.now().strftime("%H:%M"), "items": items}
            _market_summary_cache["data"] = payload
            _market_summary_cache["timestamp"] = time.time()
            return {**payload, "cached": False}

    # Tüm kaynaklar başarısız veya offline: varsa eski önbelleği, yoksa güvenli fallback verisi dön (HTTP 200)
    if cached:
        return {**cached, "status": "stale", "cached": True}, 200
    fallback_items = [
        {"code": "XU100", "name": "BIST 100", "price_fmt": "—", "change_pct": 0.0, "currency": "TL", "available": False},
        {"code": "IXIC", "name": "Nasdaq", "price_fmt": "—", "change_pct": 0.0, "currency": "$", "available": False},
        {"code": "GAU_TRY", "name": "Gram Altın", "price_fmt": "—", "change_pct": 0.0, "currency": "TL", "available": False},
        {"code": "XAG_TRY", "name": "Gram Gümüş", "price_fmt": "—", "change_pct": 0.0, "currency": "TL", "available": False},
    ]
    return {"status": "fallback", "message": "Piyasa verileri çevrimdışı modda görüntülendi.",
            "last_updated": datetime.now().strftime("%H:%M"), "items": fallback_items, "cached": False}, 200

@app.route('/api/db-diagnostics', methods=['GET'])
@login_required
def api_db_diagnostics():
    """
    Veritabanı ilişkisel VIEW (vw_user_financial_summary) ve 
    STORED PROCEDURE (sp_get_user_budget_health) nesnelerinin canlı teşhis uç noktası.
    """
    user_id = session['user_id']
    now = datetime.now()
    
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # 1. SQL View Sorgusu
        cursor.execute("SELECT * FROM vw_user_financial_summary WHERE user_id = ?", (user_id,))
        view_row = cursor.fetchone()
        columns = [col[0] for col in cursor.description] if cursor.description else []
        view_data = {}
        if view_row:
            for k, v in zip(columns, view_row):
                if hasattr(v, '__float__') or str(type(v)).find('Decimal') != -1:
                    view_data[k] = float(v)
                else:
                    view_data[k] = v

        # 2. Stored Procedure Çağrısı
        cursor.execute("EXEC sp_get_user_budget_health @user_id = ?, @month = ?, @year = ?", (user_id, now.month, now.year))
        sp_rows = cursor.fetchall()
        sp_cols = [col[0] for col in cursor.description] if cursor.description else []
        budget_health = []
        for r in sp_rows:
            row_dict = {}
            for k, val in zip(sp_cols, r):
                if hasattr(val, '__float__') or str(type(val)).find('Decimal') != -1:
                    row_dict[k] = float(val)
                else:
                    row_dict[k] = val
            budget_health.append(row_dict)

        return {
            'status': 'healthy',
            'timestamp': now.isoformat(),
            'view': {
                'name': 'vw_user_financial_summary',
                'record_count': 1 if view_row else 0,
                'data': view_data
            },
            'stored_procedure': {
                'name': 'sp_get_user_budget_health',
                'parameters': {'user_id': user_id, 'month': now.month, 'year': now.year},
                'categories_analyzed': len(budget_health),
                'results': budget_health
            }
        }
    except Exception as e:
        return {'status': 'error', 'message': str(e)}, 500
    finally:
        cursor.close()
        conn.close()


@app.route('/api/scan-receipt', methods=['POST'])
@login_required
def api_scan_receipt():
    """
    Akıllı Fiş & Fatura Okuma (OCR & AI Kategorizasyon) Uç Noktası.
    JPG, PNG, WEBP veya PDF (maks. 5 MB) dosyasını okuyup tutar, tarih, işletme adı
    ve kullanıcının mevcut veritabanı kategorileri arasından en uygun kategoriyi döner.
    """
    receipt_file = request.files.get('receipt') or request.files.get('file')
    if not receipt_file or not receipt_file.filename:
        return {
            'success': False,
            'error': 'Lütfen taranacak bir fiş veya fatura dosyası seçin.'
        }, 400

    if not is_allowed_scan_file(receipt_file.filename):
        return {
            'success': False,
            'error': 'Desteklenmeyen dosya türü. Lütfen JPG, PNG, WEBP veya PDF yükleyin.'
        }, 400

    file_bytes = receipt_file.read(MAX_RECEIPT_SCAN_BYTES + 1)
    if not file_bytes:
        return {
            'success': False,
            'error': 'Yüklenen dosya boş.'
        }, 400

    if len(file_bytes) > MAX_RECEIPT_SCAN_BYTES:
        return {
            'success': False,
            'error': 'Yüklenen dosya boyutu çok büyük (maksimum 5 MB).'
        }, 413

    user_id = session['user_id']
    categories = []
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT id, name, type FROM categories WHERE user_id = ? ORDER BY type, name",
            (user_id,)
        )
        for row in cursor.fetchall():
            categories.append({
                'id': int(row[0]),
                'name': str(row[1]),
                'type': str(row[2])
            })
    except Exception as e:
        print(f"[api_scan_receipt DB Error]: {e}")
    finally:
        cursor.close()
        conn.close()

    result = scan_receipt(
        file_bytes=file_bytes,
        filename=receipt_file.filename,
        categories=categories,
        content_type=receipt_file.mimetype,
    )
    status_code = 200 if result.get('success') else 400
    return result, status_code

# --- İŞLEMLER (ACTIONS) ---

# --- DIŞA AKTARMA (EXPORT) İŞLEMLERİ ---
@app.route('/export/transactions')
@login_required
def export_transactions():
    user_id = session['user_id']
    export_format = request.args.get('format', 'excel').lower()
    month = request.args.get('month', type=int)
    year = request.args.get('year', type=int)

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if month and year:
            cursor.execute("""
                SELECT t.id, t.transaction_date, c.name, c.type, t.description, t.payment_method, t.is_paid, t.amount
                FROM transactions t
                JOIN categories c ON t.category_id = c.id
                WHERE t.user_id = ? AND MONTH(t.transaction_date) = ? AND YEAR(t.transaction_date) = ?
                ORDER BY t.transaction_date DESC, t.id DESC
            """, (user_id, month, year))
            filename_prefix = f"islemler_{year}_{month:02d}"
        else:
            cursor.execute("""
                SELECT t.id, t.transaction_date, c.name, c.type, t.description, t.payment_method, t.is_paid, t.amount
                FROM transactions t
                JOIN categories c ON t.category_id = c.id
                WHERE t.user_id = ?
                ORDER BY t.transaction_date DESC, t.id DESC
            """, (user_id,))
            filename_prefix = "tum_islemler"

        rows = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    data = []
    for r in rows:
        cat_name = r[2] or ''
        base_type = 'Gelir' if r[3] == 'gelir' else 'Gider'
        tx_type_label = f"İç Transfer ({base_type})" if cat_name in INTERNAL_TRANSFER_CATEGORIES else base_type
        data.append({
            'İşlem No': r[0],
            'Tarih': r[1].strftime('%Y-%m-%d') if r[1] else '',
            'Kategori': cat_name,
            'Tür': tx_type_label,
            'Açıklama': r[4] or '',
            'Ödeme Yöntemi': 'Kredi Kartı' if r[5] == 'kredi_karti' else 'Nakit',
            'Durum': 'Ödendi' if r[6] == 1 else 'Ödenmemiş',
            'Tutar (TL)': float(r[7])
        })

    df = pd.DataFrame(data)

    if export_format == 'csv':
        output = io.StringIO()
        df.to_csv(output, index=False, encoding='utf-8-sig')
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment;filename={filename_prefix}.csv"}
        )
    else:
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='İşlemler')
        output.seek(0)
        return send_file(
            output,
            as_attachment=True,
            download_name=f"{filename_prefix}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

@app.route('/export/portfolio')
@login_required
def export_portfolio():
    user_id = session['user_id']
    export_format = request.args.get('format', 'excel').lower()

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT id, asset_symbol, asset_type, total_quantity, average_cost
            FROM portfolio_assets
            WHERE user_id = ? AND total_quantity > 0
        """, (user_id,))
        assets_raw = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

    type_map = {
        'hisse': 'Borsa İstanbul (BIST)',
        'abd_hisse': 'ABD Borsası (USD)',
        'tefas_fon': 'TEFAS / BEFAS Fonu',
        'befas_fon': 'TEFAS / BEFAS Fonu',
        'fon': 'Yatırım Fonu',
        'doviz': 'Döviz',
        'emtia_altin': 'Altın & Emtia',
        'diger': 'Diğer'
    }

    data = []
    for a in assets_raw:
        symbol = a[1]
        atype = a[2]
        qty = float(a[3])
        avg_cost = float(a[4])

        price_info = fetch_live_price(symbol, atype)
        if price_info is None:
            live_price = avg_cost
        elif isinstance(price_info, dict):
            live_price = float(price_info['tl_price'])
        else:
            live_price = float(price_info)

        cost_val = qty * avg_cost
        cur_val = qty * live_price
        pnl = cur_val - cost_val
        pnl_pct = round((pnl / cost_val * 100), 2) if cost_val > 0 else 0.0

        data.append({
            'Sembol': symbol,
            'Piyasa / Varlık Türü': type_map.get(atype, atype),
            'Adet / Pay': qty,
            'Birim Maliyet (TL)': avg_cost,
            'Canlı Fiyat (TL)': live_price,
            'Toplam Maliyet (TL)': round(cost_val, 2),
            'Toplam Güncel Değer (TL)': round(cur_val, 2),
            'Kâr/Zarar (TL)': round(pnl, 2),
            'Kâr/Zarar (%)': pnl_pct
        })

    df = pd.DataFrame(data)

    if export_format == 'csv':
        output = io.StringIO()
        df.to_csv(output, index=False, encoding='utf-8-sig')
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment;filename=portfoy_raporu.csv"}
        )
    else:
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Portföy')
        output.seek(0)
        return send_file(
            output,
            as_attachment=True,
            download_name="portfoy_raporu.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

def redirect_back_to_dashboard(default_tab=None, month=None, year=None):
    """Form veya referrer URL'sinden seçili ay/yıl ve aktif sekmeyi koruyarak dashboard'a yönlendirir."""
    from urllib.parse import urlparse, parse_qs

    req_month = month or request.form.get('month', type=int) or request.args.get('month', type=int)
    req_year = year or request.form.get('year', type=int) or request.args.get('year', type=int)
    req_tab = (request.form.get('active_tab') or request.args.get('tab') or '').strip()

    if request.referrer:
        try:
            parsed = urlparse(request.referrer)
            qs = parse_qs(parsed.query)
            if not req_month and 'month' in qs and qs['month']:
                req_month = int(qs['month'][0])
            if not req_year and 'year' in qs and qs['year']:
                req_year = int(qs['year'][0])
            if not req_tab and 'tab' in qs and qs['tab']:
                req_tab = qs['tab'][0].strip()
        except Exception:
            pass

    final_tab = req_tab or default_tab
    if final_tab:
        final_tab = final_tab.replace('tab-', '').replace('-tab', '')
        if final_tab == 'recurring':
            final_tab = 'goals'
        if final_tab not in ('overview', 'portfolio', 'transactions', 'goals'):
            final_tab = None

    params = {}
    if req_month and 1 <= int(req_month) <= 12:
        params['month'] = int(req_month)
    if req_year and 2000 <= int(req_year) <= 2100:
        params['year'] = int(req_year)
    if final_tab and final_tab != 'overview':
        params['tab'] = final_tab

    return redirect(url_for('dashboard', **params))

# --- DÜZENLİ / TEKRARLAYAN İŞLEMLER (RECURRING) ---
@app.route('/add_recurring', methods=['POST'])
@login_required
def add_recurring():
    user_id = session['user_id']
    title = request.form.get('title', '').strip()
    category_id = request.form.get('category_id')
    safe_amount = parse_safe_amount(request.form.get('amount'))
    rec_type = request.form.get('type', 'gider')
    payment_method = request.form.get('payment_method', 'nakit')
    day_of_month = int(request.form.get('day_of_month', 1))

    if not title or safe_amount is None or not category_id:
        flash("Geçerli bir başlık, kategori ve pozitif tutar girin.", "warning")
        return redirect_back_to_dashboard(default_tab='goals')

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, type FROM categories WHERE id = ? AND user_id = ?", (category_id, user_id))
        cat_row = cursor.fetchone()
        if not cat_row:
            flash("Geçersiz kategori seçimi.", "warning")
            return redirect_back_to_dashboard(default_tab='goals')
        rec_type = cat_row[1]

        cursor.execute("""
            INSERT INTO recurring_transactions (user_id, category_id, title, amount, type, payment_method, day_of_month, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
        """, (user_id, category_id, title, safe_amount, rec_type, payment_method, day_of_month))
        conn.commit()
        flash(f"'{title}' düzenli işlem şablonu başarıyla eklendi.", "success")
    except Exception as e:
        conn.rollback()
        print(f"[add_recurring Error]: {e}")
        flash("Düzenli işlem eklenirken beklenmeyen bir hata oluştu.", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect_back_to_dashboard(default_tab='goals')

@app.route('/delete_recurring/<int:rec_id>', methods=['POST'])
@login_required
def delete_recurring(rec_id):
    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM recurring_transactions WHERE id = ? AND user_id = ?", (rec_id, user_id))
        conn.commit()
        flash("Düzenli işlem şablonu silindi.", "info")
    except Exception as e:
        conn.rollback()
        print(f"[delete_recurring Error]: {e}")
        flash("Düzenli işlem silinirken bir hata oluştu.", "danger")
    finally:
        cursor.close()
        conn.close()
    return redirect_back_to_dashboard(default_tab='goals')

@app.route('/apply_recurring', methods=['POST'])
@login_required
def apply_recurring():
    user_id = session['user_id']
    month = request.form.get('month', type=int) or datetime.today().month
    year = request.form.get('year', type=int) or datetime.today().year

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        added_count = auto_sync_monthly_obligations(user_id, month, year, cursor=cursor, conn=conn, force_manual=True)
        if added_count > 0:
            flash(f"{month}/{year} dönemi için {added_count} adet düzenli işlem/fatura başarıyla uygulandı!", "success")
        else:
            flash("Tüm düzenli işlemler ve faturalar bu dönem için zaten eklenmiş veya aktif şablon bulunmuyor.", "info")
    except Exception as e:
        conn.rollback()
        print(f"[apply_recurring Error]: {e}")
        flash("Düzenli işlemler uygulanırken bir hata oluştu.", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect_back_to_dashboard(default_tab='goals', month=month, year=year)

# --- İŞLEMLER (ACTIONS) ---

@app.route('/add_transaction', methods=['POST'])
@login_required
def add_transaction():
    user_id = session['user_id']
    category_id = request.form.get('category_id')
    amount = request.form.get('amount')
    transaction_date = request.form.get('transaction_date') or datetime.now().strftime('%Y-%m-%d')
    description = request.form.get('description', '').strip()
    payment_method = request.form.get('payment_method', 'nakit')
    is_paid = 0 if payment_method == 'kredi_karti' else 1

    safe_amount = parse_safe_amount(amount)
    if safe_amount is None:
        flash("Lütfen geçerli ve pozitif bir işlem tutarı giriniz.", "warning")
        return redirect_back_to_dashboard()

    if not category_id:
        flash("Lütfen geçerli bir kategori seçiniz.", "warning")
        return redirect_back_to_dashboard()

    receipt_file = request.files.get('receipt')
    if receipt_file and receipt_file.filename and not allowed_file(receipt_file.filename):
        flash("İzin verilmeyen fiş dosya türü. Lütfen PNG, JPG, GIF, WEBP veya PDF yükleyin.", "warning")
        return redirect_back_to_dashboard()

    receipt_filename = None
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, type FROM categories WHERE id = ? AND user_id = ?", (category_id, user_id))
        if not cursor.fetchone():
            flash("Geçersiz kategori seçimi.", "warning")
            return redirect_back_to_dashboard()

        receipt_filename, receipt_err = _save_receipt_if_valid(receipt_file)
        if receipt_err:
            flash(receipt_err, "warning")
            return redirect_back_to_dashboard()

        cursor.execute("""
            INSERT INTO transactions (user_id, category_id, amount, transaction_date, description, payment_method, is_paid, receipt_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, category_id, safe_amount, transaction_date, description, payment_method, is_paid, receipt_filename))
        conn.commit()
        flash("İşlem başarıyla kaydedildi.", "success")
    except Exception as e:
        conn.rollback()
        if receipt_filename:
            try:
                r_path = os.path.join(UPLOAD_FOLDER, receipt_filename)
                if os.path.exists(r_path):
                    os.remove(r_path)
            except Exception:
                pass
        print(f"[add_transaction Error]: {e}")
        flash("İşlem kaydedilirken bir hata oluştu.", "danger")
    finally:
        cursor.close()
        conn.close()
    return redirect_back_to_dashboard()

@app.route('/update-initial-balance', methods=['POST'])
@login_required
def update_initial_balance():
    user_id = session['user_id']
    amount = parse_safe_amount(request.form.get('initial_cash_balance'), min_val=0.0)
    if amount is None:
        flash('Geçerli bir nakit bakiye tutarı giriniz.', 'danger')
        return redirect_back_to_dashboard()

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # Mevcut nakit işlemlerin net etkisini hesapla ki kullanıcının girdiği tutar doğrudan güncel Vadesiz Nakit Bakiyesi olsun
        cursor.execute("""
            SELECT ISNULL(SUM(CASE 
                WHEN c.type = 'gelir' THEN t.amount 
                ELSE (CASE WHEN ISNULL(t.payment_method, 'nakit') = 'nakit' THEN -t.amount ELSE 0 END) 
            END), 0)
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            WHERE t.user_id = ?
        """, (user_id,))
        net_cash_tx = float(cursor.fetchone()[0] or 0.0)
        new_initial_cash = round(amount - net_cash_tx, 2)

        cursor.execute("UPDATE users SET initial_cash_balance = ? WHERE id = ?", (new_initial_cash, user_id))
        conn.commit()
        flash('Vadesiz nakit bakiyeniz başarıyla güncellendi.', 'success')
    except Exception as e:
        conn.rollback()
        print(f"[update_initial_balance Error]: {e}")
        flash('Bakiye güncellenirken bir hata oluştu.', 'danger')
    finally:
        cursor.close()
        conn.close()
    return redirect_back_to_dashboard()

@app.route('/update-card-limit', methods=['POST'])
@app.route('/update_card_limit', methods=['POST'])
@login_required
def update_card_limit():
    user_id = session['user_id']
    limit = parse_safe_amount(request.form.get('credit_card_limit'), min_val=0.01)
    if limit is None:
        flash("Geçerli bir kredi kartı limit tutarı giriniz (0'dan büyük olmalıdır).", "warning")
        return redirect_back_to_dashboard()

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE users SET credit_card_limit = ? WHERE id = ?", (limit, user_id))
        conn.commit()
        flash(f"Kredi kartı limitiniz {currency_tr_filter(limit)} TL olarak başarıyla güncellendi.", "success")
    except Exception as e:
        conn.rollback()
        print(f"[update_card_limit Error]: {e}")
        flash("Limit güncellenirken bir hata oluştu.", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect_back_to_dashboard()

@app.route('/edit_transaction/<int:tx_id>', methods=['POST'])
@login_required
def edit_transaction(tx_id):
    user_id = session['user_id']
    category_id = request.form.get('category_id')
    amount = request.form.get('amount')
    transaction_date = request.form.get('transaction_date') or datetime.now().strftime('%Y-%m-%d')
    description = request.form.get('description', '').strip()
    payment_method = request.form.get('payment_method', 'nakit')

    safe_amount = parse_safe_amount(amount)
    if safe_amount is None or not category_id:
        flash("Lütfen geçerli bir kategori ve pozitif işlem tutarı giriniz.", "warning")
        return redirect_back_to_dashboard(default_tab='transactions')

    receipt_file = request.files.get('receipt')
    if receipt_file and receipt_file.filename and not allowed_file(receipt_file.filename):
        flash("İzin verilmeyen fiş dosya türü. Lütfen PNG, JPG, GIF, WEBP veya PDF yükleyin.", "warning")
        return redirect_back_to_dashboard(default_tab='transactions')

    receipt_filename = None
    old_receipt_path = None
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, type FROM categories WHERE id = ? AND user_id = ?", (category_id, user_id))
        if not cursor.fetchone():
            flash("Geçersiz kategori seçimi.", "warning")
            return redirect_back_to_dashboard(default_tab='transactions')

        # Mevcut işlemi kontrol et (Çift yönlü veri tutarlılığı)
        cursor.execute("""
            SELECT t.amount, ISNULL(t.description, ''), c.name, ISNULL(t.payment_method, 'nakit'), ISNULL(t.is_paid, 1), t.receipt_path
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            WHERE t.id = ? AND t.user_id = ?
        """, (tx_id, user_id))
        existing_tx = cursor.fetchone()
        if not existing_tx:
            flash("Düzenlenecek işlem bulunamadı.", "warning")
            return redirect_back_to_dashboard(default_tab='transactions')

        old_amt = float(existing_tx[0] or 0.0)
        old_desc = str(existing_tx[1] or '').strip()
        old_cat_name = str(existing_tx[2] or '').strip()
        old_payment_method = str(existing_tx[3] or 'nakit').strip()
        old_is_paid = int(existing_tx[4]) if existing_tx[4] is not None else 1
        old_receipt_path = existing_tx[5]

        if payment_method == 'nakit':
            new_is_paid = 1
        else:
            new_is_paid = 0 if old_payment_method == 'nakit' else old_is_paid

        # Otomatik üretilen Portföy veya Kredi Kartı Ekstre kapama işlemlerinde tutar değişikliğini koru
        if old_cat_name in ('Yatırım & Portföy', 'Kredi Kartı Borç Ödemesi') and abs(safe_amount - old_amt) > 0.005:
            flash("Portföy alış/satış ve kredi kartı ekstre kapama işlemlerinin tutarı doğrudan değiştirilemez. İşlemi silip yeniden oluşturabilirsiniz.", "warning")
            return redirect_back_to_dashboard(default_tab='transactions')

        # Kumbara aktarımı düzenleniyorsa aradaki farkı kumbaraya yansıt
        if old_cat_name == 'Tasarruf & Kumbara' or old_desc.startswith('Kumbaraya Ekleme:'):
            diff = round(safe_amount - old_amt, 2)
            if diff > 0.005:
                avail_cash = get_user_cash_balance(cursor, user_id)
                if avail_cash + 0.005 < diff:
                    flash(f"Yetersiz Vadesiz Nakit Bakiye! Ek {currency_tr_filter(diff)} TL kumbara aktarımı için nakit bakiyeniz yeterli değil.", "danger")
                    return redirect_back_to_dashboard(default_tab='transactions')

            goal_title = old_desc.split('Kumbaraya Ekleme:', 1)[1].strip() if 'Kumbaraya Ekleme:' in old_desc else None
            if goal_title:
                cursor.execute("SELECT id, target_amount, current_amount, status FROM savings_goals WHERE user_id = ? AND title = ?", (user_id, goal_title))
            else:
                cursor.execute("SELECT TOP 1 id, target_amount, current_amount, status FROM savings_goals WHERE user_id = ? ORDER BY id DESC", (user_id,))
            g_row = cursor.fetchone()
            if g_row:
                g_id, g_target, g_curr, g_status = g_row[0], float(g_row[1] or 0.0), float(g_row[2] or 0.0), g_row[3]
                new_curr = max(0.0, round(g_curr + diff, 2))
                new_status = 'devam_ediyor' if (g_status == 'tamamlandi' and new_curr < g_target) else g_status
                cursor.execute("UPDATE savings_goals SET current_amount = ?, status = ? WHERE id = ?", (new_curr, new_status, g_id))

        # Doğrulamalar geçildikten sonra yeni fişi kaydet
        receipt_filename, receipt_err = _save_receipt_if_valid(receipt_file)
        if receipt_err:
            conn.rollback()
            flash(receipt_err, "warning")
            return redirect_back_to_dashboard(default_tab='transactions')

        if receipt_filename:
            cursor.execute("""
                UPDATE transactions 
                SET category_id = ?, amount = ?, transaction_date = ?, description = ?, payment_method = ?, is_paid = ?, receipt_path = ?
                WHERE id = ? AND user_id = ?
            """, (category_id, safe_amount, transaction_date, description, payment_method, new_is_paid, receipt_filename, tx_id, user_id))
        else:
            cursor.execute("""
                UPDATE transactions 
                SET category_id = ?, amount = ?, transaction_date = ?, description = ?, payment_method = ?, is_paid = ?
                WHERE id = ? AND user_id = ?
            """, (category_id, safe_amount, transaction_date, description, payment_method, new_is_paid, tx_id, user_id))
        conn.commit()

        # Yeni fiş başarıyla kaydedildiyse eski fişi diskten temizle
        if receipt_filename and old_receipt_path and old_receipt_path != receipt_filename:
            try:
                old_full = os.path.join(UPLOAD_FOLDER, old_receipt_path)
                if os.path.exists(old_full):
                    os.remove(old_full)
            except Exception:
                pass

        flash("İşlem güncellendi.", "success")
    except Exception as e:
        conn.rollback()
        if receipt_filename:
            try:
                new_full = os.path.join(UPLOAD_FOLDER, receipt_filename)
                if os.path.exists(new_full):
                    os.remove(new_full)
            except Exception:
                pass
        print(f"[edit_transaction Error]: {e}")
        flash("İşlem güncellenirken bir hata oluştu.", "danger")
    finally:
        cursor.close()
        conn.close()
    return redirect_back_to_dashboard(default_tab='transactions')

@app.route('/delete_transaction/<int:tx_id>', methods=['POST'])
@login_required
def delete_transaction(tx_id):
    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT t.amount, ISNULL(t.description, ''), t.payment_method, t.transaction_date,
                   t.receipt_path, c.name, c.type
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            WHERE t.id = ? AND t.user_id = ?
        """, (tx_id, user_id))
        tx_row = cursor.fetchone()
        if not tx_row:
            flash("Silinecek işlem bulunamadı.", "warning")
            return redirect_back_to_dashboard(default_tab='transactions')

        tx_amt = float(tx_row[0] or 0.0)
        tx_desc = str(tx_row[1] or '').strip()
        tx_date = tx_row[3]
        receipt_path = tx_row[4]
        cat_name = str(tx_row[5] or '').strip()

        # 1. Portföy Alış / Satış İşlemi Siliniyorsa: Portföy Adetlerini ve Hareketlerini Senkronize Et
        if cat_name == 'Yatırım & Portföy' or 'Varlık Alışı' in tx_desc or 'Varlık Alımı' in tx_desc or 'Varlık Satışı' in tx_desc:
            buy_m1 = re.match(r'^([A-Z0-9._-]+)\s+Varlık Alışı\s*\(([\d.,]+)\s*Adet\)', tx_desc)
            buy_m2 = re.search(r'Varlık Alımı:\s*([\d.,]+)\s*adet\s+([A-Z0-9._-]+)', tx_desc)
            sell_m = re.match(r'^([A-Z0-9._-]+)\s+Varlık Satışı\s*\(([\d.,]+)\s*Adet\)', tx_desc)

            if buy_m1 or buy_m2:
                sym = buy_m1.group(1) if buy_m1 else buy_m2.group(2)
                qty_str = buy_m1.group(2) if buy_m1 else buy_m2.group(1)
                parsed_qty = parse_safe_amount(qty_str, min_val=0.0001) or 0.0
                cursor.execute("SELECT id, total_quantity FROM portfolio_assets WHERE user_id = ? AND asset_symbol = ?", (user_id, sym))
                p_row = cursor.fetchone()
                if p_row:
                    asset_id, cur_qty = p_row[0], float(p_row[1] or 0.0)
                    cursor.execute("""
                        SELECT TOP 1 id, quantity FROM asset_transactions 
                        WHERE asset_id = ? AND user_id = ? AND transaction_type = 'alis' 
                        ORDER BY id DESC
                    """, (asset_id, user_id))
                    at_row = cursor.fetchone()
                    exact_qty = float(at_row[1] or 0.0) if at_row else parsed_qty
                    if cur_qty + 0.0001 < exact_qty:
                        flash(f"{sym} varlığına ait paylar portföyde satıldığı için bu alış kaydı doğrudan silinemez.", "warning")
                        return redirect_back_to_dashboard(default_tab='transactions')
                    new_qty = 0.0 if (cur_qty - exact_qty) <= 0.0001 else round(cur_qty - exact_qty, 4)
                    if at_row:
                        cursor.execute("DELETE FROM asset_transactions WHERE id = ?", (at_row[0],))
                    if new_qty <= 0.0001:
                        new_avg = 0.0
                    else:
                        cursor.execute("""
                            SELECT SUM(quantity * unit_price), SUM(quantity)
                            FROM asset_transactions
                            WHERE asset_id = ? AND user_id = ? AND transaction_type = 'alis'
                        """, (asset_id, user_id))
                        wac_row = cursor.fetchone()
                        tot_c = float(wac_row[0] or 0.0) if wac_row else 0.0
                        tot_q = float(wac_row[1] or 0.0) if wac_row else 0.0
                        new_avg = round(tot_c / tot_q, 4) if tot_q > 0 else 0.0
                    cursor.execute("UPDATE portfolio_assets SET total_quantity = ?, average_cost = ? WHERE id = ?", (new_qty, new_avg, asset_id))
            elif sell_m:
                sym = sell_m.group(1)
                parsed_qty = parse_safe_amount(sell_m.group(2), min_val=0.0001) or 0.0
                cursor.execute("SELECT id, total_quantity FROM portfolio_assets WHERE user_id = ? AND asset_symbol = ?", (user_id, sym))
                p_row = cursor.fetchone()
                if p_row and parsed_qty > 0:
                    asset_id, cur_qty = p_row[0], float(p_row[1] or 0.0)
                    cursor.execute("""
                        SELECT TOP 1 id, quantity FROM asset_transactions 
                        WHERE asset_id = ? AND user_id = ? AND transaction_type = 'satis' 
                        ORDER BY id DESC
                    """, (asset_id, user_id))
                    at_row = cursor.fetchone()
                    exact_qty = float(at_row[1] or 0.0) if at_row else parsed_qty
                    cursor.execute("UPDATE portfolio_assets SET total_quantity = ? WHERE id = ?", (round(cur_qty + exact_qty, 4), asset_id))
                    if at_row:
                        cursor.execute("DELETE FROM asset_transactions WHERE id = ?", (at_row[0],))

        # 2. Kumbara Aktarımı veya Tamamlanan Kumbara Siliniyorsa: Kumbara Bakiyesini ve Durumunu Senkronize Et
        elif cat_name == 'Tamamlanan Kumbara' or tx_desc.startswith('Hedef Tamamlandı (Kumbaradan Nakite):'):
            goal_title = tx_desc.split(':', 1)[1].strip() if ':' in tx_desc else None
            if goal_title:
                cursor.execute("SELECT id FROM savings_goals WHERE user_id = ? AND title = ?", (user_id, goal_title))
            else:
                cursor.execute("SELECT TOP 1 id FROM savings_goals WHERE user_id = ? AND status = 'tamamlandi' ORDER BY id DESC", (user_id,))
            g_row = cursor.fetchone()
            if g_row:
                cursor.execute("UPDATE savings_goals SET status = 'devam_ediyor' WHERE id = ?", (g_row[0],))

        elif cat_name == 'Tasarruf & Kumbara' or tx_desc.startswith('Kumbaraya Ekleme:'):
            goal_title = tx_desc.split('Kumbaraya Ekleme:', 1)[1].strip() if 'Kumbaraya Ekleme:' in tx_desc else None
            if goal_title:
                cursor.execute("SELECT id, target_amount, current_amount, status, title FROM savings_goals WHERE user_id = ? AND title = ?", (user_id, goal_title))
            else:
                cursor.execute("SELECT TOP 1 id, target_amount, current_amount, status, title FROM savings_goals WHERE user_id = ? ORDER BY id DESC", (user_id,))
            g_row = cursor.fetchone()
            if g_row:
                g_id, g_target, g_curr, g_status, g_title_db = g_row[0], float(g_row[1] or 0.0), float(g_row[2] or 0.0), g_row[3], g_row[4]
                if g_status == 'tamamlandi':
                    flash(f"'{g_title_db}' kumbara hedefi tamamlanıp nakite aktarıldığı için geçmiş ekleme kaydını silmeden önce 'Hedef Tamamlandı' işlemini silmelisiniz.", "warning")
                    return redirect_back_to_dashboard(default_tab='transactions')
                new_curr = max(0.0, round(g_curr - tx_amt, 2))
                cursor.execute("UPDATE savings_goals SET current_amount = ? WHERE id = ?", (new_curr, g_id))

        # 3. Kredi Kartı Ekstre Ödemesi Siliniyorsa: İlgili Dönemin Kredi Kartı Harcamalarını Tekrar Ödenmemiş (is_paid = 0) Yap
        elif cat_name == 'Kredi Kartı Borç Ödemesi' or 'Kredi Kartı Ekstresi Ödemesi' in tx_desc:
            m_match = re.search(r'(\d{1,2})/(\d{4})\s+Kredi Kartı Ekstresi Ödemesi', tx_desc)
            if m_match:
                cc_m, cc_y = int(m_match.group(1)), int(m_match.group(2))
                cursor.execute("""
                    UPDATE transactions 
                    SET is_paid = 0 
                    WHERE user_id = ? AND payment_method = 'kredi_karti' AND is_paid = 1
                      AND MONTH(transaction_date) = ? AND YEAR(transaction_date) = ?
                """, (user_id, cc_m, cc_y))
            else:
                cursor.execute("""
                    UPDATE transactions 
                    SET is_paid = 0 
                    WHERE user_id = ? AND payment_method = 'kredi_karti' AND is_paid = 1
                """, (user_id,))

        if receipt_path:
            try:
                r_path = os.path.join(UPLOAD_FOLDER, receipt_path)
                if os.path.exists(r_path):
                    os.remove(r_path)
            except Exception:
                pass

        cursor.execute("DELETE FROM transactions WHERE id = ? AND user_id = ?", (tx_id, user_id))
        conn.commit()
        flash("İşlem ve ilişkili bakiye kayıtları başarıyla silindi.", "info")
    except Exception as e:
        conn.rollback()
        print(f"[delete_transaction Error]: {e}")
        flash("İşlem silinirken bir hata oluştu.", "danger")
    finally:
        cursor.close()
        conn.close()
    return redirect_back_to_dashboard(default_tab='transactions')

@app.route('/receipts/<path:filename>')
@login_required
def get_receipt(filename):
    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1 FROM transactions WHERE user_id = ? AND receipt_path = ?", (user_id, filename))
        if not cursor.fetchone():
            abort(404)
    finally:
        cursor.close()
        conn.close()

    candidate_dirs = list(dict.fromkeys([
        Config.resolve_upload_folder(app.root_path) if Config.is_vercel() else UPLOAD_FOLDER,
        UPLOAD_FOLDER,
        Config.TEMP_UPLOAD_FOLDER,
    ]))
    for candidate_dir in candidate_dirs:
        if not candidate_dir:
            continue
        try:
            full_path = safe_join(candidate_dir, filename)
            if full_path and os.path.isfile(full_path):
                return send_from_directory(candidate_dir, filename)
        except OSError:
            continue
    abort(404)

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
        pay_all_periods = False

        # Seçili ayda borç yoksa fakat geçmiş dönemlerden devreden ödenmemiş borç varsa onu kapat
        if unpaid_cc <= 0:
            cursor.execute("""
                SELECT ISNULL(SUM(amount), 0) 
                FROM transactions 
                WHERE user_id = ? AND payment_method = 'kredi_karti' AND ISNULL(is_paid, 0) = 0
            """, (user_id,))
            unpaid_cc = float(cursor.fetchone()[0])
            pay_all_periods = True

        if unpaid_cc <= 0:
            flash("Ödenecek kredi kartı ekstre borcu bulunmamaktadır.", "info")
            return redirect_back_to_dashboard(month=month, year=year)

        available_cash = get_user_cash_balance(cursor, user_id)
        if available_cash + 0.005 < unpaid_cc:
            flash(
                f"Yetersiz Vadesiz Nakit Bakiye! Mevcut nakit bakiyeniz ({currency_tr_filter(available_cash)} TL), {currency_tr_filter(unpaid_cc)} TL tutarındaki kredi kartı ekstresini ödemek için yeterli değil.",
                "danger"
            )
            return redirect_back_to_dashboard(month=month, year=year)

        # 1. Kredi kartı harcamalarını ödendi olarak işaretle
        if pay_all_periods:
            cursor.execute("""
                UPDATE transactions 
                SET is_paid = 1 
                WHERE user_id = ? AND payment_method = 'kredi_karti' AND ISNULL(is_paid, 0) = 0
            """, (user_id,))
            desc_text = "Tüm Dönemler Kredi Kartı Ekstresi Ödemesi"
        else:
            cursor.execute("""
                UPDATE transactions 
                SET is_paid = 1 
                WHERE user_id = ? AND payment_method = 'kredi_karti' AND ISNULL(is_paid, 0) = 0
                  AND MONTH(transaction_date) = ? AND YEAR(transaction_date) = ?
            """, (user_id, month, year))
            desc_text = f"{month}/{year} Kredi Kartı Ekstresi Ödemesi"

        # 2. Nakit akışından kredi kartı borç ödemesi olarak çıkış kaydet
        cat_id = get_or_create_category(cursor, user_id, 'Kredi Kartı Borç Ödemesi', 'gider')
        cursor.execute("""
            INSERT INTO transactions (user_id, category_id, amount, transaction_date, description, payment_method, is_paid)
            VALUES (?, ?, ?, CAST(GETDATE() AS DATE), ?, 'nakit', 1)
        """, (user_id, cat_id, unpaid_cc, desc_text))

        conn.commit()
        flash(f"{currency_tr_filter(unpaid_cc)} TL tutarındaki kredi kartı ekstresi Vadesiz Nakit Bakiyenizden başarıyla ödendi.", "success")
    except Exception as e:
        conn.rollback()
        print(f"[pay_credit_card Error]: {e}")
        flash("Ekstre ödenirken bir hata oluştu.", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect_back_to_dashboard(month=month, year=year)

@app.route('/add_subscription', methods=['POST'])
@login_required
def add_subscription():
    user_id = session['user_id']
    title = request.form.get('title', '').strip()
    amount = request.form.get('amount')
    next_due_date = request.form.get('next_due_date') or None
    payment_method = request.form.get('payment_method', 'nakit')
    if 'auto_apply_present' in request.form:
        auto_apply = 1 if request.form.get('auto_apply') == '1' else 0
    else:
        auto_apply = 0 if request.form.get('auto_apply') == '0' else 1

    safe_amount = parse_safe_amount(amount)
    if not title or safe_amount is None:
        flash("Geçerli bir fatura başlığı ve pozitif tutar giriniz.", "warning")
        return redirect_back_to_dashboard(default_tab='goals')

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO subscriptions (user_id, title, amount, billing_cycle, next_due_date, payment_method, auto_apply, is_active)
            VALUES (?, ?, ?, 'aylik', ?, ?, ?, 1)
        """, (user_id, title, safe_amount, next_due_date, payment_method, auto_apply))
        conn.commit()
        flash("Ödeme / Fatura eklendi.", "success")
    except Exception as e:
        conn.rollback()
        print(f"[add_subscription Error]: {e}")
        flash("Fatura eklenirken bir hata oluştu.", "danger")
    finally:
        cursor.close()
        conn.close()
    return redirect_back_to_dashboard(default_tab='goals')

@app.route('/delete_subscription/<int:sub_id>', methods=['POST'])
@login_required
def delete_subscription(sub_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM subscriptions WHERE id = ? AND user_id = ?", (sub_id, session['user_id']))
        conn.commit()
        flash("Fatura silindi.", "info")
    except Exception as e:
        conn.rollback()
        print(f"[delete_subscription Error]: {e}")
        flash("Fatura silinirken bir hata oluştu.", "danger")
    finally:
        cursor.close()
        conn.close()
    return redirect_back_to_dashboard(default_tab='goals')

# --- TASARRUF HEDEFİ İŞLEMLERİ ---
@app.route('/add_savings_goal', methods=['POST'])
@login_required
def add_savings_goal():
    user_id = session['user_id']
    title = request.form.get('title', '').strip()
    target_amount = request.form.get('target_amount')
    target_date = request.form.get('target_date') or None

    safe_target = parse_safe_amount(target_amount)
    if not title or safe_target is None:
        flash("Geçerli bir hedef başlığı ve pozitif hedef tutarı giriniz.", "warning")
        return redirect_back_to_dashboard(default_tab='goals')

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO savings_goals (user_id, title, target_amount, current_amount, target_date, status)
            VALUES (?, ?, ?, 0.00, ?, 'devam_ediyor')
        """, (user_id, title, safe_target, target_date))
        conn.commit()
        flash("Tasarruf hedefi oluşturuldu.", "success")
    except Exception as e:
        conn.rollback()
        print(f"[add_savings_goal Error]: {e}")
        flash("Tasarruf hedefi oluşturulurken bir hata oluştu.", "danger")
    finally:
        cursor.close()
        conn.close()
    return redirect_back_to_dashboard(default_tab='goals')

@app.route('/deposit_savings_goal', methods=['POST'])
@login_required
def deposit_savings_goal():
    user_id = session['user_id']
    goal_id = request.form.get('goal_id')
    safe_amount = parse_safe_amount(request.form.get('amount'))

    if safe_amount is None:
        flash("Geçerli ve pozitif bir birikim tutarı girin.", "warning")
        return redirect_back_to_dashboard(default_tab='goals')

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        available_cash = get_user_cash_balance(cursor, user_id)
        if available_cash + 0.005 < safe_amount:
            flash(
                f"Yetersiz Vadesiz Nakit Bakiye! Mevcut nakit bakiyeniz ({currency_tr_filter(available_cash)} TL) kumbaraya {currency_tr_filter(safe_amount)} TL aktarmak için yeterli değil.",
                "danger"
            )
            return redirect_back_to_dashboard(default_tab='goals')

        cursor.execute("SELECT title FROM savings_goals WHERE id = ? AND user_id = ?", (goal_id, user_id))
        goal = cursor.fetchone()
        if not goal:
            flash("Hedef bulunamadı.", "danger")
            return redirect_back_to_dashboard(default_tab='goals')
        goal_title = goal[0]

        cursor.execute("""
            UPDATE savings_goals 
            SET current_amount = current_amount + ?
            WHERE id = ? AND user_id = ?
        """, (safe_amount, goal_id, user_id))

        # Nakit akışından kumbara transferi olarak harcama kaydı düş
        cat_id = get_or_create_category(cursor, user_id, 'Tasarruf & Kumbara', 'gider')
        cursor.execute("""
            INSERT INTO transactions (user_id, category_id, amount, transaction_date, description, payment_method, is_paid)
            VALUES (?, ?, ?, CAST(GETDATE() AS DATE), ?, 'nakit', 1)
        """, (user_id, cat_id, safe_amount, f"Kumbaraya Ekleme: {goal_title}"))

        conn.commit()
        flash(f"Kumbaraya {currency_tr_filter(safe_amount)} TL eklendi ve Vadesiz Nakit Bakiyenizden düşüldü!", "success")
    except Exception as e:
        conn.rollback()
        print(f"[deposit_savings_goal Error]: {e}")
        flash("Kumbara aktarımı sırasında bir hata oluştu.", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect_back_to_dashboard(default_tab='goals')

@app.route('/delete_savings_goal/<int:goal_id>', methods=['POST'])
@login_required
def delete_savings_goal(goal_id):
    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT title, current_amount, status FROM savings_goals WHERE id = ? AND user_id = ?", (goal_id, user_id))
        goal = cursor.fetchone()
        if goal:
            title, amt, status = goal[0], float(goal[1]), goal[2]
            if status == 'devam_ediyor' and amt > 0:
                cat_id = get_or_create_category(cursor, user_id, 'Kumbara İadesi', 'gelir')
                cursor.execute("""
                    INSERT INTO transactions (user_id, category_id, amount, transaction_date, description, payment_method, is_paid)
                    VALUES (?, ?, ?, CAST(GETDATE() AS DATE), ?, 'nakit', 1)
                """, (user_id, cat_id, amt, f"Silinen Kumbara İadesi: {title}"))
        cursor.execute("DELETE FROM savings_goals WHERE id = ? AND user_id = ?", (goal_id, user_id))
        conn.commit()
        flash("Tasarruf hedefi kaldırıldı ve bakiyesi nakit hesabınıza iade edildi.", "info")
    except Exception as e:
        conn.rollback()
        print(f"[delete_savings_goal Error]: {e}")
        flash("Tasarruf hedefi silinirken bir hata oluştu.", "danger")
    finally:
        cursor.close()
        conn.close()
    return redirect_back_to_dashboard(default_tab='goals')

@app.route('/complete_savings_goal/<int:goal_id>', methods=['POST'])
@login_required
def complete_savings_goal(goal_id):
    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT title, current_amount FROM savings_goals WHERE id = ? AND user_id = ?", (goal_id, user_id))
        goal = cursor.fetchone()
        if goal:
            title, amt = goal[0], float(goal[1])
            cursor.execute("UPDATE savings_goals SET status = 'tamamlandi' WHERE id = ? AND user_id = ?", (goal_id, user_id))
            if amt > 0:
                cat_id = get_or_create_category(cursor, user_id, 'Tamamlanan Kumbara', 'gelir')
                cursor.execute("""
                    INSERT INTO transactions (user_id, category_id, amount, transaction_date, description, payment_method, is_paid)
                    VALUES (?, ?, ?, CAST(GETDATE() AS DATE), ?, 'nakit', 1)
                """, (user_id, cat_id, amt, f"Hedef Tamamlandı (Kumbaradan Nakite): {title}"))
        conn.commit()
        flash("Tebrikler! Tasarruf hedefini başarıyla tamamladınız ve biriken tutar nakit bakiyenize iade edildi.", "success")
    except Exception as e:
        conn.rollback()
        print(f"[complete_savings_goal Error]: {e}")
        flash("Tasarruf hedefi tamamlanırken bir hata oluştu.", "danger")
    finally:
        cursor.close()
        conn.close()
    return redirect_back_to_dashboard(default_tab='goals')

# --- YATIRIM ALIM / SATIM İŞLEMLERİ (WACC MANTIĞI) ---
@app.route('/buy_asset', methods=['POST'])
@login_required
def buy_asset():
    user_id = session['user_id']
    symbol = request.form.get('symbol', '').strip().upper()
    asset_type = request.form.get('asset_type')
    quantity = parse_safe_amount(request.form.get('quantity'), min_val=0.0001)
    unit_price = parse_safe_amount(request.form.get('unit_price'), min_val=0.0001)
    price_currency = request.form.get('price_currency', 'TRY')
    transaction_date = request.form.get('transaction_date') or datetime.now().strftime('%Y-%m-%d')

    if 'use_cash_balance_present' in request.form:
        use_cash = (request.form.get('use_cash_balance') == '1')
    else:
        use_cash = (request.form.get('use_cash_balance', '1') != '0')

    if not symbol or quantity is None or unit_price is None:
        flash("Geçerli bir varlık sembolü, pozitif adet ve fiyat girin.", "danger")
        return redirect_back_to_dashboard(default_tab='portfolio')

    # ABD hissesi için USD girildiyse TL'ye dönüştür
    unit_price_tl = unit_price
    if asset_type == 'abd_hisse' and price_currency == 'USD':
        rate = get_effective_usdtry_rate()
        unit_price_tl = round(unit_price * rate, 2)

    total_cost_tl = round(quantity * unit_price_tl, 2)

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # Mevcut Vadesiz Nakit Bakiyeden ödeme seçiliyse bakiye yeterlilik kontrolü yap
        if use_cash:
            available_cash = get_user_cash_balance(cursor, user_id)
            if available_cash + 0.005 < total_cost_tl:
                flash(
                    f"Yetersiz Vadesiz Nakit Bakiye! Mevcut nakit bakiyeniz ({currency_tr_filter(available_cash)} TL) bu alış ({currency_tr_filter(total_cost_tl)} TL) için yeterli değil.",
                    "danger"
                )
                return redirect_back_to_dashboard(default_tab='portfolio')

        # Varlık mevcut mu?
        cursor.execute("SELECT id, total_quantity, average_cost FROM portfolio_assets WHERE user_id = ? AND asset_symbol = ?", (user_id, symbol))
        existing = cursor.fetchone()

        if existing:
            asset_id, cur_qty, cur_avg = existing[0], float(existing[1]), float(existing[2])
            # Ağırlıklı Ortalama Maliyet Formülü (WACC) - Finance Engine Modüler Servisi
            wac_res = calculate_wac_and_pnl(cur_qty, cur_avg, quantity, unit_price_tl)
            new_qty = wac_res['total_quantity']
            new_avg_cost = wac_res['average_cost']

            cursor.execute("""
                UPDATE portfolio_assets 
                SET total_quantity = ?, average_cost = ?, updated_at = GETDATE()
                WHERE id = ?
            """, (new_qty, new_avg_cost, asset_id))
        else:
            cursor.execute("""
                INSERT INTO portfolio_assets (user_id, asset_symbol, asset_type, total_quantity, average_cost)
                OUTPUT INSERTED.id
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, symbol, asset_type, quantity, unit_price_tl))
            asset_id = int(cursor.fetchone()[0])

        # Alım hareketini kaydet
        cursor.execute("""
            INSERT INTO asset_transactions (asset_id, user_id, transaction_type, quantity, unit_price, total_amount, transaction_date)
            VALUES (?, ?, 'alis', ?, ?, ?, ?)
        """, (asset_id, user_id, quantity, unit_price_tl, total_cost_tl, transaction_date))

        # Nakit akışından alım tutarını harcama olarak düş
        if use_cash:
            cat_id = get_or_create_category(cursor, user_id, 'Yatırım & Portföy', 'gider')
            currency_info = f" (${unit_price:.2f})" if price_currency == 'USD' else ""
            qty_str = f"{quantity:.2f}" if round(quantity, 2) == round(quantity, 4) else f"{quantity:.4f}".rstrip('0').rstrip('.')
            cursor.execute("""
                INSERT INTO transactions (user_id, category_id, amount, transaction_date, description, payment_method, is_paid)
                VALUES (?, ?, ?, ?, ?, 'nakit', 1)
            """, (user_id, cat_id, total_cost_tl, transaction_date, f"{symbol} Varlık Alışı ({qty_str} Adet){currency_info}"))
            flash(f"{symbol} alımı portföye eklendi ve {currency_tr_filter(total_cost_tl)} TL Vadesiz Nakit Bakiyenizden düşüldü.", "success")
        else:
            flash(f"{symbol} varlığı nakit bakiyenizden düşülmeden portföyünüze eklendi.", "success")

        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[buy_asset Error]: {e}")
        flash("Varlık alımı sırasında bir hata oluştu.", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect_back_to_dashboard(default_tab='portfolio')

@app.route('/sell_asset', methods=['POST'])
@login_required
def sell_asset():
    user_id = session['user_id']
    asset_id = request.form.get('asset_id')
    quantity = parse_safe_amount(request.form.get('quantity'), min_val=0.0001)
    unit_price = parse_safe_amount(request.form.get('unit_price'), min_val=0.0001)
    sell_currency = request.form.get('sell_currency', 'TRY')
    transaction_date = request.form.get('transaction_date') or datetime.now().strftime('%Y-%m-%d')

    if quantity is None or unit_price is None:
        flash("Geçerli bir adet ve satış fiyatı girin.", "danger")
        return redirect_back_to_dashboard(default_tab='portfolio')

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
            return redirect_back_to_dashboard(default_tab='portfolio')

        symbol = asset[1]
        asset_type = asset[2]
        cur_qty = float(asset[3])
        avg_cost = float(asset[4])

        if quantity > cur_qty:
            flash(f"Yetersiz bakiye! Portföyünüzde {cur_qty:g} adet {symbol} bulunmaktadır.", "warning")
            return redirect_back_to_dashboard(default_tab='portfolio')

        unit_price_tl = unit_price
        if asset_type == 'abd_hisse' and sell_currency == 'USD':
            rate = get_effective_usdtry_rate()
            unit_price_tl = round(unit_price * rate, 2)

        new_qty = cur_qty - quantity
        total_amount_tl = round(quantity * unit_price_tl, 2)
        # Realize Edilen Kâr/Zarar - Finance Engine Modüler Servisi
        realized_pnl = calculate_realized_pnl(avg_cost, quantity, unit_price_tl)

        # 1. Satış işlem hareketini kaydet (Foreign key ilişkisinin korunması için asset_id geçerli kalmalı)
        cursor.execute("""
            INSERT INTO asset_transactions (asset_id, user_id, transaction_type, quantity, unit_price, total_amount, realized_pnl, transaction_date)
            VALUES (?, ?, 'satis', ?, ?, ?, ?, ?)
        """, (asset_id, user_id, quantity, unit_price_tl, total_amount_tl, realized_pnl, transaction_date))

        # 2. Portföy miktarını güncelle (tümü satıldıysa 0.0 yap, işlem geçmişi ve FK için satır silinmez)
        final_qty = 0.0 if new_qty <= 0.0001 else round(new_qty, 4)
        cursor.execute("""
            UPDATE portfolio_assets 
            SET total_quantity = ?, updated_at = GETDATE()
            WHERE id = ?
        """, (final_qty, asset_id))

        pnl_text = f"+{realized_pnl:.2f} TL Kâr" if realized_pnl >= 0 else f"{realized_pnl:.2f} TL Zarar"

        # Satıştan gelen nakit tutarını doğrudan Vadesiz Nakit Bakiyeye (Gelir / Nakit) kaydet
        cat_id = get_or_create_category(cursor, user_id, 'Yatırım & Portföy', 'gelir')
        currency_info = f" (${unit_price:.2f})" if sell_currency == 'USD' else ""
        qty_str = f"{quantity:.2f}" if round(quantity, 2) == round(quantity, 4) else f"{quantity:.4f}".rstrip('0').rstrip('.')
        cursor.execute("""
            INSERT INTO transactions (user_id, category_id, amount, transaction_date, description, payment_method, is_paid)
            VALUES (?, ?, ?, ?, ?, 'nakit', 1)
        """, (user_id, cat_id, total_amount_tl, transaction_date, f"{symbol} Varlık Satışı ({qty_str} Adet){currency_info} ({pnl_text})"))

        conn.commit()
        flash(f"{symbol} satışı yapıldı ({qty_str} adet, {pnl_text}) ve {currency_tr_filter(total_amount_tl)} TL Vadesiz Nakit Bakiyenize eklendi.", "success")
    except Exception as e:
        conn.rollback()
        print(f"[sell_asset Error]: {e}")
        flash("Satış işlemi sırasında bir hata oluştu.", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect_back_to_dashboard(default_tab='portfolio')

# --- KATEGORİ VE BÜTÇE YÖNETİMİ ---
@app.route('/add_category', methods=['POST'])
@login_required
def add_category():
    user_id = session['user_id']
    name = request.form.get('name', '').strip()
    cat_type = request.form.get('type', 'gider')
    safe_budget = parse_safe_amount(request.form.get('monthly_budget'), min_val=0.0)
    monthly_budget = safe_budget if safe_budget is not None else 0.0

    if not name:
        flash("Kategori adı boş bırakılamaz.", "warning")
        return redirect_back_to_dashboard(default_tab='transactions')

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM categories WHERE user_id = ? AND name = ? AND type = ?", (user_id, name, cat_type))
        if cursor.fetchone():
            flash("Bu kategori zaten mevcut.", "warning")
            return redirect_back_to_dashboard(default_tab='transactions')

        cursor.execute("""
            INSERT INTO categories (user_id, name, type, monthly_budget)
            VALUES (?, ?, ?, ?)
        """, (user_id, name, cat_type, monthly_budget))
        conn.commit()
        flash(f"'{name}' kategorisi başarıyla eklendi.", "success")
    except Exception as e:
        conn.rollback()
        print(f"[add_category Error]: {e}")
        flash("Kategori eklenirken bir hata oluştu.", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect_back_to_dashboard(default_tab='transactions')

@app.route('/update_category_budget', methods=['POST'])
@login_required
def update_category_budget():
    user_id = session['user_id']
    category_id = request.form.get('category_id')
    safe_budget = parse_safe_amount(request.form.get('monthly_budget'), min_val=0.0)
    monthly_budget = safe_budget if safe_budget is not None else 0.0

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
        print(f"[update_category_budget Error]: {e}")
        flash("Kategori bütçesi güncellenirken bir hata oluştu.", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect_back_to_dashboard(default_tab='transactions')

@app.route('/delete_category/<int:cat_id>', methods=['POST'])
@login_required
def delete_category(cat_id):
    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM transactions WHERE category_id = ? AND user_id = ?", (cat_id, user_id))
        tx_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM recurring_transactions WHERE category_id = ? AND user_id = ?", (cat_id, user_id))
        rec_count = cursor.fetchone()[0]
        if tx_count > 0 or rec_count > 0:
            flash("Bu kategoriye bağlı harcama/gelir veya düzenli işlem şablonları bulunduğu için kategori silinemez.", "warning")
        else:
            cursor.execute("DELETE FROM categories WHERE id = ? AND user_id = ?", (cat_id, user_id))
            conn.commit()
            flash("Kategori başarıyla silindi.", "info")
    except Exception as e:
        conn.rollback()
        print(f"[delete_category Error]: {e}")
        flash("Kategori silinirken bir hata oluştu.", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect_back_to_dashboard(default_tab='transactions')

# --- YEDEKLEME & GERİ YÜKLEME (BACKUP & RESTORE) ---
@app.route('/backup/download')
@login_required
def backup_download():
    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT username, email, ISNULL(initial_cash_balance, 0), ISNULL(credit_card_limit, 50000) 
            FROM users WHERE id = ?
        """, (user_id,))
        u_row = cursor.fetchone()
        username = u_row[0] if u_row else 'user'
        initial_cash_balance = float(u_row[2]) if u_row else 0.0
        credit_card_limit = float(u_row[3]) if u_row else 50000.0

        cursor.execute("SELECT name, type, monthly_budget FROM categories WHERE user_id = ?", (user_id,))
        cats = [{'name': r[0], 'type': r[1], 'monthly_budget': float(r[2])} for r in cursor.fetchall()]

        cursor.execute("""
            SELECT c.name, t.amount, t.transaction_date, t.description, t.payment_method, t.is_paid, t.receipt_path 
            FROM transactions t 
            JOIN categories c ON t.category_id = c.id 
            WHERE t.user_id = ?
        """, (user_id,))
        txs = [{
            'category_name': r[0],
            'amount': float(r[1]),
            'transaction_date': r[2].strftime('%Y-%m-%d'),
            'description': r[3] or '',
            'payment_method': r[4],
            'is_paid': int(r[5]),
            'receipt_path': r[6]
        } for r in cursor.fetchall()]

        cursor.execute("SELECT asset_symbol, asset_type, total_quantity, average_cost FROM portfolio_assets WHERE user_id = ?", (user_id,))
        assets = [{
            'asset_symbol': r[0],
            'asset_type': r[1],
            'total_quantity': float(r[2]),
            'average_cost': float(r[3])
        } for r in cursor.fetchall()]

        cursor.execute("""
            SELECT pa.asset_symbol, at.transaction_type, at.quantity, at.unit_price, 
                   at.total_amount, ISNULL(at.realized_pnl, 0), at.transaction_date
            FROM asset_transactions at
            JOIN portfolio_assets pa ON at.asset_id = pa.id
            WHERE at.user_id = ?
            ORDER BY at.id ASC
        """, (user_id,))
        asset_txs = [{
            'asset_symbol': r[0],
            'transaction_type': r[1],
            'quantity': float(r[2]),
            'unit_price': float(r[3]),
            'total_amount': float(r[4]),
            'realized_pnl': float(r[5]),
            'transaction_date': r[6].strftime('%Y-%m-%d') if hasattr(r[6], 'strftime') else str(r[6])[:10]
        } for r in cursor.fetchall()]

        cursor.execute("""
            SELECT title, amount, billing_cycle, next_due_date, is_active, 
                   ISNULL(payment_method, 'kredi_karti'), ISNULL(auto_apply, 1), last_synced_period 
            FROM subscriptions WHERE user_id = ?
        """, (user_id,))
        subs = [{
            'title': r[0],
            'amount': float(r[1]),
            'billing_cycle': r[2],
            'next_due_date': r[3].strftime('%Y-%m-%d'),
            'is_active': int(r[4]),
            'payment_method': r[5],
            'auto_apply': int(r[6]),
            'last_synced_period': r[7]
        } for r in cursor.fetchall()]

        cursor.execute("SELECT title, target_amount, current_amount, target_date, status FROM savings_goals WHERE user_id = ?", (user_id,))
        goals = [{
            'title': r[0],
            'target_amount': float(r[1]),
            'current_amount': float(r[2]),
            'target_date': r[3].strftime('%Y-%m-%d') if r[3] else None,
            'status': r[4]
        } for r in cursor.fetchall()]

        cursor.execute("""
            SELECT c.name, r.title, r.amount, r.type, r.payment_method, r.day_of_month, r.is_active, r.last_synced_period 
            FROM recurring_transactions r
            JOIN categories c ON r.category_id = c.id
            WHERE r.user_id = ?
        """, (user_id,))
        recurrings = [{
            'category_name': r[0],
            'title': r[1],
            'amount': float(r[2]),
            'type': r[3],
            'payment_method': r[4],
            'day_of_month': int(r[5]),
            'is_active': int(r[6]),
            'last_synced_period': r[7]
        } for r in cursor.fetchall()]
    finally:
        cursor.close()
        conn.close()

    backup_data = {
        'version': '1.2',
        'export_time': datetime.now().isoformat(),
        'username': username,
        'initial_cash_balance': initial_cash_balance,
        'credit_card_limit': credit_card_limit,
        'categories': cats,
        'transactions': txs,
        'portfolio_assets': assets,
        'asset_transactions': asset_txs,
        'subscriptions': subs,
        'savings_goals': goals,
        'recurring_transactions': recurrings
    }

    buf = io.BytesIO()
    buf.write(json.dumps(backup_data, ensure_ascii=False, indent=2).encode('utf-8'))
    buf.seek(0)
    filename = f"moneta_backup_{datetime.today().strftime('%Y%m%d_%H%M%S')}.json"
    return send_file(buf, as_attachment=True, download_name=filename, mimetype='application/json')

@app.route('/backup/restore', methods=['POST'])
@login_required
def backup_restore():
    user_id = session['user_id']
    backup_file = request.files.get('backup_file')
    if not backup_file or not backup_file.filename:
        flash("Lütfen geçerli bir JSON yedek dosyası seçin.", "danger")
        return redirect_back_to_dashboard()

    try:
        content = json.loads(backup_file.read().decode('utf-8'))
        if not isinstance(content, dict):
            raise ValueError("Kök JSON nesnesi dict değil")
    except Exception as e:
        print(f"[backup_restore JSON Error]: {e}")
        flash("Yedek dosyası okunamadı. Lütfen geçerli bir Moneta JSON yedek dosyası seçin.", "danger")
        return redirect_back_to_dashboard()

    restore_mode = request.form.get('restore_mode', 'merge')

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if restore_mode == 'replace':
            cursor.execute("DELETE FROM asset_transactions WHERE user_id = ?", (user_id,))
            cursor.execute("DELETE FROM portfolio_assets WHERE user_id = ?", (user_id,))
            cursor.execute("DELETE FROM transactions WHERE user_id = ?", (user_id,))
            cursor.execute("DELETE FROM recurring_transactions WHERE user_id = ?", (user_id,))
            cursor.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
            cursor.execute("DELETE FROM savings_goals WHERE user_id = ?", (user_id,))

        if 'initial_cash_balance' in content and content['initial_cash_balance'] is not None:
            try:
                init_cash_val = round(float(content['initial_cash_balance']), 2)
                if -999999999999.99 <= init_cash_val <= 999999999999.99:
                    cursor.execute("UPDATE users SET initial_cash_balance = ? WHERE id = ?", (init_cash_val, user_id))
            except (ValueError, TypeError):
                pass

        if 'credit_card_limit' in content and content['credit_card_limit'] is not None:
            safe_lim = parse_safe_amount(content['credit_card_limit'], min_val=0.01)
            if safe_lim is not None:
                cursor.execute("UPDATE users SET credit_card_limit = ? WHERE id = ?", (safe_lim, user_id))

        cat_map = {}
        for c in content.get('categories', []):
            c_name = str(c.get('name', '')).strip()
            c_type = c.get('type', 'gider')
            if not c_name or c_type not in ('gelir', 'gider'):
                continue
            budget_val = parse_safe_amount(c.get('monthly_budget', 0), min_val=0.0) or 0.0
            cat_id = get_or_create_category(cursor, user_id, c_name, c_type, budget_val)
            cat_map[c_name] = cat_id

        for t in content.get('transactions', []):
            t_amt = parse_safe_amount(t.get('amount'), min_val=0.01)
            t_cat = str(t.get('category_name', '')).strip()
            t_date = str(t.get('transaction_date', '')).strip()[:10]
            if t_amt is None or not t_cat or not t_date:
                continue
            cat_id = cat_map.get(t_cat)
            if not cat_id:
                cat_id = get_or_create_category(cursor, user_id, t_cat, 'gider', 0)
                cat_map[t_cat] = cat_id
            t_desc = str(t.get('description', '') or '').strip()
            t_method = t.get('payment_method', 'nakit')
            if t_method not in ('nakit', 'kredi_karti'):
                t_method = 'nakit'
            t_paid = 1 if int(t.get('is_paid', 1)) else 0
            raw_rp = t.get('receipt_path')
            safe_rp = secure_filename(os.path.basename(str(raw_rp))) if raw_rp else None
            if safe_rp and not os.path.exists(os.path.join(UPLOAD_FOLDER, safe_rp)):
                safe_rp = None

            if restore_mode != 'replace':
                cursor.execute("""
                    SELECT 1 FROM transactions
                    WHERE user_id = ? AND category_id = ? AND amount = ? AND transaction_date = ?
                      AND ISNULL(description, '') = ? AND ISNULL(payment_method, 'nakit') = ?
                """, (user_id, cat_id, t_amt, t_date, t_desc, t_method))
                if cursor.fetchone():
                    continue

            cursor.execute("""
                INSERT INTO transactions (user_id, category_id, amount, transaction_date, description, payment_method, is_paid, receipt_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (user_id, cat_id, t_amt, t_date, t_desc, t_method, t_paid, safe_rp))

        asset_map = {}
        for a in content.get('portfolio_assets', []):
            a_sym = str(a.get('asset_symbol', '')).strip().upper()
            a_type = str(a.get('asset_type', 'hisse')).strip()
            a_qty = parse_safe_amount(a.get('total_quantity', 0), min_val=0.0)
            a_cost = parse_safe_amount(a.get('average_cost', 0), min_val=0.0)
            if not a_sym or a_qty is None or a_cost is None:
                continue
            cursor.execute("SELECT id FROM portfolio_assets WHERE user_id = ? AND asset_symbol = ?", (user_id, a_sym))
            existing = cursor.fetchone()
            if existing:
                cursor.execute("""
                    UPDATE portfolio_assets 
                    SET total_quantity = ?, average_cost = ?, updated_at = GETDATE()
                    WHERE id = ?
                """, (a_qty, a_cost, existing[0]))
                asset_map[a_sym] = existing[0]
            else:
                cursor.execute("""
                    INSERT INTO portfolio_assets (user_id, asset_symbol, asset_type, total_quantity, average_cost)
                    OUTPUT INSERTED.id
                    VALUES (?, ?, ?, ?, ?)
                """, (user_id, a_sym, a_type, a_qty, a_cost))
                new_aid = cursor.fetchone()
                if new_aid and new_aid[0]:
                    asset_map[a_sym] = int(new_aid[0])

        for at in content.get('asset_transactions', []):
            sym = str(at.get('asset_symbol', '') or '').strip().upper()
            a_id = asset_map.get(sym)
            if not a_id and sym:
                cursor.execute("SELECT id FROM portfolio_assets WHERE user_id = ? AND asset_symbol = ?", (user_id, sym))
                arow = cursor.fetchone()
                if arow:
                    a_id = arow[0]
                    asset_map[sym] = a_id
            if not a_id:
                continue
            at_type = at.get('transaction_type', 'alis')
            at_qty = parse_safe_amount(at.get('quantity', 0), min_val=0.0) or 0.0
            at_price = parse_safe_amount(at.get('unit_price', 0), min_val=0.0) or 0.0
            at_total = parse_safe_amount(at.get('total_amount', 0), min_val=0.0) or 0.0
            try:
                at_pnl = round(float(at.get('realized_pnl', 0) or 0.0), 2)
            except (ValueError, TypeError):
                at_pnl = 0.0
            at_date = str(at.get('transaction_date') or datetime.now().strftime('%Y-%m-%d'))[:10]

            if restore_mode != 'replace':
                cursor.execute("""
                    SELECT 1 FROM asset_transactions
                    WHERE asset_id = ? AND user_id = ? AND transaction_type = ? AND quantity = ? AND unit_price = ? AND transaction_date = ?
                """, (a_id, user_id, at_type, at_qty, at_price, at_date))
                if cursor.fetchone():
                    continue

            cursor.execute("""
                INSERT INTO asset_transactions (asset_id, user_id, transaction_type, quantity, unit_price, total_amount, realized_pnl, transaction_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (a_id, user_id, at_type, at_qty, at_price, at_total, at_pnl, at_date))

        for s in content.get('subscriptions', []):
            s_title = str(s.get('title', '')).strip()
            s_amt = parse_safe_amount(s.get('amount'), min_val=0.01)
            if not s_title or s_amt is None:
                continue
            if restore_mode != 'replace':
                cursor.execute("SELECT 1 FROM subscriptions WHERE user_id = ? AND title = ? AND amount = ?", (user_id, s_title, s_amt))
                if cursor.fetchone():
                    continue
            cursor.execute("""
                INSERT INTO subscriptions (user_id, title, amount, billing_cycle, next_due_date, is_active, payment_method, auto_apply, last_synced_period)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id,
                s_title,
                s_amt,
                s.get('billing_cycle', 'aylik'),
                s.get('next_due_date'),
                int(s.get('is_active', 1)),
                s.get('payment_method', 'kredi_karti'),
                int(s.get('auto_apply', 1)),
                s.get('last_synced_period')
            ))

        for g in content.get('savings_goals', []):
            g_title = str(g.get('title', '')).strip()
            g_target = parse_safe_amount(g.get('target_amount'), min_val=0.01)
            g_curr = parse_safe_amount(g.get('current_amount', 0), min_val=0.0) or 0.0
            if not g_title or g_target is None:
                continue
            if restore_mode != 'replace':
                cursor.execute("SELECT 1 FROM savings_goals WHERE user_id = ? AND title = ?", (user_id, g_title))
                if cursor.fetchone():
                    continue
            cursor.execute("""
                INSERT INTO savings_goals (user_id, title, target_amount, current_amount, target_date, status)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, g_title, g_target, g_curr, g.get('target_date'), g.get('status', 'devam_ediyor')))

        for r in content.get('recurring_transactions', []):
            r_title = str(r.get('title', '')).strip()
            r_amt = parse_safe_amount(r.get('amount'), min_val=0.01)
            r_cat = str(r.get('category_name', '')).strip()
            r_type = r.get('type', 'gider')
            if not r_title or r_amt is None or not r_cat:
                continue
            cat_id = cat_map.get(r_cat)
            if not cat_id:
                cat_id = get_or_create_category(cursor, user_id, r_cat, r_type, 0)
                cat_map[r_cat] = cat_id
            if restore_mode != 'replace':
                cursor.execute("SELECT 1 FROM recurring_transactions WHERE user_id = ? AND title = ? AND amount = ? AND type = ?", (user_id, r_title, r_amt, r_type))
                if cursor.fetchone():
                    continue
            cursor.execute("""
                INSERT INTO recurring_transactions (user_id, category_id, title, amount, type, payment_method, day_of_month, is_active, last_synced_period)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id,
                cat_id,
                r_title,
                r_amt,
                r_type,
                r.get('payment_method', 'nakit'),
                int(r.get('day_of_month', 1)),
                int(r.get('is_active', 1)),
                r.get('last_synced_period')
            ))

        conn.commit()
        flash("Yedek başarıyla geri yüklendi ve tüm verileriniz güncellendi!", "success")
    except Exception as e:
        conn.rollback()
        print(f"[backup_restore Error]: {e}")
        flash("Yedek geri yüklenirken bir hata oluştu. Lütfen dosya içeriğini kontrol edin.", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect_back_to_dashboard()


if __name__ == '__main__':
    debug_mode = os.getenv('FLASK_DEBUG', '1') == '1' and not Config.is_production()
    app.run(debug=debug_mode, port=int(os.getenv('PORT', 5000)))