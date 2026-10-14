import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from core.exchanges.kalshi import KalshiSigner
from core.exchanges.polymarket_us import PolymarketUSClient


def _pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


# ---------------- Kalshi ----------------

def test_kalshi_ed25519_signature_verifies_and_strips_query():
    key = ed25519.Ed25519PrivateKey.generate()
    signer = KalshiSigner.from_pem("key-id", _pem(key))
    assert signer.key_type == "ed25519"
    h = signer.headers("get", "/trade-api/v2/portfolio/orders?limit=5", timestamp_ms="1703123456789")
    assert h["KALSHI-ACCESS-KEY"] == "key-id"
    assert h["KALSHI-ACCESS-TIMESTAMP"] == "1703123456789"
    # Message is timestamp + METHOD + path WITHOUT the query string.
    key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
                            b"1703123456789GET/trade-api/v2/portfolio/orders")


def test_kalshi_rsa_pss_signature_verifies():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    # PKCS#1 ("BEGIN RSA PRIVATE KEY") is what Kalshi hands out for RSA keys.
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                            serialization.NoEncryption())
    signer = KalshiSigner.from_pem("key-id", pem)
    assert signer.key_type == "rsa"
    sig = signer.sign("1703123456789", "POST", "/trade-api/v2/portfolio/orders")
    key.public_key().verify(
        base64.b64decode(sig), b"1703123456789POST/trade-api/v2/portfolio/orders",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_kalshi_rsa_pkcs8_detected_as_rsa():
    # The PEM header doesn't identify type: a PKCS#8 RSA key says "BEGIN PRIVATE KEY" like Ed25519.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert KalshiSigner.from_pem("k", _pem(key)).key_type == "rsa"


def test_kalshi_rejects_unsupported_key():
    from cryptography.hazmat.primitives.asymmetric import ec
    with pytest.raises(ValueError):
        KalshiSigner("k", ec.generate_private_key(ec.SECP256R1()))


def test_kalshi_signature_changes_with_path_and_method():
    signer = KalshiSigner.from_pem("k", _pem(ed25519.Ed25519PrivateKey.generate()))
    a = signer.sign("1", "GET", "/trade-api/v2/a")
    assert a != signer.sign("1", "POST", "/trade-api/v2/a")
    assert a != signer.sign("1", "GET", "/trade-api/v2/b")


# ---------------- Polymarket US ----------------

@pytest.mark.parametrize("secret_len", [32, 64])
def test_polymarket_us_ed25519_headers_verify(secret_len):
    key = ed25519.Ed25519PrivateKey.generate()
    seed = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    # The portal may hand out a 64-byte (seed + public key) secret; only the first 32 bytes are the seed.
    secret = base64.b64encode(seed if secret_len == 32 else seed + pub).decode()

    client = PolymarketUSClient(key_id="pm-key", secret_key=secret)
    h = client.auth_headers("GET", "/v1/portfolio/positions")
    assert h["X-PM-Access-Key"] == "pm-key"
    ts = h["X-PM-Timestamp"]
    assert ts.isdigit() and len(ts) == 13  # milliseconds
    key.public_key().verify(base64.b64decode(h["X-PM-Signature"]),
                            f"{ts}GET/v1/portfolio/positions".encode())


def test_polymarket_us_requires_credentials():
    from core.exchanges.base import ExchangeError
    with pytest.raises(ExchangeError):
        PolymarketUSClient().auth_headers("GET", "/v1/orders/open")
