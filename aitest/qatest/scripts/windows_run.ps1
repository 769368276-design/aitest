$ErrorActionPreference = "Stop"

if (!(Test-Path ".venv\\Scripts\\python.exe")) {
  Write-Host "Missing venv. Run scripts\\windows_setup.ps1 first."
  exit 1
}

$port = if ($env:QA_PORT) { [int]$env:QA_PORT } else { 8003 }
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py runserver 0.0.0.0:$port
.\.venv\Scripts\python.exe manage.py runserver 0.0.0.0:$port
