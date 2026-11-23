import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

class Config:
    """Moneta application configuration."""
    SECRET_KEY = os.getenv('FLASK_SECRET_KEY', 'super_secret_moneta_key_change_in_production')
    FLASK_ENV = os.getenv('FLASK_ENV', 'development')
    ALLOW_AUTO_LOGIN = os.getenv('ALLOW_AUTO_LOGIN', '0').lower() in ('1', 'true', 'yes')
    MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5 MB upload sınırı

    # Session Cookie Security
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    SESSION_COOKIE_SECURE = os.getenv('SESSION_COOKIE_SECURE', 'false').lower() == 'true'

    # Merkezi Finansal Yedek Sabitleri
    DEFAULT_USD_TRY_FALLBACK = float(os.getenv('DEFAULT_USD_TRY_FALLBACK', '50.60'))
    TUFE_RATES = {
        '1M': float(os.getenv('TUFE_1M', '2.8')),
        '3M': float(os.getenv('TUFE_3M', '8.9')),
        '6M': float(os.getenv('TUFE_6M', '19.5')),
    }

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
