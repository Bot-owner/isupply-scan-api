# iSupply Scan na macOS — sestavení a nasazení na web

Rovná řeč na úvod, ať víš, do čeho jdeš: **hotovou macOS aplikaci ti nikdo
neudělá vzdáleně.** Sestavení `.app` běží jedině na Macu a notarizace probíhá
na Apple serverech pod tvým vývojářským účtem. Kód (launcher, server, build
skript) máš připravený — ale posledních pár kroků musíš udělat ty na Macu.

Na tvém webu je u macOS karty napsáno „Notarizováno Applem". Bez notarizace
Gatekeeper aplikaci u zákazníka zablokuje hláškou, že je od neznámého vývojáře.
Takže notarizace není volitelná — je to nutná podmínka, aby ta karta nelhala.

---

## Co budeš potřebovat

1. **Mac** s macOS 12 Monterey nebo novějším (ideálně Apple Silicon).
2. **Xcode Command Line Tools**: `xcode-select --install`
3. **Python 3.11+ z python.org** — ne ten systémový. Stáhni z
   https://www.python.org/downloads/macos/
4. **Apple Developer Program** — 99 USD ročně, https://developer.apple.com/programs/
   Bez něj sestavíš jen nepodepsanou verzi pro vlastní test, ne pro web.

---

## Krok 1 — Přenes projekt na Mac

Zkopíruj celou složku projektu na Mac. Musí obsahovat aspoň:

```
server.py  launcher.py  scan_quota.py  quota.py  provisioning.py
iphone-diagnostic.html  isupply_admin.html  support.html
model_colors.json  requirements.txt
build_macos.sh
```

Plus obrázky (`iS.png`, foto) a ikonu, pokud je máš.

> **Ikona:** macOS chce `.icns`, ne `.ico`. Když máš jen `iS.png`, build skript
> se ji pokusí vyrobit sám přes `sips` a `iconutil`. Kdyby to nevyšlo, appka
> jen dostane výchozí ikonu — funkčnost to neovlivní.

---

## Krok 2 — Otestuj nepodepsanou verzi

Nejdřív ověř, že se vůbec sestaví a nastartuje, než budeš řešit podpis:

```bash
chmod +x build_macos.sh
./build_macos.sh
```

Vznikne `dist/iSupply Scan.app`. Protože není podepsaná, macOS ji napoprvé
nepustí přímo — otevři ji přes **pravý klik → Otevřít** a potvrď. To je normální
u nepodepsaných aplikací; u finální notarizované verze to zákazník řešit nebude.

Zkontrola, že běží:

- vedle `.app` (nebo bez ní) dej `licence.key` s tvým testovacím klíčem
- aplikace se má otevřít v okně, načíst telefon přes USB, přečíst komponenty
- log najdeš v `~/Library/Logs/iSupply Scan/isupply-scan.log`

Data (licence, databáze, tabulka barev) se ukládají do
`~/Library/Application Support/iSupply Scan/` — bundle je totiž read-only,
takže vedle aplikace zapisovat nejde. Tohle je už ošetřené v kódu.

> **USB na Macu:** žádný ovladač neinstaluješ, `usbmuxd` je součást systému.
> To je oproti Windows výhoda — odpadá „nainstaluj iTunes".

---

## Krok 3 — Podpis a notarizace (ostrá verze pro web)

Tohle proběhne jednou za release. Nejdřív jednorázová příprava přihlašovacích
údajů k notarizaci:

```bash
# TEAMID najdeš na developer.apple.com → Membership
# app-specific password si vygeneruj na https://appleid.apple.com → Zabezpečení
xcrun notarytool store-credentials "isupply-notary" \
  --apple-id "tvuj@email.cz" \
  --team-id "TEAMID" \
  --password "xxxx-xxxx-xxxx-xxxx"
```

Zjisti přesný název svého podpisového certifikátu:

```bash
security find-identity -v -p codesigning
# vypíše např.: "Developer ID Application: Tomas Pavlata (TEAMID)"
```

Pak už jen:

```bash
export DEVELOPER_ID="Developer ID Application: Tvoje Jmeno (TEAMID)"
export NOTARY_PROFILE="isupply-notary"
./build_macos.sh --sign
```

Skript podepíše `.app`, zabalí do `.dmg`, pošle k notarizaci (pár minut čekání)
a připne razítko. Výsledek: **`dist/iSupply_Scan.dmg`** — to je soubor na web.

Ověř, že notarizace prošla:

```bash
spctl -a -vvv -t install "dist/iSupply Scan.app"
# ma vypsat: accepted, source=Notarized Developer ID
```

---

## Krok 4 — Nasazení na web

Na stránce máš u macOS karty tlačítko „Stáhnout pro macOS" jako *Již brzy* a pod
ním `Ve vývoji · dej mi vědět na info@isupply.cz`. Až budeš mít `.dmg`:

1. **Nahraj `iSupply_Scan.dmg`** tam, kde hostuješ Windows `.exe` (Railway
   repo `isupply-servis` nebo kde máš download bucket).

2. **Aktivuj tlačítko.** Karta má strukturu jako ta windowsová — vyměň
   neaktivní `<div>` za odkaz na soubor. Najdi v šabloně download stránky blok
   s `Stáhnout pro macOS` a nahraď mailto verzi odkazem:

   ```html
   <!-- PŘED (placeholder) -->
   <div class="dl-btn disabled">
     Stáhnout pro macOS
     <span>iSupply_Scan.dmg</span>
   </div>
   <p class="dl-note">Ve vývoji · dej mi vědět na info@isupply.cz</p>

   <!-- PO -->
   <a class="dl-btn" href="/download/iSupply_Scan.dmg" download>
     ↓ Stáhnout pro macOS
     <span>iSupply_Scan.dmg</span>
   </a>
   ```

3. **Odeber odznak „Již brzy"** z rohu macOS karty (element s textem `Již brzy`).

4. Podmínky pod tlačítkem už sedí — `macOS 12+`, `Apple Silicon & Intel`,
   `Notarizováno Applem`. Ta poslední teď platí doopravdy.

> Přesný CSS/HTML nechávám na tobě, protože nevidím aktuální šablonu download
> stránky. Když mi ji pošleš, tlačítko a odznak upravím přesně.

---

## Universal binary (Apple Silicon + Intel)

Build výše vytvoří aplikaci pro architekturu Macu, na kterém běží. Když chceš
jeden `.dmg` pro obě (M1/M2/M3 i starší Intel Macy), máš dvě cesty:

- **Jednodušší:** sestav zvlášť na Apple Silicon a zvlášť na Intelu, nabídni
  dva soubory. Pro začátek úplně stačí Apple Silicon — Intel Macy mizí.
- **Čistší:** universal2 build. Vyžaduje universal Python a všechny závislosti
  v universal variantě, což u `pymobiledevice3` a `pyobjc` může dát práci.
  Nech si to na později, až bude poptávka.

Karta na webu slibuje „Apple Silicon & Intel", takže než budeš mít obojí
ověřené, zvaž upravit text na „Apple Silicon" — ať zákazník s Intelem nestáhne
něco, co mu nepojede.

---

## Časté potíže

**„iSupply Scan is damaged and can't be opened"** — appka není správně
podepsaná/notarizovaná, nebo se `.dmg` poškodil přenosem. Zkontroluj `spctl`
z kroku 3.

**Aplikace se otevře, ale nevidí telefon** — na Macu je `usbmuxd` vestavěný,
takže to bývá kabel nebo nepotvrzený Trust. Připoj, odemkni, potvrď „Trust".

**Notarizace zamítnuta** — `xcrun notarytool log <id> --keychain-profile ...`
vypíše důvod. Nejčastěji chybí hardened runtime (skript ho nastavuje přes
`--options runtime`) nebo některá knihovna není podepsaná.

**Sken se neautorizuje** — to je licenční server, ne macOS. Stejné chování jako
na Windows: bez internetu se sken neodskenuje, protože kvóta se ověřuje online.
