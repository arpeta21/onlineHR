import os
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

# 16:9 Landscape Dimensions (10 x 5.625 inches = 720 x 405 pt)
PAGE_WIDTH = 720
PAGE_HEIGHT = 405
PAGE_SIZE = (PAGE_WIDTH, PAGE_HEIGHT)

class NumberedCanvas(canvas.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_page_decorations(self, page_count):
        self.saveState()
        # Header accent bar
        self.setFillColor(colors.HexColor("#1E40AF")) # Primary Navy Blue
        self.rect(0, PAGE_HEIGHT - 6, PAGE_WIDTH, 6, fill=True, stroke=False)
        
        # Footer accent line
        self.setStrokeColor(colors.HexColor("#E2E8F0"))
        self.setLineWidth(0.75)
        self.line(30, 25, PAGE_WIDTH - 30, 25)

        # Footer Text
        self.setFont("Helvetica-Bold", 8)
        self.setFillColor(colors.HexColor("#1E40AF"))
        self.drawString(30, 12, "SANVIT HRMS")
        
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor("#64748B"))
        self.drawString(95, 12, "|  In Collaboration with Talent Bridge HR Services")
        
        page_str = f"Slide {self._pageNumber} of {page_count}"
        self.drawRightString(PAGE_WIDTH - 30, 12, page_str)
        self.restoreState()

def create_presentation_pdf(output_filename):
    doc = SimpleDocTemplate(
        output_filename,
        pagesize=PAGE_SIZE,
        leftMargin=30,
        rightMargin=30,
        topMargin=20,
        bottomMargin=35
    )

    styles = getSampleStyleSheet()

    # Custom Color Palette
    PRIMARY = colors.HexColor("#1E40AF")      # Deep Royal Blue
    DARK_TEXT = colors.HexColor("#0F172A")    # Slate 900
    MUTED_TEXT = colors.HexColor("#475569")   # Slate 600
    ACCENT_BLUE = colors.HexColor("#2563EB")  # Vivid Blue
    BG_LIGHT = colors.HexColor("#F8FAFC")     # Slate 50
    CARD_BG = colors.HexColor("#EFF6FF")      # Light Blue Card
    SECURITY_BG = colors.HexColor("#F0FDF4")  # Green Tint Security Card
    BORDER_COLOR = colors.HexColor("#CBD5E1") # Border Grey

    # Custom Typography Styles
    title_style = ParagraphStyle(
        'CoverTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=26,
        leading=32,
        textColor=PRIMARY,
        alignment=0,
        spaceAfter=8
    )

    subtitle_style = ParagraphStyle(
        'CoverSubTitle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=13,
        leading=18,
        textColor=MUTED_TEXT,
        spaceAfter=15
    )

    slide_heading = ParagraphStyle(
        'SlideHeading',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=18,
        leading=22,
        textColor=PRIMARY,
        spaceAfter=4
    )

    slide_subheading = ParagraphStyle(
        'SlideSubHeading',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=10,
        leading=13,
        textColor=MUTED_TEXT,
        spaceAfter=12
    )

    body_style = ParagraphStyle(
        'BodyDark',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9.5,
        leading=13.5,
        textColor=DARK_TEXT
    )

    bold_body_style = ParagraphStyle(
        'BoldBody',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=10,
        leading=14,
        textColor=PRIMARY
    )

    card_header_style = ParagraphStyle(
        'CardHeader',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=11,
        leading=14,
        textColor=PRIMARY,
        spaceAfter=4
    )

    card_text_style = ParagraphStyle(
        'CardText',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=12.5,
        textColor=DARK_TEXT
    )

    cta_heading_style = ParagraphStyle(
        'CTAHeading',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=18,
        leading=22,
        textColor=PRIMARY,
        alignment=1,
        spaceAfter=8
    )

    cta_body_style = ParagraphStyle(
        'CTABody',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=12,
        leading=16,
        textColor=DARK_TEXT,
        alignment=1,
        spaceAfter=12
    )

    email_box_style = ParagraphStyle(
        'EmailBox',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=14,
        leading=18,
        textColor=colors.HexColor("#1D4ED8"),
        alignment=1
    )

    story = []

    # ==========================================
    # SLIDE 1: COVER SLIDE
    # ==========================================
    story.append(Spacer(1, 20))
    badge_text = "<font color='#1E40AF'><b>TALENT BRIDGE HR SERVICES</b></font> &nbsp;&bull;&nbsp; PRODUCT BROCHURE"
    story.append(Paragraph(badge_text, ParagraphStyle('Badge', fontName='Helvetica-Bold', fontSize=9, textColor=PRIMARY, spaceAfter=12)))
    
    story.append(Paragraph("Experience the World of <b>Sanvit HRMS</b>", title_style))
    story.append(Paragraph("Next-Generation All-in-One HR Management Platform &nbsp;|&nbsp; Built for Seamless Growth & Enterprise Security", subtitle_style))
    story.append(Spacer(1, 10))

    # 3 Feature Highlight Boxes
    cover_cards = [
        [
            Paragraph("⚡ <b>Zero Friction HR</b>", card_header_style),
            Paragraph("Automate payroll, attendance, leave, performance & recruitment in one unified workspace.", card_text_style)
        ],
        [
            Paragraph("🔒 <b>Bank-Grade Security</b>", card_header_style),
            Paragraph("Multi-layer encryption, anti-hijacking protection, and confidential data vaults.", card_text_style)
        ],
        [
            Paragraph("📱 <b>Intuitive UI</b>", card_header_style),
            Paragraph("Designed for maximum simplicity and zero learning curve for employees and HR teams.", card_text_style)
        ]
    ]

    t1 = Table([[cover_cards[0], cover_cards[1], cover_cards[2]]], colWidths=[210, 210, 210])
    t1.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (0,0), CARD_BG),
        ('BACKGROUND', (1,0), (1,0), CARD_BG),
        ('BACKGROUND', (2,0), (2,0), CARD_BG),
        ('BOX', (0,0), (0,0), 1, colors.HexColor("#BFDBFE")),
        ('BOX', (1,0), (1,0), 1, colors.HexColor("#BFDBFE")),
        ('BOX', (2,0), (2,0), 1, colors.HexColor("#BFDBFE")),
        ('PADDING', (0,0), (-1,-1), 12),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 14),
    ]))
    story.append(t1)

    story.append(Spacer(1, 25))
    cta_prompt = Paragraph("<font color='#2563EB'><b>👉 Swipe / Click 'Next' to explore platform capabilities & security benefits</b></font>", ParagraphStyle('Prompt', fontName='Helvetica-Bold', fontSize=10, textColor=ACCENT_BLUE, alignment=1))
    story.append(cta_prompt)
    story.append(PageBreak())

    # ==========================================
    # SLIDE 2: THE PROBLEM & SOLUTION
    # ==========================================
    story.append(Paragraph("Why Upgrade to Sanvit HRMS?", slide_heading))
    story.append(Paragraph("Say Goodbye to HR Bottlenecks, Data Hijacking Risks & Manual Errors", slide_subheading))
    
    col_left = [
        Paragraph("❌ <b>Traditional HR Challenges</b>", ParagraphStyle('RedHead', fontName='Helvetica-Bold', fontSize=11, textColor=colors.HexColor("#DC2626"), spaceAfter=8)),
        Paragraph("• <b>Fragmented Spreadsheets:</b> Error-prone manual calculations causing payroll delays.", body_style),
        Spacer(1, 6),
        Paragraph("• <b>Data Hijacking & Leak Vulnerabilities:</b> Unsecured employee documents and financial records.", body_style),
        Spacer(1, 6),
        Paragraph("• <b>Attendance Disputes:</b> Lack of real-time visibility and approval audit trails.", body_style),
        Spacer(1, 6),
        Paragraph("• <b>Complex Compliance:</b> Missed statutory deadlines for PF, ESI, and TDS deductions.", body_style)
    ]

    col_right = [
        Paragraph("✅ <b>The Sanvit HRMS Advantage</b>", ParagraphStyle('GreenHead', fontName='Helvetica-Bold', fontSize=11, textColor=colors.HexColor("#16A34A"), spaceAfter=8)),
        Paragraph("• <b>100% Automated Workflows:</b> One-click payroll generation and instant payslip dispatch.", body_style),
        Spacer(1, 6),
        Paragraph("• <b>Enterprise Security Shield:</b> Encrypted database, strict session cookies & anti-hijacking protection.", body_style),
        Spacer(1, 6),
        Paragraph("• <b>Real-Time Self-Service:</b> Employees apply for leaves & access documents instantly.", body_style),
        Spacer(1, 6),
        Paragraph("• <b>Automated Tax Compliance:</b> Zero-error calculations for TDS, PF, ESI & statutory rules.", body_style)
    ]

    t2 = Table([[col_left, col_right]], colWidths=[320, 320])
    t2.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (0,0), colors.HexColor("#FEF2F2")),
        ('BACKGROUND', (1,0), (1,0), SECURITY_BG),
        ('BOX', (0,0), (0,0), 1, colors.HexColor("#FCA5A5")),
        ('BOX', (1,0), (1,0), 1, colors.HexColor("#86EFAC")),
        ('PADDING', (0,0), (-1,-1), 12),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
    ]))
    story.append(t2)
    story.append(PageBreak())

    # ==========================================
    # SLIDE 3: PLATFORM CORE MODULES
    # ==========================================
    story.append(Paragraph("Comprehensive HR Capabilities in One Platform", slide_heading))
    story.append(Paragraph("Modular, Scalable, and Built for Organizations of All Sizes", slide_subheading))

    mod1 = [
        Paragraph("⏱️ <b>Attendance & Leave</b>", card_header_style),
        Paragraph("Real-time check-ins, multi-tier approval workflows, custom leave rules & leave balance tracking.", card_text_style)
    ]
    mod2 = [
        Paragraph("💰 <b>Automated Payroll</b>", card_header_style),
        Paragraph("One-click salary processing, itemized PDF payslips, tax deductions (PF/ESI/TDS) & direct disbursement logs.", card_text_style)
    ]
    mod3 = [
        Paragraph("🎯 <b>Performance & KPIs</b>", card_header_style),
        Paragraph("360-degree feedback, appraisal review cycles, goal setting, rating metrics & promotion tracking.", card_text_style)
    ]
    mod4 = [
        Paragraph("🤝 <b>Recruitment & ATS</b>", card_header_style),
        Paragraph("Applicant tracking, candidate pipeline, interview scheduling, and seamless candidate-to-employee onboarding.", card_text_style)
    ]
    mod5 = [
        Paragraph("📁 <b>Document Management</b>", card_header_style),
        Paragraph("Centralized digital file storage for HR letters, identity proofs, contracts, and policy manuals.", card_text_style)
    ]
    mod6 = [
        Paragraph("📊 <b>Analytics & Reports</b>", card_header_style),
        Paragraph("Visual executive dashboards, headcounts, attrition metrics, attendance trends, and custom exports.", card_text_style)
    ]

    t3 = Table([
        [mod1, mod2, mod3],
        [mod4, mod5, mod6]
    ], colWidths=[210, 210, 210])
    t3.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), CARD_BG),
        ('BOX', (0,0), (0,0), 1, colors.HexColor("#BFDBFE")),
        ('BOX', (1,0), (1,0), 1, colors.HexColor("#BFDBFE")),
        ('BOX', (2,0), (2,0), 1, colors.HexColor("#BFDBFE")),
        ('BOX', (0,1), (0,1), 1, colors.HexColor("#BFDBFE")),
        ('BOX', (1,1), (1,1), 1, colors.HexColor("#BFDBFE")),
        ('BOX', (2,1), (2,1), 1, colors.HexColor("#BFDBFE")),
        ('PADDING', (0,0), (-1,-1), 10),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
    ]))
    story.append(t3)
    story.append(PageBreak())

    # ==========================================
    # SLIDE 4: SECURITY & ANTI-HIJACKING
    # ==========================================
    story.append(Paragraph("Enterprise-Grade Security & Anti-Hijacking Architecture", slide_heading))
    story.append(Paragraph("How Sanvit HRMS Guarantees Total Data Confidentiality & Protection Against Threats", slide_subheading))

    sec_cards = [
        [
            Paragraph("🛡️ <b>Session Anti-Hijacking</b>", card_header_style),
            Paragraph("HTTP-only cookies, strict SameSite lax controls, and automatic 1-hour session expiration block cookie theft and hijacking attempts.", card_text_style)
        ],
        [
            Paragraph("🔐 <b>Role-Based Access (RBAC)</b>", card_header_style),
            Paragraph("Strict permission boundaries ensure employees, managers, and admins only see data explicitly authorized for their role.", card_text_style)
        ],
        [
            Paragraph("🔒 <b>Data Confidentiality</b>", card_header_style),
            Paragraph("Encrypted storage for sensitive salary data, bank accounts, and personal identifiers. Zero unauthorized data exposure.", card_text_style)
        ],
        [
            Paragraph("⚡ <b>Threat Protection</b>", card_header_style),
            Paragraph("Active security headers (X-Frame-Options SAMEORIGIN, nosniff, XSS protection) shield the portal against injection attacks.", card_text_style)
        ]
    ]

    t4 = Table([
        [sec_cards[0], sec_cards[1]],
        [sec_cards[2], sec_cards[3]]
    ], colWidths=[320, 320])
    t4.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), SECURITY_BG),
        ('BOX', (0,0), (-1,-1), 1, colors.HexColor("#86EFAC")),
        ('PADDING', (0,0), (-1,-1), 12),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
    ]))
    story.append(t4)
    story.append(PageBreak())

    # ==========================================
    # SLIDE 5: CALL TO ACTION / REQUEST DEMO
    # ==========================================
    story.append(Spacer(1, 10))
    story.append(Paragraph("Ready to Transform Your HR Operations?", cta_heading_style))
    story.append(Paragraph("Experience Sanvit HRMS Live with a Customized Demo", cta_body_style))
    story.append(Spacer(1, 10))

    cta_content = [
        Paragraph("<font size=14 color='#1E40AF'><b>📅 Request Your Personal Live Demo</b></font>", ParagraphStyle('CTATitle', fontName='Helvetica-Bold', alignment=1, spaceAfter=8)),
        Paragraph("Get a guided walkthrough tailored to your team size, payroll needs, and security requirements.", ParagraphStyle('CTASub', fontName='Helvetica', fontSize=10, alignment=1, spaceAfter=14)),
        
        # Email Box Container
        Table([
            [Paragraph("<b>📩 Direct Demo Request Email:</b>", ParagraphStyle('E1', fontName='Helvetica-Bold', fontSize=11, textColor=PRIMARY, alignment=1))],
            [Paragraph("<b><font size=15 color='#1D4ED8'>arpeta26@gmail.com</font></b>", ParagraphStyle('E2', fontName='Helvetica-Bold', alignment=1))]
        ], colWidths=[550], style=[
            ('BACKGROUND', (0,0), (-1,-1), colors.HexColor("#FFFFFF")),
            ('BOX', (0,0), (-1,-1), 1.5, colors.HexColor("#2563EB")),
            ('PADDING', (0,0), (-1,-1), 10),
            ('ALIGN', (0,0), (-1,-1), 'CENTER')
        ]),
        
        Spacer(1, 12),
        Paragraph("<i>Our HR Solutions team will respond within 24 hours to schedule your live walkthrough.</i>", ParagraphStyle('FootNote', fontName='Helvetica-Oblique', fontSize=9, textColor=MUTED_TEXT, alignment=1))
    ]

    t_cta = Table([[cta_content]], colWidths=[620])
    t_cta.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (0,0), CARD_BG),
        ('BOX', (0,0), (0,0), 1.5, colors.HexColor("#93C5FD")),
        ('PADDING', (0,0), (-1,-1), 16),
        ('ALIGN', (0,0), (-1,-1), 'CENTER')
    ]))
    story.append(t_cta)

    doc.build(story, canvasmaker=NumberedCanvas)
    print(f"Successfully generated PDF Presentation at: {output_filename}")

if __name__ == "__main__":
    out_file1 = r"C:\Users\Arpeta\OneDrive\Desktop\hrms2\hrms\Sanvit_HRMS_LinkedIn_Brochure.pdf"
    out_file2 = r"C:\Users\Arpeta\OneDrive\Desktop\Saarthi EV\hrms\Sanvit_HRMS_LinkedIn_Brochure.pdf"
    create_presentation_pdf(out_file1)
    create_presentation_pdf(out_file2)
