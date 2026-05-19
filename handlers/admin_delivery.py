import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from keyboards.builders import build_inline_keyboard
from keyboards.reply import ADMIN_MENU
from services.airtable import sync_delivery_to_airtable, sync_parcel_to_airtable
from services.delivery import (
    delivery_status_keyboard,
    format_delivery_request_for_admin,
    get_delivery_request,
    get_delivery_requests,
    update_delivery_status,
)
from services.parcels import update_parcel_status
from services.settings import get_setting
from services.status_media import get_status_image_file_id
from utils.constants import (
    DELIVERY_STATUSES,
    DELIVERY_STATUS_DELIVERED,
    DELIVERY_STATUS_ON_DELIVERY,
    STATUS_RECEIVED,
)
from utils.validators import is_admin


router = Router(name="admin_delivery")
logger = logging.getLogger(__name__)


ADMIN_DELIVERY_LABEL = ADMIN_MENU[3][1]


def _is_admin_message(message: Message) -> bool:
    return message.from_user is not None and is_admin(message.from_user.id)


def _is_admin_callback(callback: CallbackQuery) -> bool:
    return is_admin(callback.from_user.id)


async def _safe_edit_text(message: Message, text: str, **kwargs) -> None:
    try:
        await message.edit_text(text, **kwargs)
    except TelegramBadRequest as error:
        if "message is not modified" in str(error).lower():
            return
        raise


def _delivery_received_text(language: str) -> str:
    if language == "ru":
        return (
            "✅ <b>Ваш товар получен</b>\n\n"
            "<blockquote>"
            "Статус груза изменён на «Получено».\n"
            "Спасибо, что воспользовались услугами Wasit Cargo!"
            "</blockquote>"
        )

    return (
        "✅ <b>Бори шумо супорида шуд</b>\n\n"
        "<blockquote>"
        "Статуси бор ба «Супорида шуд» иваз шуд.\n"
        "Ташаккур, ки аз хизматрасонии Wasit Cargo истифода бурдед!"
        "</blockquote>"
    )


def _requests_keyboard(requests):
    rows = tuple(
        (
            (
                f"ID {request.id} · {request.track_code}",
                f"admin_delivery:view:{request.id}",
            ),
        )
        for request in requests
    )
    return build_inline_keyboard(rows)


@router.message(F.text == ADMIN_DELIVERY_LABEL)
async def show_delivery_requests(message: Message) -> None:
    if not _is_admin_message(message):
        return

    requests = await get_delivery_requests()
    if not requests:
        await message.answer("Дархости доставка нест.")
        return

    await message.answer(
        "Дархостҳои доставка:",
        reply_markup=_requests_keyboard(requests),
    )


@router.callback_query(F.data.startswith("admin_delivery:view:"))
async def view_delivery_request(callback: CallbackQuery) -> None:
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    try:
        request_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer()
        return

    request = await get_delivery_request(request_id)
    if request is None:
        if callback.message is not None:
            await callback.message.edit_text("Дархост ёфт нашуд.")
        await callback.answer()
        return

    if callback.message is not None:
        await callback.message.edit_text(
            format_delivery_request_for_admin(request, request.user),
            reply_markup=delivery_status_keyboard(request.id),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("admin_delivery:set:"))
async def set_delivery_status(callback: CallbackQuery) -> None:
    if not _is_admin_callback(callback):
        await callback.answer()
        return

    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer()
        return

    try:
        request_id = int(parts[2])
    except ValueError:
        await callback.answer()
        return

    status = parts[3]
    if status not in DELIVERY_STATUSES:
        await callback.answer()
        return

    before_request = await get_delivery_request(request_id)

    request = await update_delivery_status(
        request_id=request_id,
        status=status,
        admin_id=callback.from_user.id,
    )
    if request is None:
        if callback.message is not None:
            await callback.message.edit_text("Дархост ёфт нашуд.")
        await callback.answer()
        return

    request = await get_delivery_request(request_id)

    status_changed = before_request is not None and before_request.status != status
    parcel_for_airtable_sync = None

    if request is not None and status == DELIVERY_STATUS_DELIVERED and status_changed:
        try:
            parcel_for_airtable_sync = await update_parcel_status(
                request.parcel_id,
                STATUS_RECEIVED,
            )
        except Exception:
            logger.exception("Failed to mark parcel as received after delivery")

    if (
        request is not None
        and request.user is not None
        and request.user.telegram_id
        and status_changed
    ):
        bot = callback.message.bot if callback.message is not None else callback.bot

        if status == DELIVERY_STATUS_ON_DELIVERY:
            if request.user.language == "ru":
                text = (
                    "🚚 <b>Доставка в пути</b>\n\n"
                    "<blockquote>"
                    "Курьер уже выехал.\n"
                    "Пожалуйста, ожидайте звонка."
                    "</blockquote>"
                )
            else:
                text = (
                    "🚚 <b>Доставка ба роҳ баромад</b>\n\n"
                    "<blockquote>"
                    "Доставчик ба роҳ баромад.\n"
                    "Лутфан зангро интизор шавед."
                    "</blockquote>"
                )
            image_key = "delivery_on_the_way_image_file_id"

        elif status == DELIVERY_STATUS_DELIVERED:
            text = _delivery_received_text(request.user.language)
            image_key = None
        else:
            text = None
            image_key = None

        if text:
            try:
                if status == DELIVERY_STATUS_DELIVERED:
                    image_id = await get_status_image_file_id(STATUS_RECEIVED)
                else:
                    image_id = await get_setting(image_key, "") if image_key else ""

                if image_id:
                    await bot.send_photo(
                        chat_id=request.user.telegram_id,
                        photo=image_id,
                        caption=text,
                    )
                else:
                    await bot.send_message(
                        chat_id=request.user.telegram_id,
                        text=text,
                    )
            except Exception:
                logger.exception("Failed to notify user about delivery status")
    if callback.message is not None and request is not None:
        await _safe_edit_text(
            callback.message,
            format_delivery_request_for_admin(request, request.user),
            reply_markup=delivery_status_keyboard(request.id),
        )
    await callback.answer("Статус нав шуд.")

    if request is not None:
        try:
            await sync_delivery_to_airtable(request, request.parcel, request.user)
        except Exception:
            logger.exception("Airtable delivery sync failed")

    if parcel_for_airtable_sync is not None:
        try:
            await sync_parcel_to_airtable(
                parcel_for_airtable_sync,
                parcel_for_airtable_sync.user,
            )
        except Exception:
            logger.exception("Airtable parcel sync failed")
