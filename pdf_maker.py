"""
Generate a PDF document from a title + body text, with proper Arabic
(RTL + letter-shaping) and Latin/French support.

The body text uses simple paragraph splitting on blank lines. Each line is
auto-detected as RTL (Arabic) or LTR (Latin) and rendered with the right
alignment + font. Headings are detected via lines starting with "# ", "## ",
or "### " (markdown-style).
"""
from __future__ import annotations

import io
import os
import re
from typing import List, Tuple

import arabic_reshaper
from bidi.algorithm import get_display
from fpdf import FPDF
from fpdf.enums import XPos, YPos


FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")
ARABIC_FONT = os.path.join(FONTS_DIR, "NotoSansArabic-Regular.ttf")
LATIN_FONT = os.path.join(FONTS_DIR, "NotoSans-Regular.ttf")

# Arabic script range (covers Arabic, Arabic Supplement, Arabic Extended-A,
# Arabic Presentation Forms-A and B).
_ARABIC_RE = re.compile(
    r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]"
)


def _is_rtl(text: str) -> bool:
    """Return True if the line contains any Arabic characters."""
    return bool(_ARABIC_RE.search(text or ""))


def _shape(text: str) -> str:
    """Reshape Arabic letters and apply bidi so they render correctly."""
    try:
        reshaped = arabic_reshaper.reshape(text)
        return get_display(reshaped)
    except Exception:
        return text


def _split_paragraphs(body: str) -> List[str]:
    """Split body into paragraphs on one-or-more blank lines."""
    body = (body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not body:
        return []
    return [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]


def _classify_line(line: str) -> Tuple[str, str]:
    """Return (kind, text) where kind is 'h1'/'h2'/'h3'/'bullet'/'p'."""
    s = line.lstrip()
    if s.startswith("### "):
        return "h3", s[4:].strip()
    if s.startswith("## "):
        return "h2", s[3:].strip()
    if s.startswith("# "):
        return "h1", s[2:].strip()
    if s.startswith(("- ", "• ", "* ")):
        return "bullet", s[2:].strip()
    return "p", line.strip()


class _PDF(FPDF):
    def __init__(self, header_title: str = ""):
        super().__init__(orientation="P", unit="mm", format="A4")
        self._header_title = header_title or ""
        self.set_auto_page_break(auto=True, margin=18)
        self.set_margins(left=18, top=18, right=18)
        self.add_font("NotoArabic", "", ARABIC_FONT)
        self.add_font("NotoLatin", "", LATIN_FONT)

    def header(self):
        if not self._header_title:
            return
        self._draw_text(self._header_title, size=11, bold=False, gray=120)
        self.ln(2)
        self.set_draw_color(220, 220, 220)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(4)

    def footer(self):
        self.set_y(-14)
        self._draw_text(f"{self.page_no()}", size=9, gray=150, center=True)

    def _font_for(self, text: str, size: int = 12):
        if _is_rtl(text):
            self.set_font("NotoArabic", "", size)
        else:
            self.set_font("NotoLatin", "", size)

    def _draw_text(
        self,
        text: str,
        size: int = 12,
        bold: bool = False,  # noqa: ARG002 (no bold variant loaded)
        gray: int | None = None,
        center: bool = False,
    ):
        self._font_for(text, size=size)
        if gray is None:
            self.set_text_color(20, 20, 20)
        else:
            self.set_text_color(gray, gray, gray)

        rtl = _is_rtl(text)
        align = "C" if center else ("R" if rtl else "L")
        rendered = _shape(text) if rtl else text
        self.cell(0, size * 0.6 + 2, rendered, ln=1, align=align)

    def add_title(self, text: str):
        if not text:
            return
        self._draw_text(text, size=20)
        self.set_draw_color(40, 40, 40)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(6)

    def add_paragraph(self, text: str):
        for raw_line in text.split("\n"):
            kind, content = _classify_line(raw_line)
            if not content:
                self.ln(2)
                continue
            self._render_block(kind, content)
        self.ln(2)

    def _render_block(self, kind: str, content: str):
        rtl = _is_rtl(content)
        self._font_for(content, size=self._size_for(kind))
        self.set_text_color(20, 20, 20)

        if kind == "bullet":
            bullet = "•"
            content = f"{content} {bullet}" if rtl else f"{bullet} {content}"

        rendered = _shape(content) if rtl else content
        align = "R" if rtl else "L"
        line_h = self._size_for(kind) * 0.55 + 2.5

        # Use multi_cell for wrapping. fpdf2 honours align but cannot do real
        # bidi inside a wrapped chunk; we shape the whole line first which is
        # fine for medium-length paragraphs.
        self.set_x(self.l_margin)
        self.multi_cell(
            0,
            line_h,
            rendered,
            align=align,
            new_x=XPos.LMARGIN,
            new_y=YPos.NEXT,
        )

        if kind in ("h1", "h2", "h3"):
            self.ln(1)

    @staticmethod
    def _size_for(kind: str) -> int:
        return {"h1": 18, "h2": 15, "h3": 13, "bullet": 12, "p": 12}.get(kind, 12)


def make_pdf(title: str, body: str) -> bytes:
    """Render a PDF and return its bytes."""
    pdf = _PDF(header_title=title or "")
    pdf.add_page()
    if title:
        pdf.add_title(title)
    for para in _split_paragraphs(body):
        pdf.add_paragraph(para)
    out = pdf.output(dest="S")
    if isinstance(out, (bytes, bytearray)):
        return bytes(out)
    if isinstance(out, str):
        return out.encode("latin-1", errors="ignore")
    buf = io.BytesIO()
    buf.write(out)
    return buf.getvalue()
