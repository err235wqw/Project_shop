"""
Notification Service - Пример подписчика событий (Хореография)

Этот сервис подписывается на событие payment.processed из RabbitMQ
и отправляет уведомление клиенту.
"""
import asyncio
import json
import os

import aio_pika
from fastapi import FastAPI

app = FastAPI(title="Notification Service", version="1.0.0")


def _get_required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} must be set")
    return value


RABBITMQ_URL = _get_required_env("RABBITMQ_URL")
EXCHANGE_NAME = "shop_events"
QUEUE_NAME = "notification_queue"


# Имитация отправки уведомления
async def send_notification(order_id: int, customer_email: str, amount: float):
    """
    Имитация отправки email/SMS уведомления.
    В реальном проекте здесь был бы вызов email/SMS сервиса.
    """
    await asyncio.sleep(0.3)  # Имитация задержки
    
    print(f"[Notification Service] Отправлено уведомление для заказа #{order_id}")
    print(f"  Email: {customer_email}")
    print(f"  Сумма: {amount}")
    print(f"  Текст: 'Ваш заказ #{order_id} успешно оплачен!'")
    
    return {
        "order_id": order_id,
        "email": customer_email,
        "sent": True,
    }


async def publish_event(routing_key: str, payload: dict):
    """Публикует событие в RabbitMQ"""
    connection = await aio_pika.connect_robust(RABBITMQ_URL)
    channel = await connection.channel()
    exchange = await channel.declare_exchange(
        EXCHANGE_NAME, aio_pika.ExchangeType.TOPIC, durable=True
    )
    
    body = json.dumps(payload).encode("utf-8")
    await exchange.publish(aio_pika.Message(body=body), routing_key=routing_key)
    
    await connection.close()


async def consume_payment_processed():
    """
    Подписчик на событие payment.processed (ХОРЕОГРАФИЯ)
    
    Этот воркер:
    1. Подписывается на очередь notification_queue
    2. Получает события payment.processed
    3. Отправляет уведомление клиенту
    4. Публикует notification.sent
    """
    while True:
        try:
            connection = await aio_pika.connect_robust(RABBITMQ_URL)
            channel = await connection.channel()
            exchange = await channel.declare_exchange(
                EXCHANGE_NAME, aio_pika.ExchangeType.TOPIC, durable=True
            )
            queue = await channel.declare_queue(QUEUE_NAME, durable=True)
            await queue.bind(exchange, routing_key="payment.processed")
            
            print("[Notification Service] Подписан на payment.processed")
            
            async with queue.iterator() as queue_iter:
                async for message in queue_iter:
                    async with message.process():
                        try:
                            event = json.loads(message.body)
                            order_id = event["order_id"]
                            payment_id = event["payment_id"]
                            amount = event["amount"]
                            customer_email = event.get("customer_email", "")
                            
                            print(f"[Notification Service] Получен платеж {payment_id} для заказа #{order_id}")
                            
                            # Отправить уведомление
                            result = await send_notification(order_id, customer_email, amount)
                            
                            # Публиковать событие
                            await publish_event("notification.sent", {
                                "order_id": order_id,
                                "email": customer_email,
                            })
                            print(f"[Notification Service] Уведомление отправлено для заказа #{order_id}")
                            
                        except Exception as exc:  # noqa: BLE001
                            print(f"[Notification Service] Ошибка обработки: {exc}")
                            
        except Exception as exc:  # noqa: BLE001
            print(f"[Notification Service] Ошибка подключения: {exc}")
            await asyncio.sleep(5)


@app.on_event("startup")
async def on_startup():
    """Запускаем подписчика в фоне"""
    asyncio.create_task(consume_payment_processed())


@app.get("/health")
async def health():
    return {"status": "ok", "service": "notification_service"}


@app.get("/notifications")
async def list_notifications():
    """Для примера: список уведомлений (в реальном проекте была бы БД)"""
    return {"notifications": []}


@app.post("/notifications/send")
async def send_notification_endpoint(request: dict):
    """
    Эндпоинт для ОРКЕСТРАЦИИ (вызывается синхронно из order_service)
    
    В режиме хореографии этот эндпоинт не используется,
    так как notification_service подписывается на события из RabbitMQ.
    """
    order_id = request.get("order_id")
    customer_email = request.get("customer_email", "")
    amount = request.get("amount", 0.0)
    
    result = await send_notification(order_id, customer_email, amount)
    
    return {
        "success": result["sent"],
        "order_id": order_id,
        "email": customer_email,
    }

