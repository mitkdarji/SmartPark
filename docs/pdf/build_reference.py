"""Generate the SmartPark technical reference PDF.

    cd backend && .venv/bin/python ../docs/pdf/build_reference.py

Everything in the document is written by hand here rather than scraped from the
source, so it explains *why* each decision was made — which is what the source
cannot tell you.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, CondPageBreak, Flowable, Frame, KeepTogether, NextPageTemplate,
    PageBreak, PageTemplate, Paragraph, Spacer, Table, TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents

# ── Fonts ─────────────────────────────────────────────────────
FONT_DIR = Path("/System/Library/Fonts/Supplemental")
pdfmetrics.registerFont(TTFont("Body", FONT_DIR / "Arial.ttf"))
pdfmetrics.registerFont(TTFont("Body-Bold", FONT_DIR / "Arial Bold.ttf"))
pdfmetrics.registerFont(TTFont("Body-Italic", FONT_DIR / "Arial Italic.ttf"))
pdfmetrics.registerFont(TTFont("Body-BoldItalic", FONT_DIR / "Arial Bold Italic.ttf"))
pdfmetrics.registerFont(TTFont("Mono", FONT_DIR / "Courier New.ttf"))
pdfmetrics.registerFont(TTFont("Mono-Bold", FONT_DIR / "Courier New Bold.ttf"))
pdfmetrics.registerFontFamily(
    "Body", normal="Body", bold="Body-Bold",
    italic="Body-Italic", boldItalic="Body-BoldItalic",
)

# ── Palette ───────────────────────────────────────────────────
BRAND = colors.HexColor("#0f6b4a")
BRAND_LIGHT = colors.HexColor("#e8f4ee")
INK = colors.HexColor("#1a1d23")
INK_SOFT = colors.HexColor("#4a5262")
INK_MUTED = colors.HexColor("#767f91")
RULE = colors.HexColor("#d9dee6")
CODE_BG = colors.HexColor("#f6f7f9")
CODE_BORDER = colors.HexColor("#e0e4ea")
AMBER = colors.HexColor("#9a6700")
AMBER_BG = colors.HexColor("#fff8e6")
RED = colors.HexColor("#a4262c")
RED_BG = colors.HexColor("#fdf1f1")
BLUE = colors.HexColor("#1f5fa8")
BLUE_BG = colors.HexColor("#eef4fc")

PAGE_W, PAGE_H = A4
MARGIN = 18 * mm
CONTENT_W = PAGE_W - 2 * MARGIN

# ── Styles ────────────────────────────────────────────────────
_ss = getSampleStyleSheet()

S = {
    "cover_title": ParagraphStyle(
        "cover_title", fontName="Body-Bold", fontSize=40, leading=44,
        textColor=INK, alignment=TA_LEFT, spaceAfter=6,
    ),
    "cover_sub": ParagraphStyle(
        "cover_sub", fontName="Body", fontSize=15, leading=22,
        textColor=INK_SOFT, spaceAfter=4,
    ),
    "cover_meta": ParagraphStyle(
        "cover_meta", fontName="Body", fontSize=9.5, leading=15, textColor=INK_MUTED,
    ),
    "part": ParagraphStyle(
        "part", fontName="Body-Bold", fontSize=27, leading=32,
        textColor=BRAND, spaceBefore=0, spaceAfter=10,
    ),
    "h1": ParagraphStyle(
        "h1", fontName="Body-Bold", fontSize=17, leading=21,
        textColor=INK, spaceBefore=18, spaceAfter=8,
    ),
    "h2": ParagraphStyle(
        "h2", fontName="Body-Bold", fontSize=12.5, leading=16,
        textColor=BRAND, spaceBefore=14, spaceAfter=5,
    ),
    "h3": ParagraphStyle(
        "h3", fontName="Body-Bold", fontSize=10.5, leading=14,
        textColor=INK, spaceBefore=10, spaceAfter=3,
    ),
    "body": ParagraphStyle(
        "body", fontName="Body", fontSize=9.6, leading=14.6,
        textColor=INK, alignment=TA_JUSTIFY, spaceAfter=7,
    ),
    "body_tight": ParagraphStyle(
        "body_tight", fontName="Body", fontSize=9.6, leading=14.6,
        textColor=INK, alignment=TA_JUSTIFY, spaceAfter=2,
    ),
    "lead": ParagraphStyle(
        "lead", fontName="Body", fontSize=11, leading=16.5,
        textColor=INK_SOFT, alignment=TA_LEFT, spaceAfter=10,
    ),
    "bullet": ParagraphStyle(
        "bullet", fontName="Body", fontSize=9.6, leading=14.2,
        textColor=INK, leftIndent=13, bulletIndent=3, spaceAfter=3.5,
        alignment=TA_JUSTIFY,
    ),
    "code": ParagraphStyle(
        "code", fontName="Mono", fontSize=8.0, leading=11.4,
        textColor=INK, alignment=TA_LEFT,
    ),
    "cap": ParagraphStyle(
        "cap", fontName="Body-Italic", fontSize=8.2, leading=11.5,
        textColor=INK_MUTED, spaceBefore=2, spaceAfter=9,
    ),
    "cell": ParagraphStyle(
        "cell", fontName="Body", fontSize=8.4, leading=11.6, textColor=INK,
    ),
    "cell_b": ParagraphStyle(
        "cell_b", fontName="Body-Bold", fontSize=8.4, leading=11.6, textColor=INK,
    ),
    "cell_mono": ParagraphStyle(
        "cell_mono", fontName="Mono", fontSize=7.8, leading=11.2, textColor=INK,
    ),
    "cell_head": ParagraphStyle(
        "cell_head", fontName="Body-Bold", fontSize=8.2, leading=11,
        textColor=colors.white,
    ),
    "callout_title": ParagraphStyle(
        "callout_title", fontName="Body-Bold", fontSize=8.6, leading=12,
        textColor=INK, spaceAfter=2.5,
    ),
    "callout_body": ParagraphStyle(
        "callout_body", fontName="Body", fontSize=9.0, leading=13.4,
        textColor=INK, alignment=TA_JUSTIFY,
    ),
    "toc1": ParagraphStyle(
        "toc1", fontName="Body-Bold", fontSize=10.5, leading=17,
        textColor=BRAND, spaceBefore=8,
    ),
    "toc2": ParagraphStyle(
        "toc2", fontName="Body", fontSize=9.3, leading=14.4,
        textColor=INK, leftIndent=12,
    ),
    "toc3": ParagraphStyle(
        "toc3", fontName="Body", fontSize=8.7, leading=13,
        textColor=INK_SOFT, leftIndent=26,
    ),
}


# ── Flowables ─────────────────────────────────────────────────
class HRule(Flowable):
    def __init__(self, width=CONTENT_W, thickness=0.7, color=RULE, space=4):
        super().__init__()
        self.width, self.thickness, self.color, self.space = width, thickness, color, space
        self.height = thickness + space

    def draw(self):
        self.canv.setStrokeColor(self.color)
        self.canv.setLineWidth(self.thickness)
        self.canv.line(0, self.space, self.width, self.space)


class Bar(Flowable):
    """A thick coloured rule used under part titles."""

    def __init__(self, width=64, thickness=4, color=BRAND):
        super().__init__()
        self.width, self.thickness, self.color = width, thickness, color
        self.height = thickness + 10

    def draw(self):
        self.canv.setFillColor(self.color)
        self.canv.rect(0, 8, self.width, self.thickness, stroke=0, fill=1)


# ── Content helpers ───────────────────────────────────────────
_ESCAPES = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"))


def esc(text: str) -> str:
    for a, b in _ESCAPES:
        text = text.replace(a, b)
    return text


_counter = {"part": 0, "h1": 0, "h2": 0}


def part(title: str, blurb: str = ""):
    _counter["part"] += 1
    _counter["h1"] = 0
    roman = ["", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X",
             "XI", "XII", "XIII", "XIV"][_counter["part"]]
    out = [
        PageBreak(),
        Spacer(1, 40),
        Paragraph(f"PART {roman}", ParagraphStyle(
            "pl", fontName="Body-Bold", fontSize=10, leading=13,
            textColor=INK_MUTED, spaceAfter=6)),
        Paragraph(esc(title), S["part"]),
        Bar(),
    ]
    if blurb:
        out.append(Paragraph(esc(blurb), S["lead"]))
    out.append(Spacer(1, 6))
    out.append(TocEntry(title, 0))
    return out


def h1(title: str):
    _counter["h1"] += 1
    _counter["h2"] = 0
    n = f"{_counter['part']}.{_counter['h1']}"
    return [
        CondPageBreak(70),
        Paragraph(f"{n}&nbsp;&nbsp;{esc(title)}", S["h1"]),
        HRule(thickness=1.1, color=BRAND, space=3),
        Spacer(1, 5),
        TocEntry(f"{n}  {title}", 1),
    ]


def h2(title: str):
    _counter["h2"] += 1
    n = f"{_counter['part']}.{_counter['h1']}.{_counter['h2']}"
    return [
        CondPageBreak(48),
        Paragraph(f"{n}&nbsp;&nbsp;{esc(title)}", S["h2"]),
        TocEntry(f"{n}  {title}", 2),
    ]


def h3(title: str):
    return [CondPageBreak(40), Paragraph(esc(title), S["h3"])]


def p(text: str, style: str = "body"):
    return Paragraph(text, S[style])


def lead(text: str):
    return Paragraph(text, S["lead"])


def bullets(items: list[str], marker: str = "▪"):
    return [
        Paragraph(item, S["bullet"], bulletText=marker)
        for item in items
    ]


def numbered(items: list[str]):
    return [
        Paragraph(item, S["bullet"], bulletText=f"{i}.")
        for i, item in enumerate(items, start=1)
    ]


def code(text: str, caption: str = "", size: float = 8.0):
    body = esc(text.strip("\n").rstrip())
    style = ParagraphStyle("c", parent=S["code"], fontSize=size, leading=size * 1.42)
    para = Paragraph(body.replace(" ", "&nbsp;").replace("\n", "<br/>"), style)
    tbl = Table([[para]], colWidths=[CONTENT_W])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CODE_BG),
        ("BOX", (0, 0), (-1, -1), 0.6, CODE_BORDER),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    out = [tbl]
    out.append(Paragraph(esc(caption), S["cap"]) if caption else Spacer(1, 9))
    return out


CALLOUT_KINDS = {
    "why": ("Why it is built this way", BRAND, BRAND_LIGHT),
    "interview": ("In an interview", BLUE, BLUE_BG),
    "honest": ("Honest caveat", AMBER, AMBER_BG),
    "gotcha": ("Failure mode", RED, RED_BG),
    "note": ("Note", INK_SOFT, colors.HexColor("#f4f5f7")),
}


def callout(kind: str, text: str, title: str | None = None):
    label, accent, bg = CALLOUT_KINDS[kind]
    inner = [
        Paragraph(esc(title or label), ParagraphStyle(
            "ct", parent=S["callout_title"], textColor=accent)),
        Paragraph(text, S["callout_body"]),
    ]
    tbl = Table([[inner]], colWidths=[CONTENT_W])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("LINEBEFORE", (0, 0), (0, -1), 2.6, accent),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return [KeepTogether(tbl), Spacer(1, 9)]


def table(rows: list[list[str]], widths: list[float], header: bool = True,
          caption: str = "", mono_cols: tuple[int, ...] = (), align_right: tuple[int, ...] = ()):
    """`widths` are fractions of the content width."""
    col_widths = [w * CONTENT_W for w in widths]
    data = []
    for r, row in enumerate(rows):
        cells = []
        for c, cell in enumerate(row):
            if header and r == 0:
                style = S["cell_head"]
            elif c in mono_cols:
                style = S["cell_mono"]
            else:
                style = S["cell"]
            if c in align_right and not (header and r == 0):
                style = ParagraphStyle(f"r{r}{c}", parent=style, alignment=2)
            cells.append(Paragraph(cell, style))
        data.append(cells)

    tbl = Table(data, colWidths=col_widths, repeatRows=1 if header else 0)
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, RULE),
        ("BOX", (0, 0), (-1, -1), 0.6, RULE),
    ]
    if header:
        style += [
            ("BACKGROUND", (0, 0), (-1, 0), BRAND),
            ("LINEBELOW", (0, 0), (-1, 0), 0, colors.white),
            ("TOPPADDING", (0, 0), (-1, 0), 6),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ]
        style += [("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fafbfc")])]
    tbl.setStyle(TableStyle(style))
    out = [tbl]
    out.append(Paragraph(esc(caption), S["cap"]) if caption else Spacer(1, 10))
    return out


class TocEntry(Flowable):
    """Zero-height marker that registers a heading with the table of contents."""

    def __init__(self, text: str, level: int):
        super().__init__()
        self.text, self.level = text, level
        self.width = 0
        self.height = 0

    def draw(self):
        return

    def wrap(self, *args):
        return (0, 0)


# ── Document template ─────────────────────────────────────────
TITLE = "SmartPark — Technical Reference"
SUBTITLE = "Intelligent parking allocation and automated billing"


class Doc(BaseDocTemplate):
    def __init__(self, path: str, **kw):
        super().__init__(path, pagesize=A4,
                         leftMargin=MARGIN, rightMargin=MARGIN,
                         topMargin=MARGIN + 8 * mm, bottomMargin=MARGIN + 4 * mm,
                         title=TITLE, author="Mit Darji",
                         subject=SUBTITLE, **kw)
        frame = Frame(self.leftMargin, self.bottomMargin,
                      self.width, self.height, id="body",
                      leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        self.addPageTemplates([
            PageTemplate(id="cover", frames=[frame], onPage=self._cover_page),
            PageTemplate(id="main", frames=[frame], onPage=self._main_page),
        ])

    def _cover_page(self, canv, doc):
        canv.saveState()
        canv.setFillColor(BRAND)
        canv.rect(0, PAGE_H - 14, PAGE_W, 14, stroke=0, fill=1)
        canv.restoreState()

    def _main_page(self, canv, doc):
        canv.saveState()
        # Running header
        canv.setFont("Body", 7.6)
        canv.setFillColor(INK_MUTED)
        canv.drawString(MARGIN, PAGE_H - MARGIN - 2, TITLE)
        canv.drawRightString(PAGE_W - MARGIN, PAGE_H - MARGIN - 2, SUBTITLE)
        canv.setStrokeColor(RULE)
        canv.setLineWidth(0.5)
        canv.line(MARGIN, PAGE_H - MARGIN - 8, PAGE_W - MARGIN, PAGE_H - MARGIN - 8)
        # Footer
        canv.line(MARGIN, MARGIN + 14, PAGE_W - MARGIN, MARGIN + 14)
        canv.setFont("Body", 7.6)
        canv.drawString(MARGIN, MARGIN + 5, "SmartPark  ·  B.Tech ICT minor project  ·  PDEU")
        canv.setFont("Body-Bold", 8.4)
        canv.setFillColor(BRAND)
        canv.drawRightString(PAGE_W - MARGIN, MARGIN + 5, str(canv.getPageNumber() - 1))
        canv.restoreState()

    def afterFlowable(self, flowable):
        if isinstance(flowable, TocEntry):
            self.notify("TOCEntry", (flowable.level, flowable.text, self.page))


def cover() -> list:
    today = dt.date.today().strftime("%d %B %Y")
    return [
        Spacer(1, 46 * mm),
        Paragraph("SmartPark", S["cover_title"]),
        Bar(width=96, thickness=5),
        Spacer(1, 4),
        Paragraph("Intelligent parking allocation<br/>and automated billing", S["cover_sub"]),
        Spacer(1, 10 * mm),
        Paragraph(
            "A camera reads the number plate at the gate. An allocation policy picks the bay "
            "and the driver is guided straight to it. On the way out the same read closes the "
            "session, prices the stay against live occupancy, and settles it from a wallet — "
            "with no ticket, no attendant and no cash.",
            ParagraphStyle("cb", fontName="Body", fontSize=10.5, leading=16,
                           textColor=INK_SOFT, alignment=TA_LEFT)),
        Spacer(1, 14 * mm),
        Table(
            [[Paragraph(k, ParagraphStyle("ck", fontName="Body-Bold", fontSize=8.4,
                                          textColor=INK_MUTED)),
              Paragraph(v, ParagraphStyle("cv", fontName="Body", fontSize=8.8,
                                          textColor=INK))]
             for k, v in [
                 ("Document", "Complete technical reference"),
                 ("Project", "Vehicle Number Plate Recognition System for Parking Management"),
                 ("Programme", "B.Tech ICT, Semester 6 — Pandit Deendayal Energy University"),
                 ("Scale", "15,500 lines Python · 6,200 lines TypeScript · 77 API routes · 136 tests"),
                 ("Stack", "FastAPI · SQLAlchemy 2 · OpenCV · scikit-learn · React · TypeScript"),
                 ("Generated", today),
             ]],
            colWidths=[0.22 * CONTENT_W, 0.78 * CONTENT_W],
        ),
        Spacer(1, 16 * mm),
        HRule(),
        Paragraph(
            "Read this end to end once, then use the table of contents. Part II is the single "
            "most useful section: it follows one car through the entire system, and every other "
            "part expands on a step in that journey.",
            ParagraphStyle("cf", fontName="Body-Italic", fontSize=9, leading=13.5,
                           textColor=INK_MUTED)),
        NextPageTemplate("main"),
        PageBreak(),
    ]


def toc_page() -> list:
    toc = TableOfContents()
    toc.levelStyles = [S["toc1"], S["toc2"], S["toc3"]]
    return [
        Paragraph("Contents", ParagraphStyle(
            "tt", fontName="Body-Bold", fontSize=22, leading=26,
            textColor=INK, spaceAfter=4)),
        Bar(),
        Spacer(1, 6),
        toc,
    ]
