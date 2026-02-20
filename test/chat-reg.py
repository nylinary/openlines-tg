curl -X POST "https://b24-gko4ik.bitrix24.ru/rest/imbot.register.json" \
  -H "Authorization: Bearer 410597690080f35e008099000000001b000007704b6479264d692856a8f4578b446846" \
  -H "Content-Type: application/json" \
  -d '{
        "CODE": "openlines_echo_bot",
        "TYPE": "B",
        "EVENT_HANDLER": "https://openlines-tg-production.up.railway.app/b24/imbot/events",
        "PROPERTIES": {
          "NAME": "openlines echo",
          "LAST_NAME": "",
          "COLOR": "AQUA",
          "EMAIL": "bot@your-domain.com",
          "PERSONAL_BIRTHDAY": "2024-01-01",
          "WORK_POSITION": "AI assistant",
          "PERSONAL_WWW": "https://openlines-tg-production.up.railway.app"
        },
        "OPENLINE": "Y"
      }'



