"""
Moneta Production WSGI Server Starter
Powered by Waitress (Production-grade, Multi-threaded WSGI HTTP Server)
"""

import sys
import os

# Proje dizinini path'e ekle
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from waitress import serve
from app import app
from config import Config


def main():
    host = os.getenv('PROD_HOST', '0.0.0.0')
    port = int(os.getenv('PROD_PORT', 5000))
    threads = int(os.getenv('PROD_THREADS', 8))

    banner = rf"""
======================================================================
  __  __  ___  _   _ _____ _____ _    
 |  \/  |/ _ \| \ | | ____|_   _/ \   
 | |\/| | | | |  \| |  _|   | |/ _ \  
 | |  | | |_| | |\  | |___  | / ___ \ 
 |_|  |_|\___/|_| \_|_____| |_/_/   \_\
 
 Moneta Production WSGI Server (Waitress)
======================================================================
 [OK] Sunucu Adresi      : http://127.0.0.1:{port}
 [OK] Ağ Arayüzü         : http://{host}:{port}
 [OK] Çoklu İş Parçacığı : {threads} Worker Threads
 [OK] Veritabanı Modu    : MSSQL ({Config.DB_DATABASE} @ {Config.DB_SERVER})
 [OK] Güvenlik Katmanı   : Aktif (HTTP Security Headers, Safe Sanitizer)
======================================================================
 Sunucu başlatıldı. Kapatmak için [Ctrl + C] tuşlayın.
    """
    try:
        print(banner)
    except Exception:
        print(f"Moneta WSGI Server started on http://127.0.0.1:{port}")
    serve(app, host=host, port=port, threads=threads)


if __name__ == '__main__':
    main()
