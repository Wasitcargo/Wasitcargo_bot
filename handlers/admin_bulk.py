from datetime import date, datetime
import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from keyboards.builders import build_inline_keyboard
from keyboards.reply import ADMIN_MENU, admin_main_menu
from services.airtable import sync_parcel_to_airtable
from services.notifications import notify_arrival_destination
from services.parcels import (
    bulk_update_parcel_status,
    count_parcels_for_bulk_status_update,
    mark_arrival_notified,
)
from states.admin_bulk_states import AdminBulkStatusStates
from texts.status import format_status
from utils.constants import (
    CITY_BOKHTAR,
    CITY_DUSHANBE,
    CITY_ISTARAVSHAN,
    CITY_KHUJAND,
    CITY_KULOB,
    CITY_NAMES,
    LANG_TJ,
    STATUS_ARRIVED_DESTINATION,
    STATUS_CHINA_RECEIVED,
    STATUS_CODES,
    STATUS_ON_THE_WAY,
    STATUS_RECEIVED,
)
from utils.validators import is_admin


router = Router(name="admin_bulk")
logger = logging.getLogger(__name__)


ADMIN_BULK_LABEL = ADMIN_MENU[1][1]
CITY_ROWS = (
    (CITY_ISTARAVSHAN, CITY_DUSHANBE),
    (CITY_KHUJAND, CITY_BOKHTAR),
    (CITY_KULOB,),
)


def _is_admin_message(message: Message) -> bool:
    return message.from_user is not None and is_admin(message.from_user.id)


def _is_admin_callback(callback: CallbackQuery) -> bool:
    return is_admin(callback.from_user.id)


def _city_name(city_key: str) -> str:
    return CITY_NAMES[city_key][LANG_TJ]


def _city_keyboard():
    return build_inline_keyboard(
        tuple(
            tuple(
                (
                    _city_name(city_key),
                    f"admin_bulk:city:{city_key}",
                )
                for city_key in row
            )
            for row in CITY_ROWS
        ),
    )


def _status_label(status_code: str, city: str) -> str:
    return format_status(status_code, city, LANG_TJ)


def _status_keyboard(prefix: str, city: str):
    return build_inline_keyboard(
        (
            (
                (
                    _status_label(STATUS_CHINA_RECEIVED, city),
                    f"admin_bulk:{prefix}:{STATUS_CHINA_RECEIVED}",
                ),
            ),
            (
                (
                    _status_label(STATUS_ON_THE_WAY, city),
                    f"admin_bulk:{prefix}:{STATUS_ON_THE_WAY}",
                ),
            ),
            (
                (
                    _status_label(STATUS_ARRIVED_DESTINATION, city),
                    f"admin_bulk:{prefix}:{STATUS_ARRIVED_DESTINATION}",
                ),
            ),
        ),
    )


def _confirm_keyboard():
    return build_inline_keyboard(
        (
            (("Тасдиқ", "admin_bulk:confirm"),),
            (("Бекор кардан", "admin_bulk:cancel"),),
        ),
    )


def _parse_batch_date(value: str):
    try:
        return datetime.strptime(value.strip(), "%d.%m.%Y").date()
    except ValueError:
        return None


def _format_confirmation(data: dict, count: int) -> str:
    return (
        f"Шумо {count} борро барои шаҳри {data['destination_city']} "
        f"ва санаи {data['batch_date_text']} аз статуси "
        f"{_status_label(data['old_status'], data['destination_city'])} ба "
        f"{_status_label(data['new_status'], data['destination_city'])}."
    )


@router.message(F.text == ADMIN_BULK_LABEL)
async def start_bulk_status(message: Message, state: FSMContext) -> None:
    if not _is_admin_message(message):
        return

    await state.clear()
    await state.set_state(AdminBulkStatusStates.choosing_city)
    await message.answer(
        "Шаҳр / складро интихоб кунед.",
        reply_markup=_city_keyboard(),
    )


@router.callback_query(
    AdminBulkStatusStates.choosing_city,
    F.data.startswith("admin_bulk:city:"),
)
async def choose_bulk_city(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    city_key = callback.data.rsplit(":", 1)[1]
    if city_key not in CITY_NAMES:
        await callback.answer()
        return

    await state.update_data(
        city_key=city_key,
        destination_city=_city_name(city_key),
    )
    await state.set_state(AdminBulkStatusStates.waiting_for_batch_date)
    if callback.message is not None:
        await callback.message.edit_text(
            "Санаи партияро дар формат DD.MM.YYYY ворид кунед.",
        )
    await callback.answer()


@router.message(AdminBulkStatusStates.waiting_for_batch_date, F.text)
async def enter_bulk_batch_date(message: Message, state: FSMContext) -> None:
    if not _is_admin_message(message):
        return

    batch_date = _parse_batch_date(message.text)
    if batch_date is None:
        await message.answer("Сана нодуруст аст. Формат: DD.MM.YYYY")
        return

    data = await state.get_data()
    await state.update_data(
        batch_date=batch_date.isoformat(),
        batch_date_text=message.text.strip(),
    )
    await state.set_state(AdminBulkStatusStates.choosing_old_status)
    await message.answer(
        "Статуси ҳозираро интихоб кунед.",
        reply_markup=_status_keyboard("old_status", data["destination_city"]),
    )


@router.callback_query(
    AdminBulkStatusStates.choosing_old_status,
    F.data.startswith("admin_bulk:old_status:"),
)
async def choose_old_status(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    old_status = callback.data.rsplit(":", 1)[1]
    if old_status not in STATUS_CODES:
        await callback.answer()
        return

    data = await state.get_data()
    await state.update_data(old_status=old_status)
    await state.set_state(AdminBulkStatusStates.choosing_new_status)
    if callback.message is not None:
        await callback.message.edit_text(
            "Статуси навро интихоб кунед.",
            reply_markup=_status_keyboard("new_status", data["destination_city"]),
        )
    await callback.answer()


@router.callback_query(
    AdminBulkStatusStates.choosing_new_status,
    F.data.startswith("admin_bulk:new_status:"),
)
async def choose_new_status(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    new_status = callback.data.rsplit(":", 1)[1]
    if new_status not in STATUS_CODES:
        await callback.answer()
        return

    await state.update_data(new_status=new_status)
    data = await state.get_data()
    batch_date = date.fromisoformat(data["batch_date"])
    count = await count_parcels_for_bulk_status_update(
        destination_city=data["destination_city"],
        batch_date=batch_date,
        old_status=data["old_status"],
    )
    await state.update_data(matching_count=count)
    await state.set_state(AdminBulkStatusStates.confirming)

    if callback.message is not None:
        await callback.message.edit_text(
            _format_confirmation(data, count),
            reply_markup=_confirm_keyboard(),
        )
    await callback.answer()


@router.callback_query(AdminBulkStatusStates.confirming, F.data == "admin_bulk:cancel")
async def cancel_bulk_status(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    await state.clear()
    if callback.message is not None:
        await callback.message.edit_text("Амалиёт бекор карда шуд.")
        await callback.message.answer("Панели админ", reply_markup=admin_main_menu())
    await callback.answer()


@router.callback_query(AdminBulkStatusStates.confirming, F.data == "admin_bulk:confirm")
async def confirm_bulk_status(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    data = await state.get_data()
    batch_date = date.fromisoformat(data["batch_date"])
    parcels = await bulk_update_parcel_status(
        destination_city=data["destination_city"],
        batch_date=batch_date,
        old_status=data["old_status"],
        new_status=data["new_status"],
    )

    notifications_sent = 0
    notifications_failed = 0
    if data["new_status"] == STATUS_ARRIVED_DESTINATION:
        bot = callback.message.bot if callback.message is not None else callback.bot
        for parcel in parcels:
            if parcel.arrival_notified_at is not None:
                continue

            notified = await notify_arrival_destination(bot, parcel.user, parcel)
            if notified:
                await mark_arrival_notified(parcel.id)
                notifications_sent += 1
            else:
                notifications_failed += 1

    await state.clear()
    result = (
        "Натиҷаи ивази гурӯҳӣ\n\n"
        f"Ҳамагӣ нав шуд: {len(parcels)}\n"
        f"Хабар фиристода шуд: {notifications_sent}\n"
        f"Хабар фиристода нашуд: {notifications_failed}"
    )
    if callback.message is not None:
        await callback.message.edit_text(result)
        await callback.message.answer("Панели админ", reply_markup=admin_main_menu())
    await callback.answer()

    for parcel in parcels:
        try:
            await sync_parcel_to_airtable(parcel, parcel.user)
        except Exception:
            logger.exception("Airtable parcel sync failed")
