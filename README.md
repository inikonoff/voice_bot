# 🤖 iГрамотей v5

**iГрамотей** — Telegram-бот для интеллектуальной обработки текста. Транскрибирует голосовые сообщения и кружочки, распознаёт текст с изображений (OCR), читает документы и веб-страницы, извлекает субтитры YouTube — и предлагает несколько режимов обработки с экспортом результата.

---

## 🚀 Возможности

### 🎙️ Транскрибация
- **Голосовые сообщения** и **кружочки** — распознавание речи через Whisper (Groq)
- Ротация пула API-ключей с обработкой Rate Limit

### 📄 Работа с файлами и изображениями
- **OCR** — распознавание текста с фото и скриншотов через Groq Vision
- **PDF** — извлечение текста и таблиц через `pdfplumber` (async, не блокирует event loop)
- **DOCX** — чтение документов Word через `python-docx`
- **TXT / LOG / RST / TEX** — UTF-8 (в т.ч. BOM), UTF-16, CP1251, KOI8-R, CP866
- **MD** (Markdown) — читается как есть, YAML front matter отбрасывается
- **HTML / HTM** — видимый текст страницы без скриптов и стилей
- **ODT, RTF, EPUB, FB2** — без внешних зависимостей
- **SRT / VTT** — субтитры без номеров и таймингов
- Неизвестные расширения — если внутри обычный текст, читаются как TXT

### 🌐 Веб и YouTube
- **Ссылки** — скрейпинг страницы, автоматическое саммари, полный набор режимов обработки
- **YouTube** — субтитры без авторизации и куки через `youtube-transcript-api`; если субтитров нет — честный отказ без попытки транскрибировать аудио; LLM форматирует субтитры в диалог и вырезает рекламные интеграции (`[реклама вырезана]`)

### ✍️ Режимы обработки текста
- 📝 **Как есть** — минимальная коррекция: опечатки, пунктуация, регистр. Голос автора сохранён полностью
- ✨ **Красиво** — редактура: убирает слова-паразиты, повторы, выравнивает структуру. Текст остаётся авторским
- 📊 **Саммари** — аналитический пересказ от третьего лица с сохранением всех фактов и деталей
- ✏️ **Работа над ошибками** — разбор каждой правки с объяснением (доступно после «Как есть» и «Красиво»)
- 💬 **Диалог по документу** — вопросы к содержимому текста со стримингом ответа (в режиме саммари)
- 🌐 **Перевод на русский** — появляется автоматически когда язык текста явно не русский; переключение туда и обратно без потери оригинала

### 💾 Экспорт
- Выгрузка результата в **TXT**, **PDF** (reportlab) или **DOCX**

---

## 🛠️ Технологический стек

| Слой | Технология |
|---|---|
| Bot Framework | [aiogram 3.x](https://docs.aiogram.dev/) |
| Web Server | [FastAPI](https://fastapi.tiangolo.com/) + [uvicorn](https://www.uvicorn.org/) |
| Текстовые LLM | [OpenRouter](https://openrouter.ai/) — бесплатные модели (Gemma 4 31B, Qwen3.8 27B, запас Nemotron 3 Super), откат на Groq |
| STT | [Groq Cloud](https://groq.com/) — Whisper large-v3-turbo |
| OCR | Groq Vision (Qwen 3.8 27B) |
| PDF | pdfplumber (чтение), reportlab (запись) |
| DOCX | python-docx |
| Веб-скрейпинг | httpx + html.parser |
| YouTube | youtube-transcript-api >= 1.0 |
| Определение языка | langdetect |
| База данных | [Supabase](https://supabase.com/) (опционально, полный fallback) |
| Мультимедиа | ffmpeg (для кружочков) |
| Деплой | [Render.com](https://render.com/) |

---

## 📦 Установка и запуск

### 1. Системные зависимости
```bash
sudo apt-get update && sudo apt-get install -y ffmpeg
```

### 2. Python-пакеты
```bash
pip install -r requirements.txt
```

### 3. Переменные окружения (`.env`)
```env
BOT_TOKEN=ваш_токен_телеграм_бота
GROQ_API_KEYS=ключ1,ключ2,ключ3          # Whisper, OCR и запасной откат
OPENROUTER_API_KEYS=sk-or-...             # текстовые LLM (можно несколько через запятую)
# необязательно: свой порядок моделей (через запятую, первая — основная)
# OR_MODELS_BASIC=google/gemma-4-31b-it:free,qwen/qwen3.8-27b:free
# OR_MODELS_PREMIUM=...  OR_MODELS_SUBTITLES=...  OR_MODELS_REASONING=...
SUPABASE_URL=https://xxx.supabase.co   # опционально
SUPABASE_KEY=ваш_anon_ключ             # опционально
PORT=8080

# Доступ и лимиты
ADMIN_IDS=123456789                       # ваш Telegram ID (несколько — через запятую)
# необязательно, значения по умолчанию в скобках:
# USER_DAILY_LIMIT=30        # обращений к ИИ в сутки на пользователя
# USER_MAX_FILE_MB=10        # размер файла или фото
# USER_MAX_AUDIO_SEC=300     # длина голосового, кружка, аудио
# USER_MAX_TEXT_CHARS=15000  # длина присланного текста
# USER_COOLDOWN_SEC=3        # пауза между сообщениями
# LIMITS_TZ=Europe/Minsk     # сутки считаются по этому поясу, сброс в 00:00
# DEFAULT_LLM_PROFILE=auto   # модель по умолчанию: auto, gemma, qwen, nemotron_super,
#                            # nemotron_ultra, space_bunny, groq
```

> Без `SUPABASE_URL` / `SUPABASE_KEY` бот работает без базы данных — история `/history` недоступна, всё хранится в памяти процесса.

### 4. Запуск
```bash
python bot.py
```

---

## 🌐 Деплой на Render.com

1. Создать **Web Service**, команда запуска: `python bot.py`
2. Build Command:
   ```bash
   apt-get install -y ffmpeg && pip install -r requirements.txt
   ```
3. Добавить переменные окружения
4. Настроить UptimeRobot / cron-job.org на `https://your-app.onrender.com/health` каждые 5 минут — иначе free tier засыпает

---

## 🏗️ Структура проекта

```
bot.py          — хендлеры, клавиатуры, роутинг, FastAPI
processors.py   — OCR, транскрибация, коррекция, YouTube, скрейпинг, перевод, экспорт
config.py       — промпты, константы, тексты сообщений
database.py     — Supabase-слой с полным fallback
access.py       — администратор, лимиты, выбор модели
requirements.txt
```

---

## 📋 Команды бота

| Команда | Описание |
|---|---|
| `/start` | Описание бота |
| `/help` | Инструкция по использованию |
| `/history` | Последние 10 обработок (требует Supabase) |
| `/limit` | Остаток дневного лимита |
| `/exit` | Выйти из режима диалога с документом |
| `/status` | Техническое состояние бота (**админ**; остальным показывает лимит) |
| `/model` | Выбор модели для текстовой обработки (**админ**) |
| `/admin` | Статистика за день и текущие настройки (**админ**) |
| `/setlimit ID число` | Индивидуальный лимит пользователю; `0` — заблокировать, `reset` — вернуть общий (**админ**) |

---

## 🗄️ SQL для Supabase

```sql
CREATE TABLE IF NOT EXISTS users (
    id BIGINT PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    first_seen TIMESTAMPTZ DEFAULT NOW(),
    last_seen TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS transcripts (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    original_text TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS results (
    id BIGSERIAL PRIMARY KEY,
    transcript_id BIGINT REFERENCES transcripts(id) ON DELETE CASCADE,
    mode TEXT NOT NULL,
    result_text TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_transcripts_user_id ON transcripts(user_id);
CREATE INDEX IF NOT EXISTS idx_transcripts_created_at ON transcripts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_results_transcript_id ON results(transcript_id);
```

Таблицы для лимитов и настроек модели (необязательно — без них всё работает на памяти процесса, после рестарта счётчики обнуляются):

```sql
CREATE TABLE IF NOT EXISTS bot_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS usage_daily (
    user_id BIGINT NOT NULL,
    day DATE NOT NULL,
    count INT NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);
CREATE INDEX IF NOT EXISTS idx_usage_daily_day ON usage_daily(day);
```

---

## 👑 Администратор, лимиты и выбор модели

**Администратор** задаётся переменной `ADMIN_IDS`. У него нет лимитов, и только ему доступны `/model`, `/admin`, `/setlimit` и `/status`. Если `ADMIN_IDS` не задан, лимиты действуют на всех, включая владельца (в логе будет предупреждение).

**Лимиты обычных пользователей.** Единица лимита — одно обращение к ИИ: распознавание голоса или фото, обработка текста, перевод, разбор правок, вопрос по документу, форматирование субтитров. Бесплатны: команды, просмотр уже готовых (кэшированных) результатов, экспорт, уже закэшированные субтитры YouTube. Ошибки и отказы по лимиту не кэшируются, их можно повторить. Дополнительно действуют потолки на одно сообщение (размер файла, длина голоса, длина текста) и пауза между сообщениями. Лимит сбрасывается в 00:00 по `LIMITS_TZ`.

**Выбор модели** (`/model`, только админ) меняет модель для текстовых задач; Whisper и OCR всегда на Groq. Админ выбирает личный вариант, может сделать его общим для всех или сбросить. Профиль «Авто» сам переключается между моделями OpenRouter и откатывается на Groq; конкретная модель работает строго: если она недоступна, будет ошибка, а не подмена. Выбор сохраняется в Supabase (`bot_settings`), без БД действует до рестарта; значение по умолчанию задаёт `DEFAULT_LLM_PROFILE`.

---

## 📝 Лицензия
Проект для частного использования. Все права на используемые API принадлежат их владельцам.
