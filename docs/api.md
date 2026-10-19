# Yard Mastery API notes

Yard Mastery publishes no API. These notes come from reading the Android app's web bundle (a Capacitor/Ionic app,
so the client is readable JavaScript) and from read-only calls made with the author's own account. They are
unofficial, can change at any time, and contain no personal data.

Legend: **verified** = seen in a live response; **from code** = read in the app bundle, not called against a real
account.

Base URL: `https://prod-api.yardmastery.com`

## Request rules

Every request (verified):

- `Authorization: <accessToken>` -- the raw token, **no** `Bearer` prefix.
- `appVersion: <version>` header **and** `appVersion=<version>` query parameter.
- `Accept: application/json`; JSON bodies also send `Content-Type: application/json`.
- The integration also sends `Origin: https://localhost` and an Android WebView `User-Agent`, as the app does. It is
  not known whether the server requires them; they are cheap insurance.

## Authentication

- `POST /login` with `{email, password}` returns `{accessToken, user{email, guid, roles, ...}}` (verified).
- There is no refresh endpoint. The app treats a session as expired 28 days after login (from code). The
  integration re-logs in when its token is 27 days old, or when any call returns 401/403, then retries once.
- Concurrent sessions are fine (verified): logging in from Home Assistant does not sign the phone app out.
- Lockout protection: at most one re-login per 15 minutes, and three consecutive re-logins that still end in 401
  stop polling and start the Home Assistant reauth flow.
- `GET /v1/system/version/validate` returns `{isValid, version}` (verified). Yard Mastery can retire app versions;
  see "How to re-read a new app version" below.

## Read endpoints

All verified unless marked.

| Endpoint | Returns |
|---|---|
| `GET /v1/user/status` | app version, program type (e.g. `STILP`), user guid/type, Dug AI turns used/limit/reset |
| `GET /v2/yard` | address, current bundle summary, lawn program tests, lawnType, productTypePreference, program {barcode, programType}, recalculatingLawnProgram, selectedTest, segments[], squareFt, squareFtMeasured, lawnId, willOverseed, blackoutOptIn/Eligible, hasBuyAllPurchase, shopifySoilTestProductId |
| `GET /v1/lawn-segment` | segments: id, name, squareFeet, color, latLngPoints (a JSON string polygon), segmentType, yardId |
| `GET /v4/home/soil-temp` | `{soilTemp, soilTempUnit, ambientTempIcon, zipCodeStatus}` (ZIP-code 24 h average) |
| `GET /v4/home/bundle` | current window summary: bundleDate, bundleId, bundleName, nextBundleDate, nextBundleId, programName, programStatus, steps, stepsCompleted, stepsSkipped |
| `GET /v1/dlux/bundle/{id}/details` | full window: id, name, description, startDate, endDate, blackoutTreatment, imageUrl, imageCarousel, youtubeId, bundleItems[] {id, status, completed, journalEntryDate, displayOrder, description, product {id, name, applicationRate, coverageSqFt, measure, materialType, nitrogen, phosphorus, potassium, shopifyId, imageUrl, productType, taxonomies}}. Works for STILP programs despite the `dlux` in the path |
| `GET /v1/calendar/search?viewType=YEAR&includeBundles=true&includeJournalEntries=true&includeReminders=true&startDate=YYYY-01-01&endDate=YYYY-12-31` | `{links{nextPage, previousPage}, years[{year, blackout, days[{date, bundle?, journalEntry?, reminders[]}]}]}`. Optional `activityIds=` (repeatable) and `searchString=` |
| `GET /v1/calendar/filter-options` | activities (Mow 1, Dethatch 2, Edge 3, Aerate 4, Blow 5, Trim 6) with counts |
| `GET /v3/journal/entry?date=YYYY-MM-DD` | one day's entry: id, entryDate, name, body, activities, productApplications[{appliedRate, bundleItemIds, product}], lawnSegments, lawnSqFt, tempValue, tempUnit, attachments[{id, hostedUrl, reference}], bundleDetails |
| `GET /v1/journal/entry/activity` | the activity list (from code) |
| `GET /v4/home/soil-test`, `GET /v1/soil-test/dashboard` | soil tests (the dashboard has completed/inProgress/archived tests with barcode, dates, status, daysOld, tracking, tags; it also carries the user's name and address) |
| `GET /v1/soil-test/report?barcode=X` | lab report in MySoil format: report{results{ppm, ...}, samples, lab_date, ...}, recommendedProducts, tags |
| `GET /v1/soil-test/stilp/products/{barcode}` | STILP product recommendations for that test |
| `GET /v1/lawn-program/status` | completeDate, isProcessing, hasAddress, hasValidZip, queueDate |
| `GET /v1/lawn-types`, `GET /v1/settings/lawn-program/values`, `GET /v1/state` | reference lists (program types DLUX / LEGACY / NATURALS / STILP, product-type preferences) |
| `GET /v2/product?type=&term=`, `GET /v1/product/{id}/application/detail` | product search / detail (from code); used to log a product that is not in the current window |

### The YEAR gotcha

`viewType` must be the uppercase `YEAR`. Lowercase `year` returns 400. Dates are `YYYY-MM-DD`. A window that
crosses New Year appears in both years' responses, so de-duplicate windows by id.

### Lawn size and applied amounts

The app's lawn size is `yard.squareFtMeasured` when it is greater than 0, otherwise `yard.squareFt`. A journal
entry stores that size as `lawnSqFt`, and each `productApplications[].appliedRate` is the **total amount applied
over `lawnSqFt`** -- not a per-1,000 sq ft rate. The app fills it with `rate x lawnSqFt / coverageSqFt`, where
`rate` is the product's `applicationRate` per `coverageSqFt` (normally 1,000 sq ft). Example: a product with rate
3.0 per 1,000 sq ft on a 900 sq ft measured lawn gives `appliedRate` 2.7.

### Journal entries with a null id

A day with no saved entry still returns a journal-entry-shaped object with `id: null` (the app's unsaved template,
with defaults such as `lawnSegments: null`, an empty `activities` list, and no `productApplications`). It is not a
real entry; the integration ignores it for the calendar and sensors. Other fields may also be `null` or missing on
real entries (activity-only days have no product applications), so parse defensively.

## Write endpoints

All **from code, not yet called** against a real account. Treat the shapes as unconfirmed until they have been
exercised live.

| Endpoint | Body |
|---|---|
| `PUT /v1/dlux/user-bundle-item/change-status` | `{bundleItemIds: [int], status}`, status one of `CREATED`, `COMPLETED`, `SKIPPED` |
| `POST /v2/journal/entry` | the full journal-entry object as `GET /v3/journal/entry` returns it, with `entryDate` as `YYYY-MM-DD`, `bundleDetails: null` and `product.measure: null` on each product application. The app sends the existing `id` when editing. Linking an application to `bundleItemIds` is what marks a program item done in the app |
| `DELETE /v1/journal/entry/{id}` | none |
| `POST /v1/journal/entry/image?date=YYYY-MM-DD` | multipart `file` (jpg); returns the entry's attachments |
| `DELETE /v1/journal/entry/image/{id}` | path from code; method to confirm (not used by the integration) |
| `POST /v1/reminder` | `{createDate, detail, dueDate}` -- `detail` is HTML, `dueDate` is `YYYY-MM-DD` |
| `PUT /v1/reminder` | the reminder object with its `id`, changed `detail` / `dueDate` |
| `PUT /v1/reminder/{id}/dismiss` | none (the app warns this cannot be undone) |

The integration never calls admin endpoints (`/v2/admin/*`, `/v1/property`, banners), the cart, "Buy All",
Shopify, or account/settings writes.

## How to re-read a new app version

Yard Mastery rejects app versions it has retired. When `GET /v1/system/version/validate` reports
`isValid: false`, Home Assistant shows a repair issue. To find the current version and check for API changes:

1. Install the current Yard Mastery app from Google Play on an Android phone with USB debugging on.
2. Find the APK: `adb shell pm path com.yardmastery.lawnjournal` (it prints one or more `package:` paths; the
   base one is `base.apk`).
3. Pull it: `adb pull <path-from-step-2> base.apk`.
4. Unzip it: `unzip base.apk -d app` (an APK is a zip file).
5. Search the web bundle for the API address and version:
   `grep -o "ymApiUrl[^,;]*" app/assets/public/main.*.js` and
   `grep -o "appVersion[^,;]*" app/assets/public/main.*.js`.
6. Put the version string in the integration's options ("App version to report"). If requests still fail, grep
   the same bundle for the failing path to see whether its shape changed.
