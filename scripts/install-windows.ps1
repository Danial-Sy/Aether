[CmdletBinding()]
param(
    [ValidateSet("Auto", "Chat", "Full")]
    [string]$Models = "Auto",
    [switch]$SkipModels,
    [switch]$NoShortcut,
    [switch]$NonInteractive
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

function Write-Step([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Stop-Setup([string]$Message) {
    throw $Message
}

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Test-PythonSpec($Spec) {
    if (-not $Spec) { return $false }
    try {
        $prefix = @($Spec.Prefix)
        $version = & $Spec.Exe @prefix -c "import sys; print('.'.join(map(str, sys.version_info[:3]))); raise SystemExit(sys.version_info < (3, 11))" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $Spec.Version = ($version | Select-Object -Last 1)
            return $true
        }
    } catch {}
    return $false
}

function Find-Python {
    $candidates = @()
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) { $candidates += ,@{ Exe = $py.Source; Prefix = @("-3") } }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) { $candidates += ,@{ Exe = $python.Source; Prefix = @() } }

    $patterns = @(
        "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
        "$env:ProgramFiles\Python3*\python.exe"
    )
    foreach ($pattern in $patterns) {
        Get-Item $pattern -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | ForEach-Object {
            $candidates += ,@{ Exe = $_.FullName; Prefix = @() }
        }
    }
    foreach ($candidate in $candidates) {
        if (Test-PythonSpec $candidate) { return $candidate }
    }
    return $null
}

function Find-Ollama {
    $command = Get-Command ollama.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $candidates = @(
        $env:AETHER_OLLAMA,
        "C:\AI\ollama\ollama.exe",
        "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe",
        "$env:ProgramFiles\Ollama\ollama.exe"
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return $candidate
        }
    }
    return $null
}

function Install-WithWinget([string]$Id, [string]$Name) {
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        Stop-Setup "$Name is missing and Windows Package Manager (winget) is unavailable. Install $Name manually, then run Install-Aether.bat again."
    }
    Write-Host "Installing $Name with winget..."
    & $winget.Source install --exact --id $Id --accept-package-agreements --accept-source-agreements --silent
    if ($LASTEXITCODE -ne 0) {
        Stop-Setup "winget could not install $Name (exit $LASTEXITCODE). Install it manually, then run Install-Aether.bat again."
    }
    Refresh-Path
}

function Get-Hardware {
    $computer = Get-CimInstance Win32_ComputerSystem
    $ram = [math]::Round([double]$computer.TotalPhysicalMemory / 1GB, 1)
    $modelRoot = if ($env:OLLAMA_MODELS) { $env:OLLAMA_MODELS } else { "$env:USERPROFILE\.ollama\models" }
    $free = -1.0
    try {
        $modelDrive = [IO.Path]::GetPathRoot($modelRoot)
        if ($modelDrive -match "^[A-Za-z]:") {
            $drive = Get-PSDrive -Name $modelDrive.Substring(0, 1) -ErrorAction Stop
            $free = [math]::Round([double]$drive.Free / 1GB, 1)
        }
    } catch {}
    $gpuName = "Not detected"
    $vram = 0.0

    $nvidia = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
    $nvidiaPath = if ($nvidia) { $nvidia.Source } else { $null }
    if (-not $nvidiaPath) {
        $defaultNvidia = "$env:ProgramFiles\NVIDIA Corporation\NVSMI\nvidia-smi.exe"
        if (Test-Path -LiteralPath $defaultNvidia) { $nvidiaPath = $defaultNvidia }
    }
    if ($nvidiaPath) {
        try {
            $rows = & $nvidiaPath --query-gpu=name,memory.total --format=csv,noheader,nounits 2>$null
            foreach ($row in $rows) {
                $parts = $row -split ",", 2
                if ($parts.Count -eq 2) {
                    $candidate = [math]::Round(([double]$parts[1].Trim()) / 1024, 1)
                    if ($candidate -gt $vram) {
                        $gpuName = $parts[0].Trim()
                        $vram = $candidate
                    }
                }
            }
        } catch {}
    } else {
        try {
            $display = Get-CimInstance Win32_VideoController | Sort-Object AdapterRAM -Descending | Select-Object -First 1
            if ($display) { $gpuName = $display.Name }
        } catch {}
    }
    return @{
        RamGB = $ram
        VramGB = $vram
        GpuName = $gpuName
        FreeGB = $free
        ModelRoot = $modelRoot
    }
}

function Get-OllamaApiBase {
    $value = if ($env:OLLAMA_HOST) { $env:OLLAMA_HOST.Trim() } else { "http://127.0.0.1:11434" }
    if ($value -notmatch "^https?://") { $value = "http://$value" }
    return $value.TrimEnd("/")
}

function Test-OllamaApi {
    try {
        [void](Invoke-RestMethod -Uri "$(Get-OllamaApiBase)/api/tags" -Method Get -TimeoutSec 3 -ErrorAction Stop)
        return $true
    } catch {}
    return $false
}

function Wait-Ollama([string]$Ollama) {
    if (Test-OllamaApi) { return $true }
    $api = Get-OllamaApiBase
    if ($api -notmatch "^https?://(127\.0\.0\.1|localhost)(:\d+)?$") {
        Write-Warning "Configured remote Ollama host is not reachable: $api"
        return $false
    }
    try {
        Start-Process -FilePath $Ollama -ArgumentList @("serve") -WindowStyle Hidden
    } catch {
        Write-Warning "Could not start Ollama: $($_.Exception.Message)"
        return $false
    }
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        Start-Sleep -Milliseconds 500
        if (Test-OllamaApi) { return $true }
    }
    return $false
}

function Normalize-ModelTag([string]$Name) {
    if ([string]::IsNullOrWhiteSpace($Name)) { return "" }
    $value = $Name.Trim().ToLowerInvariant()
    foreach ($prefix in @("registry.ollama.ai/library/", "library/")) {
        if ($value.StartsWith($prefix)) {
            $value = $value.Substring($prefix.Length)
            break
        }
    }
    if ($value -and -not $value.Contains(":")) { $value = "$value`:latest" }
    return $value
}

function Get-InstalledModels([string]$Ollama) {
    $names = @()
    try {
        $response = Invoke-RestMethod -Uri "$(Get-OllamaApiBase)/api/tags" -Method Get -TimeoutSec 10 -ErrorAction Stop
        foreach ($model in @($response.models)) {
            $name = if ($model.name) { [string]$model.name } else { [string]$model.model }
            if ($name -and $names -notcontains $name) { $names += $name }
        }
        if ($names.Count -gt 0) { return $names }
    } catch {}

    try {
        $rows = @(& $Ollama list 2>$null)
        if ($LASTEXITCODE -eq 0) {
            foreach ($row in @($rows | Select-Object -Skip 1)) {
                $name = @($row -split "\s+")[0]
                if ($name -and $names -notcontains $name) { $names += $name }
            }
        }
    } catch {}
    return $names
}

function Has-Model([string[]]$Installed, [string]$Tag) {
    $wanted = Normalize-ModelTag $Tag
    foreach ($model in @($Installed)) {
        if ((Normalize-ModelTag $model) -eq $wanted) { return $true }
    }
    return $false
}

function Get-InstallPlan($Python, [string[]]$Installed) {
    # models.py needs only the standard library, so it runs before pip does.
    # Keeping the catalog in one place stops this script drifting from the app.
    try {
        $raw = & $Python.Exe "$Root\models.py" "--plan" 2>$null
        if ($LASTEXITCODE -eq 0 -and $raw) { return ($raw -join "`n" | ConvertFrom-Json) }
    } catch {}
    return $null
}

function Show-Choice([int]$Number, [string]$Title, $Choice, [string[]]$Installed) {
    $gb = [math]::Round([double]$Choice.download_bytes / 1GB, 1)
    $detail = if ($Choice.missing.Count -gt 0) { "$gb GB to download" } else { "already installed" }
    Write-Host "  $Number. $Title ($detail)"
    if ($Choice.note) { Write-Host "     $($Choice.note)" }
    foreach ($tag in $Choice.tags) {
        $mark = if (Has-Model $Installed $tag) { "have" } else { "get " }
        Write-Host "       [$mark] $tag"
    }
}

function Read-CustomTags($Python, $Plan) {
    Write-Host ""
    Write-Host "Browse models at $($Plan.library_url) and copy the tag, for example qwen3:8b."
    Write-Host "Enter one tag per line. Blank line when done."
    $tags = New-Object System.Collections.Generic.List[string]
    while ($true) {
        $raw = (Read-Host "  tag").Trim()
        if ([string]::IsNullOrWhiteSpace($raw)) {
            if ($tags.Count -gt 0) { break }
            Write-Host "     At least one model is needed."
            continue
        }
        # Checked against Ollama's registry before anything is downloaded.
        $size = & $Python.Exe -c "import sys, models; sys.stdout.write(str(models.manifest_size(sys.argv[1])))" $raw 2>$null
        if (-not $size -or [int]$size -le 0) {
            Write-Host "     Ollama has no model called $raw. Check the tag and try again."
            continue
        }
        $gb = [math]::Round([double]$size / 1GB, 1)
        Write-Host "     $raw found, $gb GB."
        $tags.Add($raw) | Out-Null
    }
    return $tags.ToArray()
}

function Choose-Models($Python, $Plan, [string[]]$Installed) {
    if ($Models -eq "Chat") { return @($Plan.recommended.tags) }
    if ($Models -eq "Full") { return @($Plan.default.tags) }

    Write-Host ""
    Write-Host "Which models?" -ForegroundColor Yellow
    Show-Choice 1 "Default" $Plan.default $Installed
    Write-Host ""
    Show-Choice 2 "Recommended" $Plan.recommended $Installed
    Write-Host ""
    Write-Host "  3. Type your own"
    Write-Host "     Any tag from $($Plan.library_url). Checked before anything downloads."
    Write-Host ""

    # Recommended is sized for this machine; when it already is the tuned pair
    # the two choices are the same thing.
    $default = if ($Plan.same) { "1" } else { "2" }
    if ($NonInteractive) { $answer = "" } else { $answer = Read-Host "Choose 1, 2 or 3 [recommended: $default]" }
    if ([string]::IsNullOrWhiteSpace($answer)) { $answer = $default }

    switch ($answer) {
        "1" { return @($Plan.default.tags) }
        "3" { return @(Read-CustomTags $Python $Plan) }
        default { return @($Plan.recommended.tags) }
    }
}

function Set-OllamaEnvironment([string]$Ollama) {
    $values = @{
        AETHER_OLLAMA = $Ollama
        OLLAMA_KV_CACHE_TYPE = "q8_0"
        OLLAMA_FLASH_ATTENTION = "1"
        OLLAMA_NUM_PARALLEL = "1"
        GGML_CUDA_ENABLE_UNIFIED_MEMORY = "1"
    }
    foreach ($item in $values.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($item.Key, $item.Value, "User")
        Set-Item -Path "Env:$($item.Key)" -Value $item.Value
    }
}

function New-AetherShortcuts {
    $shell = New-Object -ComObject WScript.Shell
    $desktop = [Environment]::GetFolderPath("Desktop")
    $programs = [Environment]::GetFolderPath("Programs")
    $target = "$env:WINDIR\System32\wscript.exe"
    $arguments = "//nologo `"$Root\Aether.vbs`""
    $icon = "$Root\static\assets\aether.ico"

    foreach ($folder in @($desktop, $programs)) {
        if (-not (Test-Path -LiteralPath $folder)) { continue }
        $shortcut = $shell.CreateShortcut((Join-Path $folder "Aether.lnk"))
        $shortcut.TargetPath = $target
        $shortcut.Arguments = $arguments
        $shortcut.WorkingDirectory = $Root
        $shortcut.Description = "Aether local AI desktop agent"
        if (Test-Path -LiteralPath $icon) { $shortcut.IconLocation = "$icon,0" }
        $shortcut.Save()
    }
}

try {
    Write-Host "Aether Setup Wizard" -ForegroundColor Magenta
    Write-Host "Installs local dependencies, Ollama models, and desktop shortcuts."

    Write-Step "Checking Python 3.11+"
    $python = Find-Python
    if (-not $python) {
        Install-WithWinget "Python.Python.3.12" "Python 3.12"
        $python = Find-Python
    }
    if (-not $python) { Stop-Setup "Python 3.11+ was installed but could not be located. Restart Windows and run this installer again." }
    Write-Host "Python $($python.Version): $($python.Exe)"

    Write-Step "Checking Ollama"
    $ollama = Find-Ollama
    if (-not $ollama) {
        Install-WithWinget "Ollama.Ollama" "Ollama"
        $ollama = Find-Ollama
    }
    if (-not $ollama) { Stop-Setup "Ollama was installed but could not be located. Restart Windows and run this installer again." }
    Write-Host "Ollama: $ollama"
    $env:AETHER_OLLAMA = $ollama

    Write-Step "Configuring Ollama"
    Set-OllamaEnvironment $ollama
    Write-Host "Saved the Ollama path, Flash Attention, q8 KV cache, single-request, and unified-memory settings for this user."

    Write-Step "Starting Ollama"
    if (-not (Wait-Ollama $ollama)) {
        Stop-Setup "Ollama was found but its API did not become ready at $(Get-OllamaApiBase). Restart Windows, then run Install-Aether.bat again."
    }
    Write-Host "Ollama API is ready: $(Get-OllamaApiBase)"

    Write-Step "Inspecting this PC"
    $hardware = Get-Hardware
    Write-Host "RAM: $($hardware.RamGB) GB"
    $gpuLine = $hardware.GpuName
    if ($hardware.VramGB -gt 0) { $gpuLine = "{0} ({1} GB VRAM)" -f $hardware.GpuName, $hardware.VramGB }
    Write-Host "GPU: $gpuLine"
    Write-Host "Ollama model store: $($hardware.ModelRoot)"
    if ($hardware.FreeGB -ge 0) {
        Write-Host "Free disk for models: $($hardware.FreeGB) GB"
    } else {
        Write-Host "Free disk for models: could not be measured for this location"
    }

    $installed = @(Get-InstalledModels $ollama)
    if ($installed.Count -gt 0) {
        Write-Host "Existing Ollama models: $($installed -join ', ')"
    } else {
        Write-Host "Existing Ollama models: none detected"
    }
    $plan = Get-InstallPlan $python $installed
    $chosen = @()
    if ($SkipModels) {
        Write-Host "Skipping model downloads."
    } elseif (-not $plan) {
        Write-Warning "Could not read the model catalog; falling back to the default pair."
        $chosen = @("qwen2.5:0.5b", "qwen3.8:27b", "qwen3-coder:30b")
    } else {
        $chosen = Choose-Models $python $plan $installed
    }

    $missingModels = @($chosen | Where-Object { -not (Has-Model $installed $_) })
    if ($missingModels.Count -gt 0) {
        Write-Host "Models to download: $($missingModels -join ', ')"
    } elseif (-not $SkipModels) {
        Write-Host "Models to download: none (everything chosen is already here)"
    }
    # Sizes come from Ollama's registry, so the disk check is exact rather than
    # a table of numbers that goes stale.
    $requiredBytes = 0
    foreach ($tag in $missingModels) {
        $size = & $python.Exe -c "import sys, models; sys.stdout.write(str(models.manifest_size(sys.argv[1])))" $tag 2>$null
        if ($size) { $requiredBytes += [double]$size }
    }
    $requiredDisk = [math]::Round($requiredBytes / 1GB, 1)
    if (-not $SkipModels -and $requiredDisk -gt 0 -and $hardware.FreeGB -ge 0 -and $hardware.FreeGB -lt $requiredDisk) {
        Stop-Setup "That choice needs roughly $requiredDisk GB free on this drive; only $($hardware.FreeGB) GB is available."
    }
    # Warn on the choice, not on a particular model: the user picked something
    # the plan did not size for this machine.
    $overrode = $plan -and (-not $plan.same) -and
                (@(Compare-Object $chosen @($plan.default.tags) -SyncWindow 0).Count -eq 0)
    if (-not $SkipModels -and $overrode) {
        Write-Warning "That is larger than this PC was sized for and may be very slow. Recommended installs $($plan.recommended.tags -join ', ') instead."
        if (-not $NonInteractive) {
            $continue = Read-Host "Continue anyway? [y/N]"
            if ($continue -notmatch "^(y|yes)$") { Stop-Setup "Setup cancelled before downloading model weights." }
        }
    }

    Write-Step "Creating Aether environment and installing models"
    $setupArgs = @($python.Prefix) + @((Join-Path $Root "setup.py"))
    if ($SkipModels) {
        $setupArgs += "--skip-models"
    } else {
        $setupArgs += @("--tags") + $chosen
    }
    & $python.Exe @setupArgs
    if ($LASTEXITCODE -ne 0) { Stop-Setup "Aether's Python setup failed (exit $LASTEXITCODE)." }

    if (-not $NoShortcut) {
        Write-Step "Creating shortcuts"
        New-AetherShortcuts
        Write-Host "Created Aether shortcuts on the Desktop and in the Start Menu."
    }

    Write-Host "`nAether is ready." -ForegroundColor Green
    Write-Host "Open the Aether desktop shortcut, or run Aether.bat from this folder."
    if (-not $NonInteractive) { [void](Read-Host "Press Enter to close setup") }
    exit 0
} catch {
    Write-Host "`nERROR: $($_.Exception.Message)" -ForegroundColor Red
    if (-not $NonInteractive) { [void](Read-Host "Press Enter to close setup") }
    exit 1
}
