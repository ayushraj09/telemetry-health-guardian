"""
One-time generator for fixtures/long_climate_report.pdf — a synthetic
35-page "report" used as the demo app's source document, and specifically
as the R6 (silent truncation) target: it's long enough that a chaos-induced
"only the first N characters reach context" bug produces a genuinely wrong
answer rather than a coincidentally-fine one.

Run once:
    python fixtures/generate_fixture_pdf.py

Requires: reportlab (pip install reportlab)
"""

from pathlib import Path

from reportlab.lib.pagesizes import LETTER
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

OUTPUT_PATH = Path(__file__).parent / "long_climate_report.pdf"
PAGE_COUNT = 35

# Each page states one numbered, checkable "fact" about a fictional region
# so a downstream fact-check stage has concrete claims to verify, and so a
# truncation bug (only the first few pages reaching context) provably loses
# specific, identifiable facts rather than just "some vague content."
FACTS = [
    "Coastal sea levels in the Kestrel Basin rose {n} centimeters between 2005 and 2025.",
    "The Kestrel Basin's average summer temperature increased by {n} tenths of a degree Celsius per decade since 1990.",
    "Regional renewable energy capacity grew by {n} percent between 2015 and 2024.",
    "Annual rainfall in the northern Kestrel Basin decreased by {n} millimeters over the past two decades.",
    "The Kestrel Basin's coastal wetlands shrank by {n} hectares between 2000 and 2023.",
    "Municipal water reserves in the region declined by {n} percent during the 2018-2022 drought period.",
    "The number of recorded heatwave days per year in the Kestrel Basin rose from a baseline to {n} days by 2024.",
    "Forest cover in the Kestrel Basin's highland districts declined by {n} percent since 2010.",
]


def build_pdf() -> None:
    c = canvas.Canvas(str(OUTPUT_PATH), pagesize=LETTER)
    width, height = LETTER
    margin = 1 * inch
    text_width = width - 2 * margin

    for page_num in range(1, PAGE_COUNT + 1):
        c.setFont("Helvetica-Bold", 14)
        c.drawString(margin, height - margin, f"Kestrel Basin Regional Report — Page {page_num}")

        c.setFont("Helvetica", 11)
        y = height - margin - 0.5 * inch

        fact_template = FACTS[(page_num - 1) % len(FACTS)]
        fact_value = 3 + (page_num * 7) % 47  # deterministic pseudo-data, varies per page
        fact_sentence = fact_template.format(n=fact_value)

        paragraph = (
            f"Section {page_num}.1 — Observations. {fact_sentence} "
            "This section summarizes monitoring station data collected across the "
            "reporting period, cross-referenced against the regional environmental "
            "agency's baseline survey. Field teams noted consistent measurement "
            "conditions across all recording sites, with instrumentation calibrated "
            "quarterly. No anomalous readings were excluded from this dataset. "
            "Analysts note that year-over-year variation remained within expected "
            "bounds for the majority of the reporting window, with the exception of "
            "isolated events discussed in the appendix of the full technical report."
        )

        lines = _wrap_text(paragraph, c, "Helvetica", 11, text_width)
        for line in lines:
            c.drawString(margin, y, line)
            y -= 0.22 * inch

        c.setFont("Helvetica-Oblique", 9)
        c.drawString(margin, margin - 0.2 * inch, f"Kestrel Basin Regional Report — {page_num} / {PAGE_COUNT}")

        c.showPage()

    c.save()
    print(f"Wrote {OUTPUT_PATH} ({PAGE_COUNT} pages)")


def _wrap_text(text: str, c: canvas.Canvas, font: str, size: int, max_width: float) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join(current + [word])
        if c.stringWidth(candidate, font, size) <= max_width:
            current.append(word)
        else:
            if current:
                lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))
    return lines


if __name__ == "__main__":
    build_pdf()
