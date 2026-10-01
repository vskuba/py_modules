"""EIP-712 typed data: domainSeparator, structHash, digest — без eth_account.

Чужие escrow-API принимают подпись над EIP-712 digest'ом, а типизированные
структуры каждый раз собирались руками. Здесь канон: typeHash = keccak(строка
типа), structHash = keccak(typeHash ‖ поля), digest = keccak(0x1901 ‖ домен ‖
структура). Типы полей разбираются прямо из строки типа — порядок полей и их
типы не заводятся вторым списком и не разъезжаются.
"""

from evm_.evm_keys import _k as _keccak  # тот же keccak, второй раз не пишем


def evm_typed_domain(name: str, version: str, chain_id: int, verifying_contract: str) -> str:
    """domainSeparator (hex) — домен EIP-712: имя токена, версия, chain, контракт.

    Returns:
        '0x…' — keccak от хэша домена с четырьмя стандартными полями.
    """
    sig = 'EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)'
    return '0x' + _domain_hash(name, version, chain_id, verifying_contract).hex()


def evm_typed_digest(type_signature: str, fields: dict, domain: str) -> str:
    """Digest EIP-712 под подпись: 0x1901 ‖ domain ‖ struct — ровно то, что подписывают.

    Args:
        type_signature: канонический тип с полями, напр.
            'ReceiveWithAuthorization(address from,address to,uint256 value,…)'.
        fields: {имя поля: значение} — порядок берётся из type_signature, не отсюда.
        domain: domainSeparator из evm_typed_domain.

    ⚠ Поле, отсутствующее в fields, — KeyError с именем: подписать структуру
    неполной структурой тише, чем неправильно, поэтому ошибки нет — есть сбой.
    """
    body = type_signature[type_signature.index('(') + 1:-1]
    pairs = [p.strip().rsplit(' ', 1) for p in body.split(',')]
    th = _keccak(type_signature.encode())
    parts = [th]
    for t, n in pairs:
        if n not in fields:
            raise KeyError(f'полю типа {n!r} нет значения в fields')
        parts.append(_encode(t, fields[n]))
    sh = _keccak(b''.join(parts))
    return '0x' + _keccak(b'\x19\x01' + bytes.fromhex(domain[2:]) + sh).hex()


def evm_typed_type_hash(type_signature: str) -> str:
    """typeHash структуры (keccak канонической строки типа) — для сверки с чужим SDK."""
    return '0x' + _keccak(type_signature.encode()).hex()


# ─── приватное ─────────────────────────────────────────────────────────────

def _domain_hash(name, version, chain_id, verifying_contract) -> bytes:
    sig = 'EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)'
    return _keccak(b''.join([
        _keccak(sig.encode()), _enc_string(name), _enc_string(version),
        chain_id.to_bytes(32, 'big'), int(verifying_contract, 16).to_bytes(32, 'big')]))


def _enc_string(v: str) -> bytes:
    return _keccak(v.encode()) if isinstance(v, str) else v


def _encode(t: str, v) -> bytes:
    if t == 'address':
        return int(v, 16).to_bytes(32, 'big')
    if t == 'bytes32':
        return bytes.fromhex(v[2:] if isinstance(v, str) else v)
    if t == 'bool':
        return (1 if v else 0).to_bytes(32, 'big')
    if t.startswith(('uint', 'int')):
        return int(v).to_bytes(32, 'big')
    if t in ('string', 'bytes'):
        return _enc_string(v)
    raise ValueError(f'тип {t!r} не поддержан: расширьте _encode')


if __name__ == '__main__':
    import argparse
    import json

    ap = argparse.ArgumentParser(
        description='EIP-712 руками: домен, typeHash, digest под подпись. '
                    'Тем и полезно, что сверяет наш digest с чужим SDK до того, '
                    'как подпись уедет в чужой API.',
        epilog="domain 'USD Coin' 2 8453 0xA0b8… | "
               "typehash 'Permit(address owner,uint256 value)' | "
               "digest 'Permit(address owner,uint256 value)' "
               "--fields '{\"owner\":\"0x…\",\"value\":1}' --domain 0x…")
    ap.add_argument('command', choices=['domain', 'typehash', 'digest'])
    ap.add_argument('args', nargs='*',
                    help='domain: имя версия chain_id контракт; '
                         'typehash/digest: каноническая строка типа')
    ap.add_argument('--fields', default='{}', help='значения полей JSON-ом (digest)')
    ap.add_argument('--domain', default='',
                    help='готовый domainSeparator (digest); пусто — собрать из '
                         '--name/--version/--chain/--contract')
    ap.add_argument('--name', default='')
    ap.add_argument('--version', default='')
    ap.add_argument('--chain', type=int, default=0)
    ap.add_argument('--contract', default='')
    ns = ap.parse_args()

    try:
        if ns.command == 'domain':
            print(evm_typed_domain(ns.args[0], ns.args[1], int(ns.args[2]), ns.args[3]))
        elif ns.command == 'typehash':
            print(evm_typed_type_hash(ns.args[0]))
        else:
            sep = ns.domain or evm_typed_domain(ns.name, ns.version, ns.chain, ns.contract)
            print(evm_typed_digest(ns.args[0], json.loads(ns.fields), sep))
    except IndexError:
        raise SystemExit(f'ошибка: мало аргументов для «{ns.command}»')
    except (KeyError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')
