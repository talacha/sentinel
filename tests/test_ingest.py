from __future__ import annotations

import pytest
from fpdf import FPDF

from sentinel.ingest import IngestError, ingest_bytes


def make_pdf(*pages: str) -> bytes:
    pdf = FPDF()
    pdf.set_font("Helvetica", size=12)
    for text in pages:
        pdf.add_page()
        pdf.multi_cell(0, 8, text)
    return bytes(pdf.output())


def test_text_ingest_hash_and_info():
    doc = ingest_bytes(b"Tobacco use: No\nIncome: 150000", "a.txt")
    assert doc.info().pages == 1
    assert len(doc.sha256) == 64
    # Same content, same hash; different content, different hash.
    assert ingest_bytes(b"Tobacco use: No\nIncome: 150000", "b.txt").sha256 == doc.sha256
    assert ingest_bytes(b"other", "c.txt").sha256 != doc.sha256


def test_locate_is_tolerant_of_whitespace_case_and_smart_punctuation():
    doc = ingest_bytes("Applicant’s   tobacco use:\n  NO".encode(), "a.txt")
    ev = doc.evidence_for("applicant's tobacco use: no")
    assert ev is not None
    # Evidence carries the document's own text, not the model's paraphrase.
    assert ev.quote == "Applicant’s   tobacco use:\n  NO"
    assert (ev.start, ev.end) == (0, len(doc.text))


def test_locate_rejects_text_not_in_document():
    doc = ingest_bytes(b"Tobacco use: No", "a.txt")
    assert doc.locate("Tobacco use: Yes") is None
    assert doc.locate("") is None
    assert doc.locate("   ") is None
    assert doc.locate(None) is None


def test_form_feed_splits_pages_and_page_lookup():
    doc = ingest_bytes(b"first page text\fsecond page text", "a.txt")
    assert len(doc.pages) == 2
    ev = doc.evidence_for("second page")
    assert ev is not None and ev.page == 2
    assert doc.evidence_for("first page").page == 1


def test_pdf_ingest_extracts_text_per_page():
    doc = ingest_bytes(make_pdf("Cotinine (urine): POSITIVE", "Sum insured: 3,000,000"), "f.pdf")
    assert len(doc.pages) == 2
    ev = doc.evidence_for("cotinine (urine): positive")
    assert ev is not None and ev.page == 1
    assert doc.evidence_for("sum insured: 3,000,000").page == 2


def test_render_for_prompt_marks_pages_but_text_stays_clean():
    doc = ingest_bytes(b"one\ftwo", "a.txt")
    rendered = doc.render_for_prompt()
    assert "[page 1]" in rendered and "[page 2]" in rendered
    assert "[page" not in doc.text


def test_empty_and_oversized_and_broken_inputs_rejected():
    with pytest.raises(IngestError, match="no extractable text"):
        ingest_bytes(b"   \n ", "a.txt")
    with pytest.raises(IngestError, match="too large"):
        ingest_bytes(b"x" * 50, "a.txt", max_chars=10)
    with pytest.raises(IngestError, match="could not read PDF|no extractable"):
        ingest_bytes(b"%PDF-1.4 garbage", "bad.pdf")
