import asyncio
import os
from typing import Dict, Optional

import httpx
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove


def _get_required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} must be set")
    return value


BOT_TOKEN = _get_required_env("BOT_TOKEN")
AUTH_SERVICE_URL = _get_required_env("AUTH_SERVICE_URL").rstrip("/")
CATALOG_SERVICE_URL = _get_required_env("CATALOG_SERVICE_URL").rstrip("/")
ORDER_SERVICE_URL = _get_required_env("ORDER_SERVICE_URL").rstrip("/")


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
http_client = httpx.AsyncClient(timeout=10)


class UserSession:
    def __init__(self, email: str, token: str):
        self.email = email
        self.token = token


sessions: Dict[int, UserSession] = {}
# Состояния для ввода данных
user_states: Dict[int, str] = {}  # user_id -> "waiting_email", "waiting_password", "waiting_order"


def get_main_keyboard(is_authorized: bool) -> ReplyKeyboardMarkup:
    """Создает клавиатуру в зависимости от статуса авторизации"""
    if is_authorized:
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📦 Каталог"), KeyboardButton(text="📋 Мои заказы")],
                [KeyboardButton(text="🛒 Оформить заказ"), KeyboardButton(text="🚪 Выйти")],
            ],
            resize_keyboard=True,
        )
    else:
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="🔐 Войти"), KeyboardButton(text="📝 Регистрация")],
                [KeyboardButton(text="📦 Каталог")],
            ],
            resize_keyboard=True,
        )
    return keyboard


@dp.message(Command("start"))
async def cmd_start(message: Message):
    is_authorized = message.from_user.id in sessions
    keyboard = get_main_keyboard(is_authorized)
    await message.answer(
        "👋 Привет! Я бот-магазина.\n\n"
        "Используйте кнопки для навигации.",
        reply_markup=keyboard,
    )


@dp.message(lambda m: m.text == "📝 Регистрация")
async def btn_register(message: Message):
    if message.from_user.id in sessions:
        await message.answer("Вы уже авторизованы. Сначала выйдите из аккаунта.")
        return
    user_states[message.from_user.id] = "waiting_reg_email"
    await message.answer(
        "📝 Регистрация\n\nВведите ваш email:",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(lambda m: m.text == "🔐 Войти")
async def btn_login(message: Message):
    if message.from_user.id in sessions:
        await message.answer("Вы уже авторизованы.")
        return
    user_states[message.from_user.id] = "waiting_login_email"
    await message.answer(
        "🔐 Вход\n\nВведите ваш email:",
        reply_markup=ReplyKeyboardRemove(),
    )


async def _ensure_session(message: Message) -> UserSession | None:
    session = sessions.get(message.from_user.id)
    if not session:
        keyboard = get_main_keyboard(False)
        await message.answer("Сначала выполните вход.", reply_markup=keyboard)
        return None
    return session


@dp.message(lambda m: m.text == "📦 Каталог")
async def btn_products(message: Message):
    try:
        resp = await http_client.get(f"{CATALOG_SERVICE_URL}/products")
        resp.raise_for_status()
        products = resp.json()
        if not products:
            await message.answer("Каталог пуст.")
            return
        lines = ["📦 Каталог товаров:\n"]
        for p in products:
            lines.append(f"🆔 {p['id']}: {p['name']} — 💰 {p['price']}")
        text = "\n".join(lines)
        # Разбиваем на части, если сообщение слишком длинное
        if len(text) > 4096:
            chunk = ""
            for line in lines:
                if len(chunk + line) > 4000:
                    await message.answer(chunk)
                    chunk = line + "\n"
                else:
                    chunk += line + "\n"
            if chunk:
                await message.answer(chunk)
        else:
            await message.answer(text)
    except httpx.HTTPStatusError as exc:
        await message.answer(f"Ошибка каталога: {exc.response.text}")
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"Ошибка запроса: {exc}")


def _parse_order_items(arg: str):
    items = []
    if not arg:
        return items
    pairs = [p.strip() for p in arg.split(",") if p.strip()]
    for pair in pairs:
        try:
            product_part, qty_part = pair.split(":")
            items.append((int(product_part), int(qty_part)))
        except ValueError as exc:
            raise ValueError(f"Неверный формат пары '{pair}'") from exc
    return items


async def _load_catalog_prices() -> Dict[int, float]:
    resp = await http_client.get(f"{CATALOG_SERVICE_URL}/products")
    resp.raise_for_status()
    data = resp.json()
    return {item["id"]: item["price"] for item in data}


@dp.message(lambda m: m.text == "🛒 Оформить заказ")
async def btn_order_start(message: Message):
    session = await _ensure_session(message)
    if not session:
        return

    user_states[message.from_user.id] = "waiting_order"
    await message.answer(
        "🛒 Оформление заказа\n\n"
        "Введите товары в формате: product_id:qty,product_id:qty\n"
        "Пример: 1:2,2:1\n\n"
        "Для отмены отправьте /start",
        reply_markup=ReplyKeyboardRemove(),
    )


@dp.message(lambda m: m.text == "📋 Мои заказы")
async def btn_orders(message: Message):
    session = await _ensure_session(message)
    if not session:
        return

    try:
        resp = await http_client.get(f"{ORDER_SERVICE_URL}/orders")
        resp.raise_for_status()
        all_orders = resp.json()
        # Фильтруем заказы текущего пользователя
        user_orders = [o for o in all_orders if o.get("customer_email") == session.email]
        
        if not user_orders:
            await message.answer("📋 У вас пока нет заказов.")
            return
        
        lines = ["📋 Ваши заказы:\n"]
        for order in user_orders:
            lines.append(
                f"🆔 Заказ #{order['id']}\n"
                f"💰 Сумма: {order['total_amount']}\n"
                f"📊 Статус: {order['status']}\n"
                f"📅 Дата: {order['created_at']}\n"
            )
        text = "\n".join(lines)
        if len(text) > 4096:
            # Разбиваем на части
            chunk = ""
            for line in lines:
                if len(chunk + line) > 4000:
                    await message.answer(chunk)
                    chunk = line + "\n"
                else:
                    chunk += line + "\n"
            if chunk:
                await message.answer(chunk)
        else:
            await message.answer(text)
    except httpx.HTTPStatusError as exc:
        await message.answer(f"Ошибка сервиса заказов: {exc.response.text}")
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"Ошибка: {exc}")


@dp.message(lambda m: m.text == "🚪 Выйти")
async def btn_logout(message: Message):
    if message.from_user.id in sessions:
        email = sessions[message.from_user.id].email
        del sessions[message.from_user.id]
        user_states.pop(message.from_user.id, None)
        keyboard = get_main_keyboard(False)
        await message.answer(f"✅ Вы вышли из аккаунта ({email}).", reply_markup=keyboard)
    else:
        await message.answer("Вы не авторизованы.")


# Обработка состояний для ввода данных
@dp.message()
async def handle_text_messages(message: Message):
    """Обрабатывает текстовые сообщения в зависимости от состояния пользователя"""
    user_id = message.from_user.id
    state = user_states.get(user_id)
    text = message.text.strip()

    # Обработка регистрации
    if state == "waiting_reg_email":
        if "@" not in text:
            await message.answer("❌ Неверный формат email. Попробуйте еще раз:")
            return
        user_states[user_id] = f"waiting_reg_password:{text}"
        await message.answer("Введите пароль:")
        return

    if state and state.startswith("waiting_reg_password:"):
        email = state.split(":", 1)[1]
        password = text
        try:
            resp = await http_client.post(
                f"{AUTH_SERVICE_URL}/auth/register",
                json={"email": email, "password": password},
            )
            resp.raise_for_status()
            token = resp.json()["access_token"]
            sessions[user_id] = UserSession(email=email, token=token)
            user_states.pop(user_id, None)
            keyboard = get_main_keyboard(True)
            await message.answer("✅ Аккаунт создан и вход выполнен!", reply_markup=keyboard)
        except httpx.HTTPStatusError as exc:
            detail = exc.response.json().get("detail", "Ошибка регистрации")
            user_states.pop(user_id, None)
            keyboard = get_main_keyboard(False)
            await message.answer(f"❌ Не удалось зарегистрироваться: {detail}", reply_markup=keyboard)
        except Exception as exc:  # noqa: BLE001
            user_states.pop(user_id, None)
            keyboard = get_main_keyboard(False)
            await message.answer(f"❌ Ошибка запроса: {exc}", reply_markup=keyboard)
        return

    # Обработка входа
    if state == "waiting_login_email":
        if "@" not in text:
            await message.answer("❌ Неверный формат email. Попробуйте еще раз:")
            return
        user_states[user_id] = f"waiting_login_password:{text}"
        await message.answer("Введите пароль:")
        return

    if state and state.startswith("waiting_login_password:"):
        email = state.split(":", 1)[1]
        password = text
        try:
            resp = await http_client.post(
                f"{AUTH_SERVICE_URL}/auth/token",
                json={"email": email, "password": password},
            )
            resp.raise_for_status()
            token = resp.json()["access_token"]
            sessions[user_id] = UserSession(email=email, token=token)
            user_states.pop(user_id, None)
            keyboard = get_main_keyboard(True)
            await message.answer("✅ Успешный вход!", reply_markup=keyboard)
        except httpx.HTTPStatusError as exc:
            detail = exc.response.json().get("detail", "Ошибка авторизации")
            user_states.pop(user_id, None)
            keyboard = get_main_keyboard(False)
            await message.answer(f"❌ Не удалось войти: {detail}", reply_markup=keyboard)
        except Exception as exc:  # noqa: BLE001
            user_states.pop(user_id, None)
            keyboard = get_main_keyboard(False)
            await message.answer(f"❌ Ошибка запроса: {exc}", reply_markup=keyboard)
        return

    # Обработка оформления заказа
    if state == "waiting_order":
        session = sessions.get(user_id)
        if not session:
            user_states.pop(user_id, None)
            keyboard = get_main_keyboard(False)
            await message.answer("❌ Сессия истекла. Войдите снова.", reply_markup=keyboard)
            return

        try:
            pairs = _parse_order_items(text)
        except ValueError as exc:
            await message.answer(f"❌ {exc}\nПопробуйте еще раз или отправьте /start для отмены:")
            return

        if not pairs:
            await message.answer("❌ Нужно указать хотя бы один товар. Попробуйте еще раз:")
            return

        try:
            prices = await _load_catalog_prices()
            items_payload = []
            for product_id, qty in pairs:
                price = prices.get(product_id)
                if price is None:
                    await message.answer(f"❌ Товар {product_id} не найден. Попробуйте еще раз:")
                    return
                items_payload.append(
                    {"product_id": product_id, "quantity": qty, "price": price}
                )

            resp = await http_client.post(
                f"{ORDER_SERVICE_URL}/orders",
                json={"customer_email": session.email, "items": items_payload},
            )
            resp.raise_for_status()
            data = resp.json()
            user_states.pop(user_id, None)
            keyboard = get_main_keyboard(True)
            await message.answer(
                f"✅ Заказ #{data['id']} создан!\n💰 Сумма: {data['total_amount']}",
                reply_markup=keyboard,
            )
        except httpx.HTTPStatusError as exc:
            await message.answer(f"❌ Ошибка сервиса заказов: {exc.response.text}\nПопробуйте еще раз:")
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"❌ Ошибка: {exc}\nПопробуйте еще раз:")
        return

    # Если не в состоянии ожидания, показываем главное меню
    keyboard = get_main_keyboard(user_id in sessions)
    await message.answer("Используйте кнопки для навигации.", reply_markup=keyboard)


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())


