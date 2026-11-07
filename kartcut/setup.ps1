# KartCut one-time setup for Windows 10/11 (run via setup_windows.bat)
# Installs: Python 3.12 (if missing), a private Python environment, PyTorch with CUDA
# for your NVIDIA card, the AI libraries, FFmpeg with NVENC, and downloads the models.

$ErrorActionPreference = "Continue"  # native tools write to stderr; we check exit codes instead
$ProgressPreference = "SilentlyContinue"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Step($t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Yellow }
function Fail($t) { Write-Host ""; Write-Host "SETUP FAILED: $t" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------- Python 3.12
Step "Checking Python 3.12"
$py = $null
try { $v = & py -3.12 -c "import sys; print(sys.executable)" 2>$null; if ($LASTEXITCODE -eq 0 -and $v) { $py = $v.Trim() } } catch {}
if (-not $py) {
  $cand = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
  if (Test-Path $cand) { $py = $cand }
}
if (-not $py) {
  Write-Host "Installing Python 3.12 with winget..."
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
  $cand = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
  if (Test-Path $cand) { $py = $cand } else { Fail "Python 3.12 did not install. Install it from python.org, then run setup again." }
}
Write-Host "Using $py"

# ---------------------------------------------------------------- venv
Step "Creating KartCut's own Python environment (.venv)"
if (-not (Test-Path ".venv\Scripts\python.exe")) { & $py -m venv .venv; if ($LASTEXITCODE -ne 0) { Fail "could not create .venv" } }
$vpy = Join-Path $Root ".venv\Scripts\python.exe"
& $vpy -m pip install --upgrade pip wheel setuptools | Out-Host

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
  & $vpy -m pip install --upgrade torch torchvision --index-url "https://download.pytorch.org/whl/$idx" | Out-Host
  if ($LASTEXITCODE -eq 0) {
    & $vpy -c "import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)"
    if ($LASTEXITCODE -eq 0) { $torchOk = $true; Write-Host "PyTorch sees the GPU." -ForegroundColor Green; break }
  }
}
if (-not $torchOk) {
  & $vpy -c "import torch" 2>$null
  if ($LASTEXITCODE -ne 0) {
    & $vpy -m pip install torch torchvision --index-url "https://download.pytorch.org/whl/cpu" | Out-Host
    if ($LASTEXITCODE -ne 0) { Fail "could not install PyTorch" }
  }
  if ($cuda) { Write-Host "GPU build did not work: running on CPU. Update the NVIDIA driver and re-run setup." -ForegroundColor Yellow }
}

# ---------------------------------------------------------------- libraries
Step "Installing KartCut libraries"
& $vpy -m pip install -r requirements.txt | Out-Host
if ($LASTEXITCODE -ne 0) { Fail "pip install -r requirements.txt failed" }
# CLIP: text encoder for YOLO-World's 'go-kart' prompt + appearance matching
$clipOk = $false
foreach ($u in @("https://github.com/ultralytics/CLIP/archive/refs/heads/main.zip",
                 "https://github.com/openai/CLIP/archive/refs/heads/main.zip")) {
  & $vpy -m pip install $u | Out-Host
  if ($LASTEXITCODE -eq 0) { $clipOk = $true; break }
}
if (-not $clipOk) { & $vpy -m pip install openai-clip | Out-Host }

# ---------------------------------------------------------------- FFmpeg
Step "Installing FFmpeg (with NVIDIA NVENC)"
$ff = Join-Path $Root "bin\ffmpeg\bin\ffmpeg.exe"
if (-not (Test-Path $ff)) {
  $zip = Join-Path $env:TEMP "kartcut_ffmpeg.zip"
  $tmp = Join-Path $env:TEMP "kartcut_ffmpeg"
  try {
    Invoke-WebRequest -Uri "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" -OutFile $zip -ErrorAction Stop
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
    Expand-Archive -Path $zip -DestinationPath $tmp -Force -ErrorAction Stop
    $inner = Get-ChildItem $tmp -Directory | Select-Object -First 1
    New-Item -ItemType Directory -Force -Path (Join-Path $Root "bin") | Out-Null
    if (Test-Path (Join-Path $Root "bin\ffmpeg")) { Remove-Item (Join-Path $Root "bin\ffmpeg") -Recurse -Force }
    Move-Item $inner.FullName (Join-Path $Root "bin\ffmpeg") -ErrorAction Stop
    Remove-Item $zip -Force
  } catch {
    Write-Host "Direct download failed ($_). Trying winget..." -ForegroundColor Yellow
    winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
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

Write-Host ""
Write-Host "KartCut is installed. Double-click KartCut on your desktop (or KartCut.bat here)." -ForegroundColor Green
