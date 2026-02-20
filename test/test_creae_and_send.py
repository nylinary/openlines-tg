import requests
import json
import time
import uuid
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
        Создает НОВЫЙ чат для каждого сообщения (если chat_id уникальный)
        """
        url = f"{self.base_url}/imconnector.send.messages"
        
        # Генерируем УНИКАЛЬНЫЙ ID чата для каждого сообщения
        # Используем timestamp + random для гарантии уникальности
        unique_suffix = f"{int(time.time()*1000)}_{uuid.uuid4().hex[:8]}"
        chat_id = f"tg_test_{unique_suffix}"
        
        message_data = {
            "user": {
                "id": chat_id,  # Уникальный пользователь
                "name": f"{user_name} ({unique_suffix[-6:]})"  # Уникальное имя
            },
            "chat": {
                "id": chat_id,  # Уникальный чат
                "name": f"Тестовый чат {unique_suffix[-6:]}"
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
        
        print(f"\n📤 Создаем НОВЫЙ чат и отправляем сообщение...")
        print(f"  LINE: {self.line_id}")
        print(f"  CONNECTOR: {self.connector_id}")
        print(f"  Новый Chat ID: {chat_id}")
        print(f"  Пользователь: {message_data['user']['name']}")
        print(f"  Текст: {text}")
        
        response = requests.post(url, json=payload)
        result = response.json()
        
        # Выводим информацию о созданном чате
        if result.get('result') and result['result'].get('DATA', {}).get('RESULT'):
            session = result['result']['DATA']['RESULT'][0].get('session', {})
            if session:
                print(f"\n  ✅ Создан новый чат в Битрикс24:")
                print(f"     Session ID: {session.get('ID')}")
                print(f"     Chat ID в Битрикс24: {session.get('CHAT_ID')}")
        
        return result
    
    def send_multiple_test_messages(self, count=3):
        """
        Отправка нескольких тестовых сообщений, каждое в новый чат
        """
        print(f"\n🚀 Отправляем {count} тестовых сообщений, каждое в НОВЫЙ чат")
        print("="*60)
        
        results = []
        for i in range(count):
            print(f"\n--- Тест #{i+1} ---")
            result = self.send_message_to_bitrix(
                telegram_user_id=f"test_user_{i}",
                user_name=f"Тестовый Пользователь {i+1}",
                text=f"Тестовое сообщение #{i+1} в {datetime.now().strftime('%H:%M:%S')}",
                telegram_message_id=int(time.time()*1000) + i
            )
            results.append(result)
            time.sleep(1)  # Пауза между сообщениями
        
        return results
    
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
    
    def get_recent_chats(self, limit=10):
        """Получение списка последних чатов"""
        url = f"{self.base_url}/im.recent.list"
        
        payload = {
            "auth": self.access_token,
            "SKIP_OPENLINES": "N",
            "ONLY_OPENLINES": "Y"
        }
        
        print(f"\n📋 Получаем последние чаты...")
        response = requests.post(url, json=payload)
        result = response.json()
        
        if response.status_code == 200:
            items = result.get('result', [])
            print(f"Найдено диалогов: {len(items)}")
            
            # Показываем только чаты с tg_test (наши тестовые)
            test_chats = []
            for item in items:
                if item.get('type') == 'chat':
                    chat = item.get('chat', {})
                    chat_id = chat.get('entity_id') or chat.get('name')
                    if 'tg_test' in str(chat_id):
                        test_chats.append({
                            'chat_id': item.get('id'),
                            'name': chat.get('name'),
                            'last_message': item.get('message', {}).get('text')
                        })
            
            if test_chats:
                print(f"\n✅ Найдено тестовых чатов: {len(test_chats)}")
                for i, chat in enumerate(test_chats, 1):
                    print(f"\n  Чат #{i}:")
                    print(f"    ID в Битрикс24: {chat['chat_id']}")
                    print(f"    Название: {chat['name']}")
                    print(f"    Последнее сообщение: {chat['last_message'][:50]}...")
            else:
                print("❌ Тестовые чаты не найдены")
            
            return test_chats
        else:
            print(f"❌ Ошибка: {result.get('error_description', 'Unknown error')}")
            return []
    
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
            # Для тестовых чатов просто логируем
            if 'tg_test' in chat_id:
                print(f"\n📨 Получен ответ от оператора в тестовый чат {chat_id}")
                print(f"   Сообщение: {message['message']['text']}")
            
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
            print(f"\n⚠️ [Telegram] Сообщение для {chat_id}: {text}")
            return None
            
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text
        }
        return requests.post(url, json=payload).json()
    
    def save_message_mapping(self, telegram_msg_id, bitrix_result):
        """Сохранение соответствия ID сообщений"""
        pass


# Использование
if __name__ == "__main__":
    # Конфигурация
    DOMAIN = "b24-gko4ik.bitrix24.ru"
    ACCESS_TOKEN = 'e61794690080f35e008099000000001b000007ba4a1f1a8e1b9ea50648ab8a26822c6a'
    LINE_ID = "1"  # Базовая открытая линия
    CONNECTOR_ID = "my_telegram_bot"
    BOT_TOKEN = None  # Сюда токен Telegram бота
    
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
    
    # Проверяем линию
    print("\n" + "="*60)
    line_ok = connector.check_line()
    
    if line_ok:
        # Регистрируем коннектор (если еще не зарегистрирован)
        print("\n" + "="*60)
        result = connector.register_connector()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        
        # Активируем коннектор
        print("\n" + "="*60)
        result = connector.activate_connector()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        
        # Подписываемся на события
        print("\n" + "="*60)
        result = connector.bind_events()
        print(json.dumps(result, indent=2, ensure_ascii=False))
        
        # Отправляем несколько тестовых сообщений (каждое в новый чат)
        print("\n" + "="*60)
        results = connector.send_multiple_test_messages(count=5)
        
        # Проверяем созданные чаты
        print("\n" + "="*60)
        test_chats = connector.get_recent_chats()
        
        print("\n" + "="*60)
        print(f"\n✅ Всего создано тестовых чатов: {len(test_chats)}")
        print("✅ Скрипт завершен!")
    else:
        print("\n❌ Линия не найдена. Проверьте LINE_ID")