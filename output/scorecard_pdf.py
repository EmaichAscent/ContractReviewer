"""Generate professional PDF scorecard using ReportLab."""

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether
)
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from datetime import datetime


# Colors
NAVY = colors.HexColor('#003366')
GREEN = colors.HexColor('#28a745')
AMBER = colors.HexColor('#cc9900')
RED = colors.HexColor('#c00000')
LIGHT_GRAY = colors.HexColor('#f0f4f8')
BORDER_GRAY = colors.HexColor('#dee2e6')


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
    styles.add(ParagraphStyle(
        'CritName', parent=styles['Normal'],
        fontSize=10, textColor=NAVY, leading=12,
    ))
    styles.add(ParagraphStyle(
        'CritDetail', parent=styles['Normal'],
        fontSize=9, textColor=colors.HexColor('#555555'), leading=12, leftIndent=10,
    ))
    styles.add(ParagraphStyle(
        'Revision', parent=styles['Normal'],
        fontSize=9, textColor=colors.HexColor('#856404'), leading=12,
        leftIndent=10, backColor=colors.HexColor('#fff3cd'),
    ))

    elements = []

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

    # Category Score Summary Table
    categories = analysis_results.get("categories", {})
    summary_data = [
        [Paragraph('<b>Evaluation Area</b>', styles['CritName']),
         Paragraph('<b>Score</b>', styles['CritName']),
         Paragraph('<b>Rating</b>', styles['CritName'])]
    ]
    for cat_name, cat_data in categories.items():
        score = cat_data.get("score", 0)
        pct = round(score * 100)
        sc = GREEN if score >= 0.7 else AMBER if score >= 0.4 else RED
        if score >= 0.8:
            r = "Strong"
        elif score >= 0.6:
            r = "Adequate"
        elif score >= 0.4:
            r = "Needs Improvement"
        else:
            r = "Weak"
        summary_data.append([
            Paragraph(cat_name, styles['BodyText2']),
            Paragraph(f'<font color="{sc.hexval()}"><b>{pct}%</b></font>', styles['BodyText2']),
            Paragraph(r, styles['BodyText2']),
        ])

    summary_table = Table(summary_data, colWidths=[3 * inch, 1.5 * inch, 2 * inch])
    summary_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), NAVY),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTSIZE', (0, 0), (-1, 0), 11),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
        ('TOPPADDING', (0, 0), (-1, 0), 8),
        ('BACKGROUND', (0, 1), (-1, -1), colors.white),
        ('GRID', (0, 0), (-1, -1), 0.5, BORDER_GRAY),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, LIGHT_GRAY]),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 1), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 6),
    ]))
    elements.append(summary_table)
    elements.append(Spacer(1, 20))

    # Detailed Category Sections
    elements.append(Paragraph("Evaluation Details", styles['SectionHead']))
    elements.append(HRFlowable(width="100%", thickness=1, color=BORDER_GRAY, spaceAfter=8))

    for cat_name, cat_data in categories.items():
        score = cat_data.get("score", 0)
        sc = GREEN if score >= 0.7 else AMBER if score >= 0.4 else RED
        cat_elements = []

        cat_elements.append(Paragraph(
            f'{cat_name} — <font color="{sc.hexval()}">{round(score * 100)}%</font>',
            styles['CatHead']
        ))

        summary = cat_data.get("summary", "")
        if summary:
            cat_elements.append(Paragraph(summary, styles['BodyText2']))

        criteria = cat_data.get("criteria", {})
        for crit_id, crit in criteria.items():
            if not isinstance(crit, dict):
                continue
            crit_score = crit.get("score", 0)
            if crit_score == 2:
                badge = '<font color="#155724" backColor="#d4edda">&nbsp;STRONG&nbsp;</font>'
            elif crit_score == 1:
                badge = '<font color="#856404" backColor="#fff3cd">&nbsp;WEAK&nbsp;</font>'
            else:
                badge = '<font color="#721c24" backColor="#f8d7da">&nbsp;MISSING&nbsp;</font>'

            clean_name = crit_id.replace('profit_', '').replace('empower_', '').replace(
                'risk_', '').replace('board_', '').replace('value_', '').replace('_', ' ').title()

            cat_elements.append(Paragraph(
                f'{badge} &nbsp; <b>{clean_name}</b>', styles['CritName']
            ))
            if crit.get("explanation"):
                cat_elements.append(Paragraph(crit["explanation"], styles['CritDetail']))
            if crit.get("suggested_revision") and crit_score < 2:
                rev_text = crit["suggested_revision"]
                if len(rev_text) > 300:
                    rev_text = rev_text[:297] + "..."
                cat_elements.append(Paragraph(
                    f'<b>Recommended:</b> {rev_text}', styles['Revision']
                ))
            cat_elements.append(Spacer(1, 4))

        elements.append(KeepTogether(cat_elements[:3]))  # Keep header + summary together
        elements.extend(cat_elements[3:] if len(cat_elements) > 3 else [])
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
