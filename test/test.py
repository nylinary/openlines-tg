import requests
import json
import time
from datetime import datetime

B24_DOMAIN = "b24-gko4ik.bitrix24.ru"
ACCESS_TOKEN = '0d2893690080f35e008099000000001b000007617bcd270d530b198066d8b6f852d0ba'
CONNECTOR_ID = 'my_telegram_bot_1'  # ID коннектора
LINE_ID = '1234'  # ID открытой линии
import requests
import json
import time
from datetime import datetime

class TelegramBitrixConnector:
    def __init__(self, domain, access_token, line_id, connector_id, bot_token):
        self.domain = domain
        self.access_token = access_token
        self.line_id = line_id
        self.connector_id = connector_id
        self.bot_token = bot_token
        self.base_url = f"https://{domain}/rest"
        
    def register_connector(self):
        """Регистрация коннектора в Битрикс24"""
        url = f"{self.base_url}/imconnector.register"
        
        # SVG иконка Telegram
        telegram_icon = '''data:image/svg+xml;charset=US-ASCII,%3Csvg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"%3E%3Cpath fill="%2329A1E3" d="M12 0C5.373 0 0 5.373 0 12s5.373 12 12 12 12-5.373 12-12S18.627 0 12 0zm5.562 8.161l-2.466 11.625c-.184.831-.67.998-1.363.624l-3.76-2.77-1.814 1.746c-.2.2-.37.37-.757.37l.27-3.79 6.91-6.24c.3-.27-.07-.42-.46-.16l-8.54 5.38-3.68-1.15c-.8-.25-.82-.8.17-1.18l14.34-5.53c.66-.24 1.24.16 1.03 1.1z"/%3E%3C/svg%3E'''
        
        payload = {
            "auth": self.access_token,
            "ID": self.connector_id,
            "NAME": "Telegram Bot Connector",
            "ICON": {
                "DATA_IMAGE": telegram_icon,
                "COLOR": "#29A1E3",
                "SIZE": "100%",
                "POSITION": "center"
            },
            "ICON_DISABLED": {
                "DATA_IMAGE": telegram_icon,
                "COLOR": "#cccccc",
                "SIZE": "100%",
                "POSITION": "center"
            },
            "PLACEMENT_HANDLER": "https://your-server.com/bitrix-webhook"  # URL вашего вебхука
        }
        
        response = requests.post(url, json=payload)
        return response.json()
    
    def bind_events(self):
        """Подписка на события из Битрикс24"""
        url = f"{self.base_url}/event.bind"
        
        payload = {
            "auth": self.access_token,
            "event": "OnImConnectorMessageAdd",
            "handler": "https://your-server.com/bitrix-webhook"
        }
        
        response = requests.post(url, json=payload)
        return response.json()
    
    def send_message_to_bitrix(self, telegram_user_id, user_name, text, telegram_message_id):
        """
        Отправка сообщения из Telegram в Битрикс24
        Соответствует методу imconnector.send.messages из примера
        """
        url = f"{self.base_url}/imconnector.send.messages"
        
        # Формируем структуру как в примере из документации
        chat_id = f"tg:{telegram_user_id}"
        
        message_data = {
            "user": {
                "id": chat_id,
                "name": user_name
            },
            "chat": {
                "id": chat_id,
                "name": user_name,
                "url": f"https://t.me/{telegram_user_id}"  # опционально
            },
            "message": {
                "id": str(telegram_message_id),
                "date": int(time.time()),
                "text": text
            }
        }
        
        payload = {
            "auth": self.access_token,
            "CONNECTOR": self.connector_id,
            "LINE": self.line_id,
            "MESSAGES": [message_data]
        }
        
        response = requests.post(url, json=payload)
        result = response.json()
        
        # Сохраняем соответствие ID сообщений для подтверждения доставки
        if result.get('result'):
            self.save_message_mapping(telegram_message_id, result['result'])
        
        return result
    
    def handle_bitrix_webhook(self, data):
        """
        Обработка входящего вебхука от Битрикс24
        Соответствует файлу handler.php из примера
        """
        event = data.get('event')
        
        # Активация коннектора (приходит из интерфейса Битрикс24)
        if event == 'ONIMCONNECTORMESSAGEADD':
            # Это новое сообщение от оператора
            return self.handle_operator_message(data['data'])
        
        # Обработка PLACEMENT (при подключении коннектора в интерфейсе)
        elif data.get('PLACEMENT') == 'SETTING_CONNECTOR':
            return self.handle_connector_activation(data)
        
        return {"status": "ok"}
    
    def handle_operator_message(self, data):
        """Обработка сообщения от оператора"""
        # Проверяем, что сообщение для нашего коннектора
        if data.get('CONNECTOR') != self.connector_id:
            return {"status": "skip"}
        
        for message in data.get('MESSAGES', []):
            # Извлекаем Telegram ID из chat.id (формат "tg:123456789")
            chat_id = message['chat']['id']
            telegram_id = chat_id.replace('tg:', '')
            
            # Текст сообщения от оператора
            text = message['message']['text']
            
            # Отправляем в Telegram
            self.send_to_telegram(telegram_id, f"👨‍💼 Оператор: {text}")
            
            # Подтверждаем доставку (как в примере)
            self.confirm_delivery(data, message)
        
        return {"status": "ok"}
    
    def confirm_delivery(self, data, message):
        """
        Подтверждение доставки сообщения
        Соответствует методу imconnector.send.status.delivery из примера
        """
        url = f"{self.base_url}/imconnector.send.status.delivery"
        
        payload = {
            "auth": self.access_token,
            "CONNECTOR": self.connector_id,
            "LINE": self.line_id,
            "MESSAGES": [{
                "im": message.get('im'),
                "message": {
                    "id": [message['message']['id']]
                },
                "chat": {
                    "id": message['chat']['id']
                }
            }]
        }
        
        return requests.post(url, json=payload).json()
    
    def handle_connector_activation(self, data):
        """
        Активация коннектора из интерфейса Битрикс24
        Соответствует части из handler.php
        """
        options = json.loads(data.get('PLACEMENT_OPTIONS', '{}'))
        
        # Активируем коннектор
        url = f"{self.base_url}/imconnector.activate"
        payload = {
            "auth": self.access_token,
            "CONNECTOR": self.connector_id,
            "LINE": int(options.get('LINE')),
            "ACTIVE": int(options.get('ACTIVE_STATUS', 1))
        }
        
        result = requests.post(url, json=payload).json()
        
        if result.get('result'):
            # Сохраняем ID линии
            self.line_id = options.get('LINE')
            return {"status": "success"}
        
        return {"status": "error"}
    
    def send_to_telegram(self, chat_id, text):
        """Отправка сообщения в Telegram"""
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text
        }
        return requests.post(url, json=payload).json()
    
    def save_message_mapping(self, telegram_msg_id, bitrix_result):
        """Сохранение соответствия ID сообщений"""
        # Реализуйте сохранение в БД
        pass

# Использование
if __name__ == "__main__":
    connector = TelegramBitrixConnector(
        domain="b24-gko4ik.bitrix24.ru",
        access_token="0d2893690080f35e008099000000001b000007617bcd270d530b198066d8b6f852d0ba",
        line_id="1",  # ID вашей открытой линии
        connector_id="my_telegram_bot",
        bot_token="ваш_токен_telegram_бота"
    )
    
    # Шаг 1: Регистрируем коннектор
    print("Регистрация коннектора...")
    result = connector.register_connector()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    
    # Шаг 2: Подписываемся на события
    print("\nПодписка на события...")
    result = connector.bind_events()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    
    # Шаг 3: Тест отправки сообщения
    print("\nТест отправки сообщения...")
    result = connector.send_message_to_bitrix(
        telegram_user_id="123456789",
        user_name="Тестовый Пользователь",
        text="Привет! Это тестовое сообщение из Telegram",
        telegram_message_id=1
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))