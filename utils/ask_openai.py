import os
import time
import logging
from openai import (
    OpenAI,
    APIError,
    RateLimitError,
    AuthenticationError,
    APITimeoutError,
    APIConnectionError,
)

logger = logging.getLogger(__name__)
client = OpenAI(api_key=os.getenv("OPENAI_APIKEY"))

def ask_openai(prompt: str, system_prompt: str = "Ты ассистент иллюзиониста Арсения. Отвечай осмысленно, дружелюбно и кратко.", max_tokens: int = 150) -> str:
    try:
        start = time.time()
        response = client.chat.completions.create(
            model="gpt-3.5-turbo-0125",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            temperature=0,
            max_tokens=max_tokens,
            timeout=20
        )
        end = time.time()
        answer = response.choices[0].message.content.strip()
        logger.info(f"[ask_openai] ✅ Ответ: {answer}")
        logger.info(f"[ask_openai] 🕒 Время генерации: {end - start:.2f} сек")
        logger.info(f"[ask_openai] 📈 Токенов использовано: {response.usage.total_tokens}")
        return answer

    except AuthenticationError as e:
        logger.error(f"[ask_openai] ❌ Авторизация: {e}")
        return "Ошибка авторизации"

    except RateLimitError:
         logger.warning("[ask_openai] ⚠️ Лимит запросов")
         return "Превышен лимит запросов"

    except (APITimeoutError, APIConnectionError):
        logger.warning("[ask_openai] ⏰ Таймаут / нет связи")
        return "Таймаут"

    except APIError as e:
        logger.error(f"[ask_openai] ⛔ Ошибка API: {e}")
        return "API ошибка"

    except Exception as e:
        logger.exception(f"[ask_openai] 💥 Неизвестная ошибка: {e}")
        return "Неизвестная ошибка"
