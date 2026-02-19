# FLUXGEN_UNIVERSAL.py
# Polomania + Lolmarkt (Shoprenter CDN) + általános webshop kompatibilis verzió
# FONTOS: API kulcsot NE hardcode-olj. Használd env-ben: OPENAI_API_KEY, BFL_API_KEY

import os
import re
import json
import time
import hashlib
import base64
from datetime import datetime
from urllib.parse import urlparse, urljoin, urlunparse

import httpx
from bs4 import BeautifulSoup
from openai import OpenAI
from playwright.sync_api import sync_playwright

from PySide6.QtCore import QThread, Signal, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QSpinBox, QTextEdit, QFileDialog,
    QMessageBox, QCheckBox, QListWidget, QListWidgetItem, QSplitter,
    QComboBox, QGroupBox, QFormLayout, QTabWidget, QDialog, QDialogButtonBox
)

# =============================
# 🔑 OPENAI API KULCS
# =============================
# Használd: OPENAI_API_KEY környezeti változóban
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# =============================
# 🔑 BFL (Black Forest Labs / FLUX) API KULCS
# =============================
BFL_API_KEY = os.getenv("BFL_API_KEY", "").strip()

# BFL base URL (választható: global / EU / US)
BFL_BASE_URL_DEFAULT = "https://api.bfl.ai/v1"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123 Safari/537.36"
)

# ---------- helpers ----------
def slugify(text: str, max_len: int = 80) -> str:
    text = (text or '').strip().lower()
    text = re.sub(r'[\r\n\t]+', ' ', text)
    text = re.sub(r'[^\w\s-]', '', text, flags=re.UNICODE)
    text = re.sub(r'[\s_-]+', '-', text).strip('-')
    if not text:
        return 'design'
    return text[:max_len].strip('-')

def make_client(api_key: str) -> OpenAI:
    api_key = (api_key or "").strip()
    if not api_key:
        raise RuntimeError("Hiányzik az OPENAI_API_KEY. Add meg az appban vagy env-ben.")
    return OpenAI(api_key=api_key)

def dedupe(items):
    seen = set()
    out = []
    for x in items:
        if x not in seen:
            out.append(x)
            seen.add(x)
    return out

def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

def ensure_dirs(out_dir, cache_dir):
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)

def ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)

def safe_name_from_image_url(u: str) -> str:
    """
    Stabil azonosító a kép URL alapján.
    Pl: .../Tacsko-januar-62454.jpg.webp -> tacsko-januar-62454
    """
    path = urlparse(u).path
    base = os.path.basename(path)
    base = re.sub(r"\.(jpg|jpeg|png|webp)$", "", base, flags=re.I)
    base = re.sub(r"\.(jpg|jpeg|png)$", "", base, flags=re.I)  # ha .jpg.webp volt és már leszedtük a webp-t
    base = base.strip().lower()
    base = re.sub(r"[^\w\s-]", "", base)
    base = re.sub(r"[\s_-]+", "-", base).strip("-")
    return base or sha1(u)

def safe_filename_from_image_url(u: str) -> str:
    return safe_name_from_image_url(u) + ".json"

def strip_query_fragment(u: str) -> str:
    """Stabilizálás: query + fragment le, hogy cache ne csússzon el lastmod miatt."""
    p = urlparse(u)
    return urlunparse((p.scheme, p.netloc, p.path, "", "", ""))

def parse_srcset(srcset: str, base_url: str) -> list[str]:
    """
    srcset -> abs URL lista. (a legnagyobbat majd kiválaszthatod később)
    """
    out = []
    if not srcset:
        return out
    parts = [p.strip() for p in srcset.split(",") if p.strip()]
    for part in parts:
        url_part = part.split()[0].strip()
        if not url_part:
            continue
        out.append(urljoin(base_url, url_part))
    return out

def get_data_url(source: str) -> str:
    """
    Kép betöltése data URL-ként (http URL vagy helyi fájl útvonal).
    """
    if source.lower().startswith("http"):
        return download_image_as_data_url(source)
    
    # Local file
    if not os.path.exists(source):
        raise RuntimeError(f"Fájl nem található: {source}")
    
    mime_type = "image/jpeg"
    ext = os.path.splitext(source)[1].lower()
    if ext == ".png": mime_type = "image/png"
    elif ext == ".webp": mime_type = "image/webp"
    
    with open(source, "rb") as f:
        data = f.read()
        b64 = base64.b64encode(data).decode("utf-8")
        return f"data:{mime_type};base64,{b64}"

def download_image_as_data_url(url: str) -> str:
    with httpx.Client(
        follow_redirects=True,
        timeout=45,
        headers={"User-Agent": USER_AGENT},
    ) as http:
        r = http.get(url)
        r.raise_for_status()
        ctype = r.headers.get("content-type", "image/jpeg").split(";")[0]
        b64 = base64.b64encode(r.content).decode("utf-8")
        return f"data:{ctype};base64,{b64}"

def infer_allowed_hosts(seed_url: str, extra_hosts_csv: str = "") -> list[str]:
    """
    Engedélyezett hostok:
    - seed host
    - www.seedhost (ha nincs)
    - seedhost (ha www.)
    - extra_hosts_csv (vesszővel elválasztva)
    - plusz ismert Shoprenter CDN minták (ha található)
    """
    seed_host = urlparse(seed_url).netloc.lower()
    allowed = {seed_host}

    if seed_host.startswith("www."):
        allowed.add(seed_host.replace("www.", "", 1))
    else:
        allowed.add("www." + seed_host)

    # extra hostok (pl. lolmarkt.cdn.shoprenter.hu)
    for h in (extra_hosts_csv or "").split(","):
        h = h.strip().lower()
        if h:
            allowed.add(h)

    # Heurisztika: Shoprenter CDN
    # pl: <brand>.cdn.shoprenter.hu
    # ha seed "lolmarkt.hu" -> lolmarkt.cdn.shoprenter.hu
    # ha seed "www.lolmarkt.hu" -> lolmarkt.cdn.shoprenter.hu
    base = seed_host
    if base.startswith("www."):
        base = base.replace("www.", "", 1)
    brand = base.split(":")[0].split(".")[0]
    if brand:
        allowed.add(f"{brand}.cdn.shoprenter.hu")

    return sorted(allowed)

def is_likely_product_image(img) -> bool:
    """
    Általános heurisztika: product card / gallery / item image.
    """
    cls = " ".join(img.get("class", [])).lower()
    alt = (img.get("alt") or "").lower()

    good_tokens = [
        "product", "card", "image", "gallery", "thumb", "thumbnail",
        "grid", "item", "listing"
    ]
    # Shoprenter példa: product-card__image product-card__image-secondary
    if any(t in cls for t in good_tokens):
        return True
    if "product" in alt or "póló" in alt or "pulóver" in alt or "kapucnis" in alt:
        return True
    return False

def pick_img_urls_from_tag(img, base_url: str) -> list[str]:
    """
    Egy img tagból a lehetséges URL-ek:
    - data-src, src
    - srcset
    """
    urls = []
    src = img.get("data-src") or img.get("data-lazy") or img.get("data-original") or img.get("src")
    if src:
        urls.append(urljoin(base_url, src))

    srcset = img.get("srcset") or img.get("data-srcset")
    if srcset:
        urls.extend(parse_srcset(srcset, base_url))

    # néha <source srcset> van picture-ben, de itt img-ből indulunk: fallback nélkül is elég jó
    return [u for u in urls if u.startswith("http")]

# ---------- playwright collect ----------
def collect_card_images(
    seed_url: str,
    headless: bool = True,
    scroll_rounds: int = 12,
    log_cb=None,
    path_must_contain: str | None = None,
    extra_allowed_hosts: str = "",
    require_productish: bool = True,
):
    """
    Kártya képek gyűjtése (általánosított):
    - engedélyezett hostok listája (seed + www variáns + Shoprenter CDN + extra)
    - opcionális path szűrő (Polomania: /images/designs/)
    - opcionális "productish" heurisztika class/alt alapján (Shoprenter/Lolmarkt miatt hasznos)

    Visszatér: deduplikált abs URL lista (query/fragment nélkül stabilizálva)
    """
    seed_url = seed_url.split("#", 1)[0]
    allowed_hosts = set(infer_allowed_hosts(seed_url, extra_allowed_hosts))

    if log_cb:
        log_cb("Allowed hosts: " + ", ".join(sorted(allowed_hosts)))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(user_agent=USER_AGENT)

        page.set_default_navigation_timeout(120000)
        page.set_default_timeout(120000)

        page.goto(seed_url, wait_until="domcontentloaded", timeout=120000)
        page.wait_for_timeout(1200)

        last_h = 0
        for i in range(scroll_rounds):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(900)
            h = page.evaluate("document.body.scrollHeight")
            if log_cb:
                log_cb(f"Scroll {i+1}/{scroll_rounds} (height={h})")
            if h == last_h:
                break
            last_h = h

        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, "html.parser")

    urls = []
    for img in soup.select("img"):
        if require_productish and (not is_likely_product_image(img)):
            continue

        candidates = pick_img_urls_from_tag(img, seed_url)
        for absu in candidates:
            pu = urlparse(absu)
            if pu.scheme not in ("http", "https"):
                continue

            host = pu.netloc.lower()
            if host not in allowed_hosts:
                continue

            if path_must_contain and path_must_contain.lower() not in pu.path.lower():
                continue

            urls.append(strip_query_fragment(absu))

    return dedupe(urls)

def gpt_single_prompt_from_image(client: OpenAI, image_url: str, force_text=True, bg_type="transparent", use_contour=False) -> dict:
    img_data_url = get_data_url(image_url)

    if force_text:
        # Background prompt logic
        bg_instruction = "transparent background"
        if bg_type == "white":
            bg_instruction = "white background"
        elif bg_type == "black":
            bg_instruction = "black background"
        elif bg_type == "neon_green":
            bg_instruction = "bright neon green solid background (chroma key style)"
        elif bg_type == "hot_pink":
            bg_instruction = "vibrant hot pink solid background"
        elif bg_type == "none":
            bg_instruction = "contextual background (fitting the theme)"

        contour_instruction = ""
        if use_contour:
            contour_instruction = "MUST include: clean continuous black contour around the artwork."

        system = (
            "Feladat: egy póló/pulcsi/mockup fotón látható NYOMAT alapján készíts EGY darab végleges képgeneráló promptot.\n"
            "\n"
            "KRITIKUS SZABÁLYOK:\n"
            "1) A pólót/pulcsit/modellt/mockupot IGNORÁLD. Ne írd le a ruhát.\n"
            "2) CSAK a nyomat grafikáját írd le: illusztráció + tipográfia + kompozíció.\n"
            f"3) HÁTTÉR: {bg_instruction}. {contour_instruction}\n"
            "4) A grafikán lévő SZÖVEGET próbáld kiolvasni pontosan. Ha magyar, írd: 'TEXT Hungarian: \"...\"'. Ha angol: 'TEXT English: \"...\"'.\n"
            "   FONTOS: NE használj '\\n' vagy sortörést a szövegben. Ehelyett írd le az elhelyezkedést: 'TEXT...' (above) 'TEXT...'.\n"
            "5) Ha a szöveg nem 100% biztos: akkor is adj BEST-GUESS szöveget a promptban, DE\n"
            "   TILOS bármilyen bizonytalanság jelölést beleírni a promptba (pl. UNCERTAIN, ?, brackets).\n"
            "   A bizonytalanságot kizárólag a debug mezőbe írd le.\n"
            "6) Kimenet STRICT JSON a kért mezőkkel."
        )

        user = (
            "Kimenet (STRICT JSON):\n"
            "{\n"
            f'  "prompt": "ONE strong English image generation prompt describing ONLY the PRINT ARTWORK. MUST include: {bg_instruction}, {contour_instruction} centered composition, print-ready clean design, and TEXT Hungarian: \\"CSAK EGY KAPÁS\\" (above) \\"VÁLASZT EL...\\" (best-guess exact artwork text, NO newlines/\\n). Also describe typography, layout and graphics. Add explicitly: isolated artwork only; no garment, no mockup, no fabric, no shadows.",\n'
            '  "negative_prompt": "short negatives to avoid mockup/garment/photo",\n'
            '  "style_keywords": ["..."],\n'
            '  "colors": ["..."],\n'
            '  "debug": {\n'
            '     "extracted_text_raw": "what you think the text is (can include uncertainty notes here)",\n'
            '     "confidence": "high|medium|low",\n'
            '     "notes": "why uncertain / which chars ambiguous"\n'
            "  }\n"
            "}"
        )
    else:
        system = (
            "Feladat: mockup fotón látható nyomatból készíts EGY darab végleges képgeneráló promptot.\n"
            "IGNORÁLD a ruhát/hátteret – csak a grafikát írd le.\n"
            "A kimenet print-ready, transzparens hátterű, középre rendezett grafika.\n"
            "STRICT JSON."
        )
        user = (
            "Kimenet (STRICT JSON):\n"
            "{\n"
            '  "prompt": "ONE strong English prompt describing ONLY the artwork (isolated). Include: transparent background, centered, print-ready clean design, no garment/mockup/photo.",\n'
            '  "negative_prompt": "short negatives to avoid mockup/garment/photo",\n'
            '  "style_keywords": ["..."],\n'
            '  "colors": ["..."],\n'
            '  "debug": {"confidence":"high","notes":""}\n'
            "}"
        )

    resp = client.responses.create(
        model="gpt-5-mini",
        input=[
            {"role": "system", "content": [{"type": "input_text", "text": system}]},
            {"role": "user", "content": [
                {"type": "input_text", "text": user},
                {"type": "input_image", "image_url": img_data_url},
            ]},
        ],
    )

    text = ""
    for item in resp.output:
        if item.type == "message":
            for c in item.content:
                if c.type == "output_text":
                    text += c.text

    data = json.loads(text.strip())

    # agresszív negatívok (mockup ellen)
    neg = (data.get("negative_prompt") or "").strip()
    if neg:
        neg += ", "
    neg += "t-shirt mockup, hoodie mockup, garment, fabric texture, wrinkles, folds, shadows, photo, model, background scene, studio lighting"

    # Visszaadjuk a debug-ot is (JSON-ba menthető), de te csak a promptot használod generálásra.
    return {
        "prompt": data.get("prompt", ""),
        "negative_prompt": neg,
        "style_keywords": data.get("style_keywords", []),
        "colors": data.get("colors", []),
        "debug": data.get("debug", {}),
    }
    # ---------- GPT Image 1.5: generate image from prompt ----------
def generate_image_from_prompt(
    client: OpenAI,
    prompt: str,
    out_dir: str,
    quality: str = "medium",
    size: str = "1024x1024",
    transparent: bool = False,
    n: int = 1,
    base_name: str | None = None,
) -> list[str]:
    """
    Returns list of saved file paths (PNG).
    Uses Images API with gpt-image-1.5.
    """
    if not prompt.strip():
        raise RuntimeError("Üres prompt.")

    ensure_dir(out_dir)

    background = "transparent" if transparent else "auto"

    resp = client.images.generate(
        model="gpt-image-1.5",
        prompt=prompt,
        quality=quality,
        size=size,
        n=n,
        background=background,
    )

    saved = []
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    base = slugify(base_name) if base_name else f"gen_{ts}"
    for i, item in enumerate(resp.data, start=1):
        b64 = getattr(item, "b64_json", None)
        if not b64:
            raise RuntimeError("Nem kaptam b64_json-t a válaszban (ellenőrizd az SDK verziót / API választ).")
        img_bytes = base64.b64decode(b64)
        fn = os.path.join(out_dir, f"{base}_{quality}_{size}_{i}.png")
        with open(fn, "wb") as f:
            f.write(img_bytes)
        saved.append(fn)
    return saved

# ---------- BFL / FLUX: generate image from prompt (async submit + poll + download) ----------
def bfl_headers(api_key: str) -> dict:
    api_key = (api_key or "").strip()
    if not api_key:
        raise RuntimeError("Hiányzik a BFL_API_KEY (írd be az appban vagy add meg env-ben).")
    return {"accept": "application/json", "x-key": api_key, "Content-Type": "application/json"}

def size_to_dims_and_ar(size: str) -> tuple[int, int, str]:
    if not size or "x" not in size:
        return 1024, 1024, "1:1"
    
    parts = size.lower().split("x")
    try:
        w = int(parts[0].strip())
        h = int(parts[1].strip())
    except ValueError:
        return 1024, 1024, "1:1"

    # Minimal logic for aspect ratio string (approx)
    ratio = w / h
    ar_str = "1:1"
    if 0.6 < ratio < 0.7: ar_str = "2:3"
    elif 1.4 < ratio < 1.6: ar_str = "3:2"
    elif 0.5 < ratio < 0.6: ar_str = "9:16"
    elif 1.7 < ratio < 1.8: ar_str = "16:9"
    elif 0.7 < ratio < 0.8: ar_str = "3:4"
    elif 1.2 < ratio < 1.4: ar_str = "4:3"
    
    return w, h, ar_str

def bfl_submit_generation(
    prompt: str,
    base_url: str,
    endpoint: str,
    api_key: str,
    size: str = "1024x1024",
    output_format: str = "png",
    seed: int | None = None,
    prompt_upsampling: bool = False,
    safety_tolerance: int = 2,
) -> dict:
    if not prompt.strip():
        raise RuntimeError("Üres prompt.")

    base_url = (base_url or "").rstrip("/")
    endpoint = (endpoint or "").strip()
    if not endpoint.startswith("/"):
        endpoint = "/" + endpoint

    w, h, _ar = size_to_dims_and_ar(size)

    payload = {
        "prompt": prompt,
        "width": w,
        "height": h,
        "output_format": output_format,
        "prompt_upsampling": bool(prompt_upsampling),
        "safety_tolerance": int(safety_tolerance),
    }
    if seed is not None:
        payload["seed"] = int(seed)

    url = f"{base_url}{endpoint}"
    with httpx.Client(timeout=60, follow_redirects=True) as http:
        r = http.post(url, headers=bfl_headers(api_key), json=payload)
        if r.status_code >= 400:
            raise RuntimeError(f"BFL submit hiba HTTP {r.status_code}: {r.text}")
        return r.json()

def bfl_poll_until_ready(polling_url: str, api_key: str, poll_interval_sec: float = 0.5, timeout_sec: int = 180) -> dict:
    t0 = time.time()
    with httpx.Client(timeout=60, follow_redirects=True) as http:
        while True:
            if time.time() - t0 > timeout_sec:
                raise RuntimeError(f"BFL timeout: {timeout_sec}s (polling_url: {polling_url})")
            r = http.get(polling_url, headers=bfl_headers(api_key))
            if r.status_code >= 400:
                raise RuntimeError(f"BFL poll hiba HTTP {r.status_code}: {r.text}")
            data = r.json()
            status = (data.get("status") or "").strip().lower()
            if status == "ready":
                return data
            if status in ("error", "failed"):
                raise RuntimeError(f"BFL generálás sikertelen: {json.dumps(data, ensure_ascii=False)}")
            time.sleep(poll_interval_sec)

def bfl_download_sample(sample_url: str, out_path: str):
    ensure_dir(os.path.dirname(out_path))
    with httpx.Client(timeout=90, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as http:
        r = http.get(sample_url)
        r.raise_for_status()
        with open(out_path, "wb") as f:
            f.write(r.content)

def generate_image_from_prompt_bfl(
    prompt: str,
    out_dir: str,
    api_key: str,
    base_url: str = "https://api.bfl.ai/v1",
    endpoint: str = "/flux-pro-1.1",
    size: str = "1024x1024",
    output_format: str = "png",
    n: int = 1,
    base_name: str | None = None,
    prompt_upsampling: bool = False,
    safety_tolerance: int = 2,
    timeout_sec: int = 180,
) -> list[str]:
    if not prompt.strip():
        raise RuntimeError("Üres prompt.")

    ensure_dir(out_dir)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = slugify(base_name) if base_name else f"bfl_{ts}"

    saved = []
    ext = "png" if output_format.lower() == "png" else "jpg"

    for i in range(1, int(n) + 1):
        submit = bfl_submit_generation(
            prompt=prompt,
            base_url=base_url,
            endpoint=endpoint,
            api_key=api_key,
            size=size,
            output_format=output_format,
            seed=None,
            prompt_upsampling=prompt_upsampling,
            safety_tolerance=safety_tolerance,
        )
        polling_url = submit.get("polling_url")
        if not polling_url:
            raise RuntimeError(f"BFL válaszban nincs polling_url: {json.dumps(submit, ensure_ascii=False)}")

        final = bfl_poll_until_ready(polling_url=polling_url, api_key=api_key, timeout_sec=timeout_sec)
        sample_url = ((final.get("result") or {}).get("sample")) if isinstance(final, dict) else None
        if not sample_url:
            raise RuntimeError(f"BFL kész válaszban nincs result.sample: {json.dumps(final, ensure_ascii=False)}")

        fn = os.path.join(out_dir, f"{base}_bfl{endpoint.replace('/', '-')}_{size}_{i}.{ext}")
        bfl_download_sample(sample_url, fn)
        saved.append(fn)
    return saved

# ---------- Worker: crawl + prompt ----------
class ImageCrawlWorker(QThread):
    log = Signal(str)
    done = Signal()
    error = Signal(str)
    result_ready = Signal(str, dict, str)  # name, data, image_url

    def __init__(
        self,
        api_key,
        seed_url,
        out_dir,
        cache_dir,
        max_items,
        delay_ms,
        headless,
        scroll_rounds,
        path_filter,
        force_text,
        extra_hosts: str,
        require_productish: bool,
        bg_type: str = "transparent",
        use_contour: bool = False,
        local_files: list[str] | None = None,
    ):
        super().__init__()
        self.api_key = api_key
        self.seed_url = seed_url
        self.out_dir = out_dir
        self.cache_dir = cache_dir
        self.max_items = max_items
        self.delay_ms = delay_ms
        self.headless = headless
        self.scroll_rounds = scroll_rounds
        self.path_filter = path_filter
        self.force_text = force_text
        self.extra_hosts = extra_hosts
        self.require_productish = require_productish
        self.bg_type = bg_type
        self.use_contour = use_contour
        self.local_files = local_files
        self._stop = False

    def stop(self):
        self._stop = True

    def cache_path(self, image_url: str) -> str:
        return os.path.join(self.cache_dir, sha1(image_url) + ".json")

    def load_cache(self, image_url: str):
        p = self.cache_path(image_url)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        return None

    def save_cache(self, image_url: str, data: dict):
        p = self.cache_path(image_url)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def run(self):
        try:
            client = make_client(self.api_key)
            ensure_dirs(self.out_dir, self.cache_dir)

            ensure_dirs(self.out_dir, self.cache_dir)

            imgs = []
            if self.local_files:
                self.log.emit(f"Helyi fájlok feldolgozása: {len(self.local_files)} db")
                imgs = self.local_files
            else:
                self.log.emit(f"Seed: {self.seed_url}")
                imgs = collect_card_images(
                    self.seed_url,
                    headless=self.headless,
                    scroll_rounds=self.scroll_rounds,
                    log_cb=lambda s: self.log.emit(s),
                    path_must_contain=self.path_filter.strip() if self.path_filter.strip() else None,
                    extra_allowed_hosts=self.extra_hosts.strip(),
                    require_productish=self.require_productish,
                )

            if not imgs:
                self.log.emit("0 kép találat. Tipp: növeld a scroll köröket vagy állítsd a host/path szűrőket.")
                self.done.emit()
                return

            imgs = imgs[: self.max_items]
            self.log.emit(f"Feldolgozandó képek: {len(imgs)}")

            for i, img_url in enumerate(imgs, 1):
                if self._stop:
                    self.log.emit("STOP – leállítva.")
                    break

                if self.local_files:
                    # Helyi fájl esetén a név a fájlnévből jön
                    name = os.path.splitext(os.path.basename(img_url))[0]
                    name = slugify(name)
                else:
                    name = safe_name_from_image_url(img_url)
                
                out_file = os.path.join(self.out_dir, name + ".json")

                if os.path.exists(out_file):
                    self.log.emit(f"[{i}/{len(imgs)}] SKIP (kész): {img_url}")
                    try:
                        with open(out_file, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        self.result_ready.emit(name, data, img_url)
                    except:
                        pass
                    continue

                cached = self.load_cache(img_url)
                if cached:
                    with open(out_file, "w", encoding="utf-8") as f:
                        json.dump(cached, f, ensure_ascii=False, indent=2)
                    self.log.emit(f"[{i}/{len(imgs)}] CACHE → mentve")
                    self.result_ready.emit(name, cached, img_url)
                    continue

                self.log.emit(f"[{i}/{len(imgs)}] GPT: {img_url}")
                data = gpt_single_prompt_from_image(
                    client, 
                    img_url, 
                    force_text=self.force_text, 
                    bg_type=self.bg_type,
                    use_contour=self.use_contour
                )

                self.save_cache(img_url, data)
                with open(out_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)

                self.result_ready.emit(name, data, img_url)
                self.log.emit("  OK")
                time.sleep(self.delay_ms / 1000.0)

            self.done.emit()

        except Exception as e:
            self.error.emit(str(e))

# ---------- Worker: image generate ----------
class ImageGenWorker(QThread):
    log = Signal(str)
    done = Signal(list)
    error = Signal(str)

    def __init__(self, api_key, prompt, out_dir, quality, size, transparent, n, base_name):
        super().__init__()
        self.api_key = api_key
        self.prompt = prompt
        self.out_dir = out_dir
        self.quality = quality
        self.size = size
        self.transparent = transparent
        self.n = n
        self.base_name = base_name

    def run(self):
        try:
            client = make_client(self.api_key)
            self.log.emit(
                f"Image gen: model=gpt-image-1.5 quality={self.quality} size={self.size} transparent={self.transparent} n={self.n}"
            )
            paths = generate_image_from_prompt(
                client=client,
                prompt=self.prompt,
                out_dir=self.out_dir,
                quality=self.quality,
                size=self.size,
                transparent=self.transparent,
                n=self.n,
                base_name=self.base_name
            )
            self.done.emit(paths)
        except Exception as e:
            self.error.emit(str(e))

class BFLImageGenWorker(QThread):
    log = Signal(str)
    done = Signal(list)
    error = Signal(str)

    def __init__(
        self, bfl_api_key, prompt, out_dir, base_url, endpoint, size, output_format, n, base_name,
        prompt_upsampling=False, safety_tolerance=2, timeout_sec=180
    ):
        super().__init__()
        self.bfl_api_key = bfl_api_key
        self.prompt = prompt
        self.out_dir = out_dir
        self.base_url = base_url
        self.endpoint = endpoint
        self.size = size
        self.output_format = output_format
        self.n = n
        self.base_name = base_name
        self.prompt_upsampling = prompt_upsampling
        self.safety_tolerance = safety_tolerance
        self.timeout_sec = timeout_sec

    def run(self):
        try:
            self.log.emit(
                f"BFL gen: base_url={self.base_url} endpoint={self.endpoint} size={self.size} format={self.output_format} n={self.n}"
            )
            paths = generate_image_from_prompt_bfl(
                prompt=self.prompt,
                out_dir=self.out_dir,
                api_key=self.bfl_api_key,
                base_url=self.base_url,
                endpoint=self.endpoint,
                size=self.size,
                output_format=self.output_format,
                n=self.n,
                base_name=self.base_name,
                prompt_upsampling=self.prompt_upsampling,
                safety_tolerance=self.safety_tolerance,
                timeout_sec=self.timeout_sec,
            )
            self.done.emit(paths)
        except Exception as e:
            self.error.emit(str(e))

# ---------- UI: Settings Dialog ----------
class ImageGenSettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Képgenerálás Beállítások")
        self.resize(500, 600)
        
        layout = QVBoxLayout(self)
        
        form_layout = QFormLayout()
        
        self.gen_provider = QComboBox()
        self.gen_provider.addItems(["OpenAI (gpt-image-1.5)", "BFL (FLUX)"])
        self.gen_provider.setCurrentIndex(0)
        self.gen_provider.currentIndexChanged.connect(self.on_provider_changed)
        form_layout.addRow("Provider:", self.gen_provider)

        self.gen_quality = QComboBox()
        self.gen_quality.addItems(["low", "medium", "high"])
        self.gen_quality.setCurrentText("medium")
        form_layout.addRow("Quality:", self.gen_quality)

        self.gen_transparent = QCheckBox("Transzparens háttér (PNG)")
        self.gen_transparent.setChecked(False)
        form_layout.addRow("", self.gen_transparent)

        self.bfl_key = QLineEdit(BFL_API_KEY)
        self.bfl_key.setEchoMode(QLineEdit.Password)
        self.bfl_key.setPlaceholderText("BFL_API_KEY (env) vagy ide írd be…")
        form_layout.addRow("BFL API kulcs:", self.bfl_key)

        self.bfl_base_url = QComboBox()
        self.bfl_base_url.addItems([
            "https://api.bfl.ai/v1",
            "https://api.eu.bfl.ai/v1",
            "https://api.us.bfl.ai/v1",
        ])
        self.bfl_base_url.setCurrentText(BFL_BASE_URL_DEFAULT)
        form_layout.addRow("BFL base URL:", self.bfl_base_url)

        self.bfl_endpoint = QComboBox()
        self.bfl_endpoint.addItems([
            "/flux-2-max",
            "/flux-2-pro",
            "/flux-2-flex",
            "/flux-2-klein-4b",
            "/flux-2-klein-9b",
            "/flux-kontext-max",
            "/flux-kontext-pro",
            "/flux-pro-1.1-ultra",
            "/flux-pro-1.1",
            "/flux-pro",
            "/flux-dev",
        ])
        self.bfl_endpoint.setCurrentText("/flux-pro-1.1")
        form_layout.addRow("BFL endpoint:", self.bfl_endpoint)

        self.bfl_output_format = QComboBox()
        self.bfl_output_format.addItems(["png", "jpeg"])
        self.bfl_output_format.setCurrentText("png")
        form_layout.addRow("Output format:", self.bfl_output_format)

        self.bfl_prompt_upsampling = QCheckBox("Prompt upsampling")
        self.bfl_prompt_upsampling.setChecked(False)
        form_layout.addRow("", self.bfl_prompt_upsampling)

        self.bfl_safety = QSpinBox()
        self.bfl_safety.setRange(0, 6)
        self.bfl_safety.setValue(2)
        form_layout.addRow("Safety tolerance (0-6):", self.bfl_safety)

        self.bfl_timeout = QSpinBox()
        self.bfl_timeout.setRange(30, 900)
        self.bfl_timeout.setValue(180)
        form_layout.addRow("Timeout (sec):", self.bfl_timeout)

        self.gen_size = QComboBox()
        # Default starting items (will be updated by on_provider_changed)
        self.gen_size.addItems(["1024x1024"]) 
        form_layout.addRow("Méret:", self.gen_size)

        self.gen_n = QSpinBox()
        self.gen_n.setRange(1, 10)
        self.gen_n.setValue(1)
        form_layout.addRow("Darab (n):", self.gen_n)
        
        layout.addLayout(form_layout)
        
        # Dialog buttons
        self.button_box = QDialogButtonBox(QDialogButtonBox.Ok)
        self.button_box.accepted.connect(self.accept)
        layout.addWidget(self.button_box)
        
        self.on_provider_changed()

    def on_provider_changed(self):
        is_openai = (self.gen_provider.currentIndex() == 0)

        self.gen_quality.setEnabled(is_openai)
        self.gen_transparent.setEnabled(is_openai)

        self.bfl_key.setEnabled(not is_openai)
        self.bfl_base_url.setEnabled(not is_openai)
        self.bfl_endpoint.setEnabled(not is_openai)
        self.bfl_output_format.setEnabled(not is_openai)
        self.bfl_prompt_upsampling.setEnabled(not is_openai)
        self.bfl_safety.setEnabled(not is_openai)
        self.bfl_timeout.setEnabled(not is_openai)

        # Update resolutions based on provider
        current_size = self.gen_size.currentText()
        self.gen_size.clear()
        
        if is_openai:
            # DALL-E 3 standard sizes
            self.gen_size.addItems([
                "1024x1024",
                "1024x1792",
                "1792x1024"
            ])
        else:
            # BFL / Flux supported sizes
            self.gen_size.addItems([
                "1024x1024",   # Square
                "768x1344",    # Mobile 9:16
                "1344x768",    # Desktop 16:9
                "1080x1920",   # FHD Portrait
                "1920x1080",   # FHD Landscape
                "1152x896",    # Landscape 4:3
                "896x1152",    # Portrait 3:4
                "1024x1536",   # Portrait 2:3
                "1536x1024",   # Landscape 3:2
            ])
        
        # Try to restore previous selection if it exists in new list
        idx = self.gen_size.findText(current_size)
        if idx >= 0:
            self.gen_size.setCurrentIndex(idx)
        else:
            self.gen_size.setCurrentIndex(0)

# ---------- UI ----------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Póló Promptoló – Univerzális crawler + Képgenerálás (OpenAI gpt-image-1.5 / BFL FLUX)")
        self.worker = None
        self.gen_worker = None
        self._results = {}
        self._image_urls = {}
        self.current_design_name = None

        root = QWidget()
        self.setCentralWidget(root)
        main_layout = QVBoxLayout(root)

        # =======================================================
        # 1. INITIALIZE ALL WIDGETS & SUB-LAYOUTS
        # =======================================================

        # --- Row 0: API Key ---
        row0 = QHBoxLayout()
        row0.addWidget(QLabel("OpenAI API kulcs:"))
        self.key_edit = QLineEdit(OPENAI_API_KEY)
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.key_edit.setPlaceholderText("OPENAI_API_KEY (env) vagy ide írd be…")
        row0.addWidget(self.key_edit)

        # --- Input Tabs (URL vs File) ---
        self.input_tabs = QTabWidget()
        
        # Tab 1: Web
        tab_web = QWidget()
        web_layout = QVBoxLayout(tab_web)
        
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Weboldal URL:"))
        self.seed_edit = QLineEdit()
        self.seed_edit.setPlaceholderText("https://polomania.hu/piacter/tag/januar1")
        row1.addWidget(self.seed_edit)
        web_layout.addLayout(row1)
        
        # Tab 2: Helyi fájlok
        tab_local = QWidget()
        local_layout = QVBoxLayout(tab_local)
        
        row_local = QHBoxLayout()
        self.local_files_btn = QPushButton("Képfájlok kiválasztása (JPG, PNG, WEBP)...")
        self.local_files_btn.clicked.connect(self.pick_local_files)
        self.local_files_label = QLabel("Nincs fájl kiválasztva")
        row_local.addWidget(self.local_files_btn)
        row_local.addWidget(self.local_files_label)
        local_layout.addLayout(row_local)
        
        self.selected_files = []

        self.input_tabs.addTab(tab_web, "Weboldal szkennelés")
        self.input_tabs.addTab(tab_local, "Helyi fájlok")

        # --- Row 2: Output dir ---
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Kimenet mappa (prompt JSON):"))
        self.out_edit = QLineEdit(os.path.abspath("out"))
        btn_out = QPushButton("Tallóz…")
        btn_out.clicked.connect(self.pick_out_dir)
        row2.addWidget(self.out_edit)
        row2.addWidget(btn_out)

        # --- Row 3: Cache dir ---
        row3 = QHBoxLayout()
        row3.addWidget(QLabel("Cache mappa:"))
        self.cache_edit = QLineEdit(os.path.abspath("cache"))
        btn_cache = QPushButton("Tallóz…")
        btn_cache.clicked.connect(self.pick_cache_dir)
        row3.addWidget(self.cache_edit)
        row3.addWidget(btn_cache)

        # --- Row 4: Settings (Max, Delay, Scroll) ---
        row4 = QHBoxLayout()
        row4.addWidget(QLabel("Max kép:"))
        self.max_items = QSpinBox()
        self.max_items.setRange(1, 100000)
        self.max_items.setValue(200)
        row4.addWidget(self.max_items)

        row4.addWidget(QLabel("Delay (ms):"))
        self.delay_ms = QSpinBox()
        self.delay_ms.setRange(0, 60000)
        self.delay_ms.setValue(900)
        row4.addWidget(self.delay_ms)

        row4.addWidget(QLabel("Scroll körök:"))
        self.scroll_rounds = QSpinBox()
        self.scroll_rounds.setRange(1, 200)
        self.scroll_rounds.setValue(12)
        row4.addWidget(self.scroll_rounds)

        # --- Row 5: Filters + Options ---
        row5 = QHBoxLayout()
        self.headless_cb = QCheckBox("Headless (háttérben fusson a böngésző)")
        self.headless_cb.setChecked(True)
        row5.addWidget(self.headless_cb)

        self.force_text_cb = QCheckBox("Szöveg kiolvasása és beírása a promptba (FONTOS)")
        self.force_text_cb.setChecked(True)
        row5.addWidget(self.force_text_cb)
        
        row5.addWidget(QLabel("Prompt háttér:"))
        self.bg_combo = QComboBox()
        self.bg_combo.addItems([
            "Transzparens / Izolált (default)", 
            "Fehér", 
            "Fekete", 
            "Neon Zöld (Green Screen)", 
            "Hot Pink", 
            "Nincs megkötés"
        ])
        self.bg_combo.setToolTip("A promptba épített utasítás a háttérre vonatkozóan.")
        row5.addWidget(self.bg_combo)
        
        self.contour_cb = QCheckBox("Fekete kontúr")
        self.contour_cb.setToolTip("Sticker style: vastag fekete körvonal a minta körül.")
        row5.addWidget(self.contour_cb)

        # --- Row 6: Path Filter ---
        row6 = QHBoxLayout()
        row6.addWidget(QLabel("Kép URL szűrő (path contains):"))
        self.path_filter_edit = QLineEdit("")
        self.path_filter_edit.setToolTip("Polomania esetén pl: /images/designs/ | Lolmarkt-nál hagyd üresen.")
        row6.addWidget(self.path_filter_edit)

        # --- Row 7: Extra hosts ---
        row7 = QHBoxLayout()
        row7.addWidget(QLabel("Extra allowed hosts (CSV):"))
        self.extra_hosts_edit = QLineEdit("lolmarkt.cdn.shoprenter.hu")
        self.extra_hosts_edit.setToolTip("Vesszővel: pl. lolmarkt.cdn.shoprenter.hu,cdn2.valami.hu")
        row7.addWidget(self.extra_hosts_edit)

        self.require_productish_cb = QCheckBox("Csak 'product jellegű' képek (class/alt heurisztika)")
        self.require_productish_cb.setChecked(True)
        row7.addWidget(self.require_productish_cb)

        # --- Buttons Row ---
        row_btn = QHBoxLayout()
        self.start_btn = QPushButton("START (Prompt gyártás)")
        self.stop_btn = QPushButton("STOP")
        self.stop_btn.setEnabled(False)
        self.start_btn.clicked.connect(self.start)
        self.stop_btn.clicked.connect(self.stop)
        row_btn.addWidget(self.start_btn)
        row_btn.addWidget(self.stop_btn)

        # --- Image Generation Box ---
        gen_box = QGroupBox("Képgenerálás")
        gen_layout = QVBoxLayout(gen_box)
        
        # Settings Dialog
        self.gen_settings = ImageGenSettingsDialog(self)
        
        # Gen Buttons
        btn_row_gen = QHBoxLayout()
        btn_settings = QPushButton("⚙️ Beállítások")
        btn_settings.clicked.connect(self.open_gen_settings)
        btn_row_gen.addWidget(btn_settings)
        
        self.gen_btn = QPushButton("GENERATE IMAGE")
        self.gen_btn.setStyleSheet("font-weight: bold; padding: 6px;")
        self.gen_btn.clicked.connect(self.generate_image_clicked)
        btn_row_gen.addWidget(self.gen_btn)
        
        gen_layout.addLayout(btn_row_gen)
        
        # Gen Output folder
        out_img_row = QHBoxLayout()
        out_img_row.addWidget(QLabel("Mappa:"))
        self.gen_out_edit = QLineEdit(os.path.abspath(os.path.join("out", "generated_images")))
        btn_gen_out = QPushButton("Tallóz…")
        btn_gen_out.clicked.connect(self.pick_gen_out_dir)
        out_img_row.addWidget(self.gen_out_edit)
        out_img_row.addWidget(btn_gen_out)
        gen_layout.addLayout(out_img_row)

        self.preview = QLabel("Előnézet: (generálás után itt megjelenik)")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumHeight(250)
        self.preview.setStyleSheet("border: 1px solid #444; background: #222;")
        gen_layout.addWidget(self.preview)

        # --- Log Widget ---
        self.log = QTextEdit()
        self.log.setReadOnly(True)

        # --- Results List & Prompt View ---
        self.result_list = QListWidget()
        self.result_list.currentItemChanged.connect(self.on_result_selected)
        self.result_list.itemDoubleClicked.connect(self.copy_prompt_of_item)

        self.prompt_view = QTextEdit()
        self.prompt_view.setReadOnly(False)
        self.prompt_view.setPlaceholderText("Kattints egy elemre a listában… vagy írj be saját promptot…")


        # =======================================================
        # 2. BUILDING THE SPLIT LAYOUT
        # =======================================================

        # --- LEFT PANEL (Results + Prompt) ---
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        
        left_layout.addWidget(QLabel("Elkészült elemzések (dupla katt = prompt másolás):"))
        left_layout.addWidget(self.result_list)
        
        left_layout.addWidget(QLabel("Prompt nézet (ezt használja a képgenerálás is):"))
        left_layout.addWidget(self.prompt_view)

        # --- RIGHT PANEL (Controls + Gen + Log) ---
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        
        # Input
        right_layout.addWidget(self.input_tabs)
        
        # Settings Rows
        right_layout.addLayout(row0)
        right_layout.addLayout(row2)
        right_layout.addLayout(row3)
        right_layout.addLayout(row4)
        right_layout.addLayout(row5)
        right_layout.addLayout(row6)
        right_layout.addLayout(row7)
        
        # Crawler Buttons
        right_layout.addLayout(row_btn)
        
        # Image Gen
        right_layout.addWidget(gen_box)
        
        # Log
        right_layout.addWidget(self.log)

        # --- SPLITTER ---
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left_widget)  # Main Content (70%)
        splitter.addWidget(right_widget) # Controls (30%)
        
        splitter.setSizes([900, 380])
        
        main_layout.addWidget(splitter)
        self.resize(1380, 900)

    # ----- UI helpers -----
    def open_gen_settings(self):
        self.gen_settings.exec()

    def pick_local_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Képek kiválasztása", "", "Images (*.png *.jpg *.jpeg *.webp)"
        )
        if files:
            self.selected_files = files
            self.local_files_label.setText(f"{len(files)} fájl kiválasztva")
        else:
            self.selected_files = []
            self.local_files_label.setText("Nincs fájl kiválasztva")

    def api_key(self) -> str:
        return self.key_edit.text().strip()

    def pick_out_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Kimenet mappa kiválasztása")
        if d:
            self.out_edit.setText(d)

    def pick_cache_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Cache mappa kiválasztása")
        if d:
            self.cache_edit.setText(d)

    def pick_gen_out_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Kép kimenet mappa kiválasztása")
        if d:
            self.gen_out_edit.setText(d)

    def append_log(self, s: str):
        self.log.append(s)

    # Movied to ImageGenSettingsDialog
    # def on_provider_changed(self): ...

    def get_bg_type_code(self):
        idx = self.bg_combo.currentIndex()
        if idx == 1: return "white"
        if idx == 2: return "black"
        if idx == 3: return "neon_green"
        if idx == 4: return "hot_pink"
        if idx == 5: return "none"
        return "transparent"

    # ----- Prompt crawl -----
    def start(self):
        is_local = (self.input_tabs.currentIndex() == 1)
        seed = ""
        local_files = None

        if is_local:
            if not self.selected_files:
                QMessageBox.warning(self, "Hiányzik", "Válassz ki legalább egy képfájlt.")
                return
            local_files = self.selected_files
        else:
            seed = self.seed_edit.text().strip()
            if not seed:
                QMessageBox.warning(self, "Hiányzik", "Adj meg URL-t.")
                return

        if not self.api_key():
            QMessageBox.critical(self, "Kulcs", "Hiányzik az OpenAI API kulcs (env vagy fenti mező).")
            return

        self.worker = ImageCrawlWorker(
            api_key=self.api_key(),
            seed_url=seed,
            local_files=local_files,
            out_dir=self.out_edit.text().strip(),
            cache_dir=self.cache_edit.text().strip(),
            max_items=int(self.max_items.value()),
            delay_ms=int(self.delay_ms.value()),
            headless=self.headless_cb.isChecked(),
            scroll_rounds=int(self.scroll_rounds.value()),
            path_filter=self.path_filter_edit.text(),
            force_text=self.force_text_cb.isChecked(),
            extra_hosts=self.extra_hosts_edit.text(),
            require_productish=self.require_productish_cb.isChecked(),
            bg_type=self.get_bg_type_code(),
            use_contour=self.contour_cb.isChecked(),
        )
        self.worker.log.connect(self.append_log)
        self.worker.done.connect(self.on_done)
        self.worker.error.connect(self.on_error)
        self.worker.result_ready.connect(self.on_result_ready)

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.append_log("=== START (prompt) ===")
        self.worker.start()

    def stop(self):
        if self.worker:
            self.worker.stop()
            self.append_log("STOP kérés elküldve…")

    def on_done(self):
        self.append_log("=== KÉSZ (prompt) ===")
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def on_error(self, msg: str):
        self.append_log("=== HIBA ===")
        self.append_log(msg)
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        QMessageBox.critical(self, "Hiba", msg)

    # ----- Results viewer -----
    def on_result_ready(self, name: str, data: dict, image_url: str):
        self._results[name] = data
        self._image_urls[name] = image_url

        existing = self.result_list.findItems(name, Qt.MatchExactly)
        if not existing:
            self.result_list.addItem(QListWidgetItem(name))
            self.result_list.scrollToBottom()

        self.result_list.setCurrentRow(self.result_list.count() - 1)

    def on_result_selected(self, current, previous):
        if not current:
            return
        name = current.text()
        data = self._results.get(name, {})
        prompt = data.get("prompt", "")
        self.current_design_name = name
        self.prompt_view.setPlainText(prompt)

    def copy_prompt_of_item(self, item: QListWidgetItem):
        name = item.text()
        data = self._results.get(name, {})
        prompt = data.get("prompt", "")
        if prompt:
            QApplication.clipboard().setText(prompt)
            self.append_log(f"📋 Másolva vágólapra: {name}")

    # ----- Image generation -----
    def generate_image_clicked(self):
        if self.gen_worker and self.gen_worker.isRunning():
            QMessageBox.information(self, "Fut", "Már fut egy generálás.")
            return

        prompt = self.prompt_view.toPlainText().strip()
        if not prompt:
            QMessageBox.warning(self, "Prompt", "A prompt mező üres.")
            return

        out_dir = self.gen_out_edit.text().strip()
        
        # Access settings from dialog
        s = self.gen_settings
        
        size = s.gen_size.currentText()
        n = int(s.gen_n.value())

        base_name = self.current_design_name or prompt.splitlines()[0][:80]

        is_openai = (s.gen_provider.currentIndex() == 0)

        self.gen_btn.setEnabled(False)
        self.preview.setText("Generálás folyamatban…")

        if is_openai:
            if not self.api_key():
                QMessageBox.critical(self, "Kulcs", "Hiányzik az OpenAI API kulcs (env vagy fenti mező).")
                self.gen_btn.setEnabled(True)
                return

            quality = s.gen_quality.currentText()
            transparent = s.gen_transparent.isChecked()

            self.gen_worker = ImageGenWorker(
                api_key=self.api_key(),
                prompt=prompt,
                out_dir=out_dir,
                quality=quality,
                size=size,
                transparent=transparent,
                n=n,
                base_name=base_name
            )
            self.gen_worker.log.connect(self.append_log)
            self.gen_worker.error.connect(self.on_image_gen_error)
            self.gen_worker.done.connect(self.on_image_gen_done)
            self.append_log("=== START (image gen / OpenAI) ===")
            self.gen_worker.start()
        else:
            bfl_key = s.bfl_key.text().strip()
            if not bfl_key:
                QMessageBox.critical(self, "Kulcs", "Hiányzik a BFL API kulcs (BFL_API_KEY).")
                self.gen_btn.setEnabled(True)
                return

            base_url = s.bfl_base_url.currentText().strip()
            endpoint = s.bfl_endpoint.currentText().strip()
            output_format = s.bfl_output_format.currentText().strip()
            prompt_upsampling = s.bfl_prompt_upsampling.isChecked()
            safety_tolerance = int(s.bfl_safety.value())
            timeout_sec = int(s.bfl_timeout.value())

            self.gen_worker = BFLImageGenWorker(
                bfl_api_key=bfl_key,
                prompt=prompt,
                out_dir=out_dir,
                base_url=base_url,
                endpoint=endpoint,
                size=size,
                output_format=output_format,
                n=n,
                base_name=base_name,
                prompt_upsampling=prompt_upsampling,
                safety_tolerance=safety_tolerance,
                timeout_sec=timeout_sec
            )
            self.gen_worker.log.connect(self.append_log)
            self.gen_worker.error.connect(self.on_image_gen_error)
            self.gen_worker.done.connect(self.on_image_gen_done)
            self.append_log("=== START (image gen / BFL) ===")
            self.gen_worker.start()

    def on_image_gen_error(self, msg: str):
        self.append_log("=== HIBA (image gen) ===")
        self.append_log(msg)
        self.gen_btn.setEnabled(True)
        self.preview.setText("Előnézet: (hiba)")
        QMessageBox.critical(self, "Képgenerálás hiba", msg)

    def on_image_gen_done(self, paths: list):
        self.append_log("=== KÉSZ (image gen) ===")
        for p in paths:
            self.append_log(f"Mentve: {p}")

        if paths:
            p0 = paths[0]
            pix = QPixmap(p0)
            if not pix.isNull():
                self.preview.setPixmap(
                    pix.scaled(
                        self.preview.width(),
                        self.preview.height(),
                        Qt.KeepAspectRatio,
                        Qt.SmoothTransformation
                    )
                )
            else:
                self.preview.setText(f"Előnézet: nem tudtam betölteni: {p0}")
        else:
            self.preview.setText("Előnézet: nem jött kép.")

        self.gen_btn.setEnabled(True)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        pm = self.preview.pixmap()
        if pm and not pm.isNull():
            self.preview.setPixmap(
                pm.scaled(
                    self.preview.width(),
                    self.preview.height(),
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation
                )
            )

def main():
    app = QApplication([])
    w = MainWindow()
    w.show()
    app.exec()

if __name__ == "__main__":
    main()
