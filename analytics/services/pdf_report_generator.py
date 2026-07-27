import io
import xml.sax.saxutils as saxutils
from decimal import Decimal
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter

class NumberedCanvas(canvas.Canvas):
    drawn_instances = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []
        self.pages_decorated = []
        NumberedCanvas.drawn_instances.append(self)

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_decorations(self, page_count):
        if self._pageNumber == 1:
            # Skip cover page decoration
            return
            
        self.saveState()
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor("#64748b")) # slate-500
        
        # Running Header
        self.drawString(40, 755, "CloudCost Detective AI — Executive Cost Report")
        self.setStrokeColor(colors.HexColor("#cbd5e1")) # slate-300
        self.setLineWidth(0.5)
        self.line(40, 747, 572, 747)
        
        # Running Footer
        page_text = f"Page {self._pageNumber} of {page_count}"
        self.drawRightString(572, 35, page_text)
        self.drawString(40, 35, "CONFIDENTIAL — For Internal FinOps Review Only")
        self.line(40, 48, 572, 48)
        
        self.restoreState()
        
        self.pages_decorated.append({
            "page_num": self._pageNumber,
            "page_count": page_count,
            "page_text": page_text
        })

def clean_txt(val):
    """Safely escape XML characters to prevent ReportLab parsing crashes."""
    if val is None:
        return ""
    # Strip any potential tags and escape &, <, >, ", '
    s = str(val).strip()
    return saxutils.escape(s)

def generate_pdf_report(context):
    """
    Renders the aggregated context dictionary into a PDF byte stream in memory.
    """
    buffer = io.BytesIO()
    
    # 532 pt of printable width on standard letter size with 40pt margins
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=40,
        rightMargin=40,
        topMargin=55,
        bottomMargin=55
    )
    
    styles = getSampleStyleSheet()
    
    # Core Custom Styles
    style_cover_title = ParagraphStyle(
        "CoverTitle",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=28,
        leading=34,
        textColor=colors.HexColor("#0f172a"),
        alignment=1, # Center
        spaceAfter=15
    )
    
    style_cover_subtitle = ParagraphStyle(
        "CoverSubtitle",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=14,
        leading=18,
        textColor=colors.HexColor("#475569"),
        alignment=1,
        spaceAfter=30
    )
    
    style_cover_meta = ParagraphStyle(
        "CoverMeta",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#334155"),
        alignment=1
    )
    
    style_cover_disclaimer = ParagraphStyle(
        "CoverDisclaimer",
        parent=styles["Normal"],
        fontName="Helvetica-Oblique",
        fontSize=9,
        leading=13,
        textColor=colors.HexColor("#64748b"),
        alignment=1,
        spaceBefore=100
    )
    
    style_h1 = ParagraphStyle(
        "SectionHeading",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=16,
        leading=20,
        textColor=colors.HexColor("#1e3a8a"), # Navy
        spaceBefore=18,
        spaceAfter=10,
        keepWithNext=True
    )
    
    style_h2 = ParagraphStyle(
        "SubSectionHeading",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=12,
        leading=16,
        textColor=colors.HexColor("#0f172a"),
        spaceBefore=12,
        spaceAfter=6,
        keepWithNext=True
    )
    
    style_body = ParagraphStyle(
        "BodyTextCustom",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=13,
        textColor=colors.HexColor("#334155")
    )
    
    style_body_bold = ParagraphStyle(
        "BodyTextBoldCustom",
        parent=style_body,
        fontName="Helvetica-Bold"
    )
    
    style_empty = ParagraphStyle(
        "EmptyStateText",
        parent=styles["Normal"],
        fontName="Helvetica-Oblique",
        fontSize=9.5,
        leading=14,
        textColor=colors.HexColor("#64748b"),
        spaceBefore=6,
        spaceAfter=6
    )
    
    style_warning = ParagraphStyle(
        "WarningText",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8.5,
        leading=12,
        textColor=colors.HexColor("#b45309") # amber-700
    )
    
    style_th = ParagraphStyle(
        "TableHeader",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8.5,
        leading=11,
        textColor=colors.white
    )
    
    style_td = ParagraphStyle(
        "TableCell",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#1e293b")
    )

    style_td_bold = ParagraphStyle(
        "TableCellBold",
        parent=style_td,
        fontName="Helvetica-Bold"
    )

    story = []
    
    # -------------------------------------------------------------------------
    # 1. COVER PAGE
    # -------------------------------------------------------------------------
    story.append(Spacer(1, 120))
    story.append(Paragraph("CloudCost Detective AI", style_cover_title))
    story.append(Paragraph("Executive Cloud Cost Report", style_cover_subtitle))
    
    story.append(Spacer(1, 40))
    
    meta_html = (
        f"<b>Reporting Period:</b> {context['start_date']} to {context['end_date']}<br/>"
        f"<b>Generated On:</b> {context['generated_at'].strftime('%Y-%m-%d %H:%M:%S UTC')}<br/>"
    )
    story.append(Paragraph(meta_html, style_cover_meta))
    
    story.append(Paragraph(
        "Generated from historical OCI billing data available in CloudCost Detective AI.",
        style_cover_disclaimer
    ))
    story.append(PageBreak())
    
    # -------------------------------------------------------------------------
    # 2. EXECUTIVE SUMMARY
    # -------------------------------------------------------------------------
    story.append(Paragraph("1. Executive Summary", style_h1))
    
    # Summary Metrics Table
    summary_data = [
        [Paragraph("Executive KPI", style_th), Paragraph("Value", style_th)],
        [Paragraph("Billing Records Analyzed", style_td), Paragraph(str(context["total_records_count"]), style_td_bold)],
        [Paragraph("Unique Resources Tracked", style_td), Paragraph(str(context["unique_resources_count"]), style_td_bold)],
        [Paragraph("Services Tracked", style_td), Paragraph(str(context["services_tracked_count"]), style_td_bold)],
        [Paragraph("Regions Tracked", style_td), Paragraph(str(context["regions_tracked_count"]), style_td_bold)],
        [Paragraph("Open Cost Anomalies", style_td), Paragraph(str(context["open_anomalies_count"]), style_td_bold)],
        [Paragraph("Open Waste Findings", style_td), Paragraph(str(context["open_waste_count"]), style_td_bold)],
        [Paragraph("Open Optimization Recommendations", style_td), Paragraph(str(context["open_recommendations_count"]), style_td_bold)]
    ]
    
    t_summary = Table(summary_data, colWidths=[280, 252])
    t_summary.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (1, 0), colors.HexColor("#1e3a8a")),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")])
    ]))
    story.append(t_summary)
    story.append(Spacer(1, 15))
    
    # Financial Summary per currency
    story.append(Paragraph("Financial Totals by Currency", style_h2))
    
    if not context["total_costs"]:
        story.append(Paragraph("No billing records were available for the selected reporting period.", style_empty))
    else:
        financials_data = [
            [Paragraph("Currency", style_th), Paragraph("Total Cost", style_th), Paragraph("Deduplicated Open Savings KPI", style_th)]
        ]
        for curr, cost in context["total_costs"].items():
            savings = context["potential_savings"].get(curr, Decimal("0.00"))
            financials_data.append([
                Paragraph(curr, style_td_bold),
                Paragraph(f"{cost:.2f}", style_td_bold),
                Paragraph(f"{savings:.2f}", style_td_bold)
            ])
            
        t_financials = Table(financials_data, colWidths=[120, 200, 212])
        t_financials.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#334155")),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('TOPPADDING', (0, 0), (-1, -1), 5)
        ]))
        story.append(t_financials)
        
    story.append(Spacer(1, 20))
    
    # -------------------------------------------------------------------------
    # 3. COST BY SERVICE
    # -------------------------------------------------------------------------
    if "cost_breakdown" in context["enabled_sections"]:
        story.append(Paragraph("2. Cost by Service", style_h1))
        
        if not context["services_breakdown"]:
            story.append(Paragraph("No service data available for the selected period.", style_empty))
        else:
            for curr, svc_list in context["services_breakdown"].items():
                story.append(Paragraph(f"Currency: {curr}", style_h2))
                
                table_rows = [
                    [Paragraph("Service", style_th), Paragraph("Cost", style_th), Paragraph("Percentage of Same-Currency Total", style_th)]
                ]
                for item in svc_list:
                    table_rows.append([
                        Paragraph(clean_txt(item["service"]), style_td),
                        Paragraph(f"{item['cost']:.2f}", style_td),
                        Paragraph(f"{item['percentage']:.2f}%", style_td)
                    ])
                    
                t_svc = Table(table_rows, colWidths=[220, 150, 162])
                t_svc.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#0284c7")),
                    ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                    ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                    ('TOPPADDING', (0, 0), (-1, -1), 4),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")])
                ]))
                story.append(t_svc)
                story.append(Spacer(1, 10))
                
        story.append(Spacer(1, 15))
        
    # -------------------------------------------------------------------------
    # 4. COST BY REGION
    # -------------------------------------------------------------------------
    if "cost_breakdown" in context["enabled_sections"]:
        story.append(Paragraph("3. Cost by Region", style_h1))
        
        if not context["regions_breakdown"]:
            story.append(Paragraph("No region data available for the selected period.", style_empty))
        else:
            for curr, reg_list in context["regions_breakdown"].items():
                story.append(Paragraph(f"Currency: {curr}", style_h2))
                
                table_rows = [
                    [Paragraph("Region", style_th), Paragraph("Cost", style_th), Paragraph("Percentage of Same-Currency Total", style_th)]
                ]
                for item in reg_list:
                    table_rows.append([
                        Paragraph(clean_txt(item["region"]), style_td),
                        Paragraph(f"{item['cost']:.2f}", style_td),
                        Paragraph(f"{item['percentage']:.2f}%", style_td)
                    ])
                    
                t_reg = Table(table_rows, colWidths=[220, 150, 162])
                t_reg.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#0d9488")),
                    ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                    ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                    ('TOPPADDING', (0, 0), (-1, -1), 4),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")])
                ]))
                story.append(t_reg)
                story.append(Spacer(1, 10))
                
        story.append(Spacer(1, 15))
        
    # -------------------------------------------------------------------------
    # 5. TOP EXPENSIVE RESOURCES
    # -------------------------------------------------------------------------
    if "cost_breakdown" in context["enabled_sections"]:
        story.append(Paragraph("4. Most Expensive Resources", style_h1))
        
        if not context["top_resources"]:
            story.append(Paragraph("No resource billing data available.", style_empty))
        else:
            for curr, res_list in context["top_resources"].items():
                story.append(Paragraph(f"Currency: {curr}", style_h2))
                
                table_rows = [
                    [Paragraph("Resource ID / Identity", style_th), Paragraph("Service", style_th), Paragraph("Region", style_th), Paragraph("Cost", style_th)]
                ]
                for item in res_list:
                    table_rows.append([
                        Paragraph(clean_txt(item["resource"]), style_td),
                        Paragraph(clean_txt(item["service"]), style_td),
                        Paragraph(clean_txt(item["region"]), style_td),
                        Paragraph(f"{item['cost']:.2f}", style_td)
                    ])
                    
                t_res = Table(table_rows, colWidths=[180, 120, 120, 112])
                t_res.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#4f46e5")),
                    ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                    ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                    ('TOPPADDING', (0, 0), (-1, -1), 4),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")])
                ]))
                story.append(t_res)
                story.append(Spacer(1, 10))
                
        story.append(Spacer(1, 15))
        
    # -------------------------------------------------------------------------
    # 6. COST ANOMALIES
    # -------------------------------------------------------------------------
    if "anomalies" in context["enabled_sections"]:
        story.append(Paragraph("5. Cost Anomalies Detected", style_h1))
        
        if not context["anomalies"]:
            story.append(Paragraph("No cost anomalies were detected during the selected reporting period.", style_empty))
        else:
            table_rows = [
                [
                    Paragraph("Detected Date", style_th), Paragraph("Type", style_th),
                    Paragraph("Service", style_th), Paragraph("Actual Cost", style_th),
                    Paragraph("Expected Cost", style_th), Paragraph("Severity", style_th)
                ]
            ]
            for item in context["anomalies"]:
                table_rows.append([
                    Paragraph(str(item.detected_date), style_td),
                    Paragraph(item.get_anomaly_type_display(), style_td),
                    Paragraph(clean_txt(item.service_name), style_td),
                    Paragraph(f"{item.actual_cost:.2f}", style_td),
                    Paragraph(f"{item.expected_cost:.2f}", style_td),
                    Paragraph(item.get_severity_display(), style_td_bold)
                ])
                
            t_anom = Table(table_rows, colWidths=[75, 110, 110, 80, 80, 77])
            t_anom.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#b91c1c")),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")])
            ]))
            story.append(t_anom)
            
        story.append(Spacer(1, 15))
        
    # -------------------------------------------------------------------------
    # 7. WASTE / OPTIMIZATION FINDINGS
    # -------------------------------------------------------------------------
    if "waste" in context["enabled_sections"]:
        story.append(Paragraph("6. Resource Waste & Optimization Findings", style_h1))
        
        if not context["waste_findings"]:
            story.append(Paragraph("No resource waste findings were identified for the selected reporting period.", style_empty))
        else:
            table_rows = [
                [
                    Paragraph("Optimization Candidate", style_th), Paragraph("Service", style_th),
                    Paragraph("Waste Pattern", style_th), Paragraph("Confidence", style_th),
                    Paragraph("Monthly Savings", style_th)
                ]
            ]
            waste_type_map = {
                "PERSISTENT_LOW_COST_RESOURCE": "Persistent Low-Cost Resource",
                "DORMANT_COST_PATTERN": "Dormant Cost Pattern",
                "STALE_RESOURCE_COST": "Stale Resource Cost",
                "POSSIBLE_UNUSED_STORAGE": "Possible Unused Storage",
            }
            for item in context["waste_findings"]:
                res_key = item.resource_name or item.resource_id or "Unknown Resource"
                waste_display = waste_type_map.get(item.waste_type, item.waste_type)
                table_rows.append([
                    Paragraph(clean_txt(res_key), style_td),
                    Paragraph(clean_txt(item.service_name), style_td),
                    Paragraph(clean_txt(waste_display), style_td),
                    Paragraph(item.get_confidence_display(), style_td),
                    Paragraph(f"{item.estimated_monthly_savings:.2f} {item.currency}", style_td_bold)
                ])
                
            t_waste = Table(table_rows, colWidths=[130, 95, 130, 77, 100])
            t_waste.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#d97706")),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")])
            ]))
            story.append(t_waste)
            
        story.append(Spacer(1, 15))
        
    # -------------------------------------------------------------------------
    # 8. RECOMMENDATIONS
    # -------------------------------------------------------------------------
    if "recommendations" in context["enabled_sections"]:
        story.append(Paragraph("7. Cloud Cost Recommendations", style_h1))
        
        if not context["recommendations"]:
            story.append(Paragraph("No cost recommendations were generated during this reporting period.", style_empty))
        else:
            table_rows = [
                [
                    Paragraph("Recommendation", style_th), Paragraph("Target Scope", style_th),
                    Paragraph("Priority", style_th), Paragraph("Action Strategy", style_th),
                    Paragraph("Est. Savings", style_th)
                ]
            ]
            for item in context["recommendations"]:
                target = item.service_name
                if item.resource_id or item.resource_name:
                    target = item.resource_name or item.resource_id
                
                savings_txt = "Not estimated"
                if item.estimated_monthly_savings is not None:
                    savings_txt = f"{item.estimated_monthly_savings:.2f} {item.currency}"
                    
                table_rows.append([
                    Paragraph(item.get_recommendation_type_display(), style_td),
                    Paragraph(clean_txt(target), style_td),
                    Paragraph(item.get_priority_display(), style_td_bold),
                    Paragraph(clean_txt(item.recommended_action), style_td),
                    Paragraph(savings_txt, style_td_bold)
                ])
                
            t_rec = Table(table_rows, colWidths=[110, 100, 52, 180, 90])
            t_rec.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#059669")),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")])
            ]))
            story.append(t_rec)
            
        story.append(Spacer(1, 15))
        
    # -------------------------------------------------------------------------
    # 9. SAVINGS SUMMARY
    # -------------------------------------------------------------------------
    if "recommendations" in context["enabled_sections"] or "waste" in context["enabled_sections"]:
        story.append(Paragraph("8. Savings Summary & Deduplication Methodology", style_h1))
        
        summary_text = (
            "Savings KPIs are calculated using a strict deduplication methodology. "
            "Optimization recommendations that derive from existing resource waste findings "
            "are tracked via a linked source mapping. To ensure financial report integrity, "
            "savings from these overlapping findings are counted only once. "
            "Totals are separated strictly by currency and are never mixed."
        )
        story.append(Paragraph(summary_text, style_body))
        story.append(Spacer(1, 8))
        
        if not context["potential_savings"]:
            story.append(Paragraph("No potential savings were identified for this period.", style_empty))
        else:
            savings_data = [
                [Paragraph("Currency", style_th), Paragraph("Deduplicated Potential Monthly Savings", style_th)]
            ]
            for curr, val in context["potential_savings"].items():
                savings_data.append([
                    Paragraph(curr, style_td_bold),
                    Paragraph(f"{val:.2f} {curr}", style_td_bold)
                ])
            t_sav = Table(savings_data, colWidths=[150, 382])
            t_sav.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#475569")),
                ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
                ('TOPPADDING', (0, 0), (-1, -1), 5)
            ]))
            story.append(t_sav)
            
        story.append(Spacer(1, 15))
        
    # -------------------------------------------------------------------------
    # 10. MONTHLY COST FORECAST
    # -------------------------------------------------------------------------
    if "forecast" in context["enabled_sections"]:
        story.append(Paragraph("9. Monthly Spend Forecast", style_h1))
        
        if not context["forecast_results"]:
            story.append(Paragraph("No historical billing trends available.", style_empty))
        else:
            for curr, res in context["forecast_results"].items():
                story.append(Paragraph(f"Currency: {curr}", style_h2))
                
                if not res.get("forecast_available"):
                    reason = clean_txt(res.get("reason", "Forecast unavailable."))
                    story.append(Paragraph(f"Forecast unavailable: {reason}", style_empty))
                    story.append(Spacer(1, 5))
                    continue
                    
                table_rows = [
                    [
                        Paragraph("Metric", style_th), Paragraph("Value", style_th), Paragraph("Notes", style_th)
                    ],
                    [
                        Paragraph("Next Month Forecast", style_td),
                        Paragraph(f"{res['next_month_forecast']:.2f}", style_td_bold),
                        Paragraph("Deterministic linear trend estimate", style_td)
                    ],
                    [
                        Paragraph("3-Month Forecast", style_td),
                        Paragraph(f"{res['three_month_forecast']:.2f}", style_td_bold),
                        Paragraph("Cumulative trend projections", style_td)
                    ],
                    [
                        Paragraph("6-Month Forecast", style_td),
                        Paragraph(f"{res['six_month_forecast']:.2f}", style_td_bold),
                        Paragraph("Projections assuming workload stability", style_td)
                    ],
                    [
                        Paragraph("Prediction Confidence", style_td),
                        Paragraph(res["confidence"], style_td_bold),
                        Paragraph("Historical data stability classification", style_td)
                    ],
                    [
                        Paragraph("Coverage Ratio", style_td),
                        Paragraph(f"{res['coverage_ratio']:.2f}", style_td_bold),
                        Paragraph("Data span completion ratio", style_td)
                    ]
                ]
                
                t_fore = Table(table_rows, colWidths=[150, 100, 282])
                t_fore.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#0f172a")),
                    ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                    ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
                    ('TOPPADDING', (0, 0), (-1, -1), 5),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")])
                ]))
                story.append(t_fore)
                story.append(Spacer(1, 10))
                
        story.append(Spacer(1, 15))
        
    # -------------------------------------------------------------------------
    # 11. DATA QUALITY & LIMITATIONS
    # -------------------------------------------------------------------------
    story.append(Paragraph("10. Data Quality Warnings & Limitations", style_h1))
    
    # Render warnings if any
    if context["warnings"]:
        story.append(Paragraph("Quality Alerts:", style_h2))
        warning_story = []
        for w in context["warnings"]:
            warning_story.append([
                Paragraph("•", style_warning), Paragraph(clean_txt(w), style_warning)
            ])
        t_warn = Table(warning_story, colWidths=[15, 517])
        t_warn.setStyle(TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('TOPPADDING', (0, 0), (-1, -1), 3)
        ]))
        story.append(t_warn)
        story.append(Spacer(1, 10))
        
    disclaimer_text = (
        "<b>Important Limitations and Disclaimers:</b><br/>"
        "1. Report calculations depend entirely on OCI CSV billing reports uploaded by the user.<br/>"
        "2. Billing patterns do not serve as proof of hardware utilization. Workloads must be validated at runtime.<br/>"
        "3. Trend forecasts are mathematical estimations. Volatile patterns may skew projections.<br/>"
        "4. Recommendations should be validated by infrastructure and engineering leads prior to OCI changes.<br/>"
        "5. No automated cloud resource changes are performed or triggered by generating this report.<br/>"
        "6. Financial currency conversions are not applied; all totals represent absolute OCI values."
    )
    story.append(Paragraph(disclaimer_text, style_body))
    
    # -------------------------------------------------------------------------
    # BUILD DOCUMENT
    # -------------------------------------------------------------------------
    doc.build(story, canvasmaker=NumberedCanvas)
    
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes
