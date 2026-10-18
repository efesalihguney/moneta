"""
Moneta Financial SaaS - Otomatik Test Paketi (Unit & Integration Tests)
Çalıştırma:
  pytest tests/test_moneta.py
  veya
  python -m unittest tests/test_moneta.py
"""

import unittest
import sys
import os

# Proje ana dizinini path'e ekle
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from config import Config
from db import get_db_connection
from services.finance_engine import (
    calculate_fisher_real_return,
    evaluate_credit_utilization,
    analyze_portfolio_concentration,
    calculate_wac_and_pnl,
    calculate_realized_pnl
)
from app import app


class TestFisherEquation(unittest.TestCase):
    """Test 1: Fisher Denklemi ile Enflasyona Karşı Reel Getiri Hesaplama Testleri"""

    def test_positive_real_return_real_alpha(self):
        """Nominal %30, TÜFE %19.5 iken pozitif reel alfa üretilmeli."""
        res = calculate_fisher_real_return(nominal_pct=30.0, inflation_pct=19.5)
        self.assertGreater(res['real_return'], 0.0)
        self.assertEqual(res['real_return'], 8.79)
        self.assertEqual(res['status_label'], "Alım Gücü Korundu (Reel Alfa)")
        self.assertIn("bg-success-subtle", res['badge_class'])

    def test_inflationary_erosion_nominal_gain_real_loss(self):
        """Nominal %10 kâr varken TÜFE %19.5 ise enflasyonist erime tespit edilmeli."""
        res = calculate_fisher_real_return(nominal_pct=10.0, inflation_pct=19.5)
        self.assertLess(res['real_return'], 0.0)
        self.assertEqual(res['real_return'], -7.95)
        self.assertEqual(res['status_label'], "Enflasyonist Erime (Nominal Kâr, Reel Kayıp)")
        self.assertIn("bg-warning-subtle", res['badge_class'])

    def test_real_value_loss_both_negative(self):
        """Nominal kayıp ve enflasyon altında reel değer kaybı testi."""
        res = calculate_fisher_real_return(nominal_pct=-5.0, inflation_pct=19.5)
        self.assertLess(res['real_return'], 0.0)
        self.assertEqual(res['status_label'], "Reel Değer Kaybı")
        self.assertIn("bg-danger-subtle", res['badge_class'])


class TestCreditUtilization(unittest.TestCase):
    """Test 2: Kredi Kartı Limit Doluluk Oranı ve Findeks/FICO Risk Kademeleri Testleri"""

    def test_ideal_utilization_tier(self):
        """50.000 TL limit, 10.000 TL borç (%20) -> İdeal Kullanım (<=%30)"""
        res = evaluate_credit_utilization(unpaid_debt=10000.0, card_limit=50000.0)
        self.assertEqual(res['cc_utilization_pct'], 20.0)
        self.assertEqual(res['available_card_limit'], 40000.0)
        self.assertEqual(res['cc_risk_level'], "ideal")
        self.assertIn("İdeal Kullanım", res['cc_risk_status'])
        self.assertEqual(res['cc_risk_bar_class'], "bg-success")

    def test_moderate_utilization_tier(self):
        """50.000 TL limit, 25.000 TL borç (%50) -> Dengeli Kullanım (%30 - %70)"""
        res = evaluate_credit_utilization(unpaid_debt=25000.0, card_limit=50000.0)
        self.assertEqual(res['cc_utilization_pct'], 50.0)
        self.assertEqual(res['available_card_limit'], 25000.0)
        self.assertEqual(res['cc_risk_level'], "moderate")
        self.assertIn("Dengeli Kullanım", res['cc_risk_status'])
        self.assertEqual(res['cc_risk_bar_class'], "bg-warning")

    def test_high_risk_utilization_tier(self):
        """50.000 TL limit, 40.000 TL borç (%80) -> Yüksek Limit Riski (>%70)"""
        res = evaluate_credit_utilization(unpaid_debt=40000.0, card_limit=50000.0)
        self.assertEqual(res['cc_utilization_pct'], 80.0)
        self.assertEqual(res['available_card_limit'], 10000.0)
        self.assertEqual(res['cc_risk_level'], "high")
        self.assertIn("Yüksek Limit Riski", res['cc_risk_status'])
        self.assertEqual(res['cc_risk_bar_class'], "bg-danger")

    def test_over_limit_safety(self):
        """Borcun limiti aştığı durumda kullanılabilir limit 0 altına inmemeli."""
        res = evaluate_credit_utilization(unpaid_debt=60000.0, card_limit=50000.0)
        self.assertEqual(res['available_card_limit'], 0.0)
        self.assertEqual(res['cc_utilization_pct'], 120.0)
        self.assertEqual(res['cc_risk_level'], "high")


class TestPortfolioConcentration(unittest.TestCase):
    """Test 3: Portföy Konsantrasyonu ve Çeşitlendirme Skoru Testleri"""

    def test_high_concentration_single_asset_96_percent(self):
        """Tek varlık %96 ağırlıktayken Yüksek Konsantrasyon Riski uyarısı üretilmeli."""
        symbols = [
            {'name': 'THYAO', 'pct': 96.0, 'value': 96000.0},
            {'name': 'GARAN', 'pct': 4.0, 'value': 4000.0}
        ]
        types = [{'name': 'BIST Hisseleri', 'pct': 100.0, 'value': 100000.0}]
        res = analyze_portfolio_concentration(allocations_by_symbol=symbols, allocations_by_type=types)
        
        self.assertEqual(res['status'], "Yüksek Konsantrasyon Riski")
        self.assertEqual(res['level'], "high_risk")
        self.assertEqual(res['top_symbol'], "THYAO")
        self.assertEqual(res['max_weight'], 96.0)
        self.assertIn("bg-danger-subtle", res['badge_class'])

    def test_balanced_diversification(self):
        """En az 2 sınıf ve tek varlık <= %50 iken Dengeli Çeşitlendirme üretilmeli."""
        symbols = [
            {'name': 'THYAO', 'pct': 40.0, 'value': 40000.0},
            {'name': 'NVDA', 'pct': 30.0, 'value': 30000.0},
            {'name': 'ALTIN', 'pct': 30.0, 'value': 30000.0}
        ]
        types = [
            {'name': 'BIST Hisseleri', 'pct': 40.0, 'value': 40000.0},
            {'name': 'ABD Borsası', 'pct': 30.0, 'value': 30000.0},
            {'name': 'Altın & Emtia', 'pct': 30.0, 'value': 30000.0}
        ]
        res = analyze_portfolio_concentration(allocations_by_symbol=symbols, allocations_by_type=types)
        self.assertEqual(res['status'], "Dengeli Çeşitlendirme")
        self.assertEqual(res['level'], "balanced")
        self.assertIn("bg-success-subtle", res['badge_class'])

    def test_empty_portfolio(self):
        """Varlık bulunmadığında zarifçe Varlık Yok durumu dönmeli."""
        res = analyze_portfolio_concentration(allocations_by_symbol=[], allocations_by_type=[])
        self.assertEqual(res['status'], "Varlık Yok")
        self.assertEqual(res['level'], "empty")


class TestWacAndPnlCalculations(unittest.TestCase):
    """Ağırlıklı Ortalama Maliyet (WAC) ve Realize Kâr/Zarar Testleri"""

    def test_wac_calculation(self):
        """10 adet 100 TL maliyetli varlığa 10 adet 200 TL eklenirse yeni maliyet 150 TL olmalı."""
        res = calculate_wac_and_pnl(cur_qty=10.0, cur_avg_cost=100.0, new_qty=10.0, new_unit_price=200.0)
        self.assertEqual(res['total_quantity'], 20.0)
        self.assertEqual(res['average_cost'], 150.0)
        self.assertEqual(res['total_cost'], 3000.0)

    def test_realized_pnl_calculation(self):
        """150 TL maliyetli 5 adet varlık 200 TL'ye satılırsa realize pnl +250 TL olmalı."""
        pnl = calculate_realized_pnl(cur_avg_cost=150.0, sell_qty=5.0, sell_unit_price=200.0)
        self.assertEqual(pnl, 250.0)


class TestMssqlDatabaseObjects(unittest.TestCase):
    """Test 4: MSSQL View ve Stored Procedure Entegrasyon Testleri"""

    def test_vw_user_financial_summary_execution(self):
        """vw_user_financial_summary View nesnesi sorgulanabilmeli ve beklenen sütunları içermeli."""
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT TOP 5 * FROM vw_user_financial_summary")
            rows = cursor.fetchall()
            col_names = [desc[0] for desc in cursor.description]

            expected_cols = [
                'user_id', 'username', 'initial_cash_balance', 'credit_card_limit',
                'total_income', 'total_cash_expense', 'unpaid_cc_debt',
                'total_savings', 'active_asset_count'
            ]
            for col in expected_cols:
                self.assertIn(col, col_names, f"View içinde '{col}' sütunu bulunamadı.")
        finally:
            cursor.close()
            conn.close()

    def test_sp_get_user_budget_health_execution(self):
        """sp_get_user_budget_health Stored Procedure nesnesi hatasız çalışmalı."""
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("EXEC sp_get_user_budget_health @user_id = 1, @month = 10, @year = 2026")
            rows = cursor.fetchall()
            col_names = [desc[0] for desc in cursor.description]

            expected_cols = [
                'category_id', 'category_name', 'monthly_budget',
                'total_spent', 'remaining_budget', 'budget_usage_pct', 'budget_status'
            ]
            for col in expected_cols:
                self.assertIn(col, col_names, f"SP çıktısında '{col}' sütunu bulunamadı.")
        finally:
            cursor.close()
            conn.close()


class TestFlaskSecurityAndRoutes(unittest.TestCase):
    """Test 5: Flask Route Güvenliği ve Korumalı Uç Nokta Testleri"""

    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()

    def test_anonymous_dashboard_redirects_to_login(self):
        """Giriş yapmamış kullanıcı /dashboard istediğinde 302 ile login sayfasına yönlendirilmeli."""
        response = self.client.get('/dashboard', follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.headers.get('Location', ''))

    def test_anonymous_market_summary_redirects_to_login(self):
        """Giriş yapmamış kullanıcı /api/market-summary istediğinde 302 ile login sayfasına yönlendirilmeli."""
        response = self.client.get('/api/market-summary', follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.headers.get('Location', ''))

    def test_anonymous_db_diagnostics_redirects_to_login(self):
        """Giriş yapmamış kullanıcı /api/db-diagnostics istediğinde 302 ile login sayfasına yönlendirilmeli."""
        response = self.client.get('/api/db-diagnostics', follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.headers.get('Location', ''))

    def test_login_page_renders_successfully(self):
        """Login sayfası GET isteğine 200 OK ile dönmeli."""
        response = self.client.get('/login')
        self.assertEqual(response.status_code, 200)


if __name__ == '__main__':
    unittest.main()

