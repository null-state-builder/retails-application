"""Encryption for the one secret this app holds: a person's Google refresh token.

A refresh token is not a password but it is worse than one - it does not expire,
it cannot be seen and changed by the person it belongs to, and anybody holding
it can read that person's entire mailbox from anywhere, silently, until an admin
notices. A database dump that leaks one has leaked every mail KDPS has ever
received. So it never sits in a column as text.

Two deliberate choices:

**A dedicated key, not ``DJANGO_SECRET_KEY``.** Deriving from the secret key
would look tidier and would be a trap: rotating ``DJANGO_SECRET_KEY`` is a
routine, encouraged act, and the day somebody does it every stored token would
silently become undecryptable garbage. Different lifetimes, different keys.

**Fail closed, never fail open.** With no key configured this module refuses,
and :func:`is_configured` lets the views answer "mail is not set up on this
server" before anybody reaches a connect button. The alternative - storing the
token as text when the key is missing - is how a dev shortcut ends up in
production, and it would be invisible from the outside.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

#: The environment variable holding the key. Any passphrase works: it is hashed
#: to Fernet's required 32 bytes below, so an operator is never asked to
#: generate base64 by hand (and so never tempted to reuse something else).
KEY_ENV = "KDPS_MAIL_TOKEN_KEY"


class MailNotConfigured(RuntimeError):
    """No token key on this server, so mail cannot be switched on here."""


def is_configured() -> bool:
    return bool(os.environ.get(KEY_ENV, "").strip())


def _fernet() -> Fernet:
    raw = os.environ.get(KEY_ENV, "").strip()
    if not raw:
        raise MailNotConfigured(
            f"{KEY_ENV} is not set. Mail stores Google refresh tokens, which are "
            "only ever written encrypted, so the feature stays off until a key exists."
        )
    # SHA-256 to exactly 32 bytes, then urlsafe-base64 - Fernet's key format.
    # The hash is a *format* step, not a strength claim: the key's strength is
    # whatever the operator put in the variable, so generate it with
    # `openssl rand -base64 32`.
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest()))


def encrypt(plaintext: str) -> str:
    """Token in, ciphertext out. Empty stays empty: "no token" is a real state
    (an account that has been revoked) and it should read as blank in the
    database rather than as an indistinguishable blob."""
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Ciphertext in, token out.

    A value that will not decrypt returns empty rather than raising. That case
    means the key changed under stored rows, and the honest consequence is
    "this person must reconnect their mail" - which is exactly what an empty
    refresh token already causes. Raising instead would turn a key rotation into
    a 500 on the top bar of every screen in the app.
    """
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, MailNotConfigured):
        return ""
