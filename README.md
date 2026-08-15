# PDF2Kindle EPUB

Conversor em Python de PDF para EPUB reflowable, otimizado para leitura em Kindle.

## Recursos

- Extração de texto em ordem de leitura.
- Detecção automática de títulos e capítulos.
- Remoção de cabeçalhos, rodapés e números de página repetidos.
- Preservação de imagens relevantes.
- Geração de sumário navegável.
- CSS responsivo para leitores EPUB.
- OCR opcional para páginas sem texto utilizável.

## Requisitos

- Python 3.10 ou superior
- PyMuPDF
- Pillow
- pytesseract, caso utilize OCR

## Instale as dependências com:

bash
pip install -r requirements_pdf_para_epub.txt

# Uso

python pdf_para_epub_profissional.py entrada.pdf -o saida.epub

## Informando título e autor

python pdf_para_epub_profissional.py entrada.pdf \

  -o saida.epub \
  --title "Meu Livro" \
  --author "Nome do Autor"

## Usando OCR

python pdf_para_epub_profissional.py entrada.pdf \
  -o saida.epub \
  --ocr

# Para OCR, também é necessário instalar o Tesseract OCR no sistema operacional.

## Exemplo de fluxo

```text
PDF
 ↓
Extração e limpeza
 ↓
Detecção de títulos
 ↓
Reconstrução de parágrafos
 ↓
Extração de imagens
 ↓
Criação dos capítulos
 ↓
Sumário navegável
 ↓
EPUB reflowable

