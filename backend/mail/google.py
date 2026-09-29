"""Talking to Google: the OAuth handshake and the handful of Gmail calls we make.

**Why raw HTTP and not ``google-api-python-client``.** That library pulls a
large dependency tree to wrap six REST calls behind a discovery document
fetched at runtime. Six calls are cheaper to read, to pin and to test than the
wrapper, and ``requests`` is already a dependency.

**Why this is free of the CASA problem.** ``gmail.readonly`` is a *restricted*
scope: a public app asking for it must pass a Google-approved CASA Tier 2
security audit and repeat it every twelve months. The KDPS OAuth client is
registered as **Internal** to the KDPS Google Workspace organisation, and
Google's own verification rules exempt internal apps from that assessment.

That exemption is a property of the *client registration*, not of this code.
Whoever creates the credentials must set User type = Internal. Registering the
same code as External would work identically in testing and then hit a
hard wall - and an annual invoice - at go-live. It is written down here because
this file is where somebody would look.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import requests
from django.utils import timezone

# --- Google endpoints ------------------------------------------------------
#
# Overridable, but only on a debug server. `scripts/fake-google.py` is a local
# stand-in for Google that speaks these six calls, so the whole feature - the
# consent round trip included - can be driven end to end with no Google account
# and no network. Pointing a *production* server at a different token endpoint
# would hand somebody our client secret and every refresh token that follows,
# so the override is gated on DJANGO_DEBUG and read from the environment rather
# than from anything a request can influence.
_DEBUG = os.environ.get("DJANGO_DEBUG") == "1"


def _endpoint(variable: str, google: str) -> str:
    override = os.environ.get(variable, "").strip() if _DEBUG else ""
    return override or google


AUTH_URI = _endpoint("GOOGLE_AUTH_URI", "https://accounts.google.com/o/oauth2/v2/auth")
TOKEN_URI = _endpoint("GOOGLE_TOKEN_URI", "https://oauth2.googleapis.com/token")
REVOKE_URI = _endpoint("GOOGLE_REVOKE_URI", "https://oauth2.googleapis.com/revoke")
GMAIL_API = _endpoint("GOOGLE_GMAIL_API", "https://gmail.googleapis.com/gmail/v1/users/me")

#: The two spellings Google has ever used in an ID token's ``iss``.
ID_TOKEN_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}

#: What we ask a person to grant.
#:
#: ``gmail.modify`` rather than ``gmail.readonly`` + ``gmail.send``: marking a
#: mail read in KDPS has to mark it read in Gmail too, or the same mail is bold
#: on the phone after being read at the desk and the unread badge becomes
#: something people learn to ignore. ``modify`` is the narrowest scope that can
#: write a label, and it still cannot delete a mailbox (that needs
#: ``mail.google.com``, which we deliberately never ask for).
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]

#: Refresh this early, so no request begins on a token that dies mid-flight.
EXPIRY_MARGIN = timedelta(seconds=90)

TIMEOUT = 20


class GoogleError(RuntimeError):
    """Google refused, or could not be reached at all.

    Two attributes, and callers must read them rather than treat every failure
    alike. ``needs_reconnect`` says whether the person can fix it. ``status`` is
    the HTTP code, or ``None`` when the call never got an answer - and the
    difference between 404 and everything else decides whether sync may skip a
    message or must come back for it later.
    """

    def __init__(
        self, message: str, *, needs_reconnect: bool = False, status: int | None = None
    ) -> None:
        super().__init__(message)
        self.needs_reconnect = needs_reconnect
        self.status = status

    @property
    def gone(self) -> bool:
        """The one refusal that means "stop asking": the thing is not there.

        Every other failure - a timeout, a 429, a 500 - is Google being
        temporarily unable to answer a question that still has an answer.
        """
        return self.status == 404


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    client_secret: str
    redirect_uri: str

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri)


def oauth_client() -> OAuthClient:
    """Read the client registration from the environment.

    Read per call rather than at import: settings are loaded once per process,
    and a mis-set variable that only shows up after a restart is a bad way to
    learn you typed the redirect URI wrong.
    """
    return OAuthClient(
        client_id=os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip(),
        client_secret=os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip(),
        redirect_uri=os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", "").strip(),
    )


def authorize_url(state: str) -> str:
    """Where to send the browser to ask the person for consent.

    ``access_type=offline`` + ``prompt=consent`` because we need a *refresh*
    token and Google only issues one on a fresh consent. Without the prompt, a
    person who has authorised before is bounced straight back with an access
    token that dies in an hour and no way to renew it - the inbox works
    beautifully for one hour and then asks them to reconnect forever.

    ``hd`` narrows Google's account picker to the KDPS Workspace domain when one
    is configured, so somebody signed into a personal Gmail in the same browser
    is not offered it. It is a *convenience*, not a control - Google treats it
    as a hint and will still return whatever account the person picks. The
    domain is enforced on the way back, off the ID token's ``hd`` claim, in
    :func:`consented_identity`.
    """
    client = oauth_client()
    params = {
        "client_id": client.client_id,
        "redirect_uri": client.redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    hosted_domain = workspace_domain()
    if hosted_domain:
        params["hd"] = hosted_domain
    return f"{AUTH_URI}?{urlencode(params)}"


def _call(method: str, url: str, **kwargs: Any) -> requests.Response:
    """Every request to Google goes through here, so that every way a request
    can fail arrives at the caller as one exception type.

    A DNS failure, a connection reset and a read timeout are Google refusing
    just as much as a 500 is; letting them escape as ``requests`` exceptions
    means the handlers - which all catch :class:`GoogleError` - turn a flaky
    network into a 500 on somebody's screen.
    """
    try:
        return requests.request(method, url, timeout=TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        raise GoogleError(f"Could not reach Google just now: {exc}") from exc


def _body(response: requests.Response) -> dict[str, Any]:
    """The JSON, or a :class:`GoogleError` - never a ``ValueError`` escaping.

    An empty body is a legitimate answer (Gmail's modify returns one), so it
    reads as an empty dict rather than a failure.
    """
    if not response.content:
        return {}
    try:
        parsed = response.json()
    except ValueError as exc:
        raise GoogleError("Google's reply was not readable.", status=response.status_code) from exc
    return dict(parsed) if isinstance(parsed, dict) else {}


def _post_token(payload: dict[str, str]) -> dict[str, Any]:
    response = _call("POST", TOKEN_URI, data=payload)
    if response.status_code >= 400:
        # `invalid_grant` is the one refusal that means "this person must grant
        # again" - revoked access, changed password, expired token. Everything
        # else is our problem (bad secret, clock skew) and reconnecting would
        # only move the failure one screen later.
        body = response.text[:300]
        raise GoogleError(
            f"Google refused the token request: {body}",
            needs_reconnect="invalid_grant" in body,
            status=response.status_code,
        )
    return _body(response)


def exchange_code(code: str) -> dict[str, Any]:
    """Consent code → tokens."""
    client = oauth_client()
    return _post_token(
        {
            "code": code,
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "redirect_uri": client.redirect_uri,
            "grant_type": "authorization_code",
        }
    )


def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    client = oauth_client()
    return _post_token(
        {
            "refresh_token": refresh_token,
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "grant_type": "refresh_token",
        }
    )


def revoke(token: str) -> None:
    """Best-effort: tell Google to forget the grant when somebody disconnects.

    Failure is swallowed on purpose. Disconnecting must always succeed locally -
    if Google is unreachable, the right outcome is still that KDPS drops the
    token and the cached mail. Leaving a person connected because a third party
    was down would be the wrong way round.
    """
    try:
        _call("POST", REVOKE_URI, data={"token": token})
    except GoogleError:
        pass


def expires_at(token_response: dict[str, Any]) -> Any:
    seconds = int(token_response.get("expires_in", 3600))
    return timezone.now() + timedelta(seconds=seconds)


# --- who actually consented -------------------------------------------------


def workspace_domain() -> str:
    """The Workspace this server will accept mailboxes from, or blank for any."""
    return os.environ.get("GOOGLE_WORKSPACE_DOMAIN", "").strip().lower()


def _id_token_claims(raw: str) -> dict[str, Any]:
    """The claims inside an ID token, read without checking its signature.

    That is safe *here and nowhere else*. This token did not arrive from a
    browser: it came back in the body of our own HTTPS POST to Google's token
    endpoint, authenticated with our client secret. Google's OpenID Connect
    guidance says a token obtained that way needs no signature validation,
    because the TLS channel already proves where it came from. An ID token
    reaching us by any other route would need full JWKS verification, and there
    is deliberately no code here that would let one in.
    """
    parts = raw.split(".")
    if len(parts) != 3:
        raise GoogleError("Google's ID token was not a JWT.")
    try:
        claims = json.loads(b64url_decode(parts[1]))
    except (ValueError, UnicodeDecodeError) as exc:
        raise GoogleError("Google's ID token could not be read.") from exc
    if not isinstance(claims, dict):
        raise GoogleError("Google's ID token could not be read.")
    return claims


def consented_identity(token_response: dict[str, Any]) -> tuple[str, str]:
    """``(email, hosted_domain)`` for the account that actually consented.

    Read from the ID token rather than from ``/userinfo``, because only the ID
    token carries ``hd`` - and ``hd`` is the only *enforceable* statement about
    which Workspace a mailbox belongs to. The ``hd`` parameter on the consent
    URL is a hint to Google's account picker and nothing more: it changes which
    accounts are offered, not which one is accepted, so a client that is
    registered External (or simply misconfigured) will happily hand back a
    personal address. The check has to happen on the way back in.

    The address is read here too, not from the KDPS username: a person may sign
    into KDPS as `ramesh` and hold mail at `r.kumar@…`, and showing the wrong
    one over an inbox is how a misattached account goes unnoticed.
    """
    raw = token_response.get("id_token")
    if not raw:
        raise GoogleError("Google did not say which account consented.")
    claims = _id_token_claims(str(raw))

    if claims.get("aud") != oauth_client().client_id:
        raise GoogleError("That consent was issued for a different application.")
    if claims.get("iss") not in ID_TOKEN_ISSUERS:
        raise GoogleError("That sign-in did not come from Google.")
    try:
        expiry = int(claims.get("exp") or 0)
    except (TypeError, ValueError):
        expiry = 0
    if expiry <= int(timezone.now().timestamp()):
        raise GoogleError("That sign-in had already expired.")

    email = str(claims.get("email") or "").strip()
    if not email:
        raise GoogleError("Google did not say which address consented.")
    if claims.get("email_verified") is False:
        raise GoogleError("That Google address has not been verified.")
    return email, str(claims.get("hd") or "").strip()


# --- Gmail ------------------------------------------------------------------


def _gmail_refusal(response: requests.Response) -> GoogleError:
    """One place that turns a Gmail HTTP status into an exception, so the status
    always survives onto the exception. Sync reads it to decide whether a
    message may be skipped or must be asked for again."""
    if response.status_code == 401:
        return GoogleError("Google rejected the access token.", needs_reconnect=True, status=401)
    if response.status_code == 404:
        return GoogleError("Not found in the mailbox.", status=404)
    return GoogleError(f"Gmail refused: {response.text[:200]}", status=response.status_code)


def gmail_get(access_token: str, path: str, **params: Any) -> dict[str, Any]:
    response = _call(
        "GET",
        f"{GMAIL_API}{path}",
        headers={"Authorization": f"Bearer {access_token}"},
        params=params,
    )
    if response.status_code >= 400:
        raise _gmail_refusal(response)
    return _body(response)


def gmail_post(access_token: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    response = _call(
        "POST",
        f"{GMAIL_API}{path}",
        headers={"Authorization": f"Bearer {access_token}"},
        json=body,
    )
    if response.status_code >= 400:
        raise _gmail_refusal(response)
    return _body(response)


def b64url_decode(data: str) -> bytes:
    """Gmail returns bodies and attachments in URL-safe base64, usually without
    the trailing padding Python insists on. Re-add it rather than let a body
    silently come back empty."""
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode()
