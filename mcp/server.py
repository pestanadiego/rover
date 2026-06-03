import os
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from db import DEFAULT_DB_PATH, ProductDB
from formatter import (
    format_decision_result,
    format_keyword_status,
    format_pipeline_summary,
    format_product_card,
    format_product_keywords,
    format_product_list,
)


mcp = FastMCP("products")
db = ProductDB(Path(os.environ.get("PRODUCT_DB_PATH", DEFAULT_DB_PATH)))


@mcp.tool()
def get_products_needing_review(limit: int = 10) -> dict[str, Any]:
    """Return products that do not have an agent decision yet."""
    products = db.get_products_needing_review(limit)
    return {
        "count": len(products),
        "products": products,
        "markdown": format_product_list(products),
    }


@mcp.tool()
def get_latest_products(limit: int = 10) -> dict[str, Any]:
    """Return the latest ingested products regardless of review status."""
    products = db.get_latest_products(limit)
    return {
        "count": len(products),
        "products": products,
        "markdown": format_product_list(products),
    }


@mcp.tool()
def get_product_by_asin(asin: str) -> dict[str, Any]:
    """Return the latest product record for an ASIN."""
    product = db.get_product_by_asin(asin)

    if product is None:
        normalized_asin = asin.strip().upper()
        return {
            "found": False,
            "message": f"No product found with ASIN {normalized_asin}.",
        }

    return {
        "found": True,
        "product": product,
        "markdown": format_product_card(product),
    }


@mcp.tool()
def write_decision(
    asin: str,
    decision: str,
    notes: str = "",
    analysis: str | None = None,
    summary: str | None = None,
) -> dict[str, Any]:
    """Save an agent review decision for a product."""
    result = db.write_decision(asin, decision, notes, analysis=analysis, summary=summary)
    return {
        **result,
        "markdown": format_decision_result(result),
    }


@mcp.tool()
def get_reviewed_products(
    decision: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Return products that already have an agent decision."""
    products = db.get_reviewed_products(decision, limit)
    return {
        "count": len(products),
        "products": products,
        "markdown": format_product_list(products),
    }


@mcp.tool()
def search_products(
    keyword: str | None = None,
    min_roi: float | None = None,
    min_profit: float | None = None,
    max_sales_rank: int | None = None,
    min_estimated_sales: int | None = None,
    max_sellers: int | None = None,
    data_quality: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Search products using safe filters."""
    products = db.search_products(
        keyword=keyword,
        min_roi=min_roi,
        min_profit=min_profit,
        max_sales_rank=max_sales_rank,
        min_estimated_sales=min_estimated_sales,
        max_sellers=max_sellers,
        data_quality=data_quality,
        limit=limit,
    )
    return {
        "count": len(products),
        "products": products,
        "markdown": format_product_list(products),
    }


@mcp.tool()
def get_products_by_scrape_keyword(
    keyword: str,
    limit: int = 20,
) -> dict[str, Any]:
    """Return products found by a specific scrape keyword."""
    products = db.get_products_by_scrape_keyword(keyword, limit)
    return {
        "count": len(products),
        "products": products,
        "markdown": format_product_list(products),
    }


@mcp.tool()
def get_product_keywords(asin: str) -> dict[str, Any]:
    """Return keyword matches recorded for a product ASIN."""
    rows = db.get_product_keywords(asin)
    return {
        "count": len(rows),
        "keywords": rows,
        "markdown": format_product_keywords(rows),
    }


@mcp.tool()
def get_keyword_status(limit: int = 100) -> dict[str, Any]:
    """Return scraper keyword status rows."""
    rows = db.get_keyword_status(limit)
    return {
        "count": len(rows),
        "keywords": rows,
        "markdown": format_keyword_status(rows),
    }


@mcp.tool()
def get_pipeline_summary() -> dict[str, Any]:
    """Return product ingestion, review, and keyword summary counts."""
    summary = db.get_pipeline_summary()
    return {
        "summary": summary,
        "markdown": format_pipeline_summary(summary),
    }


if __name__ == "__main__":
    if len(sys.argv) > 1:
        print("server.py does not accept command-line arguments.")
        raise SystemExit(1)

    mcp.run()
