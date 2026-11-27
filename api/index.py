"""Vercel Serverless WSGI giriş noktası."""

import os
import sys
from urllib.parse import parse_qsl, urlencode, unquote

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

from app import app as _flask_app  # noqa: E402


class VercelPathRestoreMiddleware:
    """
    Vercel rewrites (/api/index.py?__vercel_path=/$1) sonrası kaybolan WSGI PATH_INFO
    değerini geri yükler ve __vercel_path parametresini QUERY_STRING içinden temizler.
    """

    _FUNCTION_PATHS = ("/api/index", "/api/index.py", "/api/index/", "/api/index.py/")
    _FALLBACK_HEADERS = (
        "HTTP_X_VERCEL_ORIGINAL_URL",
        "HTTP_X_FORWARDED_URI",
        "REQUEST_URI",
    )

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    @staticmethod
    def _normalize_path(raw_path: str) -> str:
        path = unquote(str(raw_path or "")).split("?", 1)[0].strip()
        if not path:
            return "/"
        return "/" + path.lstrip("/")

    def __call__(self, environ, start_response):
        qs = environ.get("QUERY_STRING", "") or ""
        vercel_path = None
        if qs:
            pairs = parse_qsl(qs, keep_blank_values=True)
            remaining = []
            for key, value in pairs:
                if key == "__vercel_path" and vercel_path is None:
                    vercel_path = value
                elif key != "__vercel_path":
                    remaining.append((key, value))
            if vercel_path is not None:
                environ["QUERY_STRING"] = urlencode(remaining, doseq=True)

        if environ.get("SCRIPT_NAME") in self._FUNCTION_PATHS:
            environ["SCRIPT_NAME"] = ""

        current_path = environ.get("PATH_INFO", "") or ""
        is_function_path = (
            current_path in self._FUNCTION_PATHS
            or current_path.startswith("/api/index.py/")
        )

        if vercel_path is not None:
            environ["PATH_INFO"] = self._normalize_path(vercel_path)
        elif is_function_path:
            restored = None
            for header_key in self._FALLBACK_HEADERS:
                hdr_val = environ.get(header_key)
                if hdr_val:
                    hdr_path, _, hdr_qs = str(hdr_val).partition("?")
                    candidate = self._normalize_path(hdr_path)
                    if candidate not in self._FUNCTION_PATHS:
                        restored = candidate
                        if hdr_qs and not environ.get("QUERY_STRING"):
                            cleaned = [
                                (k, v)
                                for k, v in parse_qsl(hdr_qs, keep_blank_values=True)
                                if k != "__vercel_path"
                            ]
                            environ["QUERY_STRING"] = urlencode(cleaned, doseq=True)
                        break
            environ["PATH_INFO"] = restored or "/"

        return self.wsgi_app(environ, start_response)


if not isinstance(_flask_app.wsgi_app, VercelPathRestoreMiddleware):
    _flask_app.wsgi_app = VercelPathRestoreMiddleware(_flask_app.wsgi_app)

app = _flask_app
handler = app
