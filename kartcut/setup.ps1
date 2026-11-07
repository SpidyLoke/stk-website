# KartCut one-time setup for Windows 10/11 (run via setup_windows.bat)
# Installs: a private Python 3.12, its own environment, PyTorch with CUDA
# for your NVIDIA card, the AI libraries, FFmpeg with NVENC, and downloads the models.

$ErrorActionPreference = "Continue"  # native tools write to stderr; we check exit codes instead
$ProgressPreference = "SilentlyContinue"
# Older Windows 10 PowerShell defaults to TLS 1.0, which python.org and gyan.dev refuse.
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Step($t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Yellow }
function Fail($t) { Write-Host ""; Write-Host "SETUP FAILED: $t" -ForegroundColor Red; exit 1 }
function Have($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

# A Python is only usable if its standard library is complete. (Damaged installs that
# are missing modules like difflib exist in the wild and break numpy/scipy at start-up.)
$HealthCode = "import sys, struct, difflib, unittest, venv, ensurepip, ssl, sqlite3, ctypes, json, zipfile; sys.exit(0 if struct.calcsize('P') == 8 and (3, 10) <= sys.version_info[:2] <= (3, 13) else 3)"
function Test-Python($exe) {
  if (-not $exe -or -not (Test-Path $exe)) { return $false }
  & $exe -c $HealthCode 2>$null | Out-Null
  return ($LASTEXITCODE -eq 0)
}

function Find-SystemPython {
  # Fallback only: first healthy 64-bit Python 3.10 to 3.13 on the machine.
  $cands = @()
  if (Have "py") {
    foreach ($ver in @("3.12", "3.11", "3.13", "3.10")) {
      try {
        $v = & py "-$ver" -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $v) { $cands += ("$v").Trim() }
      } catch {}
    }
  }
  foreach ($d in @("Python312", "Python311", "Python313", "Python310")) {
    foreach ($base in @((Join-Path $env:LOCALAPPDATA "Programs\Python"), $env:ProgramFiles)) {
      $cands += (Join-Path $base "$d\python.exe")
    }
  }
  foreach ($c in $cands) {
    if (Test-Python $c) { return $c }
    if (Test-Path $c) { Write-Host "Skipping damaged Python: $c" -ForegroundColor Yellow }
  }
  return $null
}

# ---------------------------------------------------------------- Python
# KartCut uses its own private Python 3.12 in .\python so nothing else on the laptop matters.
Step "Setting up KartCut's own Python 3.12"
$PrivDir = Join-Path $Root "python"
$PrivPy = Join-Path $PrivDir "python.exe"
$py = $null
if (Test-Python $PrivPy) {
  $py = $PrivPy
} else {
  if (Test-Path $PrivDir) { Remove-Item $PrivDir -Recurse -Force -ErrorAction SilentlyContinue }
  Write-Host "Downloading Python 3.12 from python.org (about 25 MB)..."
  $exe = Join-Path $env:TEMP "python-3.12.10-amd64.exe"
  try {
    Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" -OutFile $exe -UseBasicParsing -ErrorAction Stop
    Write-Host "Installing it into $PrivDir (no admin needed, your other Pythons are left alone)..."
    $instArgs = "/quiet InstallAllUsers=0 TargetDir=`"$PrivDir`" PrependPath=0 Include_launcher=0 AssociateFiles=0 Shortcuts=0 Include_doc=0"
    $p = Start-Process -FilePath $exe -ArgumentList $instArgs -Wait -PassThru
    if ($p.ExitCode -ne 0) { Write-Host "Python installer exit code $($p.ExitCode)" -ForegroundColor Yellow }
  } catch {
    Write-Host "Python download failed: $_" -ForegroundColor Yellow
  }
  if (Test-Python $PrivPy) { $py = $PrivPy }
}
if (-not $py) {
  Write-Host "Private Python not available, looking for a healthy one already installed..." -ForegroundColor Yellow
  $py = Find-SystemPython
}
if (-not $py) {
  Fail "Could not get a working Python. Install Python 3.12 (64-bit) from python.org/downloads/windows, then run setup again."
}
Write-Host "Using $py" -ForegroundColor Green

# ---------------------------------------------------------------- venv
Step "Creating KartCut's Python environment (.venv)"
$vpy = Join-Path $Root ".venv\Scripts\python.exe"
$marker = Join-Path $Root ".venv\kartcut_base.txt"
$fresh = $true
if ((Test-Python $vpy) -and (Test-Path $marker)) {
  if ((Get-Content $marker -Raw).Trim() -eq $py) { $fresh = $false }
}
if ($fresh) {
  if (Test-Path ".venv") {
    Write-Host "Removing the old environment (built on a different or damaged Python)..."
    Remove-Item ".venv" -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path ".venv") { Fail "Could not delete the old .venv folder. Close every KartCut window, then run setup again." }
  }
  & $py -m venv .venv
  if ($LASTEXITCODE -ne 0) { Fail "could not create .venv" }
  Set-Content -Path $marker -Value $py -Encoding ASCII
}
if (-not (Test-Python $vpy)) { Fail "The new Python environment failed its self-check." }
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

Write-Host ""
Write-Host "KartCut is installed. Double-click KartCut on your desktop (or KartCut.bat here)." -ForegroundColor Green
