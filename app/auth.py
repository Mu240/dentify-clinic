"""Optional staff login. Set ADMIN_PASSWORD and the whole staff app (everything except the public
booking form, signed treatment-plan links, social webhooks and static files) asks for HTTP Basic credentials.
ADMIN_USER defaults to 'admin'. Put the app behind HTTPS (Cloudflare, Caddy, nginx) when you use it."""
import base64
import hmac
import os

from fastapi import Request
from fastapi.responses import Response

PUBLIC_PREFIXES = ("/book", "/static/", "/api/public/", "/webhooks/", "/api/health", "/plan/")


def enabled():
    return bool(os.environ.get("ADMIN_PASSWORD"))


def _authorized(header):
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
    except Exception:
        return False
    ok_user = hmac.compare_digest(user, os.environ.get("ADMIN_USER", "admin"))
    ok_pw = hmac.compare_digest(pw, os.environ["ADMIN_PASSWORD"])
    return ok_user and ok_pw


async def middleware(request: Request, call_next):
    if enabled() and not request.url.path.startswith(PUBLIC_PREFIXES):
        if not _authorized(request.headers.get("authorization")):
            return Response("Login required", 401, headers={"WWW-Authenticate": 'Basic realm="Practice Hub"'})
    return await call_next(request)
