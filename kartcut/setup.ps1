# KartCut one-time setup for Windows 10/11 (run via setup_windows.bat)
# Everything lives inside this folder: a portable Python 3.12, PyTorch with CUDA for your
# NVIDIA card, the AI libraries, FFmpeg with NVENC and the AI models. Nothing is installed
# into Windows, so it runs from any drive and other Pythons on the laptop don't matter.

$ErrorActionPreference = "Continue"  # native tools write to stderr; we check exit codes instead
$ProgressPreference = "SilentlyContinue"
# Older Windows 10 PowerShell defaults to TLS 1.0, which python.org and gyan.dev refuse.
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

# Keep downloads and temporary files on this drive, not C:
$Work = Join-Path $Root ".setup_tmp"
New-Item -ItemType Directory -Force -Path $Work | Out-Null
$env:TEMP = $Work
$env:TMP = $Work
$env:PIP_CACHE_DIR = Join-Path $Root ".pipcache"
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
$env:PYTHONNOUSERSITE = "1"
$env:YOLO_CONFIG_DIR = Join-Path $Root "models\ultralytics"

function Step($t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Yellow }
function Fail($t) { Write-Host ""; Write-Host "SETUP FAILED: $t" -ForegroundColor Red; exit 1 }
function Have($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

$PyVer = "3.12.10"
$CoreCode = "import sys, struct, difflib, unittest, ssl, sqlite3, ctypes, json, zipfile; sys.exit(0 if struct.calcsize('P') == 8 and (3, 10) <= sys.version_info[:2] <= (3, 13) else 3)"
function Test-Python($exe) {
  if (-not $exe -or -not (Test-Path $exe)) { return $false }
  & $exe -c $CoreCode 2>$null | Out-Null
  return ($LASTEXITCODE -eq 0)
}

# ---------------------------------------------------------------- Python
Step "Setting up KartCut's portable Python $PyVer"
$PyDir = Join-Path $Root "python"
$vpy = Join-Path $PyDir "python.exe"
$ready = $false
if (Test-Python $vpy) {
  & $vpy -m pip --version 2>$null | Out-Null
  $ready = ($LASTEXITCODE -eq 0)
}
if (-not $ready) {
  if (Test-Path $PyDir) { Remove-Item $PyDir -Recurse -Force -ErrorAction SilentlyContinue }
  if (Test-Path $PyDir) { Fail "Could not remove the old 'python' folder. Close every KartCut window, then run setup again." }
  $zip = Join-Path $Work "python-embed.zip"
  try {
    Write-Host "Downloading Python $PyVer (about 11 MB)..."
    Invoke-WebRequest -Uri "https://www.python.org/ftp/python/$PyVer/python-$PyVer-embed-amd64.zip" -OutFile $zip -UseBasicParsing -ErrorAction Stop
    Expand-Archive -Path $zip -DestinationPath $PyDir -Force -ErrorAction Stop
  } catch { Fail "Could not download Python: $_" }
  # The portable build ignores installed packages until 'import site' is switched on.
  $pth = Get-ChildItem $PyDir -Filter "python3*._pth" | Select-Object -First 1
  if (-not $pth) { Fail "Python download looks incomplete (no ._pth file)." }
  $lines = Get-Content $pth.FullName | ForEach-Object { if ($_ -match "^\s*#\s*import site") { "import site" } else { $_ } }
  Set-Content -Path $pth.FullName -Value $lines -Encoding ASCII
  if (-not (Test-Python $vpy)) {
    Write-Host "Python self-check output:" -ForegroundColor Yellow
    & $vpy -c $CoreCode
    Fail "The portable Python failed its self-check (details above). Antivirus may be blocking files in $PyDir."
  }
  Write-Host "Installing pip..."
  $gp = Join-Path $Work "get-pip.py"
  try { Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $gp -UseBasicParsing -ErrorAction Stop } catch { Fail "Could not download pip: $_" }
  & $vpy $gp --no-warn-script-location | Out-Host
  & $vpy -m pip --version 2>$null | Out-Null
  if ($LASTEXITCODE -ne 0) { Fail "pip did not install into the portable Python." }
}
Write-Host "Python ready: $vpy" -ForegroundColor Green
& $vpy -m pip install --upgrade pip wheel "setuptools<81" --no-warn-script-location | Out-Host
# Leftovers from earlier setup attempts
foreach ($old in @(".venv")) {
  if (Test-Path (Join-Path $Root $old)) { Remove-Item (Join-Path $Root $old) -Recurse -Force -ErrorAction SilentlyContinue }
}

# ---------------------------------------------------------------- PyTorch + CUDA
Step "Installing PyTorch for your GPU"
$cuda = $null; $gpu = ""
try {
  $smi = (& nvidia-smi 2>$null | Out-String)
  if ($smi -match "CUDA Version:\s*([\d\.]+)") { $cuda = [version]$Matches[1] }
  $gpu = ((& nvidia-smi --query-gpu=name --format=csv,noheader 2>$null) | Select-Object -First 1)
} catch {}
$indexes = @()
if ($cuda) {
  Write-Host "NVIDIA GPU: $gpu (driver supports CUDA $cuda)"
  $old = $gpu -match "GTX (9|10)\d\d|Quadro (M|P)\d|Tesla (M|P)\d"
  if (-not $old) {
    if ($cuda -ge [version]"13.0") { $indexes += "cu130" }
    if ($cuda -ge [version]"12.8") { $indexes += "cu128" }
  }
  if ($cuda -ge [version]"12.6") { $indexes += "cu126" }
  if ($cuda -ge [version]"12.4") { $indexes += "cu124" }
  if ($cuda -lt [version]"12.4") { Write-Host "Your NVIDIA driver is old. Update it from nvidia.com for GPU speed." -ForegroundColor Yellow }
} else {
  Write-Host "No NVIDIA GPU found: installing the CPU version (works, slower)." -ForegroundColor Yellow
}
$torchOk = $false
foreach ($idx in $indexes) {
  Write-Host "Trying PyTorch build $idx..."
  & $vpy -m pip install --no-warn-script-location --upgrade torch torchvision --index-url "https://download.pytorch.org/whl/$idx" | Out-Host
  if ($LASTEXITCODE -eq 0) {
    & $vpy -c "import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)"
    if ($LASTEXITCODE -eq 0) { $torchOk = $true; Write-Host "PyTorch sees the GPU." -ForegroundColor Green; break }
  }
}
if (-not $torchOk) {
  & $vpy -c "import torch" 2>$null
  if ($LASTEXITCODE -ne 0) {
    & $vpy -m pip install --no-warn-script-location torch torchvision --index-url "https://download.pytorch.org/whl/cpu" | Out-Host
    if ($LASTEXITCODE -ne 0) { Fail "could not install PyTorch" }
  }
  if ($cuda) { Write-Host "GPU build did not work: running on CPU. Update the NVIDIA driver and re-run setup." -ForegroundColor Yellow }
}

# ---------------------------------------------------------------- libraries
Step "Installing KartCut libraries"
& $vpy -m pip install --no-warn-script-location --no-build-isolation -r requirements.txt | Out-Host
if ($LASTEXITCODE -ne 0) { Fail "pip install -r requirements.txt failed" }
# CLIP: text encoder for YOLO-World's 'go-kart' prompt + appearance matching
$clipOk = $false
foreach ($u in @("https://github.com/ultralytics/CLIP/archive/refs/heads/main.zip",
                 "https://github.com/openai/CLIP/archive/refs/heads/main.zip")) {
  & $vpy -m pip install --no-warn-script-location --no-build-isolation $u | Out-Host
  if ($LASTEXITCODE -eq 0) { $clipOk = $true; break }
}
if (-not $clipOk) { & $vpy -m pip install --no-warn-script-location --no-build-isolation openai-clip | Out-Host }

# ---------------------------------------------------------------- FFmpeg
Step "Installing FFmpeg (with NVIDIA NVENC)"
$ff = Join-Path $Root "bin\ffmpeg\bin\ffmpeg.exe"
if (-not (Test-Path $ff)) {
  $zip = Join-Path $Work "kartcut_ffmpeg.zip"
  $tmp = Join-Path $Work "kartcut_ffmpeg"
  try {
    Invoke-WebRequest -Uri "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" -OutFile $zip -UseBasicParsing -ErrorAction Stop
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
    Expand-Archive -Path $zip -DestinationPath $tmp -Force -ErrorAction Stop
    $inner = Get-ChildItem $tmp -Directory | Select-Object -First 1
    New-Item -ItemType Directory -Force -Path (Join-Path $Root "bin") | Out-Null
    if (Test-Path (Join-Path $Root "bin\ffmpeg")) { Remove-Item (Join-Path $Root "bin\ffmpeg") -Recurse -Force }
    Move-Item $inner.FullName (Join-Path $Root "bin\ffmpeg") -ErrorAction Stop
    Remove-Item $zip -Force
  } catch {
    Write-Host "FFmpeg download failed ($_)." -ForegroundColor Yellow
    if (Have "winget") {
      Write-Host "Trying winget..."
      winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
    } elseif (-not (Have "ffmpeg")) {
      Write-Host "Download ffmpeg-release-essentials.zip from gyan.dev, unzip it into $Root\bin and rename the folder to ffmpeg." -ForegroundColor Yellow
    }
  }
}
if (Test-Path $ff) { Write-Host "FFmpeg ready: $ff" -ForegroundColor Green }

# ---------------------------------------------------------------- models
Step "Downloading and testing the AI models (about 600 MB, one time)"
& $vpy run.py --prepare
if ($LASTEXITCODE -ne 0) { Write-Host "Model check reported a problem: KartCut will still run, check the status chips in the app." -ForegroundColor Yellow }

# ---------------------------------------------------------------- shortcut
Step "Adding a desktop shortcut"
try {
  $ws = New-Object -ComObject WScript.Shell
  $lnk = $ws.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Desktop")) "KartCut.lnk"))
  $lnk.TargetPath = Join-Path $Root "KartCut.bat"
  $lnk.WorkingDirectory = $Root
  $lnk.Save()
} catch { Write-Host "Could not create the shortcut: start KartCut.bat from this folder instead." }

# Tidy up the temporary files and download cache (several GB)
Remove-Item $Work -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $Root ".pipcache") -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "KartCut is installed. Double-click KartCut on your desktop (or KartCut.bat here)." -ForegroundColor Green
