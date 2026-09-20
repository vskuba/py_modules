"""Ed25519 ключи и подписи голыми байтами: DID-регистрации, Solana, EdDSA.

Возвращаем сырые байты ключей и подписи — кодирование (base58 у Solana,
base64url у DID-сервисов, hex везде) выбирает вызывающий: у платформ свои
требования, угадать их за вызывающего нельзя.
"""

from __future__ import annotations


def crypto_ed25519_pair(seed: bytes | None = None) -> tuple[bytes, bytes]:
    """Сгенерировать пару (priv, pub) Ed25519; seed=None — случайный ключ.

    ⚠ seed — ровно 32 байта, а не «любимая строка»: строку сначала хешируют
    (sha256), иначе ключ зависит от кодировки текста и не воспроизводится.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if seed is None:
        k = Ed25519PrivateKey.generate()
    else:
        if len(seed) != 32:
            raise ValueError(f"seed ровно 32 байта, а не {len(seed)}")
        k = Ed25519PrivateKey.from_private_bytes(seed)
    return k.private_bytes_raw(), k.public_key().public_bytes_raw()


def crypto_ed25519_sign(priv: bytes | Ed25519PrivateKey, message: str | bytes) -> bytes:
    """Подписать сообщение (строка — utf-8) приватным ключом, вернуть подпись.

    ⚠ message — «exact challenge string» как есть: ни strip, ни переносов от
    JSON-переноса; сервис сверяет подпись побайтово.
    """
    priv = _crypto_ed25519_load(priv)
    return priv.sign(message.encode() if isinstance(message, str) else message)


def crypto_ed25519_verify(pub: bytes, message: str | bytes, signature: bytes) -> bool:
    """Проверить подпись публичным ключом; True/False вместо исключения."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        pub_key = pub if isinstance(pub, Ed25519PublicKey) else Ed25519PublicKey.from_public_bytes(pub)
        pub_key.verify(signature, message.encode() if isinstance(message, str) else message)
        return True
    except Exception:
        return False


# ─── приватное ──────────────────────────────────────────────────────────────

def _crypto_ed25519_load(priv):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return priv if isinstance(priv, Ed25519PrivateKey) else Ed25519PrivateKey.from_private_bytes(priv)
