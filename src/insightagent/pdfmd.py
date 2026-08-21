from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional


DEFAULT_INCOMING_DIR = Path("data/kb/incoming")
DEFAULT_MARKDOWN_DIR = Path("data/kb/markdown")
MIN_TEXT_CHARS = 20


class PdfMdError(RuntimeError):
    pass


@dataclass(frozen=True)
class ConversionResult:
    source_path: Path
    output_path: Path
    source_sha256: str
    page_count: int
    status: str
    skipped: bool = False
    error: Optional[str] = None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def output_path_for(source: Path, output_dir: Path) -> Path:
    return output_dir / (source.stem + ".md")


def convert_pdf(
    source: Path,
    output: Path,
    *,
    force: bool = False,
) -> ConversionResult:
    source = source.expanduser().resolve()
    output = output.expanduser().resolve()
    if source.suffix.lower() != ".pdf":
        raise PdfMdError("input is not a PDF: {}".format(source))
    if not source.is_file():
        raise PdfMdError("PDF does not exist: {}".format(source))

    source_sha256 = sha256_file(source)
    if not force and output.is_file() and _markdown_has_hash(output, source_sha256):
        return ConversionResult(
            source_path=source,
            output_path=output,
            source_sha256=source_sha256,
            page_count=_markdown_page_count(output),
            status=_markdown_status(output),
            skipped=True,
        )

    fitz = _load_fitz()
    try:
        document = fitz.open(str(source))
    except Exception as error:
        raise PdfMdError("cannot open PDF {}: {}".format(source, error)) from error

    try:
        page_texts = [clean_page_text(page.get_text("text")) for page in document]
        page_count = len(page_texts)
        usable_chars = sum(len(text) for text in page_texts)
        if page_count == 0:
            status = "empty_text"
        elif usable_chars < MIN_TEXT_CHARS:
            status = "needs_ocr"
        else:
            status = "ok"
        markdown = render_markdown(
            source=source,
            source_sha256=source_sha256,
            page_texts=page_texts,
            status=status,
        )
    finally:
        document.close()

    _atomic_write(output, markdown)
    return ConversionResult(
        source_path=source,
        output_path=output,
        source_sha256=source_sha256,
        page_count=page_count,
        status=status,
    )


def convert_path(
    input_path: Path,
    *,
    output_dir: Optional[Path] = None,
    force: bool = False,
) -> List[ConversionResult]:
    input_path = input_path.expanduser()
    output_dir = (output_dir or DEFAULT_MARKDOWN_DIR).expanduser()
    if input_path.is_file():
        output = output_dir if output_dir.suffix.lower() == ".md" else output_path_for(input_path, output_dir)
        return [convert_pdf(input_path, output, force=force)]
    if not input_path.is_dir():
        raise PdfMdError("input path does not exist: {}".format(input_path))

    results: List[ConversionResult] = []
    for source in sorted(input_path.glob("*.pdf"), key=lambda item: item.name.lower()):
        results.append(
            convert_pdf(source, output_path_for(source, output_dir), force=force)
        )
    return results


def render_markdown(
    *,
    source: Path,
    source_sha256: str,
    page_texts: Iterable[str],
    status: str,
) -> str:
    pages = list(page_texts)
    title_guess = source.stem.replace("_", " ").replace("-", " ")
    lines = [
        "---",
        "source_path: {}".format(_source_path_value(source)),
        "source_sha256: {}".format(source_sha256),
        "page_count: {}".format(len(pages)),
        "extractor: pymupdf",
        "status: {}".format(status),
        "title_guess: {}".format(_yaml_scalar(title_guess)),
        "---",
        "",
    ]
    if status != "ok":
        lines.extend(
            [
                "# 转换说明",
                "",
                "该 PDF 未提取到足够的文字，疑似扫描件或空白文件。",
                "当前版本未启用 OCR，请使用本机 OCR 工具处理后再进入知识库。",
                "",
            ]
        )
    for index, text in enumerate(pages, start=1):
        lines.extend(["## 第 {} 页".format(index), "", text, ""])
    return "\n".join(lines).rstrip() + "\n"


def clean_page_text(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _load_fitz():
    try:
        import pymupdf
    except ImportError as error:
        raise PdfMdError(
            'PyMuPDF is required. Install with: pip install -e ".[docs]"'
        ) from error
    return pymupdf


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".pdfmd-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _markdown_has_hash(path: Path, source_sha256: str) -> bool:
    return "source_sha256: {}".format(source_sha256) in path.read_text(
        encoding="utf-8"
    )


def _markdown_status(path: Path) -> str:
    match = re.search(r"^status:\s*(\S+)\s*$", path.read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1) if match else "ok"


def _markdown_page_count(path: Path) -> int:
    match = re.search(r"^page_count:\s*(\d+)\s*$", path.read_text(encoding="utf-8"), re.MULTILINE)
    return int(match.group(1)) if match else 0


def _yaml_scalar(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_. /()\u4e00-\u9fff-]+", value):
        return value
    return '"{}"'.format(value.replace('"', '\\"'))


def _source_path_value(source: Path) -> str:
    parts = source.parts
    if "incoming" in parts:
        index = len(parts) - 1 - parts[::-1].index("incoming")
        return Path(*parts[index:]).as_posix()
    try:
        return source.relative_to(Path.cwd()).as_posix()
    except ValueError:
        return source.name
