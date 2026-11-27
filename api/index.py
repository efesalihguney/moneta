"""Vercel Serverless WSGI giriş noktası."""

import os
import sys

# Vercel'de /home/... salt-okunur olduğu için tüm kütüphane önbelleklerini /tmp altına yönlendir
os.environ.setdefault("HOME", "/tmp")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/.cache")
os.environ.setdefault("TMPDIR", "/tmp")

if os.getenv("VERCEL") or os.getenv("VERCEL_ENV") or not os.access(os.environ.get("HOME", "/tmp"), os.W_OK):
    os.environ["HOME"] = "/tmp"
    os.environ["XDG_CACHE_HOME"] = "/tmp/.cache"
    os.environ["TMPDIR"] = "/tmp"

for _cache_dir in ("/tmp/.cache", "/tmp/.cache/py-yfinance"):
    try:
        os.makedirs(_cache_dir, exist_ok=True)
    except OSError:
        pass

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from app import app, VercelPathRestoreMiddleware  # noqa: E402, F401

handler = app
