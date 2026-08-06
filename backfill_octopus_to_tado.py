#!/usr/bin/env python3
"""Backfill weekly Octopus gas consumption into tado Energy IQ.

Place beside sync_octopus_tado.py. Dry-run by default; add --apply to upload.
The script is resumable: matching existing dates are skipped and conflicting
existing dates stop an upload.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable

import requests
from requests.auth import HTTPBasicAuth

try:
    from sync_octopus_tado import tado_login
except ImportError as exc:
    raise SystemExit(
        "Put this file in the repository root beside sync_octopus_tado.py."
    ) from exc

API_ROOT = "https://api.octopus.energy/v1"


@dataclass(frozen=True)
class Week:
    start: datetime
    end: datetime
    consumption: Decimal


@dataclass(frozen=True)
class TadoReading:
    day: date
    reading: int


@dataclass(frozen=True)
class PlanRow:
    day: date
    exact: Decimal
    integer: int
    weekly_consumption: Decimal | None
    interval_start: datetime | None
    interval_end: datetime | None
    status: str
    note: str


def parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_day(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    raise TypeError(f"Unsupported date value: {value!r}")


def as_decimal(value: Any, label: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid {label}: {value!r}") from exc


def tado_integer(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def model_mapping(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    dump = getattr(item, "model_dump", None)
    if callable(dump):
        try:
            return dump(by_alias=True)
        except TypeError:
            return dump()
    old_dump = getattr(item, "dict", None)
    if callable(old_dump):
        return old_dump()
    return {
        name: getattr(item, name)
        for name in ("date", "reading_date", "readingDate", "reading", "value")
        if hasattr(item, name)
    }


def first(mapping: dict[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        if mapping.get(name) is not None:
            return mapping[name]
    return None


def get_tado_readings(tado: Any) -> list[TadoReading]:
    method = getattr(tado, "get_eiq_meter_readings", None)
    if not callable(method):
        method = getattr(tado, "getEIQMeterReadings", None)
    if not callable(method):
        raise RuntimeError("Installed PyTado has no Energy IQ reading method.")

    raw = method()
    items = raw.get("readings", []) if isinstance(raw, dict) else list(raw)
    readings: list[TadoReading] = []
    for item in items:
        mapping = model_mapping(item)
        raw_day = first(mapping, ("date", "readingDate", "reading_date"))
        raw_value = first(mapping, ("reading", "value"))
        if raw_day is None or raw_value is None:
            print(f"Warning: ignored unrecognised Tado reading: {mapping!r}")
            continue
        readings.append(
            TadoReading(parse_day(raw_day), tado_integer(as_decimal(raw_value, "reading")))
        )
    return sorted(readings, key=lambda row: row.day)


def fetch_weeks(
    api_key: str,
    mprn: str,
    serial: str,
    period_from: datetime,
    period_to: datetime,
) -> list[Week]:
    url = f"{API_ROOT}/gas-meter-points/{mprn}/meters/{serial}/consumption/"
    params: dict[str, Any] | None = {
        "period_from": period_from.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "period_to": period_to.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "group_by": "week",
        "order_by": "period",
        "page_size": 250,
    }
    session = requests.Session()
    session.auth = HTTPBasicAuth(api_key, "")
    session.headers["User-Agent"] = "octopus-to-tado-backfill/1.0"

    records: dict[tuple[datetime, datetime], Week] = {}
    while url:
        response = session.get(url, params=params, timeout=45)
        params = None
        if response.status_code != 200:
            raise RuntimeError(
                f"Octopus request failed: HTTP {response.status_code}: {response.text}"
            )
        payload = response.json()
        for item in payload.get("results", []):
            row = Week(
                start=parse_datetime(item["interval_start"]),
                end=parse_datetime(item["interval_end"]),
                consumption=as_decimal(item["consumption"], "consumption"),
            )
            if row.consumption < 0:
                raise RuntimeError(f"Negative Octopus consumption: {item!r}")
            records[(row.start, row.end)] = row
        url = payload.get("next")

    return sorted(records.values(), key=lambda row: (row.start, row.end))


def build_plan(
    weeks: list[Week],
    anchor_day: date,
    anchor_reading: Decimal,
    existing: list[TadoReading],
) -> tuple[list[PlanRow], Decimal]:
    weeks = [row for row in weeks if row.end.date() <= anchor_day]
    if not weeks:
        raise RuntimeError("No Octopus weeks end on or before the anchor date.")

    total = sum((row.consumption for row in weeks), Decimal("0"))
    starting = anchor_reading - total
    if starting < 0:
        raise RuntimeError(
            "Reconstructed starting reading is negative. Tado and Octopus may use "
            f"different units, or the anchor is too small. Anchor={anchor_reading}; "
            f"history={total}; start={starting}."
        )

    existing_by_day = {row.day: row.reading for row in existing}
    candidates: dict[date, tuple[Decimal, Decimal | None, datetime | None, datetime | None, str]] = {
        weeks[0].start.date(): (starting, None, weeks[0].start, None, "baseline")
    }
    running = starting
    for week in weeks:
        running += week.consumption
        candidates[week.end.date()] = (
            running,
            week.consumption,
            week.start,
            week.end,
            "weekly",
        )

    plan: list[PlanRow] = []
    for day, (exact, usage, start, end, source) in sorted(candidates.items()):
        integer = tado_integer(exact)
        old = existing_by_day.get(day)
        if old is None:
            status, note = "upload", source
        elif old == integer:
            status, note = "skip-existing", "Same date and value already exist"
        else:
            status, note = "conflict", f"Tado has {old}; reconstruction gives {integer}"
        plan.append(PlanRow(day, exact, integer, usage, start, end, status, note))
    return plan, total


def write_csv(
    path: Path,
    plan: list[PlanRow],
    anchor_day: date,
    anchor_reading: Decimal,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as output:
        fields = [
            "reading_date", "interval_start", "interval_end", "weekly_consumption",
            "exact_cumulative_reading", "tado_integer_reading", "status", "note",
            "anchor_date", "anchor_reading",
        ]
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for row in plan:
            writer.writerow({
                "reading_date": row.day.isoformat(),
                "interval_start": row.interval_start.isoformat() if row.interval_start else "",
                "interval_end": row.interval_end.isoformat() if row.interval_end else "",
                "weekly_consumption": str(row.weekly_consumption) if row.weekly_consumption is not None else "",
                "exact_cumulative_reading": str(row.exact),
                "tado_integer_reading": row.integer,
                "status": row.status,
                "note": row.note,
                "anchor_date": anchor_day.isoformat(),
                "anchor_reading": str(anchor_reading),
            })


def upload(tado: Any, plan: list[PlanRow], delay: float) -> None:
    conflicts = [row for row in plan if row.status == "conflict"]
    if conflicts:
        details = "\n".join(f"  {row.day}: {row.note}" for row in conflicts[:10])
        raise RuntimeError(f"Refusing to upload because of conflicts:\n{details}")

    rows = [row for row in plan if row.status == "upload"]
    if not rows:
        print("Nothing to upload.")
        return

    method = getattr(tado, "set_eiq_meter_readings", None)
    if not callable(method):
        method = getattr(tado, "setEIQMeterReadings", None)
    if not callable(method):
        raise RuntimeError("Installed PyTado has no Energy IQ write method.")

    print(f"Uploading {len(rows)} readings oldest first...")
    for index, row in enumerate(rows, 1):
        result = method(reading_date=row.day, reading=row.integer)
        print(f"[{index}/{len(rows)}] {row.day}: {row.integer} -> {result}")
        if index < len(rows) and delay:
            time.sleep(delay)


def required(parser: argparse.ArgumentParser, value: str | None, label: str) -> str:
    if not value or not value.strip():
        parser.error(f"{label} is required as an argument or environment variable")
    return value.strip()


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tado-email", default=os.getenv("TADO_EMAIL"))
    parser.add_argument("--tado-password", default=os.getenv("TADO_PASSWORD"))
    parser.add_argument("--mprn", default=os.getenv("OCTOPUS_MPRN"))
    parser.add_argument("--gas-serial-number", default=os.getenv("OCTOPUS_GAS_SERIAL"))
    parser.add_argument("--octopus-api-key", default=os.getenv("OCTOPUS_API_KEY"))

    period = parser.add_mutually_exclusive_group()
    period.add_argument("--years", type=int, default=3)
    period.add_argument("--start-date", type=date.fromisoformat)

    parser.add_argument("--anchor-date", type=date.fromisoformat)
    parser.add_argument("--anchor-reading", type=Decimal)
    parser.add_argument("--preview-file", type=Path, default=Path("tado-backfill-preview.csv"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--delay-seconds", type=float, default=1.0)
    args = parser.parse_args()

    args.tado_email = required(parser, args.tado_email, "Tado email")
    args.tado_password = required(parser, args.tado_password, "Tado password")
    args.mprn = required(parser, args.mprn, "Octopus MPRN")
    args.gas_serial_number = required(parser, args.gas_serial_number, "Gas serial number")
    args.octopus_api_key = required(parser, args.octopus_api_key, "Octopus API key")

    if args.years is not None and args.years <= 0:
        parser.error("--years must be positive")
    if args.delay_seconds < 0:
        parser.error("--delay-seconds cannot be negative")
    if (args.anchor_date is None) != (args.anchor_reading is None):
        parser.error("Supply --anchor-date and --anchor-reading together, or neither")
    return args


def main() -> int:
    args = arguments()
    print("Authenticating with Tado...")
    tado = tado_login(args.tado_email, args.tado_password)
    existing = get_tado_readings(tado)
    print(f"Tado contains {len(existing)} meter reading(s).")

    if args.anchor_date is not None:
        anchor_day = args.anchor_date
        anchor_reading = args.anchor_reading
    else:
        if not existing:
            raise RuntimeError(
                "Tado has no reading to use as an anchor. Add a current reading first, "
                "or supply --anchor-date and --anchor-reading."
            )
        latest = max(existing, key=lambda row: row.day)
        anchor_day = latest.day
        anchor_reading = Decimal(latest.reading)
    print(f"Anchor: {anchor_reading} on {anchor_day}")

    start_day = args.start_date or (
        anchor_day - timedelta(days=round(args.years * 365.2425))
    )
    period_from = datetime.combine(start_day, dt_time.min, tzinfo=timezone.utc)
    period_to = datetime.combine(anchor_day + timedelta(days=1), dt_time.min, tzinfo=timezone.utc)

    print(f"Fetching weekly Octopus data from {period_from} to {period_to}...")
    weeks = fetch_weeks(
        args.octopus_api_key, args.mprn, args.gas_serial_number,
        period_from, period_to,
    )
    print(f"Octopus returned {len(weeks)} weekly record(s).")

    plan, total = build_plan(weeks, anchor_day, anchor_reading, existing)
    write_csv(args.preview_file, plan, anchor_day, anchor_reading)
    counts: dict[str, int] = {}
    for row in plan:
        counts[row.status] = counts.get(row.status, 0) + 1

    print(f"History consumption total: {total}")
    print("Plan: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    print(f"Preview: {args.preview_file}")

    if not args.apply:
        print("Dry run: nothing written to Tado. Review the CSV, then rerun with --apply.")
        return 0

    upload(tado, plan, args.delay_seconds)
    print("Backfill complete.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Cancelled", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
