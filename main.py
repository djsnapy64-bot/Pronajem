#!/usr/bin/env python3
"""
Městské Byty Ostrava - Automatický hlídač nových nabídek pronájmů městských bytů
Sleduje úřední desky a portály obvodů Moravská Ostrava a Přívoz (MOaP) a Ostrava-Poruba.
Stahuje přiložená PDF, pomocí Google Gemini LLM extrahuje strukturovaná data,
aplikuje filtry a posílá notifikace přes Telegram Bot API s deduplikací v SQLite.
"""

import os
import sys
import io
import re
import json
import logging
import sqlite3
import datetime
import urllib.parse
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import requests
from bs4 import BeautifulSoup
import pypdf
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# Načtení proměnných z .env souboru
load_dotenv()

# Nastavení logování
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("byty-ostrava")


# ==============================================================================
# 1. Konfigurace aplikace
# ==============================================================================
class Config:
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "").strip()
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()

    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    DB_PATH: str = os.getenv("DB_PATH", "seen_items.db").strip()

    # Filtry
    _allowed_disp = os.getenv("FILTER_ALLOWED_DISPOSITIONS", "2+1,2+kk,3+1,3+kk,4+1,4+kk")
    FILTER_ALLOWED_DISPOSITIONS: List[str] = [
        d.strip().lower().replace(" ", "") for d in _allowed_disp.split(",") if d.strip()
    ]
    FILTER_MAX_RENT: float = float(os.getenv("FILTER_MAX_RENT", "15000"))
    FILTER_MIN_AREA_M2: float = float(os.getenv("FILTER_MIN_AREA_M2", "0"))

    _districts = os.getenv("SCRAPE_DISTRICTS", "moap,poruba")
    SCRAPE_DISTRICTS: List[str] = [d.strip().lower() for d in _districts.split(",") if d.strip()]

    HTTP_TIMEOUT: int = 25
    USER_AGENT: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    @classmethod
    def validate(cls):
        missing = []
        if not cls.GEMINI_API_KEY:
            missing.append("GEMINI_API_KEY")
        if not cls.TELEGRAM_BOT_TOKEN:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not cls.TELEGRAM_CHAT_ID:
            missing.append("TELEGRAM_CHAT_ID")
        if missing:
            logger.warning(
                f"Upozornění: Chybí konfigurace v .env: {', '.join(missing)}. "
                "Skript poběží v testovacím režimu bez odesílání Telegram zpráv nebo volání Gemini."
            )


# ==============================================================================
# 2. Pydantic Schéma pro Gemini Structured Output
# ==============================================================================
class ApartmentOffer(BaseModel):
    is_apartment_offer: bool = Field(
        description="True pokud se jedná o záměr nebo nabídku pronájmu konkrétního bytu. "
        "False pokud jde o nebytový prostor, pozemek, garáž, prodej nebo obecnou vyhlášku."
    )
    address: str = Field(
        default="",
        description="Přesná adresa bytu (např. Fügnerova 742/6, byt č. 5, Ostrava-Přívoz)",
    )
    disposition: str = Field(
        default="",
        description="Dispozice bytu (např. 1+kk, 1+1, 2+kk, 2+1, 3+1, 3+kk)",
    )
    floor_area_m2: float = Field(
        default=0.0,
        description="Podlahová / celková výměra bytu v m2 jako číslo (např. 63.88). 0.0 pokud není uvedeno.",
    )
    min_rent_czk: float = Field(
        default=0.0,
        description="Minimální měsíční nájemné v Kč (čisté nájemné nebo vyvolávací cena). "
        "Pokud je v textu sazba za m2 (např. 110 Kč/m2/měsíc), spočítej celkový nájem = plocha * sazba.",
    )
    deadline: str = Field(
        default="",
        description="Termín uzávěrky podání přihlášek či odevzdání zalepených obálek (např. 25. 9. 2026)",
    )
    auction_or_fixed: str = Field(
        default="",
        description="Způsob výběru nájemce: 'výběrové řízení' (obálková metoda / nabídková cena), 'licitace' / 'aukce' nebo 'pevné nájemné'",
    )
    summary: str = Field(
        default="",
        description="Klíčové detaily: stav bytu, termíny prohlídek, kauce, případné podmínky.",
    )


# ==============================================================================
# 3. Databáze (Deduplikace záznamů v SQLite)
# ==============================================================================
class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_offers (
                    id TEXT PRIMARY KEY,
                    district TEXT,
                    title TEXT,
                    url TEXT,
                    pdf_url TEXT,
                    address TEXT,
                    disposition TEXT,
                    floor_area_m2 REAL,
                    min_rent_czk REAL,
                    deadline TEXT,
                    auction_or_fixed TEXT,
                    passed_filter INTEGER,
                    notified INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()

    def is_seen(self, item_id: str) -> bool:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM seen_offers WHERE id = ?", (item_id,))
            return cursor.fetchone() is not None

    def mark_seen(
        self,
        item_id: str,
        district: str,
        title: str,
        url: str,
        pdf_url: str,
        offer: Optional[ApartmentOffer] = None,
        passed_filter: bool = False,
        notified: bool = False,
    ):
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO seen_offers (
                    id, district, title, url, pdf_url,
                    address, disposition, floor_area_m2, min_rent_czk,
                    deadline, auction_or_fixed, passed_filter, notified
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    district,
                    title,
                    url,
                    pdf_url,
                    offer.address if offer else "",
                    offer.disposition if offer else "",
                    offer.floor_area_m2 if offer else 0.0,
                    offer.min_rent_czk if offer else 0.0,
                    offer.deadline if offer else "",
                    offer.auction_or_fixed if offer else "",
                    1 if passed_filter else 0,
                    1 if notified else 0,
                ),
            )
            conn.commit()


# ==============================================================================
# 4. Scraper (Získávání kandidátních nabídek)
# ==============================================================================
@dataclass
class RawCandidate:
    id: str
    district: str
    title: str
    url: str
    pdf_urls: List[str] = field(default_factory=list)
    web_text: str = ""


class MunicipalityScraper:
    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.config.USER_AGENT})

    def get_candidates(self) -> List[RawCandidate]:
        candidates: List[RawCandidate] = []
        if "moap" in self.config.SCRAPE_DISTRICTS:
            try:
                candidates.extend(self._scrape_moap())
            except Exception as e:
                logger.error(f"Chyba při scrapování MOaP: {e}", exc_info=True)

        if "poruba" in self.config.SCRAPE_DISTRICTS:
            try:
                candidates.extend(self._scrape_poruba())
            except Exception as e:
                logger.error(f"Chyba při scrapování Poruby: {e}", exc_info=True)

        logger.info(f"Celkem nalezeno {len(candidates)} kandidátních položek z obvodů.")
        return candidates

    def _scrape_moap(self) -> List[RawCandidate]:
        """
        MOaP: Sleduje jak dedikovaný realitní portál www.nemovitostimoap.cz,
        tak elektronickou úřední desku moap.ostrava.cz.
        """
        results: List[RawCandidate] = []
        base_url = "https://www.nemovitostimoap.cz"
        catalog_url = f"{base_url}/cz/nemovitosti/"

        logger.info(f"[MOaP] Stahuji katalog nemovitostí: {catalog_url}")
        resp = self.session.get(catalog_url, timeout=self.config.HTTP_TIMEOUT)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            # Odkazy na byty mají tvar cz/nemovitosti/XXX-...byt...html
            apartment_links = set()
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if "cz/nemovitosti/" in href and "byt" in href.lower() and href.endswith(".html"):
                    full_url = urllib.parse.urljoin(base_url, href)
                    apartment_links.add(full_url)

            logger.info(f"[MOaP] Nalezeno {len(apartment_links)} stránek s byty v katalogu.")
            for url in apartment_links:
                # Extrahujeme ID z URL (např. 372-fugnerova...)
                match = re.search(r"/nemovitosti/(\d+-[^/]+)\.html", url)
                item_id = f"moap-{match.group(1)}" if match else f"moap-{url}"

                detail_resp = self.session.get(url, timeout=self.config.HTTP_TIMEOUT)
                if detail_resp.status_code != 200:
                    continue

                detail_soup = BeautifulSoup(detail_resp.text, "html.parser")
                title = detail_soup.find("h1")
                title_text = title.get_text(strip=True) if title else "MOaP Nabídka bytu"

                # Extrakce textu ze stránky
                body_text = detail_soup.get_text(" ", strip=True)

                # Vyhledání všech přiložených PDF souborů (žádosti, podmínky, záměry)
                pdf_urls = []
                for pa in detail_soup.find_all("a", href=True):
                    phref = pa["href"].strip()
                    if phref.lower().endswith(".pdf"):
                        pdf_urls.append(urllib.parse.urljoin(base_url, phref))

                results.append(
                    RawCandidate(
                        id=item_id,
                        district="Moravská Ostrava a Přívoz",
                        title=title_text,
                        url=url,
                        pdf_urls=pdf_urls,
                        web_text=body_text[:5000],
                    )
                )

        # Doplněk: Úřední deska MOaP
        try:
            moap_board_url = "https://moap.ostrava.cz/cs/radnice/urad/uredni-deska"
            board_resp = self.session.get(moap_board_url, timeout=self.config.HTTP_TIMEOUT)
            if board_resp.status_code == 200:
                bsoup = BeautifulSoup(board_resp.text, "html.parser")
                for table in bsoup.find_all("table", id="officialdesk"):
                    t_text = table.get_text(" ", strip=True)
                    if any(kw in t_text.lower() for kw in ["pronájem", "pronájmu", "záměr", "byt"]):
                        # Extrakce čísla oznámení a detailu
                        num_el = table.find("tr", class_="desk-detail")
                        num = num_el.find("td").get_text(strip=True) if num_el and num_el.find("td") else ""
                        item_id = f"moap-deska-{num}" if num else f"moap-deska-{hash(t_text)}"
                        results.append(
                            RawCandidate(
                                id=item_id,
                                district="Moravská Ostrava a Přívoz (Úřední deska)",
                                title=t_text[:120],
                                url=moap_board_url,
                                pdf_urls=[],
                                web_text=t_text,
                            )
                        )
        except Exception as e:
            logger.warning(f"[MOaP] Chyba při čtení úřední desky: {e}")

        return results

    def _scrape_poruba(self) -> List[RawCandidate]:
        """
        Poruba: Sleduje elektronickou úřední desku (kde jsou vyvěšovány záměry s PDF)
        a sekci Nabídky pronájmů bytů.
        """
        results: List[RawCandidate] = []
        base_url = "https://poruba.ostrava.cz"
        board_url = f"{base_url}/cs/radnice/uredni-deska"

        logger.info(f"[Poruba] Stahuji úřední desku: {board_url}")
        resp = self.session.get(board_url, timeout=self.config.HTTP_TIMEOUT)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            items = soup.find_all("div", class_=lambda c: c and "officialdesk-item" in c)
            for item in items:
                text_content = item.get_text(" ", strip=True)
                title_el = item.find("div", class_="off__title")
                title = title_el.get_text(strip=True) if title_el else "Oznámení úřední desky"

                # Zajímavé jsou položky obsahující klíčová slova týkající se bytů a záměrů
                is_relevant = any(
                    kw in text_content.lower()
                    for kw in ["záměr", "pronájem", "pronájmu", "byt", "bytu", "bytů", "výběrové řízení"]
                )
                if not is_relevant:
                    continue

                number_el = item.find("span", class_="off__number")
                item_number = number_el.get_text(strip=True) if number_el else str(hash(title))
                item_id = f"poruba-deska-{item_number.replace('/', '-')}"

                # Extrakce odkazů na PDF
                pdf_urls = []
                for a in item.find_all("a", href=True):
                    href = a["href"].strip()
                    if ".pdf" in href.lower() or "@@download/file" in href:
                        pdf_urls.append(urllib.parse.urljoin(base_url, href))

                results.append(
                    RawCandidate(
                        id=item_id,
                        district="Ostrava-Poruba",
                        title=f"{item_number}: {title}",
                        url=board_url,
                        pdf_urls=pdf_urls,
                        web_text=text_content,
                    )
                )

        return results


# ==============================================================================
# 5. Zpracování PDF dokumentů
# ==============================================================================
def download_and_extract_pdf_text(pdf_url: str, session: requests.Session) -> Tuple[str, bytes]:
    """Stáhne PDF a extrahuje z něj text pomocí pypdf."""
    try:
        resp = session.get(pdf_url, timeout=Config.HTTP_TIMEOUT)
        if resp.status_code != 200:
            logger.warning(f"Nelze stáhnout PDF ({resp.status_code}): {pdf_url}")
            return "", b""

        pdf_bytes = resp.content
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        extracted_pages = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text()
            if text:
                extracted_pages.append(text.strip())

        full_text = "\n\n--- DALŠÍ STRANA PDF ---\n\n".join(extracted_pages)
        return full_text, pdf_bytes
    except Exception as e:
        logger.error(f"Chyba při čtení PDF {pdf_url}: {e}")
        return "", b""


# ==============================================================================
# 6. LLM Parser s Google GenAI SDK (Gemini)
# ==============================================================================
def parse_with_gemini(
    text_content: str,
    pdf_bytes: Optional[bytes] = None,
    config: Optional[Config] = None,
) -> Optional[ApartmentOffer]:
    """
    Analyzuje text nabídky a/nebo PDF dokument pomocí modelu Google Gemini
    a vrací validovaný strukturovaný objekt ApartmentOffer.
    """
    if not Config.GEMINI_API_KEY:
        logger.info("GEMINI_API_KEY není nastavena, přeskakuji LLM vyhodnocení.")
        return None

    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=Config.GEMINI_API_KEY)

        prompt = (
            "Jsi expert na analýzu úředních desek a záměrů městských obvodů Ostrava "
            "(Moravská Ostrava a Přívoz, Poruba atd.). "
            "Analyzuj následující text nebo dokument a extrahuj přesné informace o nabídce pronájmu bytu.\n"
            "Pravidla:\n"
            "1. 'is_apartment_offer' nastav na TRUE pouze tehdy, pokud se jedná o záměr/nabídku pronájmu konkrétního bytu. "
            "Pokud jde o prodej, nebytové prostory, garáže, pozemky nebo pouhou obecnou směrnici, nastav FALSE.\n"
            "2. 'min_rent_czk': Zadej celkové měsíční čisté nájemné v Kč. Pokud je v textu sazba za m2 (např. 110 Kč/m2), "
            "vynásob ji podlahovou plochou bytu.\n"
            "3. 'disposition': Uveď dispozici ve standardním tvaru (např. 2+1, 2+kk, 1+1, 3+1).\n"
            "4. 'deadline': Datum uzávěrky podání přihlášek či odevzdání zalepených obálek.\n\n"
            f"=== TEXT DOKUMENTU A WEBOVÉ STRÁNKY ===\n{text_content[:25000]}"
        )

        contents = []
        # Pokud máme k dispozici binární PDF data a text z pypdf byl příliš krátký (např. sken),
        # využijeme nativní multimodální schopnost Gemini pro přímé čtení PDF
        if pdf_bytes and len(text_content.strip()) < 100:
            contents.append(types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"))

        contents.append(prompt)

        response = client.models.generate_content(
            model=Config.GEMINI_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ApartmentOffer,
                temperature=0.1,
            ),
        )

        raw_json = response.text
        offer = ApartmentOffer.model_validate_json(raw_json)
        return offer

    except ImportError:
        logger.error(
            "Balíček 'google-genai' není nainstalován. Nainstalujte jej přes: pip install google-genai"
        )
        return None
    except Exception as e:
        logger.error(f"Chyba při volání Gemini API: {e}", exc_info=True)
        return None


# ==============================================================================
# 7. Filtrovací logika
# ==============================================================================
def matches_filter(offer: ApartmentOffer, config: Config) -> Tuple[bool, str]:
    """
    Vyhodnotí, zda nabídka splňuje uživatelské požadavky na dispozici, nájemné a plochu.
    """
    if not offer.is_apartment_offer:
        return False, "Dokument není nabídkou pronájmu bytu"

    # Kontrola dispozice
    clean_disp = offer.disposition.lower().replace(" ", "")
    if config.FILTER_ALLOWED_DISPOSITIONS:
        # Povoleno pokud dispozice obsahuje libovolnou povolenou zkratku
        matched_disp = any(d in clean_disp for d in config.FILTER_ALLOWED_DISPOSITIONS)
        if not matched_disp:
            return (
                False,
                f"Dispozice '{offer.disposition}' neodpovídá povoleným: {config.FILTER_ALLOWED_DISPOSITIONS}",
            )

    # Kontrola maximálního nájemného
    if config.FILTER_MAX_RENT > 0 and offer.min_rent_czk > 0:
        if offer.min_rent_czk > config.FILTER_MAX_RENT:
            return (
                False,
                f"Nájemné {offer.min_rent_czk:.0f} Kč překračuje limit {config.FILTER_MAX_RENT:.0f} Kč",
            )

    # Kontrola minimální plochy
    if config.FILTER_MIN_AREA_M2 > 0 and offer.floor_area_m2 > 0:
        if offer.floor_area_m2 < config.FILTER_MIN_AREA_M2:
            return (
                False,
                f"Plocha {offer.floor_area_m2} m2 je menší než požadovaných {config.FILTER_MIN_AREA_M2} m2",
            )

    return True, "Splňuje filtr"


# ==============================================================================
# 8. Telegram Notifikace
# ==============================================================================
def send_telegram_alert(
    candidate: RawCandidate,
    offer: ApartmentOffer,
    config: Config,
) -> bool:
    """Odešle formátovanou HTML zprávu do Telegram chatu přes Bot API."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.info("[Telegram] Notifikace neodeslána – chybí TELEGRAM_BOT_TOKEN nebo CHAT_ID.")
        return False

    rent_str = f"{offer.min_rent_czk:,.0f} Kč/měsíc".replace(",", " ") if offer.min_rent_czk > 0 else "Dle nabídky / neuvedeno"
    area_str = f"{offer.floor_area_m2:.1f} m²" if offer.floor_area_m2 > 0 else "neuvedena"
    pdf_link_str = f'<a href="{candidate.pdf_urls[0]}">Stáhnout PDF</a>' if candidate.pdf_urls else "Není přiloženo"

    html_message = (
        f"🏢 <b>Nový obecní byt k pronájmu – Ostrava</b>\n"
        f"🏛 <b>Obvod:</b> {candidate.district}\n\n"
        f"📍 <b>Adresa:</b> {offer.address or candidate.title}\n"
        f"📐 <b>Dispozice:</b> <b>{offer.disposition or 'neuvedena'}</b> ({area_str})\n"
        f"💰 <b>Minimální nájemné:</b> {rent_str}\n"
        f"⏳ <b>Uzávěrka přihlášek:</b> <b>{offer.deadline or 'viz dokument'}</b>\n"
        f"⚖️ <b>Typ řízení:</b> {offer.auction_or_fixed or 'výběrové řízení'}\n\n"
        f"📝 <b>Podrobnosti:</b> {offer.summary}\n\n"
        f"🔗 <a href=\"{candidate.url}\">Otevřít stránku nabídky</a> | 📄 {pdf_link_str}"
    )

    api_url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.TELEGRAM_CHAT_ID,
        "text": html_message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }

    try:
        resp = requests.post(api_url, json=payload, timeout=config.HTTP_TIMEOUT)
        if resp.status_code == 200:
            logger.info(f"[Telegram] Notifikace úspěšně odeslána pro byt: {offer.address}")
            return True
        else:
            logger.error(f"[Telegram] Chyba odeslání zprávy ({resp.status_code}): {resp.text}")
            return False
    except Exception as e:
        logger.error(f"[Telegram] Výjimka při odesílání: {e}")
        return False


# ==============================================================================
# 9. Hlavní řídicí proces
# ==============================================================================
def main():
    logger.info("=== Spouštím kontrolu nabídek městských bytů v Ostravě ===")
    Config.validate()

    db = Database(Config.DB_PATH)
    scraper = MunicipalityScraper(Config)

    # 1. Získání kandidátů z webových zdrojů
    candidates = scraper.get_candidates()
    logger.info(f"Nalezeno {len(candidates)} záznamů k ověření.")

    processed_count = 0
    passed_filter_count = 0
    notified_count = 0

    for item in candidates:
        if db.is_seen(item.id):
            logger.debug(f"Položka '{item.id}' již byla v minulosti zpracována. Přeskakuji.")
            continue

        processed_count += 1
        logger.info(f"\n--- Zpracovávám novou položku: {item.title} ({item.district}) ---")

        # 2. Extrakce textu z PDF příloh (pokud existují)
        all_pdf_texts = []
        primary_pdf_bytes = None
        for pdf_url in item.pdf_urls[:3]:  # Zpracujeme max. první 3 relevantní PDF
            logger.info(f"Stahuji a čtu PDF: {pdf_url}")
            pdf_text, pdf_bytes = download_and_extract_pdf_text(pdf_url, scraper.session)
            if pdf_text:
                all_pdf_texts.append(f"--- DOKUMENT: {pdf_url} ---\n{pdf_text}")
            if not primary_pdf_bytes and pdf_bytes:
                primary_pdf_bytes = pdf_bytes

        combined_text = f"{item.title}\n\n{item.web_text}\n\n" + "\n\n".join(all_pdf_texts)

        # 3. Analýza pomocí Gemini LLM
        offer = parse_with_gemini(combined_text, primary_pdf_bytes, Config)

        if not offer:
            logger.info("Nepodařilo se získat strukturovaná data z LLM. Označuji jako viděno.")
            db.mark_seen(
                item_id=item.id,
                district=item.district,
                title=item.title,
                url=item.url,
                pdf_url=item.pdf_urls[0] if item.pdf_urls else "",
                offer=None,
                passed_filter=False,
                notified=False,
            )
            continue

        logger.info(
            f"Výsledek LLM: Je byt={offer.is_apartment_offer} | Adresa='{offer.address}' | "
            f"Dispozice='{offer.disposition}' | Plocha={offer.floor_area_m2}m2 | "
            f"Nájem={offer.min_rent_czk:.0f} Kč | Termín='{offer.deadline}'"
        )

        # 4. Aplikace filtru
        passed, reason = matches_filter(offer, Config)
        logger.info(f"Filtr: {'PROŠLO' if passed else 'NEPROŠLO'} ({reason})")

        notified = False
        if passed:
            passed_filter_count += 1
            # 5. Odeslání Telegram notifikace
            notified = send_telegram_alert(item, offer, Config)
            if notified:
                notified_count += 1

        # 6. Uložení do databáze deduplikace
        db.mark_seen(
            item_id=item.id,
            district=item.district,
            title=item.title,
            url=item.url,
            pdf_url=item.pdf_urls[0] if item.pdf_urls else "",
            offer=offer,
            passed_filter=passed,
            notified=notified,
        )

    logger.info(
        f"\n=== Hotovo! Zpracováno nových: {processed_count}, "
        f"Prošlo filtrem: {passed_filter_count}, Odesláno notifikací: {notified_count} ==="
    )


if __name__ == "__main__":
    main()
