$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать окружение Python.' }
}
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости.' }
& .\.venv\Scripts\python.exe -m unittest test_app -v
if ($LASTEXITCODE -ne 0) { throw 'Проверки завершились с ошибкой.' }
& .\.venv\Scripts\python.exe -m PyInstaller --noconfirm MeetingScribe.spec
if ($LASTEXITCODE -ne 0) { throw 'Не удалось собрать EXE.' }
Copy-Item -LiteralPath README.md -Destination dist\MeetingScribe\README.md -Force
Copy-Item -LiteralPath LICENSE -Destination dist\MeetingScribe\LICENSE -Force
& .\.venv\Scripts\python.exe package_licenses.py dist\MeetingScribe
if ($LASTEXITCODE -ne 0) { throw 'Не удалось собрать лицензии зависимостей.' }
