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
