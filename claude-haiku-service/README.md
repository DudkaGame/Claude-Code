# claude-haiku-keepalive

Systemd-сервис, который каждые 5 часов отправляет сообщение "." модели claude-haiku через Claude Code CLI. Аналогичен по структуре aeroflot-check.

## Деплой на VPS (Ubuntu 22.04)

```bash
git clone https://github.com/dudkagame/claude-code.git
cd claude-code/claude-haiku-service
sudo deploy/install.sh <ANTHROPIC_API_KEY>
```

## Управление

```bash
# Статус таймера
systemctl status claude-ping.timer

# Ближайшие запуски
systemctl list-timers claude-ping.timer

# Запустить вручную
systemctl start claude-ping.service

# Логи
tail -f /var/log/claude-ping/run.log

# Отключить
systemctl disable --now claude-ping.timer
```

## Файлы

| Путь | Описание |
|------|----------|
| `/opt/claude-ping/ping.sh` | Основной скрипт |
| `/etc/claude-ping/env` | API-ключ (права 600, не в репо) |
| `/var/log/claude-ping/run.log` | Логи |
| `/etc/systemd/system/claude-ping.*` | Systemd-юниты |
