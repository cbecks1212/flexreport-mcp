# FlexReport Finance — Privacy Policy

**Effective date:** 2026-08-03

This policy describes how FlexReport Finance ("FlexReport", "we") handles your
information when you use the FlexReport MCP connector
(`https://mcp.flexreportfinapi.com/mcp`), the FlexReport API, and the FlexReport
web application.

## What we collect

- **Account information.** Your email address and a hashed password, collected
  when you register. If you select a paid plan, payments are processed by Stripe; we never receive or store your card details.
- **Queries and requests.** The contents of the requests you (or an AI agent
  acting on your behalf) send to our tools — for example tickers, screening
  criteria, and natural-language research questions. We use these to serve the
  request and enforce plan quotas and rate limits. These are logged in AWS
  CloudWatch and purged after three days. Ad-hoc queries are not stored in a
  database; queries you save as scheduled tasks are retained until you delete
  the task.
- **Generated artifacts.** PDF research reports generated for you are stored
  in our S3 bucket under a prefix dedicated to your account and expire after
  12 hours.
- **Usage and log data.** Standard service logs (timestamps, endpoints called,
  response codes, IP addresses) used for security, rate limiting, and
  operations. These have a 3-day retention period.

## What the MCP connector itself does NOT store

The MCP connector is a stateless proxy. It holds no user database, stores no
credentials or tokens at rest, and forwards your OAuth bearer token to the
FlexReport backend on each call, where authorization, plan, and quota are
enforced.

## How we use your information

- To provide the service: answer queries, generate reports, run scheduled
  tasks you create, and deliver results to your dashboard or email.
- To operate billing through Stripe.
- To secure the service: authentication, abuse prevention, rate limiting.
- To plan research queries: bespoke research requests are routed to OpenAI,
  which converts your plain-English query into SQL queries against the
  appropriate tables and materialized views.

## Third parties we share data with

| Provider | Purpose |
|---|---|
| Amazon Web Services | Hosting, storage of generated reports and market data |
| Stripe | Payment processing and billing portal |
| OpenAI | Query planning and report generation — converting plain-English research requests into SQL queries against FlexReport's database |
| Mailgun | Emailed reports and account confirmation |
| Financial Modeling Prep, FRED, SEC EDGAR, investor-relations filings | Upstream market-data sources. Your data and requests are never shared with these providers — you access data that has been cleaned, transformed, and curated inside FlexReport's database. |

Queries sent to OpenAI are processed through its API, which does not use the
data to train OpenAI's models. We do not sell your personal information.

## Data retention

- Account data: retained while your account is active. All data is removed
  immediately when the account is deleted.
- Generated reports and artifacts: presigned links expire after 12 hours, and
  the underlying objects are removed on the same schedule.
- Logs: three-day retention, after which they are deleted.

## Your choices and rights

- FlexReport is free by default — you will never be charged unless you opt
  into a paid plan.
- You can cancel your subscription at any time via the billing portal.
- You can request deletion of your account and associated data by contacting
  us at support@flexreportfinapi.com. We will respond within one business day.
- Depending on your jurisdiction (e.g. GDPR, CCPA), you may have additional
  rights to access, correct, export, or delete your data. Contact us to
  exercise them.

## Security

Transport is TLS-encrypted end to end. Authentication uses OAuth 2.0
(authorization code + PKCE); the MCP connector validates tokens against the
backend's public keys and never sees your password. By connecting to
FlexReport, you grant Claude or your own agents access to your FlexReport
account: they can query data, generate reports, and create or delete your
scheduled tasks. FlexReport runs entirely on our infrastructure and never
accesses your device, files, or network.

## Children

The service is not directed to individuals under 18, and we do not knowingly
collect their data.

## Changes to this policy

We will post any changes to this page and update the effective date. Material
changes will be announced by email.

## Contact

Please direct all questions and support to support@flexreportfinapi.com. We will respond within one business day.
