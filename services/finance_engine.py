"""
Moneta Financial Engine & Analytical Service
Bu modül saf finansal hesaplama algoritmalarını (Fisher Denklemi, Kredi Kartı Limit Doluluğu,
Portföy Konsantrasyon Analizi ve WAC/PnL) içerir.
"""

import math
import re
from typing import List, Dict, Any, Optional, Tuple, Union


def parse_safe_amount(
    raw_val: Any, 
    min_val: float = 0.01, 
    max_val: float = 999_999_999_999.99
) -> Optional[float]:
    """
    Kullanıcı girdilerini güvenli bir sayıya (float) dönüştürür.
    - Hem standart (1250.50) hem Türkçe (1.250,50 veya 1250,50) formatlarını destekler.
    - Boş, geçersiz metin, negatif, NaN, inf ve DECIMAL(18,2) sınırını aşan değerleri reddeder.
    
    :param raw_val: Ham girdi (str, int, float)
    :param min_val: İzin verilen en küçük tutar (varsayılan: 0.01)
    :param max_val: İzin verilen en büyük tutar (varsayılan: 999.999.999.999,99)
    :return: Geçerli float veya geçersiz/taşma durumunda None
    """
    if raw_val is None:
        return None
    if isinstance(raw_val, (int, float)):
        if math.isnan(raw_val) or math.isinf(raw_val):
            return None
        val = float(raw_val)
    elif isinstance(raw_val, str):
        cleaned = raw_val.strip()
        if not cleaned:
            return None
        if cleaned.lower() in ('nan', 'inf', '-inf', '+inf'):
            return None
        # Para birimi sembollerini, boşlukları ve harfleri ayıkla
        cleaned = re.sub(r'[₺$€\s\xa0]|TL|tl', '', cleaned)
        if not cleaned:
            return None
        
        # Nokta ve virgül ayrımı
        if '.' in cleaned and ',' in cleaned:
            # 1.250,50 vs 1,250.50
            if cleaned.rfind(',') > cleaned.rfind('.'):
                cleaned = cleaned.replace('.', '').replace(',', '.')
            else:
                cleaned = cleaned.replace(',', '')
        elif ',' in cleaned:
            # 1250,50
            cleaned = cleaned.replace(',', '.')
        elif cleaned.count('.') > 1:
            # 1.250.000
            cleaned = cleaned.replace('.', '')
            
        try:
            val = float(cleaned)
        except (ValueError, TypeError):
            return None
    else:
        try:
            val = float(raw_val)
        except Exception:
            return None

    if math.isnan(val) or math.isinf(val):
        return None
    if val < min_val or val > max_val:
        return None
    precision = 4 if (0 < min_val < 0.01) else 2
    return round(val, precision)


parse_safe_decimal = parse_safe_amount


def calculate_fisher_real_return(nominal_pct: float, inflation_pct: float) -> Dict[str, Any]:
    """
    Fisher Denklemi ile enflasyona karşı reel getiriyi hesaplar.
    Formül: [(1 + Nominal / 100) / (1 + Enflasyon / 100) - 1] * 100
    
    :param nominal_pct: Portföyün veya varlığın nominal yüzde getirisi (örn: 30.0)
    :param inflation_pct: İlgili dönemdeki TÜFE enflasyon oranı (örn: 19.5)
    :return: Reel getiri ve durum göstergelerini içeren sözlük
    """
    nom = float(nominal_pct)
    inf = float(inflation_pct)
    
    # Fisher formülü: (1 + r) = (1 + R) / (1 + i)
    real_ret = round(((1.0 + (nom / 100.0)) / (1.0 + (inf / 100.0)) - 1.0) * 100.0, 2)
    
    if real_ret > 0:
        st_label = "Alım Gücü Korundu (Reel Alfa)"
        st_badge = "bg-success-subtle text-success border border-success-subtle"
        st_icon = "bi-check-circle-fill"
        st_desc = f"Portföy getiriniz (%{nom:+.2f}), dönem enflasyonunu (%{inf:+.1f}) aşarak net reel alım gücünüzü artırmıştır."
    elif nom > 0 and real_ret <= 0:
        st_label = "Enflasyonist Erime (Nominal Kâr, Reel Kayıp)"
        st_badge = "bg-warning-subtle text-warning-emphasis border border-warning-subtle"
        st_icon = "bi-exclamation-diamond-fill"
        st_desc = f"Portföyünüz nominalde kâr (%{nom:+.2f}) üretse de enflasyon karşısında reel alım gücü kaybı (%{abs(real_ret):.2f}) yaşamıştır."
    else:
        st_label = "Reel Değer Kaybı"
        st_badge = "bg-danger-subtle text-danger border border-danger-subtle"
        st_icon = "bi-arrow-down-circle-fill"
        st_desc = f"Portföyünüz hem nominalde (%{nom:+.2f}) hem de enflasyona göre reel bazda (%{real_ret:+.2f}) değer kaybetmiştir."

    return {
        'nominal_return': nom,
        'inflation_rate': inf,
        'real_return': real_ret,
        'status_label': st_label,
        'badge_class': st_badge,
        'icon': st_icon,
        'desc': st_desc,
        'description': st_desc
    }


def evaluate_credit_utilization(unpaid_debt: float, card_limit: float) -> Dict[str, Any]:
    """
    Kredi kartı limit kullanım oranını ve Findeks/FICO risk seviyelerini hesaplar.
    
    :param unpaid_debt: Dönem içi ödenmemiş kredi kartı toplam harcaması
    :param card_limit: Kullanıcının tanımlı toplam kredi kartı limiti
    :return: Kullanılabilir limit, doluluk yüzdesi ve risk durum bilgilerini içeren sözlük
    """
    debt = max(0.0, float(unpaid_debt))
    limit = max(0.0, float(card_limit))
    
    available = max(0.0, limit - debt)
    pct = round((debt / limit * 100.0), 1) if limit > 0 else 0.0
    
    if pct <= 30.0:
        status = "İdeal Kullanım (Kredi Notu Pozitif)"
        level = "ideal"
        badge = "bg-success-subtle text-success border border-success-subtle"
        bar = "bg-success"
        icon = "bi-shield-check"
        advice = "Kredi kartı borcunuz limitinizin %30'unun altında. Bu oran Findeks ve kredi notunuzu olumlu yönde destekler."
    elif pct <= 70.0:
        status = "Dengeli Kullanım (İzlenmeli)"
        level = "moderate"
        badge = "bg-warning-subtle text-warning-emphasis border border-warning-subtle"
        bar = "bg-warning"
        icon = "bi-exclamation-triangle"
        advice = "Limit doluluğunuz %30-%70 bandında. Kredi notu optimizasyonu için ekstre kesiminden önce ara ödeme yapabilirsiniz."
    else:
        status = "Yüksek Limit Riski (Kredi Notu Baskısı)"
        level = "high"
        badge = "bg-danger-subtle text-danger border border-danger-subtle"
        bar = "bg-danger"
        icon = "bi-exclamation-octagon-fill"
        advice = "Limit kullanımınız %70'in üzerinde! Yüksek limit doluluğu kredi notunuza baskı yapar; borcu düşürmeniz tavsiye edilir."

    return {
        'available_card_limit': available,
        'cc_utilization_pct': pct,
        'cc_risk_status': status,
        'cc_risk_level': level,
        'cc_risk_badge_class': badge,
        'cc_risk_bar_class': bar,
        'cc_risk_icon': icon,
        'cc_risk_advice': advice
    }


def analyze_portfolio_concentration(
    allocations_by_symbol: Optional[List[Dict[str, Any]]] = None,
    allocations_by_type: Optional[List[Dict[str, Any]]] = None,
    total_portfolio_value: Optional[float] = None,
    allocations: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """
    Portföy konsantrasyonu ve çeşitlendirme (diversification) risk skorunu analiz eder.
    
    :param allocations_by_symbol: Varlık/sembol bazlı yüzde ve değer listesi
    :param allocations_by_type: Varlık türü/sınıfı bazlı dağılım listesi
    :param total_portfolio_value: Toplam portföy piyasa değeri
    :param allocations: Kolay kullanım için tekil dağılım listesi alternatifi
    :return: Çeşitlendirme seviyesi, rozet ve finansal tavsiye sözlüğü
    """
    symbols = allocations_by_symbol if allocations_by_symbol is not None else (allocations or [])
    types = allocations_by_type or []

    if total_portfolio_value is not None and total_portfolio_value <= 0:
        symbols = []

    if not symbols:
        return {
            'status': 'Varlık Yok',
            'level': 'empty',
            'badge_class': 'bg-secondary-subtle text-secondary border border-secondary-subtle',
            'icon': 'bi-dash-circle',
            'advice': 'Portföyünüze varlık ekleyerek risk ve çeşitlendirme analizini etkinleştirebilirsiniz.',
            'max_weight': 0.0,
            'top_symbol': '',
            'class_count': 0
        }

    max_single_weight = max([float(s.get('pct', 0.0)) for s in symbols], default=0.0)
    top_symbol = symbols[0].get('name', symbols[0].get('symbol', '')) if symbols else ''
    asset_class_count = len(types) if types else (1 if symbols else 0)

    if max_single_weight > 60.0:
        return {
            'status': 'Yüksek Konsantrasyon Riski',
            'level': 'high_risk',
            'badge_class': 'bg-danger-subtle text-danger border border-danger-subtle',
            'icon': 'bi-exclamation-triangle-fill',
            'advice': f"Portföyünüzün %{max_single_weight:.1f}'i tek başına '{top_symbol}' varlığında toplanmıştır. Dalgalanmalara karşı farklı varlık sınıflarına yayılarak riski azaltabilirsiniz.",
            'max_weight': max_single_weight,
            'top_symbol': top_symbol,
            'class_count': asset_class_count
        }
    elif asset_class_count >= 2 and max_single_weight <= 50.0:
        return {
            'status': 'Dengeli Çeşitlendirme',
            'level': 'balanced',
            'badge_class': 'bg-success-subtle text-success border border-success-subtle',
            'icon': 'bi-shield-check',
            'advice': f"Portföyünüz {asset_class_count} farklı varlık sınıfına dağılmış ve tekil varlık konsantrasyonu güvenli seviyededir (En yüksek ağırlık: %{max_single_weight:.1f}).",
            'max_weight': max_single_weight,
            'top_symbol': top_symbol,
            'class_count': asset_class_count
        }
    else:
        return {
            'status': 'Orta Düzey Dağılım',
            'level': 'moderate',
            'badge_class': 'bg-primary-subtle text-primary border border-primary-subtle',
            'icon': 'bi-info-circle-fill',
            'advice': f"Portföy dağılımınız dengeli ancak tekil varlık ağırlığınız (%{max_single_weight:.1f}) dikkat gerektiriyor. Farklı sektör ve enstrümanlarla riski dağıtabilirsiniz.",
            'max_weight': max_single_weight,
            'top_symbol': top_symbol,
            'class_count': asset_class_count
        }


def calculate_wac_and_pnl(cur_qty: float, cur_avg_cost: float, new_qty: float, new_unit_price: float) -> Dict[str, float]:
    """
    Ağırlıklı Ortalama Maliyet (WAC) formülü ile yeni ortalama maliyeti ve toplam tutarı hesaplar.
    
    :param cur_qty: Mevcut varlık adedi
    :param cur_avg_cost: Mevcut birim ortalama maliyet
    :param new_qty: Yeni alınan varlık adedi
    :param new_unit_price: Yeni alım birim fiyatı
    :return: total_quantity, average_cost ve total_cost
    """
    c_qty = float(cur_qty)
    c_avg = float(cur_avg_cost)
    n_qty = float(new_qty)
    n_price = float(new_unit_price)

    tot_qty = c_qty + n_qty
    if tot_qty > 0:
        new_avg = ((c_qty * c_avg) + (n_qty * n_price)) / tot_qty
    else:
        new_avg = 0.0

    return {
        'total_quantity': round(tot_qty, 4),
        'average_cost': round(new_avg, 4),
        'total_cost': round(tot_qty * new_avg, 2)
    }


def calculate_realized_pnl(cur_avg_cost: float, sell_qty: float, sell_unit_price: float) -> float:
    """
    Varlık satışı esnasında realize edilen kâr/zarar tutarını hesaplar.
    Formül: (Satış Fiyatı - Ortalama Maliyet) * Satılan Adet
    """
    return round((float(sell_unit_price) - float(cur_avg_cost)) * float(sell_qty), 2)

