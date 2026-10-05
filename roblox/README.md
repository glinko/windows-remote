# Roblox Studio на удалённой Windows

Мост подключает Codex к **официальному встроенному MCP Roblox Studio** через
работающий Windows Remote. Он использует существующий адрес и ключ из
`%LOCALAPPDATA%\WindowsRemote\bridge.json`, без дополнительных сетевых портов,
плагинов Studio, npm-зависимостей и фоновых служб.

На удалённой машине нужен Node.js и открытый Roblox Studio. В Studio включите:
**Assistant → … → Manage MCP Servers → Enable Studio as MCP server**.
Это официальный способ из [документации Roblox](https://create.roblox.com/docs/studio/mcp).

Файл `studio_rpc.cjs` должен находиться на удалённой машине в
`%LOCALAPPDATA%\WindowsRemote\roblox\studio_rpc.cjs`.
Локальный `bridge.py` запускается Codex через Python, рядом должен находиться
исходный `remote_agent.py` из родительской папки проекта.

Пример настройки (замените пути пользователя и расположение проекта):

```toml
[mcp_servers.roblox_studio_remote]
command = 'C:\Python314\python.exe'
args = [
  'C:\Users\admin\viruswar\windows-remote\roblox\bridge.py',
  '--remote-folder',
  'C:\Users\Maxr\AppData\Local\WindowsRemote\roblox'
]
startup_timeout_sec = 60
tool_timeout_sec = 180
```

После изменения `%USERPROFILE%\.codex\config.toml` перезапустите Codex.
В новом диалоге:

> Используй roblox_studio_remote на MaxrPC. Сначала вызови list_roblox_studios,
> выбери нужный открытый проект и используй его studio_id для дальнейшей работы.

Мост возвращает реальные инструменты текущей версии Studio: чтение и
редактирование скриптов, поиск объектов, выполнение Luau, запуск и остановка
playtest и другие доступные инструменты. Работа с файлами и загрузка локальных
изображений относятся к путям на **удалённой** машине.

Каждый запрос открывает временное соединение с официальным `StudioMCP.exe`,
инициализирует MCP, выполняет одну операцию и закрывает соединение. ID окна
Studio остаётся явным параметром запросов. Мост не передаёт произвольные
уведомления, не поддерживает серверные sampling/elicitation-запросы и
не гарантирует функции, требующие постоянной клиентской сессии.

После подключения к MCP обнаружение окна Studio может занять некоторое время.
Перед операцией мост ждёт появления нужного `studio_id` до 10 секунд, повторяя
только чтение списка окон. Само изменение проекта не повторяется.

Одна операция ограничена 105 секундами. Для генерации используйте `async`,
а для `wait_job_finished` задавайте `timeout` не более 90 секунд. Автоматического
повторения операций при ошибках нет, чтобы не дублировать изменения в игре.

Скрипты и ответы передаются в сжатом виде, большими порциями, с проверкой SHA256.
Это обходит лимит вывода Windows Remote 64 КиБ. Временные ответы удаляются
после получения; оставшиеся после сбоя файлы удаляются при следующем обращении,
если им больше часа. Максимальный размер сжатого и распакованного ответа 32 МиБ.
Доступ прекращается при остановке Windows Remote. Ключи не входят в исходники.

Проверка:

```powershell
python roblox/bridge.py --remote-folder 'C:\Users\Maxr\AppData\Local\WindowsRemote\roblox' --probe
python -m unittest -v roblox/test_bridge.py
```
