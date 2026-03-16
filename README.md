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
- **TXT** — поддержка UTF-8, CP1251 и других кодировок

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
| LLM / STT | [Groq Cloud](https://groq.com/) — Llama 4 Scout, Llama 3.3 70B, Whisper large-v3-turbo |
| OCR | Groq Vision (llama-4-scout) |
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
GROQ_API_KEYS=ключ1,ключ2,ключ3
SUPABASE_URL=https://xxx.supabase.co   # опционально
SUPABASE_KEY=ваш_anon_ключ             # опционально
PORT=8080
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
requirements.txt
```

---

## 📋 Команды бота

| Команда | Описание |
|---|---|
| `/start` | Описание бота |
| `/help` | Инструкция по использованию |
| `/history` | Последние 10 обработок (требует Supabase) |
| `/status` | Техническое состояние бота |
| `/exit` | Выйти из режима диалога с документом |

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

---

## 📝 Лицензия
Проект для частного использования. Все права на используемые API принадлежат их владельцам.
