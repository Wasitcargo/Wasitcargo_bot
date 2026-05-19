import logging
import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from keyboards.builders import build_inline_keyboard
from services.airtable import sync_delivery_to_airtable
from services.delivery import (
    create_delivery_request,
    get_arrived_parcel_for_user_by_id,
    get_arrived_parcel_for_user_by_track_code,
    get_latest_arrived_parcel_for_user,
    notify_admins_about_delivery_request,
)
from services.settings import get_bool_setting, get_setting
from services.users import get_user_by_telegram_id
from states.delivery_states import DeliveryStates
from texts import ru, tj
from utils.constants import LANG_RU, LANG_TJ


router = Router(name="delivery")
logger = logging.getLogger(__name__)


TEXTS = {
    LANG_TJ: tj,
    LANG_RU: ru,
}


def _texts(lang: str):
    return TEXTS.get(lang, tj)


def _confirm_keyboard(lang: str):
    if lang == LANG_RU:
        rows = (
            (("Отправить", "delivery:confirm"),),
            (("Заново", "delivery:restart"),),
            (("Отменить", "delivery:cancel"),),
        )
    else:
        rows = (
            (("Фиристодан", "delivery:confirm"),),
            (("Аз нав", "delivery:restart"),),
            (("Бекор кардан", "delivery:cancel"),),
        )
    return build_inline_keyboard(
        rows,
    )


def _extract_track_code(text: str | None) -> str | None:
    if not text:
        return None

    match = re.search(r"Трек-код:\s*(.+)", text)
    if match is None:
        return None
    return match.group(1).strip()


def _format_terms(lang: str, inside_city: str, outside_city: str) -> str:
    return _texts(lang).DELIVERY_TERMS.format(
        inside_city=inside_city,
        outside_city=outside_city,
    )


def _format_confirmation(data: dict, lang: str) -> str:
    return _texts(lang).DELIVERY_CONFIRM.format(
        track_code=data["track_code"],
        full_name=data["full_name"],
        phone=data["phone"],
        destination_city=data["destination_city"],
        delivery_address=data["delivery_address"],
    )


def _parcel_id_from_delivery_callback(data: str | None) -> int | None:
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 3:
        return None
    try:
        return int(parts[2])
    except ValueError:
        return None


@router.callback_query(F.data == "delivery:request")
@router.callback_query(F.data.startswith("delivery:request:"))
async def start_delivery_request(callback: CallbackQuery, state: FSMContext) -> None:
    user = await get_user_by_telegram_id(callback.from_user.id)
    if user is None:
        await callback.answer()
        return

    texts = _texts(user.language)
    delivery_enabled = await get_bool_setting("delivery_enabled", False)
    if not delivery_enabled:
        if callback.message is not None:
            await callback.message.answer(texts.DELIVERY_UNAVAILABLE)
        await callback.answer()
        return

    parcel = None
    parcel_id = _parcel_id_from_delivery_callback(callback.data)
    if parcel_id is not None:
        parcel = await get_arrived_parcel_for_user_by_id(
            user_id=user.id,
            parcel_id=parcel_id,
        )

    track_code = _extract_track_code(callback.message.text if callback.message else None)
    if parcel is None and track_code is not None:
        parcel = await get_arrived_parcel_for_user_by_track_code(
            user_id=user.id,
            track_code=track_code,
        )
    if parcel is None:
        parcel = await get_latest_arrived_parcel_for_user(user.id)

    if parcel is None:
        if callback.message is not None:
            await callback.message.answer(texts.DELIVERY_NOT_FOUND)
        await callback.answer()
        return

    lang_suffix = "ru" if user.language == LANG_RU else "tj"
    inside_city = await get_setting(
        f"delivery_inside_city_{lang_suffix}",
        "По городу: 15 сомонӣ" if user.language == LANG_RU else "Дохили шаҳр: 15 сомонӣ",
    )
    outside_city = await get_setting(
        f"delivery_outside_city_{lang_suffix}",
        (
            "За город: отправляем на такси, стоимость такси оплачивается отдельно."
            if user.language == LANG_RU
            else "Берун аз шаҳр: ба таксӣ равон мекунем, ҳаққи таксӣ алоҳида ҳисоб мешавад."
        ),
    )

    await state.set_state(DeliveryStates.waiting_for_address)
    await state.update_data(
        parcel_id=parcel.id,
        track_code=parcel.track_code,
        full_name=user.full_name,
        phone=user.phone,
        destination_city=parcel.destination_city,
    )
    if callback.message is not None:
        await callback.message.answer(
            _format_terms(user.language, inside_city or "", outside_city or ""),
        )
    await callback.answer()


@router.message(DeliveryStates.waiting_for_address, F.text)
async def delivery_address_received(message: Message, state: FSMContext) -> None:
    user = await get_user_by_telegram_id(message.from_user.id) if message.from_user else None
    lang = user.language if user is not None else LANG_TJ
    address = message.text.strip()
    if not address:
        await message.answer(_texts(lang).DELIVERY_SEND_ADDRESS)
        return

    await state.update_data(delivery_address=address)
    await state.set_state(DeliveryStates.confirming)
    data = await state.get_data()
    await message.answer(
        _format_confirmation(data, lang),
        reply_markup=_confirm_keyboard(lang),
    )


@router.message(DeliveryStates.waiting_for_address)
async def delivery_address_invalid(message: Message) -> None:
    user = await get_user_by_telegram_id(message.from_user.id) if message.from_user else None
    lang = user.language if user is not None else LANG_TJ
    await message.answer(_texts(lang).DELIVERY_SEND_ADDRESS)


@router.callback_query(DeliveryStates.confirming, F.data == "delivery:cancel")
async def cancel_delivery(callback: CallbackQuery, state: FSMContext) -> None:
    user = await get_user_by_telegram_id(callback.from_user.id)
    lang = user.language if user is not None else LANG_TJ
    await state.clear()
    if callback.message is not None:
        await callback.message.edit_text(_texts(lang).DELIVERY_CANCELLED)
    await callback.answer()


@router.callback_query(DeliveryStates.confirming, F.data == "delivery:restart")
async def restart_delivery_address(callback: CallbackQuery, state: FSMContext) -> None:
    user = await get_user_by_telegram_id(callback.from_user.id)
    lang = user.language if user is not None else LANG_TJ
    await state.set_state(DeliveryStates.waiting_for_address)
    await state.update_data(delivery_address=None)
    if callback.message is not None:
        await callback.message.edit_text(_texts(lang).DELIVERY_SEND_ADDRESS)
    await callback.answer()


@router.callback_query(DeliveryStates.confirming, F.data == "delivery:confirm")
async def confirm_delivery(callback: CallbackQuery, state: FSMContext) -> None:
    user = await get_user_by_telegram_id(callback.from_user.id)
    if user is None:
        await state.clear()
        await callback.answer()
        return

    data = await state.get_data()
    parcel = await get_arrived_parcel_for_user_by_id(
        user_id=user.id,
        parcel_id=data["parcel_id"],
    )
    if parcel is None:
        parcel = await get_arrived_parcel_for_user_by_track_code(
            user_id=user.id,
            track_code=data["track_code"],
        )
    if parcel is None:
        await state.clear()
        if callback.message is not None:
            await callback.message.edit_text(_texts(user.language).DELIVERY_NOT_FOUND)
        await callback.answer()
        return

    request = await create_delivery_request(
        parcel=parcel,
        user=user,
        delivery_address=data["delivery_address"],
    )
    bot = callback.message.bot if callback.message is not None else callback.bot
    await notify_admins_about_delivery_request(bot, request, user)
    await state.clear()
    if callback.message is not None:
        await callback.message.edit_text(_texts(user.language).DELIVERY_REQUEST_ACCEPTED)
    await callback.answer()
    try:
        await sync_delivery_to_airtable(request, parcel, user)
    except Exception:
        logger.exception("Airtable delivery sync failed")
