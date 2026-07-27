"""Google Search Console MCP tools.

All tools are prefixed with gsc_ to namespace them alongside Google Ads tools.
Call register(mcp) to add these tools to an existing FastMCP instance.
"""

import json
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from gsc_mcp.auth import get_gsc_service

logger = logging.getLogger(__name__)


def register(mcp):
    """Register all GSC tools on the given FastMCP server instance."""

    @mcp.tool()
    def gsc_list_properties() -> str:
        """List all Google Search Console properties the account has access to.

        Always call this first to get the exact site_url needed for other GSC tools.
        Returns site URLs and permission levels.
        """
        try:
            service = get_gsc_service()
            site_list = service.sites().list().execute()
            sites = site_list.get("siteEntry", [])

            if not sites:
                return "No Search Console properties found."

            return json.dumps({
                "count": len(sites),
                "properties": [
                    {
                        "site_url": site.get("siteUrl", "Unknown"),
                        "permission_level": site.get("permissionLevel", "Unknown"),
                    }
                    for site in sites
                ],
            })
        except Exception as e:
            return f"Error retrieving properties: {str(e)}"

    @mcp.tool()
    def gsc_search_analytics(
        site_url: str,
        days: int = 28,
        dimensions: str = "query",
        row_limit: int = 20,
    ) -> str:
        """Get search analytics data (clicks, impressions, CTR, position).

        Args:
            site_url: Exact GSC property URL from gsc_list_properties
                (e.g. "https://example.com/" or "sc-domain:example.com")
            days: Number of days to look back (default: 28)
            dimensions: Comma-separated dimensions to group by.
                Options: query, page, device, country, date
            row_limit: Number of rows to return (default: 20, max: 500)
        """
        try:
            service = get_gsc_service()
            end_date = datetime.now().date()
            start_date = end_date - timedelta(days=days)
            dimension_list = [d.strip() for d in dimensions.split(",")]

            request = {
                "startDate": start_date.strftime("%Y-%m-%d"),
                "endDate": end_date.strftime("%Y-%m-%d"),
                "dimensions": dimension_list,
                "rowLimit": min(max(1, row_limit), 500),
                "dataState": "all",
            }

            response = service.searchanalytics().query(
                siteUrl=site_url, body=request
            ).execute()

            if not response.get("rows"):
                return f"No data found for {site_url} in the last {days} days."

            rows = []
            for row in response.get("rows", []):
                entry = {}
                for i, dim in enumerate(dimension_list):
                    entry[dim] = row.get("keys", [])[i] if i < len(row.get("keys", [])) else None
                entry["clicks"] = row.get("clicks", 0)
                entry["impressions"] = row.get("impressions", 0)
                entry["ctr"] = round(row.get("ctr", 0), 4)
                entry["position"] = round(row.get("position", 0), 1)
                rows.append(entry)

            return json.dumps({
                "site_url": site_url,
                "date_range": {
                    "start": start_date.strftime("%Y-%m-%d"),
                    "end": end_date.strftime("%Y-%m-%d"),
                    "days": days,
                },
                "dimensions": dimension_list,
                "row_count": len(rows),
                "rows": rows,
            })
        except Exception as e:
            return f"Error retrieving search analytics: {str(e)}"

    @mcp.tool()
    def gsc_advanced_search_analytics(
        site_url: str,
        start_date: str = None,
        end_date: str = None,
        dimensions: str = "query",
        search_type: str = "WEB",
        row_limit: int = 1000,
        start_row: int = 0,
        sort_by: str = "clicks",
        sort_direction: str = "descending",
        filter_dimension: str = None,
        filter_operator: str = "contains",
        filter_expression: str = None,
        filters: str = None,
    ) -> str:
        """Advanced search analytics with sorting, filtering, and pagination up to 25K rows.

        Args:
            site_url: Exact GSC property URL from gsc_list_properties
            start_date: Start date YYYY-MM-DD (defaults to 28 days ago)
            end_date: End date YYYY-MM-DD (defaults to today)
            dimensions: Comma-separated dimensions (query, page, device, country, date)
            search_type: WEB, IMAGE, VIDEO, NEWS, or DISCOVER
            row_limit: Max rows (up to 25000)
            start_row: Pagination offset
            sort_by: clicks, impressions, ctr, or position
            sort_direction: ascending or descending
            filter_dimension: Single filter dimension (query, page, country, device)
            filter_operator: contains, equals, notContains, notEquals
            filter_expression: Filter value
            filters: JSON array for multi-filter. Overrides single filter params.
                Example: [{"dimension":"country","operator":"equals","expression":"nld"}]
        """
        try:
            service = get_gsc_service()

            if not end_date:
                end_date = datetime.now().date().strftime("%Y-%m-%d")
            if not start_date:
                start_date = (datetime.now().date() - timedelta(days=28)).strftime("%Y-%m-%d")

            dimension_list = [d.strip() for d in dimensions.split(",")]

            request = {
                "startDate": start_date,
                "endDate": end_date,
                "dimensions": dimension_list,
                "rowLimit": min(row_limit, 25000),
                "startRow": start_row,
                "searchType": search_type.upper(),
                "dataState": "all",
            }

            # Sorting
            metric_map = {
                "clicks": "CLICK_COUNT",
                "impressions": "IMPRESSION_COUNT",
                "ctr": "CTR",
                "position": "POSITION",
            }
            if sort_by in metric_map:
                request["orderBy"] = [{
                    "metric": metric_map[sort_by],
                    "direction": sort_direction.lower(),
                }]

            # Filters
            active_filters = []
            if filters:
                try:
                    filter_list = json.loads(filters)
                except json.JSONDecodeError:
                    return "Invalid filters JSON."
                if not isinstance(filter_list, list) or len(filter_list) == 0:
                    return "Filters must be a non-empty JSON array."
                request["dimensionFilterGroups"] = [{"filters": filter_list}]
                active_filters = filter_list
            elif filter_dimension and filter_expression:
                single_filter = {
                    "dimension": filter_dimension,
                    "operator": filter_operator,
                    "expression": filter_expression,
                }
                request["dimensionFilterGroups"] = [{"filters": [single_filter]}]
                active_filters = [single_filter]

            response = service.searchanalytics().query(
                siteUrl=site_url, body=request
            ).execute()

            if not response.get("rows"):
                return f"No data found for the specified parameters."

            rows = []
            for row in response.get("rows", []):
                entry = {}
                for i, dim in enumerate(dimension_list):
                    entry[dim] = row.get("keys", [])[i] if i < len(row.get("keys", [])) else None
                entry["clicks"] = row.get("clicks", 0)
                entry["impressions"] = row.get("impressions", 0)
                entry["ctr"] = round(row.get("ctr", 0), 4)
                entry["position"] = round(row.get("position", 0), 1)
                rows.append(entry)

            has_more = len(response.get("rows", [])) == row_limit
            return json.dumps({
                "site_url": site_url,
                "date_range": {"start": start_date, "end": end_date},
                "search_type": search_type,
                "dimensions": dimension_list,
                "filters_applied": active_filters,
                "pagination": {
                    "start_row": start_row,
                    "row_count": len(rows),
                    "has_more": has_more,
                    "next_start_row": start_row + row_limit if has_more else None,
                },
                "rows": rows,
            })
        except Exception as e:
            return f"Error: {str(e)}"

    @mcp.tool()
    def gsc_performance_overview(site_url: str, days: int = 28) -> str:
        """Get a performance summary with totals and daily trend.

        Args:
            site_url: Exact GSC property URL from gsc_list_properties
            days: Number of days to look back (default: 28)
        """
        try:
            service = get_gsc_service()
            end_date = datetime.now().date()
            start_date = end_date - timedelta(days=days)

            # Totals
            total_request = {
                "startDate": start_date.strftime("%Y-%m-%d"),
                "endDate": end_date.strftime("%Y-%m-%d"),
                "dimensions": [],
                "rowLimit": 1,
                "dataState": "all",
            }
            total_response = service.searchanalytics().query(
                siteUrl=site_url, body=total_request
            ).execute()

            # Daily trend
            date_request = {
                "startDate": start_date.strftime("%Y-%m-%d"),
                "endDate": end_date.strftime("%Y-%m-%d"),
                "dimensions": ["date"],
                "rowLimit": days,
                "dataState": "all",
            }
            date_response = service.searchanalytics().query(
                siteUrl=site_url, body=date_request
            ).execute()

            if not total_response.get("rows"):
                return f"No data for {site_url} in the last {days} days."

            totals_row = total_response["rows"][0]
            totals = {
                "clicks": totals_row.get("clicks", 0),
                "impressions": totals_row.get("impressions", 0),
                "ctr": round(totals_row.get("ctr", 0), 4),
                "position": round(totals_row.get("position", 0), 1),
            }

            daily_trend = []
            if date_response.get("rows"):
                for row in sorted(date_response["rows"], key=lambda x: x["keys"][0]):
                    daily_trend.append({
                        "date": row["keys"][0],
                        "clicks": row.get("clicks", 0),
                        "impressions": row.get("impressions", 0),
                        "ctr": round(row.get("ctr", 0), 4),
                        "position": round(row.get("position", 0), 1),
                    })

            return json.dumps({
                "site_url": site_url,
                "date_range": {
                    "start": start_date.strftime("%Y-%m-%d"),
                    "end": end_date.strftime("%Y-%m-%d"),
                    "days": days,
                },
                "totals": totals,
                "daily_trend": daily_trend,
            })
        except Exception as e:
            return f"Error: {str(e)}"

    @mcp.tool()
    def gsc_compare_periods(
        site_url: str,
        period1_start: str,
        period1_end: str,
        period2_start: str,
        period2_end: str,
        dimensions: str = "query",
        limit: int = 20,
    ) -> str:
        """Compare search performance between two time periods.

        Args:
            site_url: Exact GSC property URL from gsc_list_properties
            period1_start: Start date period 1 (YYYY-MM-DD)
            period1_end: End date period 1 (YYYY-MM-DD)
            period2_start: Start date period 2 (YYYY-MM-DD)
            period2_end: End date period 2 (YYYY-MM-DD)
            dimensions: Comma-separated dimensions (default: query)
            limit: Number of top results to compare (default: 20)
        """
        try:
            service = get_gsc_service()
            dimension_list = [d.strip() for d in dimensions.split(",")]

            def _fetch(start, end):
                req = {
                    "startDate": start,
                    "endDate": end,
                    "dimensions": dimension_list,
                    "rowLimit": 1000,
                    "dataState": "all",
                }
                return service.searchanalytics().query(
                    siteUrl=site_url, body=req
                ).execute().get("rows", [])

            p1_rows = _fetch(period1_start, period1_end)
            p2_rows = _fetch(period2_start, period2_end)

            if not p1_rows and not p2_rows:
                return f"No data for either period."

            p1_data = {tuple(r.get("keys", [])): r for r in p1_rows}
            p2_data = {tuple(r.get("keys", [])): r for r in p2_rows}
            all_keys = set(p1_data.keys()) | set(p2_data.keys())

            comparisons = []
            for key in all_keys:
                p1 = p1_data.get(key, {"clicks": 0, "impressions": 0, "ctr": 0, "position": 0})
                p2 = p2_data.get(key, {"clicks": 0, "impressions": 0, "ctr": 0, "position": 0})
                click_diff = p2.get("clicks", 0) - p1.get("clicks", 0)
                comparisons.append({
                    "key": list(key),
                    "p1_clicks": p1.get("clicks", 0),
                    "p2_clicks": p2.get("clicks", 0),
                    "click_diff": click_diff,
                    "p1_impressions": p1.get("impressions", 0),
                    "p2_impressions": p2.get("impressions", 0),
                    "p1_ctr": round(p1.get("ctr", 0), 4),
                    "p2_ctr": round(p2.get("ctr", 0), 4),
                    "p1_position": round(p1.get("position", 0), 1),
                    "p2_position": round(p2.get("position", 0), 1),
                    "position_change": round(p1.get("position", 0) - p2.get("position", 0), 1),
                })

            comparisons.sort(key=lambda x: abs(x["click_diff"]), reverse=True)

            return json.dumps({
                "site_url": site_url,
                "period1": {"start": period1_start, "end": period1_end},
                "period2": {"start": period2_start, "end": period2_end},
                "dimensions": dimension_list,
                "total_items": len(comparisons),
                "showing": min(limit, len(comparisons)),
                "comparison": comparisons[:limit],
            })
        except Exception as e:
            return f"Error: {str(e)}"

    @mcp.tool()
    def gsc_search_by_page(
        site_url: str,
        page_url: str,
        days: int = 28,
        row_limit: int = 20,
    ) -> str:
        """Get search queries driving traffic to a specific page.

        Args:
            site_url: Exact GSC property URL from gsc_list_properties
            page_url: The specific page URL to analyze
            days: Number of days to look back (default: 28)
            row_limit: Number of rows (default: 20, max: 500)
        """
        try:
            service = get_gsc_service()
            end_date = datetime.now().date()
            start_date = end_date - timedelta(days=days)

            request = {
                "startDate": start_date.strftime("%Y-%m-%d"),
                "endDate": end_date.strftime("%Y-%m-%d"),
                "dimensions": ["query"],
                "dimensionFilterGroups": [{
                    "filters": [{
                        "dimension": "page",
                        "operator": "equals",
                        "expression": page_url,
                    }]
                }],
                "rowLimit": min(max(1, row_limit), 500),
                "orderBy": [{"metric": "CLICK_COUNT", "direction": "descending"}],
                "dataState": "all",
            }

            response = service.searchanalytics().query(
                siteUrl=site_url, body=request
            ).execute()

            if not response.get("rows"):
                return f"No search data for page {page_url} in the last {days} days."

            rows = []
            for row in response.get("rows", []):
                rows.append({
                    "query": row.get("keys", ["Unknown"])[0],
                    "clicks": row.get("clicks", 0),
                    "impressions": row.get("impressions", 0),
                    "ctr": round(row.get("ctr", 0), 4),
                    "position": round(row.get("position", 0), 1),
                })

            total_clicks = sum(r["clicks"] for r in rows)
            total_impressions = sum(r["impressions"] for r in rows)

            return json.dumps({
                "site_url": site_url,
                "page_url": page_url,
                "date_range": {
                    "start": start_date.strftime("%Y-%m-%d"),
                    "end": end_date.strftime("%Y-%m-%d"),
                    "days": days,
                },
                "totals": {
                    "clicks": total_clicks,
                    "impressions": total_impressions,
                    "avg_ctr": round(total_clicks / total_impressions, 4) if total_impressions > 0 else 0,
                },
                "row_count": len(rows),
                "rows": rows,
            })
        except Exception as e:
            return f"Error: {str(e)}"

    @mcp.tool()
    def gsc_inspect_url(site_url: str, page_url: str) -> str:
        """Inspect a URL for indexing status, crawl info, and rich results.

        Args:
            site_url: Exact GSC property URL from gsc_list_properties
            page_url: The specific URL to inspect
        """
        try:
            service = get_gsc_service()

            request = {
                "inspectionUrl": page_url,
                "siteUrl": site_url,
            }

            response = service.urlInspection().index().inspect(
                body=request
            ).execute()

            if not response or "inspectionResult" not in response:
                return f"No inspection data for {page_url}."

            inspection = response["inspectionResult"]
            index_status = inspection.get("indexStatusResult", {})

            last_crawled = None
            if "lastCrawlTime" in index_status:
                try:
                    crawl_time = datetime.fromisoformat(
                        index_status["lastCrawlTime"].replace("Z", "+00:00")
                    )
                    last_crawled = crawl_time.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    last_crawled = index_status["lastCrawlTime"]

            rich_results = None
            if "richResultsResult" in inspection:
                rich = inspection["richResultsResult"]
                rich_results = {
                    "verdict": rich.get("verdict", "UNKNOWN"),
                    "detected_types": [
                        item.get("richResultType", "Unknown")
                        for item in rich.get("detectedItems", [])
                    ],
                }

            return json.dumps({
                "page_url": page_url,
                "site_url": site_url,
                "verdict": index_status.get("verdict", "UNKNOWN"),
                "coverage_state": index_status.get("coverageState"),
                "last_crawled": last_crawled,
                "page_fetch_state": index_status.get("pageFetchState"),
                "robots_txt_state": index_status.get("robotsTxtState"),
                "indexing_state": index_status.get("indexingState"),
                "google_canonical": index_status.get("googleCanonical"),
                "user_canonical": index_status.get("userCanonical"),
                "crawled_as": index_status.get("crawledAs"),
                "referring_urls": index_status.get("referringUrls", [])[:5],
                "rich_results": rich_results,
            })
        except Exception as e:
            return f"Error inspecting URL: {str(e)}"
