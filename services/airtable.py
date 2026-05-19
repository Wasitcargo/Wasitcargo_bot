from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import logging
import os
import re
from typing import Any
from urllib.parse import quote

import aiohttp

from texts.status import format_status
from utils.constants import (
    DELIVERY_STATUS_ACCEPTED,
    DELIVERY_STATUS_CANCELLED,
    DELIVERY_STATUS_DELIVERED,
    DELIVERY_STATUS_NEW,
    DELIVERY_STATUS_ON_DELIVERY,
    LANG_TJ,
    STATUS_ARRIVED_DESTINATION,
    STATUS_RECEIVED,
)


logger = logging.getLogger(__name__)

AIRTABLE_API_URL = "https://api.airtable.com/v0"
REQUEST_TIMEOUT_SECONDS = 15

DEFAULT_USERS_TABLE = "Истифодабарандагон"
DEFAULT_PARCELS_TABLE = "Борҳо"
DEFAULT_DELIVERY_TABLE = "Дархостҳои доставка"
DEFAULT_DAILY_STATS_TABLE = "Статистикаи рӯзона"

DELIVERY_STATUS_LABELS_TJ = {
    DELIVERY_STATUS_NEW: "Дархости нав",
    DELIVERY_STATUS_ACCEPTED: "Қабул шуд",
    DELIVERY_STATUS_ON_DELIVERY: "Дар роҳ",
    DELIVERY_STATUS_DELIVERED: "Расонида шуд",
    DELIVERY_STATUS_CANCELLED: "Бекор шуд",
}


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _table_name(env_name: str, default: str) -> str:
    return _env(env_name, default) or default


def _get_config() -> tuple[str, str] | None:
    api_key = _env("AIRTABLE_API_KEY")
    base_id = _env("AIRTABLE_BASE_ID")
    if not api_key or not base_id:
        logger.warning(
            "Airtable sync skipped: AIRTABLE_API_KEY or AIRTABLE_BASE_ID is missing",
        )
        return None
    return api_key, base_id


def _read(source: Any, *names: str) -> Any:
    if source is None:
        return None

    for name in names:
        if isinstance(source, dict):
            if name in source:
                return source[name]
            continue

        if hasattr(source, name):
            return getattr(source, name)

    return None


def _serialize(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _as_airtable_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        return value.split("T", 1)[0]
    return str(value)


def _clean_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {
        name: serialized
        for name, value in fields.items()
        if value is not None and (serialized := _serialize(value)) is not None
    }


def _parcel_status(parcel: Any) -> str | None:
    status_code = _read(parcel, "status_code", "status")
    if status_code is None:
        return None

    city = _read(parcel, "destination_city", "city") or ""
    try:
        return format_status(str(status_code), str(city), LANG_TJ)
    except Exception:
        return str(status_code)


def _parcel_arrival_date(parcel: Any) -> Any:
    explicit_value = _read(parcel, "arrived_at", "arrival_date", "arrival_notified_at")
    if explicit_value is not None:
        return explicit_value

    status_code = _read(parcel, "status_code", "status")
    if status_code in {STATUS_ARRIVED_DESTINATION, STATUS_RECEIVED}:
        return _read(parcel, "updated_at")

    return None


def _delivery_status(delivery_request: Any) -> str | None:
    status = _read(delivery_request, "status")
    if status is None:
        return None
    return DELIVERY_STATUS_LABELS_TJ.get(status, str(status))


def _airtable_formula_string(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("'", "\\'")


def _invalid_column_from_airtable_error(
    body: str,
    fields: dict[str, Any] | None = None,
) -> str | None:
    match = re.search(r'Field \\"(.+?)\\" cannot accept the provided value', body)
    if match is not None:
        return match.group(1)

    if fields is None:
        return None

    for start_marker in ('new select option \\"', 'new select option "'):
        start = body.find(start_marker)
        if start == -1:
            continue

        rest = body[start + len(start_marker):]
        end_marker = '\\"' if start_marker.endswith('\\"') else '"'
        end = rest.find(end_marker)
        if end == -1:
            continue

        invalid_option = rest[:end]
        for name, value in fields.items():
            if str(value) == invalid_option:
                return name

    return None


async def _find_record_id(
    session: aiohttp.ClientSession,
    *,
    url: str,
    headers: dict[str, str],
    table_name: str,
    lookup_field: str,
    lookup_value: Any,
) -> str | None:
    formula = f"{{{lookup_field}}} = '{_airtable_formula_string(lookup_value)}'"
    params = {
        "filterByFormula": formula,
        "maxRecords": "1",
    }

    async with session.get(url, headers=headers, params=params) as response:
        if response.status >= 400:
            body = await response.text()
            raise RuntimeError(
                f"Airtable lookup failed for {table_name}: "
                f"HTTP {response.status} {body[:500]}",
            )

        data = await response.json()
        records = data.get("records") or []
        if not records:
            return None

        record_id = records[0].get("id")
        return str(record_id) if record_id else None


async def _create_record(
    session: aiohttp.ClientSession,
    *,
    url: str,
    headers: dict[str, str],
    table_name: str,
    fields: dict[str, Any],
) -> None:
    async with session.post(url, headers=headers, json={"fields": fields}) as response:
        if response.status >= 400:
            body = await response.text()
            raise RuntimeError(
                f"Airtable create failed for {table_name}: "
                f"HTTP {response.status} {body[:500]}",
            )

        await response.json()


async def _update_record(
    session: aiohttp.ClientSession,
    *,
    url: str,
    headers: dict[str, str],
    table_name: str,
    record_id: str,
    fields: dict[str, Any],
) -> None:
    record_url = f"{url}/{quote(record_id, safe='')}"
    async with session.patch(
        record_url,
        headers=headers,
        json={"fields": fields},
    ) as response:
        if response.status >= 400:
            body = await response.text()
            raise RuntimeError(
                f"Airtable update failed for {table_name}: "
                f"HTTP {response.status} {body[:500]}",
            )

        await response.json()


async def _post_record(
    table_name: str,
    fields: dict[str, Any],
    *,
    lookup_field: str | None = None,
    lookup_value: Any = None,
) -> bool:
    config = _get_config()
    if config is None:
        return False

    clean_fields = _clean_fields(fields)
    if not clean_fields:
        logger.warning("Airtable sync skipped for %s: no fields to send", table_name)
        return False

    api_key, base_id = config
    url = f"{AIRTABLE_API_URL}/{quote(base_id, safe='')}/{quote(table_name, safe='')}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        record_id = None
        if lookup_field and lookup_value is not None and str(lookup_value).strip():
            record_id = await _find_record_id(
                session,
                url=url,
                headers=headers,
                table_name=table_name,
                lookup_field=lookup_field,
                lookup_value=lookup_value,
            )

        while clean_fields:
            try:
                if record_id is None:
                    await _create_record(
                        session,
                        url=url,
                        headers=headers,
                        table_name=table_name,
                        fields=clean_fields,
                    )
                    logger.info("Airtable record created in table %s", table_name)
                else:
                    await _update_record(
                        session,
                        url=url,
                        headers=headers,
                        table_name=table_name,
                        record_id=record_id,
                        fields=clean_fields,
                    )
                    logger.info(
                        "Airtable record updated in table %s: %s",
                        table_name,
                        record_id,
                    )
                return True
            except RuntimeError as error:
                invalid_column = _invalid_column_from_airtable_error(
                    str(error),
                    clean_fields,
                )
                if invalid_column is None or invalid_column not in clean_fields:
                    raise

                clean_fields.pop(invalid_column, None)
                logger.warning(
                    "Airtable field skipped for %s: %s cannot accept the value",
                    table_name,
                    invalid_column,
                )

        logger.warning("Airtable sync skipped for %s: no valid fields left", table_name)
        return False


async def sync_user_to_airtable(user: Any) -> bool:
    try:
        client_code = _read(user, "client_code")
        return await _post_record(
            _table_name("AIRTABLE_USERS_TABLE", DEFAULT_USERS_TABLE),
            {
                "Коди мизоҷӣ": _read(user, "client_code"),
                "Ном ва насаб": _read(user, "full_name", "name"),
                "Телефон": _read(user, "phone"),
                "Username Telegram": _read(user, "username", "telegram_username"),
                "Telegram ID": _as_text(_read(user, "telegram_id")),
                "Забон": _read(user, "language", "lang"),
                "Шаҳр": _read(user, "city"),
                "Ҳолат": _read(user, "status"),
                "Санаи сабти ном": _as_airtable_date(
                    _read(user, "created_at", "registered_at"),
                ),
            },
            lookup_field="Коди мизоҷӣ",
            lookup_value=client_code,
        )
    except Exception:
        logger.exception("Airtable user sync failed")
        return False


async def sync_parcel_to_airtable(parcel: Any, user: Any = None) -> bool:
    try:
        parcel_user = user or _read(parcel, "user")
        track_code = _read(parcel, "track_code")
        return await _post_record(
            _table_name("AIRTABLE_PARCELS_TABLE", DEFAULT_PARCELS_TABLE),
            {
                "Трек-код": track_code,
                "Коди мизоҷӣ": _read(parcel, "client_code") or _read(parcel_user, "client_code"),
                "Мизоҷ": _read(parcel_user, "full_name", "name"),
                "Телефон": _read(parcel_user, "phone"),
                "Склад / Шаҳр": _read(parcel, "destination_city") or _read(parcel_user, "city"),
                "Статус": _parcel_status(parcel),
                "Санаи қабул дар Чин": _as_airtable_date(
                    _read(parcel, "received_china_at"),
                ),
                "Санаи расидан": _as_airtable_date(_parcel_arrival_date(parcel)),
                "Вазн": _read(parcel, "weight", "weight_kg"),
                "Нарх": _read(parcel, "price", "amount", "cost"),
                "Эзоҳ": _read(parcel, "note", "comment", "description"),
            },
            lookup_field="Трек-код",
            lookup_value=track_code,
        )
    except Exception:
        logger.exception("Airtable parcel sync failed")
        return False


async def sync_delivery_to_airtable(
    delivery_request: Any,
    parcel: Any = None,
    user: Any = None,
) -> bool:
    try:
        request_parcel = parcel or _read(delivery_request, "parcel")
        request_user = user or _read(delivery_request, "user")
        track_code = _read(delivery_request, "track_code") or _read(
            request_parcel,
            "track_code",
        )
        return await _post_record(
            _table_name("AIRTABLE_DELIVERY_TABLE", DEFAULT_DELIVERY_TABLE),
            {
                "Трек-код": track_code,
                "Коди мизоҷӣ": (
                    _read(request_user, "client_code")
                    or _read(request_parcel, "client_code")
                ),
                "Мизоҷ": _read(request_user, "full_name", "name"),
                "Телефон": (
                    _read(delivery_request, "delivery_phone", "phone")
                    or _read(request_user, "phone")
                ),
                "Склад / Шаҳр": (
                    _read(delivery_request, "destination_city")
                    or _read(request_parcel, "destination_city")
                    or _read(request_user, "city")
                ),
                "Адреси доставка": _read(delivery_request, "delivery_address", "address"),
                "Ҳолати доставка": _delivery_status(delivery_request),
                "Санаи дархост": _as_airtable_date(
                    _read(delivery_request, "created_at", "requested_at"),
                ),
                "Эзоҳ": _read(delivery_request, "note", "comment", "description"),
            },
            lookup_field="Трек-код",
            lookup_value=track_code,
        )
    except Exception:
        logger.exception("Airtable delivery sync failed")
        return False


async def sync_daily_stats_to_airtable(stats: Any) -> bool:
    try:
        stats_date = _as_airtable_date(_read(stats, "Сана", "date", "stats_date", "day"))
        return await _post_record(
            _table_name("AIRTABLE_DAILY_STATS_TABLE", DEFAULT_DAILY_STATS_TABLE),
            {
                "Сана": stats_date,
                "Мизоҷони нав": _read(
                    stats,
                    "Мизоҷони нав",
                    "new_users",
                    "new_clients",
                    "new_customers",
                ),
                "Борҳои қабулшуда": _read(
                    stats,
                    "Борҳои қабулшуда",
                    "accepted_parcels",
                    "received_parcels",
                    "china_received_parcels",
                    "parcels_received",
                ),
                "Борҳои дар роҳ": _read(
                    stats,
                    "Борҳои дар роҳ",
                    "on_the_way_parcels",
                    "parcels_on_the_way",
                ),
                "Борҳои расида": _read(
                    stats,
                    "Борҳои расида",
                    "arrived_parcels",
                    "parcels_arrived",
                ),
                "Дархостҳои доставка": _read(
                    stats,
                    "Дархостҳои доставка",
                    "delivery_requests",
                    "delivery_request_count",
                ),
                "Доставка расонида шуд": _read(
                    stats,
                    "Доставка расонида шуд",
                    "delivered_delivery",
                    "delivered_delivery_requests",
                    "delivery_delivered",
                ),
                "Даромади тахминӣ": _read(
                    stats,
                    "Даромади тахминӣ",
                    "estimated_income",
                    "estimated_revenue",
                ),
                "Эзоҳ": _read(stats, "Эзоҳ", "note", "comment", "description"),
            },
            lookup_field="Сана",
            lookup_value=stats_date,
        )
    except Exception:
        logger.exception("Airtable daily stats sync failed")
        return False
