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
        
        # SVG иконка Telegram (сжатая)
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
            "PLACEMENT_HANDLER": "https://your-server.com/bitrix-webhook"
        }
        
        print(f"📝 Регистрируем коннектор с ID: {self.connector_id}")
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
        
        print("\n📝 Подписываемся на события...")
        response = requests.post(url, json=payload)
        return response.json()
    
    def activate_connector(self):
        """Активация коннектора для линии"""
        url = f"{self.base_url}/imconnector.activate"
        
        payload = {
            "auth": self.access_token,
            "CONNECTOR": self.connector_id,
            "LINE": int(self.line_id),
            "ACTIVE": 1
        }
        
        print(f"\n🔌 Активируем коннектор для линии {self.line_id}...")
        response = requests.post(url, json=payload)
        return response.json()
    
    def send_message_to_bitrix(self, telegram_user_id, user_name, text, telegram_message_id):
        """
        Отправка сообщения из Telegram в Битрикс24
        """
        url = f"{self.base_url}/imconnector.send.messages"
        
        chat_id = f"tg:{telegram_user_id}"
        
        message_data = {
            "user": {
                "id": chat_id,
                "name": user_name
            },
            "chat": {
                "id": chat_id,
                "name": user_name
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
            "LINE": int(self.line_id),
            "MESSAGES": [message_data]
        }
        
        print(f"\n📤 Отправляем сообщение в Битрикс24...")
        print(f"  LINE: {self.line_id}")
        print(f"  CONNECTOR: {self.connector_id}")
        print(f"  Chat: {chat_id}")
        print(f"  Text: {text}")
        
        response = requests.post(url, json=payload)
        return response.json()
    
    def check_line(self):
        """Проверка существования и активности линии"""
        url = f"{self.base_url}/imopenlines.config.get"
        
        payload = {
            "auth": self.access_token,
            "CONFIG_ID": int(self.line_id)
        }
        
        print(f"\n🔍 Проверяем линию ID: {self.line_id}")
        response = requests.post(url, json=payload)
        result = response.json()
        
        if response.status_code == 200 and result.get('result'):
            line = result['result']
            print(f"✅ Линия найдена:")
            print(f"  Название: {line.get('LINE_NAME')}")
            print(f"  Активна: {line.get('ACTIVE')}")
            return True
        else:
            print(f"❌ Линия не найдена: {result.get('error_description', 'Unknown error')}")
            return False
    
    def handle_bitrix_webhook(self, data):
        """Обработка входящего вебхука от Битрикс24"""
        event = data.get('event')
        
        if event == 'ONIMCONNECTORMESSAGEADD':
            return self.handle_operator_message(data.get('data', {}))
        elif data.get('PLACEMENT') == 'SETTING_CONNECTOR':
            return self.handle_connector_activation(data)
        
        return {"status": "ok"}
    
    def handle_operator_message(self, data):
        """Обработка сообщения от оператора"""
        if data.get('CONNECTOR') != self.connector_id:
            return {"status": "skip"}
        
        for message in data.get('MESSAGES', []):
            chat_id = message['chat']['id']
            telegram_id = chat_id.replace('tg:', '')
            text = message['message']['text']
            
            # Отправляем в Telegram
            self.send_to_telegram(telegram_id, f"👨‍💼 Оператор: {text}")
            
            # Подтверждаем доставку
            self.confirm_delivery(message)
        
        return {"status": "ok"}
    
    def confirm_delivery(self, message):
        """Подтверждение доставки сообщения"""
        url = f"{self.base_url}/imconnector.send.status.delivery"
        
        payload = {
            "auth": self.access_token,
            "CONNECTOR": self.connector_id,
            "LINE": int(self.line_id),
            "MESSAGES": [{
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
        """Активация коннектора из интерфейса"""
        options = json.loads(data.get('PLACEMENT_OPTIONS', '{}'))
        
        url = f"{self.base_url}/imconnector.activate"
        payload = {
            "auth": self.access_token,
            "CONNECTOR": self.connector_id,
            "LINE": int(options.get('LINE', self.line_id)),
            "ACTIVE": int(options.get('ACTIVE_STATUS', 1))
        }
        
        result = requests.post(url, json=payload).json()
        
        if result.get('result'):
            self.line_id = str(options.get('LINE', self.line_id))
            return {"status": "success"}
        
        return {"status": "error"}
    
    def send_to_telegram(self, chat_id, text):
        """Отправка сообщения в Telegram"""
        if not self.bot_token:
            print("⚠️ bot_token не указан, пропускаем отправку в Telegram")
            return None
            
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text
        }
        return requests.post(url, json=payload).json()
    
    def save_message_mapping(self, telegram_msg_id, bitrix_result):
        """Сохранение соответствия ID сообщений"""
        # TODO: Реализуйте сохранение в БД
        pass


# Использование
if __name__ == "__main__":
    # Конфигурация
    DOMAIN = "b24-gko4ik.bitrix24.ru"
    ACCESS_TOKEN = '0d2893690080f35e008099000000001b000007617bcd270d530b198066d8b6f852d0ba'
    LINE_ID = "1"  # ID из imopenlines.config.get
    CONNECTOR_ID = "my_telegram_bot"  # Уникальный ID коннектора
    BOT_TOKEN = None  # Сюда вставьте токен вашего Telegram бота
    
    print("🚀 Запуск Telegram-Bitrix24 коннектора")
    print("="*60)
    
    # Создаем экземпляр коннектора
    connector = TelegramBitrixConnector(
        domain=DOMAIN,
        access_token=ACCESS_TOKEN,
        line_id=LINE_ID,
        connector_id=CONNECTOR_ID,
        bot_token=BOT_TOKEN
    )
    
    # Шаг 1: Проверяем линию
    print("\n" + "="*60)
    line_ok = connector.check_line()
    
    if line_ok:
        # Шаг 2: Регистрируем коннектор
        print("\n" + "="*60)
        result = connector.register_connector()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        
        # Шаг 3: Активируем коннектор
        print("\n" + "="*60)
        result = connector.activate_connector()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        
        # Шаг 4: Подписываемся на события
        print("\n" + "="*60)
        result = connector.bind_events()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        
        # Шаг 5: Тест отправки сообщения
        print("\n" + "="*60)
        result = connector.send_message_to_bitrix(
            telegram_user_id="123456789",
            user_name="Тестовый Пользователь",
            text=f"Тестовое сообщение в {datetime.now().strftime('%H:%M:%S')}",
            telegram_message_id=int(time.time())
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        
        print("\n" + "="*60)
        if result.get('result'):
            print("✅ УСПЕХ! Сообщение отправлено в Битрикс24")
        else:
            print("❌ Ошибка отправки. Проверьте сообщение выше")
    else:
        print("\n❌ Линия не найдена. Проверьте LINE_ID")