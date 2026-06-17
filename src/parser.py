import re
from pathlib import Path
import pypdf
import docx
import openpyxl

def clean_whitespace(text: str) -> str:
    """Replaces multiple whitespaces and newlines with single instances to clean text."""
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n+', '\n', text)
    return text.strip()

def extract_text_from_txt(filepath: Path) -> str:
    """Reads a text or markdown file, trying different encodings.

    Order matters: utf-8-sig handles UTF-8 with or without a BOM, utf-16 is
    tried (BOM-aware) before latin1, and latin1 is kept LAST as the catch-all.
    latin1 never raises UnicodeDecodeError, so anything placed after it would be
    unreachable and real UTF-16 files would be silently mangled.
    """
    encodings = ['utf-8-sig', 'utf-16', 'cp949', 'euc-kr', 'latin1']
    for encoding in encodings:
        try:
            with open(filepath, 'r', encoding=encoding) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Could not decode text file {filepath} with supported encodings.")

def extract_text_from_pdf(filepath: Path) -> str:
    """Extracts text content from a PDF file."""
    texts = []
    try:
        with open(filepath, 'rb') as f:
            reader = pypdf.PdfReader(f)
            for page in reader.pages:
                try:
                    text = page.extract_text()
                    if text:
                        texts.append(text)
                except Exception as e:
                    print(f"Warning: Failed to extract page text in PDF {filepath}: {e}")
                    continue
    except Exception as e:
        print(f"Error opening PDF {filepath}: {e}")
    return "\n".join(texts)

def extract_text_from_docx(filepath: Path) -> str:
    """Extracts text content from paragraphs and tables in a Word document."""
    doc = docx.Document(str(filepath))
    texts = []
    
    # Extract paragraphs
    for para in doc.paragraphs:
        if para.text.strip():
            texts.append(para.text)
            
    # Extract tables
    for table in doc.tables:
        for row in table.rows:
            row_texts = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if row_texts:
                texts.append(" | ".join(row_texts))
                
    return "\n".join(texts)

def extract_text_from_xlsx(filepath: Path) -> str:
    """Extracts text content from all sheets in an Excel spreadsheet."""
    wb = openpyxl.load_workbook(str(filepath), data_only=True, read_only=True)
    try:
        texts = []
        for sheet in wb.worksheets:
            texts.append(f"Sheet: {sheet.title}")
            for row in sheet.iter_rows(values_only=True):
                row_text = " ".join([str(val).strip() for val in row if val is not None])
                if row_text.strip():
                    texts.append(row_text)
        return "\n".join(texts)
    finally:
        wb.close()

def extract_text(filepath_str: str) -> str:
    """Dispatches text extraction based on file extension."""
    filepath = Path(filepath_str)
    ext = filepath.suffix.lower()
    
    if ext in {'.txt', '.md'}:
        return extract_text_from_txt(filepath)
    elif ext == '.pdf':
        return extract_text_from_pdf(filepath)
    elif ext == '.docx':
        return extract_text_from_docx(filepath)
    elif ext == '.xlsx':
        return extract_text_from_xlsx(filepath)
    else:
        raise ValueError(f"Unsupported file format: {ext}")

def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """Splits cleaned text into chunks of specified size and overlap."""
    text = clean_whitespace(text)
    if not text:
        return []

    # Guard against a misconfiguration where overlap >= chunk_size, which would
    # make the step non-positive and loop forever.
    step = chunk_size - overlap
    if step <= 0:
        step = chunk_size

    chunks = []
    start = 0
    text_len = len(text)

    while start < text_len:
        end = start + chunk_size
        chunk = text[start:end]
        chunks.append(chunk)

        if end >= text_len:
            break

        start += step

    return chunks
