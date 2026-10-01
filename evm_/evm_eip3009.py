"""EIP-3009 authorization'ы FiatToken (USDC/EURC): подпись + calldata + самопроверка.

Gasless-путь escrow (funding-authorization для чужого submit, reveal-preimage,
вывод средств на кошелёк получателя) требует подписать
ReceiveWithAuthorization/TransferWithAuthorization с точным полем полей. Здесь
оба вида одним вызовом: поля берутся из канонического типа, подписант сверяется
с адресом ключа — молчаливая подмена полей видна сразу, а не на чужом 400-ом.

Грабли, на которые здесь уже наступали:

- порядок полей диктует контракт, а не удобочитаемость: в FiatToken
  (circlefin/stablecoin-evm, contracts/v2/EIP3009.sol) структура идёт
  `from,to,value,validAfter,validBefore,nonce` — nonce ПОСЛЕДНИМ и поля maxFee
  в типе нет вовсе. Подписанная ранее структура с `nonce` в середине давала
  подпись, которая ни в кого не восстанавливалась;
- typeHash из результата сверяется с константой контракта — это единственный
  способ увидеть подмену полей ДО отправки;
- value — целые единицы токена (USDC 6 знаков: $2.80 = 2800000); scale'у тут не
  верят, передают как есть;
- подпись пакуется как `abi.encodePacked(r, s, v)`: v последний, v = 27 + rec_id.
"""

from evm_.evm_typed import (evm_typed_type_hash, evm_typed_domain, evm_typed_digest,
                            _encode as _evm_field)
from evm_.evm_keys import evm_keys_sign, evm_keys_address

# Канонические структуры FiatToken. typeHash'ы — сверять с константой контракта:
# transfer 0x7c7c6cdb67a18743f49ec6fa9b35f50d52ed05cbed4cc592e13b44501c1a2267,
# receive  0xd099cc98ef71107a616c4f0f941f04c322d8e254fe26b3c6668db87aae413de8.
EVM_EIP3009_TYPES = {
    'receive': 'ReceiveWithAuthorization(address from,address to,uint256 value,'
               'uint256 validAfter,uint256 validBefore,bytes32 nonce)',
    'transfer': 'TransferWithAuthorization(address from,address to,uint256 value,'
                'uint256 validAfter,uint256 validBefore,bytes32 nonce)',
}

# Внешние функции того же контракта: те же поля в том же порядке + v,r,s на конце.
EVM_EIP3009_FUNCS = {
    kind: name[0].lower() + name[1:] + head + ',uint8 v,bytes32 r,bytes32 s)'
    for kind, sig in EVM_EIP3009_TYPES.items()
    for name, head in [(sig[:sig.index('(')], sig[sig.index('('):-1])]
}


def evm_eip3009_authorize(priv, token: dict, chain_id: int, to: str, value: int,
                          nonce: str, valid_before: int, valid_after: int = 0,
                          kind: str = 'receive', max_fee: int = 0) -> dict:
    """Подписать authorization EIP-3009 и вернуть {'r','s','v','signer','recovered','type_hash','digest','authorization'}.

    Args:
        priv: приватный ключ подписанта (он же `from`).
        token: {'name','version','contract'} — домен токена (USDC Base:
            name='USD Coin', version='2').
        to: получатель; value: целые единицы токена (USDC 6 знаков!);
        nonce: bytes32 hex; valid_before/valid_after: unix-секунды;
        kind: 'receive' | 'transfer'; max_fee — только для контрактов, у которых
            это поле есть в структуре (у FiatToken его нет).

    Returns:
        подпись с самопроверкой signer==recovered, typeHash структуры (сверить с
        константой контракта), digest и сами поля authorization.

    ⚠ Разошлись адреса — ValueError с обоими: подписывать неверной структурой
        тише, чем верно, поэтому ошибки нет — есть сбой.
    """
    priv_i = int(priv, 16) if isinstance(priv, str) else priv
    type_sig = EVM_EIP3009_TYPES[kind]
    f = {'from': evm_keys_address(priv_i), 'to': to, 'value': value, 'nonce': nonce,
         'validAfter': valid_after, 'validBefore': valid_before, 'maxFee': max_fee}
    order = _evm_eip3009_order(type_sig)
    fields = {n: f[n] for n in order}
    domain = evm_typed_domain(token['name'], token['version'], chain_id, token['contract'])
    digest = evm_typed_digest(type_sig, fields, domain)
    sig = evm_keys_sign(priv_i, digest)
    sig['type_hash'] = evm_typed_type_hash(type_sig)
    sig['digest'] = digest
    sig['authorization'] = {n: f[n] for n in order} | {'kind': kind}
    return sig


def evm_eip3009_calldata(sig: dict) -> str:
    """Calldata внешней функции токена (…WithAuthorization(…nonce,v,r,s)) под sig от evm_eip3009_authorize.

    Параметры вызова берутся из той же строки типа, что и подписанная структура,
    плюс v,r,s — поэтому calldata и digest не разъедутся. Слово v — uint8, r и s
    — те же bytes32, что вернул authorize.

    Returns:
        '0x…' — селектор + слова аргументов, готово в eth_call / eth_sendRawTransaction.
    """
    kind = sig['authorization']['kind']
    ts = EVM_EIP3009_FUNCS[kind]
    order = _evm_eip3009_order(ts)
    types = [p.strip().rsplit(' ', 1)[0] for p in ts[ts.index('(') + 1:-1].split(',')]
    a = dict(sig['authorization'], v=sig['v'], r=sig['r'], s=sig['s'])
    words = b''.join(_evm_field(t, a[n]) for t, n in zip(types, order))   # все поля статичные
    # селектор — по типам БЕЗ имён параметров: с именами keccak даёт чужой селектор
    canon = ts[:ts.index('(')] + '(' + ','.join(types) + ')'
    return '0x' + _evm_eip3009_keccak(canon.encode())[:4].hex() + words.hex()


# ─── приватное ─────────────────────────────────────────────────────────────

def _evm_eip3009_keccak(data: bytes) -> bytes:
    from Crypto.Hash import keccak
    h = keccak.new(digest_bits=256)
    h.update(data)
    return h.digest()


def _evm_eip3009_order(type_signature: str) -> list:
    body = type_signature[type_signature.index('(') + 1:-1]
    return [p.strip().rsplit(' ', 1)[1] for p in body.split(',')]


if __name__ == '__main__':
    import argparse
    import json

    ap = argparse.ArgumentParser(
        description='Authorization EIP-3009 одной командой: подписать перевод '
                    'токена, который исполнит получатель. Подпись отдаётся с '
                    'самопроверкой signer==recovered и typeHash — сверить его с '
                    'константой контракта дешевле, чем словить чужой 400-й.',
        epilog="КЛЮЧ --token '{\"name\":\"USD Coin\",\"version\":\"2\",\"contract\":\"0x…\"}' "
               "--chain 8453 --to 0x… --value 2800000 --nonce 0x… --before 1893456000 "
               "[--calldata]")
    ap.add_argument('priv', help='приватный ключ подписанта (hex)')
    ap.add_argument('--token', required=True,
                    help='домен токена JSON-ом: {"name","version","contract"}')
    ap.add_argument('--chain', type=int, required=True, help='chain id (Base = 8453)')
    ap.add_argument('--to', required=True, help='получатель')
    ap.add_argument('--value', type=int, required=True,
                    help='целые единицы токена (USDC 6 знаков: 1 USDC = 1000000)')
    ap.add_argument('--nonce', required=True, help='bytes32 hex')
    ap.add_argument('--before', type=int, required=True, help='validBefore, unix-секунды')
    ap.add_argument('--after', type=int, default=0, help='validAfter, unix-секунды')
    ap.add_argument('--kind', default='receive', choices=['receive', 'transfer'])
    ap.add_argument('--max-fee', type=int, default=0,
                    help='только для контрактов, у которых поле maxFee есть в '
                         'структуре; у FiatToken (USDC/EURC) его нет')
    ap.add_argument('--calldata', action='store_true',
                    help='добавить готовую calldata внешней функции токена')
    ns = ap.parse_args()

    try:
        out = evm_eip3009_authorize(
            ns.priv, json.loads(ns.token), ns.chain, ns.to, ns.value, ns.nonce,
            ns.before, valid_after=ns.after, kind=ns.kind, max_fee=ns.max_fee)
    except (KeyError, ValueError) as err:
        raise SystemExit(f'ошибка: {err}')
    # Самопроверка — часть ответа: подпись, под которой восстановился не тот
    # адрес, наружу не уходит (см. `evm_signing.md`).
    if out['signer'].lower() != out['recovered'].lower():
        raise SystemExit(f"ошибка: signer {out['signer']} != recovered {out['recovered']}")
    if ns.calldata:
        out['calldata'] = evm_eip3009_calldata(out)
    print(json.dumps(out, ensure_ascii=False, indent=1))
