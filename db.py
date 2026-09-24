import pyodbc

CONNECTION_STRING = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=DESKTOP-8BAIFN8;"
    "DATABASE=wealth_tracker_db;"
    "Trusted_Connection=yes;"
    "TrustServerCertificate=yes;"
)

def get_db_connection():
    """Veritabanı bağlantısı açar ve döner."""
    try:
        conn = pyodbc.connect(CONNECTION_STRING)
        return conn
    except Exception:
        # Alternatif sürücü denemesi
        fallback = (
            "DRIVER={SQL Server};"
            "SERVER=DESKTOP-8BAIFN8;"
            "DATABASE=wealth_tracker_db;"
            "Trusted_Connection=yes;"
        )
        return pyodbc.connect(fallback)
