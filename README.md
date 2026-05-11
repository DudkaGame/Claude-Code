# Aeroflot subsidized tickets bot

Мониторит наличие субсидированных авиабилетов Аэрофлот Москва ↔ Владивосток
(категория «Молодёжь и пенсионеры») четыре раза в день (09:00, 12:00, 16:00, 20:00 по МСК)
и присылает уведомление в Telegram, когда появляются свободные места.

Работает на небольшом российском VPS (Ubuntu 22.04, 1 vCPU / 1 GB RAM).

## Как это работает

1. `systemd timer` (`deploy/aeroflot-check.timer`) запускает сервис
   в 09:00, 12:00, 16:00 и 20:00 по Европа/Москва.
2. Сервис (`deploy/aeroflot-check.service`) запускает
   `check_tickets.py` под пользователем `aeroflot`.
3. Скрипт поднимает headless Chromium через Playwright, открывает
   страницу поиска субсидированных билетов, поочерёдно выбирает
   все 8 дат (туда и обратно) и перехватывает JSON-ответы внутреннего API.
4. Если найден рейс с ≥ N свободных мест (по умолчанию N = пассажиры) —
   отправляет сообщение в Telegram.
5. Состояние хранится в `state.json`, чтобы не присылать повторные
   уведомления о том же рейсе.

## Разовая подготовка

### 1. Создать Telegram-бота

1. В Telegram открыть `@BotFather`, выполнить `/newbot`, придумать имя —
   получите токен вида `1234567890:ABCdef...`.
2. Написать созданному боту любое сообщение (например, `/start`).
3. Открыть в браузере `https://api.telegram.org/bot<ТОКЕН>/getUpdates` —
   в JSON-ответе найти `"chat":{"id":NNNNN,...}`. Это ваш `chat_id`.

### 2. Арендовать VPS

Рекомендуется российский провайдер (чтобы пройти гео-фильтр Аэрофлота):
- **Timeweb Cloud** — от ~190 ₽/мес, Ubuntu 22.04, оплата картой РФ.
- **Selectel** — от ~250 ₽/мес.
- **REG.RU** — аналогично.

Минимальная конфигурация: 1 vCPU / 1 GB RAM / 10 GB SSD / Ubuntu 22.04.

После аренды запишите:
- IP-адрес сервера
- root-пароль (или ssh-ключ)

### 3. Заполнить конфиг

```bash
cp config.example.json config.json
```

Откройте `config.json` и подставьте:
- `telegram.bot_token` — токен от @BotFather
- `telegram.chat_id` — ваш chat_id
- При необходимости — обновите даты/пассажиров/категорию.

Ключевые поля:

| Поле | Значение |
|------|----------|
| `origin` / `destination` | IATA-коды городов (`MOW`, `VVO`) |
| `dates_outbound`         | список дат туда в формате `YYYY-MM-DD` |
| `dates_return`           | список дат обратно |
| `passengers`             | число пассажиров в поиске |
| `min_seats`              | минимум мест, чтобы сработал алерт |
| `category`               | текст категории как на сайте |

## Развёртывание на VPS

```bash
# на локальной машине (Git Bash):
cd "C:/Users/dimap/Documents/Claude/Search tickets"
scp -r . root@<IP_VPS>:/tmp/aeroflot-bot

# на VPS:
ssh root@<IP_VPS>
bash /tmp/aeroflot-bot/deploy/install.sh /tmp/aeroflot-bot
```

`install.sh` делает:
- создаёт системного пользователя `aeroflot`
- копирует исходники в `/opt/aeroflot-bot`
- создаёт Python venv и ставит Playwright + Chromium
- ставит и активирует systemd timer

## Верификация

```bash
# тест Telegram
sudo -u aeroflot /opt/aeroflot-bot/.venv/bin/python \
    /opt/aeroflot-bot/check_tickets.py --test-notify
# → должно прийти «🧪 Тест уведомления»

# ручной прогон без отправки
sudo -u aeroflot /opt/aeroflot-bot/.venv/bin/python \
    /opt/aeroflot-bot/check_tickets.py --dry-run

# следующие три запуска по таймеру
systemctl list-timers aeroflot-check.timer

# логи
tail -f /var/log/aeroflot-bot/run.log
```

## Операционные команды

| Задача | Команда |
|--------|---------|
| Ручной запуск сейчас | `sudo systemctl start aeroflot-check.service` |
| Приостановить расписание | `sudo systemctl disable --now aeroflot-check.timer` |
| Возобновить | `sudo systemctl enable --now aeroflot-check.timer` |
| Посмотреть лог последнего прогона | `sudo journalctl -u aeroflot-check.service -n 100` |
| Сбросить антидубликат | `sudo -u aeroflot /opt/aeroflot-bot/.venv/bin/python /opt/aeroflot-bot/check_tickets.py --reset-state` |
| Статистика по всем датам | `sudo -u aeroflot /opt/aeroflot-bot/.venv/bin/python /opt/aeroflot-bot/check_tickets.py --stats` |
| История по одной дате | `sudo -u aeroflot /opt/aeroflot-bot/.venv/bin/python /opt/aeroflot-bot/check_tickets.py --history 2026-08-20` |
| Обновить конфиг | отредактировать `/opt/aeroflot-bot/config.json`, перезапуск не нужен |
| Обновить код | залить новый `check_tickets.py` в `/opt/aeroflot-bot/` (owner — aeroflot) |

## Безопасность

- `config.json` содержит секретный токен → права 600, владелец `aeroflot`.
- Репозиторий локально содержит `.gitignore`, исключающий `config.json`,
  `state.json` и `logs/`.
- На VPS рекомендуется: SSH по ключу (отключить пароль), `ufw allow 22/tcp`,
  `apt install fail2ban`.

## Если Аэрофлот всё равно блокирует

Симптомы: в логах `TimeoutError`, капча, 503, 0 XHR-ответов.
Последовательные меры:
1. Добавить задержку и скроллинг на странице (уже есть `networkidle`).
2. Установить `playwright-stealth`:
   `pip install playwright-stealth` и включить в `check_tickets.py`.
3. Ротация User-Agent / `--disable-blink-features=AutomationControlled`.
4. Использовать резидентный российский прокси.
5. Последний вариант — `headless=False` (GUI-режим на VPS с Xvfb).
