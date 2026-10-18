import pyodbc
from config import Config

CONNECTION_STRING = Config.get_connection_string()

def get_db_connection(database=None, autocommit=False):
    """
    Veritabanı bağlantısı açar ve döner.
    Yapılandırılan DB_SERVER erişilemezse (örn. farklı bir bilgisayarda klonlandığında)
    sırasıyla localhost, localhost\\SQLEXPRESS, ., (localdb)\\MSSQLLocalDB örneklerini
    otomatik dener ve başarılı olan sunucuyu Config._resolved_server içine kilitler.
    """
    if Config._resolved_server:
        try:
            conn_str = Config.get_connection_string(
                driver=Config._resolved_driver,
                server=Config._resolved_server,
                database=database
            )
            return pyodbc.connect(conn_str, autocommit=autocommit)
        except Exception:
            Config._resolved_server = None
            Config._resolved_driver = None

    last_err = None
    drivers_to_try = [Config.DB_DRIVER, '{ODBC Driver 18 for SQL Server}', '{SQL Server}']
    seen_drivers = []
    for d in drivers_to_try:
        if d and d not in seen_drivers:
            seen_drivers.append(d)

    for srv in Config.get_candidate_servers():
        for drv in seen_drivers:
            try:
                if drv == '{SQL Server}':
                    conn_str = Config.get_fallback_connection_string(server=srv, database=database)
                else:
                    conn_str = Config.get_connection_string(driver=drv, server=srv, database=database)
                conn = pyodbc.connect(conn_str, timeout=3, autocommit=autocommit)
                Config._resolved_server = srv
                Config._resolved_driver = drv
                return conn
            except Exception as e:
                last_err = e

    raise last_err
