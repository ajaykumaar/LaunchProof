# Launchproof Acceptable Use Policy

_Template. Last updated: [DATE]._

You may not use Launchproof to:

1. Test any website, API or service you do not own or are not authorized in writing to test.
2. Place a verification token on a site you do not control, or trick someone into placing one.
3. Get around our caps, ownership checks or rate limits (multiple accounts, split runs, scripted re-runs to add up load).
4. Run tests designed to disrupt a service (DDoS simulation) instead of measuring it. We only run capped ramp tests that stop at the first break point.
5. Submit real card numbers, real people's personal data or stolen credentials through our payment or signup tests.
6. Test payment endpoints with repeated live card attempts (this looks like card testing fraud to processors). Live payment tests go only through our one-attempt, auto-refund flow.
7. Use findings to attack, extort or embarrass the site owner, or publish another party's report without their consent.
8. Test sites that provide emergency, medical, utility or other critical services, even with authorization, without contacting us first.

We label all our traffic (User-Agent `LaunchproofLoadTest/1.0` and header `X-Launchproof-Run`). If you believe Launchproof sent traffic to your site without permission, email [ABUSE EMAIL] with the run id from the header. We respond within 1 business day and block the target.

Breaking this policy can lead to immediate suspension without refund and, where required, reports to the affected party or authorities.
