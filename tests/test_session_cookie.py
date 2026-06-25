"""No-signin session identity: the cookie is a signed session id. A valid cookie
round-trips; a tampered one is rejected (so the API mints a fresh session instead of
trusting forged input). Mirrors the signer wiring in yapper_web/api.py."""

from __future__ import annotations

import pytest

pytest.importorskip("itsdangerous")

from itsdangerous import BadSignature, URLSafeSerializer  # noqa: E402


def _signer():
    return URLSafeSerializer("test-secret", salt="jf-session")


def test_cookie_round_trips():
    s = _signer()
    sid = "11111111-2222-3333-4444-555555555555"
    token = s.dumps(sid)
    assert s.loads(token) == sid


def test_tampered_cookie_rejected():
    s = _signer()
    token = s.dumps("abc")
    with pytest.raises(BadSignature):
        s.loads(token + "x")


def test_wrong_secret_rejected():
    token = _signer().dumps("abc")
    other = URLSafeSerializer("different-secret", salt="jf-session")
    with pytest.raises(BadSignature):
        other.loads(token)
