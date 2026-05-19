import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery, Message, ReplyKeyboardRemove

from keyboards.inline_user import auth_keyboard, cities_keyboard, pickup_cities_keyboard
from keyboards.reply import (
    auth_back_keyboard,
    auth_phone_keyboard,
    user_main_menu,
)
from services.airtable import sync_user_to_airtable
from services.normalizer import normalize_full_name
from services.settings import get_setting
from services.warehouses import get_active_tj_pickup_warehouses
from services.users import (
    attach_telegram_account,
    create_user,
    get_user_by_id,
    get_user_by_phone,
    normalize_contact_phone,
    normalize_manual_phone,
)
from states.auth_states import AuthStates
from texts import ru, tj
from utils.constants import CITY_NAMES, LANG_RU, LANG_TJ


router = Router(name="auth")
logger = logging.getLogger(__name__)


TEXTS = {
    LANG_TJ: tj,
    LANG_RU: ru,
}


def _texts(lang: str):
    return TEXTS.get(lang, tj)


BACK_LABELS = {tj.BACK, ru.BACK}


def _is_own_contact(message: Message) -> bool:
    if message.contact is None or message.from_user is None:
        return False
    return message.contact.user_id == message.from_user.id


def _city_name(city_key: str, lang: str) -> str:
    names = CITY_NAMES.get(city_key)
    if names is None:
        return city_key
    return names[LANG_TJ]


async def _show_auth_selection_from_message(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("language", LANG_TJ)
    texts = _texts(lang)

    await state.clear()
    await state.update_data(language=lang)
    await state.set_state(AuthStates.choosing_auth_action)
    await message.answer(texts.BACK, reply_markup=ReplyKeyboardRemove())
    await message.answer(
        f"{texts.REGISTER} / {texts.LOGIN}",
        reply_markup=auth_keyboard(lang),
    )


async def _show_auth_selection_from_callback(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    lang = data.get("language", LANG_TJ)
    texts = _texts(lang)

    await state.clear()
    await state.update_data(language=lang)
    await state.set_state(AuthStates.choosing_auth_action)
    if callback.message is not None:
        await callback.message.edit_text(
            f"{texts.REGISTER} / {texts.LOGIN}",
            reply_markup=auth_keyboard(lang),
        )
    await callback.answer()


def _contact_phone(message: Message) -> str | None:
    if message.contact is None:
        return None
    return normalize_contact_phone(message.contact.phone_number)


def _manual_phone(message: Message) -> str | None:
    if message.text is None:
        return None
    return normalize_manual_phone(message.text)


@router.callback_query(
    AuthStates.choosing_language,
    F.data.in_({"lang:tj", "lang:ru"}),
)
async def choose_language(callback: CallbackQuery, state: FSMContext) -> None:
    lang = callback.data.split(":", 1)[1]
    texts = _texts(lang)

    await state.update_data(language=lang)
    await state.set_state(AuthStates.choosing_auth_action)

    try:
        await callback.message.delete()
    except Exception:
        pass

    await callback.message.answer(
        f"{texts.REGISTER} / {texts.LOGIN}",
        reply_markup=auth_keyboard(lang),
    )
    await callback.answer()


@router.callback_query(
    AuthStates.choosing_auth_action,
    F.data == "auth:register",
)
async def start_registration(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("language", LANG_TJ)
    texts = _texts(lang)

    await state.set_state(AuthStates.register_full_name)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(texts.ASK_FULL_NAME, reply_markup=auth_back_keyboard(lang))
    await callback.answer()


@router.callback_query(AuthStates.choosing_auth_action, F.data == "auth:login")
async def start_login(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("language", LANG_TJ)
    texts = _texts(lang)

    await state.set_state(AuthStates.login_phone)
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        texts.ASK_PHONE,
        reply_markup=auth_phone_keyboard(lang),
    )
    await callback.answer()


@router.message(
    AuthStates.register_full_name,
    F.text.in_(BACK_LABELS),
)
@router.message(
    AuthStates.register_phone,
    F.text.in_(BACK_LABELS),
)
@router.message(
    AuthStates.register_city,
    F.text.in_(BACK_LABELS),
)
@router.message(
    AuthStates.login_phone,
    F.text.in_(BACK_LABELS),
)
@router.message(
    AuthStates.login_full_name,
    F.text.in_(BACK_LABELS),
)
async def auth_back_message(message: Message, state: FSMContext) -> None:
    await _show_auth_selection_from_message(message, state)


@router.callback_query(
    AuthStates.register_city,
    F.data == "auth:back",
)
async def auth_back_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await _show_auth_selection_from_callback(callback, state)


@router.message(AuthStates.register_full_name, F.text)
async def register_full_name(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("language", LANG_TJ)
    texts = _texts(lang)

    await state.update_data(full_name=message.text.strip())
    await state.set_state(AuthStates.register_phone)
    await message.answer(texts.ASK_PHONE, reply_markup=auth_phone_keyboard(lang))


@router.message(AuthStates.register_full_name)
async def register_full_name_invalid(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    texts = _texts(data.get("language", LANG_TJ))
    await message.answer(texts.ASK_FULL_NAME)


TJ_PHONE_PREFIXES = {
    "50", "55", "77", "88",
    "90", "91", "92", "93", "94", "95", "98", "99",
}


def _normalize_tj_phone(raw_phone: str | None) -> str | None:
    phone = (raw_phone or "").strip()
    phone = (
        phone.replace(" ", "")
        .replace("-", "")
        .replace("(", "")
        .replace(")", "")
    )

    if phone.startswith("+992"):
        phone = phone[4:]
    elif phone.startswith("992"):
        phone = phone[3:]
    elif phone.startswith("+"):
        return None

    if not phone.isdigit():
        return None

    if len(phone) != 9:
        return None

    if phone[:2] not in TJ_PHONE_PREFIXES:
        return None

    return phone


def _invalid_phone_text(lang: str) -> str:
    if lang == LANG_RU:
        return (
            "❌ <b>Неверный номер телефона.</b>\n\n"
            "<blockquote>"
            "Бот принимает только номера Таджикистана.\n"
            "Введите номер без +992.\n"
            "Пример: <code>908804948</code>"
            "</blockquote>"
        )

    return (
        "❌ <b>Рақами телефон нодуруст аст.</b>\n\n"
        "<blockquote>"
        "Бот танҳо рақамҳои Тоҷикистонро қабул мекунад.\n"
        "Рақамро бе +992 ворид кунед.\n"
        "Мисол: <code>900044009</code>"
        "</blockquote>"
    )


async def _operator_keyboard(lang: str) -> InlineKeyboardMarkup | None:
    username = (await get_setting("operator_username", "") or "").strip()
    username = username.replace("@", "").strip()

    if not username:
        return None

    text = "👨‍💻 Написать оператору" if lang == LANG_RU else "👨‍💻 Ба оператор нависед"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, url=f"https://t.me/{username}")]
        ]
    )


async def _continue_after_phone(message: Message, state: FSMContext, *, lang: str, texts) -> None:
    try:
        data = await state.get_data()
        phone = data.get("phone")

        print(f"[AUTH] continue_after_phone telegram_id={message.from_user.id} phone={phone}")

        existing_by_phone = await get_user_by_phone(phone)
        print(f"[AUTH] existing_by_phone={bool(existing_by_phone)}")

        if existing_by_phone is not None:
            await state.clear()
            duplicate_text = (
                "❌ <b>Этот номер уже зарегистрирован.</b>\n\n"
                "<blockquote>Пожалуйста, напишите /start и используйте «Войти».</blockquote>"
                if lang == LANG_RU
                else
                "❌ <b>Ин рақам аллакай сабт шудааст.</b>\n\n"
                "<blockquote>Лутфан /start нависед ва аз «Ворид шудан» истифода баред.</blockquote>"
            )
            await message.answer(duplicate_text)
            return

        warehouses = await get_active_tj_pickup_warehouses()
        print(f"[AUTH] active_pickup_warehouses_count={len(warehouses)}")

        if len(warehouses) == 1:
            warehouse = warehouses[0]

            user = await create_user(
                telegram_id=message.from_user.id,
                username=message.from_user.username,
                language=lang,
                full_name=data["full_name"],
                phone=phone,
                city=_city_name(warehouse.city_key, lang),
            )

            await state.clear()
            await message.answer(
                texts.REGISTRATION_COMPLETED.format(client_code=user.client_code),
                reply_markup=user_main_menu(lang),
            )
            try:
                await sync_user_to_airtable(user)
            except Exception:
                logger.exception("Airtable user sync failed")
            return

        if len(warehouses) > 1:
            await state.set_state(AuthStates.register_city)
            await message.answer(
                texts.ASK_CITY,
                reply_markup=pickup_cities_keyboard(warehouses, lang, include_back=True),
            )
            return

        no_branch_text = (
            "❌ <b>Пока не добавлен ни один филиал для получения груза.</b>\n\n"
            "<blockquote>Пожалуйста, напишите оператору.</blockquote>"
            if lang == LANG_RU
            else
            "❌ <b>Ҳоло ягон филиали гирифтани бор илова нашудааст.</b>\n\n"
            "<blockquote>Лутфан ба оператор нависед.</blockquote>"
        )
        await message.answer(
            no_branch_text,
            reply_markup=await _operator_keyboard(lang),
        )

    except Exception as error:
        print(f"[AUTH_ERROR] continue_after_phone failed: {type(error).__name__}: {error}")

        error_text = (
            "❌ <b>Ошибка при регистрации.</b>\n\n"
            "<blockquote>Пожалуйста, попробуйте позже или напишите оператору.</blockquote>"
            if lang == LANG_RU
            else
            "❌ <b>Ҳангоми сабти ном хатогӣ шуд.</b>\n\n"
            "<blockquote>Лутфан баъдтар такрор кунед ё ба оператор нависед.</blockquote>"
        )

        await message.answer(
            error_text,
            reply_markup=await _operator_keyboard(lang),
        )


@router.callback_query(AuthStates.register_phone, F.data == "auth:back")
async def back_from_register_phone(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("language", LANG_TJ)
    texts = _texts(lang)

    await state.set_state(AuthStates.register_full_name)
    await callback.message.answer(
        texts.ASK_FULL_NAME,
        reply_markup=auth_back_keyboard(lang),
    )
    await callback.answer()


@router.message(AuthStates.register_phone, F.text.in_(["Назад", "Бозгашт"]))
async def back_from_phone_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("language", LANG_TJ)
    texts = _texts(lang)

    await state.set_state(AuthStates.register_full_name)
    await message.answer(
        texts.ASK_FULL_NAME,
        reply_markup=auth_back_keyboard(lang),
    )


@router.message(AuthStates.register_phone, F.text.startswith("/"))
async def register_phone_command(message: Message, state: FSMContext) -> None:
    command = (message.text or "").strip().split()[0].lower()

    if command == "/start":
        await state.clear()
        from handlers.start import start_handler
        await start_handler(message, state)
        return

    if command == "/admin":
        await state.clear()
        from handlers.admin_menu import admin_command
        await admin_command(message)
        return

    if lang == LANG_RU:
        text = (
            "⚠️ <b>Эта команда не работает во время регистрации.</b>\n\n"
            "<blockquote>Сначала отправьте номер телефона или напишите /start.</blockquote>"
        )
    else:
        text = (
            "⚠️ <b>Ин команда дар вақти сабти ном кор намекунад.</b>\n\n"
            "<blockquote>Лутфан аввал рақами телефонро фиристед ё /start нависед.</blockquote>"
        )

    await message.answer(text)


@router.message(AuthStates.register_phone, F.contact)
async def register_phone_contact(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("language", LANG_TJ)
    texts = _texts(lang)

    if not _is_own_contact(message):
        await message.answer(texts.INVALID_PHONE, reply_markup=auth_phone_keyboard(lang))
        return

    phone = _contact_phone(message)
    if phone is None:
        await message.answer(texts.INVALID_PHONE, reply_markup=auth_phone_keyboard(lang))
        return

    await state.update_data(phone=phone)
    await _continue_after_phone(message, state, lang=lang, texts=texts)


@router.message(AuthStates.register_phone, F.text)
async def register_phone_manual(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("language", LANG_TJ)
    texts = _texts(lang)
    phone = _manual_phone(message)

    if phone is None:
        await message.answer(texts.INVALID_PHONE, reply_markup=auth_phone_keyboard(lang))
        return

    await state.update_data(phone=phone)
    await _continue_after_phone(message, state, lang=lang, texts=texts)


@router.message(AuthStates.register_phone)
async def register_phone_invalid(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("language", LANG_TJ)
    texts = _texts(lang)
    await message.answer(texts.INVALID_PHONE, reply_markup=auth_phone_keyboard(lang))


@router.callback_query(AuthStates.register_city, F.data.startswith("city:"))
async def register_city(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user is None:
        return

    data = await state.get_data()
    lang = data.get("language", LANG_TJ)
    texts = _texts(lang)
    city_key = callback.data.split(":", 1)[1]
    warehouses = await get_active_tj_pickup_warehouses()
    allowed_city_keys = {warehouse.city_key for warehouse in warehouses}

    if city_key not in allowed_city_keys:
        await callback.answer("Ин филиал дастрас нест.", show_alert=True)
        return

    user = await create_user(
        telegram_id=callback.from_user.id,
        username=callback.from_user.username,
        language=lang,
        full_name=data["full_name"],
        phone=data["phone"],
        city=_city_name(city_key, lang),
    )
    await state.clear()
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        texts.REGISTRATION_COMPLETED.format(client_code=user.client_code),
        reply_markup=user_main_menu(lang),
    )
    await callback.answer()
    try:
        await sync_user_to_airtable(user)
    except Exception:
        logger.exception("Airtable user sync failed")


@router.message(AuthStates.login_phone, F.contact)
async def login_phone_contact(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("language", LANG_TJ)
    texts = _texts(lang)

    if not _is_own_contact(message):
        await message.answer(texts.INVALID_PHONE, reply_markup=auth_phone_keyboard(lang))
        return

    phone = _contact_phone(message)
    if phone is None:
        await message.answer(texts.INVALID_PHONE, reply_markup=auth_phone_keyboard(lang))
        return

    user = await get_user_by_phone(phone)
    if user is None:
        await message.answer(
            texts.PHONE_NOT_FOUND,
            reply_markup=auth_phone_keyboard(lang),
        )
        return

    lang = user.language
    texts = _texts(lang)
    await state.update_data(
        language=lang,
        login_user_id=user.id,
        phone=phone,
    )
    await state.set_state(AuthStates.login_full_name)
    await message.answer(texts.ASK_FULL_NAME, reply_markup=auth_back_keyboard(lang))


@router.message(AuthStates.login_phone, F.text)
async def login_phone_manual(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("language", LANG_TJ)
    texts = _texts(lang)
    phone = _manual_phone(message)

    if phone is None:
        await message.answer(texts.INVALID_PHONE, reply_markup=auth_phone_keyboard(lang))
        return

    user = await get_user_by_phone(phone)
    if user is None:
        await message.answer(
            texts.PHONE_NOT_FOUND,
            reply_markup=auth_phone_keyboard(lang),
        )
        return

    lang = user.language
    texts = _texts(lang)
    await state.update_data(
        language=lang,
        login_user_id=user.id,
        phone=phone,
    )
    await state.set_state(AuthStates.login_full_name)
    await message.answer(texts.ASK_FULL_NAME, reply_markup=auth_back_keyboard(lang))


@router.message(AuthStates.login_phone)
async def login_phone_invalid(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    lang = data.get("language", LANG_TJ)
    texts = _texts(lang)
    await message.answer(texts.INVALID_PHONE, reply_markup=auth_phone_keyboard(lang))


@router.message(AuthStates.login_full_name, F.text)
async def login_full_name(message: Message, state: FSMContext) -> None:
    if message.from_user is None:
        return

    data = await state.get_data()
    lang = data.get("language", LANG_TJ)
    texts = _texts(lang)

    user = await get_user_by_id(data["login_user_id"])

    if user is None:
        await state.set_state(AuthStates.login_phone)
        await message.answer(
            texts.PHONE_NOT_FOUND,
            reply_markup=auth_phone_keyboard(lang),
        )
        return

    if normalize_full_name(message.text) != user.normalized_full_name:
        await message.answer(texts.NAME_MISMATCH)
        return

    user = await attach_telegram_account(
        user.id,
        telegram_id=message.from_user.id,
        username=message.from_user.username,
    )
    await state.clear()
    await message.answer(
        texts.LOGIN_SUCCESS,
        reply_markup=user_main_menu(user.language),
    )


@router.message(AuthStates.login_full_name)
async def login_full_name_invalid(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    texts = _texts(data.get("language", LANG_TJ))
    await message.answer(texts.ASK_FULL_NAME)
