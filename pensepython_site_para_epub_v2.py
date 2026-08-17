#!/usr/bin/env python3
"""
Baixa todos os capítulos do site PensePython2e e gera um único EPUB reflowable.

Correção v2:
- o Sumário do site é dividido em três listas HTML separadas:
  Prefácio, capítulos 1–19, e apêndices/colofão.
- esta versão percorre todas as listas até o próximo título,
  em vez de ler apenas a primeira.

Uso:
    python pensepython_site_para_epub_v2.py \
      https://penseallen.github.io/PensePython2e/ \
      -o Pense_em_Python_2e.epub

Dependências:
    pip install requests beautifulsoup4 lxml pillow
"""

from __future__ import annotations

import argparse
import html
import mimetypes
import re
import tempfile
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from PIL import Image

UA = "Mozilla/5.0 (compatible; PensePythonEPUB/2.0)"
TIMEOUT = 45


@dataclass
class PageItem:
    title: str
    url: str
    filename: str
    body_html: str = ""


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def slugify(s: str) -> str:
    s = nfc(re.sub(r"\s+", " ", s).strip())
    s = re.sub(r"[^A-Za-z0-9À-ÿ_-]+", "-", s).strip("-").lower()
    return s[:60] or "capitulo"


def fetch(url: str):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    return r


def soup_utf8(url: str):
    return BeautifulSoup(fetch(url).content, "lxml", from_encoding="utf-8")


def discover_toc(base_url: str):
    soup = soup_utf8(base_url)

    heading = next(
        (
            h
            for h in soup.find_all(["h1", "h2", "h3"])
            if "sumário" in nfc(h.get_text(" ", strip=True)).casefold()
        ),
        None,
    )
    if heading is None:
        raise RuntimeError("Seção 'Sumário' não encontrada.")

    base = urlparse(base_url)
    base_dir = base.path.rstrip("/") + "/"
    items = []
    seen = set()

    # O sumário real está dividido em mais de uma lista:
    # UL (Prefácio), OL (capítulos), UL (apêndices/colofão).
    node = heading.find_next_sibling()

    while node is not None:
        if isinstance(node, Tag) and node.name in {"h1", "h2", "h3"}:
            break

        if isinstance(node, Tag):
            lists = [node] if node.name in {"ul", "ol"} else node.find_all(["ul", "ol"])

            for toc in lists:
                for a in toc.find_all("a", href=True):
                    title = nfc(a.get_text(" ", strip=True))
                    url, _ = urldefrag(urljoin(base_url, a["href"]))
                    parsed = urlparse(url)

                    if parsed.netloc != base.netloc:
                        continue
                    if not parsed.path.startswith(base_dir):
                        continue
                    if not parsed.path.lower().endswith(".html"):
                        continue
                    if url in seen:
                        continue

                    seen.add(url)
                    i = len(items) + 1
                    items.append(
                        PageItem(
                            title or f"Capítulo {i}",
                            url,
                            f"capitulo-{i:02d}-{slugify(title)}.xhtml",
                        )
                    )

        node = node.find_next_sibling()

    if not items:
        raise RuntimeError("Nenhum capítulo encontrado no Sumário.")

    print(f"Links encontrados no Sumário: {len(items)}")
    return items


def choose_root(soup):
    for sel in ("main", "article", ".markdown-body", ".container-lg", "#content", "body"):
        node = soup.select_one(sel)
        if isinstance(node, Tag):
            return node
    raise RuntimeError("Conteúdo principal não localizado.")


def clean_root(root):
    for sel in (
        "script",
        "style",
        "noscript",
        "iframe",
        "nav",
        "header",
        "footer",
        ".page-header",
        ".site-header",
        ".site-footer",
    ):
        for node in list(root.select(sel)):
            node.decompose()

    for h in list(root.find_all(["h1", "h2"])):
        txt = nfc(h.get_text(" ", strip=True)).casefold()
        if txt == "pensepython2e" or txt.startswith("tradução do livro pense em python"):
            h.decompose()

    for a in list(root.find_all("a")):
        if "view on github" in nfc(a.get_text(" ", strip=True)).casefold():
            a.decompose()


def normalize_text(root):
    for node in root.find_all(string=True):
        fixed = nfc(str(node))
        if fixed != str(node):
            node.replace_with(fixed)


def localize_images(root, page_url, images_dir, cache, manifest, counter):
    for img in root.find_all("img", src=True):
        abs_url = urljoin(page_url, img["src"])

        if abs_url in cache:
            filename, mime = cache[abs_url]
        else:
            try:
                r = fetch(abs_url)
            except Exception:
                img["src"] = abs_url
                continue

            counter[0] += 1
            suffix = Path(urlparse(abs_url).path).suffix.lower()
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()

            if not suffix:
                suffix = mimetypes.guess_extension(ctype) or ".jpg"
            if suffix not in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"}:
                suffix = ".jpg"

            mime = mimetypes.types_map.get(suffix, "image/jpeg")
            if suffix == ".svg":
                mime = "image/svg+xml"

            raw = r.content

            if mime in {"image/webp", "image/gif"}:
                try:
                    im = Image.open(BytesIO(raw)).convert("RGBA")
                    out = BytesIO()
                    im.save(out, "PNG")
                    raw = out.getvalue()
                    suffix = ".png"
                    mime = "image/png"
                except Exception:
                    pass

            filename = f"imagem_{counter[0]:04d}{suffix}"
            (images_dir / filename).write_bytes(raw)
            cache[abs_url] = (filename, mime)
            manifest[filename] = mime

        img["src"] = f"images/{filename}"
        img.attrs.pop("width", None)
        img.attrs.pop("height", None)
        img.attrs.pop("style", None)


def rewrite_links(root, page_url, url_to_file):
    for a in root.find_all("a", href=True):
        href = a["href"]
        absolute = urljoin(page_url, href)
        base, frag = urldefrag(absolute)

        if base in url_to_file:
            a["href"] = url_to_file[base] + (f"#{frag}" if frag else "")
        elif href.startswith("#"):
            a["href"] = href
        else:
            a["href"] = absolute


def trim_to_book_content(root):
    first_book_h1 = None

    for h1 in root.find_all("h1"):
        txt = nfc(h1.get_text(" ", strip=True))
        if txt and txt.casefold() != "pensepython2e":
            first_book_h1 = h1
            break

    if first_book_h1 is None:
        return root

    tmp = BeautifulSoup("<div></div>", "lxml")
    new_root = tmp.div

    node = first_book_h1
    while node is not None:
        nxt = node.next_sibling
        if isinstance(node, (Tag, NavigableString)):
            new_root.append(node.extract())
        node = nxt

    return new_root


def page_body(item, url_to_file, images_dir, cache, manifest, counter):
    soup = soup_utf8(item.url)
    root = choose_root(soup)
    clean_root(root)
    normalize_text(root)
    root = trim_to_book_content(root)

    localize_images(root, item.url, images_dir, cache, manifest, counter)
    rewrite_links(root, item.url, url_to_file)

    for node in root.find_all(True):
        for attr in ("style", "width", "height", "bgcolor", "align"):
            node.attrs.pop(attr, None)

    return nfc("".join(str(x) for x in root.contents))


def xhtml(title, body):
    return nfc(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" lang="pt-BR">
<head>
<meta charset="UTF-8"/>
<title>{html.escape(title)}</title>
<link rel="stylesheet" type="text/css" href="styles/book.css"/>
</head>
<body>
{body}
</body>
</html>
"""
    )


def build_epub(base_url, output, title, author, translator):
    pages = discover_toc(base_url)
    url_to_file = {p.url: p.filename for p in pages}

    with tempfile.TemporaryDirectory(prefix="pensepython_epub_") as tmp:
        root = Path(tmp)
        meta = root / "META-INF"
        oebps = root / "OEBPS"
        images = oebps / "images"
        styles = oebps / "styles"

        meta.mkdir(parents=True)
        images.mkdir(parents=True)
        styles.mkdir(parents=True)

        cache, manifest, counter = {}, {}, [0]

        for i, p in enumerate(pages, 1):
            print(f"[{i:02d}/{len(pages):02d}] {p.title}")
            p.body_html = page_body(p, url_to_file, images, cache, manifest, counter)

        (root / "mimetype").write_text("application/epub+zip", encoding="ascii")

        (meta / "container.xml").write_text(
            """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>""",
            encoding="utf-8",
        )

        css = """body{font-family:serif;line-height:1.5;margin:5%}
h1{font-size:1.7em;break-before:page;page-break-before:always;margin-top:0}
h2{font-size:1.35em;margin-top:1.5em}
h3{font-size:1.16em;margin-top:1.25em}
p{margin:0 0 .9em;text-align:justify;orphans:2;widows:2}
ul,ol{margin-top:.4em;margin-bottom:.9em}
li{margin-bottom:.3em}
pre{white-space:pre-wrap;word-wrap:break-word;font-family:monospace;font-size:.86em;line-height:1.35;padding:.7em;border:1px solid #888;overflow-wrap:anywhere}
code,kbd,samp{font-family:monospace}
img{max-width:100%;height:auto}
figure{margin:1.1em auto;text-align:center;page-break-inside:avoid}
table{border-collapse:collapse;width:100%;font-size:.9em}
th,td{border:1px solid #888;padding:.35em;vertical-align:top}
blockquote{margin:1em 1.2em;padding-left:.8em;border-left:.2em solid #888}
a{word-break:break-word}
.title-page{text-align:center;margin-top:25%}
.credits{margin-top:3em;font-size:.92em}
"""
        (styles / "book.css").write_text(css, encoding="utf-8")

        title_body = f"""<section class="title-page">
<h1>{html.escape(title)}</h1>
<p>{html.escape(author)}</p>
<p>Tradução: {html.escape(translator)}</p>
</section>
<section class="credits">
<h2>Fonte e licença</h2>
<p>Fonte digital: <a href="{html.escape(base_url)}">{html.escape(base_url)}</a></p>
<p>Licença informada na página original: CC BY-NC 3.0.</p>
<p>Esta edição apenas reorganiza o conteúdo em formato EPUB reflowable.</p>
</section>"""

        (oebps / "title.xhtml").write_text(xhtml(title, title_body), encoding="utf-8")

        for p in pages:
            (oebps / p.filename).write_text(xhtml(p.title, p.body_html), encoding="utf-8")

        nav_items = "\n".join(
            f'<li><a href="{html.escape(p.filename)}">{html.escape(p.title)}</a></li>'
            for p in pages
        )
        nav_body = f"""<nav epub:type="toc" id="toc" xmlns:epub="http://www.idpf.org/2007/ops">
<h1>Sumário</h1><ol>{nav_items}</ol></nav>"""

        (oebps / "nav.xhtml").write_text(xhtml("Sumário", nav_body), encoding="utf-8")

        ident = f"urn:uuid:{uuid.uuid4()}"

        mani = [
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
            '<item id="css" href="styles/book.css" media-type="text/css"/>',
            '<item id="title" href="title.xhtml" media-type="application/xhtml+xml"/>',
        ]
        spine = [
            '<itemref idref="title"/>',
            '<itemref idref="nav" linear="no"/>',
        ]

        for i, p in enumerate(pages, 1):
            mani.append(
                f'<item id="chap{i}" href="{p.filename}" media-type="application/xhtml+xml"/>'
            )
            spine.append(f'<itemref idref="chap{i}"/>')

        for i, (fn, mime) in enumerate(sorted(manifest.items()), 1):
            mani.append(
                f'<item id="img{i}" href="images/{fn}" media-type="{mime}"/>'
            )

        opf = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id" xml:lang="pt-BR">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="book-id">{ident}</dc:identifier>
<dc:title>{html.escape(title)}</dc:title>
<dc:creator>{html.escape(author)}</dc:creator>
<dc:language>pt-BR</dc:language>
<dc:rights>CC BY-NC 3.0 — conforme informado na fonte digital.</dc:rights>
<meta property="dcterms:modified">2026-08-16T00:00:00Z</meta>
</metadata>
<manifest>{''.join(mani)}</manifest>
<spine>{''.join(spine)}</spine>
</package>"""
        (oebps / "content.opf").write_text(opf, encoding="utf-8")

        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            output.unlink()

        with zipfile.ZipFile(output, "w") as epub:
            epub.write(
                root / "mimetype",
                "mimetype",
                compress_type=zipfile.ZIP_STORED,
            )
            for path in sorted(root.rglob("*")):
                if path.is_file() and path.name != "mimetype":
                    epub.write(
                        path,
                        path.relative_to(root).as_posix(),
                        compress_type=zipfile.ZIP_DEFLATED,
                    )

    print()
    print(f"EPUB criado: {output}")
    print(f"Capítulos/apêndices: {len(pages)}")
    print(f"Imagens: {len(manifest)}")


def parse_args():
    p = argparse.ArgumentParser(
        description="Converte todo o site PensePython2e em um EPUB reflowable."
    )
    p.add_argument(
        "url",
        nargs="?",
        default="https://penseallen.github.io/PensePython2e/",
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("Pense_em_Python_2e.epub"),
    )
    p.add_argument("--title", default="Pense em Python — 2ª edição")
    p.add_argument("--author", default="Allen B. Downey")
    p.add_argument("--translator", default="Sheila Gomes")
    return p.parse_args()


def main():
    a = parse_args()
    build_epub(a.url, a.output, a.title, a.author, a.translator)


if __name__ == "__main__":
    main()
