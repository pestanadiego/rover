import html
import re
from typing import Any

from rover.reports.config import EmailReportConfig


ROW_DEFAULT_COLOR = "#ffffff"
ROW_STRIPE_COLOR = "#f4f7f5"
ROW_BORDER_COLOR = "#e3e9e6"

IMAGE_COLUMN_WIDTH = "14%"
ASIN_COLUMN_WIDTH = "15%"
METRICS_COLUMN_WIDTH = "22%"
NOTES_COLUMN_WIDTH = "49%"

DECISION_BADGE_STYLES = {
    "keep": {
        "color": "#0f6e56",
        "background": "#dceee8",
    },
    "watchlist": {
        "color": "#285a92",
        "background": "#dceafb",
    },
    "reject": {
        "color": "#9d1f1f",
        "background": "#f5dada",
    },
    "needs_manual_review": {
        "color": "#6f3f8f",
        "background": "#eadff2",
    },
    "pending": {
        "color": "#7a5c00",
        "background": "#f5edcc",
    },
}


def render_report_html(report: dict[str, Any], config: EmailReportConfig) -> str:
    recipient_name = config.recipient_name.strip() or "there"
    title = escape_text(f"{config.agent_name} Product Report")
    intro = escape_text(
        f"Hey {recipient_name}! {config.agent_name} analyzed the following products "
        "and found them worth a closer look."
    )
    agent_possessive = f"{escape_text(config.agent_name)}&rsquo;s"

    return f"""\
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body {{
      margin: 0;
      padding: 0;
      color: #222e29;
      font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Helvetica, Arial, sans-serif;
      font-size: 14px;
      line-height: 1.45;
    }}
    
    .panel {{
      background: #ffffff;
      border-radius: 8px;
      padding: 2rem 5rem 0 0;
    }}
    .shell {{
      max-width: 960px;
      margin: 0 auto;
      padding: 28px 16px;
    }}

    h2 {{
      margin: 36px 0 14px;
      font-size: 17px;
      font-weight: 600;
      color: #131e1a;
    }}
    .report-title {{
      margin: 0 0 10px;
      font-size: 22px;
      font-weight: 700;
      color: #131e1a;
      line-height: 1.2;
    }}
    .intro {{
      margin: 0 0 48px;
      font-size: 14px;
      color: #4a5e56;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    thead tr {{
      border-bottom: 1px solid #bdc8c2;
    }}
    th {{
      background: transparent;
      color: #6b8078;
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.07em;
      text-transform: uppercase;
      text-align: left;
      padding: 0 10px 9px;
      border: none;
    }}
    th.image-column,
    th.asin-column {{
      text-align: center;
    }}
    td {{
      padding: 12px 10px;
      vertical-align: middle;
      border: none;
      word-break: normal;
      overflow-wrap: normal;
      color: #222e29;
      text-align: left;
    }}
    .image-column {{
      width: {IMAGE_COLUMN_WIDTH};
    }}
    .asin-column {{
      width: {ASIN_COLUMN_WIDTH};
    }}
    .metrics-column {{
      width: {METRICS_COLUMN_WIDTH};
    }}
    .notes-column {{
      width: {NOTES_COLUMN_WIDTH};
    }}
    .image-cell,
    .asin-cell {{
      text-align: center;
      vertical-align: middle;
    }}
    .product-image {{
      display: block;
      margin: 0 auto;
      width: 96px;
      height: 96px;
      object-fit: contain;
      border-radius: 3px;
      background: #fff;
      border: 1px solid #dde4e1;
      padding: 4px;
      box-sizing: border-box;
    }}
    a {{
      color: #0f6e56!important;
      text-decoration: none;
      font-family: "Courier New", Courier, monospace;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.02em;
    }}
    .metrics-cell {{
      vertical-align: middle;
    }}
    .metric-row {{
      line-height: 1.75;
    }}
    .metric-key {{
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      color: #6b8078;
    }}
    .metric-val {{
      color: #222e29;
    }}
    .metric-val.profit {{
      color: #0f6e56;
      font-weight: 700;
    }}
    .notes-cell {{
      vertical-align: middle;
      color: #5a7068;
      font-size: 13px;
      word-break: normal;
      overflow-wrap: normal;
      white-space: normal;
    }}
    .decision {{
      display: inline-block;
      margin-bottom: 6px;
      font-family: "Courier New", Courier, monospace;
      font-size: 9px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: #7a5c00;
      background-color: #f5edcc;
      padding: 2px 6px;
      border-radius: 3px;
    }}
    .breakdown {{
      color: #5a7068;
      font-size: 14px;
    }}
    .muted {{
      color: #8aa49a;
    }}
    .breakdown p {{
      margin: 0 0 12px;
    }}
  </style>
</head>
<body>
  <div class="shell">
    <div class="panel">
      <h1 class="report-title">{title}</h1>
      <p class="intro">{intro}</p>
      {render_products_table_html(report, config)}
      <h2>{agent_possessive} breakdown</h2>
      <div class="breakdown">
        {render_breakdown_html(report, config)}
      </div>
    </div>
  </div>
</body>
</html>
"""


def render_report_text(report: dict[str, Any], config: EmailReportConfig) -> str:
    recipient_name = config.recipient_name.strip() or "there"
    lines = [
        f"{config.agent_name} Product Report",
        "",
        (
            f"Hey {recipient_name}! {config.agent_name} analyzed the following products "
            "and found them worth a closer look."
        ),
        "",
        "Products",
    ]

    products = report.get("products") or []
    if not products:
        lines.append("No products found for this report.")
    else:
        for product in products:
            lines.extend(product_text_lines(product, config))

    lines.extend(["", f"{config.agent_name}'s breakdown"])
    lines.extend(agent_breakdown_lines(report, config))
    return "\n".join(lines).strip()


def render_products_table_html(
    report: dict[str, Any],
    config: EmailReportConfig,
) -> str:
    products = report.get("products") or []
    if not products:
        return '<p class="muted">No products found for this report.</p>'

    total_rows = len(products)
    rows = "\n".join(
        render_product_row_html(product, config, row_number, total_rows)
        for row_number, product in enumerate(products, start=1)
    )
    return f"""\
<table border="0" cellpadding="0" cellspacing="0" width="100%" style="border-collapse: collapse; table-layout: fixed; width: 100%;">
  <thead>
    <tr style="border-bottom: 1px solid #bdc8c2;">
      <th class="image-column" width="{IMAGE_COLUMN_WIDTH}" style="{escape_attr(header_inline_style('center', IMAGE_COLUMN_WIDTH))}">Image</th>
      <th class="asin-column" width="{ASIN_COLUMN_WIDTH}" style="{escape_attr(header_inline_style('center', ASIN_COLUMN_WIDTH))}">ASIN</th>
      <th class="metrics-column" width="{METRICS_COLUMN_WIDTH}" style="{escape_attr(header_inline_style('left', METRICS_COLUMN_WIDTH))}">Metrics</th>
      <th class="notes-column" width="{NOTES_COLUMN_WIDTH}" style="{escape_attr(header_inline_style('left', NOTES_COLUMN_WIDTH))}">{escape_text(config.agent_name)}&rsquo;s notes</th>
    </tr>
  </thead>
  <tbody>
    {rows}
  </tbody>
</table>
"""


def render_product_row_html(
    product: dict[str, Any],
    config: EmailReportConfig,
    row_number: int,
    total_rows: int,
) -> str:
    background_color = ROW_STRIPE_COLOR if row_number % 2 == 0 else ROW_DEFAULT_COLOR
    row_style = f"background-color: {background_color};"
    cell_style = table_cell_inline_style(background_color, row_number, total_rows)
    image_style = f"{cell_style} width: {IMAGE_COLUMN_WIDTH}; text-align: center;"
    asin_style = f"{cell_style} width: {ASIN_COLUMN_WIDTH}; text-align: center;"
    metrics_style = f"{cell_style} width: {METRICS_COLUMN_WIDTH}; vertical-align: middle;"
    notes_style = (
        f"{cell_style} "
        f"width: {NOTES_COLUMN_WIDTH}; "
        "vertical-align: middle; "
        "color: #5a7068; "
        "font-size: 13px; "
        "min-width: 200px; "
        "white-space: normal; "
        "word-break: normal; "
        "overflow-wrap: normal;"
    )

    return f"""\
<tr style="{escape_attr(row_style)}">
  <td class="image-cell" width="{IMAGE_COLUMN_WIDTH}" style="{escape_attr(image_style)}">{render_product_image_html(product)}</td>
  <td class="asin-cell" width="{ASIN_COLUMN_WIDTH}" style="{escape_attr(asin_style)}">{render_asin_link_html(product)}</td>
  <td class="metrics-cell" width="{METRICS_COLUMN_WIDTH}" style="{escape_attr(metrics_style)}">{render_metrics_html(product)}</td>
  <td class="notes-cell" width="{NOTES_COLUMN_WIDTH}" style="{escape_attr(notes_style)}">{render_agent_notes_html(product)}</td>
</tr>"""


def header_inline_style(
    text_align: str,
    width: str | None = None,
    nowrap: bool = False,
) -> str:
    styles = [
        "background: transparent;",
        "color: #6b8078;",
        "font-size: 10px;",
        "font-weight: 700;",
        "letter-spacing: 0.07em;",
        "text-transform: uppercase;",
        f"text-align: {text_align};",
        "padding: 0 5px 9px;",
        "border: none;",
        "word-break: normal;",
        "overflow-wrap: normal;",
    ]

    if width:
        styles.append(f"width: {width};")

    if nowrap:
        styles.append("white-space: nowrap;")

    return " ".join(styles)


def table_cell_inline_style(background_color: str, row_number: int, total_rows: int) -> str:
    styles = [
        f"background-color: {background_color};",
        "padding: 12px 5px;",
        "vertical-align: middle;",
        "border: none;",
        "color: #222e29;",
        "text-align: left;",
        "word-break: normal;",
        "overflow-wrap: normal;",
    ]

    if row_number < total_rows:
        styles.append(f"border-bottom: 1px solid {ROW_BORDER_COLOR};")

    return " ".join(styles)


def render_product_image_html(product: dict[str, Any]) -> str:
    image_url = clean_text(product.get("image_url"))
    if not image_url:
        return '<span class="muted">No image</span>'

    asin = clean_text(product.get("asin")) or "Product image"
    return (
        f'<img class="product-image" src="{escape_attr(image_url)}" '
        f'alt="{escape_attr(asin)}">'
    )


def render_asin_link_html(product: dict[str, Any]) -> str:
    asin = clean_text(product.get("asin")) or "N/A"
    url = product_url(product)
    if not url:
        return escape_text(asin)

    return f'<a href="{escape_attr(url)}">{escape_text(asin)}</a>'


def render_metrics_html(product: dict[str, Any]) -> str:
    lines = [
        metric_row("Profit:", format_money(product.get("profit")), extra_value_class="profit"),
        metric_row("Sale:", format_money(product.get("sale_price"))),
        metric_row("BSR:", format_int(product.get("sales_rank_current"))),
        metric_row("Sellers:", format_int(product.get("total_seller_count"))),
    ]
    return "\n".join(lines)


def metric_row(
    label: str,
    value: str,
    extra_value_class: str | None = None,
) -> str:
    value_classes = "metric-val"
    if extra_value_class:
        value_classes = f"{value_classes} {extra_value_class}"

    return (
        '<div class="metric-row">'
        f'<span class="metric-key">{escape_text(label)}</span> '
        f'<span class="{escape_attr(value_classes)}">{escape_text(value)}</span>'
        "</div>"
    )


def render_agent_notes_html(product: dict[str, Any]) -> str:
    decision_key = decision_key_from_value(product.get("agent_decision"))
    decision = decision_label(decision_key)
    notes = clean_text(product.get("agent_summary") or product.get("agent_notes"))

    if not notes:
        notes = "No notes yet."

    return (
        f'<span class="decision" style="{escape_attr(decision_badge_inline_style(decision_key))}">'
        f"{escape_text(decision)}</span>"
        f"<br>{escape_text(notes)}"
    )


def render_breakdown_html(
    report: dict[str, Any],
    config: EmailReportConfig,
) -> str:
    warning = clean_text(report.get("warning"))
    if warning:
        return f"<p>{escape_text(warning)}</p>"

    breakdown = clean_text(report.get("agent_breakdown"))
    if not breakdown:
        breakdown = f"{config.agent_name} wasn't able to generate a breakdown."

    return render_simple_markdown_html(breakdown)


def product_text_lines(
    product: dict[str, Any],
    config: EmailReportConfig,
) -> list[str]:
    notes = clean_text(product.get("agent_summary") or product.get("agent_notes")) or "No notes yet."
    return [
        "",
        f"ASIN: {clean_text(product.get('asin')) or 'N/A'}",
        f"URL: {product_url(product) or 'N/A'}",
        (
            "Metrics: "
            f"Profit {format_money(product.get('profit'))}, "
            f"Sale {format_money(product.get('sale_price'))}, "
            f"BSR {format_int(product.get('sales_rank_current'))}, "
            f"Sellers {format_int(product.get('total_seller_count'))}"
        ),
        f"{config.agent_name}'s notes: {decision_label(product.get('agent_decision'))} - {notes}",
    ]


def agent_breakdown_lines(
    report: dict[str, Any],
    config: EmailReportConfig,
) -> list[str]:
    warning = clean_text(report.get("warning"))
    if warning:
        return [warning]

    explicit_breakdown = clean_text(report.get("agent_breakdown"))
    if explicit_breakdown:
        lines = []
        for raw_line in explicit_breakdown.splitlines():
            line = strip_markdown_heading(normalize_email_text(raw_line))
            if line:
                lines.append(line)
        return lines

    return [f"{config.agent_name} wasn't able to generate a breakdown."]


def render_simple_markdown_html(text: str) -> str:
    blocks = []
    bullet_items = []

    for raw_line in text.splitlines():
        line = strip_markdown_heading(normalize_email_text(raw_line))
        if not line:
            if bullet_items:
                blocks.append(render_bullet_list_html(bullet_items))
                bullet_items = []
            continue

        bullet_match = re.match(r"^[-*]\s+(.+)$", line)
        if bullet_match:
            bullet_items.append(bullet_match.group(1).strip())
            continue

        if bullet_items:
            blocks.append(render_bullet_list_html(bullet_items))
            bullet_items = []

        blocks.append(f"<p>{render_inline_markdown_html(line)}</p>")

    if bullet_items:
        blocks.append(render_bullet_list_html(bullet_items))

    if not blocks:
        return "<p>Rover wasn't able to generate a breakdown.</p>"

    return "\n".join(blocks)


def render_bullet_list_html(items: list[str]) -> str:
    rendered_items = "\n".join(
        f'<li style="margin: 0 0 8px;">{render_inline_markdown_html(item)}</li>'
        for item in items
    )
    return f'<ul style="margin: 0 0 12px 18px; padding: 0;">\n{rendered_items}\n</ul>'


def render_inline_markdown_html(text: str) -> str:
    escaped = escape_text(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)


def strip_markdown_heading(line: str) -> str:
    return re.sub(r"^\s*#{1,6}\s*", "", line).strip()


def product_url(product: dict[str, Any]) -> str | None:
    amazon_url = clean_text(product.get("amazon_url"))
    if amazon_url:
        return amazon_url

    asin = clean_text(product.get("asin"))
    if not asin:
        return None

    return f"https://www.amazon.com/dp/{asin}"


def decision_key_from_value(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return "pending"

    key = text.strip().lower().replace(" ", "_").replace("-", "_")
    if key in DECISION_BADGE_STYLES:
        return key

    return "pending"


def decision_label(value: Any) -> str:
    key = decision_key_from_value(value)
    if key == "needs_manual_review":
        return "Review"

    return key.replace("_", " ").title()


def decision_badge_inline_style(decision_key: str) -> str:
    colors = DECISION_BADGE_STYLES.get(decision_key, DECISION_BADGE_STYLES["pending"])
    return " ".join(
        [
            "display: inline-block;",
            "margin-bottom: 6px;",
            'font-family: "Courier New", Courier, monospace;',
            "font-size: 9px;",
            "font-weight: 700;",
            "letter-spacing: 0.08em;",
            "text-transform: uppercase;",
            f"color: {colors['color']};",
            f"background-color: {colors['background']};",
            "padding: 2px 6px;",
            "border-radius: 3px;",
        ]
    )


def format_money(amount: Any) -> str:
    try:
        number = float(amount)
    except (TypeError, ValueError):
        return "N/A"

    return f"${number:,.2f}"


def format_int(number: Any) -> str:
    try:
        return f"{int(number):,}"
    except (TypeError, ValueError):
        return "N/A"


def clean_text(value: Any) -> str | None:
    if value is None:
        return None

    text = normalize_email_text(value)
    if not text:
        return None

    return text


def escape_text(value: Any) -> str:
    return html.escape(normalize_email_text(value), quote=False)


def escape_attr(value: Any) -> str:
    return html.escape(normalize_email_text(value), quote=True)


def normalize_email_text(value: Any) -> str:
    text = str(value or "").strip()
    replacements = {
        "\u2013": "-",
        "\u2014": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    return text
