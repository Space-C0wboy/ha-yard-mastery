"""Anonymise raw Yard Mastery probe responses into test fixtures.

Usage: python -I scripts/make_fixtures.py <research_dir> <out_dir>

Reads <research_dir>/probe/out/*.json (each {status, body}), replaces personal
values with fixed fakes, and refuses to write if any real value survives.
Free-text fields (body/notes/description) are not scrubbed: eyeball them when regenerating.
"""
import json
import pathlib
import sys

# raw probe file -> fixture name
FILES = {
    "user_status": "user_status",
    "yard": "yard",
    "lawn_segments": "lawn_segments",
    "home_soil_temp": "soil_temp",
    "home_bundle": "home_bundle",
    "bundle_cur": "bundle_current",
    "bundle_next": "bundle_next",
    "cal2026": "calendar_2026",
    "cal2025": "calendar_empty",
    "calendar_filter_options": "filter_options",
    "journal_day": "journal_2026_10_17",
    "soil_test_dashboard": "soil_dashboard",
    "soil_report": "soil_report",
    "stilp_products": "stilp_products",
    "lawn_program_status": "lawn_program_status",
    "version_validate": "version_validate",
    "lawn_types": "lawn_types",
    "lawn_program_values": "program_values",
}

FAKE = {
    "email": "lawn@example.com",
    "firstName": "Test",
    "lastName": "User",
    "grower_name": "Test User",
    "submitter_name": "Test User",
    "phone": "0000000000",
    "street1": "1 Test St",
    "street2": "",
    "city": "Testville",
    "postalCode": "00000",
    "guid": "user-guid-1",
    "userGuid": "user-guid-1",
    "barcode": "YM00001",
    "trackingNumber": "TRACK0001",
    "trackingUrl": "https://example.invalid/track",
    "hostedUrl": "https://example.invalid/photo.jpg",
    "reference": "photo.jpg",
    "lat": 0.0,
    "lng": 0.0,
    "stateAbbreviation": "XX",
    # soil lab identifiers (a string value gets the fake as a string)
    "lab_number": "LAB0001",
    "grower_id": 0,
    "submitter_id": 0,
    "farm_id": 0,
    "field_id": 0,
    "sample_set_id": 0,
    "sample_set_system_id": 0,
}
STR_KEYS_NULL_OK = {"street2"}
MIN_LEN = 3  # real values shorter than this are not worth a substring check


class Scrubber:
    def __init__(self):
        self.real: set[str] = set()

    def note(self, value) -> None:
        text = str(value).strip()
        if len(text) >= MIN_LEN:
            self.real.add(text)

    def scrub(self, obj):
        if isinstance(obj, dict):
            out = {}
            for key, value in obj.items():
                if key == "latLngPoints" and isinstance(value, str):
                    for pt in json.loads(value or "[]"):
                        self.note(pt.get("lat"))
                        self.note(pt.get("lng"))
                    out[key] = "[]"
                elif key in FAKE and not isinstance(value, (dict, list)):
                    if value is None and key in STR_KEYS_NULL_OK:
                        out[key] = None
                    elif value is not None:
                        self.note(value)
                        fake = FAKE[key]
                        out[key] = str(fake) if isinstance(value, str) and not isinstance(fake, str) else fake
                    else:
                        out[key] = None
                else:
                    out[key] = self.scrub(value)
            return out
        if isinstance(obj, list):
            return [self.scrub(v) for v in obj]
        return obj


def main(research: pathlib.Path, out_dir: pathlib.Path) -> int:
    src = research / "probe" / "out"
    scrubber = Scrubber()
    outputs: dict[str, object] = {
        "login": {"accessToken": "tok-fixture", "user": {"email": FAKE["email"], "guid": FAKE["guid"]}}
    }
    # The login user block holds the account's real identity: collect it for the check.
    login = json.loads((src / "login_user.json").read_text(encoding="utf-8"))["body"]
    scrubber.scrub(login)
    for raw_name, fixture_name in FILES.items():
        raw = json.loads((src / f"{raw_name}.json").read_text(encoding="utf-8"))
        outputs[fixture_name] = scrubber.scrub(raw["body"])

    # Real address pieces (street name part, zip) are collected via the keys above.
    rendered = {name: json.dumps(body, indent=2, sort_keys=True) for name, body in outputs.items()}
    leaks = []
    for real in scrubber.real:
        for name, text in rendered.items():
            if real.lower() in text.lower():
                leaks.append((name, "<redacted real value>"))
    if leaks:
        print(f"PII check FAILED: {len(leaks)} leak(s) in {sorted({n for n, _ in leaks})}", file=sys.stderr)
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    for name, text in rendered.items():
        (out_dir / f"{name}.json").write_text(text + "\n", encoding="utf-8")
    print(f"{len(rendered)} files written")
    print("PII check passed")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: make_fixtures.py <research_dir> <out_dir>")
    sys.exit(main(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])))
