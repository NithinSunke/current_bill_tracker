from __future__ import annotations

import os
import sqlite3
import uuid
import hashlib
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, abort, flash, g, redirect, render_template, request, send_from_directory, url_for
from PIL import Image, ImageFilter, ImageOps
from pypdf import PdfReader
import pypdfium2 as pdfium
import pytesseract
try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional for local sqlite fallback
    psycopg = None
    dict_row = None
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from bill_tracker.extractor import BILL_FIELDS, build_bill_draft


BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
UPLOAD_DIR = BASE_DIR / "uploads"
DATABASE_PATH = INSTANCE_DIR / "bill_tracker.db"
OCR_CACHE_DIR = INSTANCE_DIR / "ocr_cache"
OCR_CACHE_VERSION = "v5"
DEFAULT_SQLITE_URL = f"sqlite:///{DATABASE_PATH}"
DATABASE_URL = os.environ.get("DATABASE_URL", DEFAULT_SQLITE_URL)
ALLOWED_EXTENSIONS = {"pdf", "png", "jpg", "jpeg", "webp"}
APP_NAME = "All Bills Tracker"
BILL_TYPE_OPTIONS = [
    "electricity",
    "water",
    "gas",
    "internet",
    "mobile",
    "rent",
    "insurance",
    "school_fee",
    "maintenance",
    "loan",
    "credit_card",
    "other",
]


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "local-dev-secret")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024


def ensure_directories() -> None:
    INSTANCE_DIR.mkdir(exist_ok=True)
    UPLOAD_DIR.mkdir(exist_ok=True)
    OCR_CACHE_DIR.mkdir(exist_ok=True)


def db_backend() -> str:
    if DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://"):
        return "postgres"
    return "sqlite"


def sqlite_db_path() -> Path:
    return Path(DATABASE_URL.replace("sqlite:///", "", 1))


def get_db():
    if "db" not in g:
        if db_backend() == "postgres":
            if psycopg is None:
                raise RuntimeError("psycopg is required for PostgreSQL connections.")
            connection = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        else:
            connection = sqlite3.connect(sqlite_db_path())
            connection.row_factory = sqlite3.Row
        g.db = connection
    return g.db


def sql_placeholders(query: str) -> str:
    if db_backend() == "postgres":
        return query.replace("?", "%s")
    return query


def db_fetchone(query: str, params: tuple[Any, ...] = ()) -> Any:
    db = get_db()
    cursor = db.execute(sql_placeholders(query), params)
    return cursor.fetchone()


def db_fetchall(query: str, params: tuple[Any, ...] = ()) -> list[Any]:
    db = get_db()
    cursor = db.execute(sql_placeholders(query), params)
    return cursor.fetchall()


def db_execute(query: str, params: tuple[Any, ...] = (), commit: bool = False) -> None:
    db = get_db()
    db.execute(sql_placeholders(query), params)
    if commit:
        db.commit()


def db_insert_bill(params: tuple[Any, ...]) -> int:
    db = get_db()
    query = """
        INSERT INTO bills (
            provider, bill_type, consumer_name, service_number, area_code, mobile_number,
            address, bill_date, billing_month, due_date, last_paid_date, units_consumed,
            net_amount, notes, raw_extracted_text, file_path, original_filename,
            content_type, review_status, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    if db_backend() == "postgres":
        cursor = db.execute(sql_placeholders(query) + " RETURNING id", params)
        inserted_id = cursor.fetchone()["id"]
    else:
        cursor = db.execute(query, params)
        inserted_id = cursor.lastrowid
    db.commit()
    return int(inserted_id)


@app.teardown_appcontext
def close_db(_: BaseException | None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    if db_backend() == "postgres":
        if psycopg is None:
            raise RuntimeError("psycopg is required for PostgreSQL connections.")
        db = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        schema = """
            CREATE TABLE IF NOT EXISTS bills (
                id BIGSERIAL PRIMARY KEY,
                provider TEXT,
                bill_type TEXT,
                consumer_name TEXT,
                service_number TEXT,
                area_code TEXT,
                mobile_number TEXT,
                address TEXT,
                bill_date TEXT,
                billing_month TEXT,
                due_date TEXT,
                last_paid_date TEXT,
                units_consumed INTEGER,
                net_amount DOUBLE PRECISION,
                notes TEXT,
                raw_extracted_text TEXT,
                file_path TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                content_type TEXT,
                review_status TEXT NOT NULL DEFAULT 'needs_review',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """
    else:
        db = sqlite3.connect(sqlite_db_path())
        schema = """
            CREATE TABLE IF NOT EXISTS bills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT,
                bill_type TEXT,
                consumer_name TEXT,
                service_number TEXT,
                area_code TEXT,
                mobile_number TEXT,
                address TEXT,
                bill_date TEXT,
                billing_month TEXT,
                due_date TEXT,
                last_paid_date TEXT,
                units_consumed INTEGER,
                net_amount REAL,
                notes TEXT,
                raw_extracted_text TEXT,
                file_path TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                content_type TEXT,
                review_status TEXT NOT NULL DEFAULT 'needs_review',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """

    db.execute(schema)
    ensure_column(db, "bills", "notes", "TEXT")
    db.commit()
    db.close()


def ensure_column(db: Any, table_name: str, column_name: str, definition: str) -> None:
    if db_backend() == "postgres":
        exists = db.execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = %s AND column_name = %s
            """,
            (table_name, column_name),
        ).fetchone()
        if not exists:
            db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")
        return

    columns = db.execute(f"PRAGMA table_info({table_name})").fetchall()
    if not any(column[1] == column_name for column in columns):
        db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def normalize_bill_type(value: str) -> str:
    cleaned = value.strip().lower().replace("-", "_").replace(" ", "_")
    return cleaned


def display_bill_type(value: str | None) -> str:
    if not value:
        return "Not set"
    return value.replace("_", " ").title()


@app.context_processor
def inject_template_helpers() -> dict[str, Any]:
    return {
        "app_name": APP_NAME,
        "bill_type_options": BILL_TYPE_OPTIONS,
        "display_bill_type": display_bill_type,
    }


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def save_upload(upload: FileStorage) -> tuple[str, str]:
    if not upload.filename:
        raise ValueError("Please choose a bill PDF or image to upload.")
    if not allowed_file(upload.filename):
        raise ValueError("Only PDF, PNG, JPG, JPEG, and WEBP files are supported.")

    safe_name = secure_filename(upload.filename)
    extension = safe_name.rsplit(".", 1)[1].lower()
    stored_name = f"{uuid.uuid4().hex}.{extension}"
    upload.save(UPLOAD_DIR / stored_name)
    return stored_name, safe_name


def extract_pdf_text(stored_name: str) -> str:
    file_path = UPLOAD_DIR / stored_name
    try:
        reader = PdfReader(str(file_path))
    except Exception:
        return ""

    pages: list[str] = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if text.strip():
            pages.append(text)
    return "\n".join(pages).strip()


def prepare_image_for_ocr(image: Image.Image) -> Image.Image:
    grayscale = ImageOps.grayscale(image)
    boosted = ImageOps.autocontrast(grayscale)
    return boosted


def crop_to_content(image: Image.Image, threshold: int = 245, padding: int = 24) -> Image.Image:
    grayscale = ImageOps.grayscale(image)
    binary = grayscale.point(lambda px: 255 if px > threshold else 0, mode="1")
    inverted = ImageOps.invert(binary.convert("L"))
    bbox = inverted.getbbox()
    if not bbox:
        return image

    left, top, right, bottom = bbox
    left = max(0, left - padding)
    top = max(0, top - padding)
    right = min(image.width, right + padding)
    bottom = min(image.height, bottom + padding)
    return image.crop((left, top, right, bottom))


def build_ocr_variants(image: Image.Image, aggressive: bool = False) -> list[Image.Image]:
    base = prepare_image_for_ocr(image)
    enlarged = base.resize((int(base.width * 1.35), int(base.height * 1.35)), Image.Resampling.LANCZOS)
    sharpened = enlarged.filter(ImageFilter.SHARPEN)
    variants = [sharpened]
    if aggressive:
        binary = sharpened.point(lambda px: 255 if px > 180 else 0, mode="1").convert("L")
        variants.append(binary)
    return variants


def ocr_lines_from_variants(variants: list[Image.Image], configs: list[str]) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()

    for variant in variants:
        for config in configs:
            text = pytesseract.image_to_string(
                variant,
                config=f"{config} -c preserve_interword_spaces=1",
            ).strip()
            for line in text.splitlines():
                cleaned = " ".join(line.split()).strip()
                if len(cleaned) < 3:
                    continue
                normalized = cleaned.lower()
                if normalized in seen:
                    continue
                seen.add(normalized)
                lines.append(cleaned)
    return lines


def crop_relative(image: Image.Image, left: float, top: float, right: float, bottom: float) -> Image.Image:
    width, height = image.size
    box = (
        max(0, int(width * left)),
        max(0, int(height * top)),
        min(width, int(width * right)),
        min(height, int(height * bottom)),
    )
    return image.crop(box)


def merge_unique_lines(existing: list[str], new_lines: list[str]) -> list[str]:
    seen = {line.lower() for line in existing}
    for line in new_lines:
        lowered = line.lower()
        if lowered not in seen:
            existing.append(line)
            seen.add(lowered)
    return existing


def normalize_digits(value: str, target_length: int | None = None) -> str:
    translated = str.maketrans({
        "O": "0",
        "Q": "0",
        "D": "0",
        "A": "1",
        "I": "1",
        "L": "1",
        "|": "1",
        "S": "5",
        "B": "8",
        "G": "6",
    })
    cleaned = value.upper().translate(translated)
    digits = re.sub(r"\D", "", cleaned)
    if target_length and len(digits) > target_length:
        digits = digits[:target_length]
    return digits


def score_digit_candidate(value: str, target_length: int, prefer_zeros: bool = False) -> tuple[int, int, int]:
    digits = normalize_digits(value)
    return (
        -(abs(len(digits) - target_length)),
        digits.count("0") if prefer_zeros else 0,
        -sum(ch in {"8", "9", "6"} for ch in digits),
    )


def ocr_region_text(image: Image.Image, spec: tuple[float, float, float, float], config: str, aggressive: bool = False) -> str:
    region = crop_relative(image, *spec)
    variants = build_ocr_variants(region, aggressive=aggressive)
    lines = ocr_lines_from_variants(variants, [config])
    return "\n".join(lines).strip()


def ocr_best_line(
    image: Image.Image,
    spec: tuple[float, float, float, float],
    config: str,
    aggressive: bool = False,
) -> str:
    region = crop_relative(image, *spec)
    variants = build_ocr_variants(region, aggressive=aggressive)
    best_line = ""
    best_score = -1

    for variant in variants:
        text = pytesseract.image_to_string(
            variant,
            config=f"{config} -c preserve_interword_spaces=1",
        ).strip()
        for line in text.splitlines():
            cleaned = " ".join(line.split()).strip()
            score = sum(ch.isalnum() for ch in cleaned)
            if score > best_score:
                best_score = score
                best_line = cleaned

    return best_line


def ocr_best_digit_line(
    image: Image.Image,
    specs: list[tuple[float, float, float, float]],
    config: str,
    target_length: int,
    aggressive: bool = False,
    prefer_zeros: bool = False,
) -> str:
    candidates: list[str] = []
    for spec in specs:
        region = crop_relative(image, *spec)
        variants = build_ocr_variants(region, aggressive=aggressive)
        for variant in variants:
            text = pytesseract.image_to_string(
                variant,
                config=f"{config} -c preserve_interword_spaces=1",
            ).strip()
            for line in text.splitlines():
                cleaned = " ".join(line.split()).strip()
                if cleaned:
                    candidates.append(cleaned)

    if not candidates:
        return ""
    return max(candidates, key=lambda value: score_digit_candidate(value, target_length, prefer_zeros=prefer_zeros))


def extract_bill_hint_lines(image: Image.Image) -> list[str]:
    content = crop_to_content(image)
    hints: list[str] = []

    header_text = ocr_region_text(
        content,
        (0.12, 0.00, 0.92, 0.15),
        "--oem 3 --psm 7 -l eng -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ.- ",
        aggressive=True,
    ).upper()
    compact_header = re.sub(r"[^A-Z]", "", header_text)
    if "NPDCL" in compact_header:
        hints.append("Provider: TGNPDCL")
    elif any(token in compact_header for token in ["SPDCL", "TESPPCI", "TGSPDCI", "TESPDCI", "TESPPCI", "TESPDCI"]):
        hints.append("Provider: TGSPDCL")
    elif "TELANGANA" in compact_header and any(token in compact_header for token in ["TESP", "TESP", "SPDC", "SPPCI"]):
        hints.append("Provider: TGSPDCL")

    meta_text = ocr_region_text(content, (0.10, 0.14, 0.92, 0.31), "--oem 3 --psm 6 -l eng", aggressive=True)
    consumer_text = ocr_region_text(content, (0.10, 0.30, 0.92, 0.57), "--oem 3 --psm 6 -l eng", aggressive=True)
    footer_text = ocr_region_text(content, (0.10, 0.86, 0.92, 0.99), "--oem 3 --psm 6 -l eng", aggressive=True)
    usage_text = ocr_region_text(content, (0.10, 0.56, 0.92, 0.69), "--oem 3 --psm 6 -l eng")

    bill_date_line = ocr_best_line(
        content,
        (0.10, 0.145, 0.78, 0.19),
        "--oem 3 --psm 7 -l eng -c tessedit_char_whitelist=DTdt:0123456789/.- ",
        aggressive=True,
    )
    bill_date_digits = normalize_digits(bill_date_line)
    if len(bill_date_digits) >= 8:
        hints.append(f"Dt: {bill_date_digits[:2]}/{bill_date_digits[2:4]}/{bill_date_digits[4:8]}")

    area_line = ocr_best_digit_line(
        content,
        [(0.10, 0.23, 0.90, 0.28), (0.10, 0.34, 0.65, 0.40)],
        "--oem 3 --psm 7 -l eng -c tessedit_char_whitelist=AREACODESCNO:0123456789 ",
        target_length=5,
        aggressive=True,
        prefer_zeros=True,
    )
    area_digits = normalize_digits(area_line, target_length=5)
    if len(area_digits) >= 5:
        hints.append(f"AREACODE: {area_digits}")

    usc_line = ocr_best_digit_line(
        content,
        [(0.10, 0.37, 0.92, 0.42), (0.12, 0.36, 0.88, 0.43)],
        "--oem 3 --psm 7 -l eng -c tessedit_char_whitelist=USCNO:0123456789 ",
        target_length=9,
        aggressive=True,
        prefer_zeros=True,
    )
    usc_digits = normalize_digits(usc_line, target_length=9)
    if len(usc_digits) >= 8:
        hints.append(f"USC No: {usc_digits}")

    name_line = ocr_best_line(
        content,
        (0.10, 0.41, 0.92, 0.46),
        "--oem 3 --psm 7 -l eng -c tessedit_char_whitelist=NAME:ABCDEFGHIJKLMNOPQRSTUVWXYZ ",
        aggressive=True,
    )
    if name_line:
        cleaned_name = re.sub(r"^[^A-Z]*", "", name_line.upper())
        if "NAME" in cleaned_name:
            cleaned_name = cleaned_name.split("NAME", 1)[-1].strip(" :")
        if cleaned_name:
            hints.append(f"Name: {cleaned_name}")

    mobile_line = ocr_best_line(
        content,
        (0.10, 0.50, 0.92, 0.55),
        "--oem 3 --psm 7 -l eng -c tessedit_char_whitelist=MOBILE NO:0123456789 ",
        aggressive=True,
    )
    mobile_digits = normalize_digits(mobile_line, target_length=10)
    if len(mobile_digits) >= 10:
        hints.append(f"Mobile No: {mobile_digits}")

    units_line = ocr_best_line(
        content,
        (0.10, 0.63, 0.55, 0.68),
        "--oem 3 --psm 7 -l eng -c tessedit_char_whitelist=UNITS:0123456789 ",
        aggressive=True,
    )
    units_digits = normalize_digits(units_line, target_length=4)
    if units_digits:
        hints.append(f"Units: {units_digits}")

    amount_line = ocr_best_line(
        content,
        (0.45, 0.88, 0.92, 0.94),
        "--oem 3 --psm 7 -l eng -c tessedit_char_whitelist=TOTALDUE:0123456789. ",
        aggressive=True,
    )
    amount_digits = normalize_digits(amount_line)
    if len(amount_digits) >= 3:
        if len(amount_digits) >= 5:
            hints.append(f"Total Due: {amount_digits[:-2]}.{amount_digits[-2:]}")
        else:
            hints.append(f"Total Due: {amount_digits}")

    due_line = ocr_best_line(
        content,
        (0.40, 0.935, 0.92, 0.965),
        "--oem 3 --psm 7 -l eng -c tessedit_char_whitelist=DUEDATE:0123456789/.- ",
        aggressive=True,
    )
    due_digits = normalize_digits(due_line)
    if len(due_digits) >= 8:
        hints.append(f"Due Date: {due_digits[:2]}/{due_digits[2:4]}/{due_digits[4:8]}")

    last_paid_line = ocr_best_line(
        content,
        (0.40, 0.965, 0.92, 0.995),
        "--oem 3 --psm 7 -l eng -c tessedit_char_whitelist=LASTPAIDDT:0123456789/.- ",
        aggressive=True,
    )
    last_paid_digits = normalize_digits(last_paid_line)
    if len(last_paid_digits) >= 8:
        hints.append(f"Last Paid Dt: {last_paid_digits[:2]}/{last_paid_digits[2:4]}/{last_paid_digits[4:8]}")

    for source_text, label, target_length in [
        (meta_text, "AREACODE", 5),
        (consumer_text, "USC No", 9),
        (consumer_text, "Mobile No", 10),
        (usage_text, "Units", 4),
    ]:
        if label == "AREACODE":
            match = re.search(r"AREA\s*CODE[:\s]*([A-Z0-9 ]{4,10})", source_text, re.IGNORECASE)
            if match:
                digits = normalize_digits(match.group(1), target_length=target_length)
                if digits:
                    hints.append(f"AREACODE: {digits}")
        elif label == "USC No":
            match = re.search(r"USC\s*NO[:.\s]*([A-Z0-9 ]{6,15})", source_text, re.IGNORECASE)
            if match:
                digits = normalize_digits(match.group(1), target_length=target_length)
                if len(digits) >= 8:
                    hints.append(f"USC No: {digits}")
        elif label == "Mobile No":
            match = re.search(r"MOB(?:ILE)?\s*NO[:.\s]*([A-Z0-9 ]{8,15})", source_text, re.IGNORECASE)
            if match:
                digits = normalize_digits(match.group(1), target_length=target_length)
                if len(digits) >= 10:
                    hints.append(f"Mobile No: {digits}")
        elif label == "Units":
            match = re.search(r"UNITS\s*([A-Z0-9 ]{1,6})", source_text, re.IGNORECASE)
            if match:
                digits = normalize_digits(match.group(1), target_length=target_length)
                if digits:
                    hints.append(f"Units: {digits}")

    name_match = re.search(r"NAME[:.\s]*([A-Z ]{6,40})", consumer_text, re.IGNORECASE)
    if name_match:
        name = re.sub(r"\s+", " ", name_match.group(1)).strip()
        if name:
            hints.append(f"Name: {name}")

    addr_match = re.search(r"ADDR[:.\s]*([^\n]+(?:\n[^\n]+){0,2})", consumer_text, re.IGNORECASE)
    if addr_match:
        addr = re.sub(r"\s+", " ", addr_match.group(1).replace("\n", ", ")).strip(" ,")
        if addr:
            hints.append(f"Addr: {addr}")

    for label, source_text in [("Dt", meta_text), ("Due Date", footer_text), ("Last Paid Dt", footer_text)]:
        if label == "Dt":
            match = re.search(r"DT[:.\s]*([A-Z0-9/.\- ]{8,16})", source_text, re.IGNORECASE)
        else:
            match = re.search(rf"{label}[:.\s]*([A-Z0-9/.\- ]{{8,16}})", source_text, re.IGNORECASE)
        if match:
            raw = match.group(1)
            digits = normalize_digits(raw)
            if len(digits) >= 8:
                hints.append(f"{label}: {digits[:2]}/{digits[2:4]}/{digits[4:8]}")

    amount_match = re.search(r"TOTAL\s*DUE[:.\s]*([A-Z0-9 .]{3,12})", footer_text, re.IGNORECASE)
    if amount_match:
        amount_digits = normalize_digits(amount_match.group(1))
        if len(amount_digits) >= 3:
            if len(amount_digits) >= 5:
                hints.append(f"Total Due: {amount_digits[:-2]}.{amount_digits[-2:]}")
            else:
                hints.append(f"Total Due: {amount_digits}")

    return hints


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    hasher.update(OCR_CACHE_VERSION.encode("utf-8"))
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def load_cached_ocr(cache_key: str) -> str:
    cache_path = OCR_CACHE_DIR / f"{cache_key}.txt"
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8")
    return ""


def save_cached_ocr(cache_key: str, text: str) -> None:
    cache_path = OCR_CACHE_DIR / f"{cache_key}.txt"
    cache_path.write_text(text, encoding="utf-8")


def run_ocr_on_image(image: Image.Image) -> str:
    content = crop_to_content(image)
    full_page_configs = ["--oem 3 --psm 6 -l eng"]
    section_configs = ["--oem 3 --psm 6 -l eng"]
    lines = ocr_lines_from_variants(build_ocr_variants(content), full_page_configs)

    # Telangana bill layout is very vertical; region OCR often reads labels more accurately than whole-page OCR.
    section_specs = [
        (0.10, 0.14, 0.92, 0.31),
        (0.10, 0.30, 0.92, 0.57),
        (0.10, 0.86, 0.92, 0.99),
    ]
    for spec in section_specs:
        section = crop_relative(content, *spec)
        section_lines = ocr_lines_from_variants(
            build_ocr_variants(section),
            section_configs,
        )
        lines = merge_unique_lines(lines, section_lines)

    lines = merge_unique_lines(lines, extract_bill_hint_lines(content))

    return "\n".join(lines).strip()


def extract_image_text(stored_name: str) -> str:
    file_path = UPLOAD_DIR / stored_name
    cache_key = file_sha256(file_path)
    cached = load_cached_ocr(cache_key)
    if cached:
        return cached
    with Image.open(file_path) as image:
        text = run_ocr_on_image(image)
    if text:
        save_cached_ocr(cache_key, text)
    return text


def extract_scanned_pdf_text(stored_name: str, max_pages: int = 1) -> str:
    file_path = UPLOAD_DIR / stored_name
    cache_key = file_sha256(file_path)
    cached = load_cached_ocr(cache_key)
    if cached:
        return cached
    texts: list[str] = []
    pdf = pdfium.PdfDocument(str(file_path))
    page_count = min(len(pdf), max_pages)

    for index in range(page_count):
        page = pdf[index]
        bitmap = page.render(scale=1.5)
        pil_image = bitmap.to_pil()
        text = run_ocr_on_image(pil_image)
        if text:
            texts.append(text)
        page.close()

    pdf.close()
    combined = "\n".join(texts).strip()
    if combined:
        save_cached_ocr(cache_key, combined)
    return combined


def collect_bill_form(form: Any) -> dict[str, Any]:
    bill = {field: form.get(field, "").strip() for field in BILL_FIELDS}
    raw_text = form.get("raw_extracted_text", "").strip()
    draft = build_bill_draft(raw_text=raw_text, filename=form.get("filename_hint", ""))

    for field in BILL_FIELDS:
        if not bill[field] and draft.get(field):
            bill[field] = draft[field]

    bill["bill_type"] = normalize_bill_type(bill.get("bill_type", ""))
    units_value = bill.get("units_consumed") or None
    amount_value = bill.get("net_amount") or None

    try:
        bill["units_consumed"] = int(units_value) if units_value else None
    except ValueError as exc:
        raise ValueError("Usage or units must be a whole number.") from exc

    try:
        bill["net_amount"] = float(amount_value) if amount_value else None
    except ValueError as exc:
        raise ValueError("Amount must be a number.") from exc

    bill["review_status"] = form.get("review_status", "needs_review").strip() or "needs_review"
    bill["raw_extracted_text"] = raw_text
    return bill


def get_bill_or_404(bill_id: int) -> Any:
    bill = db_fetchone("SELECT * FROM bills WHERE id = ?", (bill_id,))
    if bill is None:
        abort(404)
    return bill


def delete_bill_record(bill_id: int) -> Any:
    bill = get_bill_or_404(bill_id)
    file_path = bill["file_path"] or ""

    db_execute("DELETE FROM bills WHERE id = ?", (bill_id,), commit=True)

    if file_path:
        remaining = db_fetchone("SELECT COUNT(*) AS count FROM bills WHERE file_path = ?", (file_path,))
        remaining_count = remaining["count"] if remaining else 0
        if remaining_count == 0:
            (UPLOAD_DIR / file_path).unlink(missing_ok=True)

    return bill


def fetch_bills(limit: int | None = None) -> list[Any]:
    query = "SELECT * FROM bills ORDER BY created_at DESC"
    params: tuple[Any, ...] = ()
    if limit is not None:
        query += " LIMIT ?"
        params = (limit,)
    return db_fetchall(query, params)


def parse_bill_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for pattern in ("%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, pattern)
        except ValueError:
            continue
    return None


def parse_billing_month(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("-", "/").strip()
    for pattern in ("%m/%Y", "%m/%y"):
        try:
            return datetime.strptime(normalized, pattern)
        except ValueError:
            continue
    return None


def build_ring_chart(
    items: list[dict[str, Any]],
    colors: list[str],
    value_key: str = "value",
) -> dict[str, Any]:
    if not items:
        return {"style": "", "segments": []}

    total = sum(float(item[value_key]) for item in items) or 1
    start = 0.0
    segments: list[dict[str, Any]] = []
    style_parts: list[str] = []

    for index, item in enumerate(items[:4]):
        value = float(item[value_key])
        percentage = (value / total) * 100
        end = start + percentage
        color = colors[index % len(colors)]
        style_parts.append(f"{color} {start:.2f}% {end:.2f}%")
        segments.append(
            {
                "label": item["label"],
                "value": item[value_key],
                "color": color,
            }
        )
        start = end

    if start < 100:
        style_parts.append(f"#e5e7eb {start:.2f}% 100%")

    return {
        "style": f"conic-gradient({', '.join(style_parts)})",
        "segments": segments,
    }


def build_dashboard_data(bills: list[Any], selected_year: str = "") -> dict[str, Any]:
    total_bills = len(bills)
    verified_bills = sum(1 for bill in bills if bill["review_status"] == "verified")
    total_amount = sum(float(bill["net_amount"] or 0) for bill in bills)
    total_units = sum(int(bill["units_consumed"] or 0) for bill in bills if bill["units_consumed"] is not None)
    active_types = len({bill["bill_type"] for bill in bills if bill["bill_type"]})

    provider_counts = Counter((bill["provider"] or "Unknown") for bill in bills)
    type_counts = Counter((display_bill_type(bill["bill_type"]) if bill["bill_type"] else "Not set") for bill in bills)
    status_counts = Counter((bill["review_status"] or "needs_review").replace("_", " ") for bill in bills)
    monthly_amounts: dict[str, float] = defaultdict(float)
    monthly_counts: dict[str, int] = defaultdict(int)
    yearly_amounts: dict[str, float] = defaultdict(float)
    yearly_counts: dict[str, int] = defaultdict(int)

    for bill in bills:
        bill_date = parse_bill_date(bill["bill_date"])
        billing_month = parse_billing_month(bill["billing_month"])
        chart_date = bill_date or billing_month
        if chart_date is None:
            continue

        amount = float(bill["net_amount"] or 0)
        monthly_bucket = chart_date.strftime("%b %Y")
        yearly_bucket = chart_date.strftime("%Y")
        monthly_amounts[monthly_bucket] += amount
        monthly_counts[monthly_bucket] += 1
        yearly_amounts[yearly_bucket] += amount
        yearly_counts[yearly_bucket] += 1

    provider_chart = [
        {
            "label": label,
            "value": value,
            "width": (value / max(provider_counts.values())) * 100 if provider_counts else 0,
        }
        for label, value in provider_counts.most_common()
    ]
    type_chart = [
        {
            "label": label,
            "value": value,
            "width": (value / max(type_counts.values())) * 100 if type_counts else 0,
        }
        for label, value in type_counts.most_common()
    ]
    status_chart = [
        {
            "label": label.title(),
            "value": value,
            "width": (value / max(status_counts.values())) * 100 if status_counts else 0,
        }
        for label, value in status_counts.items()
    ]

    available_years = sorted(yearly_amounts.keys(), key=int, reverse=True)
    if selected_year not in yearly_amounts:
        selected_year = available_years[0] if available_years else ""

    monthly_chart = []
    if monthly_amounts:
        ordered_months = sorted(
            monthly_amounts.items(),
            key=lambda item: datetime.strptime(item[0], "%b %Y"),
        )
        filtered_months = [
            (label, amount)
            for label, amount in ordered_months
            if not selected_year or label.endswith(selected_year)
        ]
        max_month_total = max((amount for _, amount in filtered_months), default=0) or 1
        for label, amount in filtered_months:
            monthly_chart.append(
                {
                    "label": label,
                    "amount": amount,
                    "count": monthly_counts[label],
                    "height": (amount / max_month_total) * 100,
                }
            )

    yearly_chart = []
    if yearly_amounts:
        ordered_years = sorted(yearly_amounts.items(), key=lambda item: int(item[0]))
        max_year_total = max(amount for _, amount in ordered_years) or 1
        for label, amount in ordered_years:
            yearly_chart.append(
                {
                    "label": label,
                    "amount": amount,
                    "count": yearly_counts[label],
                    "height": (amount / max_year_total) * 100,
                }
            )

    recent_due = sorted(
        bills,
        key=lambda bill: parse_bill_date(bill["due_date"]) or datetime.max,
    )[:5]
    top_amount_records = sorted(
        bills,
        key=lambda bill: float(bill["net_amount"] or 0),
        reverse=True,
    )[:5]

    type_ring = build_ring_chart(type_chart, ["#facc15", "#ef4444", "#84cc16", "#38bdf8"])
    status_ring = build_ring_chart(status_chart, ["#f59e0b", "#84cc16", "#d1d5db"])
    provider_ring = build_ring_chart(provider_chart, ["#06b6d4", "#8b5cf6", "#facc15", "#94a3b8"])

    return {
        "metrics": {
            "total_bills": total_bills,
            "verified_bills": verified_bills,
            "needs_review": total_bills - verified_bills,
            "total_amount": total_amount,
            "total_units": total_units,
            "active_types": active_types,
        },
        "type_chart": type_chart,
        "provider_chart": provider_chart,
        "status_chart": status_chart,
        "available_years": available_years,
        "selected_year": selected_year,
        "monthly_chart": monthly_chart,
        "yearly_chart": yearly_chart,
        "recent_due": recent_due,
        "top_amount_records": top_amount_records,
        "type_ring": type_ring,
        "status_ring": status_ring,
        "provider_ring": provider_ring,
    }


def blank_bill() -> dict[str, Any]:
    bill = {field: "" for field in BILL_FIELDS}
    bill["review_status"] = "needs_review"
    bill["file_path"] = ""
    bill["original_filename"] = ""
    bill["content_type"] = ""
    bill["raw_extracted_text"] = ""
    return bill


def bill_preview_from_form(form: Any) -> dict[str, Any]:
    bill = blank_bill()
    for field in BILL_FIELDS:
        bill[field] = form.get(field, "").strip()
    bill["review_status"] = form.get("review_status", "needs_review").strip() or "needs_review"
    bill["file_path"] = form.get("file_path", "").strip()
    bill["original_filename"] = form.get("original_filename", "").strip()
    bill["content_type"] = form.get("content_type", "").strip()
    bill["raw_extracted_text"] = form.get("raw_extracted_text", "").strip()
    return bill


def bill_has_content(bill: dict[str, Any]) -> bool:
    meaningful_fields = [
        "provider",
        "bill_type",
        "consumer_name",
        "service_number",
        "area_code",
        "mobile_number",
        "address",
        "bill_date",
        "billing_month",
        "due_date",
        "last_paid_date",
        "notes",
        "raw_extracted_text",
    ]
    if any(str(bill.get(field, "")).strip() for field in meaningful_fields):
        return True
    return bill.get("units_consumed") is not None or bill.get("net_amount") is not None


def build_library_data(
    bills: list[Any],
    search: str = "",
    selected_type: str = "",
    selected_status: str = "",
) -> dict[str, Any]:
    normalized_search = search.strip().lower()
    normalized_type = normalize_bill_type(selected_type) if selected_type else ""
    normalized_status = selected_status.strip().lower()

    filtered: list[Any] = []
    for bill in bills:
        haystack = " ".join(
            str(
                bill.get(field, "") if isinstance(bill, dict) else bill[field]
            )
            for field in ["consumer_name", "provider", "service_number", "original_filename", "bill_type", "notes"]
        ).lower()

        bill_type = normalize_bill_type((bill.get("bill_type", "") if isinstance(bill, dict) else bill["bill_type"]) or "")
        review_status = ((bill.get("review_status", "") if isinstance(bill, dict) else bill["review_status"]) or "").lower()

        if normalized_search and normalized_search not in haystack:
            continue
        if normalized_type and bill_type != normalized_type:
            continue
        if normalized_status and review_status != normalized_status:
            continue
        filtered.append(bill)

    available_types = sorted(
        {normalize_bill_type((bill.get("bill_type", "") if isinstance(bill, dict) else bill["bill_type"]) or "") for bill in bills if (bill.get("bill_type", "") if isinstance(bill, dict) else bill["bill_type"])},
        key=lambda item: display_bill_type(item),
    )

    total_amount = sum(float((bill.get("net_amount", 0) if isinstance(bill, dict) else bill["net_amount"]) or 0) for bill in filtered)

    return {
        "bills": filtered,
        "filters": {
            "search": search,
            "bill_type": normalized_type,
            "review_status": normalized_status,
        },
        "available_types": available_types,
        "total_amount": total_amount,
        "total_records": len(filtered),
    }


@app.route("/")
def index() -> str:
    bills = fetch_bills(limit=12)
    return render_template("index.html", bills=bills, bill=blank_bill())


@app.route("/bills-table")
def bills_table() -> str:
    bills = fetch_bills()
    library_data = build_library_data(
        bills,
        search=request.args.get("search", ""),
        selected_type=request.args.get("bill_type", ""),
        selected_status=request.args.get("review_status", ""),
    )
    return render_template("bills_table.html", bills=library_data["bills"], library=library_data)


@app.route("/dashboard")
def dashboard() -> str:
    bills = fetch_bills()
    selected_year = request.args.get("year", "").strip()
    dashboard_data = build_dashboard_data(bills, selected_year=selected_year)
    return render_template("dashboard.html", bills=bills, dashboard=dashboard_data)


@app.post("/draft")
def create_draft():
    bills = fetch_bills(limit=12)
    upload = request.files.get("bill_file")
    existing_file_path = request.form.get("file_path", "").strip()
    original_filename = request.form.get("original_filename", "").strip()
    content_type = request.form.get("content_type", "").strip()

    try:
        if upload and upload.filename:
            stored_name, original_filename = save_upload(upload)
            content_type = upload.content_type or ""
        elif existing_file_path and original_filename:
            stored_name = existing_file_path
        else:
            raise ValueError("Choose a bill PDF or image before importing.")

        extracted_text = ""
        if original_filename.lower().endswith(".pdf"):
            extracted_text = extract_pdf_text(stored_name)
            if not extracted_text:
                extracted_text = extract_scanned_pdf_text(stored_name)
                if extracted_text:
                    flash("The PDF is scanned, so OCR was used to draft the document fields. Please review them carefully.")
                else:
                    flash("No readable text was found in the PDF, even after OCR.")
        elif not request.form.get("raw_extracted_text", "").strip():
            extracted_text = extract_image_text(stored_name)
            if extracted_text:
                flash("Image OCR drafted the document fields. Please review them carefully before saving.")
            else:
                flash("No readable text was found in the uploaded image.")

        combined_text = "\n".join(
            piece for piece in [extracted_text, request.form.get("raw_extracted_text", "").strip()] if piece
        )
        form_data = request.form.to_dict()
        form_data["filename_hint"] = original_filename
        form_data["raw_extracted_text"] = combined_text
        bill = collect_bill_form(form_data)
    except ValueError as exc:
        flash(str(exc))
        return render_template("index.html", bills=bills, bill=blank_bill())

    bill["file_path"] = stored_name
    bill["original_filename"] = original_filename
    bill["content_type"] = content_type

    if extracted_text and original_filename.lower().endswith(".pdf"):
        flash("Imported the PDF and drafted the document fields. Review them and then click Save record.")

    return render_template("index.html", bills=bills, bill=bill)


@app.post("/bills")
def create_bill():
    bills = fetch_bills(limit=12)
    upload = request.files.get("bill_file")
    existing_file_path = request.form.get("file_path", "").strip()
    original_filename = request.form.get("original_filename", "").strip()
    content_type = request.form.get("content_type", "").strip()
    stored_name = ""
    original_name = ""

    try:
        if upload and upload.filename:
            stored_name, original_name = save_upload(upload)
            content_type = upload.content_type or ""
        elif existing_file_path and original_filename:
            stored_name, original_name = existing_file_path, original_filename
        else:
            stored_name, original_name, content_type = "", "", ""

        form_data = request.form.to_dict()
        form_data["filename_hint"] = original_name
        bill = collect_bill_form(form_data)
        if not bill_has_content(bill):
            raise ValueError("Enter at least one bill detail or upload a file before saving.")
    except ValueError as exc:
        flash(str(exc))
        return render_template("index.html", bills=bills, bill=bill_preview_from_form(request.form))

    now = datetime.utcnow().isoformat(timespec="seconds")
    inserted_id = db_insert_bill(
        (
            bill["provider"],
            bill["bill_type"],
            bill["consumer_name"],
            bill["service_number"],
            bill["area_code"],
            bill["mobile_number"],
            bill["address"],
            bill["bill_date"],
            bill["billing_month"],
            bill["due_date"],
            bill["last_paid_date"],
            bill["units_consumed"],
            bill["net_amount"],
            bill["notes"],
            bill["raw_extracted_text"],
            stored_name,
            original_name,
            content_type,
            bill["review_status"],
            now,
            now,
        )
    )
    flash("Record saved. You can review or edit it anytime.")
    return redirect(url_for("bill_detail", bill_id=inserted_id))


@app.route("/bills/<int:bill_id>", methods=["GET", "POST"])
def bill_detail(bill_id: int):
    bill = get_bill_or_404(bill_id)
    if request.method == "POST":
        try:
            updated = collect_bill_form(request.form)
        except ValueError as exc:
            flash(str(exc))
            return render_template("detail.html", bill=bill)

        now = datetime.utcnow().isoformat(timespec="seconds")
        db_execute(
            """
            UPDATE bills
            SET provider = ?, bill_type = ?, consumer_name = ?, service_number = ?, area_code = ?,
                mobile_number = ?, address = ?, bill_date = ?, billing_month = ?, due_date = ?,
                last_paid_date = ?, units_consumed = ?, net_amount = ?, notes = ?, raw_extracted_text = ?,
                review_status = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                updated["provider"],
                updated["bill_type"],
                updated["consumer_name"],
                updated["service_number"],
                updated["area_code"],
                updated["mobile_number"],
                updated["address"],
                updated["bill_date"],
                updated["billing_month"],
                updated["due_date"],
                updated["last_paid_date"],
                updated["units_consumed"],
                updated["net_amount"],
                updated["notes"],
                updated["raw_extracted_text"],
                updated["review_status"],
                now,
                bill_id,
            ),
            commit=True,
        )
        flash("Bill details updated.")
        return redirect(url_for("bill_detail", bill_id=bill_id))

    return render_template("detail.html", bill=bill)


@app.post("/bills/<int:bill_id>/delete")
def delete_bill(bill_id: int):
    bill = delete_bill_record(bill_id)
    flash(f"Deleted record #{bill_id}.")
    return redirect(
        url_for(
            "bills_table",
            search=request.form.get("search", "").strip(),
            bill_type=request.form.get("bill_type", "").strip(),
            review_status=request.form.get("review_status", "").strip(),
        )
    )


@app.route("/uploads/<path:filename>")
def uploaded_file(filename: str):
    return send_from_directory(UPLOAD_DIR, filename)


ensure_directories()
init_db()


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    app.run(host=host, port=port, debug=True)
