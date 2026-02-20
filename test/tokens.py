import requests
import json

def refresh_bitrix_tokens(refresh_token, client_id, client_secret):
    """
    Обновление access_token с помощью refresh_token
    
    Args:
        refresh_token: старый refresh_token
        client_id: код вашего локального приложения
        client_secret: секретный ключ приложения
    
    Returns:
        dict: новые токены или None при ошибке
    """
    url = "https://oauth.bitrix.info/oauth/token/"
    
    params = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token
    }
    
    try:
        response = requests.get(url, params=params)
        result = response.json()
        
        if response.status_code == 200:
            print("✅ Токены успешно обновлены")
            return result
        else:
            print(f"❌ Ошибка: {result.get('error_description', 'Неизвестная ошибка')}")
            return None
    except Exception as e:
        print(f"❌ Ошибка запроса: {e}")
        return None

# Использование
new_tokens = refresh_bitrix_tokens(
    refresh_token="ed97ba690080f35e008099000000001b000007d2a7082727b9bc5b88a1a1a5ec94ab80",
    client_id="local.6992ed7cce7125.09474713",
    client_secret="NJDySrCkZBwCb5S6Ss6RiMM47sN6qNuFxbfvrmoFs6iVMhy5lr"
)

if new_tokens:
    print("Новый access_token:", new_tokens['access_token'])
    print("Новый refresh_token:", new_tokens['refresh_token'])
    print("Срок действия (сек):", new_tokens['expires_in'])