---
name: Shared Google user login
description: How the GSC/GA4/Merchant/GTM OAuth token is created and quirks of the Merchant API.
---

- The GSC OAuth client is a Web client whose only working redirect URI is the OAuth Playground; new scopes require a re-login there (localhost/replit domains give redirect_uri_mismatch).
  **Why:** discovered when trying a local OAuth flow. **How to apply:** for any new Google scope (e.g. Business Profile), send the user through the Playground and store the token as GOOGLE_REFRESH_TOKEN with ALL scopes.
- Merchant API needed a one-time developer registration POST (approved by the user, 2026-11-10); unregistered projects return 401 "not registered with the merchant account".
- Merchant aggregateProductStatuses returns groups without reportingContext, and the same products are counted per destination — never sum counts across groups.
