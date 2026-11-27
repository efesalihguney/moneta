"""
Moneta Çift Motorlu (Dual-Engine) Veritabanı Katmanı
- DATABASE_URL tanımlıysa: Supabase / PostgreSQL (psycopg2) + T-SQL -> PostgreSQL uyumluluk katmanı
- DATABASE_URL boşsa: Yerel / Bulut Microsoft SQL Server (pyodbc) + akıllı sunucu keşfi
"""

import re
from decimal import Decimal
from config import Config

try:
    import pyodbc
except ImportError:
    pyodbc = None

try:
    import psycopg2
except ImportError:
    psycopg2 = None

CONNECTION_STRING = Config.get_connection_string()

_BOOL_COLUMNS = ('is_paid', 'is_active', 'auto_apply', 'email_notifications')
_BOOL_COL_PATTERN = r'(?:[A-Za-z_][A-Za-z0-9_]*\.)?(?:' + '|'.join(_BOOL_COLUMNS) + r')'


def is_postgres() -> bool:
    """Aktif veritabanı motorunun PostgreSQL (Supabase) olup olmadığını döner."""
    return Config.is_postgres()


def _replace_qmark_outside_quotes(sql: str, has_params: bool = True) -> str:
    """Tek tırnaklı SQL dizgeleri dışındaki ? yer tutucularını %s yapar; gerekirse % karakterini %% olarak kaçar."""
    out = []
    in_single = False
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "'":
            out.append(ch)
            if in_single and i + 1 < n and sql[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            in_single = not in_single
        elif ch == '?' and not in_single:
            out.append('%s')
        elif ch == '%' and has_params:
            out.append('%%')
        else:
            out.append(ch)
        i += 1
    return ''.join(out)


def _convert_insert_bool_literals(sql: str) -> str:
    """INSERT INTO ... (cols) VALUES (...) içindeki boolean sütunlara denk gelen 0/1 sabitlerini FALSE/TRUE yapar."""
    m = re.search(
        r'INSERT\s+INTO\s+[A-Za-z0-9_."]+\s*\(([^)]+)\)\s*VALUES\s*\(([^)]+)\)',
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return sql

    cols = [c.strip().strip('"').lower() for c in m.group(1).split(',')]
    # Basit VALUES listesini parantez derinliğine göre böl
    raw_vals = m.group(2)
    vals = []
    cur = []
    depth = 0
    in_str = False
    for ch in raw_vals:
        if ch == "'":
            in_str = not in_str
            cur.append(ch)
        elif not in_str and ch == '(':
            depth += 1
            cur.append(ch)
        elif not in_str and ch == ')':
            depth -= 1
            cur.append(ch)
        elif not in_str and depth == 0 and ch == ',':
            vals.append(''.join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        vals.append(''.join(cur))

    if len(cols) != len(vals):
        return sql

    changed = False
    for idx, col in enumerate(cols):
        if col in _BOOL_COLUMNS:
            stripped = vals[idx].strip()
            if stripped == '1':
                vals[idx] = vals[idx].replace('1', 'TRUE', 1)
                changed = True
            elif stripped == '0':
                vals[idx] = vals[idx].replace('0', 'FALSE', 1)
                changed = True

    if not changed:
        return sql

    new_vals = ','.join(vals)
    return sql[:m.start(2)] + new_vals + sql[m.end(2):]


def translate_tsql_to_postgres(sql: str, has_params: bool = True) -> str:
    """
    Mevcut T-SQL sorgularını PostgreSQL sözdizimine çevirir:
    - EXEC sp_get_user_budget_health @user_id = ?, @month = ?, @year = ? -> SELECT * FROM sp_get_user_budget_health(%s, %s, %s)
    - OUTPUT INSERTED.id -> RETURNING id
    - SELECT TOP N ... -> SELECT ... LIMIT N
    - CAST(GETDATE() AS DATE) -> CURRENT_DATE, GETDATE() -> CURRENT_TIMESTAMP
    - DATEDIFF(day, start, end) -> ((end)::date - (start)::date)
    - MONTH(expr) / YEAR(expr) -> EXTRACT(MONTH/YEAR FROM expr)::INT
    - ISNULL(a, b) -> COALESCE(a, b) ve boolean sütun uyumu (0/1 -> FALSE/TRUE)
    - ? -> %s
    """
    q = sql.strip()

    # 1. EXEC sp_get_user_budget_health @user_id = ?, @month = ?, @year = ?
    sp_match = re.match(
        r'^EXEC\s+([A-Za-z_][A-Za-z0-9_]*)\s+(.+)$',
        q,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if sp_match:
        fn_name = sp_match.group(1)
        raw_args = sp_match.group(2).strip().rstrip(';')
        arg_vals = []
        for part in raw_args.split(','):
            if '=' in part:
                arg_vals.append(part.split('=', 1)[1].strip())
            else:
                arg_vals.append(part.strip())
        q = f"SELECT * FROM {fn_name}({', '.join(arg_vals)})"

    # 2. Tarih fonksiyonları
    q = re.sub(r'CAST\s*\(\s*GETDATE\s*\(\s*\)\s+AS\s+DATE\s*\)', 'CURRENT_DATE', q, flags=re.IGNORECASE)
    q = re.sub(r'\bGETDATE\s*\(\s*\)', 'CURRENT_TIMESTAMP', q, flags=re.IGNORECASE)

    # 3. DATEDIFF(day, start, end) -> ((end)::date - (start)::date)
    q = re.sub(
        r'DATEDIFF\s*\(\s*day\s*,\s*([^,]+?)\s*,\s*([^)]+?)\s*\)',
        r'((\2)::date - (\1)::date)',
        q,
        flags=re.IGNORECASE,
    )

    # 3b. DATEADD(day|month|year, N, expr) -> ((expr)::date + (N || ' unit')::interval)
    q = re.sub(
        r'DATEADD\s*\(\s*(day|month|year)\s*,\s*([^,]+?)\s*,\s*([^)]+?)\s*\)',
        r"((\3)::timestamp + ((\2)::text || ' \1')::interval)",
        q,
        flags=re.IGNORECASE,
    )

    # 3c. CONVERT(DATE, expr) -> (expr)::date
    q = re.sub(r'\bCONVERT\s*\(\s*DATE\s*,\s*([^)]+?)\s*\)', r'(\1)::date', q, flags=re.IGNORECASE)

    # 4. MONTH(expr), YEAR(expr) ve DATEPART(year|month|day, expr)
    q = re.sub(r'\bDATEPART\s*\(\s*(year|month|day)\s*,\s*([^)]+?)\s*\)', r'EXTRACT(\1 FROM \2)::INT', q, flags=re.IGNORECASE)
    q = re.sub(r'\bMONTH\s*\(\s*([^)]+?)\s*\)', r'EXTRACT(MONTH FROM \1)::INT', q, flags=re.IGNORECASE)
    q = re.sub(r'\bYEAR\s*\(\s*([^)]+?)\s*\)', r'EXTRACT(YEAR FROM \1)::INT', q, flags=re.IGNORECASE)

    # 5. Boolean sütunlar için ISNULL ve 0/1 karşılaştırma dönüşümü
    q = re.sub(
        rf'\bISNULL\s*\(\s*({_BOOL_COL_PATTERN})\s*,\s*0\s*\)',
        r'COALESCE(\1, FALSE)',
        q,
        flags=re.IGNORECASE,
    )
    q = re.sub(
        rf'\bISNULL\s*\(\s*({_BOOL_COL_PATTERN})\s*,\s*1\s*\)',
        r'COALESCE(\1, TRUE)',
        q,
        flags=re.IGNORECASE,
    )
    q = re.sub(r'\bISNULL\s*\(', 'COALESCE(', q, flags=re.IGNORECASE)

    # COALESCE(bool_col, FALSE/TRUE) = 0/1
    q = re.sub(
        rf'(COALESCE\s*\(\s*{_BOOL_COL_PATTERN}\s*,\s*(?:TRUE|FALSE)\s*\))\s*=\s*0\b',
        r'\1 = FALSE',
        q,
        flags=re.IGNORECASE,
    )
    q = re.sub(
        rf'(COALESCE\s*\(\s*{_BOOL_COL_PATTERN}\s*,\s*(?:TRUE|FALSE)\s*\))\s*=\s*1\b',
        r'\1 = TRUE',
        q,
        flags=re.IGNORECASE,
    )
    # Doğrudan bool_col = / != / <> 0/1 (WHERE ve SET ifadeleri)
    q = re.sub(rf'({_BOOL_COL_PATTERN})\s*(!=|<>)\s*0\b', r'\1 \2 FALSE', q, flags=re.IGNORECASE)
    q = re.sub(rf'({_BOOL_COL_PATTERN})\s*(!=|<>)\s*1\b', r'\1 \2 TRUE', q, flags=re.IGNORECASE)
    q = re.sub(rf'({_BOOL_COL_PATTERN})\s*=\s*0\b', r'\1 = FALSE', q, flags=re.IGNORECASE)
    q = re.sub(rf'({_BOOL_COL_PATTERN})\s*=\s*1\b', r'\1 = TRUE', q, flags=re.IGNORECASE)

    # 6. OUTPUT INSERTED.id -> RETURNING id
    if re.search(r'\bOUTPUT\s+INSERTED\.id\b', q, flags=re.IGNORECASE):
        q = re.sub(r'\s*\bOUTPUT\s+INSERTED\.id\b\s*', ' ', q, flags=re.IGNORECASE)
        q = q.rstrip().rstrip(';') + ' RETURNING id'

    # 7. INSERT içindeki boolean 0/1 sabitleri
    q = _convert_insert_bool_literals(q)

    # 8. SELECT [DISTINCT] TOP N ... -> SELECT [DISTINCT] ... LIMIT N
    top_match = re.match(r'^SELECT\s+(DISTINCT\s+)?TOP\s+(\d+)\s+(.+)$', q, flags=re.IGNORECASE | re.DOTALL)
    if top_match:
        distinct_kw = top_match.group(1) or ''
        limit_n = top_match.group(2)
        rest = top_match.group(3).rstrip().rstrip(';')
        q = f"SELECT {distinct_kw}{rest} LIMIT {limit_n}"

    # 9. Parametre yer tutucuları (? -> %s)
    q = _replace_qmark_outside_quotes(q, has_params=has_params)

    return q


def _adapt_params_for_pg(original_sql: str, params):
    """PostgreSQL BOOLEAN sütunlarına gönderilen 0/1 int parametrelerini bool tipine çevirir."""
    if params is None:
        return None
    params_list = list(params)
    if not params_list:
        return tuple(params_list)

    # INSERT INTO tbl (c1, c2, ...) VALUES (?, ?, ...)
    ins_match = re.search(
        r'INSERT\s+INTO\s+[A-Za-z0-9_."]+\s*\(([^)]+)\)\s*(?:OUTPUT\s+INSERTED\.id\s*)?VALUES\s*\(([^)]+)\)',
        original_sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if ins_match:
        cols = [c.strip().strip('"').lower() for c in ins_match.group(1).split(',')]
        val_tokens = [v.strip() for v in ins_match.group(2).split(',')]
        if len(cols) == len(val_tokens):
            param_idx = 0
            for col, tok in zip(cols, val_tokens):
                if '?' in tok:
                    if col in _BOOL_COLUMNS and param_idx < len(params_list) and isinstance(params_list[param_idx], int):
                        params_list[param_idx] = bool(params_list[param_idx])
                    param_idx += tok.count('?')
            return tuple(params_list)

    # UPDATE tbl SET col1 = ?, is_paid = ? WHERE ...
    if re.match(r'^\s*UPDATE\b', original_sql, flags=re.IGNORECASE):
        assignments = re.findall(r'([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*\?', original_sql)
        for idx, col_expr in enumerate(assignments):
            col_name = col_expr.split('.')[-1].lower()
            if col_name in _BOOL_COLUMNS and idx < len(params_list) and isinstance(params_list[idx], int):
                params_list[idx] = bool(params_list[idx])

    return tuple(params_list)


def _convert_pg_row(row):
    """psycopg2 NUMERIC/DECIMAL sütunlarından gelen Decimal nesnelerini otomatik olarak float'a dönüştürür."""
    if row is None:
        return None
    if isinstance(row, tuple):
        return tuple(float(v) if isinstance(v, Decimal) else v for v in row)
    if isinstance(row, list):
        return [float(v) if isinstance(v, Decimal) else v for v in row]
    return row


class PgCursorWrapper:
    """psycopg2 imlecini pyodbc imleç arayüzüyle %100 uyumlu hale getiren sarmalayıcı."""

    def __init__(self, pg_cursor):
        self._cursor = pg_cursor

    def execute(self, sql: str, params=None):
        has_params = params is not None
        pg_sql = translate_tsql_to_postgres(sql, has_params=has_params)
        if has_params:
            adapted = _adapt_params_for_pg(sql, params)
            self._cursor.execute(pg_sql, adapted)
        else:
            self._cursor.execute(pg_sql)
        return self

    def executemany(self, sql: str, seq_of_params):
        pg_sql = translate_tsql_to_postgres(sql, has_params=True)
        adapted_seq = [_adapt_params_for_pg(sql, p) for p in seq_of_params]
        self._cursor.executemany(pg_sql, adapted_seq)
        return self

    def fetchone(self):
        return _convert_pg_row(self._cursor.fetchone())

    def fetchall(self):
        rows = self._cursor.fetchall()
        return [_convert_pg_row(r) for r in rows]

    def fetchmany(self, size=None):
        rows = self._cursor.fetchmany(size) if size is not None else self._cursor.fetchmany()
        return [_convert_pg_row(r) for r in rows]

    def close(self):
        return self._cursor.close()

    @property
    def description(self):
        return self._cursor.description

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def __iter__(self):
        for row in self._cursor:
            yield _convert_pg_row(row)


class PgConnectionWrapper:
    """psycopg2 bağlantısını pyodbc bağlantı arayüzüyle uyumlu hale getiren sarmalayıcı."""

    def __init__(self, pg_conn):
        self._conn = pg_conn

    def cursor(self):
        return PgCursorWrapper(self._conn.cursor())

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()


def get_db_connection(database=None, autocommit=False):
    """
    Veritabanı bağlantısı açar ve döner.
    - DATABASE_URL tanımlıysa Supabase/PostgreSQL (psycopg2) bağlantısı döner.
    - Tanımlı değilse yerel/bulut MSSQL (pyodbc) bağlantısını akıllı sunucu keşfiyle açar.
    """
    if Config.is_postgres():
        if psycopg2 is None:
            raise RuntimeError("PostgreSQL bağlantısı için 'psycopg2-binary' paketi kurulu olmalıdır.")
        db_url = Config.get_database_url().strip().strip('"').strip("'").strip()
        pg_conn = psycopg2.connect(db_url, connect_timeout=10)
        pg_conn.autocommit = autocommit
        return PgConnectionWrapper(pg_conn)

    if pyodbc is None:
        raise RuntimeError(
            "MSSQL bağlantısı için 'pyodbc' kurulu olmalıdır. "
            "Bulut/Linux (Supabase) ortamında çalışmak için DATABASE_URL ortam değişkenini tanımlayın."
        )

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
    conn_timeout = 8 if Config.DB_USER else 3
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
                conn = pyodbc.connect(conn_str, timeout=conn_timeout, autocommit=autocommit)
                Config._resolved_server = srv
                Config._resolved_driver = drv
                return conn
            except Exception as e:
                last_err = e

    raise last_err
