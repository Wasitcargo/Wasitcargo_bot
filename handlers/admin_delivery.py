import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from keyboards.builders import build_inline_keyboard
from keyboards.reply import ADMIN_MENU
from services.airtable import sync_parcel_to_airtable
from services.delivery import (
    delivery_status_keyboard,
    format_delivery_request_for_admin,
    get_delivery_request,
    get_delivery_requests,
    update_delivery_status,
)
from utils.constants import (
    DELIVERY_STATUSES,
)
from utils.validators import is_admin


router = Router(name="admin_delivery")
logger = logging.getLogger(__name__)


ADMIN_DELIVERY_LABEL = ADMIN_MENU[3][1]


def _is_admin_message(message: Message) -> bool:
    return message.from_user is not None and is_admin(message.from_user.id)


def _is_admin_callback(callback: CallbackQuery) -> bool:
    return is_admin(callback.from_user.id)


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

    parcel_for_airtable_sync = None

    if (
        request is not None
        and request.user is not None
        and request.user.telegram_id
        and before_request is not None
        and before_request.status != status
    ):
        bot = callback.message.bot if callback.message is not None else callback.bot

        if status == "on_delivery":
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

        elif status == "delivered":
            if request.user.language == "ru":
                text = (
                    "✅ <b>Товар получен</b>\n\n"
                    "<blockquote>"
                    "Вы получили товар через доставку.\n"
                    "🤝 Спасибо за доверие к Wasit Cargo!"
                    "</blockquote>"
                )
            else:
                text = (
                    "✅ <b>Товар қабул шуд</b>\n\n"
                    "<blockquote>"
                    "Шумо товарро тавассути доставка қабул кардед.\n"
                    "🤝 Ташаккур барои боварӣ ба Wasit Cargo!"
                    "</blockquote>"
                )
            image_key = "delivery_delivered_image_file_id"

            try:
                from services.parcels import update_parcel_status
                from utils.constants import STATUS_RECEIVED
                parcel_for_airtable_sync = await update_parcel_status(
                    request.parcel_id,
                    STATUS_RECEIVED,
                )
            except Exception:
                pass
        else:
            text = None
            image_key = None

        if text:
            try:
                if status == "delivered":
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
                pass
    if callback.message is not None and request is not None:
        await callback.message.edit_text(
            format_delivery_request_for_admin(request, request.user),
            reply_markup=delivery_status_keyboard(request.id),
        )
    await callback.answer("Статус нав шуд.")

    if parcel_for_airtable_sync is not None:
        try:
            await sync_parcel_to_airtable(
                parcel_for_airtable_sync,
                parcel_for_airtable_sync.user,
            )
        except Exception:
            logger.exception("Airtable parcel sync failed")
