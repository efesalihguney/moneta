"""
Moneta - Zengin Finansal Demo Verisi Doldurma Betiği (seed_demo_data.py)
------------------------------------------------------------------------
Bu betik (`python seed_demo_data.py`), akademik jüri değerlendirmesi ve yerel
testler için aktif kullanıcı (`efoss13` veya sistemdeki ana kullanıcı) hesabına
gerçekçi ve uçtan uca tutarlı bir finansal veri seti yükler:

1. Vadesiz Nakit ve Kredi Kartı Limiti:
   - initial_cash_balance = 35.450,00 TL
   - credit_card_limit    = 75.000,00 TL
2. Temel Kategoriler ve Aylık Bütçe Hedefleri:
   - Maaş & Gelir, Market & Gıda, Kira & Barınma, Yatırım & Portföy,
     Faturalar, Eğlence & Sosyal, Ulaşım & Yakıt, Tasarruf & Kumbara
3. Canlı Portföy Varlıkları (portfolio_assets & asset_transactions):
   - THYAO (BIST - hisse)      : 45 Adet @ 280,50 TL (Nakit akışından düşümlü)
   - NVDA  (ABD - abd_hisse)   : 12 Adet @ 125,40 USD (TL karşılığı hesaplanarak)
   - GAU   (Altın - emtia_altin): 15 Gram @ 2.850,00 TL
4. Düzenli Fatura ve Abonelikler (subscriptions & recurring_transactions):
   - Elektrik Faturası : 1.250,00 TL (kredi_karti, auto_apply=1)
   - Ev Kirası         : 14.500,00 TL (nakit, auto_apply=1)
   - Netflix & Spotify : 450,00 TL (kredi_karti, auto_apply=1)
5. Son 30 Gün İşlemleri, Kredi Kartı Ekstresi (~18.450,00 TL) ve Kumbaralar:
   - Aylık Maaş (+55.000,00 TL), Kira, Market, Akaryakıt, Sosyal harcamalar
   - 2 Aktif Tasarruf Kumbarası ("Yeni Araba Peşinatı" ve "Yaz Tatili Fonu")
"""

from datetime import datetime, timedelta
import calendar
from db import get_db_connection
from app import get_effective_usdtry_rate, get_or_create_category


def seed_demo_account():
    print("=" * 68)
    print("MONETA - ZENGİN FİNANSAL DEMO VERİSİ YÜKLEME MOTORU")
    print("=" * 68)

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # 0. Aktif kullanıcıyı tespit et (Öncelik: efoss13, yoksa ilk kullanıcı)
        cursor.execute("""
            SELECT TOP 1 id, username, ISNULL(first_name, username)
            FROM users
            ORDER BY CASE WHEN username = 'efoss13' THEN 0 ELSE 1 END, id ASC
        """)
        user_row = cursor.fetchone()
        if not user_row:
            from init_db import ensure_default_seed_data
            ensure_default_seed_data()
            cursor.execute("SELECT TOP 1 id, username, ISNULL(first_name, username) FROM users ORDER BY id ASC")
            user_row = cursor.fetchone()

        user_id, username, first_name = user_row[0], user_row[1], user_row[2]
        print(f"[1/6] Hedef Kullanıcı Tespit Edildi: {username} (ID: {user_id})")

        # İdempotent çalışma için kullanıcının önceki kayıtlarını temizle (FK sırasına uygun)
        cursor.execute("DELETE FROM asset_transactions WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM portfolio_assets WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM transactions WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM recurring_transactions WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM savings_goals WHERE user_id = ?", (user_id,))

        # 1. Vadesiz Nakit ve Kredi Kartı Limiti Güncellemesi
        initial_cash_balance = 35450.00
        credit_card_limit = 75000.00
        cursor.execute("""
            UPDATE users
            SET initial_cash_balance = ?, credit_card_limit = ?
            WHERE id = ?
        """, (initial_cash_balance, credit_card_limit, user_id))
        print(f"[2/6] Başlangıç Nakit Bakiyesi ({initial_cash_balance:,.2f} TL) ve Kart Limiti ({credit_card_limit:,.2f} TL) ayarlandı.")

        # 2. Temel Kategorilerin Kontrolü ve Bütçe Ataması
        required_categories = [
            ('Maaş & Gelir', 'gelir', 0.00),
            ('Market & Gıda', 'gider', 12000.00),
            ('Kira & Barınma', 'gider', 15000.00),
            ('Yatırım & Portföy', 'gider', 0.00),
            ('Faturalar', 'gider', 3500.00),
            ('Eğlence & Sosyal', 'gider', 5000.00),
            ('Ulaşım & Yakıt', 'gider', 6000.00),
            ('Tasarruf & Kumbara', 'gider', 0.00),
        ]
        cat_ids = {}
        for c_name, c_type, c_budget in required_categories:
            cid = get_or_create_category(cursor, user_id, c_name, c_type, c_budget)
            cursor.execute("UPDATE categories SET monthly_budget = ? WHERE id = ?", (c_budget, cid))
            cat_ids[(c_name, c_type)] = cid
        print(f"[3/6] {len(required_categories)} temel kategori ve aylık bütçe hedefleri doğrulandı.")

        now = datetime.today()
        cur_year, cur_month = now.year, now.month
        period_key = f"{cur_year:04d}-{cur_month:02d}"
        last_day_of_month = calendar.monthrange(cur_year, cur_month)[1]

        def safe_date_in_month(day_num):
            d = min(max(1, day_num), min(now.day, last_day_of_month))
            return f"{cur_year:04d}-{cur_month:02d}-{d:02d}"

        # 3. Canlı Portföy Varlıkları (portfolio_assets & asset_transactions)
        live_usd = get_effective_usdtry_rate()
        thyao_qty, thyao_cost_tl = 45.0, 280.50
        thyao_total_tl = round(thyao_qty * thyao_cost_tl, 2)  # 12.622,50 TL

        nvda_qty, nvda_cost_usd = 12.0, 125.40
        nvda_cost_tl = round(nvda_cost_usd * live_usd, 2)
        nvda_total_tl = round(nvda_qty * nvda_cost_tl, 2)

        gau_qty, gau_cost_tl = 15.0, 2850.00
        gau_total_tl = round(gau_qty * gau_cost_tl, 2)  # 42.750,00 TL

        portfolio_seed = [
            ('THYAO', 'hisse', thyao_qty, thyao_cost_tl, thyao_total_tl, safe_date_in_month(6)),
            ('NVDA', 'abd_hisse', nvda_qty, nvda_cost_tl, nvda_total_tl, (now - timedelta(days=24)).strftime('%Y-%m-%d')),
            ('GAU', 'emtia_altin', gau_qty, gau_cost_tl, gau_total_tl, (now - timedelta(days=28)).strftime('%Y-%m-%d')),
        ]

        for sym, a_type, qty, u_cost_tl, tot_tl, tx_date in portfolio_seed:
            cursor.execute("""
                INSERT INTO portfolio_assets (user_id, asset_symbol, asset_type, total_quantity, average_cost)
                OUTPUT INSERTED.id
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, sym, a_type, qty, u_cost_tl))
            asset_id = int(cursor.fetchone()[0])

            cursor.execute("""
                INSERT INTO asset_transactions (asset_id, user_id, transaction_type, quantity, unit_price, total_amount, realized_pnl, transaction_date)
                VALUES (?, ?, 'alis', ?, ?, ?, 0.00, ?)
            """, (asset_id, user_id, qty, u_cost_tl, tot_tl, tx_date))

        print(f"[4/6] Portföy varlıkları eklendi: THYAO (45 Adet), NVDA (12 Adet @ ${nvda_cost_usd:.2f}), GAU (15 Gram).")

        # 4. Düzenli Fatura ve Abonelikler (subscriptions & recurring_transactions)
        due_date_1 = (now + timedelta(days=5)).strftime('%Y-%m-%d')
        due_date_2 = (now + timedelta(days=10)).strftime('%Y-%m-%d')
        due_date_3 = (now + timedelta(days=3)).strftime('%Y-%m-%d')

        subscriptions_seed = [
            ('Elektrik Faturası', 1250.00, 'aylik', due_date_1, 1, 'kredi_karti', 1, period_key),
            ('Ev Kirası', 14500.00, 'aylik', due_date_2, 1, 'nakit', 1, period_key),
            ('Netflix & Spotify', 450.00, 'aylik', due_date_3, 1, 'kredi_karti', 1, period_key),
        ]
        for s_title, s_amt, s_cycle, s_due, s_active, s_method, s_auto, s_period in subscriptions_seed:
            cursor.execute("""
                INSERT INTO subscriptions (user_id, title, amount, billing_cycle, next_due_date, is_active, payment_method, auto_apply, last_synced_period)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (user_id, s_title, s_amt, s_cycle, s_due, s_active, s_method, s_auto, s_period))

        # Düzenli işlem şablonları (Maaş & Kira)
        cursor.execute("""
            INSERT INTO recurring_transactions (user_id, category_id, title, amount, type, payment_method, day_of_month, is_active, last_synced_period)
            VALUES (?, ?, 'Aylık Maaş Ödemesi', 55000.00, 'gelir', 'nakit', 1, 1, ?)
        """, (user_id, cat_ids[('Maaş & Gelir', 'gelir')], period_key))

        print("[5/6] Düzenli fatura/abonelikler (Elektrik, Ev Kirası, Netflix & Spotify) ve maaş şablonu eklendi.")

        # 5. Tasarruf Kumbaraları (savings_goals)
        goals_seed = [
            ('Yeni Araba Peşinatı', 250000.00, 68000.00, '2027-06-30', 'devam_ediyor'),
            ('Yaz Tatili Fonu', 45000.00, 18500.00, '2027-05-15', 'devam_ediyor'),
        ]
        for g_title, g_target, g_curr, g_date, g_status in goals_seed:
            cursor.execute("""
                INSERT INTO savings_goals (user_id, title, target_amount, current_amount, target_date, status)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, g_title, g_target, g_curr, g_date, g_status))

        # 6. Son 30 Güne Ait İşlemler ve Kredi Kartı Ekstresi (transactions)
        # Kredi kartı harcamaları (is_paid = 0) toplamı: 1.250 + 450 + 6.450 + 2.800 + 4.300 + 3.200 = 18.450,00 TL
        transactions_seed = [
            # Nakit Gelir (Maaş)
            (cat_ids[('Maaş & Gelir', 'gelir')], 55000.00, safe_date_in_month(1), '[Düzenli] Aylık Maaş Ödemesi', 'nakit', 1),
            # Nakit Gider (Ev Kirası)
            (cat_ids[('Kira & Barınma', 'gider')], 14500.00, safe_date_in_month(3), '[Fatura/Abonelik] Ev Kirası', 'nakit', 1),
            # Nakit Portföy Alışı (THYAO - 45 Adet @ 280.50 TL = 12.622,50 TL)
            (cat_ids[('Yatırım & Portföy', 'gider')], thyao_total_tl, safe_date_in_month(6), 'THYAO Varlık Alışı (45.00 Adet)', 'nakit', 1),
            # Nakit Kumbara Aktarımları
            (cat_ids[('Tasarruf & Kumbara', 'gider')], 5000.00, safe_date_in_month(8), 'Kumbaraya Ekleme: Yeni Araba Peşinatı', 'nakit', 1),
            (cat_ids[('Tasarruf & Kumbara', 'gider')], 2500.00, safe_date_in_month(10), 'Kumbaraya Ekleme: Yaz Tatili Fonu', 'nakit', 1),
            # Kredi Kartı Ekstresi Harcamaları (is_paid = 0, Toplam = 18.450,00 TL)
            (cat_ids[('Faturalar', 'gider')], 1250.00, safe_date_in_month(4), '[Fatura/Abonelik] Elektrik Faturası', 'kredi_karti', 0),
            (cat_ids[('Faturalar', 'gider')], 450.00, safe_date_in_month(5), '[Fatura/Abonelik] Netflix & Spotify', 'kredi_karti', 0),
            (cat_ids[('Market & Gıda', 'gider')], 6450.00, safe_date_in_month(7), 'Migros Aylık Mutfak & Erzak Alışverişi', 'kredi_karti', 0),
            (cat_ids[('Market & Gıda', 'gider')], 2800.00, safe_date_in_month(12), 'Taze Pazar, Manav & Şarküteri Alışverişi', 'kredi_karti', 0),
            (cat_ids[('Ulaşım & Yakıt', 'gider')], 4300.00, safe_date_in_month(14), 'Shell V-Power Akaryakıt & HGS Otoyol Dolumu', 'kredi_karti', 0),
            (cat_ids[('Eğlence & Sosyal', 'gider')], 3200.00, safe_date_in_month(16), 'Hafta Sonu Restoran, Kahve & Kültür Etkinliği', 'kredi_karti', 0),
        ]

        for cat_id, amt, t_date, desc, p_method, is_paid in transactions_seed:
            cursor.execute("""
                INSERT INTO transactions (user_id, category_id, amount, transaction_date, description, payment_method, is_paid)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (user_id, cat_id, amt, t_date, desc, p_method, is_paid))

        conn.commit()
        print("[6/6] Son 30 gün işlemleri, 18.450,00 TL aktif kredi kartı ekstresi ve 2 kumbara hedefi kaydedildi.")
        print("=" * 68)
        print("DEMO VERİ YÜKLEME İŞLEMİ BAŞARIYLA TAMAMLANDI!")
        print("=" * 68)

    except Exception as e:
        conn.rollback()
        print(f"[HATA] Demo verisi yüklenirken hata oluştu: {e}")
        raise
    finally:
        cursor.close()
        conn.close()


if __name__ == '__main__':
    seed_demo_account()

