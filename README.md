# Yard Mastery for Home Assistant

Brings your [Yard Mastery](https://yardmastery.com) lawn-care program into Home Assistant: the treatment windows,
your journal and reminders on a calendar, sensors for the current and next window, and actions to log what you
did, so you do not have to open the app.

**Unofficial.** This project is not affiliated with, endorsed by, or supported by Yard Mastery. It uses the same
private web API the phone app uses, which is undocumented and may change or stop working without notice. Use it at
your own risk and be kind to their servers (polling is deliberately slow).

Your Yard Mastery email and password are typed by you into Home Assistant's setup dialog and stored in Home
Assistant like any other cloud integration. Nothing personal is kept in this repository.

## Install

1. In HACS, add this repository as a custom repository (category: Integration):
   `https://github.com/Space-C0wboy/ha-yard-mastery`
2. Install **Yard Mastery** and restart Home Assistant.
3. Settings > Devices & services > Add integration > **Yard Mastery**, and sign in with your email and password.

The first setup imports your history: every past year of your calendar, each journal entry, and the details of
every window. This can take a while on an account with years of data; later polls are small.

## Options

Settings > Devices & services > Yard Mastery > Configure.

| Option | Default | What it does |
|---|---|---|
| Allow changes to Yard Mastery | **off** | Lets the actions below, and reminder edits in the calendar, change your Yard Mastery data. Off means Home Assistant only reads. |
| App version to report | `4.00.05` | The app version sent with every request. See "If Yard Mastery retires the app version". |
| Update interval (minutes) | 30 | 15 to 1440. Slower data (soil tests, lawn segments, next year's calendar) refreshes every 6 hours; reference lists daily. A normal poll makes no more than about 10 requests. |

Turning "Allow changes" on or off reloads the integration.

## Entities

One device, **Yard Mastery**.

Sensors: soil temperature, program (with lawn type, square feet and program details as attributes), current
window, current window progress, next window, next window start, days until next window, last application, last
journal entry, applications this year, nitrogen this year (pounds of N per 1,000 sq ft, worked out from what you
logged; liquid products are listed under `not_counted` instead of guessed), and soil test.

Diagnostic sensors: Dug AI turns, last sync, one sensor per measured value in your latest soil-test report (the
common ones are enabled, the rest are disabled by default), and one per lawn segment (square feet).

Binary sensors: **treatment due** (on while the current window still has items not done) and, as a diagnostic,
**app version valid**.

Large or private data (lawn polygons, the whole calendar, image carousels, the full lab report) is kept in
Home Assistant's storage only, not in entity attributes. The same storage keeps an append-only change log ("when
was that marked done?").

## Calendar

`calendar.yard_mastery` shows three kinds of all-day events: program windows (with each product's rate, the amount
for your lawn, and its status), journal entries (what was applied or done), and reminders.

With "Allow changes" on, you can create, edit and delete **reminders** from the calendar. These are reminders
only: editing program windows or journal entries is refused in the calendar (use the actions below). Deleting a
reminder dismisses it in Yard Mastery, which cannot be undone.

## Actions

All actions accept an optional `config_entry_id` (only needed with several accounts) and return what they changed.
With "Allow changes" off, every action except `refresh` and `import_history` fails with "Changes to Yard Mastery are
turned off in the integration's options". Every change is written to the logbook and the change log, then followed
by a refresh.

- `yard_mastery.log_application` -- create or update the journal entry for a date (default today), merging with an
  entry already on that day (existing applications, activities and photos are kept).
  - `products`: a list of `{product, amount}`. `amount` is the **total amount applied over your whole lawn**, the
    same number the app records. Leave it out and the recommended total is used: rate x lawn size / coverage.
    The lawn size is the one the app measured for your yard.
  - Without `products`, the items of the `window` (default the current one) that are not done yet are logged at
    their recommended amount and marked done. **Experimental:** this program-linked default has not yet been
    verified on a real application and may change in a later release. Name the products until then.
  - Optional: `areas` (lawn segment names), `notes`, `soil_temp` (defaults to today's ZIP soil temperature).
- `yard_mastery.set_item_status` -- mark program items `done`, `skipped` or `not_done`, by `items` (ids) or by
  `products` in a `window`.
- `yard_mastery.log_activity` -- add mow, edge, aerate, dethatch, blow or trim to a date's entry, with optional
  `areas` and `notes`.
- `yard_mastery.add_photo` -- attach a photo to a date's entry, from a camera or image entity (`entity_id`) or a
  file. A `file` must be a path under `/config` or `/media` that Home Assistant allows, meaning its folder is in
  `allowlist_external_dirs` (media folders are allowed by default).
- `yard_mastery.delete_journal_entry` -- delete an entry. You must give a `date` or an `entry_id`; there is no
  default.
- `yard_mastery.create_reminder`, `update_reminder`, `dismiss_reminder` -- the same reminder operations as the
  calendar. Reminder ids appear in the calendar events.
- `yard_mastery.refresh` and `yard_mastery.import_history` -- always available (they only read).

Examples:

```yaml
# Log what you spread today, with the amounts you actually put down
action: yard_mastery.log_application
data:
  products:
    - product: Flagship
      amount: 2.7
  areas: [Front Yard]
  notes: Watered in afterwards.
```

```yaml
# Log everything still open in the current window (experimental)
action: yard_mastery.log_application
```

```yaml
action: yard_mastery.log_activity
data:
  activities: [mow, edge]
```

```yaml
action: yard_mastery.add_photo
data:
  entity_id: camera.front_yard
```

```yaml
action: yard_mastery.set_item_status
data:
  status: skipped
  products: [Prodiamine]
```

## If Yard Mastery retires the app version

Yard Mastery can stop accepting an old app version. When it does, the **app version valid** sensor turns off and a
repair issue appears. Put a newer version in the integration's options ("App version to report"). How to find the
current version is in [docs/api.md](docs/api.md#how-to-re-read-a-new-app-version).

## Diagnostics

Download diagnostics from the integration page to attach to an issue. They include the stored data and change log
with personal information redacted (email, password, token, names, address, coordinates and lawn polygons, user id,
soil-test barcode, tracking details, and photo links). Please still glance through before posting.

## Troubleshooting

- Reauth prompt: your password changed, or sign-in failed repeatedly. Enter the current password. Home Assistant
  never retries in a loop (at most one re-login per 15 minutes).
- Data looks stale: call `yard_mastery.refresh`, or check the **last sync** sensor.

## Credits and license

Built by [Space-C0wboy](https://github.com/Space-C0wboy). Yard Mastery and the Yard Mastery app belong to their
owners. MIT licensed, see [LICENSE](LICENSE). API notes: [docs/api.md](docs/api.md).

## Development

Run tests: `bash scripts/test.sh`. Regenerate fixtures: `python -I scripts/make_fixtures.py <research_dir> tests/fixtures`.
