#!/bin/bash
# ============================================================================
#  iSupply Scan — build macOS aplikace (.app + .dmg)
#
#  SPOUSTET JEN NA MACU. Windows/Linux .app nesestavi a hlavne nenotarizuje.
#  Predpoklady (viz NASAZENI_MACOS.md):
#    - macOS 12+ s nainstalovanym Xcode Command Line Tools
#    - Python 3.11+ z python.org (NE ten systemovy /usr/bin/python3)
#    - pro podpis + notarizaci: Apple Developer ucet (99 USD/rok)
#
#  Pouziti:
#    chmod +x build_macos.sh
#    ./build_macos.sh              # jen sestavi .app (nepodepsany - pro test)
#    ./build_macos.sh --sign       # sestavi, podepise a notarizuje (ostry release)
# ============================================================================
set -e
cd "$(dirname "$0")"

APP_NAME="iSupply Scan"
BUNDLE_ID="cz.isupply.scan"
VERSION="1.0.6"

echo ""
echo "  ============================================"
echo "   iSupply Scan — build macOS"
echo "  ============================================"
echo ""

# ── Kontrola, ze jsme na Macu ──
if [[ "$(uname)" != "Darwin" ]]; then
  echo "  [CHYBA] Tenhle skript bezi jen na macOS."
  exit 1
fi

# ── Kontrola potrebnych souboru ──
for f in server.py launcher.py scan_quota.py iphone-diagnostic.html \
         isupply_admin.html support.html model_colors.json; do
  if [[ ! -f "$f" ]]; then
    echo "  [CHYBA] Ve slozce chybi $f"
    exit 1
  fi
done
echo "  [1/5] Vsechny zdrojove soubory na miste"

# ── Ikona: potrebujeme .icns. Kdyz je jen .png, zkusime prevest ──
ICON_ARG=""
if [[ -f "icon.icns" ]]; then
  ICON_ARG="--icon icon.icns"
elif [[ -f "iS.png" ]]; then
  echo "  [info] icon.icns neni, generuji z iS.png"
  mkdir -p icon.iconset
  sips -z 16 16     iS.png --out icon.iconset/icon_16x16.png      >/dev/null 2>&1 || true
  sips -z 32 32     iS.png --out icon.iconset/icon_16x16@2x.png   >/dev/null 2>&1 || true
  sips -z 128 128   iS.png --out icon.iconset/icon_128x128.png    >/dev/null 2>&1 || true
  sips -z 256 256   iS.png --out icon.iconset/icon_128x128@2x.png >/dev/null 2>&1 || true
  sips -z 512 512   iS.png --out icon.iconset/icon_512x512.png    >/dev/null 2>&1 || true
  sips -z 1024 1024 iS.png --out icon.iconset/icon_512x512@2x.png >/dev/null 2>&1 || true
  iconutil -c icns icon.iconset -o icon.icns >/dev/null 2>&1 && ICON_ARG="--icon icon.icns" || true
  rm -rf icon.iconset
fi

# ── Zavislosti ──
echo "  [2/5] Instaluji zavislosti..."
python3 -m pip install --quiet --upgrade \
  pyinstaller flask flask-cors PyJWT cryptography \
  pymobiledevice3 readchar requests pywebview pyobjc

# ── Cisteni ──
echo "  [3/5] Cistim stary build..."
rm -rf build dist "${APP_NAME}.app"

# ── Build .app ──
# POZOR na rozdily proti Windows:
#   - oddelovac v --add-data je DVOJTECKA (:), ne strednik
#   - --windowed misto --noconsole vytvori .app bundle
#   - winreg se NEPRIBALUJE (na macOS neexistuje)
#   - misto edgechromium se pouzije cocoa (WKWebView) - pywebview si vybere sam
echo "  [4/5] Sestavuji .app (chvili to trva)..."
python3 -m PyInstaller \
  --windowed \
  --name "${APP_NAME}" \
  --osx-bundle-identifier "${BUNDLE_ID}" \
  ${ICON_ARG} \
  --add-data "iphone-diagnostic.html:." \
  --add-data "isupply_admin.html:." \
  --add-data "support.html:." \
  --add-data "model_colors.json:." \
  $([ -f "iS.png" ] && echo "--add-data iS.png:.") \
  $([ -f "photo_2026-07-01_01-43-29.jpg" ] && echo "--add-data photo_2026-07-01_01-43-29.jpg:.") \
  --collect-all flask \
  --collect-all flask_cors \
  --collect-all jwt \
  --collect-all pymobiledevice3 \
  --collect-all readchar \
  --collect-all webview \
  --hidden-import pymobiledevice3.services.mobile_activation \
  --hidden-import scan_quota \
  --hidden-import encodings \
  --hidden-import encodings.utf_8 \
  --hidden-import encodings.ascii \
  --copy-metadata readchar \
  --copy-metadata pymobiledevice3 \
  --hidden-import requests \
  launcher.py

if [[ ! -d "dist/${APP_NAME}.app" ]]; then
  echo "  [CHYBA] Build selhal, .app nevznikl."
  exit 1
fi
echo "  [5/5] Hotovo: dist/${APP_NAME}.app"

# ── Podpis + notarizace (jen s --sign) ──
if [[ "$1" == "--sign" ]]; then
  echo ""
  echo "  === PODPIS A NOTARIZACE ==="
  : "${DEVELOPER_ID:?Nastav promennou DEVELOPER_ID (napr. 'Developer ID Application: Jmeno (TEAMID)')}"

  # Notarizace umi dva zpusoby prihlaseni:
  #   1) ulozeny profil v klicence (NOTARY_PROFILE) - pohodlne na vlastnim Macu
  #   2) primo udaje (APPLE_ID + APPLE_TEAM_ID + APPLE_APP_PASSWORD) - nutne
  #      v CI, kde zadna trvala klicenka neexistuje
  if [[ -n "${NOTARY_PROFILE:-}" ]]; then
    NOTARY_AUTH=(--keychain-profile "${NOTARY_PROFILE}")
  elif [[ -n "${APPLE_ID:-}" && -n "${APPLE_TEAM_ID:-}" && -n "${APPLE_APP_PASSWORD:-}" ]]; then
    NOTARY_AUTH=(--apple-id "${APPLE_ID}" --team-id "${APPLE_TEAM_ID}" \
                 --password "${APPLE_APP_PASSWORD}")
  else
    echo "  [CHYBA] Chybi pristup k notarizaci."
    echo "          Bud NOTARY_PROFILE, nebo APPLE_ID + APPLE_TEAM_ID + APPLE_APP_PASSWORD."
    exit 1
  fi

  APP="dist/${APP_NAME}.app"

  echo "  Podepisuji (hardened runtime)..."
  codesign --force --deep --options runtime --timestamp \
    --sign "${DEVELOPER_ID}" "${APP}"

  echo "  Bali do .dmg..."
  DMG="dist/iSupply_Scan.dmg"
  rm -f "${DMG}"
  hdiutil create -volname "${APP_NAME}" -srcfolder "${APP}" \
    -ov -format UDZO "${DMG}"

  echo "  Podepisuji .dmg..."
  codesign --force --sign "${DEVELOPER_ID}" "${DMG}"

  echo "  Posilam k notarizaci (nekolik minut)..."
  xcrun notarytool submit "${DMG}" "${NOTARY_AUTH[@]}" --wait

  echo "  Pripinam notarizacni razitko..."
  xcrun stapler staple "${DMG}"
  xcrun stapler staple "${APP}"

  echo ""
  echo "  ✓ HOTOVO: ${DMG} je podepsany a notarizovany."
  echo "    Tohle je soubor, ktery das na web ke stazeni."
else
  echo ""
  echo "  Vytvoren NEPODEPSANY .app (jen pro tvuj test na tomhle Macu)."
  echo "  Pro release na web spust: ./build_macos.sh --sign"
  echo "  (vyzaduje Apple Developer ucet - viz NASAZENI_MACOS.md)"
fi
