#!/usr/bin/env python3
"""
Backfill Octopus gas consumption into tado Energy IQ as weekly cumulative
meter readings, anchored to a real physical meter reading.

The script is dry-run-only unless --apply is supplied.

Important date convention
-------------------------
tado stores a meter-reading DATE rather than an exact timestamp. This script
treats --anchor-date as the local midnight boundary at the start of that date.
A photograph taken later that day can therefore introduce a small sub-day
offset. Because tado accepts whole-number gas readings, this is normally
immaterial, but the CSV preserves the exact decimal reconstruction.

Place this file beside sync_octopus_tado.py so it can reuse tado_login().
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests
from requests.auth import HTTPBasicAuth

try:
    from sync_octopus_tado import tado_login
except ImportError as exc:
    raise SystemExit(
        "Put backfill_octopus_to_tado.py in the repository root beside "
        "sync_octopus_tado.py."
    ) from exc


API_ROOT = "https://api.octopus.energy/v1"
LOCAL_TZ = ZoneInfo("Europe/London")


@dataclass(frozen=True)
class DailyConsumption:
    start: datetime
    end: datetime
    consumption: Decimal


@dataclass(frozen=True)
class ExistingReading:
    day: date
    reading: int


@dataclass(frozen=True)
class Coverage:
    requested_start: datetime
    anchor_boundary: datetime
    actual_start: datetime
    actual_end: datetime
    complete_to_anchor: bool
    missing_tail_hours: Decimal
    gaps: tuple[str, ...]


@dataclass(frozen=True)
class PlanRow:
    day: date
    exact: Decimal
    integer: int
    period_consumption: Decimal | None
    interval_start: datetime | None
    interval_end: datetime | None
    status: str
    note: str


def parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"Octopus returned a timezone-free datetime: {value!r}")
    return parsed


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


def get_tado_readings(tado: Any) -> list[ExistingReading]:
    method = getattr(tado, "get_eiq_meter_readings", None)
    if not callable(method):
        method = getattr(tado, "getEIQMeterReadings", None)
    if not callable(method):
        raise RuntimeError("The installed PyTado has no Energy IQ read method.")

    raw = method()
    if isinstance(raw, dict):
        items = raw.get("readings", [])
    else:
        try:
            items = list(raw)
        except TypeError as exc:
            raise RuntimeError(
                f"Unexpected Tado meter-reading response: {type(raw)!r}"
            ) from exc

    readings: list[ExistingReading] = []
    for item in items:
        mapping = model_mapping(item)
        raw_day = first(mapping, ("date", "readingDate", "reading_date"))
        raw_value = first(mapping, ("reading", "value"))

        if raw_day is None or raw_value is None:
            print(f"Warning: ignored unrecognised Tado reading: {mapping!r}")
            continue

        readings.append(
            ExistingReading(
                day=parse_day(raw_day),
                reading=tado_integer(as_decimal(raw_value, "Tado reading")),
            )
        )

    return sorted(readings, key=lambda row: row.day)


def fetch_daily_consumption(
    api_key: str,
    mprn: str,
    serial: str,
    period_from: datetime,
    period_to: datetime,
) -> list[DailyConsumption]:
    """
    Fetch daily local-time gas consumption and follow Octopus pagination.

    The request deliberately extends beyond the anchor boundary. Returned
    records are filtered later because the API can return overlapping periods.
    """
    url = f"{API_ROOT}/gas-meter-points/{mprn}/meters/{serial}/consumption/"
    params: dict[str, Any] | None = {
        "period_from": period_from.astimezone(ZoneInfo("UTC"))
        .isoformat()
        .replace("+00:00", "Z"),
        "period_to": period_to.astimezone(ZoneInfo("UTC"))
        .isoformat()
        .replace("+00:00", "Z"),
        "group_by": "day",
        "order_by": "period",
        "page_size": 250,
    }

    session = requests.Session()
    session.auth = HTTPBasicAuth(api_key, "")
    session.headers["User-Agent"] = "octopus-to-tado-backfill/2.0"

    records: dict[tuple[datetime, datetime], DailyConsumption] = {}

    while url:
        response = session.get(url, params=params, timeout=45)
        params = None

        if response.status_code != 200:
            raise RuntimeError(
                f"Octopus request failed: HTTP {response.status_code}: "
                f"{response.text}"
            )

        payload = response.json()
        for item in payload.get("results", []):
            row = DailyConsumption(
                start=parse_datetime(item["interval_start"]),
                end=parse_datetime(item["interval_end"]),
                consumption=as_decimal(item["consumption"], "consumption"),
            )

            if row.end <= row.start:
                raise RuntimeError(f"Invalid Octopus interval: {item!r}")
            if row.consumption < 0:
                raise RuntimeError(f"Negative Octopus consumption: {item!r}")

            records[(row.start, row.end)] = row

        url = payload.get("next")

    return sorted(records.values(), key=lambda row: (row.start, row.end))


def select_and_check_coverage(
    records: list[DailyConsumption],
    requested_start: datetime,
    anchor_boundary: datetime,
) -> tuple[list[DailyConsumption], Coverage]:
    """
    Keep complete daily intervals ending no later than the anchor boundary,
    detect internal gaps, and report whether Octopus reaches the anchor.
    """
    selected = [
        row
        for row in records
        if row.end > requested_start and row.end <= anchor_boundary
    ]

    if not selected:
        raise RuntimeError(
            "Octopus returned no complete daily intervals ending on or before "
            "the anchor boundary."
        )

    selected.sort(key=lambda row: (row.start, row.end))

    gaps: list[str] = []
    for previous, current in zip(selected, selected[1:]):
        if current.start != previous.end:
            gaps.append(
                f"{previous.end.isoformat()} to {current.start.isoformat()}"
            )

    actual_start = selected[0].start
    actual_end = selected[-1].end
    complete = actual_end == anchor_boundary

    missing_seconds = max(
        0.0,
        (anchor_boundary.astimezone(ZoneInfo("UTC"))
         - actual_end.astimezone(ZoneInfo("UTC"))).total_seconds(),
    )
    missing_hours = (
        Decimal(str(missing_seconds)) / Decimal("3600")
    ).quantize(Decimal("0.001"))

    coverage = Coverage(
        requested_start=requested_start,
        anchor_boundary=anchor_boundary,
        actual_start=actual_start,
        actual_end=actual_end,
        complete_to_anchor=complete,
        missing_tail_hours=missing_hours,
        gaps=tuple(gaps),
    )
    return selected, coverage


def make_status(
    day: date,
    integer: int,
    existing_by_day: dict[date, int],
) -> tuple[str, str]:
    old = existing_by_day.get(day)
    if old is None:
        return "upload", ""
    if old == integer:
        return "skip-existing", "Same date and value already exist in Tado"
    return "conflict", f"Tado has {old}; reconstruction gives {integer}"


def build_plan(
    days: list[DailyConsumption],
    anchor_day: date,
    anchor_reading: Decimal,
    existing: list[ExistingReading],
    coverage: Coverage,
) -> tuple[list[PlanRow], Decimal]:
    """
    Reconstruct cumulative readings backwards from the physical anchor.

    Weekly checkpoints are Monday 00:00 boundaries. The physical anchor is
    always added as its own final row on anchor_day.
    """
    total = sum((row.consumption for row in days), Decimal("0"))
    starting = anchor_reading - total

    if starting < 0:
        raise RuntimeError(
            "Reconstructed starting reading is negative. Check the meter unit "
            f"and anchor. Anchor={anchor_reading}; history={total}; "
            f"start={starting}."
        )

    existing_by_day = {row.day: row.reading for row in existing}
    candidates: dict[date, PlanRow] = {}

    baseline_day = days[0].start.astimezone(LOCAL_TZ).date()
    baseline_integer = tado_integer(starting)
    baseline_status, baseline_note = make_status(
        baseline_day, baseline_integer, existing_by_day
    )
    candidates[baseline_day] = PlanRow(
        day=baseline_day,
        exact=starting,
        integer=baseline_integer,
        period_consumption=None,
        interval_start=days[0].start,
        interval_end=None,
        status=baseline_status,
        note=("baseline" + (f"; {baseline_note}" if baseline_note else "")),
    )

    running = starting
    bucket_start = days[0].start
    bucket_consumption = Decimal("0")

    for row in days:
        running += row.consumption
        bucket_consumption += row.consumption

        local_end = row.end.astimezone(LOCAL_TZ)
        is_monday_boundary = (
            local_end.weekday() == 0
            and local_end.hour == 0
            and local_end.minute == 0
            and local_end.second == 0
        )

        if is_monday_boundary and local_end.date() < anchor_day:
            integer = tado_integer(running)
            status, extra = make_status(
                local_end.date(), integer, existing_by_day
            )
            note = "weekly"
            if extra:
                note += f"; {extra}"

            candidates[local_end.date()] = PlanRow(
                day=local_end.date(),
                exact=running,
                integer=integer,
                period_consumption=bucket_consumption,
                interval_start=bucket_start,
                interval_end=row.end,
                status=status,
                note=note,
            )
            bucket_start = row.end
            bucket_consumption = Decimal("0")

    # The physical anchor is deliberately separate from the final Monday.
    anchor_integer = tado_integer(anchor_reading)
    anchor_status, anchor_extra = make_status(
        anchor_day, anchor_integer, existing_by_day
    )

    anchor_note = "physical meter anchor"
    if not coverage.complete_to_anchor:
        anchor_note += (
            f"; WARNING: Octopus stops at {coverage.actual_end.isoformat()}, "
            f"{coverage.missing_tail_hours} hours before the anchor boundary"
        )
    if anchor_extra:
        anchor_note += f"; {anchor_extra}"

    candidates[anchor_day] = PlanRow(
        day=anchor_day,
        exact=anchor_reading,
        integer=anchor_integer,
        period_consumption=bucket_consumption,
        interval_start=bucket_start,
        interval_end=coverage.actual_end,
        status=anchor_status,
        note=anchor_note,
    )

    plan = [candidates[key] for key in sorted(candidates)]

    for previous, current in zip(plan, plan[1:]):
        if current.exact < previous.exact:
            raise RuntimeError(
                f"Reconstructed reading decreases between {previous.day} "
                f"and {current.day}."
            )
        if current.integer < previous.integer:
            raise RuntimeError(
                f"Rounded Tado reading decreases between {previous.day} "
                f"and {current.day}."
            )

    return plan, total


def write_csv(
    path: Path,
    plan: list[PlanRow],
    anchor_day: date,
    anchor_reading: Decimal,
    coverage: Coverage,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    fields = [
        "reading_date",
        "interval_start",
        "interval_end",
        "period_consumption",
        "exact_cumulative_reading",
        "tado_integer_reading",
        "status",
        "note",
        "anchor_date",
        "anchor_reading",
        "coverage_complete",
        "coverage_actual_start",
        "coverage_actual_end",
        "coverage_missing_tail_hours",
        "coverage_internal_gap_count",
    ]

    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()

        for row in plan:
            writer.writerow(
                {
                    "reading_date": row.day.isoformat(),
                    "interval_start": (
                        row.interval_start.isoformat()
                        if row.interval_start else ""
                    ),
                    "interval_end": (
                        row.interval_end.isoformat()
                        if row.interval_end else ""
                    ),
                    "period_consumption": (
                        str(row.period_consumption)
                        if row.period_consumption is not None else ""
                    ),
                    "exact_cumulative_reading": str(row.exact),
                    "tado_integer_reading": row.integer,
                    "status": row.status,
                    "note": row.note,
                    "anchor_date": anchor_day.isoformat(),
                    "anchor_reading": str(anchor_reading),
                    "coverage_complete": coverage.complete_to_anchor,
                    "coverage_actual_start": coverage.actual_start.isoformat(),
                    "coverage_actual_end": coverage.actual_end.isoformat(),
                    "coverage_missing_tail_hours": str(
                        coverage.missing_tail_hours
                    ),
                    "coverage_internal_gap_count": len(coverage.gaps),
                }
            )


def upload(
    tado: Any,
    plan: list[PlanRow],
    coverage: Coverage,
    allow_incomplete_coverage: bool,
    delay: float,
) -> None:
    conflicts = [row for row in plan if row.status == "conflict"]
    if conflicts:
        details = "\n".join(
            f"  {row.day}: {row.note}" for row in conflicts[:10]
        )
        raise RuntimeError(
            f"Refusing to upload because of Tado conflicts:\n{details}"
        )

    if coverage.gaps:
        details = "\n".join(f"  {gap}" for gap in coverage.gaps[:10])
        raise RuntimeError(
            "Refusing to upload because Octopus has internal data gaps:\n"
            f"{details}"
        )

    if not coverage.complete_to_anchor and not allow_incomplete_coverage:
        raise RuntimeError(
            "Refusing to upload because Octopus has not reached the anchor "
            f"boundary. Latest complete interval ends "
            f"{coverage.actual_end.isoformat()}, leaving "
            f"{coverage.missing_tail_hours} hours. Rerun later, or explicitly "
            "use --allow-incomplete-coverage."
        )

    rows = [row for row in plan if row.status == "upload"]
    if not rows:
        print("Nothing to upload.")
        return

    method = getattr(tado, "set_eiq_meter_readings", None)
    if not callable(method):
        method = getattr(tado, "setEIQMeterReadings", None)
    if not callable(method):
        raise RuntimeError("The installed PyTado has no Energy IQ write method.")

    print(f"Uploading {len(rows)} readings oldest first...")
    for index, row in enumerate(rows, 1):
        result = method(reading_date=row.day, reading=row.integer)
        print(
            f"[{index}/{len(rows)}] {row.day}: {row.integer} -> {result}"
        )
        if index < len(rows) and delay:
            time.sleep(delay)


def required(
    parser: argparse.ArgumentParser,
    value: str | None,
    label: str,
) -> str:
    if not value or not value.strip():
        parser.error(
            f"{label} is required as an argument or environment variable"
        )
    return value.strip()


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("--tado-email", default=os.getenv("TADO_EMAIL"))
    parser.add_argument("--tado-password", default=os.getenv("TADO_PASSWORD"))
    parser.add_argument("--mprn", default=os.getenv("OCTOPUS_MPRN"))
    parser.add_argument(
        "--gas-serial-number",
        default=os.getenv("OCTOPUS_GAS_SERIAL"),
    )
    parser.add_argument(
        "--octopus-api-key",
        default=os.getenv("OCTOPUS_API_KEY"),
    )

    period = parser.add_mutually_exclusive_group()
    period.add_argument("--years", type=int, default=3)
    period.add_argument("--start-date", type=date.fromisoformat)

    parser.add_argument(
        "--anchor-date",
        type=date.fromisoformat,
        required=True,
        help="Physical meter-reading date, YYYY-MM-DD.",
    )
    parser.add_argument(
        "--anchor-reading",
        type=Decimal,
        required=True,
        help="Exact physical cumulative gas reading in cubic metres.",
    )
    parser.add_argument(
        "--preview-file",
        type=Path,
        default=Path("tado-backfill-preview.csv"),
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--allow-incomplete-coverage",
        action="store_true",
        help=(
            "Permit upload when Octopus stops before the anchor boundary. "
            "This is not recommended and never overrides internal data gaps."
        ),
    )
    parser.add_argument("--delay-seconds", type=float, default=1.0)

    args = parser.parse_args()

    args.tado_email = required(
        parser, args.tado_email, "Tado email"
    )
    args.tado_password = required(
        parser, args.tado_password, "Tado password"
    )
    args.mprn = required(parser, args.mprn, "Octopus MPRN")
    args.gas_serial_number = required(
        parser, args.gas_serial_number, "Gas serial number"
    )
    args.octopus_api_key = required(
        parser, args.octopus_api_key, "Octopus API key"
    )

    if args.years is not None and args.years <= 0:
        parser.error("--years must be positive")
    if args.delay_seconds < 0:
        parser.error("--delay-seconds cannot be negative")
    if args.anchor_reading < 0:
        parser.error("--anchor-reading cannot be negative")

    return args


def main() -> int:
    args = arguments()

    print("Authenticating with Tado...")
    tado = tado_login(args.tado_email, args.tado_password)
    existing = get_tado_readings(tado)
    print(f"Tado contains {len(existing)} meter reading(s).")

    anchor_boundary = datetime.combine(
        args.anchor_date,
        dt_time.min,
        tzinfo=LOCAL_TZ,
    )
    print(
        f"Physical anchor: {args.anchor_reading} on {args.anchor_date} "
        f"(treated as {anchor_boundary.isoformat()})"
    )

    start_day = args.start_date or (
        args.anchor_date
        - timedelta(days=round(args.years * 365.2425))
    )
    requested_start = datetime.combine(
        start_day,
        dt_time.min,
        tzinfo=LOCAL_TZ,
    )

    # Request one additional day, then filter precisely.
    request_end = anchor_boundary + timedelta(days=1)

    print(
        "Fetching daily Octopus data from "
        f"{requested_start.isoformat()} to {request_end.isoformat()}..."
    )
    raw_days = fetch_daily_consumption(
        args.octopus_api_key,
        args.mprn,
        args.gas_serial_number,
        requested_start,
        request_end,
    )
    print(f"Octopus returned {len(raw_days)} daily record(s).")

    days, coverage = select_and_check_coverage(
        raw_days,
        requested_start,
        anchor_boundary,
    )

    print(f"Actual Octopus start: {coverage.actual_start.isoformat()}")
    print(f"Actual Octopus end:   {coverage.actual_end.isoformat()}")
    print(f"Coverage complete:    {coverage.complete_to_anchor}")
    print(f"Missing tail hours:   {coverage.missing_tail_hours}")
    print(f"Internal gaps:        {len(coverage.gaps)}")

    plan, total = build_plan(
        days,
        args.anchor_date,
        args.anchor_reading,
        existing,
        coverage,
    )
    write_csv(
        args.preview_file,
        plan,
        args.anchor_date,
        args.anchor_reading,
        coverage,
    )

    counts: dict[str, int] = {}
    for row in plan:
        counts[row.status] = counts.get(row.status, 0) + 1

    print(f"Included Octopus consumption: {total}")
    print(
        "Plan: "
        + ", ".join(
            f"{key}={value}" for key, value in sorted(counts.items())
        )
    )
    print(
        f"First planned reading: {plan[0].day} = "
        f"{plan[0].exact} ({plan[0].integer})"
    )
    print(
        f"Final planned reading: {plan[-1].day} = "
        f"{plan[-1].exact} ({plan[-1].integer})"
    )
    print(f"Preview: {args.preview_file}")

    if coverage.gaps:
        print("WARNING: internal Octopus data gaps were found:")
        for gap in coverage.gaps:
            print(f"  {gap}")
        print("Upload remains blocked.", file=sys.stderr)
    
    if not coverage.complete_to_anchor:
        print(
            "WARNING: Octopus has not reached the anchor boundary. Leave "
            "'allow incomplete coverage' disabled and rerun later.",
            file=sys.stderr,
        )

    if not args.apply:
        print(
            "Dry run: nothing written to Tado. Review the CSV, then rerun "
            "with --apply."
        )
        return 0

    upload(
        tado,
        plan,
        coverage,
        args.allow_incomplete_coverage,
        args.delay_seconds,
    )
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
