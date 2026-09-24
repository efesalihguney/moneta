import pyodbc

# Bilgisayarındaki yerel SQL Server bağlantı dizgisi
connection_string = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=DESKTOP-8BAIFN8;"
    "DATABASE=wealth_tracker_db;"
    "Trusted_Connection=yes;"
    "TrustServerCertificate=yes;"
)

try:
    conn = pyodbc.connect(connection_string)
    cursor = conn.cursor()
    
    # Oluşturduğumuz tabloları sorgulayalım
    cursor.execute("""
        SELECT TABLE_NAME 
        FROM INFORMATION_SCHEMA.TABLES 
        WHERE TABLE_TYPE = 'BASE TABLE'
    """)
    
    tables = cursor.fetchall()
    print("\n Veritabanina basariyla baglanildi!\n")
    print("Mevcut Tablolar:")
    for table in tables:
        print(f" - {table[0]}")
        
    cursor.close()
    conn.close()

except Exception as e:
    print("\nODBC Driver 17 bulunamadi, genel surucu deneniyor...")
    try:
        fallback_string = (
            "DRIVER={SQL Server};"
            "SERVER=DESKTOP-8BAIFN8;"
            "DATABASE=wealth_tracker_db;"
            "Trusted_Connection=yes;"
        )
        conn = pyodbc.connect(fallback_string)
        cursor = conn.cursor()
        cursor.execute("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE = 'BASE TABLE'")
        tables = cursor.fetchall()
        print(" Genel SQL Server surucusu ile basariyla baglanildi!\n")
        print("Mevcut Tablolar:")
        for table in tables:
            print(f" - {table[0]}")
        conn.close()
    except Exception as fallback_error:
        print("\nBaglanti Hatasi:")
        print(fallback_error)