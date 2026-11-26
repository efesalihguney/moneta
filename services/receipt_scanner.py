"""
Moneta Akıllı Fiş & Fatura Okuma (OCR & AI Kategorizasyon) Servisi
Hibrit Mimari:
  1. Birincil Motor: Gemini Multimodal Vision AI (hafif REST isteği ile gemini-2.5-flash)
  2. Yedek/Çevrimdışı Motor: pypdf / pytesseract (mevcutsa) + Türkçe fiş/fatura regex & dosya adı ayrıştırıcısı
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

from config import Config
from services.finance_engine import parse_safe_amount

ALLOWED_RECEIPT_SCAN_EXTENSIONS = {'jpg', 'jpeg', 'png', 'webp', 'pdf'}
MAX_RECEIPT_SCAN_BYTES = 5 * 1024 * 1024  # 5 MB

MIME_TYPE_MAP = {
    'jpg': 'image/jpeg',
    'jpeg': 'image/jpeg',
    'png': 'image/png',
    'webp': 'image/webp',
    'pdf': 'application/pdf',
}

# Bilinen Türk işletme adları -> standart gösterim adı ve kategori grubu
KNOWN_MERCHANTS: List[Tuple[str, str, str]] = [
    # Market & Gıda
    ('migros', 'Migros Ticaret A.Ş.', 'market'),
    ('bim', 'BİM Birleşik Mağazalar A.Ş.', 'market'),
    ('a101', 'A101 Yeni Mağazacılık A.Ş.', 'market'),
    ('sok market', 'ŞOK Marketler Ticaret A.Ş.', 'market'),
    ('carrefoursa', 'CarrefourSA', 'market'),
    ('carrefour', 'CarrefourSA', 'market'),
    ('macrocenter', 'Macrocenter', 'market'),
    ('file market', 'File Market', 'market'),
    ('metro market', 'Metro Türkiye', 'market'),
    ('hakmar', 'Hakmar', 'market'),
    ('onur market', 'Onur Market', 'market'),
    ('getir', 'Getir', 'market'),
    ('istegelsin', 'İstegelsin', 'market'),
    # Ulaşım & Yakıt
    ('shell', 'Shell & Turcas Petrol A.Ş.', 'ulasim'),
    ('opet', 'Opet Petrolcülük A.Ş.', 'ulasim'),
    ('petrol ofisi', 'Petrol Ofisi A.Ş.', 'ulasim'),
    ('totalenergies', 'TotalEnergies', 'ulasim'),
    ('aytemiz', 'Aytemiz Akaryakıt', 'ulasim'),
    ('lukoil', 'Lukoil', 'ulasim'),
    ('bp petrolleri', 'BP Petrolleri', 'ulasim'),
    ('istanbulkart', 'İstanbulkart', 'ulasim'),
    ('turkish airlines', 'Türk Hava Yolları', 'ulasim'),
    ('pegasus', 'Pegasus Hava Yolları', 'ulasim'),
    ('obilet', 'Obilet', 'ulasim'),
    ('bitaksi', 'BiTaksi', 'ulasim'),
    ('uber', 'Uber', 'ulasim'),
    ('marti', 'Martı', 'ulasim'),
    # Faturalar
    ('igdas', 'İGDAŞ', 'fatura'),
    ('tedas', 'TEDAŞ', 'fatura'),
    ('enerjisa', 'Enerjisa', 'fatura'),
    ('ck bogazici', 'CK Boğaziçi Elektrik', 'fatura'),
    ('bedas', 'BEDAŞ', 'fatura'),
    ('ayedas', 'AYEDAŞ', 'fatura'),
    ('iski', 'İSKİ', 'fatura'),
    ('aski', 'ASKİ', 'fatura'),
    ('izsu', 'İZSU', 'fatura'),
    ('turk telekom', 'Türk Telekom', 'fatura'),
    ('turkcell', 'Turkcell', 'fatura'),
    ('vodafone', 'Vodafone', 'fatura'),
    ('superonline', 'Turkcell Superonline', 'fatura'),
    ('turknet', 'TurkNet', 'fatura'),
    ('digiturk', 'Digiturk', 'fatura'),
    # Sağlık
    ('eczane', 'Eczane', 'saglik'),
    ('acibadem', 'Acıbadem Sağlık Grubu', 'saglik'),
    ('memorial', 'Memorial Hastanesi', 'saglik'),
    ('medical park', 'Medical Park', 'saglik'),
    ('hastane', 'Hastane', 'saglik'),
    # Yeme & İçme
    ('starbucks', 'Starbucks', 'yeme_icme'),
    ('kahve dunyasi', 'Kahve Dünyası', 'yeme_icme'),
    ('espressolab', 'Espressolab', 'yeme_icme'),
    ('tchibo', 'Tchibo', 'yeme_icme'),
    ('mcdonalds', "McDonald's", 'yeme_icme'),
    ('burger king', 'Burger King', 'yeme_icme'),
    ('popeyes', 'Popeyes', 'yeme_icme'),
    ('dominos', "Domino's Pizza", 'yeme_icme'),
    ('yemeksepeti', 'Yemeksepeti', 'yeme_icme'),
    ('tavuk dunyasi', 'Tavuk Dünyası', 'yeme_icme'),
    ('kofteci yusuf', 'Köfteci Yusuf', 'yeme_icme'),
    # Giyim & Alışveriş
    ('lc waikiki', 'LC Waikiki', 'alisveris'),
    ('lcw', 'LC Waikiki', 'alisveris'),
    ('defacto', 'DeFacto', 'alisveris'),
    ('koton', 'Koton', 'alisveris'),
    ('zara', 'Zara', 'alisveris'),
    ('mavi', 'Mavi', 'alisveris'),
    ('boyner', 'Boyner', 'alisveris'),
    ('trendyol', 'Trendyol', 'alisveris'),
    ('hepsiburada', 'Hepsiburada', 'alisveris'),
    ('amazon', 'Amazon Türkiye', 'alisveris'),
    ('decathlon', 'Decathlon', 'alisveris'),
    ('ikea', 'IKEA', 'alisveris'),
    ('koctas', 'Koçtaş', 'alisveris'),
    ('teknosa', 'Teknosa', 'alisveris'),
    ('mediamarkt', 'MediaMarkt', 'alisveris'),
    ('vatan bilgisayar', 'Vatan Bilgisayar', 'alisveris'),
    ('gratis', 'Gratis', 'alisveris'),
    ('watsons', 'Watsons', 'alisveris'),
    # Eğlence
    ('netflix', 'Netflix', 'eglence'),
    ('spotify', 'Spotify', 'eglence'),
    ('cineverse', 'Paribu Cineverse', 'eglence'),
    ('biletix', 'Biletix', 'eglence'),
    ('passo', 'Passo', 'eglence'),
]

# Semantik kategori grupları: (metin anahtar kelimeleri, kullanıcı kategori adı anahtar kelimeleri)
CATEGORY_BUCKETS: Dict[str, Dict[str, List[str]]] = {
    'market': {
        'text_keywords': [
            'migros', 'bim', 'a101', 'sok', 'carrefour', 'macrocenter', 'file',
            'metro', 'hakmar', 'cagdas', 'onur market', 'getir', 'istegelsin',
            'market', 'supermarket', 'bakkal', 'manav', 'kasap', 'firin',
            'gida', 'kuruyemis', 'sut', 'ekmek', 'sebze', 'meyve',
        ],
        'category_keywords': ['market', 'gida', 'mutfak', 'supermarket', 'alisveris'],
        'label': 'Market Alışverişi',
    },
    'ulasim': {
        'text_keywords': [
            'shell', 'opet', 'petrol ofisi', 'bp', 'total', 'aytemiz', 'lukoil',
            'akaryakit', 'benzin', 'motorin', 'lpg', 'istanbulkart', 'ego',
            'eshot', 'marti', 'uber', 'bitaksi', 'taksi', 'thy', 'pegasus',
            'ajet', 'tcdd', 'obilet', 'hgs', 'ogs', 'otopark', 'ispark',
            'ulasim', 'yakit', 'arac', 'otobus',
        ],
        'category_keywords': ['ulasim', 'yakit', 'akaryakit', 'arac', 'seyahat', 'taksi'],
        'label': 'Ulaşım / Yakıt',
    },
    'fatura': {
        'text_keywords': [
            'igdas', 'tedas', 'enerjisa', 'bogazici', 'bedas', 'ayedas', 'iski',
            'aski', 'izsu', 'turk telekom', 'turkcell', 'vodafone', 'superonline',
            'turknet', 'digiturk', 'd-smart', 'elektrik', 'dogalgaz', 'su faturasi',
            'internet', 'telefon', 'fatura', 'abone', 'abonelik', 'gsm',
        ],
        'category_keywords': ['fatura', 'faturalar', 'abonelik', 'elektrik', 'dogalgaz', 'internet'],
        'label': 'Fatura Ödemesi',
    },
    'saglik': {
        'text_keywords': [
            'eczane', 'hastane', 'klinik', 'medikal', 'acibadem', 'memorial',
            'medical park', 'optik', 'gozluk', 'saglik', 'ilac', 'muayene',
            'laboratuvar', 'tahlil', 'dis hekimi',
        ],
        'category_keywords': ['saglik', 'eczane', 'hastane', 'medikal'],
        'label': 'Sağlık Harcaması',
    },
    'yeme_icme': {
        'text_keywords': [
            'restoran', 'restaurant', 'kafe', 'cafe', 'kahve', 'starbucks',
            'kahve dunyasi', 'espressolab', 'tchibo', 'mcdonalds', 'burger king',
            'popeyes', 'domino', 'pizza', 'kebap', 'doner', 'lokanta', 'pastane',
            'yemeksepeti', 'kofteci', 'tavuk dunyasi', 'pide', 'lahmacun',
            'yemek', 'bistro', 'icme', 'yeme',
        ],
        'category_keywords': ['yeme', 'icme', 'restoran', 'kafe', 'yemek', 'disarida'],
        'label': 'Yeme & İçme',
    },
    'alisveris': {
        'text_keywords': [
            'lc waikiki', 'lcw', 'defacto', 'koton', 'zara', 'h&m', 'mavi',
            'boyner', 'trendyol', 'hepsiburada', 'amazon', 'n11', 'decathlon',
            'ikea', 'koctas', 'teknosa', 'mediamarkt', 'vatan bilgisayar',
            'gratis', 'watsons', 'sephora', 'giyim', 'ayakkabi', 'kozmetik',
            'elektronik', 'magaza',
        ],
        'category_keywords': ['giyim', 'alisveris', 'elektronik', 'kozmetik', 'kisisel'],
        'label': 'Alışveriş',
    },
    'eglence': {
        'text_keywords': [
            'sinema', 'cineverse', 'biletix', 'passo', 'netflix', 'spotify',
            'youtube', 'steam', 'playstation', 'konser', 'tiyatro', 'eglence',
            'hobi', 'spor', 'macfit',
        ],
        'category_keywords': ['eglence', 'kultur', 'hobi', 'spor', 'abonelik'],
        'label': 'Eğlence & Kültür',
    },
    'ev_kira': {
        'text_keywords': ['kira', 'aidat', 'emlak', 'mobilya', 'tadilat'],
        'category_keywords': ['kira', 'ev', 'konut', 'aidat', 'yasam'],
        'label': 'Ev & Kira',
    },
    'egitim': {
        'text_keywords': ['kitap', 'kirtasiye', 'kurs', 'okul', 'universite', 'udemy', 'egitim'],
        'category_keywords': ['egitim', 'kitap', 'kurs', 'okul'],
        'label': 'Eğitim',
    },
    'banka_dekont': {
        'text_keywords': [
            'havale', 'eft', 'fast', 'dekont', 'iban', 'transfer', 'komisyon', 'alici',
            'gonderen', 'banka', 'hesap no', 'islem tutari',
        ],
        'category_keywords': ['banka', 'transfer', 'fatura', 'faturalar', 'finans', 'odeme', 'diger'],
        'label': 'Banka Dekontu / Transfer',
    },
}

_TR_ASCII_MAP = str.maketrans({
    'İ': 'i', 'I': 'i', 'ı': 'i',
    'Ş': 's', 'ş': 's',
    'Ğ': 'g', 'ğ': 'g',
    'Ü': 'u', 'ü': 'u',
    'Ö': 'o', 'ö': 'o',
    'Ç': 'c', 'ç': 'c',
})


def normalize_tr(text: Optional[str]) -> str:
    """Türkçe karakterleri normalize edip küçük harfli arama metni üretir."""
    if not text:
        return ''
    return str(text).translate(_TR_ASCII_MAP).lower().strip()


def is_allowed_scan_file(filename: Optional[str]) -> bool:
    """Dosya uzantısının fiş/fatura taraması için desteklenip desteklenmediğini kontrol eder."""
    if not filename or '.' not in filename:
        return False
    ext = filename.rsplit('.', 1)[1].lower()
    return ext in ALLOWED_RECEIPT_SCAN_EXTENSIONS


def detect_mime_type(filename: Optional[str], content_type: Optional[str] = None) -> str:
    """Dosya uzantısından veya içerik başlığından güvenli MIME türünü döner."""
    if filename and '.' in filename:
        ext = filename.rsplit('.', 1)[1].lower()
        if ext in MIME_TYPE_MAP:
            return MIME_TYPE_MAP[ext]
    if content_type and content_type.split(';')[0].strip().lower() in MIME_TYPE_MAP.values():
        return content_type.split(';')[0].strip().lower()
    return 'application/octet-stream'


def _normalize_date(raw_date: Optional[str]) -> Optional[str]:
    """Farklı tarih biçimlerini (DD.MM.YYYY, DD/MM/YYYY, YYYY-MM-DD) YYYY-MM-DD formatına çevirir."""
    if not raw_date:
        return None
    s = str(raw_date).strip()
    if not s:
        return None

    for fmt in ('%Y-%m-%d', '%d.%m.%Y', '%d/%m/%Y', '%d-%m-%Y', '%Y.%m.%d', '%Y/%m/%d', '%d.%m.%y', '%d/%m/%y'):
        try:
            dt = datetime.strptime(s[:10], fmt)
            if 2000 <= dt.year <= 2100:
                return dt.strftime('%Y-%m-%d')
        except ValueError:
            continue
    return None


def _contains_keyword(haystack: str, keyword: str) -> bool:
    """Kısa anahtar kelimelerde (<=3 karakter) kelime sınırı kontrolü yaparak yanlış eşleşmeyi önler."""
    if not haystack or not keyword:
        return False
    if len(keyword) <= 3:
        return re.search(rf'(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])', haystack) is not None
    return keyword in haystack


def match_best_category(
    merchant: Optional[str],
    raw_text: Optional[str],
    categories: List[Dict[str, Any]],
    suggested_category_id: Optional[Any] = None,
    suggested_category_name: Optional[str] = None,
) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """
    Çıkarılan işletme/metin bilgisini kullanıcının veritabanındaki mevcut kategorileriyle eşleştirir.
    Dönüş: (category_id, category_name, matched_bucket_key)
    """
    if not categories:
        return None, None, None

    cat_by_id: Dict[int, Dict[str, Any]] = {}
    for cat in categories:
        try:
            cid = int(cat['id'])
            cat_by_id[cid] = {
                'id': cid,
                'name': str(cat.get('name', '')).strip(),
                'type': str(cat.get('type', 'gider')).strip().lower(),
                'norm': normalize_tr(cat.get('name', '')),
            }
        except (KeyError, TypeError, ValueError):
            continue

    if not cat_by_id:
        return None, None, None

    # 1. AI'nin önerdiği kategori ID'si gerçekten kullanıcıya aitse doğrudan kullan
    if suggested_category_id is not None and str(suggested_category_id).strip() != '':
        try:
            sid = int(suggested_category_id)
            if sid in cat_by_id:
                c = cat_by_id[sid]
                return c['id'], c['name'], None
        except (TypeError, ValueError):
            pass

    # 2. AI'nin önerdiği kategori adıyla birebir veya kapsayan eşleşme
    if suggested_category_name:
        norm_sug = normalize_tr(suggested_category_name)
        if norm_sug:
            for c in cat_by_id.values():
                if c['norm'] == norm_sug or (norm_sug in c['norm']) or (c['norm'] in norm_sug and len(c['norm']) >= 4):
                    return c['id'], c['name'], None

    # 3. İşletme adı ve fiş metninden semantik grup tespiti
    combined = normalize_tr(f"{merchant or ''} {raw_text or ''}")
    detected_bucket: Optional[str] = None

    for key, _display, bucket in KNOWN_MERCHANTS:
        if _contains_keyword(combined, key):
            detected_bucket = bucket
            break

    if not detected_bucket:
        best_bucket_score = 0
        for b_key, b_cfg in CATEGORY_BUCKETS.items():
            score = sum(1 for kw in b_cfg['text_keywords'] if _contains_keyword(combined, kw))
            if score > best_bucket_score:
                best_bucket_score = score
                detected_bucket = b_key

    # Gider kategorilerini önceliklendir
    expense_cats = [c for c in cat_by_id.values() if c['type'] == 'gider']
    candidate_cats = expense_cats if expense_cats else list(cat_by_id.values())

    if detected_bucket and detected_bucket in CATEGORY_BUCKETS:
        target_kws = CATEGORY_BUCKETS[detected_bucket]['category_keywords']
        best_cat = None
        best_score = 0
        for c in candidate_cats:
            score = 0
            for idx, kw in enumerate(target_kws):
                if kw in c['norm']:
                    # İlk sıradaki anahtar kelimeler daha yüksek öncelikli
                    score += max(1, len(target_kws) - idx)
            if score > best_score:
                best_score = score
                best_cat = c
        if best_cat:
            return best_cat['id'], best_cat['name'], detected_bucket

    # 4. Doğrudan kullanıcı kategori adının metinde geçmesi
    for c in candidate_cats:
        tokens = [t for t in re.split(r'[^a-z0-9]+', c['norm']) if len(t) >= 4]
        if any(_contains_keyword(combined, tok) for tok in tokens):
            return c['id'], c['name'], detected_bucket

    # 5. Genel/Diğer gider kategorisi yedeği (varsa)
    for c in candidate_cats:
        if any(k in c['norm'] for k in ('diger', 'genel', 'muhtelif')):
            return c['id'], c['name'], detected_bucket

    return None, None, detected_bucket


def _extract_json_object(raw_text: str) -> Optional[Dict[str, Any]]:
    """Model çıktısından JSON nesnesini güvenli biçimde ayıklar."""
    if not raw_text:
        return None
    cleaned = raw_text.strip()
    if cleaned.startswith('```'):
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r'\s*```$', '', cleaned)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = cleaned.find('{')
    end = cleaned.rfind('}')
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(cleaned[start:end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return None
    return None


def _build_description(
    merchant: Optional[str],
    category_name: Optional[str],
    bucket_key: Optional[str] = None,
    vat_amount: Optional[float] = None,
    ai_description: Optional[str] = None,
) -> str:
    """İşlem açıklaması için tutarlı ve bilgilendirici özet üretir."""
    if ai_description and str(ai_description).strip():
        desc = str(ai_description).strip()
        if vat_amount and vat_amount > 0 and 'kdv' not in desc.lower():
            desc = f"{desc} (KDV: {vat_amount:.2f} TL)"
        return desc[:240]

    parts: List[str] = []
    if merchant:
        parts.append(merchant.strip())

    label = None
    if bucket_key and bucket_key in CATEGORY_BUCKETS:
        label = CATEGORY_BUCKETS[bucket_key]['label']
    elif category_name:
        label = category_name.strip()

    if label:
        if not parts or normalize_tr(label) not in normalize_tr(parts[0]):
            parts.append(label)

    base_desc = ' - '.join(parts) if parts else 'Fiş / Fatura Harcaması'
    if vat_amount and vat_amount > 0:
        base_desc = f"{base_desc} (KDV: {vat_amount:.2f} TL)"
    return base_desc[:240]


class GeminiAPIError(RuntimeError):
    """Gemini / Vertex AI REST çağrılarında dönen HTTP durum kodu ve kısa hata özetini taşır."""

    def __init__(self, status_code: int, short_message: str):
        self.status_code = status_code
        self.short_message = short_message
        super().__init__(f"HTTP {status_code}: {short_message}")


GEMINI_FALLBACK_MODELS: Tuple[str, ...] = (
    'gemini-flash-lite-latest',
    'gemini-3.5-flash-lite',
    'gemini-3.1-flash-lite',
    'gemini-3.8-flash',
    'gemini-flash-latest',
)


def _build_model_chain(primary_model: str) -> List[str]:
    """Birincil modelden başlayarak erişilebilir Vision yedek modelleriyle tekil zincir oluşturur."""
    chain: List[str] = []
    for m in (primary_model, *GEMINI_FALLBACK_MODELS):
        clean_m = (m or '').strip()
        if clean_m and clean_m not in chain:
            chain.append(clean_m)
    return chain


def _build_gemini_endpoint(api_key: str, model: str) -> str:
    """Hem AQ. hem AIza anahtarları için standart Google Generative Language API ucunu döner."""
    return f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _extract_google_api_error(resp: Any) -> str:
    """Google REST hata gövdesinden kısa ve okunabilir hata mesajı çıkarır."""
    try:
        err_json = resp.json()
        if isinstance(err_json, dict):
            err_obj = err_json.get('error')
            if isinstance(err_obj, dict) and err_obj.get('message'):
                return str(err_obj['message']).strip().splitlines()[0][:160]
    except Exception:
        pass
    raw_txt = getattr(resp, 'text', '') or getattr(resp, 'reason', '') or 'Bilinmeyen API hatası'
    return str(raw_txt).strip().splitlines()[0][:160]


def _scan_with_gemini(
    file_bytes: bytes,
    mime_type: str,
    categories: List[Dict[str, Any]],
    api_key: str,
    model: str,
    timeout: int = 25,
) -> Dict[str, Any]:
    """
    Birincil Motor: Gemini Multimodal Vision AI REST API ile fiş/faturayı okur ve yapılandırılmış JSON döner.
    AQ. anahtarları için Vertex AI Express Mode, AIza anahtarları için Google AI Studio ucunu kullanır.
    Seçili model 404/400 döndürürse otomatik olarak gemini-2.0-flash ile bir kez daha dener.
    """
    expense_cats = [c for c in categories if str(c.get('type', 'gider')).lower() == 'gider']
    prompt_cats = expense_cats if expense_cats else categories
    cat_lines = "\n".join(
        f"- id={c['id']} | ad=\"{c['name']}\" | tur={c.get('type', 'gider')}"
        for c in prompt_cats
    ) or "- (Kullanıcı kategorisi tanımlı değil)"

    prompt = (
        "Sen Türkiye'deki market fişleri, restoran adisyonları, akaryakıt fişleri, banka dekontları ve e-Arşiv/e-Fatura "
        "belgelerini okuyan uzman bir finans OCR asistanısın.\n"
        "Yüklenen belgeyi incele ve SADECE geçerli bir JSON nesnesi döndür (ek açıklama yazma).\n\n"
        "Kullanıcının mevcut veritabanı kategorileri:\n"
        f"{cat_lines}\n\n"
        "İstenen JSON alanları:\n"
        "- merchant: string veya null (İşletme / firma / banka tam adı, örn. 'Migros Ticaret A.Ş.')\n"
        "- amount: number veya null (Genel toplam / ödenecek tutar, pozitif ondalık sayı, örn. 485.50)\n"
        "- date: string veya null (YYYY-MM-DD biçiminde işlem/fatura tarihi)\n"
        "- category_id: integer veya null (Yukarıdaki kullanıcı kategorilerinden en uygun olanın id değeri)\n"
        "- category_name: string veya null (Seçilen kategorinin adı)\n"
        "- vat_amount: number veya null (Belgede varsa toplam KDV tutarı)\n"
        "- description: string (Kısa ve net harcama açıklaması, KDV varsa parantez içinde belirt, örn. 'Migros - Market Alışverişi (KDV: 42.10 TL)')\n"
        "- confidence: number (0.0 ile 1.0 arası güven skoru)\n"
    )

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": base64.b64encode(file_bytes).decode('ascii'),
                        }
                    },
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }

    models_to_try = _build_model_chain(model)

    resp = None
    last_error: Optional[Exception] = None
    for idx, candidate_model in enumerate(models_to_try):
        url = _build_gemini_endpoint(api_key, candidate_model)
        has_next = idx + 1 < len(models_to_try)
        try:
            resp = requests.post(
                url,
                params={"key": api_key},
                json=payload,
                timeout=timeout,
            )
        except requests.exceptions.Timeout as exc:
            last_error = exc
            print(f"[Moneta ReceiptScanner API Timeout] model={candidate_model} -> zaman aşımı, sıradaki modele geçiliyor...")
            if has_next:
                continue
            raise

        status_code = getattr(resp, 'status_code', None)
        if isinstance(status_code, int) and status_code >= 400:
            short_msg = _extract_google_api_error(resp)
            last_error = GeminiAPIError(status_code, short_msg)
            print(f"[Moneta ReceiptScanner API Error] model={candidate_model} url={url} -> HTTP {status_code}: {short_msg}")
            is_invalid_key = status_code == 400 and 'api key' in short_msg.lower()
            if not is_invalid_key and status_code in (400, 404, 429, 503) and has_next:
                continue
            raise last_error
        resp.raise_for_status()
        last_error = None
        break

    if last_error is not None or resp is None:
        raise last_error or RuntimeError("Gemini API yanıtı alınamadı.")

    body = resp.json()

    candidates = body.get('candidates') or []
    if not candidates:
        raise ValueError("Gemini API boş yanıt döndürdü.")

    parts = (candidates[0].get('content') or {}).get('parts') or []
    raw_text = "".join(p.get('text', '') for p in parts if isinstance(p, dict))
    parsed = _extract_json_object(raw_text)
    if not parsed:
        raise ValueError("Gemini yanıtı geçerli JSON olarak ayrıştırılamadı.")

    merchant = str(parsed.get('merchant')).strip() if parsed.get('merchant') else None
    amount = parse_safe_amount(parsed.get('amount'))
    vat_amount = parse_safe_amount(parsed.get('vat_amount'), min_val=0.0)
    date_str = _normalize_date(parsed.get('date'))

    cat_id, cat_name, bucket_key = match_best_category(
        merchant=merchant,
        raw_text=str(parsed.get('description') or ''),
        categories=categories,
        suggested_category_id=parsed.get('category_id'),
        suggested_category_name=parsed.get('category_name'),
    )

    description = _build_description(
        merchant=merchant,
        category_name=cat_name,
        bucket_key=bucket_key,
        vat_amount=vat_amount,
        ai_description=parsed.get('description'),
    )

    try:
        confidence = float(parsed.get('confidence', 0.85))
    except (TypeError, ValueError):
        confidence = 0.85
    confidence = round(max(0.0, min(1.0, confidence)), 2)

    return {
        'success': True,
        'merchant': merchant,
        'amount': amount,
        'date': date_str,
        'category_id': cat_id,
        'category_name': cat_name,
        'description': description,
        'confidence': confidence,
        'engine': 'gemini',
        'warning': None,
    }


_SAFE_TEXT_CHARS_RE = re.compile(r"[A-Za-z0-9çğıöşüÇĞİÖŞÜ₺€$£%.,:;!?\-/()\[\]+*'\"&@#_\s]")
_BINARY_PDF_MARKERS = ('flatedecode', 'endstream', '/filter', '/xref', 'startxref')


def _is_garbled_or_binary_text(text: Optional[str]) -> bool:
    """Metnin sıkıştırılmış PDF ikili akışı, kontrol karakteri veya mojibake içerip içermediğini kontrol eder."""
    if not text or not text.strip():
        return True

    lowered = text.lower()
    if any(marker in lowered for marker in _BINARY_PDF_MARKERS):
        return True

    non_ws = [ch for ch in text if not ch.isspace()]
    if not non_ws:
        return True

    # Kontrol karakterleri (\x00-\x1f, \x7f-\x9f) veya Unicode replacement char (\ufffd)
    bad_control = sum(1 for ch in text if ((ord(ch) < 32 and ch not in '\n\r\t') or (127 <= ord(ch) <= 159) or ch == '\ufffd'))
    if bad_control > 0:
        return True

    # Standart ASCII + Türkçe karakter + yaygın noktalama dışındaki bozuk karakter oranı
    unsafe_chars = sum(1 for ch in non_ws if not _SAFE_TEXT_CHARS_RE.match(ch))
    if (unsafe_chars / len(non_ws)) > 0.12:
        return True

    return False


def _extract_text_from_pdf(file_bytes: bytes) -> str:
    """PDF metnini önce pypdf.PdfReader ile okur; bozuk/ikili akışları (FlateDecode/mojibake) reddeder."""
    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(io.BytesIO(file_bytes))
        pages_text = []
        for page in reader.pages[:5]:
            txt = page.extract_text() or ''
            if txt.strip() and not _is_garbled_or_binary_text(txt):
                pages_text.append(txt)
        if pages_text:
            joined = "\n".join(pages_text)
            if not _is_garbled_or_binary_text(joined):
                return joined
    except Exception:
        pass

    # Sıkıştırılmış ikili akış (FlateDecode) barındıran PDF'lerde ham latin-1 regex kazıma yapma
    if b'FlateDecode' in file_bytes or b'/Filter' in file_bytes:
        return ''

    # Hafif yedek: yalnızca sıkıştırılmamış düz metin PDF bloklarından güvenli metin ayıkla
    try:
        decoded = file_bytes.decode('latin-1', errors='ignore')
        chunks = re.findall(r'\(([^()]{2,200})\)', decoded)
        if chunks:
            cleaned_chunks = []
            for c in chunks:
                unescaped = c.replace(r'\(', '(').replace(r'\)', ')').strip()
                if unescaped and not _is_garbled_or_binary_text(unescaped):
                    cleaned_chunks.append(unescaped)
            text = "\n".join(cleaned_chunks)
            if text and not _is_garbled_or_binary_text(text) and re.search(r'[A-Za-z0-9]{3,}', text):
                return text
    except Exception:
        pass

    return ''


def _extract_text_from_image(file_bytes: bytes) -> str:
    """pytesseract + Pillow mevcutsa görselden OCR metni çıkarır."""
    try:
        from PIL import Image  # type: ignore
        import pytesseract  # type: ignore

        img = Image.open(io.BytesIO(file_bytes))
        try:
            return pytesseract.image_to_string(img, lang='tur+eng') or ''
        except Exception:
            return pytesseract.image_to_string(img) or ''
    except Exception:
        return ''


def _extract_amount_from_text(text: str) -> Optional[float]:
    """Fiş/fatura metninden genel toplam tutarını Türkçe/uluslararası biçimlerde çıkarır."""
    if not text:
        return None

    # 1. Etiketli toplam satırları (öncelik sırasına göre)
    labeled_patterns = [
        r'(?:genel\s*toplam|odenecek\s*tutar|ödenecek\s*tutar|toplam\s*tutar|grand\s*total|net\s*tutar)\s*[:\-*]?\s*(?:tl|₺|try)?\s*\*?\s*([0-9]{1,3}(?:[.,][0-9]{3})*[.,][0-9]{2}|[0-9]+[.,][0-9]{2}|[0-9]+)',
        r'(?:toplam|top|total|tutar)\s*[:\-*]?\s*(?:tl|₺|try)?\s*\*?\s*([0-9]{1,3}(?:[.,][0-9]{3})*[.,][0-9]{2}|[0-9]+[.,][0-9]{2})',
    ]
    for pattern in labeled_patterns:
        matches = re.findall(pattern, text, flags=re.IGNORECASE)
        if matches:
            # Fişlerde genellikle en son geçen toplam satırı nihai genel toplamdır
            for candidate in reversed(matches):
                val = parse_safe_amount(candidate)
                if val is not None:
                    return val

    # 2. Para birimi işaretli veya fiş yıldızlı tutarlar (örn. 485,50 TL, ₺485.50, *485,50)
    currency_matches = re.findall(
        r'(?:(?:₺|tl|try)\s*([0-9]{1,3}(?:[.,][0-9]{3})*[.,][0-9]{2}|[0-9]+[.,][0-9]{2})|'
        r'([0-9]{1,3}(?:[.,][0-9]{3})*[.,][0-9]{2}|[0-9]+[.,][0-9]{2})\s*(?:₺|tl|try)|'
        r'\*\s*([0-9]{1,3}(?:[.,][0-9]{3})*[.,][0-9]{2}|[0-9]+[.,][0-9]{2}))',
        text,
        flags=re.IGNORECASE,
    )
    parsed_vals: List[float] = []
    for groups in currency_matches:
        raw_num = next((g for g in groups if g), None)
        val = parse_safe_amount(raw_num)
        if val is not None:
            parsed_vals.append(val)
    if parsed_vals:
        return max(parsed_vals)

    return None


def _extract_vat_from_text(text: str) -> Optional[float]:
    """Fiş/fatura metninden KDV tutarını çıkarır."""
    if not text:
        return None
    matches = re.findall(
        r'(?:topkdv|toplam\s*kdv|kdv\s*tutari|kdv\s*tutarı|kdv)\s*[:\-*]?\s*(?:tl|₺|try)?\s*\*?\s*([0-9]{1,3}(?:[.,][0-9]{3})*[.,][0-9]{2}|[0-9]+[.,][0-9]{2})',
        text,
        flags=re.IGNORECASE,
    )
    if matches:
        for candidate in reversed(matches):
            val = parse_safe_amount(candidate, min_val=0.01)
            if val is not None:
                return val
    return None


def _extract_date_from_text(text: str) -> Optional[str]:
    """Metin veya dosya adından tarih bilgisini YYYY-MM-DD olarak çıkarır."""
    if not text:
        return None
    patterns = [
        r'\b(20[0-9]{2}[-./](?:0[1-9]|1[0-2])[-./](?:0[1-9]|[12][0-9]|3[01]))\b',
        r'\b((?:0[1-9]|[12][0-9]|3[01])[./-](?:0[1-9]|1[0-2])[./-]20[0-9]{2})\b',
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            normalized = _normalize_date(match.group(1))
            if normalized:
                return normalized
    return None


def _extract_merchant_from_text(raw_text: str, filename: str) -> Optional[str]:
    """Bilinen marka listesi, OCR üst satırları veya dosya adından işletme adını çıkarır."""
    stem = os.path.splitext(os.path.basename(filename or ''))[0]
    cleaned_stem = re.sub(r'[_\-]+', ' ', stem).strip()
    combined_norm = normalize_tr(f"{raw_text} {cleaned_stem}")

    # 1. Bilinen markalar
    for key, display_name, _bucket in KNOWN_MERCHANTS:
        if _contains_keyword(combined_norm, key):
            return display_name

    # 2. OCR/PDF metnindeki ilk anlamlı başlık satırı
    if raw_text and not _is_garbled_or_binary_text(raw_text):
        skip_words = ('tarih', 'saat', 'fis no', 'fiş no', 'fatura', 'vergi', 'vkn', 'tckn', 'mersis', 'tel:', 'adres')
        for line in raw_text.splitlines()[:8]:
            stripped = line.strip()
            if len(stripped) < 3 or len(stripped) > 70:
                continue
            if _is_garbled_or_binary_text(stripped):
                continue
            norm_line = normalize_tr(stripped)
            if any(sw in norm_line for sw in skip_words):
                continue
            if re.search(r'[a-z]{3,}', norm_line) and not re.fullmatch(r'[0-9\s.,:/\-]+', stripped):
                return stripped

    # 3. Dosya adından anlamlı metin (tarih ve tutar parçalarını temizle)
    if cleaned_stem:
        no_dates = re.sub(r'\b20[0-9]{2}[-./]?[0-1][0-9][-./]?[0-3][0-9]\b', ' ', cleaned_stem)
        no_dates = re.sub(r'\b[0-3]?[0-9][./-][0-1]?[0-9][./-]20[0-9]{2}\b', ' ', no_dates)
        no_nums = re.sub(r'\b[0-9]+(?:[.,][0-9]+)?\s*(?:tl|try)?\b', ' ', no_dates, flags=re.IGNORECASE)
        no_generic = re.sub(
            r'\b(fis|fiş|fatura|dekont|makbuz|receipt|invoice|img|image|photo|scan|whatsapp|screenshot)\b',
            ' ',
            no_nums,
            flags=re.IGNORECASE,
        )
        candidate = re.sub(r'\s+', ' ', no_generic).strip()
        if len(candidate) >= 2 and not _is_garbled_or_binary_text(candidate):
            return candidate.title()

    return None


def _scan_with_fallback(
    file_bytes: bytes,
    filename: str,
    mime_type: str,
    categories: List[Dict[str, Any]],
    fallback_reason: str,
) -> Dict[str, Any]:
    """
    Yedek/Çevrimdışı Motor: pypdf / pytesseract (mevcutsa) + regex/dosya adı ayrıştırıcı.
    Sistemi kilitlemez; kısmi veri ve kullanıcıya manuel düzenleme uyarısı döner.
    """
    raw_text = ''
    if mime_type == 'application/pdf':
        raw_text = _extract_text_from_pdf(file_bytes)
    elif mime_type.startswith('image/'):
        raw_text = _extract_text_from_image(file_bytes)

    stem = os.path.splitext(os.path.basename(filename or ''))[0]
    cleaned_stem = re.sub(r'[_\-]+', ' ', stem).strip()
    search_blob = f"{raw_text}\n{cleaned_stem}".strip()

    merchant = _extract_merchant_from_text(raw_text, filename)
    if not merchant and mime_type == 'application/pdf':
        merchant = 'Banka Dekontu / Belge'
    amount = _extract_amount_from_text(raw_text) or _extract_amount_from_text(cleaned_stem)
    vat_amount = _extract_vat_from_text(raw_text)
    date_str = (
        _extract_date_from_text(raw_text)
        or _extract_date_from_text(stem.replace('_', ' '))
        or _extract_date_from_text(cleaned_stem)
    )

    cat_id, cat_name, bucket_key = match_best_category(
        merchant=merchant,
        raw_text=search_blob,
        categories=categories,
    )

    description = _build_description(
        merchant=merchant,
        category_name=cat_name,
        bucket_key=bucket_key,
        vat_amount=vat_amount,
    ) if (merchant or cat_name or amount) else None

    # Çıkarılan alan sayısına göre gerçekçi güven skoru
    has_real_merchant = bool(merchant and merchant != 'Banka Dekontu / Belge')
    extracted_signals = sum([
        1 if has_real_merchant else 0,
        1 if amount is not None else 0,
        1 if date_str else 0,
        1 if cat_id is not None else 0,
    ])
    confidence = round(0.20 + (extracted_signals * 0.14), 2) if extracted_signals > 0 else 0.10
    if raw_text.strip():
        confidence = round(min(0.82, confidence + 0.08), 2)

    if extracted_signals == 0:
        warning = (
            f"{fallback_reason} Belgeden otomatik veri okunamadı; lütfen tutar, tarih ve kategoriyi manuel olarak doldurun."
        )
    else:
        warning = (
            f"{fallback_reason} Çevrimdışı ayrıştırıcı ile kısmi veri çıkarıldı; kaydetmeden önce alanları kontrol edip düzenleyebilirsiniz."
        )

    return {
        'success': True,
        'merchant': merchant,
        'amount': amount,
        'date': date_str,
        'category_id': cat_id,
        'category_name': cat_name,
        'description': description,
        'confidence': confidence,
        'engine': 'fallback',
        'warning': warning,
    }


def scan_receipt(
    file_bytes: bytes,
    filename: str,
    categories: List[Dict[str, Any]],
    content_type: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    timeout: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Fiş / e-Arşiv / e-Fatura dosyasını hibrit motorla tarar.
    1. GEMINI_API_KEY tanımlıysa birincil Vision AI motorunu kullanır.
    2. Anahtar yoksa veya bağlantı/API hatası oluşursa çevrimdışı yedek motora zarifçe düşer.
    """
    if not file_bytes:
        return {
            'success': False,
            'error': 'Yüklenen dosya boş.',
        }

    if len(file_bytes) > MAX_RECEIPT_SCAN_BYTES:
        return {
            'success': False,
            'error': 'Yüklenen dosya boyutu çok büyük (maksimum 5 MB).',
        }

    if not is_allowed_scan_file(filename):
        return {
            'success': False,
            'error': 'Desteklenmeyen dosya türü. Lütfen JPG, PNG, WEBP veya PDF yükleyin.',
        }

    mime_type = detect_mime_type(filename, content_type)
    effective_key = (api_key if api_key is not None else Config.GEMINI_API_KEY or '').strip()
    effective_model = (model or Config.GEMINI_MODEL or 'gemini-2.5-flash').strip()
    effective_timeout = timeout if timeout is not None else Config.RECEIPT_SCAN_TIMEOUT

    if effective_key:
        try:
            return _scan_with_gemini(
                file_bytes=file_bytes,
                mime_type=mime_type,
                categories=categories,
                api_key=effective_key,
                model=effective_model,
                timeout=effective_timeout,
            )
        except GeminiAPIError as exc:
            err_summary = f"Vision AI hatası (HTTP {exc.status_code}: {exc.short_message})"
            print(f"[Moneta ReceiptScanner Gemini Fallback]: {err_summary}")
            return _scan_with_fallback(
                file_bytes=file_bytes,
                filename=filename,
                mime_type=mime_type,
                categories=categories,
                fallback_reason=f"{err_summary}.",
            )
        except Exception as exc:
            short_exc = str(exc).strip().splitlines()[0][:140] if str(exc).strip() else type(exc).__name__
            err_summary = f"Vision AI hatası ({type(exc).__name__}: {short_exc})"
            print(f"[Moneta ReceiptScanner Gemini Fallback]: {err_summary}")
            return _scan_with_fallback(
                file_bytes=file_bytes,
                filename=filename,
                mime_type=mime_type,
                categories=categories,
                fallback_reason=f"{err_summary}.",
            )

    return _scan_with_fallback(
        file_bytes=file_bytes,
        filename=filename,
        mime_type=mime_type,
        categories=categories,
        fallback_reason="GEMINI_API_KEY tanımlı değil.",
    )
