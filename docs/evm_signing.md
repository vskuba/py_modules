# EVM-подпись без eth-библиотек (`evm_keys`, `evm_typed`, `evm_eip3009`)

> Кошелёк проекта без web3/eth_account: адрес из ключа, подпись EIP-712 digest'а,
> authorization'ы EIP-3009 для gasless-escrow. Чистый secp256k1 на cryptography
> + keccak на pycryptodome; ленивые импорты — без пакетов импорт проходит,
> вызов — нет.

## Как устроено

- `evm_keys` — арифметика точек (add/double/mul double-and-add), адрес EIP-55
  из pub = priv·G, подпись `ec.ECDSA(Prehashed(SHA256))` по готовому digest'у,
  recovery перебором четырёх кандидатов (`rec&1` — чётность y, `rec&2` — x=r+n).
- `evm_typed` — канон EIP-712: typeHash от строки типа, structHash из полей в
  порядке строки, digest `0x1901‖domain‖struct`. Порядок полей диктует строка
  типа, а не словарь caller'а — поля не разъезжаются.
- `evm_eip3009` — Receive/TransferWithAuthorization поверх typed: поля берутся
  из канонического типа, подписант обязан сойтись с адресом ключа (ValueError
  иначе).

## Грабли

- `3·x²` в удвоении, `u1 = −z·r⁻¹` в recovery, pub из priv·G — три ошибки,
  каждая из которых даёт «валидную» подпись с вероятностью ~½; самопроверка
  signer==recovered на каждом вызове — не украшение, а единственный сторож.
- value для 6-значного токена — целые единицы; scale здесь не угадывается.
- v уже `27+rec_id` (27..30), не 0/1.
