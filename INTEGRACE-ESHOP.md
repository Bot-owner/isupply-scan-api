# iSupply Scan → e-shop: integrace přes API

iSupply Scan umí po vytištění štítku automaticky odeslat naskenovaný kus na
libovolný e-shop. Stačí, aby e-shop implementoval jeden HTTP endpoint (webhook)
podle této specifikace. Zákazník si v admin panelu scanu (`/admin` → **E-shop**)
nastaví URL endpointu a tajný klíč a integraci zapne.

## Jak to funguje

```
diagnostika → Tisk (technik vybere stav) → scan (server.py) → POST na váš endpoint (HTTPS)
```

Volání jde ze serveru scanu, ne z prohlížeče — klíč se tak nikde nezobrazí.

## Endpoint, který má e-shop implementovat

- **Metoda:** `POST`
- **Hlavičky:** `Content-Type: application/json` a `x-scan-key: <tajný klíč>`
  (endpoint MUSÍ ověřit shodu klíče, jinak vrátit `401`).

### Tělo požadavku (JSON)

Na e-shop se zapisují **jen parametry produktu**:

| pole        | typ            | zdroj                 | popis                                   |
|-------------|----------------|-----------------------|-----------------------------------------|
| `source`    | string         | scan                  | vždy `"isupply-scan"`                    |
| `model`     | string         | diagnostika (auto)    | např. `"iPhone 13"`                     |
| `capacity`  | string         | diagnostika (auto)    | např. `"128 GB"`, `"1 TB"`              |
| `color`     | string         | diagnostika (auto)    | barva, jak ji hlásí zařízení            |
| `condition` | string         | **technik ručně**     | stav: `A` / `B` / `C` / `zánovní` / `nový` |
| `imei`      | string \| null | diagnostika (auto)    | **jen k deduplikaci**, není to parametr produktu |
| `serial`    | string \| null | diagnostika (auto)    | **jen k deduplikaci**                   |
| `test`      | bool           | scan                  | přítomné a `true` jen u testovacího požadavku |

> **Stav zadává technik** při tisku (výběr z 5 možností), diagnostika ho
> neurčuje. `imei`/`serial` slouží pouze k tomu, aby dvojí tisk téhož kusu
> nevytvořil dva skladové kusy — nezobrazují se jako parametr produktu.

### Očekávaná odpověď (JSON)

- Test (`test: true`) → nic neukládat: `{ "ok": true, "test": true, "stored": false }`
- Duplicita (stejné IMEI/serial už skladem) → `{ "ok": true, "stored": false, "duplicate": true }`
- Kus uložen → `{ "ok": true, "stored": true }`
- Neplatný/chybějící stav → HTTP `400` s `{ "ok": false, "stored": false, "error": "..." }`
- Špatný klíč → HTTP `401`

Pole `stored` řídí hlášku ve scanu: `true` → „✓ Přidáno na sklad", `false` → tiše.

## Referenční implementace

E-shop `iSupply.cz` (`server.js`, endpoint `POST /api/inventory/add`) je hotová
referenční implementace — ověření klíče, validace stavu, dedupe podle IMEI/serial,
normalizace kapacity, příznak `test`. Lze ji dát zákazníkům jako vzor.

## Bezpečnost

- HTTPS + ověření `x-scan-key`; každý zákazník má svůj vlastní náhodný klíč.
- Doporučeno logovat příchozí kusy a případně omezit rate.
