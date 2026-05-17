"""
reports/views.py
Unified reporting module for Searing Sandwiches ERP.
"""
from __future__ import annotations
from io import BytesIO
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Sum, Count, Q
from django.http import HttpResponse
from django.shortcuts import render

from core.views import manager_required
from core.models import (Order, OrderItem, MenuItem, MenuCategory,
                          Ingredient, Employee, Expense,
                          StockLog, CostHistory, ActivityLog, DamagedStock)
from hr.models import AttendanceRecord, SalaryRun, SalaryLine, Advance
from purchasing.models import PurchaseOrder, PurchaseOrderLine, SupplierInvoice

# ── ReportLab ─────────────────────────────────────────────────────────────────
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import cm
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                 Paragraph, Spacer, HRFlowable)

# ── openpyxl ──────────────────────────────────────────────────────────────────
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

# ═══════════════════════════════════════════════════════════════════════════════
#  SHARED STYLE CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════════
ORANGE      = colors.HexColor('#FF4500')
LIGHT_ORG   = colors.HexColor('#FFF5F2')
GREY        = colors.HexColor('#6b7280')
LIGHT_GREY  = colors.HexColor('#f3f4f6')
MID_GREY    = colors.HexColor('#e5e7eb')

_OH  = 'FF4500'
_LH  = 'FFF5F2'
_HDR_FONT  = Font(bold=True, color='FFFFFF', size=9)
_HDR_FILL  = PatternFill('solid', fgColor=_OH)
_TOT_FILL  = PatternFill('solid', fgColor=_LH)
_TOT_FONT  = Font(bold=True, color=_OH, size=9)
_THIN      = Side(style='thin', color='E5E7EB')
_BDR       = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_AL_C      = Alignment(horizontal='center', vertical='center', wrap_text=True)
_AL_L      = Alignment(horizontal='left',   vertical='center', wrap_text=True)
_AL_R      = Alignment(horizontal='right',  vertical='center')


# ═══════════════════════════════════════════════════════════════════════════════
#  PDF HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
def _pdf_doc(buf, land=False):
    ps = landscape(A4) if land else A4
    return SimpleDocTemplate(buf, pagesize=ps,
        leftMargin=1.8*cm, rightMargin=1.8*cm,
        topMargin=1.5*cm,  bottomMargin=1.5*cm)

def _styles():
    b = ParagraphStyle('b8', fontName='Helvetica-Bold', fontSize=8)
    n = ParagraphStyle('n8', fontName='Helvetica',      fontSize=8)
    return b, n

def _pdf_header(story, title, subtitle='', period=''):
    b8, n8 = _styles()
    title_st = ParagraphStyle('T', fontName='Helvetica-Bold', fontSize=17,
                               textColor=ORANGE, spaceAfter=2)
    sub_st   = ParagraphStyle('S', fontName='Helvetica', fontSize=9,
                               textColor=GREY, spaceAfter=4)
    story.append(Paragraph('Searing Sandwiches', title_st))
    story.append(Paragraph(title,
        ParagraphStyle('T2', fontName='Helvetica-Bold', fontSize=12,
                       textColor=GREY, spaceAfter=2)))
    if period:   story.append(Paragraph(f'Period: {period}', sub_st))
    if subtitle: story.append(Paragraph(subtitle, sub_st))
    story.append(Spacer(1, 0.2*cm))
    story.append(HRFlowable(width='100%', thickness=1.5, color=ORANGE))
    story.append(Spacer(1, 0.3*cm))

def _base_ts():
    return TableStyle([
        ('BACKGROUND',    (0, 0), (-1,  0), ORANGE),
        ('TEXTCOLOR',     (0, 0), (-1,  0), colors.white),
        ('FONTNAME',      (0, 0), (-1,  0), 'Helvetica-Bold'),
        ('FONTNAME',      (0, 1), (-1, -1), 'Helvetica'),
        ('FONTSIZE',      (0, 0), (-1, -1), 8),
        ('ROWBACKGROUNDS',(0, 1), (-1, -1), [colors.white, LIGHT_GREY]),
        ('GRID',          (0, 0), (-1, -1), 0.3, MID_GREY),
        ('ALIGN',         (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING',    (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING',   (0, 0), (-1, -1), 5),
        ('RIGHTPADDING',  (0, 0), (-1, -1), 5),
    ])

def _totals_row_style(ts, row_idx):
    ts.add('BACKGROUND', (0, row_idx), (-1, row_idx), LIGHT_ORG)
    ts.add('LINEABOVE',  (0, row_idx), (-1, row_idx), 1.2, ORANGE)

def _pdf_response(buf, filename):
    buf.seek(0)
    r = HttpResponse(buf.read(), content_type='application/pdf')
    r['Content-Disposition'] = f'attachment; filename="{filename}"'
    return r


# ═══════════════════════════════════════════════════════════════════════════════
#  EXCEL HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
def _xl_response(wb, filename):
    buf = BytesIO()
    wb.save(buf); buf.seek(0)
    r = HttpResponse(buf.read(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    r['Content-Disposition'] = f'attachment; filename="{filename}"'
    return r

def _xl_title(ws, text, row=1):
    c = ws.cell(row=row, column=1, value=text)
    c.font = Font(bold=True, size=12, color=_OH)
    ws.row_dimensions[row].height = 20

def _xl_headers(ws, cols, row=2):
    for ci, (label, width) in enumerate(cols, 1):
        c = ws.cell(row=row, column=ci, value=label)
        c.font = _HDR_FONT; c.fill = _HDR_FILL
        c.alignment = _AL_C; c.border = _BDR
        ws.column_dimensions[get_column_letter(ci)].width = width
    ws.row_dimensions[row].height = 15

def _xl_cell(ws, row, col, value, bold=False, num_fmt=None,
             align=None, fill=None, font_color=None):
    c = ws.cell(row=row, column=col, value=value)
    c.border = _BDR
    c.alignment = align or _AL_L
    c.font = Font(bold=bold, size=9, color=(font_color or '000000'))
    if num_fmt: c.number_format = num_fmt
    if fill:    c.fill = fill
    return c

def _xl_total_row(ws, row, values, num_cols_right=None):
    for ci, v in enumerate(values, 1):
        is_num = isinstance(v, (int, float, Decimal))
        fmt  = '#,##0.00' if is_num and ci > 1 else None
        aln  = _AL_R if is_num else _AL_L
        _xl_cell(ws, row, ci, v, bold=True,
                 fill=_TOT_FILL, font_color=_OH,
                 num_fmt=fmt, align=aln)


# ═══════════════════════════════════════════════════════════════════════════════
#  DATE / QUERY HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
def _get_dates(request):
    df = request.GET.get('date_from', '') or request.POST.get('date_from', '')
    dt = request.GET.get('date_to',   '') or request.POST.get('date_to',   '')
    return df, dt

def _filter_by_date(qs, df, dt, field='created_at__date'):
    if df:
        try:
            qs = qs.filter(**{f'{field}__gte': df})
        except Exception:
            pass
    if dt:
        try:
            qs = qs.filter(**{f'{field}__lte': dt})
        except Exception:
            pass
    return qs

def _period_label(df, dt):
    if df and dt: return f'{df}  →  {dt}'
    if df:        return f'From {df}'
    if dt:        return f'Until {dt}'
    return 'All Time'

def _convert_to_stock_unit(qty, recipe_unit, stock_unit):
    q = float(qty)
    if recipe_unit == stock_unit:
        return q
    factors = {'g': 0.001, 'mL': 0.001, 'kg': 1.0, 'L': 1.0,
               'pieces': 1.0, 'packets': 1.0, 'cans': 1.0}
    return q * factors.get(recipe_unit, 1.0)


# ═══════════════════════════════════════════════════════════════════════════════
#  FINANCE DATA ENGINE
# ═══════════════════════════════════════════════════════════════════════════════
def _build_finance_general(df, dt):
    orders = Order.objects.filter(status='active').prefetch_related(
        'items__menu_item__ingredients__ingredient').order_by('created_at')
    orders = _filter_by_date(orders, df, dt)

    item_map   = {}
    total_rev  = Decimal('0')
    total_cogs = Decimal('0')
    n_orders   = 0

    for order in orders:
        n_orders += 1
        for oi in order.items.all():
            mi = oi.menu_item
            if not mi:
                continue
            mid = mi.id
            _snap = getattr(oi, 'cogs_at_sale', None)
            if _snap and _snap > 0:
                cogs_unit = _snap / oi.quantity
            else:
                cogs_unit = Decimal(str(sum(
                    _convert_to_stock_unit(r.quantity_required, r.recipe_unit, r.ingredient.unit)
                    * float(r.ingredient.cost_per_unit)
                    for r in mi.ingredients.all()
                )))
            if mid not in item_map:
                item_map[mid] = {
                    'name':     mi.name,
                    'category': mi.category.name if mi.category else '—',
                    'price':    float(oi.price_at_order),
                    'qty':      0,
                    'revenue':  Decimal('0'),
                    'cogs':     Decimal('0'),
                }
            qty = oi.quantity
            item_map[mid]['qty']     += qty
            item_map[mid]['revenue'] += oi.price_at_order * qty
            item_map[mid]['cogs']    += cogs_unit * qty
            total_rev  += oi.price_at_order * qty
            total_cogs += cogs_unit * qty

    expenses_qs  = _filter_by_date(Expense.objects.all(), df, dt, field='date')
    total_exp    = expenses_qs.aggregate(t=Sum('amount'))['t'] or Decimal('0')
    payroll      = (Employee.objects.filter(is_active=True)
                   .aggregate(t=Sum('salary'))['t'] or Decimal('0'))

    gross_profit = total_rev - total_cogs
    net_profit   = gross_profit - total_exp - payroll
    gross_margin = (gross_profit / total_rev * 100) if total_rev else Decimal('0')
    avg_order    = (total_rev / n_orders) if n_orders else Decimal('0')

    items_sorted = sorted(item_map.values(), key=lambda x: x['revenue'], reverse=True)
    for it in items_sorted:
        it['gp']     = it['revenue'] - it['cogs']
        it['margin'] = (it['gp'] / it['revenue'] * 100) if it['revenue'] else Decimal('0')

    return {
        'period':       _period_label(df, dt),
        'date_from':    df, 'date_to': dt,
        'n_orders':     n_orders,
        'total_rev':    total_rev,
        'total_cogs':   total_cogs,
        'gross_profit': gross_profit,
        'gross_margin': gross_margin.quantize(Decimal('0.01')),
        'total_exp':    total_exp,
        'payroll':      payroll,
        'net_profit':   net_profit,
        'avg_order':    avg_order,
        'items':        items_sorted,
        'expenses_qs':  expenses_qs,
    }

def _build_finance_custom(params):
    df       = params.get('date_from', '')
    dt       = params.get('date_to',   '')
    group_by = params.get('group_by', 'item')
    item_ids = params.getlist('item_ids') if hasattr(params, 'getlist') else params.get('item_ids', [])
    cat_ids  = params.getlist('cat_ids')  if hasattr(params, 'getlist') else params.get('cat_ids',  [])

    orders = Order.objects.filter(status='active').prefetch_related(
        'items__menu_item__ingredients__ingredient').order_by('created_at')
    orders = _filter_by_date(orders, df, dt)

    bucket = {}
    for order in orders:
        for oi in order.items.all():
            mi = oi.menu_item
            if not mi:
                continue
            if item_ids and str(mi.id) not in item_ids:
                continue
            if cat_ids and (not mi.category or str(mi.category_id) not in cat_ids):
                continue

            cogs_unit = Decimal(str(sum(
                _convert_to_stock_unit(r.quantity_required, r.recipe_unit, r.ingredient.unit)
                * float(r.ingredient.cost_per_unit)
                for r in mi.ingredients.all()
            )))

            if group_by == 'item':
                key, label = mi.id, mi.name
            elif group_by == 'category':
                key   = mi.category_id or 0
                label = mi.category.name if mi.category else 'Uncategorised'
            elif group_by == 'day':
                key = label = str(order.created_at.date())
            elif group_by == 'week':
                d      = order.created_at.date()
                monday = d - timedelta(days=d.weekday())
                key    = str(monday)
                label  = f'Week of {monday}'
            else:  # month
                key   = order.created_at.strftime('%Y-%m')
                label = order.created_at.strftime('%B %Y')

            if key not in bucket:
                bucket[key] = {'label': label, 'qty': 0,
                               'revenue': Decimal('0'), 'cogs': Decimal('0')}
            qty = oi.quantity
            bucket[key]['qty']     += qty
            bucket[key]['revenue'] += oi.price_at_order * qty
            bucket[key]['cogs']    += cogs_unit * qty

    rows = sorted(bucket.values(), key=lambda x: x['revenue'], reverse=True)
    for row in rows:
        row['gp']     = row['revenue'] - row['cogs']
        row['margin'] = (row['gp'] / row['revenue'] * 100) if row['revenue'] else Decimal('0')

    return {
        'rows':      rows,
        'group_by':  group_by,
        'period':    _period_label(df, dt),
        'date_from': df, 'date_to': dt,
        'total_rev':  sum(r['revenue'] for r in rows),
        'total_cogs': sum(r['cogs']    for r in rows),
        'total_gp':   sum(r['gp']      for r in rows),
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  HUB VIEW
# ═══════════════════════════════════════════════════════════════════════════════
@manager_required
def hub(request):
    return render(request, 'reports/hub.html')


# ═══════════════════════════════════════════════════════════════════════════════
#  FINANCE — GENERAL (HTML)
# ═══════════════════════════════════════════════════════════════════════════════
@manager_required
def finance_general(request):
    df, dt = _get_dates(request)
    ctx = _build_finance_general(df, dt)
    return render(request, 'reports/finance_general.html', ctx)


# ── Finance General — PDF ─────────────────────────────────────────────────────
@manager_required
def finance_general_pdf(request):
    df, dt = _get_dates(request)
    data   = _build_finance_general(df, dt)
    b8, n8 = _styles()
    buf    = BytesIO()
    doc    = _pdf_doc(buf)
    story  = []

    _pdf_header(story, 'General Financial Report',
                f'Orders: {data["n_orders"]}   Avg order: {data["avg_order"]:.2f} EGP',
                data['period'])

    red8 = ParagraphStyle('red8', fontName='Helvetica', fontSize=8,
                           textColor=colors.HexColor('#dc2626'))
    net_color = ParagraphStyle('np', fontName='Helvetica-Bold', fontSize=8,
                                textColor=ORANGE if data['net_profit'] >= 0
                                else colors.HexColor('#dc2626'))
    kpi_rows = [
        [Paragraph('<b>Metric</b>', b8),         Paragraph('<b>Amount (EGP)</b>', b8)],
        [Paragraph('Total Revenue',      n8),    Paragraph(f'{data["total_rev"]:.2f}',    n8)],
        [Paragraph('Total COGS',         n8),    Paragraph(f'{data["total_cogs"]:.2f}',   n8)],
        [Paragraph('Gross Profit',       n8),    Paragraph(f'{data["gross_profit"]:.2f}', n8)],
        [Paragraph('Gross Margin',       n8),    Paragraph(f'{data["gross_margin"]:.2f}%',n8)],
        [Paragraph('Operating Expenses', n8),    Paragraph(f'- {data["total_exp"]:.2f}',  red8)],
        [Paragraph('Monthly Payroll',    n8),    Paragraph(f'- {data["payroll"]:.2f}',    red8)],
        [Paragraph('<b>Net Profit</b>',  b8),
         Paragraph(f'<b>{data["net_profit"]:.2f}</b>', net_color)],
    ]
    kt  = Table(kpi_rows, colWidths=[7*cm, 4*cm])
    kts = TableStyle([
        ('BACKGROUND',    (0, 0), (-1,  0), ORANGE),
        ('TEXTCOLOR',     (0, 0), (-1,  0), colors.white),
        ('FONTNAME',      (0, 0), (-1,  0), 'Helvetica-Bold'),
        ('GRID',          (0, 0), (-1, -1), 0.3, MID_GREY),
        ('FONTSIZE',      (0, 0), (-1, -1), 8),
        ('TOPPADDING',    (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING',   (0, 0), (-1, -1), 6),
        ('BACKGROUND',    (0, -1),(-1, -1), LIGHT_ORG),
        ('LINEABOVE',     (0, -1),(-1, -1), 1.2, ORANGE),
    ])
    kt.setStyle(kts)
    story.append(kt)
    story.append(Spacer(1, 0.5*cm))

    story.append(Paragraph('<b>Sales Breakdown by Menu Item</b>', b8))
    story.append(Spacer(1, 0.15*cm))
    hdrs  = ['Item', 'Category', 'Qty Sold', 'Revenue', 'COGS', 'Gross Profit', 'Margin %']
    irows = [[Paragraph(f'<b>{h}</b>', b8) for h in hdrs]]
    for it in data['items']:
        irows.append([
            Paragraph(it['name'],                  n8),
            Paragraph(it['category'],              n8),
            Paragraph(str(it['qty']),              n8),
            Paragraph(f'{it["revenue"]:.2f}',      n8),
            Paragraph(f'{it["cogs"]:.2f}',         n8),
            Paragraph(f'{float(it["gp"]):.2f}',    n8),
            Paragraph(f'{float(it["margin"]):.1f}%', n8),
        ])
    tot_rev  = data['total_rev']
    tot_cogs = data['total_cogs']
    tot_gp   = data['gross_profit']
    tot_mg   = data['gross_margin']
    irows.append([
        Paragraph('<b>TOTAL</b>', b8), Paragraph('', n8), Paragraph('', n8),
        Paragraph(f'<b>{tot_rev:.2f}</b>',       b8),
        Paragraph(f'<b>{tot_cogs:.2f}</b>',      b8),
        Paragraph(f'<b>{float(tot_gp):.2f}</b>', b8),
        Paragraph(f'<b>{float(tot_mg):.1f}%</b>', b8),
    ])
    it_t = Table(irows,
                 colWidths=[4.5*cm, 2.8*cm, 1.6*cm, 2.4*cm, 2.4*cm, 2.8*cm, 1.8*cm],
                 repeatRows=1)
    its = _base_ts()
    _totals_row_style(its, len(irows) - 1)
    it_t.setStyle(its)
    story.append(it_t)
    doc.build(story)
    return _pdf_response(buf, 'finance_general.pdf')


# ── Finance General — Excel ───────────────────────────────────────────────────
@manager_required
def finance_general_excel(request):
    df, dt = _get_dates(request)
    data   = _build_finance_general(df, dt)
    wb = Workbook()

    ws = wb.active; ws.title = 'Summary'
    _xl_title(ws, f'Searing Sandwiches — Financial Report  |  {data["period"]}')
    _xl_headers(ws, [('Metric', 32), ('Value (EGP)', 20)], row=2)
    kpis = [
        ('Total Orders',        data['n_orders']),
        ('Average Order (EGP)', float(data['avg_order'])),
        ('Total Revenue',       float(data['total_rev'])),
        ('Total COGS',          float(data['total_cogs'])),
        ('Gross Profit',        float(data['gross_profit'])),
        ('Gross Margin %',      float(data['gross_margin'])),
        ('Operating Expenses',  float(data['total_exp'])),
        ('Monthly Payroll',     float(data['payroll'])),
        ('Net Profit',          float(data['net_profit'])),
    ]
    for r, (label, val) in enumerate(kpis, 3):
        _xl_cell(ws, r, 1, label)
        is_net = label == 'Net Profit'
        _xl_cell(ws, r, 2, val,
                 num_fmt='#,##0.00', align=_AL_R,
                 bold=is_net,
                 fill=_TOT_FILL if is_net else None,
                 font_color=_OH if is_net else None)

    ws2 = wb.create_sheet('By Menu Item')
    _xl_title(ws2, f'Sales by Menu Item  |  {data["period"]}')
    _xl_headers(ws2, [('Item', 28), ('Category', 20), ('Qty Sold', 10),
                      ('Revenue (EGP)', 16), ('COGS (EGP)', 16),
                      ('Gross Profit', 16), ('Margin %', 12)], row=2)
    for r, it in enumerate(data['items'], 3):
        vals = [it['name'], it['category'], it['qty'],
                float(it['revenue']), float(it['cogs']),
                float(it['gp']),      round(float(it['margin']), 2)]
        for ci, v in enumerate(vals, 1):
            fmt = '#,##0.00' if ci in (4, 5, 6) else ('#0.00' if ci == 7 else None)
            _xl_cell(ws2, r, ci, v, num_fmt=fmt,
                     align=_AL_R if ci >= 3 else _AL_L)
    tot_row = len(data['items']) + 3
    _xl_total_row(ws2, tot_row, ['TOTAL', '', '',
                                  float(data['total_rev']),
                                  float(data['total_cogs']),
                                  float(data['gross_profit']),
                                  float(data['gross_margin'])])

    ws3 = wb.create_sheet('Expenses')
    _xl_title(ws3, f'Operating Expenses  |  {data["period"]}')
    _xl_headers(ws3, [('Date', 14), ('Category', 18), ('Title', 30),
                      ('Amount (EGP)', 16), ('Notes', 35)], row=2)
    for r, exp in enumerate(data['expenses_qs'], 3):
        vals = [str(exp.date), exp.get_category_display(), exp.title,
                float(exp.amount), exp.notes or '']
        for ci, v in enumerate(vals, 1):
            _xl_cell(ws3, r, ci, v, num_fmt='#,##0.00' if ci == 4 else None,
                     align=_AL_R if ci == 4 else _AL_L)

    return _xl_response(wb, 'finance_general.xlsx')


# ═══════════════════════════════════════════════════════════════════════════════
#  FINANCE — CUSTOM (HTML)
# ═══════════════════════════════════════════════════════════════════════════════
GROUP_CHOICES = [
    ('item',     'Menu Item'),
    ('category', 'Category'),
    ('day',      'Day'),
    ('week',     'Week'),
    ('month',    'Month'),
]

@manager_required
def finance_custom(request):
    menu_items = MenuItem.objects.select_related('category').order_by('name')
    categories = MenuCategory.objects.order_by('name')
    ctx = {
        'menu_items':    menu_items,
        'categories':    categories,
        'group_choices': GROUP_CHOICES,
        'results':       None,
        'rows':          [],
        'group_by':      'item',
        'date_from':     '',
        'date_to':       '',
        'sel_items':     [],
        'sel_cats':      [],
        'total_rev':     Decimal('0'),
        'total_cogs':    Decimal('0'),
        'total_gp':      Decimal('0'),
        'period':        'All Time',
    }

    if request.GET.get('group_by'):
        data = _build_finance_custom(request.GET)
        ctx.update(data)
        ctx['results']   = data['rows']
        ctx['sel_items'] = [str(x) for x in request.GET.getlist('item_ids')]
        ctx['sel_cats']  = [str(x) for x in request.GET.getlist('cat_ids')]
        ctx['date_from'] = request.GET.get('date_from', '')
        ctx['date_to']   = request.GET.get('date_to', '')

    return render(request, 'reports/finance_custom.html', ctx)


# ── Finance Custom — PDF ──────────────────────────────────────────────────────
@manager_required
def finance_custom_pdf(request):
    data   = _build_finance_custom(request.GET)
    b8, n8 = _styles()
    buf    = BytesIO()
    doc    = _pdf_doc(buf, land=True)
    story  = []
    _pdf_header(story,
                f'Custom Report — Grouped by {data["group_by"].title()}',
                '', data['period'])

    hdrs  = ['Group', 'Qty', 'Revenue (EGP)', 'COGS (EGP)', 'Gross Profit', 'Margin %']
    trows = [[Paragraph(f'<b>{h}</b>', b8) for h in hdrs]]
    for row in data['rows']:
        trows.append([
            Paragraph(str(row['label']),            n8),
            Paragraph(str(row['qty']),              n8),
            Paragraph(f'{row["revenue"]:.2f}',      n8),
            Paragraph(f'{row["cogs"]:.2f}',         n8),
            Paragraph(f'{float(row["gp"]):.2f}',    n8),
            Paragraph(f'{float(row["margin"]):.1f}%', n8),
        ])
    tot_mg = (float(data['total_gp']) / float(data['total_rev']) * 100
              ) if data['total_rev'] else 0
    trows.append([
        Paragraph('<b>TOTAL</b>', b8), Paragraph('', n8),
        Paragraph(f'<b>{data["total_rev"]:.2f}</b>',  b8),
        Paragraph(f'<b>{data["total_cogs"]:.2f}</b>', b8),
        Paragraph(f'<b>{float(data["total_gp"]):.2f}</b>', b8),
        Paragraph(f'<b>{tot_mg:.1f}%</b>', b8),
    ])
    t   = Table(trows, colWidths=[6*cm, 2*cm, 3.5*cm, 3.5*cm, 3.5*cm, 2.5*cm], repeatRows=1)
    ts_ = _base_ts()
    _totals_row_style(ts_, len(trows) - 1)
    t.setStyle(ts_)
    story.append(t)
    doc.build(story)
    return _pdf_response(buf, 'finance_custom.pdf')


# ── Finance Custom — Excel ────────────────────────────────────────────────────
@manager_required
def finance_custom_excel(request):
    data = _build_finance_custom(request.GET)
    wb = Workbook(); ws = wb.active; ws.title = 'Custom Report'
    _xl_title(ws, f'Custom Report — Grouped by {data["group_by"].title()}  |  {data["period"]}')
    _xl_headers(ws, [('Group', 30), ('Qty Sold', 10), ('Revenue (EGP)', 16),
                     ('COGS (EGP)', 16), ('Gross Profit', 16), ('Margin %', 12)], row=2)
    for r, row in enumerate(data['rows'], 3):
        vals = [row['label'], row['qty'], float(row['revenue']),
                float(row['cogs']), float(row['gp']),
                round(float(row['margin']), 2)]
        for ci, v in enumerate(vals, 1):
            _xl_cell(ws, r, ci, v,
                     num_fmt='#,##0.00' if ci in (3, 4, 5) else ('#0.00' if ci == 6 else None),
                     align=_AL_R if ci >= 2 else _AL_L)
    tot_mg = (float(data['total_gp']) / float(data['total_rev']) * 100
              ) if data['total_rev'] else 0
    _xl_total_row(ws, len(data['rows']) + 3,
                  ['TOTAL', '', float(data['total_rev']),
                   float(data['total_cogs']), float(data['total_gp']), round(tot_mg, 2)])
    return _xl_response(wb, 'finance_custom.xlsx')


# ═══════════════════════════════════════════════════════════════════════════════
#  PURCHASING REPORTS
# ═══════════════════════════════════════════════════════════════════════════════
@manager_required
def purchasing_pdf(request):
    df, dt = _get_dates(request)
    pos    = (PurchaseOrder.objects
              .select_related('supplier')
              .prefetch_related('lines__ingredient')
              .order_by('-order_date'))
    pos    = _filter_by_date(pos, df, dt, field='order_date')
    b8, n8 = _styles()
    buf    = BytesIO()
    doc    = _pdf_doc(buf, land=True)
    story  = []
    _pdf_header(story, 'Purchase Orders Report', f'Total POs: {pos.count()}', _period_label(df, dt))
    hdrs  = ['PO #', 'Date', 'Supplier', 'Status', 'Ingredient',
             'Qty Ordered', 'Qty Received', 'Unit Cost', 'Line Total (EGP)']
    trows = [[Paragraph(f'<b>{h}</b>', b8) for h in hdrs]]
    for po in pos:
        lines = list(po.lines.all())
        for i, line in enumerate(lines):
            trows.append([
                Paragraph(po.po_number if i == 0 else '',              n8),
                Paragraph(str(po.order_date) if i == 0 else '',        n8),
                Paragraph(po.supplier.name if i == 0 else '',          n8),
                Paragraph(po.get_status_display() if i == 0 else '',   n8),
                Paragraph(line.ingredient.name if line.ingredient else '—', n8),
                Paragraph(f'{line.quantity_ordered:.3f} {line.ingredient.unit if line.ingredient else ""}', n8),
                Paragraph(f'{line.quantity_received:.3f}',             n8),
                Paragraph(f'{line.unit_cost:.4f}',                     n8),
                Paragraph(f'{line.total_cost:.2f}',                    n8),
            ])
        if not lines:
            trows.append([Paragraph(po.po_number, n8), Paragraph(str(po.order_date), n8),
                          Paragraph(po.supplier.name, n8), Paragraph(po.get_status_display(), n8),
                          Paragraph('—', n8), Paragraph('—', n8), Paragraph('—', n8),
                          Paragraph('—', n8), Paragraph(f'{po.total_amount:.2f}', n8)])
    t = Table(trows,
              colWidths=[2.2*cm,2.2*cm,3*cm,2.2*cm,3.2*cm,2.6*cm,2.4*cm,2.2*cm,2.5*cm],
              repeatRows=1)
    t.setStyle(_base_ts()); story.append(t)
    doc.build(story)
    return _pdf_response(buf, 'purchase_orders.pdf')


@manager_required
def purchasing_excel(request):
    df, dt = _get_dates(request)
    pos    = (PurchaseOrder.objects
              .select_related('supplier')
              .prefetch_related('lines__ingredient')
              .order_by('-order_date'))
    pos    = _filter_by_date(pos, df, dt, field='order_date')
    wb = Workbook()

    ws = wb.active; ws.title = 'Purchase Orders'
    _xl_title(ws, f'Purchase Orders  |  {_period_label(df, dt)}')
    _xl_headers(ws, [('PO #', 14), ('Date', 13), ('Supplier', 22), ('Status', 16),
                     ('Ingredient', 22), ('Qty Ordered', 13), ('Unit', 8),
                     ('Unit Cost (EGP)', 16), ('Line Total (EGP)', 16),
                     ('Qty Received', 14)], row=2)
    r = 3
    for po in pos:
        for line in po.lines.all():
            ing_name = line.ingredient.name if line.ingredient else '—'
            ing_unit = line.ingredient.unit if line.ingredient else '—'
            vals = [po.po_number, str(po.order_date), po.supplier.name,
                    po.get_status_display(), ing_name,
                    float(line.quantity_ordered), ing_unit,
                    float(line.unit_cost), float(line.total_cost),
                    float(line.quantity_received)]
            for ci, v in enumerate(vals, 1):
                fmt = '#,##0.00' if ci in (8, 9) else ('#,##0.000' if ci in (6, 10) else None)
                _xl_cell(ws, r, ci, v, num_fmt=fmt,
                         align=_AL_R if ci >= 6 else _AL_L)
            r += 1

    ws2 = wb.create_sheet('Invoices')
    _xl_title(ws2, f'Supplier Invoices  |  {_period_label(df, dt)}')
    _xl_headers(ws2, [('Invoice #', 16), ('Supplier', 22), ('PO #', 14),
                      ('Date', 13), ('Due Date', 13), ('Amount (EGP)', 16),
                      ('Amount Paid', 16), ('Status', 14)], row=2)
    invs = (SupplierInvoice.objects
            .select_related('supplier', 'purchase_order')
            .order_by('-invoice_date'))
    invs = _filter_by_date(invs, df, dt, field='invoice_date')
    for ri, inv in enumerate(invs, 3):
        vals = [inv.invoice_number, inv.supplier.name,
                inv.purchase_order.po_number if inv.purchase_order else '—',
                str(inv.invoice_date), str(inv.due_date or ''),
                float(inv.amount), float(inv.amount_paid),
                inv.get_status_display()]
        for ci, v in enumerate(vals, 1):
            _xl_cell(ws2, ri, ci, v,
                     num_fmt='#,##0.00' if ci in (6, 7) else None,
                     align=_AL_R if ci in (6, 7) else _AL_L)

    return _xl_response(wb, 'purchase_orders.xlsx')


# ═══════════════════════════════════════════════════════════════════════════════
#  HR — ATTENDANCE
# ═══════════════════════════════════════════════════════════════════════════════
@manager_required
def hr_attendance_pdf(request):
    df, dt = _get_dates(request)
    recs   = (AttendanceRecord.objects
              .select_related('employee')
              .order_by('-date', 'employee__full_name'))
    recs   = _filter_by_date(recs, df, dt, field='date')
    b8, n8 = _styles()
    buf    = BytesIO(); doc = _pdf_doc(buf, land=True); story = []
    _pdf_header(story, 'Attendance Report',
                f'Records: {recs.count()}', _period_label(df, dt))
    hdrs  = ['Date', 'Employee', 'Status', 'Check In', 'Check Out', 'Notes']
    trows = [[Paragraph(f'<b>{h}</b>', b8) for h in hdrs]]
    for rec in recs:
        trows.append([
            Paragraph(str(rec.date), n8),
            Paragraph(rec.employee.full_name, n8),
            Paragraph(rec.get_status_display(), n8),
            Paragraph(str(rec.check_in  or '—'), n8),
            Paragraph(str(rec.check_out or '—'), n8),
            Paragraph(rec.notes or '', n8),
        ])
    t = Table(trows, colWidths=[2.5*cm, 5*cm, 2.5*cm, 2.5*cm, 2.5*cm, 5.5*cm], repeatRows=1)
    t.setStyle(_base_ts()); story.append(t)
    doc.build(story)
    return _pdf_response(buf, 'attendance.pdf')


@manager_required
def hr_attendance_excel(request):
    df, dt = _get_dates(request)
    recs   = (AttendanceRecord.objects
              .select_related('employee')
              .order_by('-date', 'employee__full_name'))
    recs   = _filter_by_date(recs, df, dt, field='date')
    wb = Workbook(); ws = wb.active; ws.title = 'Attendance'
    _xl_title(ws, f'Attendance Records  |  {_period_label(df, dt)}')
    _xl_headers(ws, [('Date', 14), ('Employee', 26), ('Status', 14),
                     ('Check In', 12), ('Check Out', 12), ('Notes', 35)], row=2)
    for r, rec in enumerate(recs, 3):
        vals = [str(rec.date), rec.employee.full_name,
                rec.get_status_display(),
                str(rec.check_in  or ''), str(rec.check_out or ''),
                rec.notes or '']
        for ci, v in enumerate(vals, 1):
            _xl_cell(ws, r, ci, v)
    return _xl_response(wb, 'attendance.xlsx')


# ═══════════════════════════════════════════════════════════════════════════════
#  HR — PAYROLL
# ═══════════════════════════════════════════════════════════════════════════════
@manager_required
def hr_payroll_pdf(request):
    df, dt = _get_dates(request)
    runs   = SalaryRun.objects.prefetch_related('lines__employee').order_by('-period_end')
    if df: runs = runs.filter(period_start__gte=df)
    if dt: runs = runs.filter(period_end__lte=dt)
    b8, n8 = _styles()
    buf    = BytesIO(); doc = _pdf_doc(buf, land=True); story = []
    _pdf_header(story, 'Payroll Report',
                f'Salary runs: {runs.count()}', _period_label(df, dt))
    for run in runs:
        story.append(Paragraph(
            f'<b>{run.period_start} → {run.period_end}</b>'
            f'  ·  Status: {run.get_status_display()}'
            f'  ·  Net Total: <b>{float(run.total_net):.2f} EGP</b>', b8))
        story.append(Spacer(1, 0.15*cm))
        hdrs  = ['Employee', 'Days Worked', 'Days Total',
                 'Base Salary', 'Gross', 'Deductions', 'Advances', 'Net Pay']
        lrows = [[Paragraph(f'<b>{h}</b>', b8) for h in hdrs]]
        for line in run.lines.all():
            lrows.append([
                Paragraph(line.employee.full_name,   n8),
                Paragraph(str(line.days_worked),     n8),
                Paragraph(str(line.days_total),      n8),
                Paragraph(f'{line.base_salary:.2f}', n8),
                Paragraph(f'{line.gross:.2f}',       n8),
                Paragraph(f'{line.deductions:.2f}',  n8),
                Paragraph(f'{line.advances:.2f}',    n8),
                Paragraph(f'{line.net:.2f}',         n8),
            ])
        t = Table(lrows,
                  colWidths=[4.5*cm,2.2*cm,2.2*cm,2.8*cm,2.5*cm,2.8*cm,2.5*cm,2.5*cm],
                  repeatRows=1)
        t.setStyle(_base_ts()); story.append(t); story.append(Spacer(1, 0.5*cm))
    doc.build(story)
    return _pdf_response(buf, 'payroll.pdf')


@manager_required
def hr_payroll_excel(request):
    df, dt = _get_dates(request)
    runs   = SalaryRun.objects.prefetch_related('lines__employee').order_by('-period_end')
    if df: runs = runs.filter(period_start__gte=df)
    if dt: runs = runs.filter(period_end__lte=dt)
    wb = Workbook(); first = True
    for run in runs:
        ws       = wb.active if first else wb.create_sheet()
        first    = False
        ws.title = f'{run.period_start}'[:31]
        _xl_title(ws, f'Payroll: {run.period_start} → {run.period_end}  |  {run.get_status_display()}')
        _xl_headers(ws, [('Employee', 26), ('Days Worked', 13), ('Days Total', 13),
                         ('Base Salary', 16), ('Gross', 14), ('Deductions', 14),
                         ('Advances', 14), ('Net Pay', 14)], row=2)
        tot_gross = tot_net = 0
        for r, line in enumerate(run.lines.all(), 3):
            vals = [line.employee.full_name,
                    float(line.days_worked), float(line.days_total),
                    float(line.base_salary), float(line.gross),
                    float(line.deductions), float(line.advances), float(line.net)]
            for ci, v in enumerate(vals, 1):
                _xl_cell(ws, r, ci, v,
                         num_fmt='#,##0.00' if ci >= 4 else None,
                         align=_AL_R if ci >= 2 else _AL_L)
            tot_gross += float(line.gross); tot_net += float(line.net)
        tot_row = run.lines.count() + 3
        _xl_total_row(ws, tot_row, ['TOTAL', '', '', '', tot_gross, '', '', tot_net])
    if first:
        wb.active.title = 'No Data'
        wb.active['A1'] = 'No salary runs found for the selected period.'
    return _xl_response(wb, 'payroll.xlsx')


# ═══════════════════════════════════════════════════════════════════════════════
#  INVENTORY REPORT
# ═══════════════════════════════════════════════════════════════════════════════
@manager_required
def inventory_pdf(request):
    df, dt = _get_dates(request)
    b8, n8 = _styles()
    buf    = BytesIO(); doc = _pdf_doc(buf, land=True); story = []
    _pdf_header(story, 'Inventory Report', '', _period_label(df, dt))

    story.append(Paragraph('<b>Current Stock Levels</b>', b8))
    story.append(Spacer(1, 0.15*cm))
    ings  = Ingredient.objects.order_by('name')
    hdrs  = ['Ingredient', 'Unit', 'Stock Qty', 'Low-Stock Alert', 'Cost / Unit (EGP)', 'Stock Value (EGP)']
    irows = [[Paragraph(f'<b>{h}</b>', b8) for h in hdrs]]
    total_value = Decimal('0')
    for ing in ings:
        val = ing.stock_quantity * ing.cost_per_unit
        total_value += val
        irows.append([
            Paragraph(ing.name, n8),
            Paragraph(ing.unit, n8),
            Paragraph(f'{ing.stock_quantity:.3f}', n8),
            Paragraph(f'{ing.low_stock_threshold:.3f}', n8),
            Paragraph(f'{ing.cost_per_unit:.4f}', n8),
            Paragraph(f'{float(val):.2f}', n8),
        ])
    irows.append([Paragraph('<b>TOTAL STOCK VALUE</b>', b8), '', '', '', '',
                  Paragraph(f'<b>{float(total_value):.2f}</b>', b8)])
    t = Table(irows, colWidths=[4.5*cm,1.8*cm,2.3*cm,2.5*cm,3*cm,3.2*cm], repeatRows=1)
    ts_ = _base_ts(); _totals_row_style(ts_, len(irows)-1)
    t.setStyle(ts_); story.append(t); story.append(Spacer(1, 0.5*cm))

    story.append(Paragraph('<b>Stock Movement Log</b>', b8))
    story.append(Spacer(1, 0.15*cm))
    logs  = _filter_by_date(StockLog.objects.select_related('ingredient', 'changed_by')
                             .order_by('-created_at'), df, dt)
    hdrs2 = ['Date / Time', 'Ingredient', 'Change', 'Unit', 'Before', 'After', 'Reason', 'User']
    lrows = [[Paragraph(f'<b>{h}</b>', b8) for h in hdrs2]]
    for log in logs[:200]:
        lrows.append([
            Paragraph(log.created_at.strftime('%Y-%m-%d %H:%M'), n8),
            Paragraph(log.ingredient.name if log.ingredient else '—', n8),
            Paragraph(f'{float(log.qty_change):+.3f}', n8),
            Paragraph(log.ingredient.unit if log.ingredient else '—', n8),
            Paragraph(f'{float(log.qty_before):.3f}', n8),
            Paragraph(f'{float(log.qty_after):.3f}',  n8),
            Paragraph(log.get_reason_display(), n8),
            Paragraph(log.changed_by.username if log.changed_by else '—', n8),
        ])
    t2 = Table(lrows,
               colWidths=[3.2*cm,3.5*cm,1.8*cm,1.5*cm,1.8*cm,1.8*cm,2.5*cm,2.2*cm],
               repeatRows=1)
    t2.setStyle(_base_ts()); story.append(t2)
    doc.build(story)
    return _pdf_response(buf, 'inventory.pdf')


@manager_required
def inventory_excel(request):
    df, dt = _get_dates(request)
    wb = Workbook()

    ws = wb.active; ws.title = 'Stock Levels'
    _xl_title(ws, f'Current Stock Levels  |  {_period_label(df, dt)}')
    _xl_headers(ws, [('Ingredient', 26), ('Unit', 10), ('Stock Qty', 13),
                     ('Low-Stock Alert', 16), ('Cost / Unit (EGP)', 18),
                     ('Stock Value (EGP)', 18)], row=2)
    total_value = 0.0
    for r, ing in enumerate(Ingredient.objects.order_by('name'), 3):
        val = float(ing.stock_quantity) * float(ing.cost_per_unit)
        total_value += val
        vals = [ing.name, ing.unit, float(ing.stock_quantity),
                float(ing.low_stock_threshold), float(ing.cost_per_unit), val]
        for ci, v in enumerate(vals, 1):
            fmt = '#,##0.000' if ci in (3, 4) else ('#,##0.0000' if ci == 5 else
                  '#,##0.00'  if ci == 6 else None)
            _xl_cell(ws, r, ci, v, num_fmt=fmt,
                     align=_AL_R if ci >= 3 else _AL_L)
    tot_row = Ingredient.objects.count() + 3
    _xl_total_row(ws, tot_row, ['TOTAL STOCK VALUE', '', '', '', '', total_value])

    ws2 = wb.create_sheet('Stock Log')
    _xl_title(ws2, f'Stock Movement Log  |  {_period_label(df, dt)}')
    _xl_headers(ws2, [('Date / Time', 18), ('Ingredient', 26), ('Change', 12),
                      ('Unit', 8), ('Qty Before', 13), ('Qty After', 13),
                      ('Reason', 18), ('Changed By', 18)], row=2)
    logs = _filter_by_date(StockLog.objects.select_related('ingredient', 'changed_by')
                           .order_by('-created_at'), df, dt)
    for r, log in enumerate(logs, 3):
        vals = [log.created_at.strftime('%Y-%m-%d %H:%M'),
                log.ingredient.name if log.ingredient else '—',
                float(log.qty_change),
                log.ingredient.unit if log.ingredient else '—',
                float(log.qty_before), float(log.qty_after),
                log.get_reason_display(),
                log.changed_by.username if log.changed_by else '']
        for ci, v in enumerate(vals, 1):
            _xl_cell(ws2, r, ci, v,
                     num_fmt='#,##0.000' if ci in (3, 5, 6) else None,
                     align=_AL_R if ci in (3, 5, 6) else _AL_L)

    ws3 = wb.create_sheet('Cost History')
    _xl_title(ws3, f'Ingredient Cost History  |  {_period_label(df, dt)}')
    _xl_headers(ws3, [('Date', 18), ('Ingredient', 26), ('Old Cost', 14),
                      ('New Cost', 14), ('Qty Restocked', 14),
                      ('Total Paid (EGP)', 16), ('Recorded By', 18)], row=2)
    hist = _filter_by_date(CostHistory.objects.select_related('ingredient', 'recorded_by')
                           .order_by('-created_at'), df, dt)
    for r, h in enumerate(hist, 3):
        vals = [h.created_at.strftime('%Y-%m-%d %H:%M'),
                h.ingredient.name if h.ingredient else '—',
                float(h.old_cost), float(h.new_cost),
                float(h.qty_restocked), float(h.total_paid),
                h.recorded_by.username if h.recorded_by else '']
        for ci, v in enumerate(vals, 1):
            fmt = '#,##0.0000' if ci in (3, 4) else ('#,##0.000' if ci == 5 else
                  '#,##0.00' if ci == 6 else None)
            _xl_cell(ws3, r, ci, v, num_fmt=fmt,
                     align=_AL_R if ci in (3, 4, 5, 6) else _AL_L)

    return _xl_response(wb, 'inventory.xlsx')


# ═══════════════════════════════════════════════════════════════════════════════
#  ACTIVITY LOG
# ═══════════════════════════════════════════════════════════════════════════════
@manager_required
def activity_pdf(request):
    df, dt = _get_dates(request)
    logs   = ActivityLog.objects.select_related('user').order_by('-created_at')
    logs   = _filter_by_date(logs, df, dt, field='created_at__date')
    b8, n8 = _styles()
    buf    = BytesIO(); doc = _pdf_doc(buf, land=True); story = []
    _pdf_header(story, 'Activity Log',
                f'Entries shown: {min(logs.count(), 500)}  (capped at 500)',
                _period_label(df, dt))
    hdrs  = ['Timestamp', 'User', 'Action', 'Detail']
    trows = [[Paragraph(f'<b>{h}</b>', b8) for h in hdrs]]
    for log in logs[:500]:
        trows.append([
            Paragraph(log.created_at.strftime('%Y-%m-%d %H:%M'), n8),
            Paragraph(log.user.username if log.user else '—',    n8),
            Paragraph(log.get_action_display(),                   n8),
            Paragraph((log.detail or '')[:100],                   n8),
        ])
    t = Table(trows, colWidths=[3.5*cm, 3*cm, 4*cm, 10.5*cm], repeatRows=1)
    t.setStyle(_base_ts()); story.append(t)
    doc.build(story)
    return _pdf_response(buf, 'activity_log.pdf')


@manager_required
def activity_excel(request):
    # FIX: was incorrectly ordering/filtering by non-existent 'timestamp' field.
    # ActivityLog uses 'created_at'.
    df, dt = _get_dates(request)
    logs   = ActivityLog.objects.select_related('user').order_by('-created_at')
    logs   = _filter_by_date(logs, df, dt, field='created_at__date')
    wb = Workbook(); ws = wb.active; ws.title = 'Activity Log'
    _xl_title(ws, f'Activity Log  |  {_period_label(df, dt)}')
    _xl_headers(ws, [('Timestamp', 20), ('User', 18),
                     ('Action', 28), ('Detail', 55)], row=2)
    for r, log in enumerate(logs, 3):
        vals = [log.created_at.strftime('%Y-%m-%d %H:%M'),
                log.user.username if log.user else '',
                log.get_action_display(),
                log.detail or '']
        for ci, v in enumerate(vals, 1):
            _xl_cell(ws, r, ci, v)
    return _xl_response(wb, 'activity_log.xlsx')


# ── Ingredient Detail Report ───────────────────────────────────────────────────

def _build_ingredient_detail(ing_ids, df, dt):
    ings = Ingredient.objects.order_by('name')
    if ing_ids:
        ings = ings.filter(id__in=ing_ids)

    results = []
    for ing in ings:
        po_lines = (PurchaseOrderLine.objects
                    .filter(ingredient=ing, quantity_received__gt=0)
                    .select_related('purchase_order__supplier')
                    .order_by('-purchase_order__received_date'))
        if df:
            po_lines = po_lines.filter(purchase_order__received_date__gte=df)
        if dt:
            po_lines = po_lines.filter(purchase_order__received_date__lte=dt)

        dmg = (DamagedStock.objects
               .filter(ingredient=ing)
               .select_related('order', 'recorded_by')
               .order_by('-created_at'))
        if df:
            dmg = dmg.filter(created_at__date__gte=df)
        if dt:
            dmg = dmg.filter(created_at__date__lte=dt)

        total_restocked    = sum(l.quantity_received for l in po_lines)
        total_restock_cost = sum(l.quantity_received * l.unit_cost for l in po_lines)
        total_damaged      = sum(d.qty_damaged for d in dmg)
        total_damage_cost  = sum(d.qty_damaged * ing.cost_per_unit for d in dmg)

        results.append({
            'ingredient':         ing,
            'po_lines':           list(po_lines),
            'damage_logs':        list(dmg),
            'total_restocked':    total_restocked,
            'total_restock_cost': total_restock_cost,
            'total_damaged':      total_damaged,
            'total_damage_cost':  total_damage_cost,
        })
    return results


@manager_required
def ingredient_detail(request):
    df, dt = _get_dates(request)
    all_ings    = Ingredient.objects.order_by('name')
    ing_ids_raw = request.GET.getlist('ingredients')
    ing_ids     = [int(i) for i in ing_ids_raw if i.isdigit()]
    results     = (
        _build_ingredient_detail(ing_ids, df, dt)
        if (ing_ids or request.GET.get('show_all'))
        else []
    )

    return render(request, 'reports/ingredient_detail.html', {
        'all_ingredients': all_ings,
        'results':         results,
        'selected_ids':    ing_ids,
        'date_from':       df,
        'date_to':         dt,
        'show_all':        bool(request.GET.get('show_all')),
        'period_label':    _period_label(df, dt),
    })


@manager_required
def ingredient_detail_pdf(request):
    df, dt   = _get_dates(request)
    ing_ids  = [int(i) for i in request.GET.getlist('ingredients') if i.isdigit()]
    show_all = bool(request.GET.get('show_all'))
    results  = _build_ingredient_detail(ing_ids if not show_all else [], df, dt)

    b8, n8 = _styles()
    buf = BytesIO(); doc = _pdf_doc(buf, land=True); story = []
    _pdf_header(story, 'Ingredient Detail Report', '', _period_label(df, dt))

    for res in results:
        ing = res['ingredient']
        story.append(Paragraph(
            f'<b>{ing.name}</b> &nbsp;·&nbsp; {ing.unit} &nbsp;·&nbsp; '
            f'Current stock: <b>{float(ing.stock_quantity):.3f}</b> {ing.unit} &nbsp;·&nbsp; '
            f'Cost/unit: <b>{float(ing.cost_per_unit):.4f} EGP</b>', b8))
        story.append(Spacer(1, 0.1*cm))

        story.append(Paragraph('<b>Purchase History</b>',
            ParagraphStyle('s7', fontName='Helvetica', fontSize=7, textColor=GREY)))
        story.append(Spacer(1, 0.08*cm))
        ph = ['PO #', 'Supplier', 'Date Received', 'Qty Ordered', 'Qty Received', 'Unit Cost', 'Total Paid']
        prows = [[Paragraph(f'<b>{h}</b>', b8) for h in ph]]
        if res['po_lines']:
            for line in res['po_lines']:
                prows.append([
                    Paragraph(line.purchase_order.po_number, n8),
                    Paragraph(line.purchase_order.supplier.name, n8),
                    Paragraph(str(line.purchase_order.received_date or '—'), n8),
                    Paragraph(f'{float(line.quantity_ordered):.3f}', n8),
                    Paragraph(f'{float(line.quantity_received):.3f}', n8),
                    Paragraph(f'{float(line.unit_cost):.4f}', n8),
                    Paragraph(f'{float(line.quantity_received * line.unit_cost):.2f}', n8),
                ])
            prows.append([
                Paragraph('<b>TOTALS</b>', b8), '', '', '',
                Paragraph(f'<b>{float(res["total_restocked"]):.3f}</b>', b8),
                '',
                Paragraph(f'<b>{float(res["total_restock_cost"]):.2f} EGP</b>', b8),
            ])
        else:
            prows.append([Paragraph('No purchase history in this period', n8)] + ['']*6)

        pt = Table(prows, colWidths=[2.5*cm,4*cm,2.8*cm,2.5*cm,2.5*cm,2.5*cm,2.8*cm], repeatRows=1)
        pts = _base_ts()
        if len(prows) > 2:
            _totals_row_style(pts, len(prows)-1)
        pt.setStyle(pts); story.append(pt)
        story.append(Spacer(1, 0.3*cm))

        story.append(Paragraph('<b>Damage / Wastage Log</b>',
            ParagraphStyle('s7', fontName='Helvetica', fontSize=7, textColor=GREY)))
        story.append(Spacer(1, 0.08*cm))
        dh = ['Date', 'Qty Damaged', 'Unit', 'Est. Cost (EGP)', 'Linked Order', 'Reason', 'Recorded By']
        drows = [[Paragraph(f'<b>{h}</b>', b8) for h in dh]]
        if res['damage_logs']:
            for d in res['damage_logs']:
                cost = float(d.qty_damaged * ing.cost_per_unit)
                drows.append([
                    Paragraph(d.created_at.strftime('%Y-%m-%d'), n8),
                    Paragraph(f'{float(d.qty_damaged):.3f}', n8),
                    Paragraph(d.unit, n8),
                    Paragraph(f'{cost:.2f}', n8),
                    Paragraph(d.order.order_number if d.order else '—', n8),
                    Paragraph((d.reason or '—')[:40], n8),
                    Paragraph(d.recorded_by.username if d.recorded_by else '—', n8),
                ])
            drows.append([
                Paragraph('<b>TOTALS</b>', b8),
                Paragraph(f'<b>{float(res["total_damaged"]):.3f}</b>', b8), '',
                Paragraph(f'<b>{float(res["total_damage_cost"]):.2f} EGP</b>', b8),
                '', '', '',
            ])
        else:
            drows.append([Paragraph('No damage events in this period', n8)] + ['']*6)

        dt_ = Table(drows, colWidths=[2.5*cm,2.5*cm,1.8*cm,3*cm,2.5*cm,4.5*cm,2.7*cm], repeatRows=1)
        dts = _base_ts()
        if len(drows) > 2:
            _totals_row_style(dts, len(drows)-1)
        dt_.setStyle(dts); story.append(dt_)
        story.append(Spacer(1, 0.6*cm))
        story.append(HRFlowable(width='100%', thickness=0.5, color=MID_GREY))
        story.append(Spacer(1, 0.4*cm))

    doc.build(story)
    return _pdf_response(buf, f'ingredient_detail_{date.today()}.pdf')


@manager_required
def ingredient_detail_excel(request):
    df, dt   = _get_dates(request)
    ing_ids  = [int(i) for i in request.GET.getlist('ingredients') if i.isdigit()]
    show_all = bool(request.GET.get('show_all'))
    results  = _build_ingredient_detail(ing_ids if not show_all else [], df, dt)

    wb = Workbook()

    for res in results:
        ing = res['ingredient']
        ws  = wb.create_sheet(ing.name[:30])

        _xl_title(ws, f'{ing.name} — Ingredient Detail  |  {_period_label(df, dt)}')

        ws.append([])
        ws.append(['Current Stock', f'{float(ing.stock_quantity):.3f} {ing.unit}',
                   'Cost/Unit', f'{float(ing.cost_per_unit):.4f} EGP',
                   'Stock Value', f'{float(ing.stock_quantity * ing.cost_per_unit):.2f} EGP'])
        for ci in range(1, 7):
            ws.cell(row=3, column=ci).font = Font(bold=True)
        ws.append([])

        _xl_headers(ws, [('PO #', 14), ('Supplier', 24), ('Date Received', 16),
                         ('Qty Ordered', 14), ('Qty Received', 14),
                         ('Unit Cost (EGP)', 16), ('Total Paid (EGP)', 16)], row=5)
        r = 6
        for line in res['po_lines']:
            vals = [line.purchase_order.po_number,
                    line.purchase_order.supplier.name,
                    str(line.purchase_order.received_date or ''),
                    float(line.quantity_ordered),
                    float(line.quantity_received),
                    float(line.unit_cost),
                    float(line.quantity_received * line.unit_cost)]
            for ci, v in enumerate(vals, 1):
                _xl_cell(ws, r, ci, v,
                         num_fmt='#,##0.000' if ci in (4, 5) else
                                 '#,##0.0000' if ci == 6 else
                                 '#,##0.00'   if ci == 7 else None,
                         align=_AL_R if ci >= 4 else _AL_L)
            r += 1
        if res['po_lines']:
            _xl_total_row(ws, r, ['TOTALS', '', '', '',
                                   float(res['total_restocked']), '',
                                   float(res['total_restock_cost'])])
            r += 1
        ws.append([])
        r += 1

        dmg_hdr_row = r
        _xl_headers(ws, [('Date', 16), ('Qty Damaged', 14), ('Unit', 10),
                         ('Est. Cost (EGP)', 16), ('Linked Order', 14),
                         ('Reason', 30), ('Recorded By', 18)], row=dmg_hdr_row)
        r = dmg_hdr_row + 1
        for d in res['damage_logs']:
            cost = float(d.qty_damaged * ing.cost_per_unit)
            vals = [d.created_at.strftime('%Y-%m-%d %H:%M'),
                    float(d.qty_damaged), d.unit, cost,
                    d.order.order_number if d.order else '',
                    d.reason or '', d.recorded_by.username if d.recorded_by else '']
            for ci, v in enumerate(vals, 1):
                _xl_cell(ws, r, ci, v,
                         num_fmt='#,##0.000' if ci == 2 else '#,##0.00' if ci == 4 else None,
                         align=_AL_R if ci in (2, 4) else _AL_L)
            r += 1
        if res['damage_logs']:
            _xl_total_row(ws, r, ['TOTALS', float(res['total_damaged']), '',
                                   float(res['total_damage_cost']), '', '', ''])

    if 'Sheet' in wb.sheetnames:
        del wb['Sheet']

    return _xl_response(wb, f'ingredient_detail_{date.today()}.xlsx')