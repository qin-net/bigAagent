from __future__ import annotations

from pathlib import Path

import pytest

from insightagent.pdfmd import PdfMdError, convert_path, convert_pdf


def _write_pdf(path: Path, pages: list[str]) -> None:
    pymupdf = pytest.importorskip("pymupdf")
    document = pymupdf.open()
    try:
        for text in pages:
            page = document.new_page()
            if text:
                page.insert_text((72, 72), text)
        document.save(str(path))
    finally:
        document.close()


def test_convert_text_pdf_writes_metadata_and_page_sections(tmp_path: Path):
    source = tmp_path / "notice.pdf"
    output = tmp_path / "markdown" / "notice.md"
    _write_pdf(source, ["Notice No. 2026-01\nKnown clause text.", "Second page text."])

    result = convert_pdf(source, output)

    content = output.read_text(encoding="utf-8")
    assert result.status == "ok"
    assert result.page_count == 2
    assert "source_sha256: {}".format(result.source_sha256) in content
    assert "status: ok" in content
    assert "## 第 1 页" in content
    assert "## 第 2 页" in content
    assert "Known clause text." in content


def test_convert_blank_pdf_marks_needs_ocr(tmp_path: Path):
    source = tmp_path / "scan.pdf"
    output = tmp_path / "markdown" / "scan.md"
    _write_pdf(source, [""])

    result = convert_pdf(source, output)

    assert result.status == "needs_ocr"
    assert "status: needs_ocr" in output.read_text(encoding="utf-8")
    assert "当前版本未启用 OCR" in output.read_text(encoding="utf-8")


def test_unchanged_source_is_skipped(tmp_path: Path):
    source = tmp_path / "notice.pdf"
    output = tmp_path / "markdown" / "notice.md"
    _write_pdf(source, ["Searchable text content."])
    first = convert_pdf(source, output)
    second = convert_pdf(source, output)

    assert first.skipped is False
    assert second.skipped is True
    assert second.source_sha256 == first.source_sha256


def test_directory_mode_only_processes_pdfs(tmp_path: Path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    _write_pdf(incoming / "one.pdf", ["First page searchable text."])
    (incoming / "ignore.txt").write_text("ignore", encoding="utf-8")

    results = convert_path(incoming, output_dir=tmp_path / "markdown")

    assert len(results) == 1
    assert results[0].output_path.name == "one.md"


def test_missing_input_raises_clear_error(tmp_path: Path):
    with pytest.raises(PdfMdError, match="does not exist"):
        convert_path(tmp_path / "missing")
