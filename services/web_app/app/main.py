import os
from typing import Dict, List

import httpx
from fastapi import FastAPI, Form, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware


def _get_required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} must be set")
    return value


SESSION_SECRET = _get_required_env("WEB_SESSION_SECRET")
AUTH_SERVICE_URL = _get_required_env("WEB_AUTH_URL").rstrip("/")
CATALOG_SERVICE_URL = _get_required_env("WEB_CATALOG_URL").rstrip("/")
ORDER_SERVICE_URL = _get_required_env("WEB_ORDER_URL").rstrip("/")

app = FastAPI(title="Shop Web UI", version="1.0.0")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, max_age=3600)

templates = Jinja2Templates(directory="app/templates")
http_client: httpx.AsyncClient | None = None


@app.on_event("startup")
async def on_startup():
    global http_client
    http_client = httpx.AsyncClient(timeout=10)


@app.on_event("shutdown")
async def on_shutdown():
    global http_client
    if http_client:
        await http_client.aclose()
        http_client = None


def _get_client() -> httpx.AsyncClient:
    if http_client is None:
        raise RuntimeError("HTTP client is not initialized")
    return http_client


def _current_user(request: Request) -> Dict | None:
    return request.session.get("user")


def _set_user(request: Request, email: str, token: str):
    request.session["user"] = {"email": email, "token": token}


def _clear_user(request: Request):
    request.session.pop("user", None)


def _set_flash(request: Request, message: str):
    request.session["flash"] = message


def _pop_flash(request: Request) -> str | None:
    return request.session.pop("flash", None)


@app.get("/")
async def home(request: Request):
    return templates.TemplateResponse(
        "home.html",
        {"request": request, "user": _current_user(request), "flash": _pop_flash(request)},
    )


@app.get("/register")
async def register_form(request: Request):
    return templates.TemplateResponse(
        "register.html",
        {"request": request, "flash": _pop_flash(request)},
    )


@app.post("/register")
async def register_user(request: Request, email: str = Form(...), password: str = Form(...)):
    client = _get_client()
    try:
        resp = await client.post(
            f"{AUTH_SERVICE_URL}/auth/register",
            json={"email": email, "password": password},
        )
        if resp.status_code >= 400:
            detail = resp.json().get("detail", "Registration failed")
            _set_flash(request, f"Ошибка регистрации: {detail}")
            return RedirectResponse(request.url_for("register_form"), status_code=status.HTTP_303_SEE_OTHER)
        token = resp.json()["access_token"]
        _set_user(request, email=email, token=token)
        _set_flash(request, "Регистрация успешна")
        return RedirectResponse(request.url_for("catalog"), status_code=status.HTTP_303_SEE_OTHER)
    except Exception as exc:  # noqa: BLE001
        _set_flash(request, f"Ошибка регистрации: {exc}")
        return RedirectResponse(request.url_for("register_form"), status_code=status.HTTP_303_SEE_OTHER)


@app.get("/login")
async def login_form(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "flash": _pop_flash(request)},
    )


@app.post("/login")
async def login_user(request: Request, email: str = Form(...), password: str = Form(...)):
    client = _get_client()
    try:
        resp = await client.post(
            f"{AUTH_SERVICE_URL}/auth/token",
            json={"email": email, "password": password},
        )
        if resp.status_code >= 400:
            detail = resp.json().get("detail", "Login failed")
            _set_flash(request, f"Ошибка входа: {detail}")
            return RedirectResponse(request.url_for("login_form"), status_code=status.HTTP_303_SEE_OTHER)
        token = resp.json()["access_token"]
        _set_user(request, email=email, token=token)
        _set_flash(request, "Вход выполнен")
        return RedirectResponse(request.url_for("catalog"), status_code=status.HTTP_303_SEE_OTHER)
    except Exception as exc:  # noqa: BLE001
        _set_flash(request, f"Ошибка входа: {exc}")
        return RedirectResponse(request.url_for("login_form"), status_code=status.HTTP_303_SEE_OTHER)


@app.post("/logout")
async def logout(request: Request):
    _clear_user(request)
    _set_flash(request, "Вы вышли из системы")
    return RedirectResponse(request.url_for("home"), status_code=status.HTTP_303_SEE_OTHER)


@app.get("/orders")
async def orders(request: Request):
    user = _current_user(request)
    if not user:
        _set_flash(request, "Сначала выполните вход")
        return RedirectResponse(request.url_for("login_form"), status_code=status.HTTP_303_SEE_OTHER)

    try:
        client = _get_client()
        # Получаем все заказы (в реальном проекте фильтровали бы по email)
        resp = await client.get(f"{ORDER_SERVICE_URL}/orders")
        resp.raise_for_status()
        all_orders = resp.json()
        # Фильтруем заказы текущего пользователя
        user_orders = [o for o in all_orders if o.get("customer_email") == user["email"]]
    except Exception as exc:  # noqa: BLE001
        _set_flash(request, f"Не удалось загрузить заказы: {exc}")
        user_orders = []

    return templates.TemplateResponse(
        "orders.html",
        {
            "request": request,
            "user": user,
            "flash": _pop_flash(request),
            "orders": user_orders,
        },
    )


async def _fetch_catalog() -> List[Dict]:
    client = _get_client()
    resp = await client.get(f"{CATALOG_SERVICE_URL}/products")
    resp.raise_for_status()
    return resp.json()


def _parse_order_items(raw: str) -> List[Dict]:
    items: List[Dict] = []
    if not raw:
        return items
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    for part in parts:
        try:
            prod, qty = part.split(":")
            items.append({"product_id": int(prod), "quantity": int(qty)})
        except ValueError as exc:
            raise ValueError(f"Неверный формат '{part}', ожидается product_id:qty") from exc
    return items


@app.get("/catalog")
async def catalog(request: Request):
    user = _current_user(request)
    if not user:
        _set_flash(request, "Сначала выполните вход")
        return RedirectResponse(request.url_for("login_form"), status_code=status.HTTP_303_SEE_OTHER)

    try:
        products = await _fetch_catalog()
    except Exception as exc:  # noqa: BLE001
        _set_flash(request, f"Не удалось загрузить каталог: {exc}")
        products = []

    return templates.TemplateResponse(
        "catalog.html",
        {
            "request": request,
            "user": user,
            "flash": _pop_flash(request),
            "products": products,
        },
    )


async def _build_order_payload(raw_items: str) -> List[Dict]:
    parsed = _parse_order_items(raw_items)
    if not parsed:
        raise ValueError("Нужно указать хотя бы один товар")
    catalog = await _fetch_catalog()
    price_map = {item["id"]: item["price"] for item in catalog}
    payload = []
    for item in parsed:
        price = price_map.get(item["product_id"])
        if price is None:
            raise ValueError(f"Товар {item['product_id']} не найден в каталоге")
        payload.append(
            {"product_id": item["product_id"], "quantity": item["quantity"], "price": price}
        )
    return payload


@app.post("/order")
async def create_order(request: Request, items: str = Form(...)):
    user = _current_user(request)
    if not user:
        _set_flash(request, "Сначала выполните вход")
        return RedirectResponse(request.url_for("login_form"), status_code=status.HTTP_303_SEE_OTHER)

    try:
        items_payload = await _build_order_payload(items)
        client = _get_client()
        resp = await client.post(
            f"{ORDER_SERVICE_URL}/orders",
            json={"customer_email": user["email"], "items": items_payload},
        )
        resp.raise_for_status()
        data = resp.json()
        _set_flash(request, f"Заказ #{data['id']} создан. Сумма: {data['total_amount']}")
    except ValueError as exc:
        _set_flash(request, str(exc))
    except httpx.HTTPStatusError as exc:
        _set_flash(request, f"Ошибка сервиса заказов: {exc.response.text}")
    except Exception as exc:  # noqa: BLE001
        _set_flash(request, f"Ошибка оформления заказа: {exc}")

    return RedirectResponse(request.url_for("catalog"), status_code=status.HTTP_303_SEE_OTHER)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "web_app"}


