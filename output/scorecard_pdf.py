"""Generate professional PDF scorecard using ReportLab."""

import os

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, Image, KeepTogether, Flowable,
)
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from datetime import datetime

import config

STATUTORY_CATEGORY = "Statutory Compliance"

# Colors
NAVY = colors.HexColor('#003366')
GREEN = colors.HexColor('#28a745')
AMBER = colors.HexColor('#cc9900')
RED = colors.HexColor('#c00000')
LIGHT_GRAY = colors.HexColor('#f0f4f8')
BORDER_GRAY = colors.HexColor('#dee2e6')
TRACK_GRAY = colors.HexColor('#e9ecef')


class ScoreBar(Flowable):
    """Horizontal progress bar showing a 0–1 category score."""

    def __init__(self, score, width=3.2 * inch, height=10):
        Flowable.__init__(self)
        self.score = max(0.0, min(1.0, float(score or 0)))
        self.bar_width = width
        self.bar_height = height
        self.width = width
        self.height = height

    def draw(self):
        self.canv.setFillColor(TRACK_GRAY)
        self.canv.roundRect(0, 0, self.bar_width, self.bar_height, 3, fill=1, stroke=0)
        fill_w = self.bar_width * self.score
        if fill_w > 0:
            fill_color = GREEN if self.score >= 0.7 else AMBER if self.score >= 0.4 else RED
            self.canv.setFillColor(fill_color)
            # Clip to rounded track by drawing a rect; short fills use square ends.
            self.canv.rect(0, 0, fill_w, self.bar_height, fill=1, stroke=0)


def _logo_path():
    path = getattr(config, "LOGO_PATH", None) or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "assets",
        "cam-leadership-institute-logo.png",
    )
    return path if os.path.exists(path) else None


def _display_categories(analysis_results, scorecard_only):
    categories = dict(analysis_results.get("categories") or {})
    if scorecard_only:
        categories.pop(STATUTORY_CATEGORY, None)
    return categories


def _display_overall_score(analysis_results, categories, scorecard_only):
    if not scorecard_only:
        return analysis_results.get("overall_score", 0)
    if STATUTORY_CATEGORY not in (analysis_results.get("categories") or {}):
        return analysis_results.get("overall_score", 0)
    from analysis.scoring_criteria import calculate_overall_score
    cat_scores = {name: data.get("score", 0) for name, data in categories.items()}
    return round(calculate_overall_score(cat_scores), 2)


def generate_scorecard_pdf(analysis_results, client_name, output_path, jurisdiction=None, scorecard_only=False):
    """Generate a professional PDF scorecard."""
    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
    )

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        'Title2', parent=styles['Title'],
        fontSize=22, textColor=NAVY, spaceAfter=6, alignment=TA_CENTER,
    ))
    styles.add(ParagraphStyle(
        'SubTitle', parent=styles['Normal'],
        fontSize=11, textColor=colors.gray, alignment=TA_CENTER, spaceAfter=20,
    ))
    styles.add(ParagraphStyle(
        'SectionHead', parent=styles['Heading2'],
        fontSize=14, textColor=NAVY, spaceBefore=16, spaceAfter=8,
        borderWidth=0, borderPadding=0,
    ))
    styles.add(ParagraphStyle(
        'CatHead', parent=styles['Heading3'],
        fontSize=12, textColor=NAVY, spaceBefore=12, spaceAfter=4,
    ))
    styles.add(ParagraphStyle(
        'BodyText2', parent=styles['BodyText'],
        fontSize=10, leading=14, spaceAfter=6,
    ))
    styles.add(ParagraphStyle(
        'SmallText', parent=styles['Normal'],
        fontSize=8, textColor=colors.gray, alignment=TA_CENTER,
    ))
    styles.add(ParagraphStyle(
        'BarLabel', parent=styles['Normal'],
        fontSize=10, textColor=NAVY, leading=12, alignment=TA_LEFT,
    ))
    styles.add(ParagraphStyle(
        'BarPct', parent=styles['Normal'],
        fontSize=10, leading=12, alignment=TA_RIGHT,
    ))

    elements = []

    # Brand logo at top of first page
    logo = _logo_path()
    if logo:
        img = Image(logo, width=2.25 * inch, height=2.25 * inch * (398 / 1458))
        img.hAlign = 'CENTER'
        elements.append(img)
        elements.append(Spacer(1, 10))

    # Title
    elements.append(Paragraph("CONTRACT REVIEW SCORECARD", styles['Title2']))

    # Client info
    review_date = datetime.now().strftime("%B %d, %Y")
    jurisdiction_text = ""
    if jurisdiction and jurisdiction.get("state"):
        jurisdiction_text = (
            f" &nbsp;|&nbsp; Jurisdiction: {jurisdiction['state']} "
            f"({jurisdiction.get('state_abbrev', '')})"
        )
    elements.append(Paragraph(
        f"Client: <b>{client_name}</b> &nbsp;|&nbsp; Review Date: <b>{review_date}</b>{jurisdiction_text}",
        styles['SubTitle']
    ))

    elements.append(HRFlowable(width="100%", thickness=2, color=NAVY, spaceAfter=20))

    categories = _display_categories(analysis_results, scorecard_only)
    overall_score = _display_overall_score(analysis_results, categories, scorecard_only)
    overall_pct = round(overall_score * 100)
    score_color = GREEN if overall_score >= 0.7 else AMBER if overall_score >= 0.4 else RED

    if overall_score >= 0.8:
        rating = "Strong Agreement"
    elif overall_score >= 0.6:
        rating = "Adequate — Some Improvements Recommended"
    elif overall_score >= 0.4:
        rating = "Needs Significant Improvement"
    else:
        rating = "Major Revision or Replacement Recommended"

    score_table = Table(
        [[Paragraph(f'<font size="28" color="{score_color.hexval()}">{overall_pct}%</font>', styles['Title2']),
          Paragraph(f'<font size="14" color="{NAVY.hexval()}">Overall Score</font><br/>'
                    f'<font size="10" color="#666666">{rating}</font>', styles['BodyText2'])]],
        colWidths=[2 * inch, 4.5 * inch],
    )
    score_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (0, 0), (0, 0), 'CENTER'),
        ('BACKGROUND', (0, 0), (-1, -1), LIGHT_GRAY),
        ('ROUNDEDCORNERS', [8, 8, 8, 8]),
        ('TOPPADDING', (0, 0), (-1, -1), 12),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
    ]))
    elements.append(score_table)
    elements.append(Spacer(1, 16))

    # Category score overview with visual progress bars
    if categories:
        elements.append(Paragraph("Category Scores", styles['SectionHead']))
        elements.append(HRFlowable(width="100%", thickness=1, color=BORDER_GRAY, spaceAfter=8))

        bar_rows = []
        for cat_name, cat_data in categories.items():
            score = cat_data.get("score", 0)
            pct = round(score * 100)
            sc = GREEN if score >= 0.7 else AMBER if score >= 0.4 else RED
            bar_rows.append([
                Paragraph(cat_name, styles['BarLabel']),
                ScoreBar(score, width=3.0 * inch, height=9),
                Paragraph(
                    f'<font color="{sc.hexval()}"><b>{pct}%</b></font>',
                    styles['BarPct'],
                ),
            ])

        bars_table = Table(bar_rows, colWidths=[2.4 * inch, 3.1 * inch, 0.7 * inch])
        bars_table.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('BACKGROUND', (0, 0), (-1, -1), colors.white),
            ('ROWBACKGROUNDS', (0, 0), (-1, -1), [colors.white, LIGHT_GRAY]),
            ('BOX', (0, 0), (-1, -1), 0.5, BORDER_GRAY),
            ('LINEBELOW', (0, 0), (-1, -2), 0.5, BORDER_GRAY),
        ]))
        elements.append(bars_table)
        elements.append(Spacer(1, 16))

    # Section summaries only (no per-criterion detail tables)
    for cat_name, cat_data in categories.items():
        score = cat_data.get("score", 0)
        sc = GREEN if score >= 0.7 else AMBER if score >= 0.4 else RED
        cat_elements = [
            Paragraph(
                f'{cat_name} — <font color="{sc.hexval()}">{round(score * 100)}%</font>',
                styles['CatHead']
            )
        ]
        summary = cat_data.get("summary", "")
        if summary:
            cat_elements.append(Paragraph(summary, styles['BodyText2']))
        elements.append(KeepTogether(cat_elements))
        elements.append(Spacer(1, 8))

    # Statute concerns + recommendation (full review only)
    if not scorecard_only:
        statute_concerns = analysis_results.get("statute_concerns", [])
        if statute_concerns:
            elements.append(Paragraph("Statutory Compliance Notes", styles['SectionHead']))
            elements.append(HRFlowable(width="100%", thickness=1, color=BORDER_GRAY, spaceAfter=8))
            for concern in statute_concerns:
                elements.append(Paragraph(f"• {concern}", styles['BodyText2']))

        recommendation = analysis_results.get("overall_recommendation", "")
        if recommendation and not str(recommendation).startswith('{'):
            elements.append(Spacer(1, 12))
            elements.append(Paragraph("Recommendation", styles['SectionHead']))
            elements.append(HRFlowable(width="100%", thickness=1, color=BORDER_GRAY, spaceAfter=8))
            elements.append(Paragraph(recommendation, styles['BodyText2']))

    # Footer
    elements.append(Spacer(1, 30))
    elements.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_GRAY, spaceAfter=6))
    elements.append(Paragraph(
        "This scorecard is provided for informational purposes and does not constitute legal advice.",
        styles['SmallText']
    ))

    doc.build(elements)
    return output_path
