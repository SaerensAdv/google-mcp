---
name: GBP API access status
description: Google-side prerequisites for the gbp_ tools and what they looked like when the tools were built
---

As of 2026-11-10 (GCP project 294994430690, shared GSC login with business.manage):
- Account Management, Business Information and Google My Business (v4, reviews) APIs: SERVICE_DISABLED.
- Business Profile Performance API: enabled but 429 with quota_limit_value "0" = GBP API access request not approved yet.
- Only the user can fix this (enable APIs in Cloud Console + submit Google's GBP API access request form).

**Why:** the gbp_ tools were built and unit-tested against captured real error payloads, but never against a real profile.
**How to apply:** when the user says access is approved, call each gbp_ tool on the dev server against a real profile and check the response shapes (especially performance/keywords parsing) before suggesting publish. Update or delete this note afterwards.
