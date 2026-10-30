# Surfweer-alert

Checkt twee keer per dag de voorspelling (Open-Meteo, gratis, geen API-key) voor je spots
en stuurt een Telegram-bericht als er een goed surfvenster aankomt.

## Hoe het werkt
- Golfhoogte, swellperiode en swellrichting komen uit de Open-Meteo Marine API, wind uit de Forecast API.
- Per uur wordt gekeken of alles binnen de grenzen in `spots.toml` valt: golfhoogte, minimale periode,
  swellrichting, maximale wind en aflandige wind (of zo weinig wind dat de richting niet uitmaakt).
- Minstens 2 goede daglichturen achter elkaar = een alert.
- Per venster wordt een zekerheid berekend: het aandeel van de 51 ECMWF-ensembleruns met goede wind,
  en hoeveel van de golfmodellen het venster goed vinden. De laagste van de twee telt;
  onder `min_confidence` (standaard 60%) wordt de alert niet verstuurd.

## Instellen
1. Maak een Telegram-bot via @BotFather en noteer de token.
2. Stuur je bot een bericht en haal je chat-id op via
   `https://api.telegram.org/bot<TOKEN>/getUpdates`.
3. Zet deze map in een GitHub-repo en voeg de secrets `TELEGRAM_BOT_TOKEN` en `TELEGRAM_CHAT_ID` toe.
4. De alert draait om ~07:00 en ~18:00, het weekoverzicht elke zondag om ~08:00.
   Beide kun je ook handmatig starten via "Run workflow".

Lokaal testen: `python surf_alert.py --dry-run` of `python surf_alert.py --weekly --dry-run` (Python 3.11+, geen extra packages).
