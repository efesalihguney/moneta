import hashlib
import os
import re
import secrets
from urllib.parse import unquote
from dotenv import load_dotenv

# Load environment variables from .env file (override stale OS/process values)
load_dotenv(override=True)


def _strip_wrapping_quotes_and_ws(value: str) -> str:
    s = str(value or '').strip()
    while len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
        s = s[1:-1].strip()
    return s.strip(' \t\r\n"\'')


def parse_postgres_url(raw_url: str) -> dict:
    """
    PostgreSQL / Supabase bağlantı adresini güvenli şekilde bileşenlerine ayırır.
    - Baştaki/sondaki boşlukları, \\r, \\n ve tırnakları temizler.
    - Yanlışlıkla yapışmış 'DATABASE_URL=' önekini kaldırır.
    - Şema sonrasını sondan ilk '@' ile bölerek şifredeki '@', '?' ve '#' karakterlerini korur.
    - Sonda kalan boş '?' veya '&' parametrelerini temizler; sslmode yoksa 'require' atar.
    """
    s = _strip_wrapping_quotes_and_ws(raw_url)
    if not s:
        return {}

    s = re.sub(r'^\s*DATABASE_URL\s*=\s*', '', s, flags=re.IGNORECASE)
    s = _strip_wrapping_quotes_and_ws(s)
    # Satır içi kopyalama kaynaklı \r ve \n karakterlerini temizle
    s = s.replace('\r', '').replace('\n', '').strip()
    if not s:
        return {}

    m = re.match(r'^postgres(?:ql)?://(.*)$', s, flags=re.IGNORECASE)
    if not m:
        raise ValueError("Geçersiz PostgreSQL bağlantı şeması (postgres:// veya postgresql:// bekleniyor).")

    remainder = m.group(1)
    if '@' in remainder:
        userinfo, host_and_rest = remainder.rsplit('@', 1)
        if ':' in userinfo:
            raw_user, raw_pass = userinfo.split(':', 1)
            user = unquote(raw_user)
            password = unquote(raw_pass)
        else:
            user = unquote(userinfo)
            password = ''
    else:
        user = ''
        password = ''
        host_and_rest = remainder

    # Host/path/query kısmındaki olası #fragment parçasını at
    host_and_rest = host_and_rest.split('#', 1)[0]
    host_path, _, query_str = host_and_rest.partition('?')
    host_port, _, dbname_raw = host_path.partition('/')

    dbname = unquote(dbname_raw.strip().strip('/')) or 'postgres'

    host_port = host_port.strip()
    port = 5432
    if host_port.startswith('['):
        # IPv6 [::1]:5432
        end_bracket = host_port.find(']')
        if end_bracket != -1:
            host = host_port[1:end_bracket]
            after = host_port[end_bracket + 1:].strip()
            if after.startswith(':') and after[1:].strip():
                port = int(after[1:].strip())
        else:
            host = host_port.strip('[]')
    elif ':' in host_port:
        h_part, p_part = host_port.rsplit(':', 1)
        host = h_part.strip()
        if p_part.strip():
            port = int(p_part.strip())
    else:
        host = host_port

    if not host:
        raise ValueError("PostgreSQL bağlantı adresinde sunucu (host) bulunamadı.")

    query_params = {}
    for seg in query_str.split('&'):
        seg = seg.strip()
        if not seg:
            continue
        if '=' in seg:
            k, v = seg.split('=', 1)
            k = k.strip()
            v = v.strip()
            if k and v:
                query_params[k.lower()] = v

    sslmode = query_params.get('sslmode') or 'require'

    return {
        'host': host,
        'port': port,
        'dbname': dbname,
        'user': user,
        'password': password,
        'sslmode': sslmode,
        'options_dict': query_params,
    }


class Config:
    """Moneta application configuration."""
    VERCEL = os.getenv('VERCEL', '').strip()
    VERCEL_ENV = os.getenv('VERCEL_ENV', '').strip()
    FLASK_ENV = os.getenv('FLASK_ENV', 'development').strip()
    _IS_PROD_STARTUP = (
        bool(VERCEL)
        or (VERCEL_ENV.lower() == 'production')
        or (FLASK_ENV.lower() == 'production')
    )

    _KNOWN_WEAK_SECRET_KEYS = frozenset({
        'super_secret_moneta_key_change_in_production',
        'change_this_to_a_secure_random_string_in_production',
        'moneta_production_secret_key_2026',
    })
    _DEV_EPHEMERAL_SECRET_KEY = None

    SECRET_KEY = None  # Sınıf tanımı sonunda Config.get_secret_key() ile doğrulanarak atanır
    ALLOW_AUTO_LOGIN = False if _IS_PROD_STARTUP else (
        os.getenv('ALLOW_AUTO_LOGIN', '0').lower() in ('1', 'true', 'yes')
    )
    MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5 MB upload sınırı
    TEMP_UPLOAD_FOLDER = '/tmp/moneta_uploads'

    # Session Cookie Security
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    SESSION_COOKIE_SECURE = _IS_PROD_STARTUP

    # Merkezi Finansal Yedek Sabitleri
    DEFAULT_USD_TRY_FALLBACK = float(os.getenv('DEFAULT_USD_TRY_FALLBACK', '50.60'))
    TUFE_RATES = {
        '1M': float(os.getenv('TUFE_1M', '2.8')),
        '3M': float(os.getenv('TUFE_3M', '8.9')),
        '6M': float(os.getenv('TUFE_6M', '19.5')),
    }

    # Akıllı Fiş & Fatura Okuma (Gemini Vision AI) Ayarları
    GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '').strip()
    GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash').strip() or 'gemini-2.5-flash'
    RECEIPT_SCAN_TIMEOUT = int(os.getenv('RECEIPT_SCAN_TIMEOUT', '25'))
    GEMINI_PAID_TIER_CONFIRMED = os.getenv('GEMINI_PAID_TIER_CONFIRMED', 'false').strip().lower() == 'true'

    # Supabase / PostgreSQL Bağlantı Adresi (Tanımlıysa birincil motor PostgreSQL olur)
    DATABASE_URL = os.getenv('DATABASE_URL', '').strip().strip('"').strip("'").strip()

    # MSSQL Database Settings (Windows Trusted Auth veya Bulut/Linux SQL Auth destekli)
    DB_DRIVER = os.getenv('DB_DRIVER', '{ODBC Driver 17 for SQL Server}')
    DB_SERVER = os.getenv('DB_SERVER', 'DESKTOP-8BAIFN8')
    DB_DATABASE = os.getenv('DB_DATABASE', 'wealth_tracker_db')
    DB_USER = os.getenv('DB_USER', '').strip()
    DB_PASSWORD = os.getenv('DB_PASSWORD', '')
    DB_TRUSTED_CONNECTION = os.getenv('DB_TRUSTED_CONNECTION', 'yes')
    DB_TRUST_SERVER_CERTIFICATE = os.getenv('DB_TRUST_SERVER_CERTIFICATE', 'yes')

    # Akıllı Yerel SQL Server Keşif Önbelleği (Auto-Discovery Cache)
    _resolved_server = None
    _resolved_driver = None

    CANDIDATE_SERVERS = [
        os.getenv('DB_SERVER', 'DESKTOP-8BAIFN8'),
        'localhost',
        r'localhost\SQLEXPRESS',
        '.',
        r'.\SQLEXPRESS',
        r'(localdb)\MSSQLLocalDB',
    ]

    @classmethod
    def normalize_database_url(cls, raw_url=None):
        """
        Supabase / PostgreSQL DATABASE_URL değerini temizler ve normalize eder:
        - DATABASE_URL= önekini, tırnakları, \\r/\\n karakterlerini ve boş ?/& parametrelerini temizler.
        - postgres:// önekini postgresql:// olarak dönüştürür; sslmode yoksa sslmode=require ekler.
        """
        candidate = cls.DATABASE_URL if raw_url is None else raw_url
        cleaned = _strip_wrapping_quotes_and_ws(candidate or '')
        if not cleaned:
            return ''
        cleaned = re.sub(r'^\s*DATABASE_URL\s*=\s*', '', cleaned, flags=re.IGNORECASE)
        cleaned = _strip_wrapping_quotes_and_ws(cleaned).replace('\r', '').replace('\n', '').strip()
        if not cleaned:
            return ''
        if cleaned.lower().startswith('postgres://'):
            cleaned = 'postgresql://' + cleaned[len('postgres://'):]

        # Sonda kalan boş ? veya & karakterlerini temizle
        if '@' in cleaned:
            left, right = cleaned.rsplit('@', 1)
        else:
            left, right = '', cleaned

        host_path, has_q, q_str = right.partition('?')
        valid_pairs = []
        has_sslmode = False
        if has_q:
            for seg in q_str.split('&'):
                seg = seg.strip()
                if not seg or '=' not in seg:
                    continue
                k, v = seg.split('=', 1)
                k, v = k.strip(), v.strip()
                if k and v:
                    if k.lower() == 'sslmode':
                        has_sslmode = True
                    valid_pairs.append(f"{k}={v}")
        if not has_sslmode:
            valid_pairs.append('sslmode=require')

        rebuilt_right = f"{host_path}?{'&'.join(valid_pairs)}" if valid_pairs else host_path
        return f"{left}@{rebuilt_right}" if left else rebuilt_right

    @classmethod
    def get_database_url(cls):
        return cls.normalize_database_url(cls.DATABASE_URL)

    @classmethod
    def get_postgres_params(cls, raw_url=None) -> dict:
        target = cls.DATABASE_URL if raw_url is None else raw_url
        return parse_postgres_url(target)

    @classmethod
    def get_db_host_display(cls):
        """Kimlik bilgisi (kullanıcı adı/şifre) sızdırmadan aktif veritabanı sunucusunu (host:port) döner."""
        raw = cls.DATABASE_URL
        if raw and str(raw).strip():
            try:
                pg = parse_postgres_url(raw)
                if pg.get('host'):
                    return f"{pg['host']}:{pg['port']}" if pg.get('port') else pg['host']
            except Exception:
                return 'unknown'
        return cls._resolved_server or cls.DB_SERVER or 'local'

    @classmethod
    def is_vercel(cls):
        """Vercel Serverless çalışma ortamında olup olmadığını döner."""
        return bool(
            os.getenv('VERCEL', '').strip()
            or os.getenv('VERCEL_ENV', '').strip()
            or cls.VERCEL
            or cls.VERCEL_ENV
        )

    @classmethod
    def is_production(cls):
        """Üretim ortamını tanır: VERCEL ortam değişkeni varsa veya FLASK_ENV=production (veya VERCEL_ENV=production) ise."""
        has_vercel = bool(os.getenv('VERCEL', cls.VERCEL or '').strip())
        vercel_env = os.getenv('VERCEL_ENV', cls.VERCEL_ENV or '').strip().lower()
        flask_env = os.getenv('FLASK_ENV', cls.FLASK_ENV or '').strip().lower()
        return has_vercel or vercel_env == 'production' or flask_env == 'production'

    @classmethod
    def is_weak_secret_key(cls, key: str) -> bool:
        """Anahtarın eksik, 32 karakterden kısa veya bilinen zayıf örneklerden biri olup olmadığını denetler."""
        cleaned = (key or '').strip()
        if not cleaned or len(cleaned) < 32:
            return True
        lowered = cleaned.lower()
        if 'change_this' in lowered:
            return True
        if lowered in {w.lower() for w in cls._KNOWN_WEAK_SECRET_KEYS}:
            return True
        return False

    @classmethod
    def get_secret_key(cls):
        """
        Ortam değişkenlerinden (SECRET_KEY / FLASK_SECRET_KEY) gizli anahtarı okur ve doğrular.
        - Canlı ortamda SECRET_KEY eksik/boşsa ve DATABASE_URL mevcutsa deterministik SHA-256 yedek anahtar üretir.
        - Canlı ortamda hem SECRET_KEY hem DATABASE_URL yoksa veya açıkça zayıf bir anahtar verilmişse RuntimeError fırlatır.
        - Geliştirme ortamında anahtar yoksa süreç başına rastgele secrets.token_hex(32) üretir.
        """
        raw_key = (os.getenv('SECRET_KEY') or os.getenv('FLASK_SECRET_KEY') or '').strip()
        if cls.is_production():
            if not raw_key:
                db_seed = _strip_wrapping_quotes_and_ws(os.getenv('DATABASE_URL', cls.DATABASE_URL or ''))
                if db_seed:
                    return hashlib.sha256(f"moneta-prod-fallback::{db_seed}".encode('utf-8')).hexdigest()
                raise RuntimeError(
                    "Üretim ortamında FLASK_SECRET_KEY / SECRET_KEY (veya yedek türetim için DATABASE_URL) zorunludur."
                )
            if cls.is_weak_secret_key(raw_key):
                raise RuntimeError(
                    "Üretim ortamında zayıf veya 32 karakterden kısa SECRET_KEY kullanılamaz. "
                    "Üretmek için: python -c \"import secrets; print(secrets.token_hex(32))\""
                )
            return raw_key

        if not raw_key:
            if not cls._DEV_EPHEMERAL_SECRET_KEY:
                cls._DEV_EPHEMERAL_SECRET_KEY = secrets.token_hex(32)
                print(
                    "[UYARI] FLASK_SECRET_KEY tanımlı değil; geliştirme ortamı için süreç başına "
                    "rastgele anahtar üretildi (oturumlar yeniden başlatmada sıfırlanır)."
                )
            return cls._DEV_EPHEMERAL_SECRET_KEY

        return raw_key

    @classmethod
    def get_session_cookie_secure(cls) -> bool:
        """Üretimde SESSION_COOKIE_SECURE varsayılan True, yerel HTTP geliştirmede False döner."""
        raw = os.getenv('SESSION_COOKIE_SECURE')
        if raw is not None and raw.strip() != '':
            return raw.strip().lower() in ('1', 'true', 'yes')
        return cls.is_production()

    @classmethod
    def is_gemini_paid_tier_confirmed(cls) -> bool:
        """GEMINI_PAID_TIER_CONFIRMED ortam değişkeninin 'true' olup olmadığını döner."""
        raw = os.getenv('GEMINI_PAID_TIER_CONFIRMED')
        if raw is not None:
            return raw.strip().lower() == 'true'
        return bool(cls.GEMINI_PAID_TIER_CONFIRMED)

    @classmethod
    def is_auto_login_enabled(cls):
        """Production ortamında ALLOW_AUTO_LOGIN varsayılan olarak kapalıdır."""
        if cls.is_production():
            return False
        raw = os.getenv('ALLOW_AUTO_LOGIN')
        if raw is not None:
            return raw.strip().lower() in ('1', 'true', 'yes')
        return bool(cls.ALLOW_AUTO_LOGIN)

    @classmethod
    def resolve_upload_folder(cls, app_root_path=None):
        """
        Vercel veya salt-okunur dosya sistemi algılandığında /tmp/moneta_uploads dizinine yönlendirir.
        Dizin oluşturma hatalarını (OSError) yakalayarak uygulama açılış çökmesini önler.
        """
        if cls.is_vercel():
            target = cls.TEMP_UPLOAD_FOLDER
            try:
                os.makedirs(target, exist_ok=True)
            except OSError:
                pass
            return target

        base_root = app_root_path or os.path.dirname(os.path.abspath(__file__))
        local_target = os.path.join(base_root, 'uploads', 'receipts')
        try:
            os.makedirs(local_target, exist_ok=True)
            if not os.access(local_target, os.W_OK):
                raise OSError(f"Upload dizini yazılabilir değil: {local_target}")
            return local_target
        except OSError:
            fallback = cls.TEMP_UPLOAD_FOLDER
            try:
                os.makedirs(fallback, exist_ok=True)
            except OSError:
                pass
            return fallback

    @classmethod
    def is_postgres(cls):
        return bool(cls.get_database_url())

    @classmethod
    def get_candidate_servers(cls):
        if cls.DB_USER and cls.DB_SERVER:
            return [cls.DB_SERVER]
        seen = set()
        ordered = []
        for srv in [cls.DB_SERVER] + cls.CANDIDATE_SERVERS:
            if srv and srv not in seen:
                seen.add(srv)
                ordered.append(srv)
        return ordered

    @classmethod
    def _auth_segment(cls, include_trust_cert=True):
        if cls.DB_USER:
            seg = f"UID={cls.DB_USER};PWD={cls.DB_PASSWORD};Trusted_Connection=no;"
        else:
            seg = f"Trusted_Connection={cls.DB_TRUSTED_CONNECTION};"
        if include_trust_cert:
            seg += f"TrustServerCertificate={cls.DB_TRUST_SERVER_CERTIFICATE};"
        return seg

    @classmethod
    def get_connection_string(cls, driver=None, server=None, database=None):
        drv = driver or cls._resolved_driver or cls.DB_DRIVER
        srv = server or cls._resolved_server or cls.DB_SERVER
        db_name = database or cls.DB_DATABASE
        return (
            f"DRIVER={drv};"
            f"SERVER={srv};"
            f"DATABASE={db_name};"
            f"{cls._auth_segment(include_trust_cert=True)}"
        )

    @classmethod
    def get_fallback_connection_string(cls, server=None, database=None):
        srv = server or cls._resolved_server or cls.DB_SERVER
        db_name = database or cls.DB_DATABASE
        return (
            f"DRIVER={{SQL Server}};"
            f"SERVER={srv};"
            f"DATABASE={db_name};"
            f"{cls._auth_segment(include_trust_cert=False)}"
        )


Config.SECRET_KEY = Config.get_secret_key()
Config.SESSION_COOKIE_SECURE = Config.get_session_cookie_secure()
