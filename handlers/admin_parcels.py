from datetime import datetime, time
import logging
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from keyboards.builders import build_inline_keyboard
from keyboards.reply import ADMIN_MENU, admin_main_menu
from services.airtable import sync_parcel_to_airtable
from services.normalizer import normalize_track_code
from services.notifications import notify_china_received
from services.parcels import (
    create_parcel,
    get_parcel_by_normalized_track_code,
    mark_china_received_notified,
)
from services.users import get_user_by_client_code, get_user_by_id
from states.admin_parcel_states import AdminAddParcelStates
from texts.status import format_status
from utils.constants import LANG_TJ, STATUS_CHINA_RECEIVED
from utils.validators import is_admin


router = Router(name="admin_parcels")
logger = logging.getLogger(__name__)


ADMIN_ADD_PARCEL_LABEL = ADMIN_MENU[0][0]
APP_TZ = ZoneInfo("Asia/Shanghai")


def _is_admin_message(message: Message) -> bool:
    return message.from_user is not None and is_admin(message.from_user.id)


def _is_admin_callback(callback: CallbackQuery) -> bool:
    return is_admin(callback.from_user.id)


def _date_keyboard():
    return build_inline_keyboard(
        (
            (("Имрӯз", "admin_add_parcel:today"),),
            (("Санаи дигар", "admin_add_parcel:manual_date"),),
            (("Бекор кардан", "admin_add_parcel:cancel"),),
        ),
    )


def _confirm_keyboard():
    return build_inline_keyboard(
        (
            (("Сабт кардан", "admin_add_parcel:save"),),
            (("Аз нав", "admin_add_parcel:restart"),),
            (("Бекор кардан", "admin_add_parcel:cancel"),),
        ),
    )


def _format_date(value: datetime) -> str:
    return value.strftime("%d.%m.%Y")


def _parse_manual_date(value: str) -> datetime | None:
    try:
        parsed_date = datetime.strptime(value.strip(), "%d.%m.%Y").date()
    except ValueError:
        return None
    return datetime.combine(parsed_date, time.min, tzinfo=APP_TZ)


def _format_user_found(user) -> str:
    return (
        "Мизоҷ ёфт шуд\n\n"
        f"Ном: {user.full_name}\n"
        f"Телефон: {user.phone}\n"
        f"Коди мизоҷ: {user.client_code}\n"
        f"Шаҳр / склад: {user.city}\n\n"
        "Трек-кодро ворид кунед."
    )


def _format_duplicate(parcel) -> str:
    status = format_status(parcel.status_code, parcel.destination_city, LANG_TJ)
    return (
        "Ин трек-код аллакай сабт шудааст.\n\n"
        f"Трек-код: {parcel.track_code}\n"
        f"Коди мизоҷ: {parcel.client_code}\n"
        f"Статус: {status}\n"
        f"Склад: {parcel.destination_city}\n"
        f"Санаи қабул: {_format_date(parcel.received_china_at)}"
    )


def _format_confirmation(data: dict, user, received_china_at: datetime) -> str:
    status = format_status(STATUS_CHINA_RECEIVED, user.city, LANG_TJ)
    return (
        "Маълумотро тасдиқ кунед\n\n"
        f"Ном: {user.full_name}\n"
        f"Телефон: {user.phone}\n"
        f"Коди мизоҷ: {user.client_code}\n"
        f"Шаҳр / склад: {user.city}\n\n"
        f"Трек-код: {data['track_code']}\n"
        f"Статус: {status}\n"
        f"Санаи қабул: {_format_date(received_china_at)}"
    )


async def _restart_add_parcel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AdminAddParcelStates.waiting_for_client_code)
    await message.answer("Коди мизоҷро ворид кунед. Мисол: WS1007")


@router.message(F.text == ADMIN_ADD_PARCEL_LABEL)
async def start_add_parcel(message: Message, state: FSMContext) -> None:
    if not _is_admin_message(message):
        return

    await _restart_add_parcel(message, state)


@router.message(AdminAddParcelStates.waiting_for_client_code, F.text)
async def add_parcel_client_code(message: Message, state: FSMContext) -> None:
    if not _is_admin_message(message):
        return

    client_code = message.text.strip().upper()
    user = await get_user_by_client_code(client_code)
    if user is None:
        await message.answer("Ин коди мизоҷ дар база ёфт нашуд.")
        return

    await state.update_data(user_id=user.id, client_code=user.client_code)
    await state.set_state(AdminAddParcelStates.waiting_for_track_code)
    await message.answer(_format_user_found(user))


@router.message(AdminAddParcelStates.waiting_for_track_code, F.text)
async def add_parcel_track_code(message: Message, state: FSMContext) -> None:
    if not _is_admin_message(message):
        return

    track_code = message.text.strip()
    normalized_track_code = normalize_track_code(track_code)
    if not normalized_track_code:
        await message.answer("Трек-кодро ворид кунед.")
        return

    duplicate = await get_parcel_by_normalized_track_code(normalized_track_code)
    if duplicate is not None:
        await state.clear()
        await message.answer(_format_duplicate(duplicate), reply_markup=admin_main_menu())
        return

    await state.update_data(
        track_code=track_code,
        normalized_track_code=normalized_track_code,
    )
    await state.set_state(AdminAddParcelStates.choosing_received_date)
    await message.answer(
        "Санаи қабули бор дар склади Чинро интихоб кунед.",
        reply_markup=_date_keyboard(),
    )


@router.callback_query(
    AdminAddParcelStates.choosing_received_date,
    F.data == "admin_add_parcel:today",
)
async def add_parcel_today(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    received_china_at = datetime.now(APP_TZ)
    await state.update_data(received_china_at=received_china_at.isoformat())
    await state.set_state(AdminAddParcelStates.confirming)
    await _show_confirmation(callback, state, received_china_at)


@router.callback_query(
    AdminAddParcelStates.choosing_received_date,
    F.data == "admin_add_parcel:manual_date",
)
async def add_parcel_manual_date(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    await state.set_state(AdminAddParcelStates.waiting_for_manual_date)
    if callback.message is not None:
        await callback.message.edit_text("Санаро дар формат DD.MM.YYYY ворид кунед.")
    await callback.answer()


@router.message(AdminAddParcelStates.waiting_for_manual_date, F.text)
async def add_parcel_manual_date_value(message: Message, state: FSMContext) -> None:
    if not _is_admin_message(message):
        return

    received_china_at = _parse_manual_date(message.text)
    if received_china_at is None:
        await message.answer("Сана нодуруст аст. Формат: DD.MM.YYYY")
        return

    await state.update_data(received_china_at=received_china_at.isoformat())
    await state.set_state(AdminAddParcelStates.confirming)
    data = await state.get_data()
    user = await get_user_by_id(data["user_id"])
    if user is None:
        await state.clear()
        await message.answer("Ин коди мизоҷ дар база ёфт нашуд.")
        return

    await message.answer(
        _format_confirmation(data, user, received_china_at),
        reply_markup=_confirm_keyboard(),
    )


async def _show_confirmation(
    callback: CallbackQuery,
    state: FSMContext,
    received_china_at: datetime,
) -> None:
    data = await state.get_data()
    user = await get_user_by_id(data["user_id"])
    if user is None:
        await state.clear()
        if callback.message is not None:
            await callback.message.edit_text("Ин коди мизоҷ дар база ёфт нашуд.")
        await callback.answer()
        return

    if callback.message is not None:
        await callback.message.edit_text(
            _format_confirmation(data, user, received_china_at),
            reply_markup=_confirm_keyboard(),
        )
    await callback.answer()


@router.callback_query(F.data == "admin_add_parcel:restart")
async def add_parcel_restart(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    await state.clear()
    await state.set_state(AdminAddParcelStates.waiting_for_client_code)
    if callback.message is not None:
        await callback.message.edit_text("Коди мизоҷро ворид кунед. Мисол: WS1007")
    await callback.answer()


@router.callback_query(F.data == "admin_add_parcel:cancel")
async def add_parcel_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    await state.clear()
    if callback.message is not None:
        await callback.message.edit_text("Амалиёт бекор карда шуд.")
        await callback.message.answer("Панели админ", reply_markup=admin_main_menu())
    await callback.answer()


@router.callback_query(AdminAddParcelStates.confirming, F.data == "admin_add_parcel:save")
async def add_parcel_save(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    data = await state.get_data()
    user = await get_user_by_id(data["user_id"])
    if user is None:
        await state.clear()
        if callback.message is not None:
            await callback.message.edit_text("Ин коди мизоҷ дар база ёфт нашуд.")
        await callback.answer()
        return

    duplicate = await get_parcel_by_normalized_track_code(data["normalized_track_code"])
    if duplicate is not None:
        await state.clear()
        if callback.message is not None:
            await callback.message.edit_text(_format_duplicate(duplicate))
        await callback.answer()
        return

    received_china_at = datetime.fromisoformat(data["received_china_at"])
    parcel = await create_parcel(
        track_code=data["track_code"],
        normalized_track_code=data["normalized_track_code"],
        client_code=user.client_code,
        user_id=user.id,
        destination_city=user.city,
        received_china_at=received_china_at,
        created_by_admin_id=callback.from_user.id,
    )

    bot = callback.message.bot if callback.message is not None else callback.bot
    notified = await notify_china_received(bot, user, parcel)
    if notified:
        await mark_china_received_notified(parcel.id)

    await state.clear()
    if callback.message is not None:
        await callback.message.edit_text(
            "Бор сабт шуд.\n"
            f"Трек-код: {parcel.track_code}\n"
            f"Коди мизоҷ: {parcel.client_code}"
        )
        await callback.message.answer("Панели админ", reply_markup=admin_main_menu())
    await callback.answer()
    try:
        await sync_parcel_to_airtable(parcel, user)
    except Exception:
        logger.exception("Airtable parcel sync failed")
