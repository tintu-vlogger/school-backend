"""Talking to Google and Microsoft: the sign-in address and turning the returned code into a
verified email address.

Google (OpenID Connect)
  authorise  https://accounts.google.com/o/oauth2/v2/auth
  token      https://oauth2.googleapis.com/token
  userinfo   https://openidconnect.googleapis.com/v1/userinfo  -> email, email_verified, hd
  Only addresses Google marks `email_verified` are accepted.

Microsoft identity platform v2 (Entra ID / Microsoft 365, multi-tenant)
  authorise  https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize
  token      https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token
  profile    https://graph.microsoft.com/v1.0/me                 -> userPrincipalName
  {tenant} is "organizations" (any work or school account, the default), "common" (also
  personal Microsoft accounts) or one directory id. The sign-in name (userPrincipalName) is used,
  never the editable `mail` attribute or the `email` claim: a directory may only use domains
  it has verified as sign-in names, while `mail` can be set to anything by that directory's
  admin (the "nOAuth" problem). Guest accounts (#EXT#) are refused.

`fetch_identity` is the one function tests replace.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
MS_LOGIN = "https://login.microsoftonline.com"
MS_GRAPH_ME = "https://graph.microsoft.com/v1.0/me?$select=id,displayName,userPrincipalName,mail"
MS_SCOPE = "openid profile email User.Read"

PROVIDERS = {"google": "Google", "microsoft": "Microsoft"}


class SsoError(Exception):
    """A problem the person should see on the sign-in page (code is sent in the address)."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


@dataclass
class Identity:
    provider: str
    subject: str
    email: str
    email_verified: bool
    name: str = ""
    picture: str = ""
    tenant: str = ""          # Microsoft directory id, Google Workspace domain (hd)


def authorize_url(provider: str, client_id: str, redirect_uri: str, state: str, tenant: str | None = None,
                  login_hint: str | None = None) -> str:
    if provider == "google":
        params = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
                  "scope": "openid email profile", "state": state, "prompt": "select_account"}
        if login_hint:
            params["login_hint"] = login_hint
        return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"
    if provider == "microsoft":
        params = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
                  "response_mode": "query", "scope": MS_SCOPE, "state": state, "prompt": "select_account"}
        if login_hint:
            params["login_hint"] = login_hint
        return f"{MS_LOGIN}/{tenant or 'organizations'}/oauth2/v2.0/authorize?{urlencode(params)}"
    raise SsoError("unknown_provider")


def _claims(id_token: str | None) -> dict:
    """Claims of an ID token received straight from the token endpoint over TLS (OpenID
    Connect Core 3.1.3.7 allows skipping the signature check in that case)."""
    if not id_token or id_token.count(".") != 2:
        return {}
    part = id_token.split(".")[1]
    try:
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:
        return {}


def fetch_identity(provider: str, code: str, redirect_uri: str, client_id: str, client_secret: str,
                   tenant: str | None = None) -> Identity:
    """Exchange the code and return who signed in. Raises SsoError on any failure."""
    try:
        with httpx.Client(timeout=15) as http:
            if provider == "google":
                tok = http.post(GOOGLE_TOKEN_URL, data={"code": code, "client_id": client_id, "client_secret": client_secret,
                                                        "redirect_uri": redirect_uri, "grant_type": "authorization_code"})
                if tok.status_code != 200:
                    raise SsoError("exchange_failed")
                info = http.get(GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {tok.json().get('access_token')}"})
                if info.status_code != 200:
                    raise SsoError("exchange_failed")
                u = info.json()
                return Identity("google", str(u.get("sub") or ""), (u.get("email") or "").strip().lower(),
                                bool(u.get("email_verified")), u.get("name") or "", u.get("picture") or "", u.get("hd") or "")
            if provider == "microsoft":
                tok = http.post(f"{MS_LOGIN}/{tenant or 'organizations'}/oauth2/v2.0/token",
                                data={"code": code, "client_id": client_id, "client_secret": client_secret,
                                      "redirect_uri": redirect_uri, "grant_type": "authorization_code", "scope": MS_SCOPE})
                if tok.status_code != 200:
                    raise SsoError("exchange_failed")
                body = tok.json()
                claims = _claims(body.get("id_token"))
                me = http.get(MS_GRAPH_ME, headers={"Authorization": f"Bearer {body.get('access_token')}"})
                if me.status_code != 200:
                    raise SsoError("exchange_failed")
                u = me.json()
                upn = (u.get("userPrincipalName") or "").strip().lower()
                if "#ext#" in upn:
                    raise SsoError("guest_account")
                return Identity("microsoft", str(claims.get("oid") or u.get("id") or ""), upn, "@" in upn,
                                u.get("displayName") or claims.get("name") or "", "", str(claims.get("tid") or ""))
    except SsoError:
        raise
    except httpx.HTTPError:
        raise SsoError("provider_unreachable")
    raise SsoError("unknown_provider")
