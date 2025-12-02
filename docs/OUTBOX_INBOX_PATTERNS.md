# Transaction Outbox и Transaction Inbox Patterns

## Введение

В микросервисной архитектуре для надежной асинхронной обработки событий используются два ключевых паттерна:

1. **Transaction Outbox** — гарантирует надежную публикацию событий
2. **Transaction Inbox** — гарантирует идемпотентную обработку событий

Оба паттерна решают проблему **distributed transactions** в микросервисах, где нельзя использовать классические ACID-транзакции между сервисами.

---

## 1. Transaction Outbox Pattern

### Проблема

Когда сервис должен:
1. Сохранить данные в БД
2. Опубликовать событие в RabbitMQ

**Проблема:** Если между этими операциями произойдет сбой, мы получим:
- Данные сохранены, но событие не опубликовано → другие сервисы не узнают об изменении
- Или наоборот: событие опубликовано, но транзакция откатилась → ложное событие

**Классическое решение не работает:**
```python
# ❌ Проблема: нет транзакции между БД и RabbitMQ
async with session.begin():
    order = OrderORM(...)
    session.add(order)
    await session.commit()

# Если здесь упадет сервис - событие не будет опубликовано
await rabbitmq.publish("order.created", {...})
```

### Решение: Transaction Outbox

**Идея:** Сохраняем событие в БД в той же транзакции, что и бизнес-данные. Отдельный воркер публикует события из БД в RabbitMQ.

```
┌─────────────────────────────────────┐
│  1. Создание заказа                 │
│     ┌─────────────┐                 │
│     │ orders      │                 │
│     └─────────────┘                 │
│           │                         │
│           ▼                         │
│     ┌─────────────┐                 │
│     │ outbox_     │  ← В одной      │
│     │ messages    │    транзакции!  │
│     └─────────────┘                 │
└─────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────┐
│  2. Outbox Worker (фоновый)         │
│     - Читает pending из outbox      │
│     - Публикует в RabbitMQ           │
│     - Помечает как sent             │
└─────────────────────────────────────┘
           │
           ▼
      ┌─────────┐
      │RabbitMQ │
      └─────────┘
```

### Реализация в проекте

**Файл:** `services/order_service/app/main.py`

```python
@app.post("/orders")
async def create_order(req: CreateOrderRequest, session: AsyncSession):
    total_amount = sum(i.price * i.quantity for i in req.items)
    
    # В ОДНОЙ транзакции:
    async with session.begin():
        # 1. Создаем заказ
        order = OrderORM(...)
        session.add(order)
        await session.flush()
        
        # 2. Сохраняем событие в outbox
        outbox_msg = OutboxMessageORM(
            event_type="order_created",
            payload={"order_id": order.id, ...},
            status="pending"
        )
        session.add(outbox_msg)
        # Транзакция коммитится здесь
```

**Файл:** `services/order_service/app/outbox_publisher.py`

```python
async def publish_outbox_messages():
    """Фоновый воркер публикует события из outbox"""
    while True:
        # 1. Читаем pending сообщения
        messages = await session.execute(
            select(OutboxMessageORM)
            .where(OutboxMessageORM.status == "pending")
        )
        
        for msg in messages:
            # 2. Публикуем в RabbitMQ
            await exchange.publish(msg.payload, routing_key=...)
            
            # 3. Помечаем как sent
            await session.execute(
                update(OutboxMessageORM)
                .where(OutboxMessageORM.id == msg.id)
                .values(status="sent")
            )
        
        await session.commit()
        await asyncio.sleep(5)
```

### Преимущества

✅ **Надежность:** Событие гарантированно будет опубликовано (даже после перезапуска сервиса)  
✅ **Консистентность:** Бизнес-данные и событие сохраняются атомарно  
✅ **Отказоустойчивость:** Если RabbitMQ недоступен, события останутся в outbox и будут опубликованы позже

### Недостатки

❌ **Задержка:** Событие публикуется не мгновенно (зависит от частоты работы воркера)  
❌ **Дополнительная таблица:** Нужна таблица `outbox_messages`

---

## 2. Transaction Inbox Pattern

### Проблема

Когда сервис получает событие из RabbitMQ и должен:
1. Обработать событие (сохранить в БД)
2. Выполнить бизнес-логику

**Проблема:** RabbitMQ гарантирует **at-least-once delivery** (сообщение может быть доставлено несколько раз). Если обработать сообщение дважды:
- Дублирование платежей
- Дублирование уведомлений
- Нарушение консистентности данных

**Пример проблемы:**
```python
# ❌ Проблема: если сообщение придет дважды - платеж обработается дважды
async def consume_order_created(message):
    event = json.loads(message.body)
    order_id = event["order_id"]
    
    # Обработка платежа
    await process_payment(order_id, amount)  # ← Выполнится дважды!
```

### Решение: Transaction Inbox

**Идея:** Сохраняем сообщение в БД (inbox) в той же транзакции, что и обработка. Перед обработкой проверяем, не было ли это сообщение обработано ранее.

```
┌─────────────────────────────────────┐
│  RabbitMQ → Сообщение order.created │
└─────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────┐
│  1. Проверка inbox                  │
│     - message_id уже обработан?     │
│     - Если да → пропускаем          │
│     - Если нет → продолжаем         │
└─────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────┐
│  2. Обработка (в транзакции)        │
│     ┌─────────────┐                 │
│     │ payments    │                 │
│     └─────────────┘                 │
│           │                         │
│           ▼                         │
│     ┌─────────────┐                 │
│     │ inbox_      │  ← В одной      │
│     │ messages    │    транзакции!  │
│     └─────────────┘                 │
└─────────────────────────────────────┘
```

### Реализация в проекте

**Файл:** `services/payment_service/app/database.py`

```python
class InboxMessageORM(Base):
    """Таблица для идемпотентной обработки сообщений"""
    __tablename__ = "inbox_messages"
    
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[str] = mapped_column(String, unique=True, index=True)  # Уникальный ID
    event_type: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String)  # pending, processed, failed
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
```

**Файл:** `services/payment_service/app/main.py`

```python
async def _process_payment_with_inbox(event: dict, message_id: str, session: AsyncSession):
    """Обработка с использованием Transaction Inbox"""
    
    # 1. Проверяем, не обрабатывалось ли это сообщение
    if await _is_message_processed(message_id, session):
        print(f"Сообщение {message_id} уже обработано, пропускаем")
        return  # Идемпотентность!
    
    # 2. В ОДНОЙ транзакции:
    async with session.begin():
        # Сохраняем в inbox
        inbox_msg = InboxMessageORM(
            message_id=message_id,
            event_type="order_created",
            payload=event,
            status="pending"
        )
        session.add(inbox_msg)
        await session.flush()
        
        # Обрабатываем платеж
        payment = PaymentORM(...)
        session.add(payment)
        
        # Помечаем как обработанное
        await session.execute(
            update(InboxMessageORM)
            .where(InboxMessageORM.message_id == message_id)
            .values(status="processed")
        )
        # Транзакция коммитится здесь
```

### Генерация message_id

Для идемпотентности нужен уникальный ID сообщения:

```python
def _generate_message_id(message_body: bytes, routing_key: str) -> str:
    """Генерирует уникальный ID на основе содержимого сообщения"""
    content = f"{routing_key}:{message_body.decode('utf-8')}"
    return hashlib.sha256(content.encode()).hexdigest()
```

**Альтернатива:** Использовать `message_id` из RabbitMQ (если он есть в заголовках).

### Преимущества

✅ **Идемпотентность:** Одно сообщение обработается только один раз  
✅ **Надежность:** Даже при повторной доставке не будет дублирования  
✅ **Консистентность:** Обработка и сохранение в inbox атомарны

### Недостатки

❌ **Дополнительная таблица:** Нужна таблица `inbox_messages`  
❌ **Нужен уникальный message_id:** Должен быть способ идентифицировать сообщение

---

## Сравнение паттернов

| Критерий | Transaction Outbox | Transaction Inbox |
|----------|-------------------|------------------|
| **Назначение** | Надежная публикация событий | Идемпотентная обработка событий |
| **Где используется** | Сервис-издатель (producer) | Сервис-подписчик (consumer) |
| **Проблема** | Событие может не быть опубликовано | Сообщение может быть обработано дважды |
| **Решение** | Сохранить событие в БД, воркер публикует | Сохранить сообщение в БД перед обработкой |
| **Таблица** | `outbox_messages` | `inbox_messages` |
| **Статусы** | `pending` → `sent` | `pending` → `processed` |

---

## Использование в проекте

### Transaction Outbox

**Сервис:** `order_service`  
**Файлы:**
- `services/order_service/app/db.py` — модель `OutboxMessageORM`
- `services/order_service/app/main.py` — создание заказа с сохранением в outbox
- `services/order_service/app/outbox_publisher.py` — воркер публикации

**Поток:**
1. Клиент создает заказ → `POST /orders`
2. Order Service сохраняет заказ + событие в outbox (в одной транзакции)
3. Outbox Worker читает pending сообщения
4. Worker публикует в RabbitMQ
5. Worker помечает сообщения как `sent`

### Transaction Inbox

**Сервис:** `payment_service`  
**Файлы:**
- `services/payment_service/app/database.py` — модель `InboxMessageORM`
- `services/payment_service/app/main.py` — обработка с проверкой inbox

**Поток:**
1. Payment Service получает событие `order.created` из RabbitMQ
2. Генерируется `message_id` (на основе содержимого)
3. Проверяется, не обрабатывалось ли это сообщение (по `message_id`)
4. Если новое → сохраняется в inbox + обрабатывается платеж (в одной транзакции)
5. Если уже обработано → пропускается (идемпотентность)

---

## Когда использовать

### Используй Transaction Outbox, если:
- Сервис должен публиковать события в очередь
- Нужна гарантия, что событие будет опубликовано
- Бизнес-данные и событие должны быть атомарными

### Используй Transaction Inbox, если:
- Сервис подписывается на события из очереди
- Нужна идемпотентность обработки
- Важно избежать дублирования операций

### Используй оба, если:
- Сервис и публикует, и подписывается на события
- Нужна полная надежность и идемпотентность

---

## Пример полного цикла

```
1. Order Service (Outbox):
   ┌─────────────────┐
   │ Создать заказ   │
   │ + outbox        │ ← Transaction Outbox
   └─────────────────┘
           │
           ▼
   ┌─────────────────┐
   │ Outbox Worker   │
   │ → RabbitMQ      │
   └─────────────────┘

2. RabbitMQ:
   ┌─────────────────┐
   │ order.created   │
   └─────────────────┘
           │
           ▼

3. Payment Service (Inbox):
   ┌─────────────────┐
   │ Получить событие│
   │ Проверить inbox │ ← Transaction Inbox
   │ Обработать      │
   │ + inbox         │
   └─────────────────┘
```

---

## Резюме

- **Transaction Outbox** гарантирует, что событие **будет опубликовано**
- **Transaction Inbox** гарантирует, что событие **обработается только один раз**

Оба паттерна критически важны для надежных микросервисных систем с асинхронной обработкой событий.

