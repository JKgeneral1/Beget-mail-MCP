# Beget Mail MCP

Минимальный MCP-сервис для одного почтового ящика Beget:

- список последних писем без изменения флага `Seen`;
- поиск по тексту письма;
- чтение письма по IMAP UID;
- двухшаговая отправка: `prepare_email` → подтверждение пользователя → `send_email`.

Вложения не скачиваются и не отправляются в ChatGPT: сервис возвращает только их имена.

## Настройки Beget

По официальной справке Beget:

- IMAP: `imap.beget.com`, SSL, порт `993`;
- SMTP: `smtp.beget.com`, SSL, порт `465`;
- логин: полный адрес почтового ящика;
- SMTP-лимиты: до 30 писем в минуту и до 1500 писем в час.

Источник: <https://beget.com/ru/kb/how-to/mail/obshhie-svedeniya>

## Локальный запуск

Нужен Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
```

Заполните `.env`, затем экспортируйте значения удобным для вашей системы способом и запустите:

```bash
beget-mail-mcp
```

Проверка состояния без доступа к почте:

```bash
curl http://localhost:8000/health
```

MCP endpoint: `http://localhost:8000/mcp`.

## Необходимые значения

- `BEGET_EMAIL` — полный адрес ящика;
- `BEGET_PASSWORD` — пароль именно этого почтового ящика;
- `MCP_BEARER_TOKEN` — отдельная случайная строка длиной не менее 32 символов.

Сгенерировать токен можно локально: `openssl rand -hex 32`. Не присылайте пароль или токен в чат и не добавляйте `.env` в Git.

## Подключение к ChatGPT

Для теста сервис должен быть доступен по публичному HTTPS или через Secure MCP Tunnel. В ChatGPT откройте Plugins, добавьте custom MCP server и укажите полный URL с `/mcp`. Настройте передачу `MCP_BEARER_TOKEN` как Bearer token.

Для публикации общего плагина статический Bearer token недостаточен: следует добавить OAuth 2.1, раздельные аккаунты/права, журналирование без тел писем и ротацию токенов.

Официальные требования OpenAI:

- <https://developers.openai.com/plugins/build/mcp-server>
- <https://developers.openai.com/plugins/deploy/connect-chatgpt>
- <https://developers.openai.com/plugins/build/auth>

## Безопасность MVP

- секреты читаются только из окружения;
- MCP endpoint закрыт Bearer token;
- соединения с Beget используют TLS;
- письма читаются через `BODY.PEEK`, то есть не помечаются прочитанными;
- отправитель всегда равен `BEGET_EMAIL`;
- `send_email` принимает только неизменённое письмо из `prepare_email`; токен подтверждения живёт 10 минут;
- тела писем и секреты не журналируются приложением.

