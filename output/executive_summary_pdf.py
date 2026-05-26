"""Generate a 1-page executive summary PDF scorecard."""

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable,
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


def _score_color(score):
    return GREEN if score >= 0.7 else AMBER if score >= 0.4 else RED


def _rating_text(score):
    if score >= 0.8:
        return "Strong Agreement"
    elif score >= 0.6:
        return "Adequate \u2014 Some Improvements Recommended"
    elif score >= 0.4:
        return "Needs Significant Improvement"
    return "Major Revision or Replacement Recommended"


def _cat_rating(score):
    if score >= 0.8:
        return "Strong"
    elif score >= 0.6:
        return "Adequate"
    elif score >= 0.4:
        return "Needs Improvement"
    return "Weak"


def generate_executive_summary_pdf(analysis_results, client_name, output_path, jurisdiction=None):
    """Generate a concise 1-page executive summary PDF."""
    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        topMargin=0.6 * inch,
        bottomMargin=0.5 * inch,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
    )

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        'ExTitle', parent=styles['Title'],
        fontSize=20, textColor=NAVY, spaceAfter=4, alignment=TA_CENTER,
    ))
    styles.add(ParagraphStyle(
        'ExSubTitle', parent=styles['Normal'],
        fontSize=10, textColor=colors.gray, alignment=TA_CENTER, spaceAfter=12,
    ))
    styles.add(ParagraphStyle(
        'ExSection', parent=styles['Heading2'],
        fontSize=13, textColor=NAVY, spaceBefore=14, spaceAfter=6,
    ))
    styles.add(ParagraphStyle(
        'ExBody', parent=styles['BodyText'],
        fontSize=10, leading=14, spaceAfter=4,
    ))
    styles.add(ParagraphStyle(
        'ExBullet', parent=styles['BodyText'],
        fontSize=10, leading=14, spaceAfter=3, leftIndent=15,
        bulletIndent=5,
    ))
    styles.add(ParagraphStyle(
        'ExSmall', parent=styles['Normal'],
        fontSize=8, textColor=colors.gray, alignment=TA_CENTER,
    ))

    elements = []

    # Title
    elements.append(Paragraph("EXECUTIVE SUMMARY", styles['ExTitle']))

    # Client info line
    review_date = datetime.now().strftime("%B %d, %Y")
    jurisdiction_text = ""
    if jurisdiction and jurisdiction.get("state"):
        jurisdiction_text = f" &nbsp;|&nbsp; Jurisdiction: {jurisdiction['state']}"
    elements.append(Paragraph(
        f"Client: <b>{client_name}</b> &nbsp;|&nbsp; Date: <b>{review_date}</b>{jurisdiction_text}",
        styles['ExSubTitle']
    ))

    elements.append(HRFlowable(width="100%", thickness=2, color=NAVY, spaceAfter=14))

    # Overall Score box
    overall_score = analysis_results.get("overall_score", 0)
    overall_pct = round(overall_score * 100)
    sc = _score_color(overall_score)
    rating = _rating_text(overall_score)

    score_table = Table(
        [[Paragraph(f'<font size="32" color="{sc.hexval()}">{overall_pct}%</font>', styles['ExTitle']),
          Paragraph(f'<font size="16" color="{NAVY.hexval()}">Overall Score</font><br/>'
                    f'<font size="11" color="#666666">{rating}</font>', styles['ExBody'])]],
        colWidths=[2 * inch, 4.5 * inch],
    )
    score_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (0, 0), (0, 0), 'CENTER'),
        ('BACKGROUND', (0, 0), (-1, -1), LIGHT_GRAY),
        ('ROUNDEDCORNERS', [8, 8, 8, 8]),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
    ]))
    elements.append(score_table)
    elements.append(Spacer(1, 14))

    # Category Scores Table
    categories = analysis_results.get("categories", {})
    summary_data = [
        [Paragraph('<b>Evaluation Area</b>', styles['ExBody']),
         Paragraph('<b>Score</b>', styles['ExBody']),
         Paragraph('<b>Rating</b>', styles['ExBody'])]
    ]
    for cat_name, cat_data in categories.items():
        score = cat_data.get("score", 0)
        csc = _score_color(score)
        summary_data.append([
            Paragraph(cat_name, styles['ExBody']),
            Paragraph(f'<font color="{csc.hexval()}"><b>{round(score * 100)}%</b></font>', styles['ExBody']),
            Paragraph(_cat_rating(score), styles['ExBody']),
        ])

    summary_table = Table(summary_data, colWidths=[3.2 * inch, 1.3 * inch, 2 * inch])
    summary_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), NAVY),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
        ('TOPPADDING', (0, 0), (-1, 0), 6),
        ('BACKGROUND', (0, 1), (-1, -1), colors.white),
        ('GRID', (0, 0), (-1, -1), 0.5, BORDER_GRAY),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, LIGHT_GRAY]),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 1), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 4),
    ]))
    elements.append(summary_table)
    elements.append(Spacer(1, 14))

    # Key Findings — strengths and concerns
    strengths = []
    concerns = []
    for cat_name, cat_data in categories.items():
        for crit_id, crit in cat_data.get("criteria", {}).items():
            if not isinstance(crit, dict):
                continue
            crit_score = crit.get("score", 0)
            clean_name = crit_id.replace('profit_', '').replace('empower_', '').replace(
                'risk_', '').replace('board_', '').replace('value_', '').replace('_', ' ').title()
            if crit_score == 2 and crit.get("explanation"):
                strengths.append((clean_name, crit["explanation"]))
            elif crit_score < 2 and crit.get("explanation"):
                concerns.append((crit_score, clean_name, crit["explanation"]))

    # Sort concerns by score (worst first), take top items
    concerns.sort(key=lambda x: x[0])

    elements.append(Paragraph("Key Strengths", styles['ExSection']))
    if strengths:
        for name, expl in strengths[:4]:
            summary = expl[:120] + "..." if len(expl) > 120 else expl
            elements.append(Paragraph(
                f'\u2022 <b>{name}</b> \u2014 {summary}', styles['ExBullet']
            ))
    else:
        elements.append(Paragraph("No areas scored as strong.", styles['ExBody']))

    elements.append(Spacer(1, 6))

    elements.append(Paragraph("Key Concerns", styles['ExSection']))
    if concerns:
        for score, name, expl in concerns[:5]:
            summary = expl[:120] + "..." if len(expl) > 120 else expl
            label = "Missing" if score == 0 else "Weak"
            elements.append(Paragraph(
                f'\u2022 <b>{name}</b> ({label}) \u2014 {summary}', styles['ExBullet']
            ))
    else:
        elements.append(Paragraph("No significant concerns identified.", styles['ExBody']))

    # Structured executive summary (from analysis JSON's executive_summary
    # object — the five-section format the firm specified).
    exec_summary = analysis_results.get("executive_summary") or {}
    if exec_summary and isinstance(exec_summary, dict):
        # Risk Rating
        risk_rating = (exec_summary.get("risk_rating") or "").strip()
        risk_justification = (exec_summary.get("risk_justification") or "").strip()
        if risk_rating or risk_justification:
            elements.append(Spacer(1, 8))
            elements.append(Paragraph("Overall Risk Rating", styles['ExSection']))
            if risk_rating:
                rating_color = {
                    "low": GREEN, "moderate": AMBER, "high": RED,
                }.get(risk_rating.lower(), NAVY)
                elements.append(Paragraph(
                    f'<font size="13" color="{rating_color.hexval()}"><b>{risk_rating} Risk</b></font>',
                    styles['ExBody']
                ))
            if risk_justification:
                elements.append(Paragraph(risk_justification, styles['ExBody']))

        # Top 5 Priority Revisions
        priorities = exec_summary.get("top_priorities") or []
        if isinstance(priorities, list) and priorities:
            elements.append(Spacer(1, 6))
            elements.append(Paragraph("Top 5 Priority Revisions", styles['ExSection']))
            for i, item in enumerate(priorities[:5], start=1):
                if isinstance(item, str) and item.strip():
                    elements.append(Paragraph(
                        f'<b>{i}.</b> {item.strip()}', styles['ExBullet']
                    ))

        # Statutory Compliance Summary
        statute_summary = (exec_summary.get("statutory_compliance_summary") or "").strip()
        if statute_summary:
            elements.append(Spacer(1, 6))
            elements.append(Paragraph("Statutory Compliance Summary", styles['ExSection']))
            elements.append(Paragraph(statute_summary, styles['ExBody']))

        # Recommended Next Steps
        next_steps = (exec_summary.get("recommended_next_steps") or "").strip()
        if next_steps:
            elements.append(Spacer(1, 6))
            elements.append(Paragraph("Recommended Next Steps", styles['ExSection']))
            elements.append(Paragraph(next_steps, styles['ExBody']))
    else:
        # Fallback for older analyses that don't carry the executive_summary
        # object yet — render the prior recommendation paragraph if short.
        recommendation = analysis_results.get("overall_recommendation", "")
        if recommendation and not recommendation.startswith('{') and len(recommendation) < 500:
            elements.append(Spacer(1, 8))
            elements.append(Paragraph("Recommendation", styles['ExSection']))
            elements.append(Paragraph(recommendation, styles['ExBody']))

    # Footer
    elements.append(Spacer(1, 16))
    elements.append(HRFlowable(width="100%", thickness=0.5, color=BORDER_GRAY, spaceAfter=4))
    elements.append(Paragraph(
        "This summary is provided for informational purposes and does not constitute legal advice.",
        styles['ExSmall']
    ))

    doc.build(elements)
    return output_path
