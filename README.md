## Интернет-магазин: микросервисная архитектура (Python, FastAPI)

Учебный проект интернет‑магазина на Python в виде набора микросервисов:

- **Микросервис авторизации** (auth)
- **Микросервис каталога** (catalog)
- **Микросервис заказов** (order, transaction outbox)
- Инфраструктура: RabbitMQ, Redis, PostgreSQL, Docker Compose.

Используются компоненты:
- **БД**: PostgreSQL (`db`)
- **Кеш**: Redis (`redis`)
- **Шина данных**: RabbitMQ (`rabbitmq`)
- **Микросервис авторизации**: `auth_service`
- **Микросервис каталога**: `catalog_service`
- **Микросервис заказов (outbox)**: `order_service`
- **Telegram-бот (UI)**: `telegram_bot`
- **Веб-приложение (UI)**: `web_app`

---

## Схема взаимодействия сервисов

Высокоуровневая схема:

```text
        [Клиент / Браузер / Telegram]
                   |
                   v
      +----------------+      +----------------+
      |   Web UI       |      | Telegram bot   |
      | (web_app)      |      | (telegram_bot) |
      +----------------+      +----------------+
                   |                  |
                   +---------+--------+
                             |
                             v
          +--------------------+
          |   auth_service     |
          |  JWT аутентификация|
          +--------------------+
                   |
            (JWT-токен)
                   |
                   v
    +---------------------------+
    |       catalog_service     |
    |  /products  (чтение)      |
    |  кеш в Redis              |
    +---------------------------+
                   |
                   | (создание заказа)
                   v
    +---------------------------+
    |       order_service       |
    |  /orders (создание)       |
    |  Postgres + outbox        |
    +---------------------------+
                   |
          (событие order.created)
                   v
      +-------------------------+
      |        RabbitMQ         |
      |  exchange: shop_events  |
      +-------------------------+
                   |
                   v
      [Подписчики других сервисов]
```

Ключевые моменты:
- Клиент сначала получает JWT‑токен из `auth_service`, затем использует его при обращении к другим сервисам.
- `catalog_service` читает данные, кешируя их в Redis.
- `order_service` создаёт заказ и пишет событие в outbox в одной транзакции с БД, затем воркер отправляет событие в RabbitMQ.
- Другие сервисы (например, платежи, уведомления) могут подписываться на события из RabbitMQ и реагировать асинхронно.

---

## Структура и ответственность компонентов

### База данных и инфраструктура (общая для микросервисов)

- **`db` (PostgreSQL)**  
  - Общая БД для примера (order_service, потенциально другие сервисы).
  - Поднимается как сервис `db` в `docker-compose.yml`.

- **`redis` (Redis)**  
  - Используется **`catalog_service`** для кеширования списка товаров (`catalog:products`).

- **`rabbitmq` (RabbitMQ + management UI)**  
  - Шина сообщений для асинхронного взаимодействия.
  - **`order_service`** публикует события `order.created` в exchange `shop_events`.
  - Менеджмент‑панель по порту `15672` (логин/пароль по умолчанию `guest/guest`).

### Веб-приложение `web_app`

- **Точка входа**: `services/web_app/app/main.py`
- **Назначение**:
  - Удобный веб-интерфейс для пользователей.
  - Работает поверх API (`auth_service`, `catalog_service`, `order_service`) через HTTP.
  - Поддерживает регистрацию, авторизацию, просмотр каталога и создание заказов.
- **Маршруты**:
  - `GET /` — главная страница.
  - `GET/POST /register` — регистрация пользователя.
  - `GET/POST /login`, `POST /logout`.
  - `GET /catalog`, `POST /order`.
- **Настройки**: использует сессионные cookie (переменная `WEB_SESSION_SECRET`).

### Микросервис авторизации `auth_service`

- **Точка входа**: `services/auth_service/app/main.py`
- **Назначение**:
  - Централизованная **аутентификация/авторизация** с использованием **JWT**.
  - В реальном проекте монолит и микросервисы будут валидировать токен, полученный здесь.

- **Основные эндпоинты** (порт `8001`):
  - `POST /auth/register` — создаёт пользователя (email + пароль), возвращает JWT.
  - `POST /auth/token` — выдача JWT по логину/паролю.
  - `GET /auth/verify`  
    - Проверяет токен из `Authorization: Bearer <token>` (упрощённо) и возвращает данные пользователя.
  - `GET /health` — статус сервиса.

---

### Микросервис каталога `catalog_service`

- **Точка входа**: `services/catalog_service/app/main.py`
- **Назначение**:
  - Предоставление списка товаров.
  - Демонстрация **кеширования в Redis**.

- **Основные эндпоинты** (порт `8002`):
  - `GET /products`  
    - При первом запросе формирует список товаров (заглушка).
    - Сохраняет результат в Redis в ключ `catalog:products` с TTL 60 секунд.
    - При последующих запросах читает данные из кеша.
  - `GET /health` — статус сервиса.

---

### Микросервис заказов `order_service` (transaction outbox + RabbitMQ)

- **Точка входа**: `services/order_service/app/main.py`
- **БД/ORM**: `services/order_service/app/db.py`  
  Таблицы:
  - `orders` — заказы (id, email, total_amount, status, created_at).
  - `outbox_messages` — outbox для асинхронных событий:
    - `event_type`, `payload` (JSON), `status` (`pending`/`sent`), временные метки.

- **Паттерн transaction outbox**:
  - При создании заказа:
    - В одной **БД‑транзакции**:
      - создаётся запись в `orders`;
      - добавляется запись в `outbox_messages` с `event_type = "order_created"` и payload `{order_id, customer_email, total_amount}`.
  - Отдельный фоновой воркер `publish_outbox_messages`:
    - периодически читает `pending`‑сообщения;
    - публикует в RabbitMQ (exchange `shop_events`, routing key `order.created`);
    - помечает сообщения как `sent`.

- **Эндпоинты** (порт `8003`):
  - `GET /health`
  - `GET /orders` — список заказов (из своей таблицы `orders`).
  - `POST /orders` — пример распределённой транзакции (локальная транзакция + надёжная отправка события во внешнюю систему через outbox).

---

## Конфигурация и переменные окружения

- Все секреты и чувствительные параметры задаются **только через переменные окружения** (в коде нет дефолтных значений).
- Используем стандартный механизм `docker compose`, который читает переменные из файла `.env` в корне проекта (файл не хранится в репозитории).
- Перед запуском создайте `.env` в корне (рядом с `docker-compose.yml`) и заполните нужные значения. Пример содержимого:

```env
POSTGRES_DB=shop_db
POSTGRES_USER=shop_user
POSTGRES_PASSWORD=change_me

RABBITMQ_DEFAULT_USER=guest
RABBITMQ_DEFAULT_PASS=guest

AUTH_JWT_SECRET=change_me_super_secret
AUTH_JWT_ALGORITHM=HS256
AUTH_JWT_EXPIRE_MINUTES=60
AUTH_DATABASE_URL=postgresql+asyncpg://shop_user:change_me@db:5432/shop_db

CATALOG_REDIS_URL=redis://redis:6379/0

ORDER_DATABASE_URL=postgresql+asyncpg://shop_user:change_me@db:5432/shop_db
ORDER_RABBITMQ_URL=amqp://guest:guest@rabbitmq:5672//

TELEGRAM_BOT_TOKEN=123456789:ABCDEF...
TELEGRAM_AUTH_URL=http://auth_service:8000
TELEGRAM_CATALOG_URL=http://catalog_service:8000
TELEGRAM_ORDER_URL=http://order_service:8000

WEB_SESSION_SECRET=change_me_session
WEB_AUTH_URL=http://auth_service:8000
WEB_CATALOG_URL=http://catalog_service:8000
WEB_ORDER_URL=http://order_service:8000
```

> Замените все `change_me` на собственные значения. Пароль в `ORDER_DATABASE_URL` должен совпадать с `POSTGRES_PASSWORD`. Файл `.env` добавлен в `.gitignore`, поэтому секреты не попадут в репозиторий.

---

## Как запустить проект

### Предварительные требования

- Установлены **Docker** и **docker-compose** (или `docker compose`).

### Шаги запуска

1. Создайте файл `.env` (см. пример выше) и пропишите собственные значения секретов.
2. Настройте Telegram‑бота через BotFather, получите токен и пропишите в `.env`.
3. Настройте переменные для веб-интерфейса (`WEB_SESSION_SECRET`, URL'ы сервисов).
4. В корне проекта (`Project_shop`) выполнить:

   ```bash
   docker compose up --build
   ```

5. Дождаться, пока поднимутся все сервисы:
   - `db`, `redis`, `rabbitmq`
   - `auth_service` (порт `8001`)
   - `catalog_service` (порт `8002`)
   - `order_service` (порт `8003`)
   - `telegram_bot` (в фоне, без порта)
   - `web_app` (порт `8080`)

6. Проверка, что всё работает:
   - Auth: `GET http://localhost:8001/health`
   - Catalog: `GET http://localhost:8002/health`
   - Orders: `GET http://localhost:8003/health`
   - RabbitMQ UI: `http://localhost:15672` (логин/пароль `guest/guest`).
   - Telegram: напишите своему боту `/start`
   - Web UI: откройте `http://localhost:8080`

---

## Примеры использования

### 1. Регистрация и получение JWT в `auth_service`

```bash
curl -X POST http://localhost:8001/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"user@example.com","password":"password123"}'
```

```bash
curl -X POST http://localhost:8001/auth/token \
  -H "Content-Type: application/json" \
  -d '{"email":"user@example.com","password":"password123"}'
```

```bash
curl -X POST http://localhost:8001/auth/token \
  -H "Content-Type: application/json" \
  -d '{"email":"user@example.com","password":"password123"}'
```

Пример ответа:

```json
{"access_token": "<JWT>", "token_type": "bearer"}
```

---

### 2. Кеширование в `catalog_service` (Redis)

```bash
curl http://localhost:8002/products
```

- Первый запрос:
  - сгенерирует список товаров (заглушка),
  - положит его в Redis в ключ `catalog:products`.
- Повторные запросы в течение ~60 сек будут читать данные из Redis.

---

### 3. Пример transaction outbox и асинхронного события заказа

Создать заказ через `order_service`:

```bash
curl -X POST http://localhost:8003/orders \
  -H "Content-Type: application/json" \
  -d '{
    "customer_email": "buyer@example.com",
    "items": [
      { "product_id": 1, "quantity": 2, "price": 500.0 },
      { "product_id": 2, "quantity": 1, "price": 1500.0 }
    ]
  }'
```

- В одной транзакции в Postgres:
  - создаётся запись в `orders`,
  - создаётся запись в `outbox_messages` с `event_type="order_created"`.
- Фоновый воркер (`publish_outbox_messages`) периодически:
  - читает из `outbox_messages` все `pending`,
  - публикует в RabbitMQ `shop_events` событие `order.created` с JSON‑payload,
  - помечает сообщения как `sent`.

Проверить список заказов:

```bash
curl http://localhost:8003/orders
```

---

### 4. Пользовательские интерфейсы

- **Telegram-бот**:
  - `/start` — подсказка.
  - `/login email пароль` — получить JWT через `auth_service`.
  - `/products` — показать товары из `catalog_service`.
  - `/order product_id:qty,...` — оформить заказ в `order_service`. Например: `/order 1:2,2:1`.
- **Web UI** (`http://localhost:8080`):
  - Регистрация/логин через формы.
  - Просмотр каталога (данные из `catalog_service`).
  - Оформление заказа через форму, с отправкой данных в `order_service`.


