"""
Moneta Financial SaaS - Production Readiness & Edge-Case Stress Test Suite
Bu test paketi; sıfır bakiye/işlem durumu (Zero-State), girdi sanitasyonu & SQL overflow,
hatalı form gönderimleri, özel hata sayfaları (404/500), HTTP güvenlik başlıkları ve
dış API kesintilerini (Offline Modu) sınar.

Çalıştırma:
  pytest tests/test_production_readiness.py -v
  veya
  python -m unittest tests/test_production_readiness.py
"""

import unittest
from unittest import mock
import sys
import os

# Proje ana dizinini path'e ekle
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import app
from db import get_db_connection
from services.finance_engine import parse_safe_amount


class TestZeroStateUser(unittest.TestCase):
    """Test 1: Zero-State / Yeni Boş Kullanıcı Testi"""

    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()
        self.zero_user_id = None

        # Veritabanında sıfır bakiyeli, hiç işlemi olmayan geçici test kullanıcısı oluştur
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO users (username, email, password_hash, first_name, last_name, initial_cash_balance, credit_card_limit)
                VALUES ('zero_test_user_qa', 'zero_qa@moneta.local', 'sha256_mock_hash', 'Zero', 'Tester', 0.0, 50000.0)
            """)
            conn.commit()
            cursor.execute("SELECT id FROM users WHERE username = 'zero_test_user_qa'")
            row = cursor.fetchone()
            if row:
                self.zero_user_id = row[0]
        finally:
            cursor.close()
            conn.close()

    def tearDown(self):
        # Geçici kullanıcıyı ve ilişkili verileri temizle
        if self.zero_user_id:
            conn = get_db_connection()
            cursor = conn.cursor()
            try:
                cursor.execute("DELETE FROM transactions WHERE user_id = ?", (self.zero_user_id,))
                cursor.execute("DELETE FROM portfolio_assets WHERE user_id = ?", (self.zero_user_id,))
                cursor.execute("DELETE FROM savings_goals WHERE user_id = ?", (self.zero_user_id,))
                cursor.execute("DELETE FROM categories WHERE user_id = ?", (self.zero_user_id,))
                cursor.execute("DELETE FROM users WHERE id = ?", (self.zero_user_id,))
                conn.commit()
            finally:
                cursor.close()
                conn.close()

    def test_zero_state_dashboard_and_apis_return_200(self):
        """Hiçbir varlığı veya işlemi olmayan sıfır kullanıcının sayfaları ZeroDivisionError olmadan 200 OK dönmeli."""
        self.assertIsNotNone(self.zero_user_id, "Geçici zero-state kullanıcısı oluşturulamadı.")
        
        with self.client.session_transaction() as sess:
            sess['user_id'] = self.zero_user_id
            sess['username'] = 'zero_test_user_qa'

        # 1. Dashboard Uç Noktası
        res_dash = self.client.get('/dashboard')
        self.assertEqual(res_dash.status_code, 200, "Sıfır kullanıcılı dashboard 200 dönmedi.")
        self.assertIn("Moneta", res_dash.text)

        # 2. Market Summary API
        res_market = self.client.get('/api/market-summary')
        self.assertEqual(res_market.status_code, 200, "Sıfır kullanıcılı market summary 200 dönmedi.")

        # 3. DB Diagnostics API (View ve SP testi)
        res_diag = self.client.get('/api/db-diagnostics')
        self.assertEqual(res_diag.status_code, 200, "Sıfır kullanıcılı db-diagnostics 200 dönmedi.")
        json_data = res_diag.get_json()
        self.assertEqual(json_data.get('status'), 'healthy')
        self.assertEqual(json_data['view']['data']['user_id'], self.zero_user_id)


class TestInputSanitationAndOverflow(unittest.TestCase):
    """Test 2: Girdi Sanitasyonu, Türkçe Format ve SQL Overflow Koruması"""

    def test_turkish_number_formatting(self):
        """Türkçe binlik ve ondalık formatların ('1.250,50' ve '1250,50') doğru ayrıştırılması."""
        self.assertEqual(parse_safe_amount("1.250,50"), 1250.50)
        self.assertEqual(parse_safe_amount("1250,50"), 1250.50)
        self.assertEqual(parse_safe_amount("₺ 1.250,50 TL"), 1250.50)
        self.assertEqual(parse_safe_amount(" 50.000,00 "), 50000.00)

    def test_negative_values_rejected(self):
        """Negatif tutarların reddedilmesi (None dönmesi)."""
        self.assertIsNone(parse_safe_amount("-500"))
        self.assertIsNone(parse_safe_amount("-1250.50"))
        self.assertIsNone(parse_safe_amount("-0.01"))

    def test_sql_arithmetic_overflow_rejected(self):
        """DECIMAL(18,2) sınırını aşan devasa sayıların reddedilmesi (SQL taşma koruması)."""
        self.assertIsNone(parse_safe_amount("999999999999999999999"))
        self.assertIsNone(parse_safe_amount("1000000000000.00"))  # 1 Trilyon (sınır 999.999.999.999,99)

    def test_invalid_strings_and_nan(self):
        """Harf ifadeleri, boş değerler ve NaN / Inf girdilerinin güvenle reddedilmesi."""
        self.assertIsNone(parse_safe_amount("abc"))
        self.assertIsNone(parse_safe_amount("NaN"))
        self.assertIsNone(parse_safe_amount("Infinity"))
        self.assertIsNone(parse_safe_amount(float('nan')))
        self.assertIsNone(parse_safe_amount(float('inf')))
        self.assertIsNone(parse_safe_amount(""))
        self.assertIsNone(parse_safe_amount(None))


class TestBadFormPostProtection(unittest.TestCase):
    """Test 3: Hatalı Form Post & Negatif/Aşırı Tutar Koruması"""

    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['username'] = 'efoss13'

    def test_negative_amount_transaction_redirects_safely(self):
        """Negatif tutarlı işlem girişi 500 hatası vermeden zarifçe yönlendirmeli."""
        res = self.client.post('/add_transaction', data={
            'category_id': 16,
            'amount': '-250',
            'transaction_date': '2026-10-15',
            'description': 'Negatif Test',
            'payment_method': 'nakit'
        }, follow_redirects=False)
        self.assertEqual(res.status_code, 302)

    def test_overflow_amount_transaction_redirects_safely(self):
        """Aşırı büyük sayı post edildiğinde SQL overflow vermeden yönlendirmeli."""
        res = self.client.post('/add_transaction', data={
            'category_id': 16,
            'amount': '999999999999999999999999999',
            'transaction_date': '2026-10-15',
            'description': 'Overflow Test',
            'payment_method': 'nakit'
        }, follow_redirects=False)
        self.assertEqual(res.status_code, 302)

    def test_invalid_card_limit_post_redirects_safely(self):
        """Geçersiz kredi kartı limiti post edildiğinde güvenle yönlendirmeli."""
        res = self.client.post('/update_card_limit', data={
            'credit_card_limit': '-5000'
        }, follow_redirects=False)
        self.assertEqual(res.status_code, 302)

    def test_invalid_buy_asset_post_redirects_safely(self):
        """Negatif adet veya geçersiz fiyatla varlık alımı denendiğinde yönlendirmeli."""
        res = self.client.post('/buy_asset', data={
            'symbol': 'THYAO',
            'asset_type': 'hisse',
            'quantity': '-10',
            'unit_price': 'abc'
        }, follow_redirects=False)
        self.assertEqual(res.status_code, 302)

    def test_insufficient_cash_balance_blocks_buy_asset(self):
        """Vadesiz nakit bakiyeden ödeme seçiliyse ve bakiye yetersizse alım engellenmeli."""
        res = self.client.post('/buy_asset', data={
            'symbol': 'TESTINSUFF',
            'asset_type': 'hisse',
            'quantity': '100000',
            'unit_price': '5000',
            'use_cash_balance_present': '1',
            'use_cash_balance': '1'
        }, follow_redirects=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn("Yetersiz Vadesiz Nakit Bakiye", res.text)

    def test_full_asset_sell_foreign_key_integrity(self):
        """Varlığın tamamı satıldığında FK_asset_transactions_assets hatası vermeden başarıyla tamamlanmalı ve nakit bakiyeye eklenmeli."""
        conn = get_db_connection()
        cursor = conn.cursor()
        test_sym = 'TESTSELLFK'
        try:
            # Temiz başlangıç
            cursor.execute("SELECT id FROM portfolio_assets WHERE user_id = 1 AND asset_symbol = ?", (test_sym,))
            existing = cursor.fetchone()
            if existing:
                cursor.execute("DELETE FROM asset_transactions WHERE asset_id = ?", (existing[0],))
                cursor.execute("DELETE FROM portfolio_assets WHERE id = ?", (existing[0],))
                conn.commit()

            # 1. Test varlığı alımı yap (10 adet @ 100 TL, nakit bakiyeden düşmeden test et)
            buy_res = self.client.post('/buy_asset', data={
                'symbol': test_sym,
                'asset_type': 'hisse',
                'quantity': '10',
                'unit_price': '100',
                'use_cash_balance_present': '1',
                'use_cash_balance': '0'
            }, follow_redirects=False)
            self.assertEqual(buy_res.status_code, 302)

            cursor.execute("SELECT id, total_quantity FROM portfolio_assets WHERE user_id = 1 AND asset_symbol = ?", (test_sym,))
            asset_row = cursor.fetchone()
            self.assertIsNotNone(asset_row)
            asset_id = asset_row[0]
            self.assertEqual(float(asset_row[1]), 10.0)

            # 2. Varlığın tamamını sat (10 adet @ 120 TL)
            sell_res = self.client.post('/sell_asset', data={
                'asset_id': str(asset_id),
                'quantity': '10',
                'unit_price': '120',
                'sell_currency': 'TRY'
            }, follow_redirects=False)
            self.assertEqual(sell_res.status_code, 302)

            # 3. Kontrol: FK hatası olmadan portfolio_assets total_quantity 0 olmalı ve asset_transactions'a satış işlenmeli
            cursor.execute("SELECT total_quantity FROM portfolio_assets WHERE id = ?", (asset_id,))
            updated_asset = cursor.fetchone()
            self.assertIsNotNone(updated_asset)
            self.assertEqual(float(updated_asset[0]), 0.0)

            cursor.execute("SELECT COUNT(*) FROM asset_transactions WHERE asset_id = ? AND transaction_type = 'satis'", (asset_id,))
            sell_tx_count = cursor.fetchone()[0]
            self.assertEqual(sell_tx_count, 1)

            # 4. Kontrol: Satış tutarı (1200 TL) nakit gelir işlemi olarak transactions tablosuna işlenmeli
            cursor.execute("SELECT COUNT(*) FROM transactions WHERE user_id = 1 AND description LIKE ? AND payment_method = 'nakit'", (f"%{test_sym} Varlık Satışı%",))
            cash_tx_count = cursor.fetchone()[0]
            self.assertEqual(cash_tx_count, 1)

        finally:
            # Test verilerini temizle
            cursor.execute("DELETE FROM transactions WHERE description LIKE ? AND user_id = 1", (f"%{test_sym}%",))
            cursor.execute("DELETE FROM asset_transactions WHERE user_id = 1 AND asset_id IN (SELECT id FROM portfolio_assets WHERE asset_symbol = ?)", (test_sym,))
            cursor.execute("DELETE FROM portfolio_assets WHERE user_id = 1 AND asset_symbol = ?", (test_sym,))
            conn.commit()
            cursor.close()
            conn.close()

    def test_bidirectional_savings_goal_delete_and_cash_sufficiency(self):
        """Kumbaraya nakit yetersizse aktarım engellenmeli; aktarım işlemi silindiğinde kumbara bakiyesi otomatik geri düşmeli."""
        conn = get_db_connection()
        cursor = conn.cursor()
        goal_title = 'TEST_BI_GOAL_QA'
        goal_id = None
        income_tx_id = None
        try:
            # 1. Test kumbarası oluştur (Hedef: 1000 TL)
            cursor.execute("""
                INSERT INTO savings_goals (user_id, title, target_amount, current_amount, status)
                VALUES (1, ?, 1000.0, 0.0, 'devam_ediyor')
            """, (goal_title,))
            conn.commit()
            cursor.execute("SELECT id FROM savings_goals WHERE user_id = 1 AND title = ?", (goal_title,))
            goal_id = cursor.fetchone()[0]

            # 2. Yetersiz bakiye ile devasa tutar aktarmayı dene -> engellenmeli
            insuff_res = self.client.post('/deposit_savings_goal', data={
                'goal_id': str(goal_id),
                'amount': '999999999'
            }, follow_redirects=True)
            self.assertEqual(insuff_res.status_code, 200)
            self.assertIn("Yetersiz Vadesiz Nakit Bakiye", insuff_res.text)

            # 3. Geçici nakit gelir ekle (Mevcut bakiye + en az 500 TL olacak şekilde), kumbaraya 500 TL aktar
            from app import get_user_cash_balance
            current_cash = get_user_cash_balance(cursor, 1)
            temp_income_amount = max(500.0, 1000.0 - current_cash)

            cursor.execute("SELECT TOP 1 id FROM categories WHERE user_id = 1 AND type = 'gelir'")
            cat_row = cursor.fetchone()
            inc_cat_id = cat_row[0] if cat_row else 1
            cursor.execute("""
                INSERT INTO transactions (user_id, category_id, amount, transaction_date, description, payment_method, is_paid)
                VALUES (1, ?, ?, GETDATE(), 'TEST_BI_GOAL_INCOME', 'nakit', 1)
            """, (inc_cat_id, temp_income_amount))
            conn.commit()
            cursor.execute("SELECT id FROM transactions WHERE user_id = 1 AND description = 'TEST_BI_GOAL_INCOME'")
            income_tx_id = cursor.fetchone()[0]

            dep_res = self.client.post('/deposit_savings_goal', data={
                'goal_id': str(goal_id),
                'amount': '500'
            }, follow_redirects=False)
            self.assertEqual(dep_res.status_code, 302)

            cursor.execute("SELECT current_amount FROM savings_goals WHERE id = ?", (goal_id,))
            self.assertEqual(float(cursor.fetchone()[0]), 500.0)

            # 4. Kumbara aktarım işlemini bul ve /delete_transaction ile sil -> kumbara bakiyesi 0.0'a geri düşmeli
            cursor.execute("SELECT id FROM transactions WHERE user_id = 1 AND description = ?", (f"Kumbaraya Ekleme: {goal_title}",))
            dep_tx_row = cursor.fetchone()
            self.assertIsNotNone(dep_tx_row)
            dep_tx_id = dep_tx_row[0]

            del_res = self.client.post(f'/delete_transaction/{dep_tx_id}', follow_redirects=False)
            self.assertEqual(del_res.status_code, 302)

            cursor.execute("SELECT current_amount FROM savings_goals WHERE id = ?", (goal_id,))
            self.assertEqual(float(cursor.fetchone()[0]), 0.0)

        finally:
            if income_tx_id:
                cursor.execute("DELETE FROM transactions WHERE id = ?", (income_tx_id,))
            cursor.execute("DELETE FROM transactions WHERE user_id = 1 AND description LIKE ?", (f"%{goal_title}%",))
            if goal_id:
                cursor.execute("DELETE FROM savings_goals WHERE id = ?", (goal_id,))
            conn.commit()
            cursor.close()
            conn.close()


class TestCustomErrorPagesAndSecurityHeaders(unittest.TestCase):
    """Test 4: Özel 404 & 500 Hata Şablonları ve HTTP Güvenlik Başlıkları"""

    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()

    def test_custom_404_page_and_headers(self):
        """Var olmayan sayfada özel 404 şablonu ve HTTP güvenlik başlıkları dönmeli."""
        res = self.client.get('/olmayan-sayfa-test-404')
        self.assertEqual(res.status_code, 404)
        self.assertIn("404", res.text)
        self.assertIn("Aradığınız Sayfa Bulunamadı", res.text)
        self.assertIn("Ana Panele Dön", res.text)

        # HTTP Güvenlik Başlıkları
        self.assertEqual(res.headers.get('X-Content-Type-Options'), 'nosniff')
        self.assertEqual(res.headers.get('X-Frame-Options'), 'SAMEORIGIN')
        self.assertEqual(res.headers.get('Referrer-Policy'), 'strict-origin-when-cross-origin')

    def test_custom_500_page_rendering(self):
        """500 hatası tetiklendiğinde özel 500 şablonu render edilmeli."""
        with app.test_request_context():
            from app import internal_server_error
            res, code = internal_server_error(Exception("Test Exception"))
            self.assertEqual(code, 500)
            self.assertIn("500", res)
            self.assertIn("İşleminiz Güvenli Şekilde Durduruldu", res)


class TestOfflineExternalApiResilience(unittest.TestCase):
    """Test 5: Dış API Kesintisi ve Offline Modu Simülasyonu"""

    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['username'] = 'efoss13'

    @mock.patch('yfinance.Ticker')
    def test_dashboard_offline_resilience(self, mock_ticker):
        """yfinance tamamen çöktüğünde /dashboard sayfası 200 OK ile açılmalı."""
        mock_ticker.side_effect = Exception("Simulated Offline / Timeout Error")
        
        res = self.client.get('/dashboard')
        self.assertEqual(res.status_code, 200)
        self.assertIn("Moneta", res.text)
        self.assertIn("Net Servet", res.text)

    @mock.patch('yfinance.Ticker')
    def test_market_summary_offline_resilience(self, mock_ticker):
        """yfinance çöktüğünde /api/market-summary rotası 200 OK fallback ile dönmeli."""
        mock_ticker.side_effect = Exception("Simulated Offline / Timeout Error")
        
        # force refresh ile cache atlanarak API çağrılır
        res = self.client.get('/api/market-summary?refresh=1')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn(data.get('status'), ['success', 'fallback', 'stale'])
        self.assertIsInstance(data.get('items'), list)


class TestStatePersistenceAndBackupSchema(unittest.TestCase):
    """Test 6 (Faz 2/3): Sekme/Dönem Hafızası ve JSON Yedekleme Şema Bütünlüğü"""

    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['username'] = 'efoss13'

    def test_redirect_back_to_dashboard_preserves_tab_and_period(self):
        """redirect_back_to_dashboard form veya referrer üzerinden month, year ve tab değerlerini korumalı."""
        from app import redirect_back_to_dashboard
        with app.test_request_context(
            '/buy_asset',
            method='POST',
            data={'active_tab': 'portfolio', 'month': '9', 'year': '2026'}
        ):
            resp = redirect_back_to_dashboard(default_tab='portfolio')
            self.assertEqual(resp.status_code, 302)
            self.assertIn('month=9', resp.location)
            self.assertIn('year=2026', resp.location)
            self.assertIn('tab=portfolio', resp.location)

        # Referrer üzerinden koruma testi (recurring -> goals normalizasyonu ile)
        with app.test_request_context(
            '/delete_subscription/1',
            method='POST',
            headers={'Referer': 'http://localhost:5000/dashboard?month=9&year=2026&tab=goals'}
        ):
            resp2 = redirect_back_to_dashboard(default_tab='recurring')
            self.assertEqual(resp2.status_code, 302)
            self.assertIn('month=9', resp2.location)
            self.assertIn('year=2026', resp2.location)
            self.assertIn('tab=goals', resp2.location)

    def test_backup_download_includes_extended_schema_fields(self):
        """/backup/download çıktısı initial_cash_balance, credit_card_limit ve asset_transactions alanlarını içermeli."""
        res = self.client.get('/backup/download')
        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertIsNotNone(payload)
        self.assertIn('initial_cash_balance', payload)
        self.assertIn('credit_card_limit', payload)
        self.assertIn('asset_transactions', payload)
        self.assertIn('subscriptions', payload)
        self.assertIn('recurring_transactions', payload)

    def test_edit_transaction_payment_method_syncs_is_paid(self):
        """/edit_transaction ile nakit <-> kredi_karti geçişlerinde is_paid durumu otomatik senkronize edilmeli."""
        conn = get_db_connection()
        cursor = conn.cursor()
        tx_id = None
        try:
            cursor.execute("SELECT TOP 1 id FROM categories WHERE user_id = 1 AND type = 'gider'")
            cat_row = cursor.fetchone()
            self.assertIsNotNone(cat_row)
            cat_id = cat_row[0]

            cursor.execute("""
                INSERT INTO transactions (user_id, category_id, amount, transaction_date, description, payment_method, is_paid)
                VALUES (1, ?, 150.0, '2026-10-15', 'TEST_EDIT_IS_PAID_SYNC', 'nakit', 1)
            """, (cat_id,))
            conn.commit()

            cursor.execute("SELECT TOP 1 id, is_paid FROM transactions WHERE user_id = 1 AND description = 'TEST_EDIT_IS_PAID_SYNC' ORDER BY id DESC")
            row = cursor.fetchone()
            self.assertIsNotNone(row)
            tx_id = row[0]
            self.assertEqual(int(row[1]), 1)

            # 1. nakit -> kredi_karti düzenlemesi: is_paid = 0 olmalı
            res1 = self.client.post(f'/edit_transaction/{tx_id}', data={
                'category_id': str(cat_id),
                'amount': '150',
                'transaction_date': '2026-10-15',
                'description': 'TEST_EDIT_IS_PAID_SYNC',
                'payment_method': 'kredi_karti'
            }, follow_redirects=False)
            self.assertEqual(res1.status_code, 302)

            cursor.execute("SELECT payment_method, is_paid FROM transactions WHERE id = ?", (tx_id,))
            after_cc = cursor.fetchone()
            self.assertEqual(after_cc[0], 'kredi_karti')
            self.assertEqual(int(after_cc[1]), 0)

            # 2. kredi_karti -> nakit düzenlemesi: is_paid = 1 olmalı
            res2 = self.client.post(f'/edit_transaction/{tx_id}', data={
                'category_id': str(cat_id),
                'amount': '150',
                'transaction_date': '2026-10-15',
                'description': 'TEST_EDIT_IS_PAID_SYNC',
                'payment_method': 'nakit'
            }, follow_redirects=False)
            self.assertEqual(res2.status_code, 302)

            cursor.execute("SELECT payment_method, is_paid FROM transactions WHERE id = ?", (tx_id,))
            after_cash = cursor.fetchone()
            self.assertEqual(after_cash[0], 'nakit')
            self.assertEqual(int(after_cash[1]), 1)
        finally:
            if tx_id:
                cursor.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
                conn.commit()
            cursor.close()
            conn.close()

    def test_receipt_idor_protection_returns_404_for_unowned_file(self):
        """Kullanıcıya ait olmayan bir fiş/dekont dosyası istendiğinde /receipts/<filename> 404 dönmeli."""
        res = self.client.get('/receipts/unowned_foreign_receipt_file.png')
        self.assertEqual(res.status_code, 404)


if __name__ == '__main__':
    unittest.main()
