"""Generate professional PDF scorecard using ReportLab."""

import os

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, Image, KeepTogether,
)
from reportlab.lib.enums import TA_CENTER
from datetime import datetime

import config


# Colors
NAVY = colors.HexColor('#003366')
GREEN = colors.HexColor('#28a745')
AMBER = colors.HexColor('#cc9900')
RED = colors.HexColor('#c00000')
LIGHT_GRAY = colors.HexColor('#f0f4f8')
BORDER_GRAY = colors.HexColor('#dee2e6')


def _logo_path():
    path = getattr(config, "LOGO_PATH", None) or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "assets",
        "cam-leadership-institute-logo.png",
    )
    return path if os.path.exists(path) else None


def generate_scorecard_pdf(analysis_results, client_name, output_path, jurisdiction=None):
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
        jurisdiction_text = f" &nbsp;|&nbsp; Jurisdiction: {jurisdiction['state']} ({jurisdiction.get('state_abbrev', '')})"
    elements.append(Paragraph(
        f"Client: <b>{client_name}</b> &nbsp;|&nbsp; Review Date: <b>{review_date}</b>{jurisdiction_text}",
        styles['SubTitle']
    ))

    elements.append(HRFlowable(width="100%", thickness=2, color=NAVY, spaceAfter=20))

    # Overall Score
    overall_score = analysis_results.get("overall_score", 0)
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

    # Section summaries only (no category summary table, no per-criterion details)
    categories = analysis_results.get("categories", {})
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

    # Statute Concerns
    statute_concerns = analysis_results.get("statute_concerns", [])
    if statute_concerns:
        elements.append(Paragraph("Statutory Compliance Notes", styles['SectionHead']))
        elements.append(HRFlowable(width="100%", thickness=1, color=BORDER_GRAY, spaceAfter=8))
        for concern in statute_concerns:
            elements.append(Paragraph(f"• {concern}", styles['BodyText2']))

    # Recommendation
    recommendation = analysis_results.get("overall_recommendation", "")
    if recommendation and not recommendation.startswith('{'):
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
