# Nasazení bezpečnostních oprav — přesný postup

Pořadí je důležité. Kdyby ses zastavil uprostřed, aplikace poběží dál jako dřív
(jen bez ověřování podpisu), nic se nerozbije.

---

## 1. Vygeneruj pár klíčů

Kdekoliv na svém počítači, ve složce projektu:

```powershell
python -c "from cryptography.hazmat.primitives.asymmetric import rsa; from cryptography.hazmat.primitives import serialization as s; k=rsa.generate_private_key(public_exponent=65537,key_size=2048); open('licence_private.pem','wb').write(k.private_bytes(s.Encoding.PEM,s.PrivateFormat.PKCS8,s.NoEncryption())); open('licence_public.pem','wb').write(k.public_key().public_bytes(s.Encoding.PEM,s.PublicFormat.SubjectPublicKeyInfo)); print('hotovo')"
```

Vzniknou dva soubory:

- `licence_private.pem` — **tajný**, patří jen na Railway. Nikdy do gitu.
- `licence_public.pem` — veřejný, půjde přímo do kódu aplikace.

Hned přidej do `.gitignore`:

```
licence_private.pem
```

> Pozn.: soubor `.gitignore` u tebe leží jako `gitignore` bez tečky — takový
> soubor Git ignoruje jinak řečeno vůbec. Přejmenuj ho.

---

## 2. Railway: nastav privátní klíč

V projektu `heartfelt-learning` → služba `isupply-scan-api` → Variables:

- název: `LICENCE_PRIVATE_KEY`
- hodnota: **celý obsah** `licence_private.pem`, včetně řádků
  `-----BEGIN PRIVATE KEY-----` a `-----END PRIVATE KEY-----`

Railway víceřádkové hodnoty zvládne. Kdyby dělaly potíže, nahraď konce řádků
za `\n` — kód si to převede zpátky.

Po nasazení zkontroluj log. **Nesmí** tam být:

```
[licence] VAROVANI: LICENCE_PRIVATE_KEY neni nastaveny
```

---

## 3. Aplikace: vlož veřejný klíč do kódu

Otevři `server.py`, najdi:

```python
LICENCE_PUBLIC_KEY = """
"""
```

a mezi uvozovky vlož **celý obsah** `licence_public.pem`, včetně BEGIN/END řádků.
Výsledek vypadá takhle:

```python
LICENCE_PUBLIC_KEY = """
-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA...
-----END PUBLIC KEY-----
"""
```

**Musí být v kódu, ne v souboru vedle aplikace.** Kdyby se načítal ze souboru,
stačí ho podvrhnout a celá ochrana padá.

---

## 4. Doinstaluj závislost a přebuilduj

```powershell
pip install cryptography
```

`requirements.txt` už ji obsahuje (kvůli Railway). Pak nový build:

```powershell
BUILD_WINDOWS.bat
```

Do `BUILD_WINDOWS.bat` si zároveň přidej řádek, ať se kopíruje tabulka barev:

```bat
copy /Y model_colors.json dist\model_colors.json
```

---

## 5. Ověř, že to funguje

Spusť aplikaci a v konzoli sleduj start. **Nesmí** se objevit:

```
⚠ LICENCE_PUBLIC_KEY neni nastaveny - podpis tokenu se NEOVERUJE.
```

Pak zkus podvržení — musí selhat:

```powershell
Set-Content .token_cache "eyJhbGciOiJub25lIn0.eyJwbGFuIjoiZW50ZXJwcmlzZSJ9."
```

Odpoj se od internetu a spusť aplikaci. Musí licenci odmítnout, ne pustit dál.
Potom soubor smaž a připoj se zpátky.

---

## Co je po opravách skutečně ošetřené

| Díra | Stav |
|---|---|
| Ručně složený token v `.token_cache` | **zavřeno** — ověřuje se podpis |
| Podvržený licenční server přes `hosts` | **zavřeno** — token nepodepíše |
| Zkopírovaný token na jiný počítač | **zavřeno** — token je vázaný na HWID |
| Funkce tarifu podvržené odpovědí serveru | **zavřeno** — jdou v podepsaném tokenu |
| Excel export na tarifu bez něj | **zavřeno** — autorizuje `/api/feature` |
| Limity skenů | bylo v pořádku už předtím |

---

## Co ošetřené NENÍ a nikdy nebude

Aplikace běží na počítači zákazníka, takže **odhodlaný člověk si ji upraví**.
`iphone-diagnostic.html` leží vedle EXE jako obyčejný soubor a jde v něm smazat
libovolnou podmínku. Patchnout se dá i EXE.

Co to znamená prakticky: klientské kontroly odrazují běžného uživatele, ne
někoho, kdo to chce cíleně obejít. Skutečně nepřekonatelné jsou jen věci,
o kterých rozhoduje **tvůj server**:

- kvóty skenů — server autorizuje každý sken, `ALLOW_OFFLINE = False`
- platnost licence — bez podpisu se token neprojde

Proto doporučení do budoucna: co má být opravdu zamčené, ať dělá server.
Analýza panic logů je ideální kandidát — logy z telefonu vytáhne klient, ale
vyhodnocení ať proběhne na Railway a vrátí se jen tarifům, které na něj mají
nárok. Pak nepomůže žádná úprava HTML.

---

## Poznámka k rotaci klíčů

Až budeš privátnit GitHub repo (máš to v plánu), rotuj i `SECRET_KEY`.
Privátní klíč z kroku 1 v repu nikdy nebyl, takže ten rotovat nemusíš —
pokud se ti ovšem nedostal do commitu omylem.
