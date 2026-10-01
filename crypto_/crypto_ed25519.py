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


if __name__ == '__main__':
    import argparse
    import base64
    import hashlib

    ap = argparse.ArgumentParser(
        description='Ed25519 руками: пара ключей, подпись челленджа, сверка. '
                    'Кодирование выбирается флагом — у платформ оно разное.',
        epilog="pair --seed-text моя-фраза --encoding base58 | "
               "sign КЛЮЧ 'exact challenge' | verify PUB 'challenge' SIG")
    ap.add_argument('command', choices=['pair', 'sign', 'verify'])
    ap.add_argument('args', nargs='*', help='ключ, сообщение, подпись — по команде')
    ap.add_argument('--seed-text', default='',
                    help='фраза вместо случайного ключа: seed = sha256(фраза), '
                         'ровно 32 байта (голую фразу брать нельзя — см. ⚠ у pair)')
    ap.add_argument('--encoding', default='hex', choices=['hex', 'base64url', 'base58'],
                    help='как печатать и как читать ключи/подпись')
    ns = ap.parse_args()

    def _enc(raw: bytes) -> str:
        if ns.encoding == 'hex':
            return raw.hex()
        if ns.encoding == 'base64url':
            return base64.urlsafe_b64encode(raw).decode().rstrip('=')
        # ⚠ Ведущий нулевой байт в base58 — это символ '1' в префиксе, и
        # только он: подстраховка «пусто -> '1'» удваивала бы его на ключе,
        # начинающемся с нуля (1 ключ из 256), давая чужой адрес Solana.
        alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
        number = int.from_bytes(raw, 'big')
        out = ''
        while number:
            number, rest = divmod(number, 58)
            out = alphabet[rest] + out
        return '1' * (len(raw) - len(raw.lstrip(b'\0'))) + out

    def _dec(text: str) -> bytes:
        if ns.encoding == 'hex':
            return bytes.fromhex(text.removeprefix('0x'))
        if ns.encoding == 'base64url':
            return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))
        alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
        number = 0
        for ch in text:
            number = number * 58 + alphabet.index(ch)
        body = number.to_bytes((number.bit_length() + 7) // 8, 'big')
        return b'\0' * (len(text) - len(text.lstrip('1'))) + body

    try:
        if ns.command == 'pair':
            seed = hashlib.sha256(ns.seed_text.encode()).digest() if ns.seed_text else None
            priv, pub = crypto_ed25519_pair(seed)
            print(f'priv {_enc(priv)}\npub  {_enc(pub)}')
        elif ns.command == 'sign':
            # Самопроверка на каждом вызове — как у `evm_keys sign`: подпись,
            # которую не принял собственный публичный ключ, наружу не уходит.
            priv = _dec(ns.args[0])
            _, pub = crypto_ed25519_pair(priv)
            signature = crypto_ed25519_sign(priv, ns.args[1])
            if not crypto_ed25519_verify(pub, ns.args[1], signature):
                raise SystemExit('ошибка: подпись не сверилась своим же ключом')
            print(_enc(signature))
        else:
            ok = crypto_ed25519_verify(_dec(ns.args[0]), ns.args[1], _dec(ns.args[2]))
            print('подпись верна' if ok else 'ПОДПИСЬ НЕ СХОДИТСЯ')
            raise SystemExit(0 if ok else 1)
    except IndexError:
        raise SystemExit(f'ошибка: мало аргументов для «{ns.command}»')
    except ValueError as err:
        raise SystemExit(f'ошибка: не разобрано как {ns.encoding}: {err}')
