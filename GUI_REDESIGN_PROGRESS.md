# OpenADB GUI redesign progress

Обновлено: 10 июля 2026 года.

Статусы: `не начато`, `в работе`, `выполнено`, `заблокировано`.

> SHA текущего коммита нельзя записать внутрь самого этого коммита: изменение строки изменило бы SHA. Для части 0 используется стабильная ссылка на `HEAD` с сообщением `docs: add local GUI audit and redesign plan`; точный SHA приводится в итогах задачи и доступен через `git log -1 --oneline`.

| № части | Название | Статус | Затронутые файлы | Выполненные тесты | Известные ограничения | Хеш коммита |
|---:|---|---|---|---|---|---|
| 0 | Локальный аудит OpenADB и план работ | **выполнено** | `GUI_AUDIT.md`, `GUI_REDESIGN_PROGRESS.md` | Git snapshot/remote comparison; dependency imports; launcher dry-run; штатный source launch; no-device и simulated no-tools; все страницы × System/Light/Dark; normal/small/maximized; long device/path; empty и disabled/hover/selected/focus; Apps 1200-row mock; DPI 100/150/200%; shutdown worker; compileall; unittest discovery | Нет реального Android-устройства; destructive ADB/fastboot-команды не выполнялись; Windows 10 не был доступен; mock существовал только в одноразовом процессе; исходная папка была без `.git` | `HEAD` — `docs: add local GUI audit and redesign plan` |
| 1 | Safety gate и тестовая матрица Commands | не начато | — | — | Определяется по результатам части 0 | — |
| 2 | Device context, workers и корректное завершение | не начато | — | — | Определяется по результатам части 0 | — |
| 3 | Responsive shell, DPI и сохранение состояния окна | не начато | — | — | Windows 10/11, Light/Dark/System обязательны | — |
| 4 | Semantic темы и состояния контролов | не начато | — | — | Нужны contrast и screenshot regression tests | — |
| 5 | Apps table, фильтрация и device-safe кэш | не начато | — | — | Нужны fake ADB и большие наборы данных | — |
| 6 | File Manager, backups, logs и error/empty states | не начато | — | — | Реальные операции требуют отдельного безопасного стенда | — |
| 7 | Финальная Windows-матрица и стабилизация | не начато | — | — | Нужны Windows 10/11 и реальные USB/Wi-Fi сценарии | — |

## Правило обновления

После каждой части обновляются только её строка и действительно затронутые последующие ограничения. Указываются выполненные команды/сценарии, а не общий текст «проверено». Часть получает статус `выполнено` только после проверки Light, Dark, System, Windows DPI и отсутствия регрессий существующих функций.
