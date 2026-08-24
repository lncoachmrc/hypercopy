from pathlib import Path
import re
import fitz
from PIL import Image
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Image as RLImage
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

LANDING = Path(__file__).resolve().parents[1]
TMP = LANDING / ".whitepaper-tmp"
PUBLIC = LANDING / "public" / "whitepaper"
TEAL = colors.HexColor("#0A9990")
DARK = colors.HexColor("#0B1C26")
TEXT = colors.HexColor("#1E282D")
MUTED = colors.HexColor("#707B80")
LINE = colors.HexColor("#C9D3D5")

STYLES = {
    "cover_kicker": ParagraphStyle("cover_kicker", fontName="Helvetica-Bold", fontSize=9, leading=11, textColor=TEAL, alignment=TA_CENTER, spaceAfter=12),
    "cover_title": ParagraphStyle("cover_title", fontName="Helvetica-Bold", fontSize=24, leading=28, textColor=DARK, alignment=TA_CENTER, spaceAfter=12),
    "cover_lead": ParagraphStyle("cover_lead", fontName="Helvetica", fontSize=10.5, leading=15, textColor=MUTED, alignment=TA_CENTER, spaceAfter=14),
    "cover_agent": ParagraphStyle("cover_agent", fontName="Helvetica-Bold", fontSize=9.5, leading=12, textColor=TEAL, alignment=TA_CENTER, spaceAfter=7),
    "cover_version": ParagraphStyle("cover_version", fontName="Helvetica", fontSize=8.5, leading=11, textColor=MUTED, alignment=TA_CENTER),
    "section": ParagraphStyle("section", fontName="Helvetica-Bold", fontSize=16, leading=20, textColor=TEAL, spaceAfter=15),
    "subtitle": ParagraphStyle("subtitle", fontName="Helvetica-Bold", fontSize=10.5, leading=13, textColor=TEXT, spaceAfter=9),
    "h3": ParagraphStyle("h3", fontName="Helvetica-Bold", fontSize=10.2, leading=13, textColor=TEXT, spaceBefore=6, spaceAfter=5),
    "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=10, leading=12, textColor=TEXT, spaceBefore=6, spaceAfter=5),
    "body": ParagraphStyle("body", fontName="Helvetica", fontSize=8.6, leading=12.7, textColor=TEXT, spaceAfter=5),
    "bullet": ParagraphStyle("bullet", fontName="Helvetica", fontSize=8.4, leading=12, textColor=TEXT, leftIndent=13, firstLineIndent=-8, bulletIndent=3, spaceAfter=2),
    "quote": ParagraphStyle("quote", fontName="Helvetica-Bold", fontSize=10, leading=13, textColor=TEAL, alignment=TA_CENTER, spaceBefore=8, spaceAfter=7),
}


def inline(text: str) -> str:
    text = text.strip()
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = text.replace("&", "&amp;").replace("<b>", "@@B@@").replace("</b>", "@@/B@@")
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    return text.replace("@@B@@", "<b>").replace("@@/B@@", "</b>")


def parse_document(path: Path):
    text = path.read_text(encoding="utf-8")
    cover, *parts = re.split(r"\n(?=## 0[1-5] \| )", text)
    sections = []
    for part in parts:
        lines = part.splitlines()
        sections.append((lines[0].replace("## ", "").strip(), "\n".join(lines[1:]).strip()))
    if len(sections) != 5:
        raise RuntimeError(f"Expected five sections in {path}, got {len(sections)}")
    return cover, sections


def section_flow(title: str, body: str):
    flow = [Paragraph(inline(title), STYLES["section"])]
    lines = body.splitlines()
    paragraph = []
    index = 0

    def flush():
        nonlocal paragraph
        if paragraph:
            flow.append(Paragraph(inline(" ".join(item.strip() for item in paragraph)), STYLES["body"]))
            paragraph = []

    while index < len(lines):
        value = lines[index].strip()
        if not value or value == "---":
            flush()
            index += 1
            continue
        if value.startswith("### "):
            flush()
            style = STYLES["subtitle"] if len(flow) == 1 else STYLES["h3"]
            flow.append(Paragraph(inline(value[4:]), style))
            index += 1
            continue
        if value.startswith("## "):
            flush()
            flow.append(Paragraph(inline(value[3:]), STYLES["h2"]))
            index += 1
            continue
        if value.startswith("|"):
            flush()
            rows = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                row = [cell.strip() for cell in lines[index].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?", cell.replace(" ", "")) for cell in row):
                    rows.append(row)
                index += 1
            data = []
            for row_index, row in enumerate(rows):
                cells = []
                for column_index, cell in enumerate(row):
                    style = ParagraphStyle(
                        f"cell-{row_index}-{column_index}",
                        fontName="Helvetica-Bold" if row_index == 0 or column_index == 0 else "Helvetica",
                        fontSize=7.6,
                        leading=9.5,
                        textColor=colors.white if row_index == 0 else (TEAL if column_index == 0 else TEXT),
                    )
                    cells.append(Paragraph(inline(cell), style))
                data.append(cells)
            table = Table(data, colWidths=[48 * mm, 130 * mm], repeatRows=1)
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), DARK),
                ("GRID", (0, 0), (-1, -1), 0.4, LINE),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 5),
                ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]))
            flow.extend([table, Spacer(1, 6)])
            continue
        if value.startswith("- "):
            flush()
            while index < len(lines) and lines[index].strip().startswith("- "):
                flow.append(Paragraph("• " + inline(lines[index].strip()[2:]), STYLES["bullet"]))
                index += 1
            continue
        if value.startswith("> "):
            flush()
            flow.append(Paragraph(inline(value[2:]), STYLES["quote"]))
            index += 1
            continue
        if value.startswith("**") and value.endswith("**"):
            flush()
            flow.append(Paragraph(inline(value), STYLES["h3"]))
            index += 1
            continue
        paragraph.append(value)
        index += 1
    flush()
    return flow


def build_pdf(source: Path, output: Path, language: str):
    cover, sections = parse_document(source)
    cover_lines = [line.rstrip() for line in cover.splitlines()]
    title = next(line[4:].strip() for line in cover_lines if line.startswith("### "))
    title_index = cover_lines.index("### " + title)
    cover_values = [line.strip() for line in cover_lines[title_index + 1:] if line.strip() and line.strip() != "---"]
    lead = cover_values[0]
    agent = cover_values[1].strip("* ")
    version = cover_values[2].strip("* ")

    logo_source = LANDING / "public" / "traxion-logo-completo.webp"
    logo_png = TMP / "traxion-logo-render.png"
    Image.open(logo_source).convert("RGBA").save(logo_png, "PNG")

    document = SimpleDocTemplate(
        str(output), pagesize=letter,
        leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=15 * mm, bottomMargin=14 * mm,
        title=f"TRAXION White Paper {language.upper()}", author="TRAXION",
    )
    story = [
        Spacer(1, 18 * mm),
        RLImage(str(logo_png), width=150 * mm, height=64.9 * mm),
        Spacer(1, 4 * mm),
        Paragraph("WHITE PAPER", STYLES["cover_kicker"]),
        Paragraph(inline(title), STYLES["cover_title"]),
        Paragraph(inline(lead), STYLES["cover_lead"]),
        Paragraph(inline(agent), STYLES["cover_agent"]),
        Paragraph(inline(version), STYLES["cover_version"]),
        PageBreak(),
    ]
    for section_index, (section_title, section_body) in enumerate(sections):
        story.extend(section_flow(section_title, section_body))
        if section_index < len(sections) - 1:
            story.append(PageBreak())

    def first_page(canvas, _doc):
        canvas.saveState()
        canvas.setFillColor(colors.white)
        canvas.rect(0, 0, *letter, stroke=0, fill=1)
        canvas.restoreState()

    def later_pages(canvas, doc):
        width, height = letter
        canvas.saveState()
        canvas.setFillColor(MUTED)
        canvas.setFont("Helvetica-Bold", 7.5)
        canvas.drawString(14 * mm, height - 8.5 * mm, f"TRAXION | WHITE PAPER {language.upper()}")
        canvas.setFont("Helvetica", 7.5)
        canvas.drawString(14 * mm, 7.5 * mm, "TRAXION | 2026")
        canvas.drawCentredString(width / 2, 7.5 * mm, str(doc.page))
        canvas.restoreState()

    document.build(story, onFirstPage=first_page, onLaterPages=later_pages)


def rasterize(pdf: Path, language: str):
    target = PUBLIC / language
    target.mkdir(parents=True, exist_ok=True)
    document = fitz.open(str(pdf))
    if len(document) != 6:
        raise RuntimeError(f"Expected six pages for {language}, got {len(document)}")
    for page_index, page in enumerate(document, start=1):
        pixmap = page.get_pixmap(matrix=fitz.Matrix(2.8, 2.8), alpha=False)
        image = Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)
        image = image.resize((1547, 2002), Image.Resampling.LANCZOS)
        output = target / f"trx-wp-{language}-{page_index:02d}.webp"
        image.save(output, "WEBP", quality=86, method=6)
        check = Image.open(output)
        if check.size != (1547, 2002):
            raise RuntimeError(f"Unexpected raster size for {output}: {check.size}")


def main():
    for language in ("en", "es"):
        source = TMP / f"TRAXION_White_Paper_{language.upper()}.md"
        pdf = TMP / f"TRAXION_White_Paper_{language.upper()}.pdf"
        build_pdf(source, pdf, language)
        rasterize(pdf, language)
    print("Generated 12 localized TRAXION whitepaper WebP pages.")


if __name__ == "__main__":
    main()
