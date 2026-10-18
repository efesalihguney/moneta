import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

class Config:
    """Moneta application configuration."""
    SECRET_KEY = os.getenv('FLASK_SECRET_KEY', 'super_secret_moneta_key_change_in_production')
    FLASK_ENV = os.getenv('FLASK_ENV', 'development')

    # Session Cookie Security
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    SESSION_COOKIE_SECURE = os.getenv('SESSION_COOKIE_SECURE', 'false').lower() == 'true'

    # MSSQL Database Settings
    DB_DRIVER = os.getenv('DB_DRIVER', '{ODBC Driver 17 for SQL Server}')
    DB_SERVER = os.getenv('DB_SERVER', 'DESKTOP-8BAIFN8')
    DB_DATABASE = os.getenv('DB_DATABASE', 'wealth_tracker_db')
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
        seen = set()
        ordered = []
        for srv in [cls.DB_SERVER] + cls.CANDIDATE_SERVERS:
            if srv and srv not in seen:
                seen.add(srv)
                ordered.append(srv)
        return ordered

    @classmethod
    def get_connection_string(cls, driver=None, server=None, database=None):
        drv = driver or cls._resolved_driver or cls.DB_DRIVER
        srv = server or cls._resolved_server or cls.DB_SERVER
        db_name = database or cls.DB_DATABASE
        return (
            f"DRIVER={drv};"
            f"SERVER={srv};"
            f"DATABASE={db_name};"
            f"Trusted_Connection={cls.DB_TRUSTED_CONNECTION};"
            f"TrustServerCertificate={cls.DB_TRUST_SERVER_CERTIFICATE};"
        )

    @classmethod
    def get_fallback_connection_string(cls, server=None, database=None):
        srv = server or cls._resolved_server or cls.DB_SERVER
        db_name = database or cls.DB_DATABASE
        return (
            f"DRIVER={{SQL Server}};"
            f"SERVER={srv};"
            f"DATABASE={db_name};"
            f"Trusted_Connection={cls.DB_TRUSTED_CONNECTION};"
        )
