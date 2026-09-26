#!/usr/bin/env python3
"""
Městské Byty Ostrava - Automatický hlídač nových nabídek pronájmů městských bytů
Sleduje úřední desky a portály obvodů Moravská Ostrava a Přívoz (MOaP) a Ostrava-Poruba.
Stahuje přiložená PDF, pomocí Google Gemini LLM extrahuje strukturovaná data,
aplikuje filtry a posílá notifikace přes ntfy (iOS / Android / Web) s deduplikací v SQLite.
"""

import os
import sys
import io
import re
import json
import html
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

    NTFY_TOPIC: str = os.getenv("NTFY_TOPIC", "").strip()
    NTFY_SERVER_URL: str = os.getenv("NTFY_SERVER_URL", "https://ntfy.sh").strip().rstrip("/")
    NTFY_ACCESS_TOKEN: str = os.getenv("NTFY_ACCESS_TOKEN", "").strip()

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
        if not cls.NTFY_TOPIC:
            missing.append("NTFY_TOPIC")
        if missing:
            logger.warning(
                f"Upozornění: Chybí konfigurace v .env: {', '.join(missing)}. "
                "Skript poběží v testovacím režimu bez odesílání ntfy notifikací nebo volání Gemini."
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
                    summary TEXT,
                    passed_filter INTEGER,
                    notified INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            # Ověříme, zda existuje sloupec summary pro případnou migraci staré db
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(seen_offers)")
            columns = [col[1] for col in cursor.fetchall()]
            if "summary" not in columns:
                conn.execute("ALTER TABLE seen_offers ADD COLUMN summary TEXT DEFAULT ''")
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
                    deadline, auction_or_fixed, summary, passed_filter, notified
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    offer.summary if offer else "",
                    1 if passed_filter else 0,
                    1 if notified else 0,
                ),
            )
            conn.commit()

    def get_all_offers(self) -> List[dict]:
        with self._get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, district, title, url, pdf_url, address, disposition,
                       floor_area_m2, min_rent_czk, deadline, auction_or_fixed,
                       summary, passed_filter, notified, created_at
                FROM seen_offers
                WHERE (address != '' OR disposition != '' OR title != '')
                ORDER BY created_at DESC
                """
            )
            return [dict(row) for row in cursor.fetchall()]


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
        resp.encoding = "utf-8"
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
                detail_resp.encoding = "utf-8"

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


# ==============================================
# 8. ntfy Notifikace (pro iOS / Android / Web)
# ==============================================
def send_ntfy_alert(
    candidate: RawCandidate,
    offer: ApartmentOffer,
    config: Config,
) -> bool:
    """
    Odešle push notifikaci do aplikace ntfy (iOS / Android) přes HTTP POST JSON API.
    Obsahuje přímá akční tlačítka pro otevření webu i stažení PDF.
    """
    if not config.NTFY_TOPIC:
        logger.info("[ntfy] Notifikace neodeslána – chybí NTFY_TOPIC.")
        return False

    rent_str = (
        f"{offer.min_rent_czk:,.0f} Kč/měsíc".replace(",", " ")
        if offer.min_rent_czk > 0
        else "Dle nabídky / neuvedeno"
    )
    area_str = f"{offer.floor_area_m2:.1f} m²" if offer.floor_area_m2 > 0 else "neuvedena"

    title = f"🏢 Nový byt {offer.disposition or ''}: {offer.address or candidate.title}".strip()

    body_lines = [
        f"🏛 Obvod: {candidate.district}",
        f"📍 Adresa: {offer.address or candidate.title}",
        f"📐 Dispozice: {offer.disposition or 'neuvedena'} ({area_str})",
        f"💰 Nájemné: {rent_str}",
        f"⏳ Uzávěrka: {offer.deadline or 'viz nabídka'}",
        f"⚖️ Typ řízení: {offer.auction_or_fixed or 'výběrové řízení'}",
    ]
    if offer.summary:
        body_lines.append(f"\n📝 {offer.summary}")

    actions = [
        {"action": "view", "label": "📊 Webový přehled", "url": "https://djsnapy64-bot.github.io/Pronajem/"}
    ]
    if candidate.url:
        actions.append({"action": "view", "label": "🌐 Detail nabídky", "url": candidate.url})
    if candidate.pdf_urls:
        actions.append({"action": "view", "label": "📄 PDF záměr", "url": candidate.pdf_urls[0]})

    payload = {
        "topic": config.NTFY_TOPIC,
        "title": title,
        "message": "\n".join(body_lines),
        "tags": ["house", "building"],
        "priority": 4,  # vysoká priorita pro iOS upozornění
        "click": "https://djsnapy64-bot.github.io/Pronajem/",
        "actions": actions,
    }

    headers = {"Content-Type": "application/json; charset=utf-8"}
    if config.NTFY_ACCESS_TOKEN:
        headers["Authorization"] = f"Bearer {config.NTFY_ACCESS_TOKEN}"

    url = config.NTFY_SERVER_URL
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=config.HTTP_TIMEOUT)
        if resp.status_code == 200:
            logger.info(
                f"[ntfy] Notifikace úspěšně odeslána na téma '{config.NTFY_TOPIC}' pro byt: {offer.address}"
            )
            return True
        else:
            logger.error(f"[ntfy] Chyba odeslání zprávy ({resp.status_code}): {resp.text}")
            return False
    except Exception as e:
        logger.error(f"[ntfy] Výjimka při odesílání: {e}")
        return False


# ==============================================================================
# 9. Záložní HTML parser (při nedostupnosti LLM)
# ==============================================================================
def parse_fallback_from_html(candidate: RawCandidate) -> Optional[ApartmentOffer]:
    """Záložní extrakce údajů přímo z textu stránky pro případ nedostupnosti Gemini API klíče."""
    text = candidate.web_text
    title = candidate.title

    # 1. Dispozice (např. 2+1, 2+kk)
    disp_match = re.search(r'([0-9]\s*\+\s*(?:kk|[0-9]))', title, re.I)
    if not disp_match:
        disp_match = re.search(r'dispozice[^\w\d]*([0-9]\s*\+\s*(?:kk|[0-9]))', text, re.I)
    disp = disp_match.group(1).replace(" ", "") if disp_match else ""

    # 2. Adresa (oříznutí části "velikost 2+1...")
    if "velikost" in title.lower():
        address = re.split(r',\s*velikost', title, flags=re.I)[0].strip()
    elif "byt" in title.lower():
        address = title
    else:
        addr_match = re.search(r'Ulice a č\.p\./č\.o\.:\s*([^\n\r]+)', text, re.I)
        address = addr_match.group(1).strip() if addr_match else title

    # 3. Výměra (z titulku nebo z textu)
    area = 0.0
    area_match = re.search(r'-\s*([0-9]+(?:,[0-9]+)?)\s*m', title, re.I)
    if not area_match:
        area_match = re.search(r'Výměra[^\d]*([0-9]+(?:,[0-9]+)?)', text, re.I)
    if area_match:
        area = float(area_match.group(1).replace(",", "."))

    # 4. Minimální nájemné (sazba Kč/m2 * plocha m2)
    price_match = re.search(r'Minimální cena[^\:]*:\s*(\d+)', text, re.I)
    rate_m2 = float(price_match.group(1)) if price_match else 0.0
    rent = round(rate_m2 * area) if (rate_m2 > 0 and area > 0) else 0.0

    # 5. Termín uzávěrky
    deadline_match = re.search(r'Termín uzávěrky[^\d]*(\d+\.\s*\d+\.\s*\d+)', text, re.I)
    deadline = deadline_match.group(1).strip() if deadline_match else ""

    if disp or "byt" in title.lower() or address:
        return ApartmentOffer(
            is_apartment_offer=True,
            address=address or title,
            disposition=disp,
            floor_area_m2=area,
            min_rent_czk=rent,
            deadline=deadline,
            auction_or_fixed="výběrové řízení",
            summary=title,
        )
    return None


# ==============================================================================
# 10. Generátor webového přehledu (HTML Dashboard pro GitHub Pages)
# ==============================================================================
def generate_html_dashboard(db: Database, output_path: str = "index.html"):
    """
    Vygeneruje moderní, responzivní přehled nabídek do souboru index.html.
    Přehled lze přímo otevřít v prohlížeči nebo publikovat přes GitHub Pages.
    """
    offers = db.get_all_offers()
    now_str = datetime.datetime.now().strftime("%d. %m. %Y v %H:%M")
    total_count = len(offers)
    passed_count = sum(1 for o in offers if o.get("passed_filter") == 1)

    cards_html = []
    for o in offers:
        addr = o.get("address") or o.get("title") or "Neznámá adresa"
        disp = o.get("disposition") or ""
        district = o.get("district") or ""
        rent = float(o.get("min_rent_czk") or 0.0)
        area = float(o.get("floor_area_m2") or 0.0)
        deadline = o.get("deadline") or ""
        auction = o.get("auction_or_fixed") or "výběrové řízení"
        summary = o.get("summary") or ""
        url = o.get("url") or ""
        pdf_url = o.get("pdf_url") or ""
        passed = o.get("passed_filter") == 1
        created_at = o.get("created_at") or ""

        rent_formatted = f"{rent:,.0f} Kč / měs.".replace(",", " ") if rent > 0 else "Dle nabídky"
        area_formatted = f"{area:.1f} m²" if area > 0 else "neuvedeno"
        search_blob = f"{addr} {disp} {district} {summary} {auction}".lower()

        card = f"""
        <div class="card" 
             data-district="{html.escape(district)}" 
             data-disp="{html.escape(disp.lower().replace(' ', ''))}" 
             data-rent="{rent}" 
             data-area="{area}" 
             data-passed="{'1' if passed else '0'}" 
             data-search="{html.escape(search_blob)}">
            <div class="card-top">
                <div class="badge-group">
                    <span class="badge badge-district">{html.escape(district)}</span>
                    {f'<span class="badge badge-disp">{html.escape(disp)}</span>' if disp else ''}
                    {f'<span class="badge badge-match">⭐ Splňuje filtr</span>' if passed else '<span class="badge badge-neutral">Mimo filtr</span>'}
                </div>
                <div class="card-rent">{rent_formatted}</div>
            </div>
            <h3 class="card-title">{html.escape(addr)}</h3>
            <div class="specs-grid">
                <div class="spec-box"><span class="spec-label">📐 Výměra</span><span class="spec-val">{area_formatted}</span></div>
                <div class="spec-box"><span class="spec-label">⏳ Uzávěrka</span><span class="spec-val">{html.escape(deadline or 'viz web')}</span></div>
                <div class="spec-box"><span class="spec-label">⚖️ Řízení</span><span class="spec-val">{html.escape(auction)}</span></div>
            </div>
            {f'<p class="card-desc">{html.escape(summary)}</p>' if summary else ''}
            <div class="card-btns">
                {f'<a href="{html.escape(url)}" target="_blank" rel="noopener noreferrer" class="btn btn-primary">🌐 Přejít na nabídku</a>' if url else ''}
                {f'<a href="{html.escape(pdf_url)}" target="_blank" rel="noopener noreferrer" class="btn btn-secondary">📄 Stáhnout PDF</a>' if pdf_url else ''}
            </div>
            <div class="card-footer">Zjištěno: {html.escape(created_at[:16])}</div>
        </div>
        """
        cards_html.append(card)

    cards_block = "\n".join(cards_html) if cards_html else '<div class="empty-state">Zatím nebyly uloženy žádné byty. Spusťte kontrolu v GitHub Actions.</div>'

    html_content = f"""<!DOCTYPE html>
<html lang="cs">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>🏢 Městské Byty Ostrava – Přehled volných pronájmů</title>
  <style>
    :root {{
      --bg: #0f172a;
      --card-bg: #1e293b;
      --card-border: #334155;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --primary: #38bdf8;
      --primary-hover: #0284c7;
      --accent-green: #10b981;
      --accent-amber: #f59e0b;
      --accent-blue: #6366f1;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
    body {{ background: var(--bg); color: var(--text); padding: 1.5rem 1rem; min-height: 100vh; }}
    .container {{ max-width: 1200px; margin: 0 auto; }}
    header {{ margin-bottom: 2rem; text-align: center; }}
    h1 {{ font-size: 2rem; font-weight: 800; margin-bottom: 0.5rem; }}
    p.subtitle {{ color: var(--text-muted); font-size: 1rem; }}
    
    /* Stats Bar */
    .stats-bar {{ display: flex; flex-wrap: wrap; justify-content: center; gap: 1rem; margin: 1.5rem 0; }}
    .stat-pill {{ background: var(--card-bg); border: 1px solid var(--card-border); padding: 0.6rem 1.2rem; border-radius: 9999px; font-size: 0.9rem; }}
    .stat-pill strong {{ color: var(--primary); }}

    /* Filters Box */
    .filter-panel {{ background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 1rem; padding: 1.25rem; margin-bottom: 2rem; }}
    .search-input {{ width: 100%; padding: 0.75rem 1rem; border-radius: 0.5rem; border: 1px solid var(--card-border); background: #0f172a; color: #fff; font-size: 1rem; margin-bottom: 1rem; }}
    .search-input:focus {{ outline: 2px solid var(--primary); }}
    .filter-row {{ display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: center; justify-content: space-between; }}
    .filter-controls {{ display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: center; }}
    select {{ padding: 0.6rem 1rem; border-radius: 0.5rem; border: 1px solid var(--card-border); background: #0f172a; color: #fff; font-size: 0.9rem; cursor: pointer; }}
    .checkbox-label {{ display: flex; align-items: center; gap: 0.5rem; font-size: 0.9rem; cursor: pointer; color: var(--text); user-select: none; }}
    .checkbox-label input {{ width: 1.1rem; height: 1.1rem; accent-color: var(--primary); cursor: pointer; }}

    /* Cards Grid */
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 1.5rem; }}
    .card {{ background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 1rem; padding: 1.25rem; display: flex; flex-direction: column; transition: transform 0.15s ease, border-color 0.15s ease; }}
    .card:hover {{ transform: translateY(-3px); border-color: var(--primary); }}
    .card-top {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 0.5rem; margin-bottom: 0.75rem; }}
    .badge-group {{ display: flex; flex-wrap: wrap; gap: 0.4rem; }}
    .badge {{ font-size: 0.75rem; font-weight: 700; padding: 0.25rem 0.6rem; border-radius: 9999px; text-transform: uppercase; letter-spacing: 0.05em; }}
    .badge-district {{ background: rgba(99, 102, 241, 0.2); color: #818cf8; border: 1px solid rgba(99, 102, 241, 0.3); }}
    .badge-disp {{ background: rgba(56, 189, 248, 0.2); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.3); }}
    .badge-match {{ background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3); }}
    .badge-neutral {{ background: rgba(148, 163, 184, 0.15); color: #94a3b8; border: 1px solid rgba(148, 163, 184, 0.2); }}
    .card-rent {{ font-size: 1.2rem; font-weight: 800; color: #38bdf8; text-align: right; white-space: nowrap; }}
    .card-title {{ font-size: 1.2rem; font-weight: 700; line-height: 1.3; margin-bottom: 1rem; color: #fff; }}
    .specs-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0.5rem; background: #0f172a; padding: 0.75rem; border-radius: 0.5rem; margin-bottom: 1rem; font-size: 0.85rem; }}
    .spec-box {{ display: flex; flex-direction: column; }}
    .spec-label {{ color: var(--text-muted); font-size: 0.75rem; }}
    .spec-val {{ font-weight: 600; color: #fff; margin-top: 0.1rem; }}
    .card-desc {{ font-size: 0.88rem; color: var(--text-muted); margin-bottom: 1.25rem; flex-grow: 1; line-height: 1.4; }}
    .card-btns {{ display: flex; gap: 0.5rem; margin-top: auto; }}
    .btn {{ flex: 1; text-align: center; text-decoration: none; padding: 0.65rem 0.5rem; font-size: 0.85rem; font-weight: 600; border-radius: 0.5rem; transition: background 0.15s ease; }}
    .btn-primary {{ background: var(--primary); color: #0f172a; }}
    .btn-primary:hover {{ background: var(--primary-hover); }}
    .btn-secondary {{ background: #334155; color: #fff; }}
    .btn-secondary:hover {{ background: #475569; }}
    .card-footer {{ font-size: 0.75rem; color: #64748b; margin-top: 0.75rem; text-align: right; }}
    .empty-state {{ grid-column: 1 / -1; text-align: center; padding: 3rem; color: var(--text-muted); font-size: 1.1rem; background: var(--card-bg); border-radius: 1rem; }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>🏢 Městské Byty Ostrava</h1>
      <p class="subtitle">Automatický monitoring nabídek pronájmů (Moravská Ostrava a Přívoz, Ostrava-Poruba)</p>
      
      <div class="stats-bar">
        <div class="stat-pill">Celkem nabídek: <strong>{total_count}</strong></div>
        <div class="stat-pill">Vyhovuje filtru: <strong style="color: var(--accent-green);">{passed_count}</strong></div>
        <div class="stat-pill">Aktualizováno: <strong>{now_str}</strong></div>
      </div>
    </header>

    <div class="filter-panel">
      <input type="text" id="search-input" class="search-input" placeholder="🔍 Rychlé hledání podle adresy, ulice, dispozice (např. Fügnerova, 2+1)...">
      
      <div class="filter-row">
        <div class="filter-controls">
          <select id="district-filter">
            <option value="">Všechny obvody</option>
            <option value="Moravská Ostrava a Přívoz">Moravská Ostrava a Přívoz</option>
            <option value="Ostrava-Poruba">Ostrava-Poruba</option>
          </select>

          <select id="disp-filter">
            <option value="">Všechny dispozice</option>
            <option value="1+kk,1+1">1+kk / 1+1</option>
            <option value="2+kk,2+1">2+kk / 2+1</option>
            <option value="3+kk,3+1">3+kk / 3+1</option>
            <option value="4+kk,4+1">4+kk / 4+1</option>
          </select>

          <select id="sort-select">
            <option value="newest">Řadit: Nejnovější</option>
            <option value="rent-asc">Řadit: Nejlevnější nájem</option>
            <option value="rent-desc">Řadit: Nejdražší nájem</option>
            <option value="area-desc">Řadit: Největší plocha</option>
          </select>
        </div>

        <label class="checkbox-label">
          <input type="checkbox" id="only-passed">
          <span>Pouze vyhovující mému filtru ⭐</span>
        </label>
      </div>
    </div>

    <div id="cards-container" class="grid">
      {cards_block}
    </div>
  </div>

  <script>
    const searchInput = document.getElementById('search-input');
    const districtFilter = document.getElementById('district-filter');
    const dispFilter = document.getElementById('disp-filter');
    const sortSelect = document.getElementById('sort-select');
    const onlyPassedCheck = document.getElementById('only-passed');
    const container = document.getElementById('cards-container');
    const allCards = Array.from(document.querySelectorAll('.card'));

    function applyFilters() {{
      const query = searchInput.value.trim().toLowerCase();
      const district = districtFilter.value;
      const allowedDisps = dispFilter.value ? dispFilter.value.split(',') : [];
      const onlyPassed = onlyPassedCheck.checked;

      let visibleCards = allCards.filter(card => {{
        const cardSearch = card.dataset.search || '';
        const cardDistrict = card.dataset.district || '';
        const cardDisp = card.dataset.disp || '';
        const cardPassed = card.dataset.passed === '1';

        if (query && !cardSearch.includes(query)) return false;
        if (district && !cardDistrict.includes(district)) return false;
        if (allowedDisps.length > 0 && !allowedDisps.some(d => cardDisp.includes(d))) return false;
        if (onlyPassed && !cardPassed) return false;
        return true;
      }});

      // Řazení
      const sortMode = sortSelect.value;
      visibleCards.sort((a, b) => {{
        if (sortMode === 'rent-asc') return (parseFloat(a.dataset.rent) || 999999) - (parseFloat(b.dataset.rent) || 999999);
        if (sortMode === 'rent-desc') return (parseFloat(b.dataset.rent) || 0) - (parseFloat(a.dataset.rent) || 0);
        if (sortMode === 'area-desc') return (parseFloat(b.dataset.area) || 0) - (parseFloat(a.dataset.area) || 0);
        return 0; // standardně zachovat pořadí
      }});

      allCards.forEach(card => card.style.display = 'none');
      visibleCards.forEach(card => {{
        card.style.display = 'flex';
        container.appendChild(card);
      }});
    }}

    searchInput.addEventListener('input', applyFilters);
    districtFilter.addEventListener('change', applyFilters);
    dispFilter.addEventListener('change', applyFilters);
    sortSelect.addEventListener('change', applyFilters);
    onlyPassedCheck.addEventListener('change', applyFilters);
  </script>
</body>
</html>
"""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    logger.info(f"Webový přehled byl úspěšně vygenerován do: {output_path}")


# ==============================================================================
# 11. Hlavní řídicí proces
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

        # 3. Analýza pomocí Gemini LLM (nebo záložní HTML parser)
        offer = parse_with_gemini(combined_text, primary_pdf_bytes, Config)
        if not offer:
            offer = parse_fallback_from_html(item)

        if not offer:
            logger.info("Nepodařilo se získat strukturovaná data z LLM ani z HTML. Označuji jako viděno.")
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
            f"Výsledek: Je byt={offer.is_apartment_offer} | Adresa='{offer.address}' | "
            f"Dispozice='{offer.disposition}' | Plocha={offer.floor_area_m2}m2 | "
            f"Nájem={offer.min_rent_czk:.0f} Kč | Termín='{offer.deadline}'"
        )

        # 4. Aplikace filtru
        passed, reason = matches_filter(offer, Config)
        logger.info(f"Filtr: {'PROŠLO' if passed else 'NEPROŠLO'} ({reason})")

        notified = False
        if passed:
            passed_filter_count += 1
            # 5. Odeslání ntfy notifikace
            notified = send_ntfy_alert(item, offer, Config)
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

    # 7. Vytvoření/aktualizace webového přehledu
    generate_html_dashboard(db, "index.html")

    logger.info(
        f"\n=== Hotovo! Zpracováno nových: {processed_count}, "
        f"Prošlo filtrem: {passed_filter_count}, Odesláno notifikací: {notified_count} ==="
    )


if __name__ == "__main__":
    main()
