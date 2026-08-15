#!/usr/bin/env python3
"""
Converte PDF em EPUB reflowable (compatível com Kindle).

Recursos:
- Extrai texto em ordem de leitura.
- Detecta títulos por tamanho/estilo da fonte.
- Remove cabeçalhos, rodapés e números de página repetidos.
- Preserva imagens relevantes e suas posições aproximadas.
- Gera capítulos, sumário navegável e CSS responsivo.
- Usa OCR opcional apenas quando a página não possui texto utilizável.

Uso:
    python pdf_para_epub_profissional.py entrada.pdf -o saida.epub

Com OCR opcional:
    python pdf_para_epub_profissional.py entrada.pdf -o saida.epub --ocr

Dependências:
    pip install pymupdf pillow

OCR opcional:
    pip install pytesseract
    Também é necessário instalar o Tesseract no sistema operacional.
"""

from __future__ import annotations

import argparse
import collections
import html
import io
import os
import re
import shutil
import statistics
import tempfile
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import fitz  # PyMuPDF
from PIL import Image


@dataclass
class Element:
    kind: str  # h1, h2, h3, p, image, pagebreak
    text: str = ""
    image_name: str = ""
    alt: str = "Figura"


@dataclass
class Chapter:
    title: str
    elements: list[Element] = field(default_factory=list)


@dataclass
class Settings:
    min_image_width: int = 260
    min_image_height: int = 160
    jpeg_quality: int = 88
    max_image_width: int = 1600
    min_text_chars_for_no_ocr: int = 35
    header_zone: float = 0.10
    footer_zone: float = 0.10
    repeated_threshold_ratio: float = 0.35


def normalize_space(text: str) -> str:
    text = text.replace("\u00ad", "")
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    return text.strip()


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return value or "capitulo"


def is_page_number(text: str) -> bool:
    t = normalize_space(text)
    return bool(re.fullmatch(r"(?:p[aá]gina\s*)?\d{1,4}", t, flags=re.I))


def is_noise(text: str) -> bool:
    t = normalize_space(text)
    if not t:
        return True
    if is_page_number(t):
        return True
    if len(t) == 1 and not t.isalnum():
        return True
    return False


def extract_line_text(line: dict) -> str:
    return normalize_space("".join(span.get("text", "") for span in line.get("spans", [])))


def collect_repeated_marginal_lines(doc: fitz.Document, settings: Settings) -> set[str]:
    counts: collections.Counter[str] = collections.Counter()
    total_pages = max(1, len(doc))

    for page in doc:
        h = page.rect.height
        page_seen: set[str] = set()
        data = page.get_text("dict", flags=fitz.TEXTFLAGS_TEXT)
        for block in data.get("blocks", []):
            if block.get("type") != 0:
                continue
            y0, y1 = block["bbox"][1], block["bbox"][3]
            if not (y1 <= h * settings.header_zone or y0 >= h * (1 - settings.footer_zone)):
                continue
            for line in block.get("lines", []):
                text = extract_line_text(line)
                key = re.sub(r"\d+", "#", text.casefold())
                if text and len(text) <= 140:
                    page_seen.add(key)
        counts.update(page_seen)

    threshold = max(2, round(total_pages * settings.repeated_threshold_ratio))
    return {key for key, count in counts.items() if count >= threshold}


def font_statistics(doc: fitz.Document, repeated: set[str]) -> tuple[float, float, float]:
    sizes: list[float] = []
    bold_sizes: list[float] = []
    for page in doc:
        data = page.get_text("dict", flags=fitz.TEXTFLAGS_TEXT)
        for block in data.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                text = extract_line_text(line)
                key = re.sub(r"\d+", "#", text.casefold())
                if is_noise(text) or key in repeated:
                    continue
                for span in line.get("spans", []):
                    txt = normalize_space(span.get("text", ""))
                    if not txt:
                        continue
                    size = float(span.get("size", 10.0))
                    sizes.extend([size] * min(len(txt), 30))
                    font = span.get("font", "").casefold()
                    if "bold" in font or "black" in font or "semibold" in font:
                        bold_sizes.append(size)
    body = statistics.median(sizes) if sizes else 11.0
    h2 = max(body * 1.25, statistics.quantiles(sizes, n=10)[-2] if len(sizes) >= 10 else body * 1.35)
    h1 = max(body * 1.55, max(bold_sizes, default=body * 1.7))
    return body, h2, h1


def classify_line(line: dict, body: float, h2_threshold: float, h1_threshold: float) -> tuple[str, str]:
    text = extract_line_text(line)
    spans = line.get("spans", [])
    if not text or not spans:
        return "p", text

    max_size = max(float(s.get("size", body)) for s in spans)
    fonts = " ".join(str(s.get("font", "")) for s in spans).casefold()
    bold = any(word in fonts for word in ("bold", "black", "semibold", "demi"))
    short = len(text) <= 110
    numbered = bool(re.match(r"^(?:cap[ií]tulo\s+)?\d+(?:\.\d+){0,3}[\s.:-]", text, re.I))
    all_caps = text.isupper() and len(text) > 3

    if short and (max_size >= h1_threshold or (bold and max_size >= body * 1.55)):
        return "h1", text
    if short and (max_size >= h2_threshold or (bold and numbered)):
        return "h2", text
    if short and bold and (max_size >= body * 1.08 or all_caps):
        return "h3", text
    return "p", text


def join_paragraphs(lines: list[tuple[str, str]]) -> list[Element]:
    result: list[Element] = []
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        text = " ".join(buffer)
        text = re.sub(r"(\w)-\s+(\w)", r"\1\2", text)
        text = normalize_space(text)
        if text:
            result.append(Element("p", text=text))
        buffer.clear()

    for kind, text in lines:
        if not text:
            flush()
            continue
        if kind.startswith("h"):
            flush()
            result.append(Element(kind, text=text))
            continue

        if buffer:
            previous = buffer[-1]
            starts_new = bool(re.match(r"^(?:[•▪◦‣*-]|\d+[.)]|[a-z][.)])\s+", text, re.I))
            previous_ends = bool(re.search(r"[.!?:;]$", previous))
            if starts_new or (previous_ends and len(previous) < 80):
                flush()
        buffer.append(text)
    flush()
    return result


def ocr_page(page: fitz.Page) -> str:
    try:
        import pytesseract
    except ImportError as exc:
        raise RuntimeError("OCR solicitado, mas pytesseract não está instalado.") from exc
    pix = page.get_pixmap(matrix=fitz.Matrix(2.2, 2.2), alpha=False)
    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    try:
        return pytesseract.image_to_string(image, lang="por")
    except Exception:
        return pytesseract.image_to_string(image)


def save_image(raw: bytes, ext: str, output_dir: Path, name: str, settings: Settings) -> Optional[str]:
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception:
        return None

    if image.width < settings.min_image_width or image.height < settings.min_image_height:
        return None

    if image.mode not in ("RGB", "L"):
        background = Image.new("RGB", image.size, "white")
        if image.mode == "RGBA":
            background.paste(image, mask=image.getchannel("A"))
        else:
            background.paste(image.convert("RGB"))
        image = background
    else:
        image = image.convert("RGB")

    if image.width > settings.max_image_width:
        ratio = settings.max_image_width / image.width
        image = image.resize((settings.max_image_width, round(image.height * ratio)), Image.Resampling.LANCZOS)

    filename = f"{name}.jpg"
    image.save(output_dir / filename, "JPEG", quality=settings.jpeg_quality, optimize=True)
    return filename


def extract_elements(doc: fitz.Document, work_images: Path, settings: Settings, use_ocr: bool) -> list[Element]:
    repeated = collect_repeated_marginal_lines(doc, settings)
    body, h2_threshold, h1_threshold = font_statistics(doc, repeated)
    all_elements: list[Element] = []
    image_counter = 0

    for page_index, page in enumerate(doc, start=1):
        plain_text = normalize_space(page.get_text("text"))
        if use_ocr and len(plain_text) < settings.min_text_chars_for_no_ocr:
            text = ocr_page(page)
            lines = [("p", normalize_space(line)) for line in text.splitlines() if normalize_space(line)]
            all_elements.extend(join_paragraphs(lines))
            all_elements.append(Element("pagebreak"))
            continue

        data = page.get_text("dict", flags=fitz.TEXTFLAGS_TEXT | fitz.TEXT_PRESERVE_IMAGES)
        blocks = sorted(data.get("blocks", []), key=lambda b: (round(b.get("bbox", [0, 0])[1], 1), round(b.get("bbox", [0, 0])[0], 1)))

        text_lines: list[tuple[str, str]] = []
        for block in blocks:
            if block.get("type") == 0:
                block_lines: list[tuple[str, str]] = []
                for line in block.get("lines", []):
                    text = extract_line_text(line)
                    key = re.sub(r"\d+", "#", text.casefold())
                    if is_noise(text) or key in repeated:
                        continue
                    block_lines.append(classify_line(line, body, h2_threshold, h1_threshold))
                text_lines.extend(block_lines)
                text_lines.append(("p", ""))

            elif block.get("type") == 1 and block.get("image"):
                all_elements.extend(join_paragraphs(text_lines))
                text_lines.clear()
                image_counter += 1
                ext = block.get("ext", "png")
                filename = save_image(block["image"], ext, work_images, f"figura_{image_counter:03d}", settings)
                if filename:
                    all_elements.append(Element("image", image_name=filename, alt=f"Figura da página {page_index}"))

        all_elements.extend(join_paragraphs(text_lines))
        all_elements.append(Element("pagebreak"))

    return all_elements


def choose_title(doc: fitz.Document, elements: list[Element], source: Path) -> str:
    metadata_title = normalize_space(doc.metadata.get("title", "") if doc.metadata else "")
    if metadata_title and metadata_title.lower() not in {"untitled", "sem título"}:
        return metadata_title
    for el in elements:
        if el.kind == "h1" and 4 <= len(el.text) <= 160:
            return el.text
    return source.stem.replace("_", " ")


def split_chapters(elements: list[Element], book_title: str) -> list[Chapter]:
    chapters: list[Chapter] = []
    current = Chapter("Apresentação")

    for el in elements:
        if el.kind == "h1":
            if current.elements:
                chapters.append(current)
            current = Chapter(el.text)
            current.elements.append(el)
        else:
            current.elements.append(el)
    if current.elements:
        chapters.append(current)

    # Evita capítulos excessivamente curtos criados por falsos positivos.
    merged: list[Chapter] = []
    for chapter in chapters:
        text_length = sum(len(e.text) for e in chapter.elements)
        if merged and text_length < 180:
            merged[-1].elements.extend(chapter.elements)
        else:
            merged.append(chapter)

    if not merged:
        merged = [Chapter(book_title, elements)]
    return merged


def element_to_html(el: Element) -> str:
    if el.kind in {"h1", "h2", "h3"}:
        return f"<{el.kind}>{html.escape(el.text)}</{el.kind}>"
    if el.kind == "p":
        text = html.escape(el.text)
        # Converte marcadores simples em parágrafos visualmente adequados.
        if re.match(r"^[•▪◦‣*-]\s+", el.text):
            text = re.sub(r"^[•▪◦‣*-]\s+", "", text)
            return f'<p class="bullet">{text}</p>'
        return f"<p>{text}</p>"
    if el.kind == "image":
        return f'<figure><img src="images/{html.escape(el.image_name)}" alt="{html.escape(el.alt)}"/></figure>'
    if el.kind == "pagebreak":
        return '<div class="source-page-break" aria-hidden="true"></div>'
    return ""


def make_epub(source: Path, output: Path, title: Optional[str], author: str, use_ocr: bool, settings: Settings) -> None:
    with tempfile.TemporaryDirectory(prefix="pdf_epub_") as tmp:
        root = Path(tmp)
        meta_inf = root / "META-INF"
        oebps = root / "OEBPS"
        images_dir = oebps / "images"
        styles_dir = oebps / "styles"
        meta_inf.mkdir()
        images_dir.mkdir(parents=True)
        styles_dir.mkdir()

        doc = fitz.open(source)
        elements = extract_elements(doc, images_dir, settings, use_ocr)
        book_title = title or choose_title(doc, elements, source)
        chapters = split_chapters(elements, book_title)
        identifier = f"urn:uuid:{uuid.uuid4()}"

        (root / "mimetype").write_text("application/epub+zip", encoding="ascii")
        (meta_inf / "container.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            '</rootfiles></container>',
            encoding="utf-8",
        )

        css = """
body { font-family: serif; line-height: 1.48; margin: 5%; }
h1 { page-break-before: always; break-before: page; margin-top: 0; font-size: 1.65em; }
h2 { margin-top: 1.5em; font-size: 1.30em; }
h3 { margin-top: 1.25em; font-size: 1.12em; }
p { margin: 0 0 0.85em 0; text-align: justify; orphans: 2; widows: 2; }
p.bullet { margin-left: 1.2em; text-indent: -0.9em; }
p.bullet::before { content: "• "; }
figure { margin: 1.2em auto; text-align: center; page-break-inside: avoid; }
img { max-width: 100%; height: auto; }
.source-page-break { height: 0; margin: 0; padding: 0; }
nav ol { list-style: none; padding-left: 0; }
nav li { margin: 0.55em 0; }
.title-page { text-align: center; margin-top: 30%; }
.title-page h1 { page-break-before: avoid; break-before: avoid; }
.small { font-size: 0.85em; }
""".strip()
        (styles_dir / "book.css").write_text(css, encoding="utf-8")

        title_xhtml = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" lang="pt-BR">
<head><title>{html.escape(book_title)}</title><link rel="stylesheet" type="text/css" href="styles/book.css"/></head>
<body><section class="title-page"><h1>{html.escape(book_title)}</h1><p>{html.escape(author)}</p><p class="small">Edição digital reflowable</p></section></body></html>'''
        (oebps / "title.xhtml").write_text(title_xhtml, encoding="utf-8")

        chapter_files: list[tuple[str, str]] = []
        used_names: collections.Counter[str] = collections.Counter()
        for index, chapter in enumerate(chapters, start=1):
            base_name = slugify(chapter.title)[:45]
            used_names[base_name] += 1
            suffix = f"-{used_names[base_name]}" if used_names[base_name] > 1 else ""
            filename = f"capitulo-{index:02d}-{base_name}{suffix}.xhtml"
            body = "\n".join(element_to_html(el) for el in chapter.elements)
            xhtml = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" lang="pt-BR">
<head><title>{html.escape(chapter.title)}</title><link rel="stylesheet" type="text/css" href="styles/book.css"/></head>
<body><section>{body}</section></body></html>'''
            (oebps / filename).write_text(xhtml, encoding="utf-8")
            chapter_files.append((filename, chapter.title))

        nav_items = "\n".join(
            f'<li><a href="{html.escape(filename)}">{html.escape(chapter_title)}</a></li>'
            for filename, chapter_title in chapter_files
        )
        nav_xhtml = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="pt-BR">
<head><title>Sumário</title><link rel="stylesheet" type="text/css" href="styles/book.css"/></head>
<body><nav epub:type="toc" id="toc"><h1>Sumário</h1><ol>{nav_items}</ol></nav></body></html>'''
        (oebps / "nav.xhtml").write_text(nav_xhtml, encoding="utf-8")

        nav_points = "\n".join(
            f'<navPoint id="navPoint-{i}" playOrder="{i}"><navLabel><text>{html.escape(chapter_title)}</text></navLabel><content src="{html.escape(filename)}"/></navPoint>'
            for i, (filename, chapter_title) in enumerate(chapter_files, start=1)
        )
        toc_ncx = f'''<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
<head><meta name="dtb:uid" content="{identifier}"/></head>
<docTitle><text>{html.escape(book_title)}</text></docTitle><navMap>{nav_points}</navMap></ncx>'''
        (oebps / "toc.ncx").write_text(toc_ncx, encoding="utf-8")

        manifest = [
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
            '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
            '<item id="css" href="styles/book.css" media-type="text/css"/>',
            '<item id="title" href="title.xhtml" media-type="application/xhtml+xml"/>',
        ]
        spine = ['<itemref idref="title"/>', '<itemref idref="nav" linear="no"/>']
        for i, (filename, _) in enumerate(chapter_files, start=1):
            manifest.append(f'<item id="chap{i}" href="{filename}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="chap{i}"/>')
        for image_path in sorted(images_dir.glob("*")):
            manifest.append(f'<item id="img-{image_path.stem}" href="images/{image_path.name}" media-type="image/jpeg"/>')

        opf = f'''<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id" xml:lang="pt-BR">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="book-id">{identifier}</dc:identifier>
<dc:title>{html.escape(book_title)}</dc:title>
<dc:creator>{html.escape(author)}</dc:creator>
<dc:language>pt-BR</dc:language>
<meta property="dcterms:modified">2026-07-26T00:00:00Z</meta>
</metadata><manifest>{''.join(manifest)}</manifest><spine toc="ncx">{''.join(spine)}</spine></package>'''
        (oebps / "content.opf").write_text(opf, encoding="utf-8")

        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            output.unlink()
        with zipfile.ZipFile(output, "w") as epub:
            epub.write(root / "mimetype", "mimetype", compress_type=zipfile.ZIP_STORED)
            for path in sorted(root.rglob("*")):
                if path.is_file() and path.name != "mimetype":
                    epub.write(path, path.relative_to(root).as_posix(), compress_type=zipfile.ZIP_DEFLATED)

        doc.close()
        print(f"EPUB criado: {output}")
        print(f"Capítulos: {len(chapters)} | Imagens: {len(list(images_dir.glob('*')))}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Converte PDF em EPUB reflowable para Kindle.")
    parser.add_argument("pdf", type=Path, help="Arquivo PDF de entrada")
    parser.add_argument("-o", "--output", type=Path, help="Arquivo EPUB de saída")
    parser.add_argument("--title", help="Título do livro")
    parser.add_argument("--author", default="Autor não informado", help="Autor do livro")
    parser.add_argument("--ocr", action="store_true", help="Usar OCR em páginas sem texto")
    parser.add_argument("--min-image-width", type=int, default=260)
    parser.add_argument("--min-image-height", type=int, default=160)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.pdf.exists():
        raise SystemExit(f"PDF não encontrado: {args.pdf}")
    if args.pdf.suffix.lower() != ".pdf":
        raise SystemExit("O arquivo de entrada precisa ser PDF.")
    output = args.output or args.pdf.with_suffix(".epub")
    settings = Settings(min_image_width=args.min_image_width, min_image_height=args.min_image_height)
    make_epub(args.pdf, output, args.title, args.author, args.ocr, settings)


if __name__ == "__main__":
    main()
