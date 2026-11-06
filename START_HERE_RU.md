# Background GUI — настоящие окна на другом workspace

Это полный исходный код background-gui с новым режимом host-background.
Готового бинарника niri в архиве нет.

Режим использует отдельный курсор агента, который рисует patched niri.
Патч AgentInput/AgentScreenshot должен работать в основном сеансе niri.
Обычный niri и приватная сборка observer этот режим не поддерживают.

1. Подготовка patched niri: references/host-background.md (там указан закреплённый исходный проект и команда сборки через Nix).
2. Из обычного терминала основного сеанса niri:

   python3 scripts/bgui.py doctor --backend host-background

3. Перемести независимое окно на workspace Agent, вернись на свой workspace и выбери ID окна из ответа doctor:

   python3 scripts/bgui.py start --backend host-background --window ID --name 'Agent task'

4. Сохрани session из ответа. Дальше:

   python3 scripts/bgui.py screenshot SESSION
   python3 scripts/bgui.py click SESSION X Y
   python3 scripts/bgui.py type SESSION 'Привет'

Координаты X/Y берутся из сохранённого скриншота окна.
Переключись на Agent, чтобы наблюдать настоящее окно и отдельный курсор агента.
Команда focus SESSION ID выбирает цель агента и не переключает системный фокус.

Остановить управление:

   python3 scripts/bgui.py stop-host

Нативные приложения после остановки остаются открытыми.
Для браузера используй отдельный профиль/процесс.

Изолированный режим Weston → niri сохранён и остаётся режимом по умолчанию.
Проверены 27 локальных тестов, включая обмен через настоящий Unix-сокет
и первый запуск сессии, когда systemd ещё не загрузил её службу.
Во вложенном patched niri на Fedora 44 проверены GTK-окна, фоновый ввод
кириллицы и эмодзи, сохранение фокуса, отдельный курсор и отключение управления.
Запуск основного рабочего стола и работа на реальных мониторах требуют
отдельной проверки после входа в новую сессию.

Для отдельного пункта входа systemd/GDM есть переносимые примеры
examples/niri-agent-session, examples/niri-agent.service и
examples/niri-agent.desktop.in; установка описана в references/host-background.md.

Основная инструкция: README.md
Нативный режим: references/host-background.md
Пример правил niri: examples/host-background.kdl
