# Локальный аудит GUI OpenADB и план работ

Дата аудита: 10 июля 2026 года

Текущая версия: OpenADB 3.0.2

Среда: Windows, Python 3.14.3, PySide6 6.11.1, экран 1920×1080, системная тёмная тема

## Область и ограничения аудита

Аудит выполнен по текущим локальным файлам, а не только по README или веб-версии репозитория. Проверены активный каталог `openadb/`, точки запуска, зависимости, все страницы и пользовательские виджеты, настройки и профили устройств, фоновые задачи, завершение приложения, таблица приложений, фильтрация, резервные копии, файловый менеджер, Wireless ADB, команды, логи, диалоги, локальные артефакты сборки и наличие тестов.

В исходной папке отсутствовал каталог или файл `.git`. Поэтому до восстановления метаданных Git локальные ветка, индекс и история были недоступны. Удалённый `main` указывал на исходный коммит `7c6471a`; побайтовое сравнение отслеживаемого дерева показало, что первоначальный локальный снимок совпадал с этим коммитом. Последние доступные на тот момент коммиты: `7c6471a`, `a9852d3`, `57bc6e6`.

Подключённого ADB- или fastboot-устройства не было. Выполнены только безопасные команды обнаружения (`adb devices -l`, `fastboot devices`) и локальные mock-сценарии в одноразовых процессах. Bootloader unlock/lock, flash, erase, format, sideload, uninstall, удаление данных, запись на устройство и реальные Wireless ADB-подключения не запускались.

## Текущая архитектура GUI

### Запуск и сборка объектов

- `OpenADB.bat` находит `pythonw.exe`, `pyw.exe` или `python.exe` и запускает `python -m openadb.main` из каталога программы. Ошибки консольного запуска пишутся в `%APPDATA%/OpenADB/logs/openadb-launcher.log`.
- `openadb/main.py` передаёт управление в `openadb/app.py`.
- `openadb/app.py` создаёт один `QApplication`, применяет тему, собирает сервисы (`SettingsManager`, `PlatformToolsManager`, `CommandRunner`, ADB/fastboot, `DeviceManager`, backup/icon/file-transfer managers), затем создаёт `MainWindow`.
- Непойманные исключения верхнего уровня пишутся в `openadb-crash.log`. Runtime-ссылки сохраняются в `_RUNTIME_REFS`.

### Главное окно и навигация

`MainWindow` состоит из общей строки состояния устройства, фиксированной левой навигации шириной 190 px, `QStackedWidget` и системной строки состояния. Все семь страниц создаются сразу:

1. `DashboardPage` — сведения об устройстве и Platform Tools, быстрые команды и Wireless ADB.
2. `AppsPage` — таблица пакетов, кэш метаданных/иконок, UAD-классификация и операции с приложениями.
3. `BackupsPage` — просмотр, восстановление и удаление APK-бэкапов.
4. `FileManagerPage` — Android-панель, нативная Windows Explorer-панель с fallback на `QFileSystemModel`, копирование и управление файлами.
5. `CommandsPage` — пресеты ADB/fastboot и ручной ввод.
6. `LogsPage` — команды текущего сеанса и экспорт видимого текста.
7. `SettingsPage` — пути, тема, обновление устройства, root-параметры и очистка кэшей.

Переход на Apps лениво загружает приложения, если список пуст. Backups обновляется при каждом переходе. File Manager выполняет `refresh_all()` при каждом переходе и при каждом обновлении активного устройства, пока страница открыта.

### Фоновые задачи

Основная абстракция — `Worker(QRunnable)` и глобальный `QThreadPool`. `start_worker()` удерживает Python-ссылку на worker до сигнала `finished`, что предотвращает преждевременную сборку объекта. Долгие ADB/fastboot-команды, загрузка приложений, бэкапы и большинство Android-файловых операций вынесены из GUI-потока.

`DeviceStatusBar` одновременно использует постоянный `adb track-devices` и периодический таймер (по умолчанию каждые 8 секунд). `CommandRunner` запускает процессы без shell, скрывает консольные окна, поддерживает таймауты и частичную отмену streaming-команд.

### Настройки и профили

Глобальные настройки находятся в `~/OpenADB/settings.json`. После обнаружения устройства активируется профиль `~/OpenADB/Phones/<serial>/` или `~/OpenADB/TVs/<serial>/`; вместе с профилем переключаются папки backup/temp/logs и кэши. Есть миграция из старых `%APPDATA%/OpenADB`, `OpenADB-data` и `devices/<serial>`.

Окно, выбранная страница, состояние maximized, размеры/позиция окна, ширины колонок и последние пути файлового менеджера сейчас не сохраняются.

### Темы и DPI

`openadb/ui/style.py` содержит две встроенные QSS-темы. Режим System выбирает Light или Dark по `AppsUseLightTheme` в реестре Windows, затем также применяет QSS и стиль Fusion. Qt 6 корректно создаёт high-DPI pixmap с DPR 1.5 и 2.0, но компоновка не адаптирована к уменьшившейся логической рабочей области.

## Шкала серьёзности

- **Критическая** — риск необратимого действия не над тем устройством, обход обязательного подтверждения или аварийная ошибка жизненного цикла.
- **Высокая** — зависание, гонка, потеря/смешение данных, недоступность интерфейса на поддерживаемой конфигурации.
- **Средняя** — заметная деградация UX, производительности, доступности или диагностики.
- **Низкая** — локальная непоследовательность, технический долг или косметический дефект.

## Найденные проблемы

| ID | Серьёзность | Проблема и подтверждение | Затронутые файлы | Риск | Рекомендация и проверка |
|---|---|---|---|---|---|
| C-01 | Критическая | `spec["danger"]` влияет на красное оформление кнопки, но `run_spec()` показывает подтверждение только при `risk.needs_confirmation`. Без подтверждения остаются `adb reboot sideload`, `root shell command`, четыре кнопки `flash ... image`, `erase userdata`, `erase cache`, `format userdata`; `fastboot boot image` даже не имеет `danger`. README утверждает обратное. | `openadb/ui/commands_page.py`, `openadb/core/safety.py`, `README.md` | Непреднамеренный переход в sideload, стирание/форматирование/прошивка или запуск root-команды одним нажатием. | Формировать риск по реальным `kind + args`, а `danger` всегда считать обязательным подтверждением; для destructive-команд добавить typed confirmation. Автотест должен нажать каждую спецификацию с подменённым runner и доказать, что команда не вызывается до подтверждения. |
| C-02 | Критическая | Все операции используют общий изменяемый `ADBClient.serial`/`FastbootClient.serial`. Обновление или выбор устройства меняет serial во время многошагового backup/uninstall/transfer/assets workflow. Следующая команда той же задачи может уйти уже на другое устройство. | `openadb/core/adb.py`, `fastboot.py`, `device.py`, `openadb/ui/apps_page.py`, `file_manager_page.py`, `backups_page.py`, `commands_page.py`, `main_window.py` | Бэкап с одного устройства и удаление с другого; копирование, install или команда не на том устройстве. | Снимок immutable `DeviceContext(serial, generation)` перед стартом операции; передавать serial явно во все команды и отклонять устаревшие результаты. Интеграционный тест с двумя fake-устройствами и переключением между шагами. |
| H-01 | Высокая | Закрытие останавливает только device monitor. Остальные QRunnable, subprocess и transfer cancel events не отменяются. Безопасный тест: event loop закрылся за 0,54 с, но процесс жил около 4,94 с до конца worker; затем `WorkerSignals` уже был удалён и `Worker.run()` получил три `RuntimeError: Signal source has been deleted`. | `openadb/ui/main_window.py`, `workers.py`, `device_status_bar.py`, `file_manager_page.py`, `core/command_runner.py` | Процесс остаётся после закрытия; незавершённые ADB-команды продолжаются; возможны ошибки/краш при завершении. | Единый реестр задач с cancel token, запрет новых задач при shutdown, ограниченное ожидание и безопасная проверка живых signal objects. Тест закрытия на каждой долгой операции должен завершать процесс и дочерние процессы за заданный срок без stderr. |
| H-02 | Высокая | Нет generation/token у загрузки Apps и File Manager. Старый worker после смены устройства/профиля может заполнить новую страницу, кэш или лог данными предыдущего устройства. `reset_for_device_profile()` не отменяет worker. Mutable `settings`, `runner.log_file`, icon/cache roots переключаются во время работы. | `openadb/ui/apps_page.py`, `file_manager_page.py`, `main_window.py`, `core/settings_manager.py`, `command_runner.py`, `icon_extractor.py`, `app_cache.py` | Смешение приложений, логов, иконок и файлов между профилями; ложные действия по устаревшей таблице. | Каждая задача фиксирует serial/profile paths/generation; GUI принимает результат только при совпадении generation. Старые задачи отменяются или их результат игнорируется. |
| H-03 | Высокая | Возможны повторные и конкурирующие workers. После окончания package-list `Get app data` снова включается, хотя assets ещё грузятся, поэтому запускаются параллельные ACBridge/fallback workers. Commands, Wireless ADB, backup restore/delete и transfers почти не имеют operation guard. Повторное QR pairing перезаписывает ссылку на диалог. | `openadb/ui/apps_page.py`, `commands_page.py`, `backups_page.py`, `file_manager_page.py`, `main_window.py` | Конкуренция за ADB, повторные helper/install/transfer операции, повреждение кэша, непредсказуемые диалоги. | Ввести per-feature operation controller: idle/running/cancelling, disable или queue несовместимых действий, idempotent refresh, явную отмену. |
| H-04 | Высокая | Интерфейс имеет жёсткую минимальную область около 1110×744. Запрос 800×560 фактически дал 1110×744. При 150% DPI default 1280×820 выше доступной области 1280×696; при 200% minimum 1081×657 больше экрана 960×522. Settings не является scroll area; боковые action rows не переносятся. | `openadb/ui/main_window.py`, `dashboard_page.py`, `apps_page.py`, `backups_page.py`, `file_manager_page.py`, `logs_page.py`, `settings_page.py` | На поддерживаемых Windows 10/11 и high-DPI экранах часть окна и управляющих элементов недоступна. | Responsive breakpoints: сворачиваемая/компактная навигация, scroll для Settings, перенос action rows, адаптивная Apps action panel, уменьшение fixed/min widths. Тесты на 960×540@200%, 1280×720@150%, 1366×768@100%. |
| H-05 | Высокая | Device monitor и таймер параллельно решают одну задачу. При открытом File Manager каждые обновление устройства вызывает Windows refresh, storage scan, Android `ls` и `df`; переход на страницу повторяет всё снова. | `openadb/ui/device_status_bar.py`, `main_window.py`, `file_manager_page.py` | Постоянная ADB/COM/дисковая нагрузка, мерцание, лаги, очередь refresh и нестабильность на медленном USB/Wi-Fi. | Track-devices использовать как trigger, details обновлять с debounce/cache; страницы обновлять только при изменении serial/state или по явной команде, с TTL и dirty flags. |
| H-06 | Высокая | `SettingsManager.save()` не атомарен и не защищён lock. Запись вызывается и из GUI, и из worker-потоков. Повреждённый JSON молча заменяется defaults при следующем сохранении, без резервной копии/сообщения. | `openadb/core/settings_manager.py`, вызовы из `device.py`, `platform_tools.py`, `dashboard_page.py`, `settings_page.py` | Усечённый JSON, потеря настроек или profile pointer при конкурентной записи/сбое питания. | Запись во временный файл + `os.replace`, lock, schema validation, backup повреждённого файла и видимое предупреждение. Тесты конкурентной записи и recovery corrupt JSON. |
| H-07 | Высокая | В светлой теме цвета UAD и critical app захардкожены под тёмный фон. Контраст на белом: critical label 1,70:1, Recommended 1,81:1, Advanced 1,44:1, Expert 1,70:1, Unsafe 2,28:1, Not listed 2,53:1. | `openadb/ui/widgets/app_list_widget.py`, `style.py` | Текст плохо читается или визуально исчезает в Light/System-Light. | Theme-aware semantic palette и автоматическая проверка contrast. Не кодировать смысл только цветом. |
| M-01 | Средняя | Dashboard уже при обычных 1280×820 показывает горизонтальный scroll из-за девяти кнопок в одном `QHBoxLayout`. Длинные serial/model/manufacturer обрезаются; status details имел width 945 при sizeHint 3427, без elide/tooltip. Длинный текст в path edit увеличивает minimum-size hint. | `dashboard_page.py`, `device_status_bar.py`, `file_manager_page.py` | Скрытые значения, неудобная прокрутка, резкий рост минимальной ширины окна. | Grid/wrap для быстрых действий, `QSizePolicy.Ignored`, elide + tooltip/copy, ограничение sizeHint path edits. |
| M-02 | Средняя | Disabled dangerous buttons выглядят почти как enabled: поздний selector `QPushButton[danger="true"]` перекрывает цвет/рамку `:disabled`. Focus-состояния не описаны; hover у части контролов визуально слаб. | `openadb/ui/style.py` | Пользователь не понимает, доступно ли опасное действие; клавиатурный фокус плохо заметен. | Явные `[danger="true"]:disabled`, `:focus-visible`/focus frame, состояние hover/pressed/checked для всех control classes. Screenshot regression по состояниям. |
| M-03 | Средняя | Режим System выбирает Light/Dark только при применении темы и не реагирует на смену темы Windows во время работы. Fusion + полный QSS не воспроизводит системную палитру; встроенный Explorer может выглядеть иначе. | `openadb/ui/style.py`, `native_explorer_panel.py` | Несогласованная System theme и смешанные поверхности. | Подписка на palette/theme change Windows/Qt, semantic palette, отдельная проверка нативной панели. |
| M-04 | Средняя | Apps filter на каждое нажатие сортирует таблицу, проходит все строки и планирует полный пересчёт ширины по всем видимым ячейкам. Background item updates также планируют resize. Mock 1200 строк: первичное заполнение 0,48 с, отдельный filter около 0,02–0,03 с на этой машине; стоимость растёт с количеством updates. | `openadb/ui/widgets/app_list_widget.py`, `apps_page.py` | Микрофризы при реальной загрузке и частых metadata/icon signals, особенно на high DPI/слабых ПК. | Debounce поиска, resize только изменённых колонок/после batch, модель `QAbstractTableModel` + proxy filter/sort, performance budget. |
| M-05 | Средняя | Часть тяжёлого локального I/O остаётся в GUI-потоке: очистка temp/icon/all caches, проверка/стат сетевых Windows-путей, rename/new folder; native Explorer COM polling выполняется каждые 500 мс даже после первого открытия страницы. | `settings_page.py`, `main_window.py`, `file_manager_page.py`, `native_explorer_panel.py` | Зависание окна на больших кэшах, UNC, отключённых дисках или медленном Shell namespace. | Вынести рекурсивное I/O и потенциально медленные path/COM операции, останавливать poll при hidden, показывать progress/error result. |
| M-06 | Средняя | Ошибки часто подавляются или не доводятся до пользователя: `except Exception: pass`, очистка temp игнорирует OSError, log writes не защищены, save logs не обёрнут, backup scan может целиком упасть на одном недоступном каталоге. | `app.py`, `settings_manager.py`, `platform_tools.py`, `settings_page.py`, `logs_page.py`, `backup_manager.py`, `icon_extractor.py` | Тихая потеря данных/диагностики и непонятные пустые состояния. | Разделить expected errors и bugs, единый error presenter, per-item errors, fallback log, structured diagnostics. |
| M-07 | Средняя | Логи в GUI содержат только команды текущего сеанса; существующие `openadb.log`/JSONL не загружаются. Launcher пишет в `%APPDATA%`, а GUI-профиль — в `~/OpenADB/...`, поэтому Open logs folder может не содержать launcher log. | `OpenADB.bat`, `openadb/ui/logs_page.py`, `core/command_runner.py`, `settings_manager.py` | Диагностика запуска и предыдущего сеанса разнесена и плохо обнаружима. | Вкладки Session/Persistent/Launcher, tail с лимитом, единая известная папка или ссылки на обе. |
| M-08 | Средняя | Нет автоматических тестов: `unittest discover` сообщает 0 tests. Нет GUI screenshot/interaction, safety matrix, parser, profile race, shutdown или Windows DPI CI. | Весь проект; отсутствует `tests/` | Регрессии в опасных командах, потоках и темах остаются незамеченными. | Сначала safety/unit и lifecycle tests, затем Qt offscreen/native smoke, fake ADB integration и Windows matrix. |
| M-09 | Средняя | `requirements.txt` не фиксирует ни минимальные, ни проверенные версии; заявлен Python 3.10+, локально проверено только текущее сочетание Python 3.14.3/PySide6 6.11.1. | `requirements.txt`, README/build workflow | Невоспроизводимые установки и неожиданные Qt/API-регрессии. | Совместимые диапазоны, lock/constraints для release, CI по минимальной и целевой версии Python/PySide6. |
| L-01 | Низкая | Нет сохранения geometry/windowState, последней страницы, путей File Manager и ширин таблиц. | `main_window.py`, `settings_manager.py`, widgets | Каждый запуск сбрасывает рабочий контекст. | `saveGeometry/restoreGeometry`, versioned UI state и проверка границ доступных экранов. |
| L-02 | Низкая | Пустые Backups и Logs выглядят как безымянная пустая таблица/поле; большинство действий остаются enabled без выбора/устройства. Apps сообщает состояние лучше. | `backups_page.py`, `logs_page.py`, `file_manager_page.py`, `commands_page.py` | Неясно, загружено ли содержимое и почему действие недоступно. | Явные empty/loading/error states и контекстное enablement. |
| L-03 | Низкая | Дублируются Windows file-panel API, форматирование bytes/result messages, наборы action buttons и fallback UI. `FileTransferManager` создаётся, но фактически не передаётся страницам. Локальный `old_version/` — полная игнорируемая копия старого дерева. Явных TODO/FIXME не найдено. | `file_panel.py`, `windows_file_panel.py`, `native_explorer_panel.py`, `progress_dialog.py`, `file_manager_page.py`, `apps_page.py`, `commands_page.py`, `main_window.py`, `app.py`, локальный `old_version/` | Расхождение поведения и рост стоимости изменений. | Общие интерфейсы/formatters/controllers после устранения safety/lifecycle рисков; старую копию держать вне рабочего дерева или документировать её назначение. |

## Рекомендуемый порядок изменений

1. **Safety gate команд.** Исправить C-01, покрыть каждую спецификацию Commands тестом, синхронизировать README. Это наименьшая по объёму, но самая срочная работа.
2. **Device context и lifecycle.** Immutable serial/profile generation, централизованная отмена, корректное закрытие приложения, защита от повторных workers (C-02, H-01–H-03).
3. **Атомарные настройки и границы профиля.** Lock/atomic save, corrupt recovery, фиксированные пути задач и логов (H-02, H-06).
4. **Responsive shell и DPI.** Сохранить текущие функции, но перестроить компоновки с breakpoints/scroll/elide, проверить 100/150/200% и Windows 10/11 (H-04, M-01, L-01).
5. **Semantic theme/state layer.** Light/Dark/System palette, contrast, disabled/hover/selected/focus, реакция на системную тему (H-07, M-02–M-03).
6. **Refresh policy и модели данных.** Убрать дублирующие refresh, добавить dirty/TTL/debounce; затем оптимизировать Apps table/filter и File Manager polling (H-05, M-04–M-05).
7. **Ошибки, empty states и логи.** Единая диагностика, история логов, context enablement, доступные диалоги (M-06–M-07, L-02).
8. **Уборка дублирования и dependency/build policy.** Только после стабилизации поведения (M-08–M-09, L-03).

Каждый шаг должен быть отдельным небольшим коммитом; нельзя одновременно менять safety semantics, threading и внешний вид большой страницы.

## Способы проверки следующих частей

- Unit: полная таблица `command spec -> risk -> confirmation -> exact argv`; parsers; profile/settings recovery; path safety.
- Fake ADB integration: два serial, переключение во время многошаговой операции, offline/disconnect, delayed/out-of-order responses.
- Lifecycle: закрытие во время detect, apps assets, QR pairing, backup, push/pull; отсутствие дочерних процессов и Qt RuntimeError.
- GUI: все страницы, empty/loading/error/content; Light/Dark/System; hover/pressed/disabled/selected/focus; keyboard-only navigation.
- Responsive: 800×600@100%, 1280×720@150%, 1920×1080@200%, maximized, multi-monitor и смена DPI между мониторами.
- Performance: 500/1200/3000 packages, streaming icon updates, длинные пути, 100k файлов в дереве/transfer plan; budgets для frame/filter/close.
- Windows: Windows 10 и 11, без Platform Tools, один/несколько installations, без устройства, unauthorized/offline/mock ADB, USB и Wi-Fi отдельно.

## Что уже сделано хорошо

- Приложение штатно запускается без устройства и корректно показывает actionable no-device состояние.
- Поиск Platform Tools поддерживает saved path, каталог программы, PATH, реестр и типичные Android SDK-пути; версии проверяются вне GUI-потока при обычном запуске.
- `CommandRunner` использует `shell=False`, скрывает консольные окна, хранит structured `CommandResult`, текстовый log и JSONL, имеет таймауты и streaming cancel hooks.
- `start_worker()` удерживает ссылки на QRunnable; тяжёлые ADB/backup/app/file операции в основном вынесены из GUI-потока.
- Backup-before-uninstall включён по умолчанию; критические/system packages имеют дополнительное typed confirmation в Apps.
- `BackupManager.delete_backup()` проверяет, что target находится внутри backup root.
- TAR pull вручную нормализует member path и не извлекает `..`, absolute paths, symlink/hardlink entries — это хорошая защита PC-файловой системы.
- File transfers имеют подробный progress, cancel event, временный файл для single-file pull и безопасный `os.replace` после успеха.
- Apps использует локальный кэш, bounded parallelism и batch-подход; UAD-классификация и fallback labels/icons функционально продуманы.
- Профили Phones/TVs, миграция старых каталогов и защита backups при reset — хорошая основа, требующая только более строгой потоковой границы.
- Light и Dark QSS последовательно покрывают базовые поверхности, таблицы, scrollbars и основные selected/hover/disabled состояния; архитектуру тем можно развивать без переписывания страниц.
- Native Explorer имеет `QFileSystemModel` fallback и отключается на offscreen platform, что полезно для будущих GUI-тестов.

## Выполненные проверки части 0

| Проверка | Результат |
|---|---|
| `git status --short --branch` до любых изменений | Не выполнен как Git-операция: `.git` отсутствовал; команда первой вернула `not a git repository`. |
| Сопоставление с GitHub | Локальные отслеживаемые файлы совпали с удалённым `main` `7c6471a`; получены последние три коммита. |
| Импорт зависимостей | PySide6, Pillow, apkutils2, qrcode, zeroconf импортируются. |
| Launcher dry-run | Unicode/space path корректен; выбраны `pythonw.exe` и `-m openadb.main`. |
| Штатный запуск с Platform Tools без устройства | Окно OpenADB появилось, tools Found, no-device состояние; обычное закрытие дало exit code 0, stderr пуст. |
| Platform Tools | Фактически найдены 2 кандидата; missing-tools безопасно смоделирован, active status `Not found`. |
| Все 7 страниц | Отрисованы в System, Light и Dark при 1280×820; страницы доступны без устройства. |
| Размеры | Проверены 1280×820, запрос 800×560, maximized 1920×1021, длинные device fields и пути. |
| DPI | DPR 1.0/1.5/2.0; физические pixmap масштабируются, responsive layout не помещается при 150–200%. |
| UI states | Selected nav/table, focused search, hover и disabled button; обнаружен конфликт disabled + danger. |
| Apps mock | 1200 строк, заполнение 0,48 с; фильтры около 0,02–0,03 с; mock не сохранялся и не включён в release. |
| Empty states | Apps/Backups/File Manager/Logs/Settings/Commands без устройства и данных. |
| Shutdown с worker | Подтверждены задержка процесса и Qt `Signal source has been deleted`. |
| Синтаксис | `python -m compileall -q openadb tools` — успешно. |
| Автотесты | `python -m unittest discover -v` — 0 tests. |

## Итог после частей 0–11

Дата итоговой проверки: 12 июля 2026 года.

Исходный текст выше сохранён как базовый снимок состояния до редизайна. После частей 1–11 архитектура приложения не переписывалась, но интерфейс, safety model и проверяемость существенно изменились.

### Статус основных рисков исходного аудита

| ID | Итоговый статус | Результат |
|---|---|---|
| C-01 | Исправлено | Commands использует централизованный анализ фактического argv, блокирует недоступные команды и требует обычное либо typed confirmation для всех risky/destructive/critical операций. |
| C-02 | Остаётся | Отдельная команда фиксирует serial при построении argv, однако многошаговые Apps/Backup/File workflows всё ещё используют общий изменяемый client serial между шагами. Нужен immutable `DeviceContext`; это не внедрялось в рамках GUI-редизайна. |
| H-01 | Исправлено для управляемых операций | При закрытии отменяются Commands, transfers, QR и device monitor; зарегистрированные subprocess завершаются, очередь workers очищается, выполняется ограниченное ожидание, а поздние Qt emits безопасно игнорируются. Пять реальных изолированных startup/shutdown сценариев оставили 0 новых adb/fastboot процессов и не дали traceback. |
| H-02 | Остаётся | Полного generation token для всех Apps/File Manager результатов нет. Profile switch покрыт тестами UI-state, но delayed old-device result требует отдельного data-lifecycle проекта. |
| H-03 | Существенно уменьшено | Есть guards для Apps bulk/background work, Commands, transfers, refresh и QR pairing. Backup restore/delete всё ещё не имеют общего operation controller. |
| H-04 | Исправлено | Главное окно, навигация и страницы проходят narrow/standard/maximized проверки при DPI 100/125/150/200% в System/Light/Dark. |
| H-05 | Частично исправлено | Повторный identical device snapshot больше не запускает тяжёлый File Manager refresh. Monitor и fallback timer остаются двумя источниками status refresh, но duplicate guards и debounce сохранены. |
| H-06 | Исправлено частично | Settings записываются через временный файл и `os.replace` под `RLock`; конкурентный тест не обнаружил повреждённого JSON. Отдельный backup/recovery повреждённого JSON пока не реализован. |
| H-07 | Исправлено | UAD и critical-package foreground теперь выбирается из Light/Dark semantic palette и обновляется при смене темы; добавлен автоматический тест обеих тем. |
| M-01/M-02 | Исправлено | Длинные значения имеют elide/tooltips, actions перегруппированы, danger/disabled/focus и adaptive layout унифицированы. |
| M-03 | Остаётся | System корректно разрешается в Light/Dark при применении, но приложение не подписано на live-смену системной темы Windows. |
| M-04 | Исправлено для целевого масштаба | Локальный фильтр 1200 mock-приложений: среднее 7,6 мс, максимум 35,8 мс; полный table load 391,6 мс, сортировка 252,5 мс. |

### Итоговая валидация

- 96 unittest покрывают фильтры, selection, profiles, actions, safety/risk, window state, settings migration/reset, device formatting, File Manager, Commands, empty states, dialogs, Backups, shutdown и атомарные настройки.
- Пять изолированных запусков проверили no-tools, found-tools, saved settings, повторный запуск и запуск после reset; каждый завершился с code 0 за 1,0–1,5 секунды.
- Light/Dark/System и DPI 100/125/150/200% проверены без pixel-perfect привязки.
- Реальные bootloader unlock/lock, flash, erase, format, sideload, uninstall, root, backup restore и file transfer не запускались.
- Настоящее Android-устройство и Windows 10 не были доступны; эти сценарии явно остаются непроверенными.

Полный итог и рекомендации находятся в `GUI_REDESIGN_REPORT.md`.
