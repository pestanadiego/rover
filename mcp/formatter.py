from typing import Any


def format_product_card(product: dict[str, Any]) -> str:
    asin = value(product.get("asin"))
    name = value(product.get("name"))
    roi = format_percent(product.get("roi_percent"))
    margin = format_percent(product.get("profit_margin_percent"))
    warnings = format_json_text(product.get("validation_warnings"))

    lines = [
        f"### {asin} - {name}",
        "",
        f"- Category: {value(product.get('category'))}",
        f"- Brand: {value(product.get('brand'))}",
        f"- Manufacturer: {value(product.get('manufacturer'))}",
        f"- Scrape Keyword: {value(product.get('scrape_keyword'))}",
        f"- SellerAmp Search Term: {value(product.get('selleramp_search_term'))}",
        f"- Amazon URL: {value(product.get('amazon_url'))}",
        f"- Cost Price: {format_money(product.get('cost_price'))}",
        f"- Sale Price: {format_money(product.get('sale_price'))}",
        f"- Current Buy Box: {format_money(product.get('buy_box_current'))}",
        f"- 180d Avg Buy Box: {format_money(product.get('buy_box_average_180d'))}",
        f"- Buy Box Delta: {format_percent(product.get('buy_box_delta_percent'))}",
        f"- Profit: {format_money(product.get('profit'))}",
        f"- ROI: {roi}",
        f"- Profit Margin: {margin}",
        f"- Breakeven: {format_money(product.get('breakeven'))}",
        f"- Max Cost: {format_money(product.get('max_cost'))}",
        f"- Spread to Max Cost: {format_money(product.get('spread_to_max_cost'))}",
        f"- Spread to Breakeven: {format_money(product.get('spread_to_breakeven'))}",
        f"- BSR: {format_int(product.get('sales_rank_current'))}",
        f"- Estimated Sales: {format_int(product.get('estimated_sales'))}",
        (
            "- Sellers: "
            f"{format_int(product.get('total_seller_count'))} total, "
            f"{format_int(product.get('fba_seller_count'))} FBA, "
            f"{format_int(product.get('fbm_seller_count'))} FBM"
        ),
        f"- Data Quality: {value(product.get('data_quality'))}",
        f"- Validation Warnings: {warnings}",
        f"- Agent Status: {value(product.get('agent_status'))}",
        f"- Agent Decision: {value(product.get('agent_decision'))}",
        f"- Agent Summary: {value(product.get('agent_summary') or product.get('agent_notes'))}",
        f"- Agent Analysis: {value(product.get('agent_analysis'))}",
        f"- Imported At: {value(product.get('imported_at_utc'))}",
    ]

    return "\n".join(lines).strip()


def format_product_list(products: list[dict[str, Any]]) -> str:
    if not products:
        return "No products found."

    return "\n\n---\n\n".join(format_product_card(product) for product in products)


def format_decision_result(result: dict[str, Any]) -> str:
    if not result.get("saved"):
        return str(result.get("message", "Decision was not saved."))

    product = result.get("product")
    if not product:
        return str(result.get("message", "Decision saved."))

    return "\n\n".join([str(result["message"]), format_product_card(product)])


def format_keyword_status(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No keyword status rows found."

    lines = [
        "| Keyword | Status | Last Result Count | Total Found | Last Scraped | Last Error |",
        "|---|---:|---:|---:|---|---|",
    ]

    for row in rows:
        lines.append(
            "| "
            f"{value(row.get('keyword'))} | "
            f"{value(row.get('status'))} | "
            f"{format_int(row.get('last_result_count'))} | "
            f"{format_int(row.get('total_products_found'))} | "
            f"{value(row.get('last_scraped_at_utc'))} | "
            f"{value(row.get('last_error'))} |"
        )

    return "\n".join(lines)


def format_product_keywords(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No keyword matches found for this product."

    lines = [
        "| Keyword | Scrape Run | Raw Row | Exported At | Imported At |",
        "|---|---:|---:|---|---|",
    ]

    for row in rows:
        lines.append(
            "| "
            f"{value(row.get('keyword'))} | "
            f"{format_int(row.get('scrape_run_id'))} | "
            f"{format_int(row.get('raw_row_number'))} | "
            f"{value(row.get('exported_at_utc'))} | "
            f"{value(row.get('imported_at_utc'))} |"
        )

    return "\n".join(lines)


def format_pipeline_summary(summary: dict[str, Any]) -> str:
    latest_run = summary.get("latest_normalization_run") or {}

    lines = [
        "### Pipeline Summary",
        "",
        f"- Total Products: {format_int(summary.get('total_products'))}",
        f"- Pending Reviews: {format_int(summary.get('pending_reviews'))}",
        f"- Reviewed Products: {format_int(summary.get('reviewed_products'))}",
        f"- Kept Products: {format_int(summary.get('kept_products'))}",
        f"- Rejected Products: {format_int(summary.get('rejected_products'))}",
        f"- Watchlist Products: {format_int(summary.get('watchlist_products'))}",
        f"- Manual Review Products: {format_int(summary.get('manual_review_products'))}",
        f"- Complete Data Products: {format_int(summary.get('complete_data_products'))}",
        f"- Partial Data Products: {format_int(summary.get('partial_data_products'))}",
        f"- Rejected Import Rows: {format_int(summary.get('rejected_import_rows'))}",
        f"- Keywords Total: {format_int(summary.get('keywords_total'))}",
        f"- Keywords With No Results: {format_int(summary.get('keywords_no_results'))}",
        f"- Keywords With Errors: {format_int(summary.get('keywords_error'))}",
        "",
        "### Latest Normalization Run",
        "",
        f"- Raw File: {value(latest_run.get('raw_file'))}",
        f"- Imported At: {value(latest_run.get('imported_at_utc'))}",
        f"- Total Rows: {format_int(latest_run.get('total_rows'))}",
        f"- Saved Rows: {format_int(latest_run.get('saved_rows'))}",
        f"- Rejected Rows: {format_int(latest_run.get('rejected_rows'))}",
    ]

    return "\n".join(lines).strip()


def format_money(amount: Any) -> str:
    if amount is None:
        return "N/A"

    try:
        return f"${float(amount):,.2f}"
    except (TypeError, ValueError):
        return str(amount)


def format_percent(percent: Any) -> str:
    if percent is None:
        return "N/A"

    try:
        return f"{float(percent):,.2f}%"
    except (TypeError, ValueError):
        return str(percent)


def format_int(number: Any) -> str:
    if number is None:
        return "N/A"

    try:
        return f"{int(number):,}"
    except (TypeError, ValueError):
        return str(number)


def format_json_text(text: Any) -> str:
    if not text:
        return "N/A"

    if isinstance(text, list):
        return "; ".join(str(item) for item in text) or "N/A"

    cleaned = str(text).strip()
    if cleaned in {"[]", ""}:
        return "N/A"

    return cleaned


def value(raw_value: Any) -> str:
    if raw_value is None:
        return "N/A"

    text = str(raw_value).strip()
    if not text:
        return "N/A"

    return text
