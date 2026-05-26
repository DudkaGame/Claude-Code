# claude-haiku-keepalive

Cron-сервис: каждые 5 часов отправляет `"."` в один и тот же чат Claude Code (модель `claude-haiku-4-5`), используя `claude --continue`. Авторизация через OAuth-логин Claude Code.

## Деплой на VPS (Ubuntu 22.04+)

```bash
ssh root@<server-ip>
git clone https://github.com/dudkagame/claude-code.git
cd claude-code/claude-haiku-service
git checkout claude/setup-message-scheduler-L0E0M
sudo bash deploy/install.sh
```

`install.sh` поставит Node.js 22 + Claude Code CLI, положит `ping.sh` в `/opt/claude-ping/`, создаст `/var/log/claude-ping/`, добавит cron-строку `0 */5 * * * /opt/claude-ping/ping.sh` и удалит старые systemd-юниты (если были).

## Первый запуск (обязательно)

Cron вызывает `claude --continue` — для этого должна существовать **исходная сессия**. Создайте её один раз вручную:

```bash
claude                       # OAuth-логин в браузере (одноразовый)
# в интерактивном чате отправьте любое сообщение, например "hi", затем Ctrl+D / /exit
```

После этого `--continue` будет продолжать именно эту беседу при каждом cron-запуске.

## Ручная проверка

```bash
sudo /opt/claude-ping/ping.sh
tail /var/log/claude-ping/run.log
```

В логе должны появиться блоки `--- ping ---`, ответ модели и `--- done ---`. Запустите `ping.sh` второй раз — ответ модели должен учитывать предыдущий контекст (доказательство `--continue`).

## Управление

```bash
crontab -l                              # посмотреть расписание
tail -f /var/log/claude-ping/run.log    # стрим логов
crontab -l | grep -v claude-ping | crontab -    # отключить
```

## Файлы

| Путь | Описание |
|------|----------|
| `/opt/claude-ping/ping.sh` | Cron-скрипт |
| `/var/log/claude-ping/run.log` | Логи |
| `~/.config/claude/` (или `~/.claude/`) | OAuth-токен и сессии Claude Code |
| crontab root | Строка `0 */5 * * * /opt/claude-ping/ping.sh` |
