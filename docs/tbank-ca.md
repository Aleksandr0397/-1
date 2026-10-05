# Дополнительный CA для TLS Т‑Инвестиций

[Официальные сетевые требования Т‑Банка](https://developer.tbank.ru/invest/intro/developer/network)
указывают на установку сертификатов Минцифры при ошибке проверки TLS.
В отдельном сервисе Render добавляется только корневой сертификат
`Russian Trusted Root CA`. Обычные системные CA, проверка имени сервера и
проверка цепочки сохраняются.

## Установка в Render

Команда сборки из корня репозитория:

```bash
python scripts/install-tbank-ca.py --out state/tbank-ca.pem
```

Значение переменной Render `TINVEST_CA_FILE`:

```text
/opt/render/project/src/state/tbank-ca.pem
```

Скрипт проверяет SHA‑256 DER, срок действия закреплённого сертификата,
единственность сертификата и возможность загрузки в стандартный SSLContext.
Он атомарно создаёт дополнительный PEM из `certs/russian-trusted-root-ca.pem`.
Загрузка из интернета при сборке не требуется. Изменённый сертификат или
дополнительные сертификаты в исходном файле вызывают ошибку до замены результата.

Адаптер использует стандартный контекст Python и добавляет файл к его доверию:

```python
context = ssl.create_default_context()
context.load_verify_locations(cafile=os.environ["TINVEST_CA_FILE"])
```

`verify_mode` остаётся `ssl.CERT_REQUIRED`, `check_hostname` — `True`.
Этот файл содержит только дополнительный корень: его не следует задавать как
`SSL_CERT_FILE`, заменяющий стандартный пакет доверия.
Промежуточные сертификаты не добавляются как отдельные доверенные корни.

Установка CA в Render относится к TLS нового сервиса. Она не меняет проверку,
которую выполняет внешний прокси текущей облачной среды; её ошибка HTTP 503
не исправляется установкой CA внутри локального Python.

## Источник и проверка подлинности

2026‑10‑05 сертификаты получены через обычный унаследованный HTTPS‑прокси;
проверка TLS включена. Оба независимых адреса корня вернули HTTP 200 и
`curl ssl_verify_result=0`:

- [Госуслуги: PEM корневого CA](https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt),
  портал установки — [gosuslugi.ru/tls](https://www.gosuslugi.ru/tls).
- [Ростелеком: тот же корневой CA](https://company.rt.ru/cdp/rootca_ssl_rsa2022.crt),
  HTTPS‑редирект на `www.company.rt.ru`.
  [Официальный корпоративный сайт](https://company.rt.ru/) подтверждает владельца.
  Этот адрес также указан в подписанном расширении Authority Information Access
  официального промежуточного сертификата 2022 года; для скачивания использован HTTPS.

Скачанные PEM корня совпали побайтно. SHA‑256 исходного PEM с окончаниями CRLF:
`936a43fea6e8e525bcc0f81acd9c3d21b4fc4b9b68acea7906d698005afc6504`.
Сохранённый в репозитории PEM имеет каноническое представление Python
`ssl.DER_cert_to_PEM_cert`; его SHA‑256:
`aa800ef345422d6158c6fafe1c06c429dbda21c3df4bb1ccb45a920ec1111399`.
Представление PEM не влияет на закреплённый ниже SHA‑256 самого DER сертификата.
Хеши рассчитаны по этим проверенным загрузкам и сопоставлены между источниками;
отдельная официальная публикация полного отпечатка не найдена.

Корень:

- Subject и Issuer:
  `C=RU, O=The Ministry of Digital Development and Communications, CN=Russian Trusted Root CA`.
- SHA‑256 DER: `d26d2d0231b7c39f92cc738512ba54103519e4405d68b5bd703e9788ca8ecf31`.
- Срок действия: `2022‑03‑01 21:04:15 UTC` — `2032‑02‑27 21:04:15 UTC`.
- Серийный номер: `0x1000`; RSA 4096; `CA:TRUE, pathlen:4`.

Промежуточные сертификаты проверены только для подтверждения цепочки:

| Сертификат | Официальный PEM | SHA‑256 DER | Действует до, UTC |
| --- | --- | --- | --- |
| Russian Trusted Sub CA, 2022 | [Скачать](https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt) | `bbbde2103e790b999ec62bd03cf625a5a2e7c316e10afe6a490eedead8b3fd9b` | `2027‑03‑06 11:25:19` |
| Russian Trusted Sub CA, 2024 | [Скачать](https://gu-st.ru/content/lending/russian_trusted_sub_ca_2024_pem.crt) | `2155785036c900dbb5f1bb2a1569c80c55595bd6bf94867a29bbddbc7d88a3f2` | `2029‑07‑19 12:50:41` |

Проверка подписи корня и подписи промежуточного CA 2024 года выполнена OpenSSL
с единственным закреплённым корнем, без системного trust store:

```bash
openssl verify -no-CApath -no-CAstore -check_ss_sig \
  -CAfile certs/russian-trusted-root-ca.pem certs/russian-trusted-root-ca.pem
openssl verify -no-CApath -no-CAstore -check_ss_sig \
  -CAfile certs/russian-trusted-root-ca.pem /tmp/russian_trusted_sub_ca_2024_pem.crt
```

Обе проверки вернули `OK`, код 0. Путь `/tmp/` во второй команде обозначает
загруженный официальный промежуточный сертификат и не входит в репозиторий.
Прямые обращения к порталу Т‑Банка и текущему SDK в этой облачной среде
возвращали HTTP 503; копии CA из неофициальных репозиториев не использовались.

## Локальная проверка установщика

```bash
python -m unittest discover -s tests -p 'test_tbank_ca.py' -v
```

Тесты проверяют точный DER‑отпечаток, сохранение системных CA и настроек TLS,
отказ при изменении сертификата или добавлении второго сертификата, а также
сохранение существующего выходного файла при отказе.
