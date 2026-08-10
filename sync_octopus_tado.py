#!/usr/bin/env python3

import argparse
import asyncio
import os
from datetime import date, datetime, time as datetime_time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from playwright.async_api import async_playwright
from PyTado.interface import Tado
from requests.auth import HTTPBasicAuth


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LOCAL_TIMEZONE = ZoneInfo("Europe/London")

# Real physical gas-meter reading photographed on 6 August 2026.
#
# The photograph was taken during the day, while Octopus daily aggregates use
# midnight boundaries. To avoid double-counting consumption that was already
# included in the physical reading, the ongoing sync adds complete Octopus
# daily periods beginning on the DAY AFTER this anchor date.
DEFAULT_METER_ANCHOR_DATE = "2026-08-06"
DEFAULT_METER_ANCHOR_READING = Decimal("13835.877")


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def parse_api_date(value):
    """Parse a date/datetime/API string and return datetime.date."""
    if value is None:
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    if isinstance(value, str):
        normalized_value = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized_value).date()

    raise TypeError(f"Unsupported date value: {value!r}")


def format_api_date(value):
    """Format a date-like value as YYYY-MM-DD."""
    return parse_api_date(value).isoformat()


def parse_octopus_datetime(value):
    """Parse an offset-aware Octopus interval datetime."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(
            f"Octopus returned a timezone-free datetime: {value!r}"
        )
    return parsed


def object_field(obj, *names):
    """
    Return the first matching field from either a dict or a model object.

    PyTado versions differ in whether Energy IQ responses are ordinary dicts
    or Pydantic/model objects.
    """
    if isinstance(obj, dict):
        for name in names:
            if name in obj and obj[name] is not None:
                return obj[name]
        return None

    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value

    return None


def call_tado_method(tado, *method_names, **kwargs):
    """Call the first available Tado client method from a list of candidates."""
    for method_name in method_names:
        method = getattr(tado, method_name, None)
        if callable(method):
            return method(**kwargs)

    raise AttributeError(
        f"None of the Tado methods exist on the client: "
        f"{', '.join(method_names)}"
    )


def round_meter_reading(reading):
    """
    Round a cumulative meter reading once, half-up, before sending it to Tado.

    We deliberately do not use int(), because int() truncates. More
    importantly, future calculations are never based on this rounded value:
    they always start again from the exact physical anchor.
    """
    return int(
        Decimal(str(reading)).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    )


# ---------------------------------------------------------------------------
# Octopus tariff support
# ---------------------------------------------------------------------------

def fetch_paginated_results(url, api_key):
    """Fetch all results from a paginated Octopus endpoint."""
    results = []

    while url:
        response = requests.get(
            url,
            auth=HTTPBasicAuth(api_key, ""),
            timeout=30,
        )

        if response.status_code != 200:
            raise RuntimeError(
                "Failed to retrieve data from Octopus. "
                f"Status code: {response.status_code}, "
                f"Message: {response.text}"
            )

        payload = response.json()
        results.extend(payload.get("results", []))
        url = payload.get("next")

    return results


def get_octopus_account_details(api_key, account_number):
    """Retrieve Octopus account details, including meter agreements."""
    url = f"https://api.octopus.energy/v1/accounts/{account_number}/"

    response = requests.get(
        url,
        auth=HTTPBasicAuth(api_key, ""),
        timeout=30,
    )

    if response.status_code != 200:
        raise RuntimeError(
            "Failed to retrieve Octopus account details. "
            f"Status code: {response.status_code}, "
            f"Message: {response.text}"
        )

    return response.json()


def derive_product_code_from_tariff_code(tariff_code):
    """Infer the Octopus product code from a gas tariff code."""
    parts = tariff_code.split("-")

    if len(parts) <= 2:
        return tariff_code

    product_parts = parts[2:]

    if (
        product_parts
        and len(product_parts[-1]) == 1
        and product_parts[-1].isalpha()
    ):
        product_parts = product_parts[:-1]

    return "-".join(product_parts)


def get_octopus_gas_agreements(
    account_details,
    mprn,
    gas_serial_number,
):
    """Extract gas agreements matching this MPRN and meter serial number."""
    matching_agreements = []

    for property_info in account_details.get("properties", []):
        for gas_meter_point in property_info.get("gas_meter_points", []):
            meter_point_mprn = gas_meter_point.get("mprn")

            if mprn and meter_point_mprn != mprn:
                continue

            meters = gas_meter_point.get("meters", [])

            if gas_serial_number:
                serial_numbers = {
                    meter.get("serial_number")
                    or meter.get("serialNumber")
                    for meter in meters
                }
                serial_numbers.discard(None)

                if (
                    serial_numbers
                    and gas_serial_number not in serial_numbers
                ):
                    continue

            matching_agreements.extend(
                gas_meter_point.get("agreements", [])
            )

    return matching_agreements


def get_octopus_standard_unit_rates(
    api_key,
    product_code,
    tariff_code,
):
    """Retrieve all gas unit-rate periods for an Octopus tariff."""
    encoded_tariff_code = quote(tariff_code, safe="")

    url = (
        f"https://api.octopus.energy/v1/products/{product_code}/"
        f"gas-tariffs/{encoded_tariff_code}/standard-unit-rates/"
    )

    return fetch_paginated_results(url, api_key)


def build_octopus_tariff_periods(agreement, unit_rates):
    """Convert Octopus rate records into Tado-friendly tariff periods."""
    agreement_start = (
        parse_api_date(agreement.get("valid_from"))
        or date.min
    )
    agreement_end = parse_api_date(agreement.get("valid_to"))

    raw_periods = []

    for rate in unit_rates:
        tariff_pence = rate.get("value_inc_vat")
        rate_start = parse_api_date(rate.get("valid_from"))

        if tariff_pence is None or rate_start is None:
            continue

        start_date = max(agreement_start, rate_start)

        if (
            agreement_end is not None
            and start_date > agreement_end
        ):
            continue

        raw_periods.append(
            {
                "start_date": start_date,
                "tariff_pence_per_kwh": tariff_pence,
                "unit": "kWh",
            }
        )

    raw_periods.sort(
        key=lambda period: period["start_date"]
    )

    merged_periods = []

    for period in raw_periods:
        if (
            merged_periods
            and merged_periods[-1]["start_date"]
            == period["start_date"]
        ):
            merged_periods[-1] = period
            continue

        if (
            merged_periods
            and merged_periods[-1]["tariff_pence_per_kwh"]
            == period["tariff_pence_per_kwh"]
        ):
            continue

        merged_periods.append(period)

    for index, period in enumerate(merged_periods):
        end_date = None

        if index + 1 < len(merged_periods):
            end_date = (
                merged_periods[index + 1]["start_date"]
                - timedelta(days=1)
            )
        elif agreement_end is not None:
            end_date = agreement_end

        period["end_date"] = end_date

    return merged_periods


def get_tado_last_tariff_checkpoint(tado):
    """Return the latest tariff start date already stored in Tado."""
    try:
        tariff_data = call_tado_method(
            tado,
            "get_eiq_tariffs",
            "getEIQTariffs",
        )

        if isinstance(tariff_data, dict):
            tariffs = tariff_data.get("tariffs", [])
        else:
            try:
                tariffs = list(tariff_data)
            except TypeError:
                tariffs = []

        latest_start_date = None

        for tariff in tariffs:
            start_value = object_field(
                tariff,
                "startDate",
                "start_date",
                "date",
                "fromDate",
                "from_date",
            )

            if not start_value:
                continue

            start_date = parse_api_date(start_value)

            if (
                latest_start_date is None
                or start_date > latest_start_date
            ):
                latest_start_date = start_date

        if latest_start_date is not None:
            print(
                "Last Tado tariff starts on: "
                f"{latest_start_date.isoformat()}"
            )

        return latest_start_date

    except Exception as exc:
        print(
            f"Could not retrieve Tado tariff history: {exc}"
        )
        return None


def discover_octopus_tariff_periods(
    api_key,
    account_number,
    mprn,
    gas_serial_number,
    since_date=None,
):
    """Discover Octopus gas tariff periods missing from Tado."""
    account_details = get_octopus_account_details(
        api_key,
        account_number,
    )

    agreements = get_octopus_gas_agreements(
        account_details,
        mprn,
        gas_serial_number,
    )

    if not agreements:
        raise RuntimeError(
            "No matching gas agreements found in Octopus account "
            "details for the supplied MPRN / gas serial number."
        )

    periods_to_sync = []

    sorted_agreements = sorted(
        agreements,
        key=lambda agreement: (
            parse_api_date(agreement.get("valid_from"))
            or date.min
        ),
    )

    for agreement in sorted_agreements:
        tariff_code = (
            agreement.get("tariff_code")
            or agreement.get("tariffCode")
        )

        if not tariff_code:
            continue

        product_code = (
            agreement.get("product_code")
            or derive_product_code_from_tariff_code(
                tariff_code
            )
        )

        unit_rates = get_octopus_standard_unit_rates(
            api_key,
            product_code,
            tariff_code,
        )

        agreement_periods = build_octopus_tariff_periods(
            agreement,
            unit_rates,
        )

        for period in agreement_periods:
            if (
                since_date is not None
                and period["start_date"] <= since_date
            ):
                continue

            periods_to_sync.append(period)

    periods_to_sync.sort(
        key=lambda period: period["start_date"]
    )

    return periods_to_sync


def sync_octopus_tariffs_to_tado(
    tado,
    api_key,
    account_number,
    mprn,
    gas_serial_number,
):
    """Sync missing Octopus gas tariff periods into Tado Energy IQ."""
    last_tado_tariff_start = (
        get_tado_last_tariff_checkpoint(tado)
    )

    tariff_periods = discover_octopus_tariff_periods(
        api_key,
        account_number,
        mprn,
        gas_serial_number,
        since_date=last_tado_tariff_start,
    )

    if not tariff_periods:
        print(
            "No Octopus tariff changes need to be synced to Tado"
        )
        return []

    synced_periods = []

    for period in tariff_periods:
        payload = {
            "from_date": format_api_date(
                period["start_date"]
            ),
            "tariff": (
                period["tariff_pence_per_kwh"] / 100
            ),
            "unit": period["unit"],
        }

        if period["end_date"] is not None:
            payload["to_date"] = format_api_date(
                period["end_date"]
            )
            payload["is_period"] = True
        else:
            payload["is_period"] = False

        result = call_tado_method(
            tado,
            "set_eiq_tariff",
            "setEIQTariff",
            **payload,
        )

        print(
            f"Synced tariff period to Tado: "
            f"{payload} -> {result}"
        )

        synced_periods.append(payload)

    return synced_periods


# ---------------------------------------------------------------------------
# Tado Energy IQ meter-reading support
# ---------------------------------------------------------------------------

def get_tado_last_meter_reading(tado):
    """
    Return the most recent Tado Energy IQ cumulative reading.

    Supports both older dict responses and newer list/model responses.
    """
    try:
        eiq_data = call_tado_method(
            tado,
            "get_eiq_meter_readings",
            "getEIQMeterReadings",
        )

        if isinstance(eiq_data, dict):
            readings = eiq_data.get("readings", [])
        else:
            try:
                readings = list(eiq_data)
            except TypeError:
                readings = []

        parsed = []

        for reading in readings:
            reading_value = object_field(
                reading,
                "reading",
                "value",
            )

            reading_date = object_field(
                reading,
                "date",
                "readingDate",
                "reading_date",
            )

            if (
                reading_value is None
                or reading_date is None
            ):
                continue

            parsed.append(
                (
                    Decimal(str(reading_value)),
                    parse_api_date(reading_date),
                )
            )

        if not parsed:
            print(
                "Tado contains no Energy IQ meter readings."
            )
            return None, None

        latest_reading, latest_date = max(
            parsed,
            key=lambda item: item[1],
        )

        print(
            f"Last Tado meter reading: {latest_reading} "
            f"(date: {latest_date.isoformat()})"
        )

        return latest_reading, latest_date

    except Exception as exc:
        print(
            f"Could not retrieve last Tado meter reading: {exc}"
        )
        return None, None


def fetch_octopus_daily_consumption_since_anchor(
    api_key,
    mprn,
    gas_serial_number,
    anchor_date,
):
    """
    Fetch all complete Octopus daily gas-consumption intervals after anchor.

    The physical meter photo was taken sometime during anchor_date, not at
    midnight. We therefore begin with midnight at the START OF THE NEXT DAY.
    This avoids counting any part of anchor_date twice.

    Returns a sorted list of dictionaries:
      {
          "start": aware datetime,
          "end": aware datetime,
          "consumption": Decimal
      }
    """
    anchor_day = parse_api_date(anchor_date)

    first_counted_day = anchor_day + timedelta(days=1)

    period_from = datetime.combine(
        first_counted_day,
        datetime_time.min,
        tzinfo=LOCAL_TIMEZONE,
    )

    now = datetime.now(LOCAL_TIMEZONE)

    base_url = (
        "https://api.octopus.energy/v1/"
        f"gas-meter-points/{mprn}/meters/"
        f"{gas_serial_number}/consumption/"
    )

    params = {
        "period_from": (
            period_from
            .astimezone(ZoneInfo("UTC"))
            .isoformat()
            .replace("+00:00", "Z")
        ),
        "period_to": (
            now
            .astimezone(ZoneInfo("UTC"))
            .isoformat()
            .replace("+00:00", "Z")
        ),
        "group_by": "day",
        "order_by": "period",
        "page_size": 250,
    }

    intervals = []
    url = base_url

    while url:
        response = requests.get(
            url,
            params=params,
            auth=HTTPBasicAuth(api_key, ""),
            timeout=30,
        )

        # Pagination URLs already contain their own query string.
        params = None

        if response.status_code != 200:
            raise RuntimeError(
                "Failed to retrieve Octopus gas consumption. "
                f"MPRN: {mprn}, "
                f"Gas serial number: {gas_serial_number}, "
                f"Status code: {response.status_code}, "
                f"Message: {response.text}"
            )

        payload = response.json()

        for interval in payload.get("results", []):
            interval_start = (
                parse_octopus_datetime(
                    interval["interval_start"]
                )
                .astimezone(LOCAL_TIMEZONE)
            )

            interval_end = (
                parse_octopus_datetime(
                    interval["interval_end"]
                )
                .astimezone(LOCAL_TIMEZONE)
            )

            consumption = Decimal(
                str(interval["consumption"])
            )

            # The Octopus consumption API can return records that overlap
            # requested boundaries. Only retain complete daily intervals
            # wholly after our counted boundary and wholly in the past.
            if interval_start < period_from:
                continue

            if interval_end > now:
                continue

            intervals.append(
                {
                    "start": interval_start,
                    "end": interval_end,
                    "consumption": consumption,
                }
            )

        url = payload.get("next")

    # De-duplicate defensively.
    unique = {}

    for interval in intervals:
        key = (
            interval["start"],
            interval["end"],
        )
        unique[key] = interval

    intervals = sorted(
        unique.values(),
        key=lambda interval: (
            interval["start"],
            interval["end"],
        ),
    )

    return period_from, intervals


def calculate_meter_reading_from_anchor(
    api_key,
    mprn,
    gas_serial_number,
    anchor_date,
    anchor_reading,
):
    """
    Recalculate the meter register from the fixed physical anchor every run.

    Returns:
        exact_reading: Decimal
        reading_date: date

    The reading date is the boundary at the end of the latest complete Octopus
    daily period.
    """
    anchor_day = parse_api_date(anchor_date)
    exact_anchor = Decimal(str(anchor_reading))

    expected_start, intervals = (
        fetch_octopus_daily_consumption_since_anchor(
            api_key,
            mprn,
            gas_serial_number,
            anchor_day,
        )
    )

    print(
        f"Physical meter anchor: "
        f"{exact_anchor} m3 on "
        f"{anchor_day.isoformat()}"
    )

    if not intervals:
        print(
            "Octopus has no complete daily periods after "
            "the physical anchor yet."
        )

        return exact_anchor, anchor_day

    # Never silently under-count a missing day.
    if intervals[0]["start"] != expected_start:
        raise RuntimeError(
            "Octopus data does not begin at the first "
            "complete day after the physical meter anchor. "
            f"Expected {expected_start.isoformat()}, "
            f"received {intervals[0]['start'].isoformat()}. "
            "No Tado meter reading was uploaded."
        )

    for previous, current in zip(
        intervals,
        intervals[1:],
    ):
        if current["start"] != previous["end"]:
            raise RuntimeError(
                "Gap detected in Octopus daily consumption "
                "data between "
                f"{previous['end'].isoformat()} and "
                f"{current['start'].isoformat()}. "
                "No Tado meter reading was uploaded."
            )

    consumption_since_anchor = sum(
        (
            interval["consumption"]
            for interval in intervals
        ),
        Decimal("0"),
    )

    exact_reading = (
        exact_anchor
        + consumption_since_anchor
    )

    latest_complete_boundary = intervals[-1]["end"]
    reading_date = latest_complete_boundary.date()

    print(
        "Octopus consumption since first complete day "
        f"after anchor: {consumption_since_anchor} m3"
    )

    print(
        "Latest complete Octopus boundary: "
        f"{latest_complete_boundary.isoformat()}"
    )

    print(
        "Calculated cumulative meter reading: "
        f"{exact_reading} m3"
    )

    return exact_reading, reading_date


def set_tado_meter_reading(
    tado,
    reading_date,
    reading,
):
    """
    Write a dated cumulative reading to Tado Energy IQ.

    python-tado 0.19.2 expects the date as a YYYY-MM-DD string and works with
    positional arguments. This also avoids keyword-name differences between
    PyTado releases.
    """
    formatted_date = format_api_date(reading_date)
    rounded_reading = round_meter_reading(reading)

    for method_name in (
        "set_eiq_meter_readings",
        "setEIQMeterReadings",
    ):
        method = getattr(tado, method_name, None)

        if callable(method):
            return method(
                formatted_date,
                rounded_reading,
            )

    raise AttributeError(
        "Neither set_eiq_meter_readings nor "
        "setEIQMeterReadings exists on the Tado client."
    )


def sync_meter_reading(
    tado,
    api_key,
    mprn,
    gas_serial_number,
    anchor_date,
    anchor_reading,
):
    """Calculate and, when appropriate, upload the latest cumulative reading."""
    calculated_reading, reading_date = (
        calculate_meter_reading_from_anchor(
            api_key,
            mprn,
            gas_serial_number,
            anchor_date,
            anchor_reading,
        )
    )

    rounded_reading = round_meter_reading(
        calculated_reading
    )

    last_tado_reading, last_tado_date = (
        get_tado_last_meter_reading(tado)
    )

    if last_tado_date is not None:
        last_tado_date = parse_api_date(
            last_tado_date
        )

        if last_tado_date > reading_date:
            print(
                "Tado already contains a newer meter "
                f"reading dated "
                f"{last_tado_date.isoformat()}; "
                f"Octopus currently reaches only "
                f"{reading_date.isoformat()}. "
                "Skipping meter upload."
            )
            return None

        if last_tado_date == reading_date:
            if (
                last_tado_reading is not None
                and round_meter_reading(
                    last_tado_reading
                )
                == rounded_reading
            ):
                print(
                    "Tado already contains "
                    f"{rounded_reading} for "
                    f"{reading_date.isoformat()}; "
                    "nothing to update."
                )
            else:
                print(
                    "Tado already contains a different "
                    "meter reading for "
                    f"{reading_date.isoformat()}. "
                    "Skipping the conflicting same-date "
                    "insert rather than overwriting it."
                )

            return None

    print(
        f"Uploading Tado meter reading "
        f"{rounded_reading} for "
        f"{reading_date.isoformat()}"
    )

    result = set_tado_meter_reading(
        tado,
        reading_date,
        calculated_reading,
    )

    print(f"Tado meter update result: {result}")

    return result


# ---------------------------------------------------------------------------
# Tado authentication
# ---------------------------------------------------------------------------

async def browser_login(
    url,
    username,
    password,
):
    """
    Automate the Tado device-code authorisation page.

    This is the login flow already proven to work in the GitHub Action.
    """
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True
        )

        context = await browser.new_context()
        page = await context.new_page()

        try:
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=30000,
            )

            # Initial device-authorisation page.
            await page.wait_for_selector(
                'text="Submit"',
                timeout=10000,
            )

            await page.click('text="Submit"')

            # Tado login form.
            await page.wait_for_selector(
                'input[name="loginId"]',
                timeout=15000,
            )

            await page.fill(
                'input[id="loginId"]',
                username,
            )

            await page.fill(
                'input[name="password"]',
                password,
            )

            await page.click(
                'button.c-btn--primary:has-text("Sign in")'
            )

            # Wait for the login form to disappear rather than depending on
            # Tado's internal success-page CSS classes.
            await page.locator(
                'input[name="loginId"]'
            ).wait_for(
                state="hidden",
                timeout=30000,
            )

            # Give the OAuth device authorisation request time to complete.
            await page.wait_for_timeout(3000)

            await page.screenshot(
                path="tado-login-result.png",
                full_page=True,
            )

        except Exception:
            await page.screenshot(
                path="tado-login-result.png",
                full_page=True,
            )
            raise

        finally:
            await browser.close()


def tado_login(username, password):
    """Authenticate to Tado using PyTado's device-code flow."""
    tado = Tado(
        token_file_path="/tmp/tado_refresh_token"
    )

    status = tado.device_activation_status()

    if status == "PENDING":
        url = tado.device_verification_url()

        asyncio.run(
            browser_login(
                url,
                username,
                password,
            )
        )

        tado.device_activation()

        status = tado.device_activation_status()

    if status == "COMPLETED":
        print("Login successful")
    else:
        raise RuntimeError(
            f"Tado login did not complete. "
            f"Status: {status}"
        )

    return tado


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Sync Octopus gas consumption and tariff data "
            "to Tado Energy IQ."
        )
    )

    parser.add_argument(
        "--tado-email",
        required=True,
        help="Tado account email",
    )

    parser.add_argument(
        "--tado-password",
        required=True,
        help="Tado account password",
    )

    parser.add_argument(
        "--mprn",
        required=True,
        help=(
            "MPRN (Meter Point Reference Number) "
            "for the gas meter"
        ),
    )

    parser.add_argument(
        "--gas-serial-number",
        required=True,
        help="Gas meter serial number",
    )

    parser.add_argument(
        "--octopus-api-key",
        required=True,
        help="Octopus API key",
    )

    parser.add_argument(
        "--meter-anchor-date",
        default=os.getenv(
            "METER_ANCHOR_DATE",
            DEFAULT_METER_ANCHOR_DATE,
        ),
        help=(
            "Date of the physical meter anchor, "
            "YYYY-MM-DD. "
            f"Default: {DEFAULT_METER_ANCHOR_DATE}"
        ),
    )

    parser.add_argument(
        "--meter-anchor-reading",
        type=Decimal,
        default=Decimal(
            os.getenv(
                "METER_ANCHOR_READING",
                str(DEFAULT_METER_ANCHOR_READING),
            )
        ),
        help=(
            "Exact physical cumulative gas meter "
            "reading in m3 at the anchor date. "
            f"Default: {DEFAULT_METER_ANCHOR_READING}"
        ),
    )

    parser.add_argument(
        "--octopus-account-number",
        default=os.getenv(
            "OCTOPUS_ACCOUNT_NUMBER"
        ),
        help=(
            "Octopus account number. Required when "
            "--update-tariff is enabled; can also be "
            "supplied via OCTOPUS_ACCOUNT_NUMBER."
        ),
    )

    parser.add_argument(
        "--update-tariff",
        action="store_true",
        help=(
            "Also sync Octopus gas tariff periods "
            "to Tado Energy IQ."
        ),
    )

    return parser.parse_args()


def main():
    args = parse_args()

    # Authenticate once and reuse the client for meter and tariff sync.
    tado = tado_login(
        args.tado_email,
        args.tado_password,
    )

    # Meter reading sync.
    sync_meter_reading(
        tado=tado,
        api_key=args.octopus_api_key,
        mprn=args.mprn,
        gas_serial_number=args.gas_serial_number,
        anchor_date=args.meter_anchor_date,
        anchor_reading=args.meter_anchor_reading,
    )

    # Optional tariff sync.
    if args.update_tariff:
        if not args.octopus_account_number:
            print(
                "--update-tariff was enabled but no "
                "Octopus account number was provided. "
                "Set OCTOPUS_ACCOUNT_NUMBER or use "
                "--octopus-account-number."
            )
        else:
            try:
                sync_octopus_tariffs_to_tado(
                    tado=tado,
                    api_key=args.octopus_api_key,
                    account_number=(
                        args.octopus_account_number
                    ),
                    mprn=args.mprn,
                    gas_serial_number=(
                        args.gas_serial_number
                    ),
                )
            except Exception as exc:
                # Keep the meter sync successful even if tariff discovery has
                # a temporary problem.
                print(
                    f"Tariff sync failed: {exc}"
                )


if __name__ == "__main__":
    main()
