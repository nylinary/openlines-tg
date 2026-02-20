import requests
import json

B24_DOMAIN = "b24-gko4ik.bitrix24.ru"
ACCESS_TOKEN = '0d2893690080f35e008099000000001b000007617bcd270d530b198066d8b6f852d0ba'

def get_openlines_list_alternative():
    """
    Получение списка открытых линий альтернативными методами
    """
    
    # Пробуем разные методы
    methods = [
        "imopenlines.config.get",           # получить конкретную линию
        "imopenlines.network.list",          # список сетевых линий
        "imopenlines.operator.lines.get",    # линии оператора
        "im.recent.list",                    # последние диалоги (там есть линии)
        "im.dialog.get"                       # получить диалоги
    ]
    
    for method in methods:
        print(f"\n📋 Пробуем метод: {method}")
        url = f"https://{B24_DOMAIN}/rest/{method}"
        
        payload = {
            "auth": ACCESS_TOKEN
        }
        
        # Для некоторых методов нужны дополнительные параметры
        if method == "imopenlines.config.get":
            # Попробуем получить линию с ID 1
            payload["CONFIG_ID"] = 1
        elif method == "imopenlines.operator.lines.get":
            payload["USER_ID"] = 1  # ID оператора
        
        try:
            response = requests.post(url, json=payload)
            result = response.json()
            
            print(f"Статус: {response.status_code}")
            
            if response.status_code == 200 and 'result' in result:
                print(f"✅ Успешно!")
                if result['result']:
                    print(f"Результат: {json.dumps(result['result'], indent=2, ensure_ascii=False)[:500]}...")
                else:
                    print("Результат пустой")
                return result
            else:
                print(f"❌ Ошибка: {result.get('error_description', 'Неизвестная ошибка')}")
                
        except Exception as e:
            print(f"❌ Ошибка запроса: {e}")

def get_lines_from_recent():
    """
    Получение информации о линиях из последних диалогов
    """
    print("\n📋 Получаем последние диалоги...")
    url = f"https://{B24_DOMAIN}/rest/im.recent.list"
    
    payload = {
        "auth": ACCESS_TOKEN,
        "SKIP_OPENLINES": "N",  # не пропускать открытые линии
        "ONLY_OPENLINES": "Y"    # только открытые линии
    }
    
    try:
        response = requests.post(url, json=payload)
        result = response.json()
        
        print(f"Статус: {response.status_code}")
        
        if response.status_code == 200:
            items = result.get('result', [])
            print(f"Найдено диалогов: {len(items)}")
            
            lines_info = {}
            for item in items:
                if item.get('type') == 'chat':
                    chat = item.get('chat', {})
                    if chat.get('type') == 'lines':
                        line_id = chat.get('entity_id')
                        line_name = chat.get('name')
                        if line_id not in lines_info:
                            lines_info[line_id] = {
                                'name': line_name,
                                'chat_id': item.get('id'),
                                'dialog_id': item.get('dialog_id')
                            }
            
            if lines_info:
                print(f"\n✅ Найдены линии в диалогах:")
                for line_id, info in lines_info.items():
                    print(f"\n  Линия ID: {line_id}")
                    print(f"  Название: {info['name']}")
                    print(f"  Chat ID: {info['chat_id']}")
                    print(f"  Dialog ID: {info['dialog_id']}")
                return lines_info
            else:
                print("❌ Линии не найдены в последних диалогах")
                print("Сначала создайте тестовый диалог в интерфейсе Битрикс24")
        else:
            print(f"❌ Ошибка: {result.get('error_description', 'Неизвестная ошибка')}")
            
    except Exception as e:
        print(f"❌ Ошибка запроса: {e}")
    
    return None

def get_available_lines_interface():
    """
    Получение информации через пользовательский интерфейс (консольный вариант)
    """
    print("\n📋 Если API не работает, можно найти ID линии в интерфейсе:")
    print("\nСпособ 1: В URL при открытии линии")
    print("1. Откройте Контакт-центр в Битрикс24")
    print("2. Нажмите на нужную открытую линию")
    print("3. Посмотрите URL в браузере")
    print("   Там будет что-то вроде: /contact_center/openlines/connector/line/123/")
    print("   Число 123 - это ID линии")
    
    print("\nСпособ 2: Через консоль браузера")
    print("1. Откройте Контакт-центр")
    print("2. Нажмите F12 (инструменты разработчика)")
    print("3. Перейдите на вкладку Console")
    print("4. Выполните команду:")
    print("   BX24.callMethod('imopenlines.config.list', {}, function(r) { console.log(r.data()) })")
    
    return None

def check_specific_line(line_id):
    """
    Проверка конкретной линии
    """
    print(f"\n📋 Проверяем линию с ID {line_id}...")
    url = f"https://{B24_DOMAIN}/rest/imopenlines.config.get"
    
    payload = {
        "auth": ACCESS_TOKEN,
        "CONFIG_ID": line_id
    }
    
    try:
        response = requests.post(url, json=payload)
        result = response.json()
        
        print(f"Статус: {response.status_code}")
        
        if response.status_code == 200:
            if 'result' in result and result['result']:
                print(f"✅ Линия найдена!")
                line = result['result']
                print(f"  ID: {line.get('ID')}")
                print(f"  Название: {line.get('NAME')}")
                print(f"  Активна: {line.get('ACTIVE')}")
                return True
            else:
                print(f"❌ Линия не найдена")
        else:
            print(f"❌ Ошибка: {result.get('error_description', 'Неизвестная ошибка')}")
    except Exception as e:
        print(f"❌ Ошибка запроса: {e}")
    
    return False

if __name__ == "__main__":
    print("🚀 Поиск открытых линий в Битрикс24\n")
    print("="*60)
    
    # Сначала пробуем альтернативные методы
    get_openlines_list_alternative()
    
    print("\n" + "="*60)
    
    # Пробуем найти линии в последних диалогах
    lines = get_lines_from_recent()
    
    print("\n" + "="*60)
    
    # Если нашли линии, проверяем их
    if lines:
        for line_id in lines.keys():
            check_specific_line(line_id)
    
    print("\n" + "="*60)
    
    # Показываем как найти ID вручную
    get_available_lines_interface()
    
    print("\n" + "="*60)
    print("\n💡 Если ничего не нашлось, создайте тестовый диалог:")
    print("1. Войдите в Битрикс24 как клиент (или откройте в режиме инкогнито)")
    print("2. Напишите в открытую линию сообщение")
    print("3. Запустите этот скрипт снова - он найдет линию")