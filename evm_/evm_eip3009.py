"""EIP-3009 authorization'ы USDC: components + подпись + самопроверка.

Gasless-путь escrow (funding-authorization для чужого submit, reveal-preimage)
требует подписать ReceiveWithAuthorization/TransferWithAuthorization с точным
полем полей. Здесь оба вида одним вызовом: поля берутся из канонического
типа, подписант сверяется с адресом ключа — молчаливая подмена полей видна
сразу, а не на чужом 400-ом.
"""

from evm_.evm_typed import evm_typed_digest, evm_typed_domain
from evm_.evm_keys import evm_keys_sign, evm_keys_address

EVM_EIP3009_TYPES = {
    'receive': 'ReceiveWithAuthorization(address from,address to,uint256 value,'
               'bytes32 nonce,uint256 validAfter,uint256 validBefore)',
    'transfer': 'TransferWithAuthorization(address from,address to,uint256 value,'
                'uint256 maxFee,bytes32 nonce,uint256 validAfter,uint256 validBefore)',
}


def evm_eip3009_authorize(priv, token: dict, chain_id: int, to: str, value: int,
                          nonce: str, valid_before: int, valid_after: int = 0,
                          kind: str = 'receive', max_fee: int = 0) -> dict:
    """Подписать authorization EIP-3009 и вернуть {'r','s','v','signer','recovered','digest','authorization'}.

    Args:
        priv: приватный ключ подписанта (он же `from`).
        token: {'name','version','contract'} — домен токена (USDC Base:
            name='USD Coin', version='2').
        to: получатель escrow; value: целые единицы токена (USDC 6 знаков!);
        nonce: bytes32 hex; valid_before/valid_after: unix-секунды;
        kind: 'receive' | 'transfer'; max_fee — только для transfer.

    ⚠ value — целые единицы (1 USDC = 1), а не wei-подобные 10^18; scale'у
        шестизначного токена здесь не верят — передают как есть.
    """
    priv_i = int(priv, 16) if isinstance(priv, str) else priv
    frm = evm_keys_address(priv_i)
    f = {'from': frm, 'to': to, 'value': value, 'nonce': nonce,
         'validAfter': valid_after, 'validBefore': valid_before}
    if kind == 'transfer':
        f['maxFee'] = max_fee
    else:
        f.pop('maxFee', None)
    fields = {n: f[n] for n in _order(EVM_EIP3009_TYPES[kind])}
    domain = evm_typed_domain(token['name'], token['version'], chain_id, token['contract'])
    digest = evm_typed_digest(EVM_EIP3009_TYPES[kind], fields, domain)
    sig = evm_keys_sign(priv_i, digest)
    sig['digest'] = digest
    sig['authorization'] = dict(f, kind=kind)
    return sig


# ─── приватное ─────────────────────────────────────────────────────────────

def _order(type_signature: str) -> list:
    body = type_signature[type_signature.index('(') + 1:-1]
    return [p.strip().rsplit(' ', 1)[1] for p in body.split(',')]
