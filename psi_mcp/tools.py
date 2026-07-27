"""Google PageSpeed Insights MCP tools.

Uses the PageSpeed Insights API v5 (REST, API key auth).
Call register(mcp) to add tools to an existing FastMCP instance.

Requires env var: GOOGLE_API_KEY
"""

import json
import logging
import os
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

PSI_ENDPOINT = "https://pagespeedonline.googleapis.com/pagespeedonline/v5/runPagespeed"


def _get_api_key():
    key = os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise EnvironmentError(
            "Missing GOOGLE_API_KEY in Replit Secrets. "
            "Get one at https://console.cloud.google.com/apis/credentials"
        )
    return key


def register(mcp):
    """Register PSI tools on the given FastMCP server instance."""

    @mcp.tool()
    def psi_analyze(
        url: str,
        strategy: str = "mobile",
        categories: str = "performance,seo,accessibility,best-practices",
    ) -> str:
        """Run a PageSpeed Insights analysis on a URL.

        Returns Lighthouse scores, Core Web Vitals (field data if available),
        and top optimization opportunities.

        Args:
            url: Full URL to analyze (e.g. https://example.com/)
            strategy: "mobile" or "desktop" (default: mobile)
            categories: Comma-separated categories to audit.
                Options: performance, seo, accessibility, best-practices
        """
        try:
            api_key = _get_api_key()

            params = {
                "url": url,
                "strategy": strategy.lower(),
                "key": api_key,
            }
            for cat in categories.split(","):
                params.setdefault("category", [])
                # httpx handles list params correctly
            
            # Build category params manually for the API
            category_list = [c.strip() for c in categories.split(",")]
            query_parts = [
                f"url={url}",
                f"strategy={strategy.lower()}",
                f"key={api_key}",
            ]
            for cat in category_list:
                query_parts.append(f"category={cat.strip().upper()}" if cat.strip() == "seo" else f"category={cat.strip()}")
            
            # Use proper params with httpx
            request_params = [
                ("url", url),
                ("strategy", strategy.lower()),
                ("key", api_key),
            ]
            for cat in category_list:
                request_params.append(("category", cat.strip()))

            with httpx.Client(timeout=60) as client:
                response = client.get(PSI_ENDPOINT, params=request_params)
                response.raise_for_status()
                data = response.json()

            # Extract scores
            lighthouse = data.get("lighthouseResult", {})
            categories_result = lighthouse.get("categories", {})

            scores = {}
            for cat_key, cat_data in categories_result.items():
                scores[cat_key] = {
                    "score": round((cat_data.get("score", 0) or 0) * 100),
                    "title": cat_data.get("title", cat_key),
                }

            # Extract Core Web Vitals from field data (CrUX)
            field_data = data.get("loadingExperience", {})
            crux_metrics = {}
            if field_data.get("metrics"):
                for metric_key, metric_data in field_data["metrics"].items():
                    crux_metrics[metric_key] = {
                        "percentile": metric_data.get("percentile"),
                        "category": metric_data.get("category"),
                    }

            overall_crux_category = field_data.get("overall_category", "NOT_AVAILABLE")

            # Extract top opportunities (performance improvements)
            audits = lighthouse.get("audits", {})
            opportunities = []
            for audit_key, audit_data in audits.items():
                if (
                    audit_data.get("score") is not None
                    and audit_data["score"] < 1
                    and audit_data.get("details", {}).get("type") == "opportunity"
                ):
                    savings = audit_data.get("details", {}).get("overallSavingsMs")
                    opportunities.append({
                        "audit": audit_key,
                        "title": audit_data.get("title", audit_key),
                        "score": round((audit_data["score"] or 0) * 100),
                        "savings_ms": savings,
                        "description": audit_data.get("displayValue", ""),
                    })

            # Sort by savings (biggest first)
            opportunities.sort(key=lambda x: x.get("savings_ms") or 0, reverse=True)

            # Extract key lab metrics
            lab_metrics = {}
            key_audits = [
                "first-contentful-paint",
                "largest-contentful-paint",
                "total-blocking-time",
                "cumulative-layout-shift",
                "speed-index",
                "interactive",
            ]
            for key in key_audits:
                if key in audits:
                    lab_metrics[key] = {
                        "value": audits[key].get("displayValue", ""),
                        "score": round((audits[key].get("score", 0) or 0) * 100),
                    }

            return json.dumps({
                "url": url,
                "strategy": strategy,
                "scores": scores,
                "field_data": {
                    "overall_category": overall_crux_category,
                    "metrics": crux_metrics,
                } if crux_metrics else None,
                "lab_metrics": lab_metrics,
                "opportunities": opportunities[:10],
            })
        except httpx.HTTPStatusError as e:
            return f"PSI API error ({e.response.status_code}): {e.response.text[:200]}"
        except Exception as e:
            return f"Error running PageSpeed analysis: {str(e)}"
