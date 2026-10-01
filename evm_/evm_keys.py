"""Ключ и подпись secp256k1 без eth-библиотек: адрес, подпись digest'а, recovery.

Кошельки проектов живут иногда в venv без crypto-стека вообще, а подписать
EIP-712 digest для чужого escrow-API нужно сегодня. Отсюда чистый secp256k1:
арифметика точек на cryptography, keccak на pycryptodome, RFC6979-подпись и
восстановление ключа из подписи — всё с самопроверкой на каждом вызове.

Грабли, на которые здесь уже наступали:

- удвоение точки: наклон `3·x²`, а не `3x` — неверный член даёт точку вне
  кривой и «Point is not on the curve» на валидации ключа;
- recovery: `u1 = −z·r⁻¹`, без минуса восстановленный ключ сходится с
  вероятностью ½ и подпись проходит для неверного ключа;
- публичный ключ выводится из приватного умножением на G, а не берётся из
  памяти исходника-репетитора;
- подпись нормализуется в low-s (контракт Ethereum), v = recovery + 27.
"""

EVM_KEYS_P = 2**256 - 2**32 - 977          # поле кривой p
EVM_KEYS_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141  # порядок n
EVM_KEYS_G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
              0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def evm_keys_address(priv) -> str:
    """EIP-55 адрес кошелька из приватного ключа — без web3 и eth_account.

    Args:
        priv: приватный ключ: int или hex-строка с 0x.
    """
    return _evm_keys_eip55(_mul(int(priv, 16) if isinstance(priv, str) else priv, EVM_KEYS_G))


def evm_keys_sign(priv, digest) -> dict:
    """Подписать digest secp256k1 — подпись с проверкой ключа и recovery.

    Args:
        priv: приватный ключ int или hex-строка.
        digest: bytes или hex-строка — подписывается как есть, без пере-хэша.

    Returns:
        {'r','s','v','signer','recovered'}: v уже 27/28, signer — адрес
        подписанта (EIP-55), recovered — восстановленный из подписи.

    ⚠ Разошлись адреса — ValueError с обоими: подписывать неверным digest'ом
    здесь тише не значит верно, самопроверка обязательна.
    """
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import (
        decode_dss_signature, Prehashed)
    from cryptography.hazmat.primitives import hashes
    if isinstance(digest, str):
        digest = bytes.fromhex(digest.removeprefix('0x'))
    priv = int(priv, 16) if isinstance(priv, str) else priv
    key = _evm_keys_priv_ec(priv)
    der = key.sign(digest, ec.ECDSA(Prehashed(hashes.SHA256())))
    r, s = decode_dss_signature(der)
    if s > EVM_KEYS_N // 2:                       # low-s, как требует Ethereum
        s = EVM_KEYS_N - s
    z = int.from_bytes(digest, 'big')
    pub = _mul(priv, EVM_KEYS_G)
    rec = next((g for g in range(4) if _recover(z, s, r, g) == pub), None)
    if rec is None:
        raise ValueError(f'подпись не восстанавливает ключ: signer={_evm_keys_eip55(pub)}')
    return {'r': hex(r), 's': hex(s), 'v': 27 + rec,
            'signer': _evm_keys_eip55(pub), 'recovered': _evm_keys_eip55(_recover(z, s, r, rec))}


# ─── приватное ─────────────────────────────────────────────────────────────

def _k(data: bytes) -> bytes:
    from Crypto.Hash import keccak
    h = keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


def _add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    (x1, y1), (x2, y2) = p1, p2
    if x1 == x2:
        return None if (y1 + y2) % EVM_KEYS_P == 0 else _double(p1)
    lam = (y2 - y1) * pow((x2 - x1) % EVM_KEYS_P, EVM_KEYS_P - 2, EVM_KEYS_P) % EVM_KEYS_P
    x = (lam * lam - x1 - x2) % EVM_KEYS_P
    return (x, (lam * (x1 - x) - y1) % EVM_KEYS_P)


def _double(p):
    x, y = p
    lam = (3 * x * x % EVM_KEYS_P) * pow(2 * y, EVM_KEYS_P - 2, EVM_KEYS_P) % EVM_KEYS_P
    x3 = (lam * lam - 2 * x) % EVM_KEYS_P
    return (x3, (lam * (x - x3) - y) % EVM_KEYS_P)


def _mul(k: int, p):
    """Умножение точки double-and-add; точка на кривой «в бесконечности» — None."""
    r = None
    for bit in bin(k)[2:]:
        if r is not None:
            r = _double(r)
        if bit == '1':
            r = p if r is None else _add(r, p)
    return r


def _y_of_x(x: int, parity: bool) -> int:
    y = pow((x**3 + 7) % EVM_KEYS_P, (EVM_KEYS_P + 1) // 4, EVM_KEYS_P)
    return y if (y % 2 == 1) == parity else (EVM_KEYS_P - y) % EVM_KEYS_P


def _recover(z: int, s: int, r: int, rec: int):
    """Q = r⁻¹(s·R − z·G): без минуса на z ключ бы «сходился» вдвое чаще.

    rec ∈ 0..3: бит 0 — чётность y у R, бит 1 — что x = r + n (редко).
    Перебор четырёх кандидатов, а не двух, иначе подпись «не восстанавливается»
    на digest'ах с истинным rec=0 и нечётным y.
    """
    ri = pow(r, EVM_KEYS_N - 2, EVM_KEYS_N)
    x = (r + (EVM_KEYS_N if rec & 2 else 0)) % EVM_KEYS_P
    return _add(_mul((-z * ri) % EVM_KEYS_N, EVM_KEYS_G),
                _mul((s * ri) % EVM_KEYS_N, (x, _y_of_x(x, bool(rec & 1)))))


def _evm_keys_priv_ec(priv: int):
    from cryptography.hazmat.primitives.asymmetric.ec import (
        EllipticCurvePrivateNumbers, EllipticCurvePublicNumbers, SECP256K1)
    px, py = _mul(priv, EVM_KEYS_G)
    return EllipticCurvePrivateNumbers(
        priv, EllipticCurvePublicNumbers(px, py, SECP256K1())).private_key()


def _evm_keys_eip55(pub) -> str:
    body = (_k(pub[0].to_bytes(32, 'big') + pub[1].to_bytes(32, 'big'))[-20:]).hex()
    mix = _k(body.encode()).hex()
    return '0x' + ''.join(c.upper() if int(mix[i], 16) >= 8 else c for i, c in enumerate(body))


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='адрес из ключа и подпись digest'
                                              '\'а самопроверкой: evm_keys address|sign')
    ap.add_argument('cmd', choices=['address', 'sign'])
    ap.add_argument('priv', help='приватный ключ hex')
    ap.add_argument('--digest', help='digest hex для sign')
    ns = ap.parse_args()
    print(evm_keys_address(ns.priv) if ns.cmd == 'address'
          else evm_keys_sign(ns.priv, ns.digest))
