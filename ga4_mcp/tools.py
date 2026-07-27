"""Google Analytics 4 MCP tools.

All tools are prefixed with ga4_ to namespace them alongside Google Ads and GSC tools.
Call register(mcp) to add these tools to an existing FastMCP instance.
"""

import json
import logging
from datetime import datetime, timedelta

from ga4_mcp.auth import get_ga4_data_client, get_ga4_admin_client

logger = logging.getLogger(__name__)


def register(mcp):
    """Register all GA4 tools on the given FastMCP server instance."""

    @mcp.tool()
    def ga4_list_properties() -> str:
        """List all GA4 properties the account has access to.

        Always call this first to get property IDs needed for other GA4 tools.
        Returns property IDs, display names, and time zones.
        """
        try:
            admin_client = get_ga4_admin_client()
            accounts = admin_client.list_account_summaries()

            properties = []
            for account in accounts:
                for prop in account.property_summaries:
                    properties.append({
                        "property_id": prop.property.replace("properties/", ""),
                        "display_name": prop.display_name,
                        "account": account.display_name,
                    })

            if not properties:
                return "No GA4 properties found."

            return json.dumps({
                "count": len(properties),
                "properties": properties,
            })
        except Exception as e:
            return f"Error listing GA4 properties: {str(e)}"

    @mcp.tool()
    def ga4_run_report(
        property_id: str,
        dimensions: str = "date",
        metrics: str = "sessions,totalUsers,screenPageViews",
        days: int = 28,
        start_date: str = None,
        end_date: str = None,
        row_limit: int = 50,
        dimension_filter: str = None,
    ) -> str:
        """Run a flexible GA4 report with any dimensions and metrics.

        Args:
            property_id: GA4 property ID (digits only, from ga4_list_properties)
            dimensions: Comma-separated dimensions (e.g. date, sessionSource,
                sessionMedium, pagePath, country, city, deviceCategory, browser,
                operatingSystem, landingPage, sessionDefaultChannelGroup)
            metrics: Comma-separated metrics (e.g. sessions, totalUsers,
                newUsers, screenPageViews, bounceRate, averageSessionDuration,
                conversions, eventCount, engagementRate, userEngagementDuration)
            days: Number of days to look back (default: 28). Ignored if
                start_date/end_date are provided.
            start_date: Start date YYYY-MM-DD (optional, overrides days)
            end_date: End date YYYY-MM-DD (optional, defaults to today)
            row_limit: Max rows to return (default: 50, max: 10000)
            dimension_filter: Optional filter as "dimension==value" or
                "dimension contains value" (e.g. "pagePath contains /blog")
        """
        try:
            from google.analytics.data_v1beta.types import (
                RunReportRequest,
                DateRange,
                Dimension,
                Metric,
                FilterExpression,
                Filter,
            )

            client = get_ga4_data_client()

            if not end_date:
                end_date = datetime.now().date().strftime("%Y-%m-%d")
            if not start_date:
                start_date = (datetime.now().date() - timedelta(days=days)).strftime("%Y-%m-%d")

            dimension_list = [Dimension(name=d.strip()) for d in dimensions.split(",")]
            metric_list = [Metric(name=m.strip()) for m in metrics.split(",")]

            request = RunReportRequest(
                property=f"properties/{property_id}",
                dimensions=dimension_list,
                metrics=metric_list,
                date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
                limit=min(row_limit, 10000),
            )

            # Add dimension filter if provided
            if dimension_filter:
                if " contains " in dimension_filter:
                    dim_name, value = dimension_filter.split(" contains ", 1)
                    request.dimension_filter = FilterExpression(
                        filter=Filter(
                            field_name=dim_name.strip(),
                            string_filter=Filter.StringFilter(
                                match_type=Filter.StringFilter.MatchType.CONTAINS,
                                value=value.strip(),
                            ),
                        )
                    )
                elif "==" in dimension_filter:
                    dim_name, value = dimension_filter.split("==", 1)
                    request.dimension_filter = FilterExpression(
                        filter=Filter(
                            field_name=dim_name.strip(),
                            string_filter=Filter.StringFilter(
                                match_type=Filter.StringFilter.MatchType.EXACT,
                                value=value.strip(),
                            ),
                        )
                    )

            response = client.run_report(request)

            if not response.rows:
                return f"No data found for property {property_id}."

            dim_names = [d.strip() for d in dimensions.split(",")]
            met_names = [m.strip() for m in metrics.split(",")]

            rows = []
            for row in response.rows:
                entry = {}
                for i, dim_val in enumerate(row.dimension_values):
                    entry[dim_names[i]] = dim_val.value
                for i, met_val in enumerate(row.metric_values):
                    entry[met_names[i]] = met_val.value
                rows.append(entry)

            return json.dumps({
                "property_id": property_id,
                "date_range": {"start": start_date, "end": end_date},
                "dimensions": dim_names,
                "metrics": met_names,
                "row_count": len(rows),
                "total_rows": response.row_count,
                "rows": rows,
            })
        except Exception as e:
            return f"Error running GA4 report: {str(e)}"

    @mcp.tool()
    def ga4_traffic_overview(property_id: str, days: int = 28) -> str:
        """Get a traffic overview: sessions, users, pageviews, bounce rate, avg duration.

        Args:
            property_id: GA4 property ID (digits only, from ga4_list_properties)
            days: Number of days to look back (default: 28)
        """
        try:
            from google.analytics.data_v1beta.types import (
                RunReportRequest,
                DateRange,
                Dimension,
                Metric,
            )

            client = get_ga4_data_client()
            end_date = datetime.now().date().strftime("%Y-%m-%d")
            start_date = (datetime.now().date() - timedelta(days=days)).strftime("%Y-%m-%d")

            # Totals (no dimensions)
            totals_request = RunReportRequest(
                property=f"properties/{property_id}",
                metrics=[
                    Metric(name="sessions"),
                    Metric(name="totalUsers"),
                    Metric(name="newUsers"),
                    Metric(name="screenPageViews"),
                    Metric(name="bounceRate"),
                    Metric(name="averageSessionDuration"),
                    Metric(name="engagementRate"),
                    Metric(name="conversions"),
                ],
                date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
            )
            totals_response = client.run_report(totals_request)

            # Daily trend
            daily_request = RunReportRequest(
                property=f"properties/{property_id}",
                dimensions=[Dimension(name="date")],
                metrics=[
                    Metric(name="sessions"),
                    Metric(name="totalUsers"),
                    Metric(name="screenPageViews"),
                ],
                date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
                limit=days,
            )
            daily_response = client.run_report(daily_request)

            if not totals_response.rows:
                return f"No traffic data for property {property_id}."

            totals_row = totals_response.rows[0]
            met_names = ["sessions", "totalUsers", "newUsers", "screenPageViews",
                         "bounceRate", "averageSessionDuration", "engagementRate", "conversions"]
            totals = {}
            for i, name in enumerate(met_names):
                totals[name] = totals_row.metric_values[i].value

            daily_trend = []
            if daily_response.rows:
                for row in sorted(daily_response.rows, key=lambda r: r.dimension_values[0].value):
                    daily_trend.append({
                        "date": row.dimension_values[0].value,
                        "sessions": row.metric_values[0].value,
                        "users": row.metric_values[1].value,
                        "pageviews": row.metric_values[2].value,
                    })

            return json.dumps({
                "property_id": property_id,
                "date_range": {"start": start_date, "end": end_date, "days": days},
                "totals": totals,
                "daily_trend": daily_trend,
            })
        except Exception as e:
            return f"Error: {str(e)}"

    @mcp.tool()
    def ga4_traffic_sources(
        property_id: str, days: int = 28, row_limit: int = 20
    ) -> str:
        """Get traffic sources breakdown by channel, source, and medium.

        Args:
            property_id: GA4 property ID (digits only, from ga4_list_properties)
            days: Number of days to look back (default: 28)
            row_limit: Number of rows (default: 20)
        """
        try:
            from google.analytics.data_v1beta.types import (
                RunReportRequest,
                DateRange,
                Dimension,
                Metric,
                OrderBy,
            )

            client = get_ga4_data_client()
            end_date = datetime.now().date().strftime("%Y-%m-%d")
            start_date = (datetime.now().date() - timedelta(days=days)).strftime("%Y-%m-%d")

            request = RunReportRequest(
                property=f"properties/{property_id}",
                dimensions=[
                    Dimension(name="sessionDefaultChannelGroup"),
                    Dimension(name="sessionSource"),
                    Dimension(name="sessionMedium"),
                ],
                metrics=[
                    Metric(name="sessions"),
                    Metric(name="totalUsers"),
                    Metric(name="conversions"),
                    Metric(name="engagementRate"),
                ],
                date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
                order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
                limit=row_limit,
            )

            response = client.run_report(request)

            if not response.rows:
                return f"No traffic source data for property {property_id}."

            rows = []
            for row in response.rows:
                rows.append({
                    "channel": row.dimension_values[0].value,
                    "source": row.dimension_values[1].value,
                    "medium": row.dimension_values[2].value,
                    "sessions": row.metric_values[0].value,
                    "users": row.metric_values[1].value,
                    "conversions": row.metric_values[2].value,
                    "engagement_rate": row.metric_values[3].value,
                })

            return json.dumps({
                "property_id": property_id,
                "date_range": {"start": start_date, "end": end_date, "days": days},
                "row_count": len(rows),
                "rows": rows,
            })
        except Exception as e:
            return f"Error: {str(e)}"

    @mcp.tool()
    def ga4_top_pages(
        property_id: str, days: int = 28, row_limit: int = 20
    ) -> str:
        """Get top pages by pageviews.

        Args:
            property_id: GA4 property ID (digits only, from ga4_list_properties)
            days: Number of days to look back (default: 28)
            row_limit: Number of rows (default: 20)
        """
        try:
            from google.analytics.data_v1beta.types import (
                RunReportRequest,
                DateRange,
                Dimension,
                Metric,
                OrderBy,
            )

            client = get_ga4_data_client()
            end_date = datetime.now().date().strftime("%Y-%m-%d")
            start_date = (datetime.now().date() - timedelta(days=days)).strftime("%Y-%m-%d")

            request = RunReportRequest(
                property=f"properties/{property_id}",
                dimensions=[
                    Dimension(name="pagePath"),
                    Dimension(name="pageTitle"),
                ],
                metrics=[
                    Metric(name="screenPageViews"),
                    Metric(name="totalUsers"),
                    Metric(name="averageSessionDuration"),
                    Metric(name="bounceRate"),
                ],
                date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
                order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="screenPageViews"), desc=True)],
                limit=row_limit,
            )

            response = client.run_report(request)

            if not response.rows:
                return f"No page data for property {property_id}."

            rows = []
            for row in response.rows:
                rows.append({
                    "page_path": row.dimension_values[0].value,
                    "page_title": row.dimension_values[1].value,
                    "pageviews": row.metric_values[0].value,
                    "users": row.metric_values[1].value,
                    "avg_session_duration": row.metric_values[2].value,
                    "bounce_rate": row.metric_values[3].value,
                })

            return json.dumps({
                "property_id": property_id,
                "date_range": {"start": start_date, "end": end_date, "days": days},
                "row_count": len(rows),
                "rows": rows,
            })
        except Exception as e:
            return f"Error: {str(e)}"

    @mcp.tool()
    def ga4_conversions(property_id: str, days: int = 28) -> str:
        """Get conversion events with counts and breakdown by source.

        Args:
            property_id: GA4 property ID (digits only, from ga4_list_properties)
            days: Number of days to look back (default: 28)
        """
        try:
            from google.analytics.data_v1beta.types import (
                RunReportRequest,
                DateRange,
                Dimension,
                Metric,
                OrderBy,
                FilterExpression,
                Filter,
            )

            client = get_ga4_data_client()
            end_date = datetime.now().date().strftime("%Y-%m-%d")
            start_date = (datetime.now().date() - timedelta(days=days)).strftime("%Y-%m-%d")

            # Conversions by event name
            request = RunReportRequest(
                property=f"properties/{property_id}",
                dimensions=[
                    Dimension(name="eventName"),
                    Dimension(name="sessionDefaultChannelGroup"),
                ],
                metrics=[
                    Metric(name="conversions"),
                    Metric(name="totalRevenue"),
                ],
                date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
                dimension_filter=FilterExpression(
                    filter=Filter(
                        field_name="eventName",
                        string_filter=Filter.StringFilter(
                            match_type=Filter.StringFilter.MatchType.EXACT,
                            value="(not set)",
                            case_sensitive=False,
                        ),
                    ),
                    not_expression=True,
                ) if False else None,  # No filter, get all events with conversions
                order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="conversions"), desc=True)],
                limit=50,
            )

            # Remove the None filter
            request.dimension_filter = None

            response = client.run_report(request)

            if not response.rows:
                return f"No conversion data for property {property_id}."

            rows = []
            for row in response.rows:
                conv_val = row.metric_values[0].value
                if conv_val and float(conv_val) > 0:
                    rows.append({
                        "event_name": row.dimension_values[0].value,
                        "channel": row.dimension_values[1].value,
                        "conversions": conv_val,
                        "revenue": row.metric_values[1].value,
                    })

            return json.dumps({
                "property_id": property_id,
                "date_range": {"start": start_date, "end": end_date, "days": days},
                "row_count": len(rows),
                "rows": rows,
            })
        except Exception as e:
            return f"Error: {str(e)}"

    @mcp.tool()
    def ga4_landing_pages(
        property_id: str, days: int = 28, row_limit: int = 20
    ) -> str:
        """Get top landing pages with sessions, engagement, and conversions.

        Args:
            property_id: GA4 property ID (digits only, from ga4_list_properties)
            days: Number of days to look back (default: 28)
            row_limit: Number of rows (default: 20)
        """
        try:
            from google.analytics.data_v1beta.types import (
                RunReportRequest,
                DateRange,
                Dimension,
                Metric,
                OrderBy,
            )

            client = get_ga4_data_client()
            end_date = datetime.now().date().strftime("%Y-%m-%d")
            start_date = (datetime.now().date() - timedelta(days=days)).strftime("%Y-%m-%d")

            request = RunReportRequest(
                property=f"properties/{property_id}",
                dimensions=[
                    Dimension(name="landingPage"),
                ],
                metrics=[
                    Metric(name="sessions"),
                    Metric(name="totalUsers"),
                    Metric(name="engagementRate"),
                    Metric(name="averageSessionDuration"),
                    Metric(name="conversions"),
                    Metric(name="bounceRate"),
                ],
                date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
                order_bys=[OrderBy(metric=OrderBy.MetricOrderBy(metric_name="sessions"), desc=True)],
                limit=row_limit,
            )

            response = client.run_report(request)

            if not response.rows:
                return f"No landing page data for property {property_id}."

            rows = []
            for row in response.rows:
                rows.append({
                    "landing_page": row.dimension_values[0].value,
                    "sessions": row.metric_values[0].value,
                    "users": row.metric_values[1].value,
                    "engagement_rate": row.metric_values[2].value,
                    "avg_session_duration": row.metric_values[3].value,
                    "conversions": row.metric_values[4].value,
                    "bounce_rate": row.metric_values[5].value,
                })

            return json.dumps({
                "property_id": property_id,
                "date_range": {"start": start_date, "end": end_date, "days": days},
                "row_count": len(rows),
                "rows": rows,
            })
        except Exception as e:
            return f"Error: {str(e)}"
