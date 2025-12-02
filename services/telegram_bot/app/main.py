import asyncio
import os
from typing import Dict

import httpx
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message


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


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я бот-магазина.\n"
        "Команды:\n"
        "/register email пароль — создать аккаунт и получить JWT.\n"
        "/login email пароль — авторизация и получение JWT.\n"
        "/products — показать товары каталога.\n"
        "/order product_id:qty,... — оформить заказ. Пример: /order 1:2,2:1"
    )


@dp.message(Command("register"))
async def cmd_register(message: Message):
    parts = message.text.strip().split()
    if len(parts) != 3:
        await message.answer("Использование: /register email пароль")
        return

    _, email, password = parts
    try:
        resp = await http_client.post(
            f"{AUTH_SERVICE_URL}/auth/register",
            json={"email": email, "password": password},
        )
        resp.raise_for_status()
        token = resp.json()["access_token"]
        sessions[message.from_user.id] = UserSession(email=email, token=token)
        await message.answer("Аккаунт создан и вход выполнен.")
    except httpx.HTTPStatusError as exc:
        detail = exc.response.json().get("detail", "Ошибка регистрации")
        await message.answer(f"Не удалось зарегистрироваться: {detail}")
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"Ошибка запроса: {exc}")


@dp.message(Command("login"))
async def cmd_login(message: Message):
    parts = message.text.strip().split()
    if len(parts) != 3:
        await message.answer("Использование: /login email пароль")
        return

    _, email, password = parts
    try:
        resp = await http_client.post(
            f"{AUTH_SERVICE_URL}/auth/token",
            json={"email": email, "password": password},
        )
        resp.raise_for_status()
        token = resp.json()["access_token"]
        sessions[message.from_user.id] = UserSession(email=email, token=token)
        await message.answer("Успешный вход. Теперь можно использовать команды.")
    except httpx.HTTPStatusError as exc:
        detail = exc.response.json().get("detail", "Ошибка авторизации")
        await message.answer(f"Не удалось войти: {detail}")
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"Ошибка запроса: {exc}")


async def _ensure_session(message: Message) -> UserSession | None:
    session = sessions.get(message.from_user.id)
    if not session:
        await message.answer("Сначала выполните /login email пароль")
        return None
    return session


@dp.message(Command("products"))
async def cmd_products(message: Message):
    try:
        resp = await http_client.get(f"{CATALOG_SERVICE_URL}/products")
        resp.raise_for_status()
        products = resp.json()
        if not products:
            await message.answer("Каталог пуст.")
            return
        lines = [f"{p['id']}: {p['name']} — {p['price']}" for p in products]
        await message.answer("\n".join(lines))
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


@dp.message(Command("order"))
async def cmd_order(message: Message):
    session = await _ensure_session(message)
    if not session:
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Использование: /order product_id:qty,product_id:qty")
        return

    try:
        pairs = _parse_order_items(parts[1])
    except ValueError as exc:
        await message.answer(str(exc))
        return

    if not pairs:
        await message.answer("Нужно указать хотя бы один товар.")
        return

    try:
        prices = await _load_catalog_prices()
        items_payload = []
        for product_id, qty in pairs:
            price = prices.get(product_id)
            if price is None:
                await message.answer(f"Товар {product_id} не найден.")
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
        await message.answer(
            f"Заказ #{data['id']} создан. Сумма: {data['total_amount']}."
        )
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text
        await message.answer(f"Ошибка сервиса заказов: {detail}")
    except Exception as exc:  # noqa: BLE001
        await message.answer(f"Ошибка: {exc}")


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())


