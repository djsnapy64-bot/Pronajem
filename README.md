# 🏢 Automatický hlídač pronájmů městských bytů v Ostravě

Tento projekt automaticky monitoruje úřední desky a nabídky pronájmů městských bytů v Ostravě (aktuálně podporuje obvody **Moravská Ostrava a Přívoz** a **Ostrava-Poruba**).

## 🚀 Jak systém funguje

1. **Scraping webů:** Projde aktuální nabídky na webových portálech městských obvodů:
   - Portál nemovitostí MOaP (`nemovitostimoap.cz`) a úřední desku MOaP
   - Elektronickou úřední desku a nabídky bytů Ostrava-Poruba (`poruba.ostrava.cz`)
2. **Stažení & čtení PDF:** U nových nabídek stáhne přiložené PDF dokumenty (záměry, žádosti k výběrovým řízením, vyhlášky) a knihovnou `pypdf` z nich extrahuje text.
3. **Analýza přes Google Gemini:** Text a dokument odešle modelu **Gemini 2.5 Flash** (či 1.5 Flash), který pomocí strukturovaného JSON výstupu (Pydantic) extrahuje:
   - zda jde skutečně o byt k pronájmu (`is_apartment_offer`)
   - přesnou adresu a lokalitu (`address`)
   - dispozici (`disposition`, např. 2+1, 2+kk)
   - podlahovou plochu v m² (`floor_area_m2`)
   - minimální čisté nájemné v Kč/měsíc (`min_rent_czk`)
   - termín uzávěrky podání přihlášek (`deadline`)
   - typ řízení (`auction_or_fixed`, např. výběrové řízení obálkovou metodou nebo licitace)
   - stručné shrnutí podmínek a termínů prohlídek
4. **Filtrování:** Ověří, zda nabídka odpovídá vašim požadavkům (např. min. dispozice 2+kk, nájem do 15 000 Kč).
5. **Notifikace na Telegram:** Pokud byt projde filtrem, obdržíte přehlednou zprávu s přímým odkazem na detail nabídky i PDF.
6. **Deduplikace v SQLite:** Zpracované záznamy se ukládají do lokální databáze `seen_items.db`, aby vám nechodily duplicitní zprávy.

---

## 🛠️ Rychlá instalace a spuštění lokálně

### 1. Klonování a instalace závislostí
```bash
cd ostrava_byty_checker
python -m venv venv
# Na Windows:
venv\Scripts\activate
# Na Linux/macOS:
source venv/bin/activate

pip install -r requirements.txt
```

### 2. Konfigurace `.env`
Zkopírujte vzorový soubor a doplňte své klíče:
```bash
cp .env.example .env
```

Obsah `.env`:
```env
GEMINI_API_KEY=AIzaSy...
GEMINI_MODEL=gemini-2.5-flash

TELEGRAM_BOT_TOKEN=123456789:ABC...
TELEGRAM_CHAT_ID=123456789

# Filtry
FILTER_ALLOWED_DISPOSITIONS=2+1,2+kk,3+1,3+kk,4+1,4+kk
FILTER_MAX_RENT=15000
FILTER_MIN_AREA_M2=40
SCRAPE_DISTRICTS=moap,poruba
DB_PATH=seen_items.db
```

### 3. Získání potřebných klíčů:
- **Google Gemini API Key:** Zdarma získáte na [Google AI Studio](https://aistudio.google.com/).
- **Telegram Bot Token:** Napište uživateli `@BotFather` na Telegramu a vytvořte nového bota příkazem `/newbot`.
- **Telegram Chat ID:** Napište botovi `@userinfobot` nebo pošlete zprávu svému novému botovi a zjistěte své ID přes `https://api.telegram.org/bot<TOKEN>/getUpdates`.

### 4. Spuštění
```bash
python main.py
```

---

## ⏰ Automatický běh na GitHub Actions (zdarma)

V repozitáři je připraven workflow soubor `.github/workflows/checker.yml`, který skript spouští **každý den v 7:00 UTC**.

### Jak nastavit GitHub:
1. Nahrajte tento adresář do svého (soukromého či veřejného) GitHub repozitáře.
2. V repozitáři přejděte do **Settings -> Secrets and variables -> Actions**.
3. V záložce **Secrets** klikněte na **New repository secret** a přidejte:
   - `GEMINI_API_KEY`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. V **Settings -> Actions -> General -> Workflow permissions** zaškrtněte volbu:
   - **Read and write permissions** (aby GitHub Actions mohl uložit `seen_items.db` zpět do repozitáře a pamatovat si, co už bylo odesláno).
5. V záložce **Actions** můžete workflow kdykoliv spustit ručně tlačítkem **Run workflow**.
