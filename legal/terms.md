# Launchproof Terms of Service

_Template. Last updated: [DATE]. Have a lawyer review before charging real customers. Not legal advice._

These terms are an agreement between you and [LEGAL ENTITY NAME] ("Launchproof", "we"). By creating an account or starting a test you agree to them.

## 1. Who can use Launchproof
You must be at least 18 and able to form a binding contract. If you use Launchproof for a company, you confirm you can bind that company.

## 2. You may only test sites you control (read this)
Launchproof sends automated traffic to websites, including high-volume load tests and test purchases. Sending that traffic to a site without permission can be illegal (for example under the U.S. Computer Fraud and Abuse Act and similar laws elsewhere).

Each time you start a test you represent and warrant that:
- you own the target site, or you have written permission from its owner to run the tests you select;
- you have any permission your hosting, CDN, database and payment providers require for load testing (many allow ordinary load tests; some, such as DDoS-style simulations, require prior approval);
- you completed ownership verification yourself and did not place our token on a site you do not control.

We verify ownership with a DNS record, file or meta tag before running load or payment tests. Verification does not shift responsibility for authorization to us.

## 3. What a test does, and the risks you accept
- **Load tests** can slow down or take down the target site while they run, and can trigger rate limits, WAF blocks or alerts. Tests stop at the first break point, and are capped (currently 1,000 virtual users, 5 minutes and 200 requests per second per region), but we do not guarantee your site stays available.
- **Costs are yours.** Traffic we generate may be billed to you by your providers (bandwidth, serverless invocations, database usage, logging). We show an estimate before each run; it is not a guarantee.
- **Payment tests** run in Stripe test mode unless you explicitly enable a live test. A live test makes one real purchase with a card we control and then requests a refund; payment processor fees are not refundable and are charged to you.
- Our test accounts use synthetic identities. We do not enter real personal data into your forms.

## 4. Acceptable use
You agree to our [Acceptable Use Policy](acceptable-use.md). We may refuse, stop or cap any test at any time, and suspend accounts that break these terms.

## 5. Payments and refunds
Prices are shown before purchase, in USD. Payments are processed by Stripe. If a paid test fails to complete for reasons on our side, contact us within 14 days for a full refund. Subscriptions renew until cancelled; cancel any time from the billing portal and keep access until the end of the period.

## 6. Reports and your content
Reports, screenshots and fix prompts belong to you. Reports are private unless you share them. You grant us the rights needed to run the tests and store results. Screenshots may capture content shown on your site; you are responsible for having the right to let us process it. We delete run data after 30 days unless you save the report.

## 7. No warranty
Launchproof is provided "as is". Tests find some problems, not all problems. A good score is not a guarantee that your site, payments or infrastructure will work under real traffic.

## 8. Limitation of liability
To the maximum extent permitted by law, we are not liable for indirect, incidental, special, consequential or punitive damages, or for lost profits, revenue or data, including downtime or provider charges caused by tests you started. Our total liability for any claim is limited to the amount you paid us in the 3 months before the claim.

## 9. Indemnity
You will defend and indemnify Launchproof against claims arising from tests you started against sites you were not authorized to test, or from your breach of these terms.

## 10. Changes, law and contact
We may update these terms and will notify you of material changes by email or in the app. These terms are governed by the laws of [STATE], USA, and disputes go to the courts of [COUNTY, STATE]. Contact: [EMAIL], [POSTAL ADDRESS].
