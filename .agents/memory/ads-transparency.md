---
name: Google Ads Transparency Center
description: Public competitor-ad research behavior and limits
---

- The public Center accepts a website through the `domain` query parameter, while advertiser names must be entered through the search UI and selected from a result.
- Public results expose advertiser verification, creative preview links and image URLs, but not spend, clicks, targeting performance or conversions.
- Results vary by region and current Google availability; do not present the public ad count as a complete account inventory.

**Why:** The service has no equivalent read-only Google Ads account endpoint for competitor ads, so the safe integration uses the public UI and must state its limits.

**How to apply:** Keep future Transparency features read-only, bounded by a maximum number of creatives, and link back to the public Google result instead of copying or following ad destinations.
