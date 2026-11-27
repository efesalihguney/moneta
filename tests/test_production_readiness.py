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


class TestProductionSecurityHardening(unittest.TestCase):
    """Test 7: Canlı Ortam Güvenlik, CSRF, SQL Auth, Kategori FK ve Yedek Tekilleştirme Testleri"""

    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()

    def tearDown(self):
        app.config.pop('CSRF_ENABLED_IN_TESTS', None)

    def test_csrf_protection_blocks_missing_token_and_allows_valid_token(self):
        """CSRF aktifken token olmadan gelen POST 403 almalı; geçerli oturum token'ı ile kabul edilmeli."""
        import re
        app.config['CSRF_ENABLED_IN_TESTS'] = True
        bad_res = self.client.post('/login', data={'login_identifier': 'efoss13', 'password': 'wrong'})
        self.assertEqual(bad_res.status_code, 403)

        page_res = self.client.get('/login')
        self.assertEqual(page_res.status_code, 200)
        m = re.search(r'name="csrf_token"\s+value="([^"]+)"', page_res.text)
        self.assertIsNotNone(m, "Login formunda csrf_token gizli alanı bulunamadı.")
        token = m.group(1)

        ok_res = self.client.post('/login', data={
            'csrf_token': token,
            'login_identifier': 'nonexistent_qa_user',
            'password': 'wrongpassword123'
        })
        self.assertNotEqual(ok_res.status_code, 403)

    def test_auto_login_disabled_in_production_by_default(self):
        """Debug/Testing kapalıyken ALLOW_AUTO_LOGIN açık değilse otomatik giriş engellenmeli."""
        from app import _is_auto_login_allowed
        from config import Config
        orig_debug = app.debug
        orig_testing = app.config.get('TESTING')
        orig_allow = Config.ALLOW_AUTO_LOGIN
        try:
            app.debug = False
            app.config['TESTING'] = False
            Config.ALLOW_AUTO_LOGIN = False
            self.assertFalse(_is_auto_login_allowed())

            res = self.client.get('/auto-login', follow_redirects=False)
            self.assertEqual(res.status_code, 302)
            self.assertIn('/login', res.headers.get('Location', ''))
        finally:
            app.debug = orig_debug
            app.config['TESTING'] = orig_testing
            Config.ALLOW_AUTO_LOGIN = orig_allow

    def test_config_supports_sql_auth_connection_string(self):
        """Bulut/Linux MSSQL için DB_USER/DB_PASSWORD verildiğinde UID/PWD bağlantı dizgesi üretilmeli."""
        from config import Config
        orig_user = Config.DB_USER
        orig_pwd = Config.DB_PASSWORD
        orig_srv = Config.DB_SERVER
        try:
            Config.DB_USER = 'sa_cloud'
            Config.DB_PASSWORD = 'Secret!123'
            Config.DB_SERVER = 'moneta.database.windows.net'
            conn_str = Config.get_connection_string()
            self.assertIn('UID=sa_cloud;', conn_str)
            self.assertIn('PWD=Secret!123;', conn_str)
            self.assertIn('Trusted_Connection=no;', conn_str)
            self.assertEqual(Config.get_candidate_servers(), ['moneta.database.windows.net'])
        finally:
            Config.DB_USER = orig_user
            Config.DB_PASSWORD = orig_pwd
            Config.DB_SERVER = orig_srv

    def test_register_rejects_short_password_and_invalid_email(self):
        """Kayıt formu 8 karakterden kısa şifreleri ve geçersiz e-postaları reddetmeli."""
        res_short = self.client.post('/register', data={
            'first_name': 'Test',
            'last_name': 'User',
            'username': 'shortpwduser_qa',
            'email': 'valid@moneta.local',
            'password': '123'
        })
        self.assertEqual(res_short.status_code, 200)
        self.assertIn('en az 8 karakter', res_short.text)

        res_email = self.client.post('/register', data={
            'first_name': 'Test',
            'last_name': 'User',
            'username': 'bademailuser_qa',
            'email': 'not-an-email',
            'password': 'StrongPassword123'
        })
        self.assertEqual(res_email.status_code, 200)
        self.assertIn('geçerli bir e-posta', res_email.text)

    def test_delete_category_blocked_when_used_by_recurring(self):
        """Düzenli işlem şablonuna bağlı kategori silinmeye çalışıldığında güvenli uyarı vermeli."""
        conn = get_db_connection()
        cursor = conn.cursor()
        cat_id = None
        rec_id = None
        try:
            # Önceki yarım kalmış test artıklarını temizle (FK sırasına uygun)
            cursor.execute("DELETE FROM transactions WHERE user_id = 1 AND (description LIKE '%TEST_REC_FK_QA%' OR category_id IN (SELECT id FROM categories WHERE user_id = 1 AND name = 'TEST_CAT_FK_QA'))")
            cursor.execute("DELETE FROM recurring_transactions WHERE user_id = 1 AND title = 'TEST_REC_FK_QA'")
            cursor.execute("DELETE FROM categories WHERE user_id = 1 AND name = 'TEST_CAT_FK_QA'")
            conn.commit()

            cursor.execute("INSERT INTO categories (user_id, name, type, monthly_budget) OUTPUT INSERTED.id VALUES (1, 'TEST_CAT_FK_QA', 'gider', 0)")
            cat_id = int(cursor.fetchone()[0])

            cursor.execute("""
                INSERT INTO recurring_transactions (user_id, category_id, title, amount, type, payment_method, day_of_month, is_active)
                OUTPUT INSERTED.id
                VALUES (1, ?, 'TEST_REC_FK_QA', 100.0, 'gider', 'nakit', 1, 0)
            """, (cat_id,))
            rec_id = int(cursor.fetchone()[0])
            conn.commit()

            with self.client.session_transaction() as sess:
                sess['user_id'] = 1
                sess['username'] = 'efoss13'

            res = self.client.post(f'/delete_category/{cat_id}', follow_redirects=True)
            self.assertEqual(res.status_code, 200)
            self.assertIn('silinemez', res.text)
        finally:
            cursor.execute("DELETE FROM transactions WHERE user_id = 1 AND (description LIKE '%TEST_REC_FK_QA%' OR category_id IN (SELECT id FROM categories WHERE user_id = 1 AND name = 'TEST_CAT_FK_QA'))")
            cursor.execute("DELETE FROM recurring_transactions WHERE user_id = 1 AND title = 'TEST_REC_FK_QA'")
            if rec_id:
                cursor.execute("DELETE FROM recurring_transactions WHERE id = ?", (rec_id,))
            if cat_id:
                cursor.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
            cursor.execute("DELETE FROM categories WHERE user_id = 1 AND name = 'TEST_CAT_FK_QA'")
            conn.commit()
            cursor.close()
            conn.close()

    def test_upload_folder_is_outside_static(self):
        """Yüklenen fiş klasörü kimlik doğrulamasız /static/ altından servis edilmemeli."""
        from app import UPLOAD_FOLDER
        normalized = os.path.normpath(UPLOAD_FOLDER).lower()
        self.assertNotIn(os.path.sep + 'static' + os.path.sep, normalized)


class TestSmartReceiptScanner(unittest.TestCase):
    """Test 8: Akıllı Fiş & Fatura Okuma (OCR & AI Kategorizasyon) Hibrit Motor ve API Testleri"""

    SAMPLE_CATEGORIES = [
        {'id': 1, 'name': 'Maaş', 'type': 'gelir'},
        {'id': 2, 'name': 'Market & Gıda', 'type': 'gider'},
        {'id': 3, 'name': 'Ulaşım & Yakıt', 'type': 'gider'},
        {'id': 4, 'name': 'Faturalar', 'type': 'gider'},
        {'id': 5, 'name': 'Sağlık', 'type': 'gider'},
        {'id': 6, 'name': 'Yeme & İçme', 'type': 'gider'},
    ]

    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['username'] = 'efoss13'

    def tearDown(self):
        app.config.pop('CSRF_ENABLED_IN_TESTS', None)

    @mock.patch('services.receipt_scanner.requests.post')
    def test_gemini_vision_primary_engine_returns_structured_json(self, mock_post):
        """GEMINI_API_KEY tanımlıysa birincil Vision AI motoru yapılandırılmış JSON çıktısı döndürmeli."""
        import json
        from services.receipt_scanner import scan_receipt

        ai_payload = {
            'merchant': 'Migros Ticaret A.Ş.',
            'amount': 485.50,
            'date': '2026-11-26',
            'category_id': 2,
            'category_name': 'Market & Gıda',
            'vat_amount': 42.10,
            'description': 'Migros - Market Alışverişi (KDV: 42.10 TL)',
            'confidence': 0.96,
        }
        mock_resp = mock.MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            'candidates': [{'content': {'parts': [{'text': json.dumps(ai_payload)}]}}]
        }
        mock_post.return_value = mock_resp

        result = scan_receipt(
            file_bytes=b'\x89PNG\r\n\x1a\nfake_png_bytes',
            filename='migros_fis.png',
            categories=self.SAMPLE_CATEGORIES,
            api_key='fake-gemini-key',
            model='gemini-2.5-flash',
        )

        self.assertTrue(result['success'])
        self.assertEqual(result['engine'], 'gemini')
        self.assertEqual(result['merchant'], 'Migros Ticaret A.Ş.')
        self.assertEqual(result['amount'], 485.50)
        self.assertEqual(result['date'], '2026-11-26')
        self.assertEqual(result['category_id'], 2)
        self.assertEqual(result['category_name'], 'Market & Gıda')
        self.assertIn('KDV: 42.10 TL', result['description'])
        self.assertEqual(result['confidence'], 0.96)
        self.assertIsNone(result['warning'])

    @mock.patch('services.receipt_scanner.requests.post')
    def test_gemini_failure_falls_back_to_offline_engine(self, mock_post):
        """Gemini API bağlantı hatası verdiğinde sistem kilitlenmemeli ve yedek ayrıştırıcıya düşmeli."""
        from services.receipt_scanner import scan_receipt

        mock_post.side_effect = ConnectionError("Simulated Gemini outage")

        result = scan_receipt(
            file_bytes=b'\xff\xd8\xff\xe0fake_jpg',
            filename='shell_yakit_1250.50tl_2026-11-26.jpg',
            categories=self.SAMPLE_CATEGORIES,
            api_key='fake-gemini-key',
        )

        self.assertTrue(result['success'])
        self.assertEqual(result['engine'], 'fallback')
        self.assertEqual(result['category_id'], 3)
        self.assertEqual(result['category_name'], 'Ulaşım & Yakıt')
        self.assertEqual(result['amount'], 1250.50)
        self.assertEqual(result['date'], '2026-11-26')
        self.assertIsNotNone(result['warning'])

    def test_offline_smart_category_matching(self):
        """Bilinen Türk işletmeleri kullanıcının ilgili veritabanı kategorisiyle doğru eşleşmeli."""
        from services.receipt_scanner import match_best_category

        cases = [
            ('Migros Ticaret A.Ş.', '', 2, 'Market & Gıda'),
            ('BİM Birleşik Mağazalar', '', 2, 'Market & Gıda'),
            ('Shell & Turcas', '', 3, 'Ulaşım & Yakıt'),
            ('Opet Akaryakıt', '', 3, 'Ulaşım & Yakıt'),
            ('İGDAŞ Doğalgaz Faturası', '', 4, 'Faturalar'),
            ('Türk Telekom', '', 4, 'Faturalar'),
            ('Merkez Eczanesi', '', 5, 'Sağlık'),
            ('Starbucks Coffee', '', 6, 'Yeme & İçme'),
        ]
        for merchant, raw_text, exp_id, exp_name in cases:
            cid, cname, _ = match_best_category(merchant, raw_text, self.SAMPLE_CATEGORIES)
            self.assertEqual(cid, exp_id, f"{merchant} için beklenen kategori ID {exp_id}, gelen {cid}")
            self.assertEqual(cname, exp_name)

    def test_api_scan_receipt_validation_and_offline_pdf(self):
        """POST /api/scan-receipt dosya doğrulaması, 5 MB sınırı ve çevrimdışı PDF ayrıştırmayı doğru yönetmeli."""
        import io
        from config import Config

        # 1. Dosya gönderilmeden istek -> 400
        res_empty = self.client.post('/api/scan-receipt', data={})
        self.assertEqual(res_empty.status_code, 400)
        self.assertFalse(res_empty.get_json()['success'])

        # 2. İzin verilmeyen uzantı -> 400
        res_bad_ext = self.client.post(
            '/api/scan-receipt',
            data={'receipt': (io.BytesIO(b'not allowed'), 'script.exe')},
            content_type='multipart/form-data',
        )
        self.assertEqual(res_bad_ext.status_code, 400)
        self.assertFalse(res_bad_ext.get_json()['success'])

        # 3. 5 MB üzeri dosya -> 413 JSON
        oversized = b'A' * (5 * 1024 * 1024 + 128)
        res_large = self.client.post(
            '/api/scan-receipt',
            data={'receipt': (io.BytesIO(oversized), 'big_receipt.jpg')},
            content_type='multipart/form-data',
        )
        self.assertEqual(res_large.status_code, 413)
        self.assertFalse(res_large.get_json()['success'])

        # 4. Çevrimdışı modda (GEMINI_API_KEY boşken) metin içeren basit PDF taraması -> 200 JSON
        orig_key = Config.GEMINI_API_KEY
        try:
            Config.GEMINI_API_KEY = ''
            simple_pdf = (
                b'%PDF-1.4\n'
                b'(MIGROS TICARET A.S.) Tj\n'
                b'(TARIH: 26.11.2026) Tj\n'
                b'(TOPKDV: 42,10 TL) Tj\n'
                b'(GENEL TOPLAM: 485,50 TL) Tj\n'
                b'%%EOF'
            )
            res_ok = self.client.post(
                '/api/scan-receipt',
                data={'receipt': (io.BytesIO(simple_pdf), 'migros_earsiv.pdf')},
                content_type='multipart/form-data',
            )
            self.assertEqual(res_ok.status_code, 200)
            payload = res_ok.get_json()
            self.assertTrue(payload['success'])
            self.assertEqual(payload['merchant'], 'Migros Ticaret A.Ş.')
            self.assertEqual(payload['amount'], 485.50)
            self.assertEqual(payload['date'], '2026-11-26')
            self.assertIn('KDV: 42.10 TL', payload['description'])
            self.assertIn('category_id', payload)
            self.assertIn('category_name', payload)
            self.assertIn('confidence', payload)
            self.assertEqual(payload['engine'], 'fallback')
        finally:
            Config.GEMINI_API_KEY = orig_key

    def test_api_scan_receipt_enforces_csrf_header(self):
        """CSRF aktifken /api/scan-receipt X-CSRF-Token olmadan 403 dönmeli, geçerli token ile kabul edilmeli."""
        import io
        from config import Config

        app.config['CSRF_ENABLED_IN_TESTS'] = True
        orig_key = Config.GEMINI_API_KEY
        try:
            Config.GEMINI_API_KEY = ''
            with self.client.session_transaction() as sess:
                sess['user_id'] = 1
                sess['username'] = 'efoss13'
                sess['_csrf_token'] = 'scan_test_csrf_token_123'

            # Token olmadan -> 403
            res_no_csrf = self.client.post(
                '/api/scan-receipt',
                data={'receipt': (io.BytesIO(b'\x89PNG\r\n\x1a\n'), 'migros_100tl.png')},
                content_type='multipart/form-data',
            )
            self.assertEqual(res_no_csrf.status_code, 403)

            # Geçerli X-CSRF-Token ile -> 200
            res_with_csrf = self.client.post(
                '/api/scan-receipt',
                headers={'X-CSRF-Token': 'scan_test_csrf_token_123'},
                data={'receipt': (io.BytesIO(b'\x89PNG\r\n\x1a\n'), 'migros_100tl.png')},
                content_type='multipart/form-data',
            )
            self.assertEqual(res_with_csrf.status_code, 200)
            self.assertTrue(res_with_csrf.get_json()['success'])
        finally:
            Config.GEMINI_API_KEY = orig_key

    @mock.patch('services.receipt_scanner.requests.post')
    def test_dual_endpoint_routing_and_model_fallback_with_transparent_error(self, mock_post):
        """Hem AQ. hem AIza anahtarları generativelanguage ucuna gitmeli; 404/503/429'da sıradaki aktif modele geçmeli."""
        import json
        from services.receipt_scanner import scan_receipt, _build_gemini_endpoint, GEMINI_FALLBACK_MODELS

        # 1. Endpoint yönlendirme kontrolü (hem AQ. hem AIza -> standart generativelanguage ucu)
        self.assertEqual(
            _build_gemini_endpoint('AQ.Ab8test_key', 'gemini-2.5-flash'),
            'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent',
        )
        self.assertEqual(
            _build_gemini_endpoint('AIzaSyFakeStudioKey123', 'gemini-2.5-flash'),
            'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent',
        )

        # 2. Çoklu model zinciri: primary 404 -> 1. yedek 503 -> 2. yedek 429 -> 3. yedek 200 OK
        resp_404 = mock.MagicMock()
        resp_404.status_code = 404
        resp_404.json.return_value = {'error': {'code': 404, 'message': 'Model gemini-2.5-flash not found'}}

        resp_503 = mock.MagicMock()
        resp_503.status_code = 503
        resp_503.json.return_value = {'error': {'code': 503, 'message': 'Model is currently experiencing high demand'}}

        resp_429 = mock.MagicMock()
        resp_429.status_code = 429
        resp_429.json.return_value = {'error': {'code': 429, 'message': 'Resource exhausted / rate limit exceeded'}}

        resp_200 = mock.MagicMock()
        resp_200.status_code = 200
        resp_200.raise_for_status.return_value = None
        resp_200.json.return_value = {
            'candidates': [{'content': {'parts': [{'text': json.dumps({
                'merchant': 'Garanti BBVA',
                'amount': 2500.0,
                'date': '2026-11-26',
                'category_id': 4,
                'category_name': 'Faturalar',
                'description': 'FAST Transfer Dekontu',
                'confidence': 0.94,
            })}]}}]
        }
        mock_post.side_effect = [resp_404, resp_503, resp_429, resp_200]

        res_retry = scan_receipt(
            file_bytes=b'%PDF-1.4\n%%EOF',
            filename='dekont.pdf',
            categories=self.SAMPLE_CATEGORIES,
            api_key='AQ.Ab8test_key',
            model='gemini-2.5-flash',
        )
        self.assertTrue(res_retry['success'])
        self.assertEqual(res_retry['engine'], 'gemini')
        self.assertEqual(mock_post.call_count, 4)
        self.assertIn('models/gemini-2.5-flash:generateContent', mock_post.call_args_list[0].args[0])
        self.assertIn(f'models/{GEMINI_FALLBACK_MODELS[0]}:generateContent', mock_post.call_args_list[1].args[0])
        self.assertIn(f'models/{GEMINI_FALLBACK_MODELS[1]}:generateContent', mock_post.call_args_list[2].args[0])
        self.assertIn(f'models/{GEMINI_FALLBACK_MODELS[2]}:generateContent', mock_post.call_args_list[3].args[0])

        # 3. Geçersiz API anahtarı (HTTP 400 API key not valid) ilk çağrıda durup warning'e şeffaf hata yazmalı
        resp_400 = mock.MagicMock()
        resp_400.status_code = 400
        resp_400.json.return_value = {'error': {'code': 400, 'message': 'API key not valid. Please pass a valid API key.'}}
        mock_post.reset_mock()
        mock_post.side_effect = [resp_400]

        res_fail = scan_receipt(
            file_bytes=b'\x89PNG\r\n\x1a\n',
            filename='migros_250tl.png',
            categories=self.SAMPLE_CATEGORIES,
            api_key='AIzaSyInvalidKey',
            model='gemini-2.5-flash',
        )
        self.assertTrue(res_fail['success'])
        self.assertEqual(res_fail['engine'], 'fallback')
        self.assertEqual(mock_post.call_count, 1)
        self.assertIn('HTTP 400', res_fail['warning'])
        self.assertIn('API key not valid', res_fail['warning'])
        self.assertIn('generativelanguage.googleapis.com', mock_post.call_args_list[0].args[0])

    def test_compressed_binary_pdf_sanitization_and_bank_receipt_keywords(self):
        """Sıkıştırılmış ikili PDF verisinde (FlateDecode/mojibake) bozuk karakter üretilmemeli ve banka dekontu kelimeleri eşleşmeli."""
        from services.receipt_scanner import scan_receipt, match_best_category

        compressed_pdf = (
            b'%PDF-1.4\n'
            b'1 0 obj << /Length 64 /Filter /FlateDecode >>\n'
            b'stream\n'
            b'(\x80\x81\x82\xf0\xf0\x10\x04\x05\xff\xfe\x1b\x07garbled_stream_bytes) Tj\n'
            b'endstream\n'
            b'%%EOF'
        )
        res = scan_receipt(
            file_bytes=compressed_pdf,
            filename='dekont.pdf',
            categories=self.SAMPLE_CATEGORIES,
            api_key='',
        )
        self.assertTrue(res['success'])
        self.assertEqual(res['engine'], 'fallback')
        self.assertEqual(res['merchant'], 'Banka Dekontu / Belge')
        self.assertNotIn('garbled', (res['merchant'] or '').lower())
        self.assertNotIn('garbled', (res['description'] or '').lower())
        # Kontrol karakteri içermemeli
        for field in (res['merchant'] or '', res['description'] or ''):
            self.assertFalse(any(ord(ch) < 32 for ch in field), f"Kontrol karakteri sızdı: {field!r}")

        # Banka dekontu kelimeleri kategori eşleştirici tarafından tanınmalı
        bank_cats = self.SAMPLE_CATEGORIES + [{'id': 7, 'name': 'Banka & Transfer', 'type': 'gider'}]
        for kw in ('Havale', 'EFT', 'FAST', 'Dekont', 'IBAN', 'Transfer', 'Komisyon', 'Alıcı'):
            cid, cname, bucket = match_best_category('', f'İşlem tipi: {kw} 500 TL', bank_cats)
            self.assertEqual(bucket, 'banka_dekont', f"'{kw}' kelimesi banka_dekont grubuna eşleşmedi.")
            self.assertEqual(cid, 7)
            self.assertEqual(cname, 'Banka & Transfer')


class TestPostgresSupabaseCompatibility(unittest.TestCase):
    """Supabase (PostgreSQL) uyumluluk katmanı, URL normalizasyonu ve T-SQL dönüşüm testleri."""

    def test_database_url_normalization_and_sslmode(self):
        """postgres:// şeması postgresql:// olmalı ve eksikse sslmode=require eklenmeli."""
        from config import Config

        self.assertEqual(Config.normalize_database_url(''), '')
        self.assertEqual(Config.normalize_database_url('   '), '')

        raw_supabase = 'postgres://postgres:secret@db.abc.supabase.co:5432/postgres'
        normalized = Config.normalize_database_url(raw_supabase)
        self.assertTrue(normalized.startswith('postgresql://'))
        self.assertIn('sslmode=require', normalized)

        with_query = 'postgresql://user:pw@host:6543/postgres?pgbouncer=true'
        normalized_q = Config.normalize_database_url(with_query)
        self.assertIn('pgbouncer=true', normalized_q)
        self.assertIn('&sslmode=require', normalized_q)

        explicit_ssl = 'postgresql://user:pw@localhost:5432/moneta?sslmode=disable'
        self.assertEqual(Config.normalize_database_url(explicit_ssl), explicit_ssl)

    def test_translate_tsql_to_postgres_core_constructs(self):
        """Parametre yer tutucuları, OUTPUT INSERTED.id, TOP N, tarih, boolean ve EXEC dönüşümleri doğru çalışmalı."""
        from db import translate_tsql_to_postgres

        # 1. OUTPUT INSERTED.id -> RETURNING id ve ? -> %s
        ins_sql = (
            "INSERT INTO transactions (user_id, amount, is_paid, description) "
            "OUTPUT INSERTED.id VALUES (?, ?, 0, 'Fatura?')"
        )
        pg_ins = translate_tsql_to_postgres(ins_sql, has_params=True)
        self.assertNotIn('OUTPUT INSERTED.id', pg_ins)
        self.assertTrue(pg_ins.endswith('RETURNING id'))
        self.assertIn("VALUES (%s, %s, FALSE, 'Fatura?')", pg_ins)

        # 2. SELECT TOP N -> SELECT ... LIMIT N
        top_sql = "SELECT TOP 50 id, amount FROM transactions WHERE user_id = ? ORDER BY transaction_date DESC"
        pg_top = translate_tsql_to_postgres(top_sql, has_params=True)
        self.assertNotIn('TOP 50', pg_top)
        self.assertTrue(pg_top.startswith('SELECT id, amount'))
        self.assertTrue(pg_top.endswith('LIMIT 50'))
        self.assertIn('user_id = %s', pg_top)

        # 3. GETDATE, CAST(GETDATE() AS DATE), DATEDIFF, MONTH, YEAR, ISNULL + boolean
        date_sql = (
            "SELECT DATEDIFF(day, CAST(GETDATE() AS DATE), due_date), "
            "ISNULL(is_paid, 0), MONTH(transaction_date), YEAR(transaction_date) "
            "FROM transactions WHERE created_at <= GETDATE() AND ISNULL(is_active, 1) = 1 AND description LIKE '%kira%'"
        )
        pg_date = translate_tsql_to_postgres(date_sql, has_params=True)
        self.assertIn('((due_date)::date - (CURRENT_DATE)::date)', pg_date)
        self.assertIn('COALESCE(is_paid, FALSE)', pg_date)
        self.assertIn('EXTRACT(MONTH FROM transaction_date)::INT', pg_date)
        self.assertIn('EXTRACT(YEAR FROM transaction_date)::INT', pg_date)
        self.assertIn('CURRENT_TIMESTAMP', pg_date)
        self.assertIn('COALESCE(is_active, TRUE) = TRUE', pg_date)
        self.assertIn("LIKE '%%kira%%'", pg_date)

        # 4. EXEC sp_get_user_budget_health -> SELECT * FROM sp_get_user_budget_health(%s, %s, %s)
        exec_sql = "EXEC sp_get_user_budget_health @user_id = ?, @month = ?, @year = ?"
        pg_exec = translate_tsql_to_postgres(exec_sql, has_params=True)
        self.assertEqual(pg_exec, "SELECT * FROM sp_get_user_budget_health(%s, %s, %s)")

    def test_adapt_params_for_pg_boolean_columns(self):
        """INSERT ve UPDATE sorgularında boolean sütunlara giden 0/1 int parametreleri bool tipine çevrilmeli."""
        from db import _adapt_params_for_pg

        ins_sql = (
            "INSERT INTO transactions (user_id, amount, is_paid, is_active) "
            "OUTPUT INSERTED.id VALUES (?, ?, ?, ?)"
        )
        adapted_ins = _adapt_params_for_pg(ins_sql, (1, 250.0, 0, 1))
        self.assertEqual(adapted_ins, (1, 250.0, False, True))
        self.assertIs(adapted_ins[2], False)
        self.assertIs(adapted_ins[3], True)

        upd_sql = "UPDATE recurring_transactions SET amount = ?, auto_apply = ?, is_active = ? WHERE id = ?"
        adapted_upd = _adapt_params_for_pg(upd_sql, (1500.0, 1, 0, 42))
        self.assertEqual(adapted_upd, (1500.0, True, False, 42))
        self.assertIs(adapted_upd[1], True)
        self.assertIs(adapted_upd[2], False)
        self.assertEqual(adapted_upd[3], 42)

    def test_get_db_connection_routes_to_postgres_wrapper(self):
        """DATABASE_URL tanımlıysa get_db_connection psycopg2 üzerinden PgConnectionWrapper döndürmeli."""
        import db
        from config import Config

        mock_pg_cursor = mock.MagicMock()
        mock_pg_cursor.fetchone.return_value = (99,)
        mock_pg_conn = mock.MagicMock()
        mock_pg_conn.cursor.return_value = mock_pg_cursor

        fake_psycopg2 = mock.MagicMock()
        fake_psycopg2.connect.return_value = mock_pg_conn

        orig_url = Config.DATABASE_URL
        orig_psycopg2 = db.psycopg2
        try:
            Config.DATABASE_URL = 'postgres://postgres:pass@db.example.supabase.co:5432/postgres'
            db.psycopg2 = fake_psycopg2

            self.assertTrue(db.is_postgres())
            conn = db.get_db_connection(autocommit=True)
            self.assertIsInstance(conn, db.PgConnectionWrapper)
            fake_psycopg2.connect.assert_called_once_with(
                'postgresql://postgres:pass@db.example.supabase.co:5432/postgres?sslmode=require',
                connect_timeout=10,
            )
            self.assertTrue(mock_pg_conn.autocommit)

            cur = conn.cursor()
            cur.execute(
                "INSERT INTO categories (name, type, is_active) OUTPUT INSERTED.id VALUES (?, ?, ?)",
                ('Test', 'gider', 1),
            )
            row = cur.fetchone()
            self.assertEqual(row, (99,))
            executed_sql, executed_params = mock_pg_cursor.execute.call_args.args
            self.assertIn('RETURNING id', executed_sql)
            self.assertIn('VALUES (%s, %s, %s)', executed_sql)
            self.assertEqual(executed_params, ('Test', 'gider', True))
            self.assertIs(executed_params[2], True)
        finally:
            Config.DATABASE_URL = orig_url
            db.psycopg2 = orig_psycopg2

    def test_schema_postgres_sql_contains_all_required_objects(self):
        """schema_postgres.sql tüm tabloları, görünümü ve bütçe sağlığı fonksiyonunu PostgreSQL sözdizimiyle içermeli."""
        schema_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'schema_postgres.sql')
        self.assertTrue(os.path.exists(schema_path), 'schema_postgres.sql bulunamadı.')

        with open(schema_path, 'r', encoding='utf-8') as f:
            content = f.read()

        required_tokens = [
            'CREATE TABLE IF NOT EXISTS users',
            'CREATE TABLE IF NOT EXISTS categories',
            'CREATE TABLE IF NOT EXISTS transactions',
            'CREATE TABLE IF NOT EXISTS portfolio_assets',
            'CREATE TABLE IF NOT EXISTS asset_transactions',
            'CREATE TABLE IF NOT EXISTS savings_goals',
            'CREATE TABLE IF NOT EXISTS subscriptions',
            'CREATE TABLE IF NOT EXISTS recurring_transactions',
            'CREATE OR REPLACE VIEW vw_user_financial_summary',
            'CREATE OR REPLACE FUNCTION sp_get_user_budget_health',
            'SERIAL PRIMARY KEY',
            'BOOLEAN',
            'TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP',
        ]
        for token in required_tokens:
            self.assertIn(token, content, f"schema_postgres.sql içinde eksik ifade: {token}")


class TestVercelServerlessReadOnlyCompatibility(unittest.TestCase):
    """Faz 2: Vercel Serverless, salt-okunur dosya sistemi ve production güvenlik testleri."""

    def test_vercel_env_routes_upload_folder_to_tmp(self):
        """VERCEL=1 veya VERCEL_ENV tanımlıysa upload dizini /tmp/moneta_uploads olmalı ve OSError'da çökmemeli."""
        from config import Config

        with mock.patch.dict(os.environ, {'VERCEL': '1', 'VERCEL_ENV': ''}, clear=False):
            with mock.patch('config.os.makedirs', side_effect=OSError('Read-only file system')):
                self.assertTrue(Config.is_vercel())
                resolved = Config.resolve_upload_folder('/var/task')
                self.assertEqual(resolved, '/tmp/moneta_uploads')

        with mock.patch.dict(os.environ, {'VERCEL': '', 'VERCEL_ENV': 'preview'}, clear=False):
            with mock.patch('config.os.makedirs', return_value=None):
                self.assertTrue(Config.is_vercel())
                self.assertEqual(Config.resolve_upload_folder('/var/task'), '/tmp/moneta_uploads')

    def test_readonly_filesystem_fallback_to_tmp(self):
        """Yerel kök dizin salt-okunur olduğunda (OSError) uygulama çökmeden /tmp/moneta_uploads dizinine düşmeli."""
        from config import Config

        with mock.patch.dict(os.environ, {'VERCEL': '', 'VERCEL_ENV': ''}, clear=False):
            with mock.patch('config.os.makedirs', side_effect=PermissionError('Read-only root')):
                resolved = Config.resolve_upload_folder('/readonly_root')
                self.assertEqual(resolved, '/tmp/moneta_uploads')

    def test_production_env_disables_auto_login_and_reads_secret_key(self):
        """VERCEL_ENV=production veya FLASK_ENV=production durumunda ALLOW_AUTO_LOGIN kapalı olmalı ve SECRET_KEY env'den okunmalı."""
        from config import Config
        from app import _is_auto_login_allowed

        orig_debug = app.debug
        orig_testing = app.config.get('TESTING')
        try:
            app.debug = False
            app.config['TESTING'] = False

            with mock.patch.dict(
                os.environ,
                {
                    'VERCEL_ENV': 'production',
                    'FLASK_ENV': 'development',
                    'ALLOW_AUTO_LOGIN': '1',
                    'SECRET_KEY': 'vercel_prod_super_secret_key_xyz',
                },
                clear=False,
            ):
                self.assertTrue(Config.is_production())
                self.assertFalse(Config.is_auto_login_enabled())
                self.assertFalse(_is_auto_login_allowed())
                self.assertEqual(Config.get_secret_key(), 'vercel_prod_super_secret_key_xyz')

            with mock.patch.dict(
                os.environ,
                {
                    'VERCEL_ENV': '',
                    'FLASK_ENV': 'production',
                    'ALLOW_AUTO_LOGIN': 'true',
                },
                clear=False,
            ):
                self.assertTrue(Config.is_production())
                self.assertFalse(Config.is_auto_login_enabled())
                self.assertFalse(_is_auto_login_allowed())
        finally:
            app.debug = orig_debug
            app.config['TESTING'] = orig_testing

    def test_save_receipt_survives_readonly_disk_and_missing_file_returns_404(self):
        """Salt-okunur diskte fiş yazılamasa bile dosya adı dönmeli ve eksik fiziksel dosya isteği 500 yerine 404 dönmeli."""
        import io
        from werkzeug.datastructures import FileStorage
        from app import _save_receipt_if_valid

        dummy_file = FileStorage(
            stream=io.BytesIO(b'\x89PNG\r\n\x1a\nfake_png_bytes'),
            filename='market_fisi.png',
            content_type='image/png',
        )
        with mock.patch('app.os.makedirs', side_effect=OSError('Read-only file system')):
            filename, err = _save_receipt_if_valid(dummy_file)
        self.assertIsNone(err)
        self.assertIsNotNone(filename)
        self.assertTrue(filename.endswith('_market_fisi.png'))

        # DB'de kayıtlı ama fiziksel dosyası olmayan fiş istendiğinde 500 yerine temiz 404 dönmeli
        app.config['TESTING'] = True
        app.config['CSRF_ENABLED_IN_TESTS'] = False
        client = app.test_client()
        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['username'] = 'efoss13'

        conn = get_db_connection()
        cursor = conn.cursor()
        tx_id = None
        try:
            cursor.execute("SELECT TOP 1 id FROM categories WHERE user_id = 1 AND type = 'gider'")
            cat_row = cursor.fetchone()
            self.assertIsNotNone(cat_row)
            cursor.execute(
                """
                INSERT INTO transactions (user_id, category_id, amount, transaction_date, description, payment_method, is_paid, receipt_path)
                OUTPUT INSERTED.id
                VALUES (1, ?, 12.34, '2026-11-26', 'Vercel Missing File Test', 'nakit', 1, ?)
                """,
                (cat_row[0], filename),
            )
            tx_id = cursor.fetchone()[0]
            conn.commit()

            res = client.get(f'/receipts/{filename}')
            self.assertEqual(res.status_code, 404)
        finally:
            if tx_id is not None:
                cursor.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
                conn.commit()
            cursor.close()
            conn.close()

    def test_scan_receipt_runs_strictly_in_memory_without_disk_writes(self):
        """/api/scan-receipt salt-okunur ortamda diske hiçbir şey yazmadan tamamen bellek içi çalışmalı."""
        import io
        from config import Config

        app.config['TESTING'] = True
        app.config['CSRF_ENABLED_IN_TESTS'] = False
        client = app.test_client()
        with client.session_transaction() as sess:
            sess['user_id'] = 1
            sess['username'] = 'efoss13'

        orig_key = Config.GEMINI_API_KEY
        try:
            Config.GEMINI_API_KEY = ''
            simple_pdf = (
                b'%PDF-1.4\n'
                b'(A101 YENI MAGAZACILIK) Tj\n'
                b'(TARIH: 26.11.2026) Tj\n'
                b'(TOPLAM: 199,90 TL) Tj\n'
                b'%%EOF'
            )
            with mock.patch('app.os.makedirs', side_effect=PermissionError('Read-only FS')):
                res = client.post(
                    '/api/scan-receipt',
                    data={'receipt': (io.BytesIO(simple_pdf), 'a101_fis.pdf')},
                    content_type='multipart/form-data',
                )
            self.assertEqual(res.status_code, 200)
            payload = res.get_json()
            self.assertTrue(payload['success'])
            self.assertEqual(payload['amount'], 199.90)
        finally:
            Config.GEMINI_API_KEY = orig_key

    def test_vercel_entrypoint_and_deployment_config_valid(self):
        """api/index.py sorunsuz import edilmeli; vercel.json, .vercelignore ve requirements.txt eksiksiz olmalı."""
        import importlib.util
        import json

        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        # 1. api/index.py import ve Flask app doğrulaması
        entry_path = os.path.join(root_dir, 'api', 'index.py')
        self.assertTrue(os.path.isfile(entry_path), 'api/index.py bulunamadı.')
        spec = importlib.util.spec_from_file_location('vercel_api_index', entry_path)
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(hasattr(module, 'app'))
        self.assertIs(module.app, app)

        # 2. vercel.json geçerlilik ve v2 routes yönlendirme doğrulaması
        vercel_json_path = os.path.join(root_dir, 'vercel.json')
        self.assertTrue(os.path.isfile(vercel_json_path), 'vercel.json bulunamadı.')
        with open(vercel_json_path, 'r', encoding='utf-8') as f:
            vcfg = json.load(f)
        self.assertEqual(vcfg.get('version'), 2)
        self.assertIn('routes', vcfg)
        self.assertTrue(
            any(r.get('src') == '/static/(.*)' and r.get('dest') == '/static/$1' for r in vcfg['routes']),
            'vercel.json routes içinde /static/(.*) kuralı eksik.',
        )
        self.assertTrue(
            any(r.get('src') == '/(.*)' and r.get('dest') == '/api/index.py' for r in vcfg['routes']),
            'vercel.json routes içinde /api/index.py hedefi eksik.',
        )

        # 3. .vercelignore ve requirements.txt doğrulaması
        ignore_path = os.path.join(root_dir, '.vercelignore')
        self.assertTrue(os.path.isfile(ignore_path), '.vercelignore bulunamadı.')
        with open(ignore_path, 'r', encoding='utf-8') as f:
            ignore_text = f.read()
        for entry in ('venv/', '__pycache__/', '.git/', 'uploads/', 'tests/', '.env'):
            self.assertIn(entry, ignore_text)

        req_path = os.path.join(root_dir, 'requirements.txt')
        with open(req_path, 'r', encoding='utf-8') as f:
            req_text = f.read().lower()
        for pkg in ('flask', 'psycopg2-binary', 'python-dotenv', 'pypdf', 'werkzeug', 'requests'):
            self.assertIn(pkg, req_text, f"requirements.txt içinde eksik paket: {pkg}")

    def test_api_index_cold_start_with_readonly_home(self):
        """Salt-okunur HOME simülasyonu ile api/index.py cold-start import edildiğinde önbellekler /tmp altına yönlenmeli."""
        import importlib.util
        from config import Config

        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        entry_path = os.path.join(root_dir, 'api', 'index.py')

        # Tırnak ve boşluk içeren DATABASE_URL temizlenmeli
        quoted_url = '  "postgres://user:pw@db.example.supabase.co:5432/postgres"  '
        self.assertEqual(
            Config.normalize_database_url(quoted_url),
            'postgresql://user:pw@db.example.supabase.co:5432/postgres?sslmode=require',
        )

        with mock.patch.dict(
            os.environ,
            {
                'VERCEL': '1',
                'HOME': '/home/sbx_readonly_user',
                'XDG_CACHE_HOME': '/home/sbx_readonly_user/.cache',
                'TMPDIR': '/home/sbx_readonly_user/tmp',
            },
            clear=False,
        ):
            with mock.patch('os.access', return_value=False):
                with mock.patch('os.makedirs', side_effect=OSError('Read-only /tmp test guard')):
                    spec = importlib.util.spec_from_file_location('vercel_cold_start_index', entry_path)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)

                    self.assertEqual(os.environ['HOME'], '/tmp')
                    self.assertEqual(os.environ['XDG_CACHE_HOME'], '/tmp/.cache')
                    self.assertEqual(os.environ['TMPDIR'], '/tmp')
                    self.assertIs(module.app, app)

    def test_api_health_endpoint_ok_and_sanitized_error(self):
        """/api/health sağlıklı durumda 200 ok, DB hatasında ise şifre sızdırmadan 503 degraded dönmeli."""
        app.config['TESTING'] = True
        client = app.test_client()

        # 1. Normal bağlantı -> 200 OK
        res_ok = client.get('/api/health')
        self.assertEqual(res_ok.status_code, 200)
        data_ok = res_ok.get_json()
        self.assertEqual(data_ok['status'], 'ok')
        self.assertIn(data_ok['db_engine'], ('postgres', 'mssql'))
        self.assertTrue(data_ok['db_connected'])
        self.assertIsNone(data_ok['db_error'])

        # 2. DB bağlantı hatası -> 503 Degraded ve kimlik bilgileri maskelenmiş
        with mock.patch(
            'app.get_db_connection',
            side_effect=RuntimeError('Connection failed: postgresql://admin:SuperSecret123@db.host:5432/postgres password=TopSecretPwd'),
        ):
            res_err = client.get('/api/health')
            self.assertEqual(res_err.status_code, 503)
            data_err = res_err.get_json()
            self.assertEqual(data_err['status'], 'degraded')
            self.assertFalse(data_err['db_connected'])
            self.assertIsNotNone(data_err['db_error'])
            self.assertNotIn('SuperSecret123', data_err['db_error'])
            self.assertNotIn('TopSecretPwd', data_err['db_error'])

    def test_vercel_path_restore_middleware_preserves_route_and_query(self):
        """PATH_INFO='/api/index.py' ve __vercel_path query parametresiyle gerçek rota ve query parametreleri korunmalı."""
        import importlib.util
        from urllib.parse import parse_qs

        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        entry_path = os.path.join(root_dir, 'api', 'index.py')
        spec = importlib.util.spec_from_file_location('vercel_mw_index', entry_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        self.assertTrue(hasattr(module, 'handler'))
        self.assertIs(module.handler, module.app)

        # 1. İzole WSGI middleware birim testi
        captured = {}

        def fake_downstream(environ, start_response):
            captured['PATH_INFO'] = environ.get('PATH_INFO')
            captured['QUERY_STRING'] = environ.get('QUERY_STRING')
            captured['SCRIPT_NAME'] = environ.get('SCRIPT_NAME')
            start_response('200 OK', [('Content-Type', 'text/plain')])
            return [b'ok']

        mw = module.VercelPathRestoreMiddleware(fake_downstream)

        # a) __vercel_path + ek query parametresi
        env1 = {
            'PATH_INFO': '/api/index.py',
            'SCRIPT_NAME': '/api/index.py',
            'QUERY_STRING': '__vercel_path=/login&next=/dashboard',
        }
        mw(env1, lambda status, headers: None)
        self.assertEqual(captured['PATH_INFO'], '/login')
        self.assertEqual(captured['SCRIPT_NAME'], '')
        parsed_q = parse_qs(captured['QUERY_STRING'])
        self.assertNotIn('__vercel_path', parsed_q)
        self.assertEqual(parsed_q.get('next'), ['/dashboard'])

        # b) Başlık yedeği (HTTP_X_VERCEL_ORIGINAL_URL)
        env2 = {
            'PATH_INFO': '/api/index.py',
            'QUERY_STRING': '',
            'HTTP_X_VERCEL_ORIGINAL_URL': '/health?probe=1',
        }
        mw(env2, lambda status, headers: None)
        self.assertEqual(captured['PATH_INFO'], '/health')
        self.assertEqual(parse_qs(captured['QUERY_STRING']).get('probe'), ['1'])

        # c) Parametresiz /api/index -> '/'
        env3 = {'PATH_INFO': '/api/index', 'QUERY_STRING': ''}
        mw(env3, lambda status, headers: None)
        self.assertEqual(captured['PATH_INFO'], '/')

        # 2. Uçtan uca Flask test client doğrulaması (/login ve /health)
        module.app.config['TESTING'] = True
        client = module.app.test_client()
        res_login = client.get('/api/index.py?__vercel_path=/login&next=/dashboard')
        self.assertEqual(res_login.status_code, 200)

        res_health = client.get('/api/index.py?__vercel_path=/health')
        self.assertEqual(res_health.status_code, 200)
        self.assertEqual(res_health.get_json()['status'], 'ok')

        # 3. 404.html şablonu url_for('index') kullanmalı
        tpl_404 = os.path.join(root_dir, 'templates', '404.html')
        with open(tpl_404, 'r', encoding='utf-8') as f:
            self.assertIn("url_for('index')", f.read())

    def test_direct_app_wsgi_restores_path_from_vercel_headers_and_full_url(self):
        """Doğrudan app.py üzerinden yüklenen app, Vercel başlıkları ve tam URL değerlerinden gerçek PATH_INFO'yu kurtarmalı."""
        from app import app as direct_app, VercelPathRestoreMiddleware

        self.assertIsInstance(direct_app.wsgi_app, VercelPathRestoreMiddleware)
        direct_app.config['TESTING'] = True
        client = direct_app.test_client()

        # 1. PATH_INFO='/api/index.py' + HTTP_X_VERCEL_ORIGINAL_URL='/register' -> 200 OK
        res_register = client.get(
            '/api/index.py',
            environ_base={'HTTP_X_VERCEL_ORIGINAL_URL': '/register'},
        )
        self.assertEqual(res_register.status_code, 200)

        # 2. PATH_INFO='/api/index.py' + tam URL içeren HTTP_X_FORWARDED_URI -> /login (200 OK)
        res_login = client.get(
            '/api/index.py',
            environ_base={'HTTP_X_FORWARDED_URI': 'https://moneta.vercel.app/login'},
        )
        self.assertEqual(res_login.status_code, 200)

        # 3. PATH_INFO='/api/index.py' + HTTP_X_INVOKE_PATH='/health' -> 200 OK
        res_health = client.get(
            '/api/index.py',
            environ_base={'HTTP_X_INVOKE_PATH': '/health'},
        )
        self.assertEqual(res_health.status_code, 200)
        self.assertEqual(res_health.get_json()['status'], 'ok')

        # 4. PATH_INFO='/api/index.py' + başlıksız -> '/' (girişsiz kullanıcıyı /login'e 302 yönlendirir, 404 dönmez)
        res_root = client.get('/api/index.py', follow_redirects=False)
        self.assertEqual(res_root.status_code, 302)
        self.assertIn('/login', res_root.headers.get('Location', ''))


if __name__ == '__main__':
    unittest.main()
