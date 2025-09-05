from utils.env_loader import ensure_env_loaded
ensure_env_loaded()
import os
import time
import logging
from openai import OpenAI, APIError, RateLimitError, AuthenticationError, APITimeoutError, APIConnectionError

logger = logging.getLogger(__name__)

# 1) Сначала пробрасываем старую переменную в новую
os.environ["OPENAI_API_KEY"] = os.getenv("OPENAI_APIKEY", "")

# 2) Создаём клиента лениво, чтобы не падать на импорте
_client = None
def get_client():
    global _client
    if _client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("Missing OPENAI_APIKEY/OPENAI_API_KEY")
        _client = OpenAI(api_key=api_key)
    return _client

def ask_openai(prompt: str, system_prompt: str = "Ты ассистент иллюзиониста Арсения. Отвечай осмысленно, дружелюбно и кратко.", max_tokens: int = 150) -> str:
    try:
        client = get_client()
        start = time.time()
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo-0125",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            temperature=0,
            max_tokens=max_tokens,
            timeout=20
        )
        ans = resp.choices[0].message.content.strip()
        logger.info(f"[ask_openai] ✅ Ответ: {ans}")
        logger.info(f"[ask_openai] 🕒 {time.time() - start:.2f} сек")
        logger.info(f"[ask_openai] 📈 Токены: {resp.usage.total_tokens}")
        return ans
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
