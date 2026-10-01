"""Proof-of-work от строки: подобрать nonce под число ведущих нулевых битов.

Каноничное правило challenge'ей DID-сервисов: «sha256(seed + nonce) имеет не
менее N ведущих нулевых битов». Счёт — big-endian биты хеша, конкатенация —
строками «как в правиле», а не байтами seed.
"""

from __future__ import annotations

import hashlib


def crypto_pow_solve(seed: str, bits: int, *, max_tries: int = 1 << 22) -> str:
    """Подобрать nonce: sha256(seed + nonce) даёт >= bits ведущих нулевых битов.

    ⚠ seed и nonce склеиваются как строки (seed как дали, nonce — str(i));
    sha256(bytes(seed)+bytes(nonce)) с hex-decode seed дал бы другой ответ.
    ⚠ ожидание ~2**bits попыток: при bits=18 это ~260k sha256, доли секунды;
    при bits>24 закладывать timeout в вызывающем.
    """
    for i in range(max_tries):
        nonce = str(i)
        if crypto_pow_leading_zero_bits(seed + nonce) >= bits:
            return nonce
    raise RuntimeError(f"nonce не найден за {max_tries} попыток — bits={bits} слишком?")


def crypto_pow_leading_zero_bits(payload: str) -> int:
    """Сколько ведущих нулевых битов у sha256(payload) — для сверки nonce."""
    digest = hashlib.sha256(payload.encode()).digest()
    value = int.from_bytes(digest, "big")
    return 256 - value.bit_length() if value else 256


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="proof-of-work: seed+nonce → ведущие нули")
    p.add_argument("seed")
    p.add_argument("--bits", type=int, default=18)
    p.add_argument("--check", help="nonce для сверки вместо подбора")
    a = p.parse_args()
    if a.check:
        print(crypto_pow_leading_zero_bits(a.seed + a.check))
    else:
        n = crypto_pow_solve(a.seed, a.bits)
        print(n, crypto_pow_leading_zero_bits(a.seed + n))
