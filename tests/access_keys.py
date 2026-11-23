"""Test helper: a fake Cloudflare Access team that signs JWTs with a throwaway RSA key.

`AccessKeys(monkeypatch)` replaces the JWKS fetch in `festival_planner.auth` (no network), gives
the auth module a fresh key cache and a fake monotonic clock, and signs tokens on demand.
Keys are generated per test and never written to disk.
"""

from __future__ import annotations

import secrets
import time
from datetime import date
from zoneinfo import ZoneInfo

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from festival_planner import auth
from festival_planner.config import Config, Organizer, Token

TEAM_DOMAIN = "team.example.com"
AUD = "test-audience-tag"
ISSUER = f"https://{TEAM_DOMAIN}"


class AccessKeys:
    def __init__(self, monkeypatch, team_domain: str = TEAM_DOMAIN, aud: str = AUD):
        self.team_domain = team_domain
        self.aud = aud
        self.issuer = f"https://{team_domain}"
        self.fetches: list[str] = []  # URLs requested, one entry per JWKS fetch
        self.now = 1000.0  # fake monotonic clock read by the auth module's JWKS cache
        self._published: list[dict] = []  # public JWKs served by the fake certs endpoint
        self.rotate()
        monkeypatch.setattr(auth, "_fetch_jwks", self._fetch)
        monkeypatch.setattr(auth, "_monotonic", lambda: self.now)
        monkeypatch.setattr(auth, "_jwks_cache", auth.JwksCache())

    def rotate(self) -> None:
        """Start signing with a new key (new kid) and publish it next to the old ones."""
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = secrets.token_hex(8)
        jwk = RSAAlgorithm.to_jwk(self._key.public_key(), as_dict=True)
        jwk.update(kid=self.kid, alg="RS256", use="sig")
        self._published.append(jwk)

    def public_pem(self) -> bytes:
        return self._key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def _fetch(self, url: str) -> dict:
        self.fetches.append(url)
        return {"keys": [dict(k) for k in self._published]}

    def sign(
        self,
        email: str | None,
        aud: str | None = None,
        exp_delta: int = 300,
        *,
        iss: str | None = None,
        kid: str | None = None,
        omit: tuple[str, ...] = (),
    ) -> str:
        """A signed Access JWT. `exp_delta` is seconds from now (negative means expired)."""
        now = int(time.time())
        claims = {
            "aud": [aud or self.aud],
            "iss": iss or self.issuer,
            "iat": now,
            "nbf": now,
            "exp": now + exp_delta,
            "sub": "user-id",
            "type": "app",
        }
        if email is not None:
            claims["email"] = email
        for name in omit:
            claims.pop(name, None)
        return jwt.encode(claims, self._key, algorithm="RS256", headers={"kid": kid or self.kid})


ORGANIZER_A = "organizer-a@example.com"
ORGANIZER_B = "organizer-b@example.com"
ALIAS_A = "organizer-a.alias@example.com"
WRITE_SECRET = "w" * 32
READ_SECRET = "r" * 32


def make_config(tmp_path, **overrides) -> Config:
    """A Config with two organizers, Access settings matching AccessKeys, and two tokens."""
    values = {
        "event_name": "Example Fall Festival",
        "event_date": date(2099, 10, 10),
        "timezone": ZoneInfo("UTC"),
        "organizers": (
            Organizer(ORGANIZER_A, "Organizer A", (ALIAS_A,)),
            Organizer(ORGANIZER_B, "Organizer B", ()),
        ),
        "seed_workstreams": (),
        "seed_milestones": (),
        "data_dir": tmp_path,
        "cf_team_domain": TEAM_DOMAIN,
        "cf_aud": AUD,
        "mcp_tokens": (Token("claude", "write", WRITE_SECRET), Token("duet", "read", READ_SECRET)),
    }
    values.update(overrides)
    return Config(**values)
