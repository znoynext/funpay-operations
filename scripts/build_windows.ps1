$ErrorActionPreference = 'Stop'
# Install only pinned runtime/build dependencies.  The PyInstaller search path
# below always points at this checkout, so a global package copy is never used.
python -m pip install -r requirements.txt 'pyinstaller==6.22.0'
if ($LASTEXITCODE -ne 0) { throw 'Python build dependency installation failed.' }
New-Item -ItemType Directory -Force -Path .\dist | Out-Null
dotnet publish .\src\FunPayOperations.AuthHelper\FunPayOperations.AuthHelper.csproj --configuration Release --runtime win-x64 --self-contained true --output .\build\auth-helper
if ($LASTEXITCODE -ne 0) { throw 'WebView2 authentication helper build failed.' }
Copy-Item -LiteralPath .\build\auth-helper\funpay-operations-auth.exe -Destination .\dist\funpay-operations-auth.exe -Force
Copy-Item -LiteralPath .\THIRD_PARTY_NOTICES.md -Destination .\dist\THIRD_PARTY_NOTICES.md -Force
python -m PyInstaller --noconfirm --clean --onefile --noconsole --paths src --name funpay-operations scripts/windows_entrypoint.py
if ($LASTEXITCODE -ne 0) { throw 'Background executable build failed.' }
python -m PyInstaller --noconfirm --clean --onefile --console --paths src --name funpay-operations-cli scripts/windows_entrypoint.py
if ($LASTEXITCODE -ne 0) { throw 'CLI executable build failed.' }
python -m PyInstaller --noconfirm --clean --onefile --noconsole --paths src --name funpay-operations-setup scripts/windows_setup_entrypoint.py
if ($LASTEXITCODE -ne 0) { throw 'Setup Center executable build failed.' }
