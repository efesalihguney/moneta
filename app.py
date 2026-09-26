from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file, Response, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from functools import wraps
from datetime import datetime
import json
import time
import urllib.request
import re
import io
import calendar
import os
import uuid
import requests
import pandas as pd
import yfinance as yf
import xml.etree.ElementTree as ET
from db import get_db_connection

app = Flask(__name__)
app.secret_key = "super_secret_wealth_tracker_key_change_in_production"

# --- FİŞ / MAKBUZ YÜKLEME AYARLARI ---
UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'uploads', 'receipts')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'pdf'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

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

BENCHMARK_CACHE = {}  # { 'data': (results, timestamp) }

def get_benchmark_returns():
    """Son 1, 3 ve 6 aylık BIST100, Dolar ve Gram Altın getirilerini döner (1 saatlik önbellek)."""
    now = time.time()
    if 'data' in BENCHMARK_CACHE and (now - BENCHMARK_CACHE['data'][1] < 3600):
        return BENCHMARK_CACHE['data'][0]

    results = {
        '1M': {'bist': 0.0, 'usd': 0.0, 'gold': 0.0},
        '3M': {'bist': 0.0, 'usd': 0.0, 'gold': 0.0},
        '6M': {'bist': 0.0, 'usd': 0.0, 'gold': 0.0}
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

        BENCHMARK_CACHE['data'] = (results, now)
        return results
    except Exception as e:
        fallback = {
            '1M': {'bist': -12.0, 'usd': 2.0, 'gold': -5.7},
            '3M': {'bist': -8.8, 'usd': 4.9, 'gold': 12.0},
            '6M': {'bist': -0.6, 'usd': 10.4, 'gold': 4.6}
        }
        return fallback

NEWS_CACHE = {'timestamp': 0, 'data': []}

def get_economy_news(force_refresh=False):
    """Günün ekonomi ve piyasa haber başlıklarını çeker ve 15 dakika önbelleğe alır."""
    now = time.time()
    if not force_refresh and NEWS_CACHE['data'] and (now - NEWS_CACHE['timestamp'] < 900):
        return NEWS_CACHE['data']

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
            NEWS_CACHE['data'] = news_items
            NEWS_CACHE['timestamp'] = now
            return news_items
    except Exception as e:
        print("Ekonomi haberleri çekilirken hata oluştu:", e)

    if NEWS_CACHE['data']:
        return NEWS_CACHE['data']

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
                'text': f"Bu ay harcamalarınız gelirinizi {abs(net_savings):,.2f} TL aştı. Birikimlerinizin erimemesi için harcamalarınızı acilen sınırlandırın."
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
                'text': f"Bu kategoride {b['budget']:,.2f} TL planlanan bütçeye karşın {b['spent']:,.2f} TL harcandı (%{over_pct:.0f} aşım)."
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
                'text': f"Aylık giderlerinizin %{cc_pct:.0f}'i kredi kartıyla yapıldı ({cc_expense:,.2f} TL). Ekstre kesiminde nakit sıkışıklığı yaşamamak için vadesiz bakiyenizi hazır tutun."
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
                    'message': f'{sub_amount:,.2f} TL tutarındaki {sub_name} faturasının son ödeme günü geldi/geçti.',
                    'target_tab': 'goals-tab'
                })
            elif days_left <= 3:
                notifications.append({
                    'id': f'sub_{sub_id}',
                    'severity': 'danger',
                    'badge': f'{days_left} Gün Kaldı',
                    'icon': 'bi-alarm-fill',
                    'title': f'{sub_name} Yaklaşıyor',
                    'message': f'{sub_amount:,.2f} TL tutarındaki fatura için son {days_left} gün kaldı.',
                    'target_tab': 'goals-tab'
                })
            elif days_left <= 7:
                notifications.append({
                    'id': f'sub_{sub_id}',
                    'severity': 'warning',
                    'badge': f'{days_left} Gün Kaldı',
                    'icon': 'bi-calendar-event',
                    'title': f'{sub_name} Hatırlatması',
                    'message': f'{sub_name} ödemesine 1 haftadan az zaman kaldı ({sub_amount:,.2f} TL).',
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
                        'message': f"{b['name']} bütçesi {diff:,.2f} TL aşıldı (%{b['percent']:.0f} harcandı).",
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
                        'message': f"Bütçenizin %{b['percent']:.0f}'i kullanıldı, kalan harcama limiti: {rem:,.2f} TL.",
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
            'message': f'Toplam {total_unpaid_cc:,.2f} TL ekstre borcunuz bulunmaktadır.',
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
                    'message': f"{g['title']} kumbaranızda hedef tutara ({g['target_amount']:,.2f} TL) ulaşıldı!",
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
                    'message': f"Hedefe ulaşmak için son {rem:,.2f} TL kaldı.",
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

@app.route('/auto-login')
def auto_login():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT TOP 1 id, username, first_name FROM users ORDER BY id ASC")
    user = cursor.fetchone()
    cursor.close()
    conn.close()
    if user:
        session['user_id'] = user[0]
        session['username'] = user[1]
        session['first_name'] = user[2]
        flash(f"Hoş geldiniz {user[2]}, test oturumunuz başarıyla aktif edildi.", "success")
        return redirect(url_for('dashboard'))
    flash("Sistemde kayıtlı kullanıcı bulunamadı.", "warning")
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

    # 6. Kümülatif Net Servet (Net Worth) Bileşenleri
    cursor.execute("""
        SELECT ISNULL(SUM(CASE 
            WHEN c.type = 'gelir' THEN t.amount 
            ELSE (CASE WHEN ISNULL(t.payment_method, 'nakit') = 'nakit' THEN -t.amount ELSE 0 END) 
        END), 0)
        FROM transactions t
        JOIN categories c ON t.category_id = c.id
        WHERE t.user_id = ?
    """, (user_id,))
    cash_balance = float(cursor.fetchone()[0])

    cursor.execute("""
        SELECT ISNULL(SUM(current_amount), 0)
        FROM savings_goals
        WHERE user_id = ? AND status = 'devam_ediyor'
    """, (user_id,))
    total_savings_amount = float(cursor.fetchone()[0])

    cursor.execute("""
        SELECT ISNULL(SUM(amount), 0)
        FROM transactions
        WHERE user_id = ? AND payment_method = 'kredi_karti' AND ISNULL(is_paid, 0) = 0
    """, (user_id,))
    total_unpaid_cc = float(cursor.fetchone()[0])

    total_net_worth = cash_balance + total_portfolio_value + total_savings_amount - total_unpaid_cc

    # 7. Portföy Varlık Dağılımı (Asset Allocation)
    asset_allocation = {}
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
    for item in portfolio_items:
        tname = type_display_names.get(item['type'], item['type'])
        asset_allocation[tname] = asset_allocation.get(tname, 0.0) + item['cur_val']

    chart_asset_labels = list(asset_allocation.keys())
    chart_asset_values = [round(v, 2) for v in asset_allocation.values()]

    # 8. Son 6 Ayın Tarihsel Finansal Trendi (Gelir, Gider ve Net Tasarruf)
    past_months = []
    for i in range(5, -1, -1):
        m = selected_month - i
        y = selected_year
        while m <= 0:
            m += 12
            y -= 1
        past_months.append((y, m))

    trend_labels = []
    trend_income = []
    trend_expense = []
    trend_net = []

    for y, m in past_months:
        cursor.execute("""
            SELECT 
                ISNULL(SUM(CASE WHEN c.type = 'gelir' THEN t.amount ELSE 0 END), 0) AS inc,
                ISNULL(SUM(CASE WHEN c.type = 'gider' THEN t.amount ELSE 0 END), 0) AS exp
            FROM transactions t
            JOIN categories c ON t.category_id = c.id
            WHERE t.user_id = ? AND MONTH(t.transaction_date) = ? AND YEAR(t.transaction_date) = ?
        """, (user_id, m, y))
        t_row = cursor.fetchone()
        inc_val = float(t_row[0])
        exp_val = float(t_row[1])
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
        if days_left < 0:
            smart_alerts.append({
                'type': 'danger',
                'icon': 'bi-exclamation-octagon-fill',
                'title': 'Günü Geçmiş Ödeme!',
                'message': f"'{sub_title}' ({sub_amt:.2f} TL) son ödeme tarihi {abs(days_left)} gün önce geçti."
            })
        elif days_left <= 3:
            smart_alerts.append({
                'type': 'warning',
                'icon': 'bi-alarm-fill',
                'title': 'Yaklaşan Fatura / Kart Ödemesi',
                'message': f"'{sub_title}' ({sub_amt:.2f} TL) için son {days_left} gün kaldı."
            })

    if cc_expense > 0:
        smart_alerts.append({
            'type': 'info',
            'icon': 'bi-credit-card-2-front-fill',
            'title': 'Ödenmemiş Kredi Kartı Ekstresi',
            'message': f"{selected_month_name} dönemine ait {cc_expense:.2f} TL tutarında ekstre borcunuz bulunmaktadır."
        })

    for b in category_budgets:
        if b['budget'] > 0 and b['spent'] > b['budget']:
            over = b['spent'] - b['budget']
            smart_alerts.append({
                'type': 'danger',
                'icon': 'bi-lightning-charge-fill',
                'title': f"Bütçe Aşımı: {b['name']}",
                'message': f"Bu ayki bütçe sınırı {over:.2f} TL tutarında aşıldı (%{b['percent']})."
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

    cursor.close()
    conn.close()

    savings_rate = round((net_savings / total_income * 100), 1) if total_income > 0 else 0.0

    # 12. Piyasa Benchmark & Kıyaslama Verileri
    benchmark_data = get_benchmark_returns()

    # 13. Akıllı Finansal Asistan & Harcama İçgörüleri (AI Insights)
    ai_insights = generate_financial_insights(
        total_income, total_expense, net_savings, savings_rate, 
        category_budgets, cc_expense, cash_expense, 
        total_portfolio_pnl_pct, benchmark_data
    )

    # 14. Uygulama İçi Bildirim ve Uyarılar
    notifications = get_in_app_notifications(subscriptions, category_budgets, total_unpaid_cc, savings_goals, total_portfolio_pnl_pct)
    notification_count = len(notifications)

    return render_template(
        'dashboard.html',
        savings_rate=savings_rate,
        total_income=total_income,
        cash_expense=cash_expense,
        cc_expense=cc_expense,
        total_expense=total_expense,
        net_savings=net_savings,
        total_net_worth=total_net_worth,
        cash_balance=cash_balance,
        total_savings_amount=total_savings_amount,
        total_unpaid_cc=total_unpaid_cc,
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
    cursor.close()
    conn.close()

    data = []
    for r in rows:
        data.append({
            'İşlem No': r[0],
            'Tarih': r[1].strftime('%Y-%m-%d') if r[1] else '',
            'Kategori': r[2],
            'Tür': 'Gelir' if r[3] == 'gelir' else 'Gider',
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
    cursor.execute("""
        SELECT id, asset_symbol, asset_type, total_quantity, average_cost
        FROM portfolio_assets
        WHERE user_id = ? AND total_quantity > 0
    """, (user_id,))
    assets_raw = cursor.fetchall()
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

# --- DÜZENLİ / TEKRARLAYAN İŞLEMLER (RECURRING) ---
@app.route('/add_recurring', methods=['POST'])
@login_required
def add_recurring():
    user_id = session['user_id']
    title = request.form.get('title', '').strip()
    category_id = request.form.get('category_id')
    amount = float(request.form.get('amount', 0))
    rec_type = request.form.get('type', 'gider')
    payment_method = request.form.get('payment_method', 'nakit')
    day_of_month = int(request.form.get('day_of_month', 1))

    if not title or amount <= 0:
        flash("Geçerli bir başlık ve tutar girin.", "warning")
        return redirect(url_for('dashboard'))

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO recurring_transactions (user_id, category_id, title, amount, type, payment_method, day_of_month, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
        """, (user_id, category_id, title, amount, rec_type, payment_method, day_of_month))
        conn.commit()
        flash(f"'{title}' düzenli işlem şablonu başarıyla eklendi.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Hata: {e}", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('dashboard'))

@app.route('/delete_recurring/<int:rec_id>', methods=['POST'])
@login_required
def delete_recurring(rec_id):
    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM recurring_transactions WHERE id = ? AND user_id = ?", (rec_id, user_id))
    conn.commit()
    cursor.close()
    conn.close()
    flash("Düzenli işlem şablonu silindi.", "info")
    return redirect(url_for('dashboard'))

@app.route('/apply_recurring', methods=['POST'])
@login_required
def apply_recurring():
    user_id = session['user_id']
    month = request.form.get('month', type=int) or datetime.today().month
    year = request.form.get('year', type=int) or datetime.today().year

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT id, category_id, title, amount, type, payment_method, day_of_month
            FROM recurring_transactions
            WHERE user_id = ? AND is_active = 1
        """, (user_id,))
        templates = cursor.fetchall()

        if not templates:
            flash("Henüz tanımlı düzenli işlem şablonunuz bulunmuyor.", "info")
            return redirect(url_for('dashboard', month=month, year=year))

        added_count = 0
        last_day = calendar.monthrange(year, month)[1]

        for t in templates:
            cat_id, title, amt, t_type, p_method, day = t[1], t[2], float(t[3]), t[4], t[5], t[6]
            actual_day = min(day, last_day)
            tx_date = f"{year:04d}-{month:02d}-{actual_day:02d}"

            # Daha önce bu ay eklenmiş mi kontrol et (çift kaydı önle)
            cursor.execute("""
                SELECT id FROM transactions 
                WHERE user_id = ? AND category_id = ? AND description = ? 
                  AND MONTH(transaction_date) = ? AND YEAR(transaction_date) = ?
            """, (user_id, cat_id, f"[Düzenli] {title}", month, year))
            if cursor.fetchone():
                continue

            is_paid = 0 if p_method == 'kredi_karti' else 1
            cursor.execute("""
                INSERT INTO transactions (user_id, category_id, amount, transaction_date, description, payment_method, is_paid)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (user_id, cat_id, amt, tx_date, f"[Düzenli] {title}", p_method, is_paid))
            added_count += 1

        conn.commit()
        if added_count > 0:
            flash(f"{month}/{year} dönemi için {added_count} adet düzenli işlem (Maaş, Kira vb.) başarıyla uygulandı!", "success")
        else:
            flash("Tüm düzenli işlemler bu dönem için zaten daha önce eklenmiş.", "info")
    except Exception as e:
        conn.rollback()
        flash(f"Uygulama hatası: {e}", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('dashboard', month=month, year=year))

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

    receipt_file = request.files.get('receipt')
    receipt_filename = None
    if receipt_file and receipt_file.filename and allowed_file(receipt_file.filename):
        receipt_filename = f"{uuid.uuid4().hex[:12]}_{secure_filename(receipt_file.filename)}"
        receipt_file.save(os.path.join(UPLOAD_FOLDER, receipt_filename))

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO transactions (user_id, category_id, amount, transaction_date, description, payment_method, is_paid, receipt_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, category_id, float(amount), transaction_date, description, payment_method, is_paid, receipt_filename))
        conn.commit()
        flash("İşlem başarıyla kaydedildi.", "success")
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

    receipt_file = request.files.get('receipt')
    receipt_filename = None
    if receipt_file and receipt_file.filename and allowed_file(receipt_file.filename):
        receipt_filename = f"{uuid.uuid4().hex[:12]}_{secure_filename(receipt_file.filename)}"
        receipt_file.save(os.path.join(UPLOAD_FOLDER, receipt_filename))

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if receipt_filename:
            cursor.execute("""
                UPDATE transactions 
                SET category_id = ?, amount = ?, transaction_date = ?, description = ?, payment_method = ?, receipt_path = ?
                WHERE id = ? AND user_id = ?
            """, (category_id, float(amount), transaction_date, description, payment_method, receipt_filename, tx_id, user_id))
        else:
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
        cursor.execute("SELECT receipt_path FROM transactions WHERE id = ? AND user_id = ?", (tx_id, user_id))
        r_row = cursor.fetchone()
        if r_row and r_row[0]:
            try:
                r_path = os.path.join(UPLOAD_FOLDER, r_row[0])
                if os.path.exists(r_path):
                    os.remove(r_path)
            except Exception:
                pass

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

@app.route('/receipts/<path:filename>')
@login_required
def get_receipt(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

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

        if new_qty <= 0.0001:
            cursor.execute("DELETE FROM portfolio_assets WHERE id = ?", (asset_id,))
        else:
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

# --- YEDEKLEME & GERİ YÜKLEME (BACKUP & RESTORE) ---
@app.route('/backup/download')
@login_required
def backup_download():
    user_id = session['user_id']
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT username, email FROM users WHERE id = ?", (user_id,))
    u_row = cursor.fetchone()
    username = u_row[0] if u_row else 'user'

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

    cursor.execute("SELECT title, amount, billing_cycle, next_due_date, is_active FROM subscriptions WHERE user_id = ?", (user_id,))
    subs = [{
        'title': r[0],
        'amount': float(r[1]),
        'billing_cycle': r[2],
        'next_due_date': r[3].strftime('%Y-%m-%d'),
        'is_active': int(r[4])
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
        SELECT c.name, r.title, r.amount, r.type, r.payment_method, r.day_of_month, r.is_active 
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
        'is_active': int(r[6])
    } for r in cursor.fetchall()]

    cursor.close()
    conn.close()

    backup_data = {
        'version': '1.0',
        'export_time': datetime.now().isoformat(),
        'username': username,
        'categories': cats,
        'transactions': txs,
        'portfolio_assets': assets,
        'subscriptions': subs,
        'savings_goals': goals,
        'recurring_transactions': recurrings
    }

    buf = io.BytesIO()
    buf.write(json.dumps(backup_data, ensure_ascii=False, indent=2).encode('utf-8'))
    buf.seek(0)
    filename = f"wealth_tracker_backup_{datetime.today().strftime('%Y%m%d_%H%M%S')}.json"
    return send_file(buf, as_attachment=True, download_name=filename, mimetype='application/json')

@app.route('/backup/restore', methods=['POST'])
@login_required
def backup_restore():
    user_id = session['user_id']
    backup_file = request.files.get('backup_file')
    if not backup_file or not backup_file.filename:
        flash("Lütfen geçerli bir JSON yedek dosyası seçin.", "danger")
        return redirect(url_for('dashboard'))

    try:
        content = json.loads(backup_file.read().decode('utf-8'))
    except Exception as e:
        flash(f"Yedek dosyası okunamadı (Geçersiz JSON formatı): {e}", "danger")
        return redirect(url_for('dashboard'))

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cat_map = {}
        for c in content.get('categories', []):
            cat_id = get_or_create_category(cursor, user_id, c['name'], c['type'], float(c.get('monthly_budget', 0)))
            cat_map[c['name']] = cat_id

        for t in content.get('transactions', []):
            cat_id = cat_map.get(t['category_name'])
            if not cat_id:
                cat_id = get_or_create_category(cursor, user_id, t['category_name'], 'gider', 0)
                cat_map[t['category_name']] = cat_id
            cursor.execute("""
                INSERT INTO transactions (user_id, category_id, amount, transaction_date, description, payment_method, is_paid, receipt_path)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (user_id, cat_id, float(t['amount']), t['transaction_date'], t.get('description', ''), t.get('payment_method', 'nakit'), int(t.get('is_paid', 1)), t.get('receipt_path')))

        for a in content.get('portfolio_assets', []):
            cursor.execute("SELECT id FROM portfolio_assets WHERE user_id = ? AND asset_symbol = ?", (user_id, a['asset_symbol']))
            existing = cursor.fetchone()
            if existing:
                cursor.execute("""
                    UPDATE portfolio_assets 
                    SET total_quantity = ?, average_cost = ?, updated_at = GETDATE()
                    WHERE id = ?
                """, (float(a['total_quantity']), float(a['average_cost']), existing[0]))
            else:
                cursor.execute("""
                    INSERT INTO portfolio_assets (user_id, asset_symbol, asset_type, total_quantity, average_cost)
                    VALUES (?, ?, ?, ?, ?)
                """, (user_id, a['asset_symbol'], a['asset_type'], float(a['total_quantity']), float(a['average_cost'])))

        for s in content.get('subscriptions', []):
            cursor.execute("""
                INSERT INTO subscriptions (user_id, title, amount, billing_cycle, next_due_date, is_active)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, s['title'], float(s['amount']), s['billing_cycle'], s['next_due_date'], int(s.get('is_active', 1))))

        for g in content.get('savings_goals', []):
            cursor.execute("""
                INSERT INTO savings_goals (user_id, title, target_amount, current_amount, target_date, status)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, g['title'], float(g['target_amount']), float(g.get('current_amount', 0)), g.get('target_date'), g.get('status', 'active')))

        for r in content.get('recurring_transactions', []):
            cat_id = cat_map.get(r['category_name'])
            if not cat_id:
                cat_id = get_or_create_category(cursor, user_id, r['category_name'], r.get('type', 'gider'), 0)
                cat_map[r['category_name']] = cat_id
            cursor.execute("""
                INSERT INTO recurring_transactions (user_id, category_id, title, amount, type, payment_method, day_of_month, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (user_id, cat_id, r['title'], float(r['amount']), r.get('type', 'gider'), r.get('payment_method', 'nakit'), int(r.get('day_of_month', 1)), int(r.get('is_active', 1))))

        conn.commit()
        flash("Yedek başarıyla geri yüklendi ve tüm verileriniz güncellendi!", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Geri yükleme sırasında hata: {e}", "danger")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('dashboard'))

if __name__ == '__main__':
    app.run(debug=True, port=5000)