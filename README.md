1) Быстрый старт
# Создать venv
python -m venv .venv

# Активация venv
.\.venv\Scripts\Activate.ps1

# Обновление менеджера пакетов pip до последней версии
python -m pip install --upgrade pip

# Установка зависимостей
pip install -r requirements.txt

# Запуск проекта
python app.py

# Проброс туннеля для вебхука при локальном запуске
npx cloudflared tunnel --url http://127.0.0.1:5000 (делать в отдельном окне PowerShell)

2) Запуск тестов
# Интеграционный тест Яндекс Cloud S3
pytest -vv tests/integration/test_schedule_s3_integration.py -s

# Интеграционный тест Meta API
pytest -q tests/integration/test_waba_io.py -s

# Дымо-тест (тест на падение при старте)
pytest -vv -ra tests/unit/test_router_smoke.py -s

# Тест логики расписания
pytest -q tests/unit/test_schedule_rule.py -s

# Тест безопасности вебхука
pytest -q tests/unit/test_webhook_security.py -s

# Тест классификации типа шоу
pytest -q tests/unit/test_classification.py -s

# Тест повторных касаний
pytest -q tests/unit/test_reminders.py -s

# Тест автоопределения языка обращения
pytest -q tests/unit/test_lang_detect.py -s

# Собрать тесты, но не выполнять
pytest --collect-only -q

# Выполнить тест до первой ошибки
pytest --maxfail=1 -q

# Запустить тесты по ключевому слову
pytest -q -k "webhook or reminders" -s

3) Устройство проекта бота Bot_MVP
- app.py - главный файл для старта Flask;
- router.py - маршрутизация сообщений между блоками сценария;
- logger.py - настройка и использование логирования по проекту;
- rollover_scheduler.py - модуль работы с расписанием для переноса и очистки задач расписания;
- .env - файл с переменными окружения;
- .gitignore - перечисление файлов и папок, которые не нужно добавлять в git при коммите;
- config.py - единая конфигурация проекта;
- pytest.ini - файл конфигурирования глобальных параметров для тестов;
- requirements.txt - перечисление зависимостей, используемых в проекте;
- DEVLOG.md - журнал с описанием процесса разработки;
- README.md - руководство по работе с проектом;

- blocks/ - папка с блоками, реализующими логику работы соответствующих этапов проекта;
- prompts/ - папка с промптами: глобальный, промпты этапов сценария, промпты повторных касаний, промпты сбора данных на этапе 3;
- routes/ - папка для хранения роутов - обработчиков HTTP-запросов:
            _init_.py - обязательный файл для превращения папки в пакет python;
            admin_routes.py - админка для обновления токена WhatsApp;
            debug_mem_route.py - технический маршрут для проверки потребления памяти проектом.
            debug_tail_route.py - технический маршрут для просмотра последних строк из лог-файла непосредственно в браузере;
            debug_upload_log_route.py - служебный маршрут для загрузки лог-файла на сервер;
            home_route.py - маршрут для проверки, что сервер живой и отвечает;
            ping_route.py - минимальный технический маршрут для проверки доступности сервера;
            whatsapp_route.py - обработка всех запросов от Meta API;
- state/ - папка для хранения файлов состояния:
           _init_.py - обязательный файл для превращения папки в пакет python;
           state.py - хранение и обновление состояния диалога;
- templates/ - папка для хранения html-шаблонов
           token.html - шаблон админки для обновления токена WhatsApp;
- tests/ - папка для автотестов для проверки функциональности проекта:
           integration/ - папка для хранения интеграционных тестов:
                          test_24h_window.py - файл с тестами отправки сообщений только внутри "окна" 24 часа от последнего входящего сообщения клиента (требование Meta);
                          test_handover_owner.py - файл с тестами корректной передачи управления человеку во всех описанных в ТЗ ситуациях;
                          test_materials_end2end.py - файл с интеграционными тестами получения материалов о шоу из Яндекс Cloud S3;
                          test_notion_upsert.py - файл с тестами передачи данных в CRM;
                          test_openai_gpt.py - файл с тестами интеграции с OpenAI API;
                          test_openai_whisper.py - файл с тестами интеграции с OpenAI Whisper для транскрибации голосовых сообщений;
                          test_schedule_s3_integration.py - файл с тестами интеграции с Яндекс Cloud S3;
                          test_waba_io.py - файл с тестами интеграции с Meta API для приема входящих и отправки исходящих сообщений.
           unit/ - папка с тестами отдельных функций и модулей:
                   test_classification.py - файл с тестами классификации типа шоу в блоке 2;
                   test_lang_detect.py - файл с тестами автоопределения языка обращения;
                   test_reminders.py - файл с тестами повторных касаний в блоках 2 и 3;
                   test_router_smoke.py - файл с дымо-тестами на падения при старте;
                   test_schedule_rule.py - файл с тестами ведения и расписания выступлений и проверки доступности слотов;
                   test_webhook_security.py - файл с тестами безопасности вебхука;
            conftest.py - спциальный файл для хранения фикстур для тестов;
- utils/ - папка для хранения файлов, реализующих отдельные функции бота:
           _init_.py - обязательный файл для превращения папки в пакет python;
           ai_extract.py - формирование шаблона для хранения в state информации по выступлению в виде JSON;
           ask_openai.py - отправка запроса к OpenAI API и получение ответа;
           cleanup.py - обслуживающие функции (очистка, мониторинг и логирование памяти);
           constants.py - хранение всех необходимых боту констант;
           env_check.py - проверка, что все нужные переменные окружения загружены;
           env_loader.py - корректная загрузка переменных окружения из .env;
           incoming_message.py - функции обработки входящих сообщений разного типа;
           lang_detect.py - автоматическое определение языка обращения;
           lang_prompt.py - формирование ответа клиенту на языке обращения;
           materials.py - обработка материалов о выступлении, загруженных в Яндекс Cloud S3 и подготовка к отправке клиенту;
           outgoing_message.py - отправка исходящих сообщений в Meta API;
           process_and_compress_videos_from_s3.py - автоматическая загрузка видео из Яндекс Cloud S3, сжатие до требуемого Meta размера и сохранение обратно в Яндекс Cloud S3;
           reminder_engine.py - запуск и управление планировщиком APScheduler;
           s3_upload.py - загрузка фото именинника в Яндекс Cloud S3;
           schedule.py - работа с расписанием в Яндекс Cloud S3;
           structured.py - формирование шаблона информации о заявке для передачи Арсению и в CRM;
           supabase_token.py - работа с Supabase: загрузка, сохранение токена WhatsApp, пинг Supabase;
           telegram_alert.py - отправка уведомления в Telegram об истечении срока годности токена WhatsApp;
           token_manager.py - менеджер токенов WhatsApp (следит за наличием и актуальностью токена);
           upload_materials_to_meta_and_update_registry.py - синхронизация материалов о выступлении между Meta и Яндекс Cloud S3;
           waba_guard.py - защитные функции для Meta API согласно ТЗ: проверка подписи и заголовков, защита от дублирования, идемпотентность и т.д.;
           wants_handover_ai - ИИ-классификатор необходимости передачи управления человеку;
           whatsapp_senders.py - модуль отправки сообщений разных типов через Meta API.

4) Структура папки с проектом
Bot_MVP/
  app.py
  config.py
  logger.py
  requirements.txt
  router.py
  rollover_scheduler.py
  pytest.ini
  .gitignore
  DEVLOG.md
  README.md

  state/
    state.py

  blocks/
    _init_.py
    block_01.py                # приветствие + языковая вилка
    block_02.py                # классификация + постановка напоминаний
    block_03a.py               # сбор (детское)
    block_03b.py               # сбор (взрослое)
    block_03c.py               # сбор (семейное, поддержка no_celebrant)
    block_03d.py               # нестандартное → сразу хендовер
    block_04.py               # материалы (КП + видео <16 МБ)
    block_05.py               # handover Арсению (чанки ≤4096, пауза 1с)
    block_06.py               # CRM (Notion, 5 ретраев по 10 мин)

  prompts/
    block01_prompt.txt
    block02_prompt.txt
    block02_reminder_1_prompt.txt
    block02_reminder_2_prompt.txt
    block03_reminder_1_prompt.txt
    block03_reminder_2_prompt.txt
    block03a_prompt.txt
    block03a_data_prompt.txt
    block03b_prompt.txt
    block03b_data_prompt.txt
    block03c_prompt.txt
    block03c_data_prompt.txt
    block03d_prompt.txt
    block04_prompt.txt  
    block05_prompt.txt
    global_prompt.txt

  utils/
    _init_.py
    ask_openai.py
    ai_extract.py
    cleanup.py
    constants.py
    env_check.py
    env_loader.py
    incoming_message.py
    lang_detect.py
    lang_prompt.py
    materials.py 
    outgoing_message.py
    process_and_compress_videos_from_s3.py
    reminder_engine.py
    s3_upload.py
    schedule.py
    structured.py
    supabase_token.py
    telegram_alert.py
    token_manager.py
    upload_materials_to_meta_and_update_registry.py
    waba_guard.py
    wants_handover_ai.py
    whatsapp_senders.py
  routes/
    _init_.py
    admin_routes.py
    debug_mem_route.py
    debug_tail_route.py
    debug_upload_log_route.py
    home_route.py
    ping_route.py
    webhook_route.py
  templates/
    token.html
  tests/
    conftest.py
    integration/
      test_24h_window.py
      test_handover_owner.py
      test_materials_end2end.py
      test_notion_upsert.py
      test_openai_gpt.py
      test_openai_whisper.py
      test_schedule_s3_integration.py
      test_waba_io.py
    unit/
      test_classification.py
      test_lang_detect.py
      test_reminders.py
      test_router_smoke.py
      test_schedule_rule.py
      test_webhook_security.py
    

