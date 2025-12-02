# Оркестрация vs Хореография: Распределенные транзакции

## Введение

В микросервисной архитектуре для координации работы нескольких сервисов используются два основных паттерна:

1. **Оркестрация (Orchestration)** — централизованный координатор управляет всем процессом
2. **Хореография (Choreography)** — каждый сервис реагирует на события независимо

## Текущий пример: Создание заказа

В нашем проекте при создании заказа нужно выполнить несколько шагов:
1. Создать заказ (order_service)
2. Обработать платеж (payment_service)
3. Отправить уведомление (notification_service)
4. Обновить склад (inventory_service) — опционально

---

## 1. Оркестрация (Orchestration)

### Концепция

**Оркестратор** (центральный сервис) знает всю последовательность шагов и координирует выполнение:

```
[Клиент] 
    |
    v
[Order Service (Оркестратор)]
    |
    |---> [1] Создать заказ (локально)
    |---> [2] Вызвать Payment Service (HTTP/RPC)
    |         |---> Обработать платеж
    |         |<--- Ответ: success/failed
    |
    |---> [3] Если платеж успешен:
    |         |---> Вызвать Notification Service (HTTP/RPC)
    |         |---> Отправить email/SMS
    |         |<--- Ответ: sent
    |
    |---> [4] Обновить статус заказа
    |
    v
[Ответ клиенту]
```

### Характеристики

✅ **Плюсы:**
- Централизованное управление — легко понять последовательность
- Проще отладка — все логи в одном месте
- Легко откатить транзакцию — оркестратор знает все шаги
- Можно реализовать Saga Pattern для отката

❌ **Минусы:**
- Оркестратор становится узким местом
- Тесная связанность — оркестратор должен знать все сервисы
- Сложнее масштабировать — все запросы идут через оркестратор

### Реализация в проекте

**Файл:** `services/order_service/app/orchestrator.py`

```python
# Order Service выступает оркестратором
async def create_order_with_orchestration(req: CreateOrderRequest):
    # Шаг 1: Создать заказ
    order = await create_order_in_db(req)
    
    # Шаг 2: Обработать платеж (синхронный вызов)
    payment_result = await http_client.post(
        f"{PAYMENT_SERVICE_URL}/payments",
        json={"order_id": order.id, "amount": order.total_amount}
    )
    
    if payment_result.status_code != 200:
        # Откат: отменить заказ
        await cancel_order(order.id)
        raise PaymentFailedError()
    
    # Шаг 3: Отправить уведомление
    await http_client.post(
        f"{NOTIFICATION_SERVICE_URL}/notifications",
        json={"order_id": order.id, "email": order.customer_email}
    )
    
    # Шаг 4: Обновить статус
    await update_order_status(order.id, "confirmed")
    
    return order
```

**Поток данных:**
1. Клиент → `POST /orders` → Order Service
2. Order Service → `POST /payments` → Payment Service (синхронно)
3. Order Service → `POST /notifications` → Notification Service (синхронно)
4. Order Service → Обновляет статус заказа
5. Order Service → Ответ клиенту

---

## 2. Хореография (Choreography)

### Концепция

Каждый сервис **независимо** подписывается на события и реагирует на них:

```
[Клиент]
    |
    v
[Order Service]
    |---> Создать заказ
    |---> Публикует событие: order.created
    |
    v
[RabbitMQ: order.created]
    |
    +---> [Payment Service] подписан на order.created
    |     |---> Обрабатывает платеж
    |     |---> Публикует: payment.processed (или payment.failed)
    |
    +---> [Notification Service] подписан на order.created
          |---> Ждет payment.processed
          |---> Отправляет уведомление
          |---> Публикует: notification.sent
```

### Характеристики

✅ **Плюсы:**
- Слабая связанность — сервисы не знают друг о друге
- Легко масштабировать — каждый сервис работает независимо
- Отказоустойчивость — если один сервис упал, другие продолжают работать
- Горизонтальное масштабирование — можно запустить несколько экземпляров

❌ **Минусы:**
- Сложнее отладка — логи разбросаны по сервисам
- Сложнее понять общий поток — нет центральной точки
- Сложнее реализовать откат — нужен компенсирующий паттерн (Saga)

### Реализация в проекте

**Файл:** `services/payment_service/app/event_consumer.py`

```python
# Payment Service подписывается на order.created
async def consume_order_created():
    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    channel = await connection.channel()
    exchange = await channel.declare_exchange("shop_events", aio_pika.ExchangeType.TOPIC)
    queue = await channel.declare_queue("payment_queue", durable=True)
    await queue.bind(exchange, routing_key="order.created")
    
    async with queue.iterator() as queue_iter:
        async for message in queue_iter:
            async with message.process():
                event = json.loads(message.body)
                order_id = event["order_id"]
                amount = event["total_amount"]
                
                # Обработать платеж
                payment_result = await process_payment(order_id, amount)
                
                # Публиковать результат
                if payment_result.success:
                    await publish_event("payment.processed", {
                        "order_id": order_id,
                        "payment_id": payment_result.id
                    })
                else:
                    await publish_event("payment.failed", {
                        "order_id": order_id,
                        "reason": payment_result.error
                    })
```

**Файл:** `services/notification_service/app/event_consumer.py`

```python
# Notification Service подписывается на payment.processed
async def consume_payment_processed():
    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    channel = await connection.channel()
    exchange = await channel.declare_exchange("shop_events", aio_pika.ExchangeType.TOPIC)
    queue = await channel.declare_queue("notification_queue", durable=True)
    await queue.bind(exchange, routing_key="payment.processed")
    
    async with queue.iterator() as queue_iter:
        async for message in queue_iter:
            async with message.process():
                event = json.loads(message.body)
                order_id = event["order_id"]
                
                # Отправить уведомление
                await send_notification(order_id)
                
                # Публиковать событие
                await publish_event("notification.sent", {"order_id": order_id})
```

**Поток данных:**
1. Клиент → `POST /orders` → Order Service
2. Order Service → Создает заказ → Публикует `order.created` в RabbitMQ
3. Payment Service (подписчик) → Получает `order.created` → Обрабатывает платеж → Публикует `payment.processed`
4. Notification Service (подписчик) → Получает `payment.processed` → Отправляет уведомление → Публикует `notification.sent`
5. Order Service (опционально) → Подписывается на `payment.processed` → Обновляет статус заказа

---

## Сравнение

| Критерий | Оркестрация | Хореография |
|----------|-------------|-------------|
| **Координация** | Централизованная (оркестратор) | Децентрализованная (события) |
| **Связанность** | Тесная (оркестратор знает все) | Слабая (сервисы независимы) |
| **Отладка** | Легко (все в одном месте) | Сложнее (логи разбросаны) |
| **Масштабирование** | Сложнее (узкое место) | Легче (независимые сервисы) |
| **Отказоустойчивость** | Зависит от оркестратора | Выше (изоляция сервисов) |
| **Откат транзакций** | Проще (Saga Pattern) | Сложнее (компенсирующие события) |

---

## Когда использовать что?

### Используй Оркестрацию, если:
- Нужна строгая последовательность шагов
- Важна консистентность данных (ACID-подобное поведение)
- Процесс сложный и требует координации
- Примеры: банковские переводы, бронирование билетов

### Используй Хореографию, если:
- Сервисы должны быть слабо связаны
- Нужна высокая производительность и масштабируемость
- Процесс можно разбить на независимые шаги
- Примеры: обработка заказов, аналитика, логирование

---

## Реализация в проекте

В проекте реализованы **оба подхода**:

1. **Хореография** (текущая реализация):
   - `order_service` публикует `order.created` через transaction outbox
   - `payment_service` и `notification_service` подписываются на события

2. **Оркестрация** (альтернативный эндпоинт):
   - `order_service` имеет эндпоинт `/orders/orchestrated` для синхронной координации

См. код в:
- `services/order_service/app/main.py` — создание заказа и публикация событий
- `services/payment_service/` — пример подписчика (хореография)
- `services/notification_service/` — пример подписчика (хореография)

