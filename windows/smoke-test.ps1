<#
.SYNOPSIS
    Windows-side end-to-end test for claude-wsl-mcp.

.DESCRIPTION
    Reads mcpServers.<ServerName> from Claude Desktop's claude_desktop_config.json, starts exactly
    that command the same way Claude Desktop does (stdio pipes), performs the MCP handshake and
    calls tools. Every command goes through the run_command tool, so the output shows where the
    commands really execute.

    Works with Windows PowerShell 5.1 and PowerShell 7. This file is ASCII-only on purpose.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File \\wsl.localhost\Ubuntu-24.04\root\project\claude-wsl-mcp\windows\smoke-test.ps1
#>
param(
    [string]$ServerName = "wsl",
    [string]$ConfigPath,
    [string[]]$Commands = @("whoami", "pwd", "ls", "git status", "python --version", "node --version", "npm --version")
)

$ErrorActionPreference = "Stop"
$script:failures = New-Object System.Collections.Generic.List[string]
$script:nonJsonLines = 0
$script:nextId = 1
$script:pendingRead = $null

function Find-ClaudeConfig {
    $found = @()
    $packages = Join-Path $env:LOCALAPPDATA "Packages"
    if (Test-Path $packages) {
        foreach ($dir in Get-ChildItem $packages -Directory -Filter "Claude_*") {
            $candidate = Join-Path $dir.FullName "LocalCache\Roaming\Claude\claude_desktop_config.json"
            if (Test-Path $candidate) { $found += $candidate }
        }
    }
    $classic = Join-Path $env:APPDATA "Claude\claude_desktop_config.json"
    if (Test-Path $classic) { $found += $classic }
    return $found
}

function ConvertTo-ArgumentString([string[]]$Items) {
    $quoted = foreach ($item in $Items) {
        if ($item -match '[\s"]') {
            '"' + (($item -replace '(\\*)"', '$1$1\"') -replace '(\\+)$', '$1$1') + '"'
        } else {
            $item
        }
    }
    return ($quoted -join " ")
}

function Send-Json($Object) {
    $json = ConvertTo-Json -InputObject $Object -Depth 20 -Compress
    $ascii = [regex]::Replace($json, '[^\x00-\x7F]', { param($m) '\u{0:x4}' -f [int][char]$m.Value })
    $bytes = [System.Text.Encoding]::ASCII.GetBytes($ascii + "`n")
    $script:proc.StandardInput.BaseStream.Write($bytes, 0, $bytes.Length)
    $script:proc.StandardInput.BaseStream.Flush()
}

function Read-Response([int]$Id, [int]$TimeoutSeconds) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ($true) {
        if ($null -eq $script:pendingRead) { $script:pendingRead = $script:proc.StandardOutput.ReadLineAsync() }
        $remaining = [int]($deadline - [DateTime]::UtcNow).TotalMilliseconds
        if ($remaining -le 0 -or -not $script:pendingRead.Wait($remaining)) {
            throw "Timed out after $TimeoutSeconds s waiting for response id=$Id"
        }
        $line = $script:pendingRead.Result
        $script:pendingRead = $null
        if ($null -eq $line) { throw "Server closed stdout (exit code: $($script:proc.ExitCode))" }
        if (-not $line.Trim()) { continue }
        try {
            $message = $line | ConvertFrom-Json
        } catch {
            $script:nonJsonLines++
            Write-Warning "Non-JSON line on stdout (would corrupt the MCP stream): $line"
            continue
        }
        if (($message.PSObject.Properties.Name -contains "id") -and ($message.id -eq $Id)) { return $message }
    }
}

function Invoke-Request([string]$Method, $Params, [int]$TimeoutSeconds = 120) {
    $id = $script:nextId
    $script:nextId++
    $request = [ordered]@{ jsonrpc = "2.0"; id = $id; method = $Method }
    if ($null -ne $Params) { $request.params = $Params }
    Send-Json $request
    $response = Read-Response $id $TimeoutSeconds
    if ($response.PSObject.Properties.Name -contains "error") {
        throw "JSON-RPC error for ${Method}: $($response.error.message)"
    }
    return $response.result
}

function Invoke-Tool([string]$Name, $Arguments) {
    $result = Invoke-Request "tools/call" @{ name = $Name; arguments = $Arguments } 300
    $text = ($result.content | Where-Object { $_.type -eq "text" } | ForEach-Object { $_.text }) -join "`n"
    $isError = ($result.PSObject.Properties.Name -contains "isError") -and [bool]$result.isError
    return [pscustomobject]@{ IsError = $isError; Text = $text }
}

function Write-Section([string]$Title) {
    Write-Host ""
    Write-Host ("=" * 8 + " " + $Title + " " + "=" * 8) -ForegroundColor Cyan
}

function Test-Check([bool]$Condition, [string]$Description) {
    if ($Condition) {
        Write-Host "[PASS] $Description" -ForegroundColor Green
    } else {
        Write-Host "[FAIL] $Description" -ForegroundColor Red
        $script:failures.Add($Description)
    }
}

# ---------------------------------------------------------------- locate config
if (-not $ConfigPath) { $ConfigPath = @(Find-ClaudeConfig)[0] }
if (-not $ConfigPath -or -not (Test-Path $ConfigPath)) { throw "claude_desktop_config.json not found; pass -ConfigPath" }
$config = [System.IO.File]::ReadAllText($ConfigPath, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
if (-not ($config.PSObject.Properties.Name -contains "mcpServers") -or -not ($config.mcpServers.PSObject.Properties.Name -contains $ServerName)) {
    throw "mcpServers.$ServerName not found in $ConfigPath"
}
$entry = $config.mcpServers.$ServerName
$arguments = ConvertTo-ArgumentString @($entry.args)

Write-Section "Windows side (this script)"
Write-Host "PowerShell $($PSVersionTable.PSVersion) on $([System.Environment]::OSVersion.VersionString), pid $PID"
Write-Host "Config : $ConfigPath"
Write-Host "Launch : $($entry.command) $arguments"

# ---------------------------------------------------------------- start server exactly like Claude Desktop
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $entry.command
$psi.Arguments = $arguments
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$psi.RedirectStandardInput = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.StandardOutputEncoding = New-Object System.Text.UTF8Encoding $false
$psi.StandardErrorEncoding = New-Object System.Text.UTF8Encoding $false
# Windows PowerShell 5.1 (.NET Framework) wraps the child's stdin in a StreamWriter built from
# [Console]::InputEncoding and flushes the encoding preamble at Process.Start. With the system-wide
# UTF-8 code page that preamble is a BOM, and the MCP server silently drops the BOM-prefixed first
# message. Claude Desktop itself writes raw bytes and is not affected.
$savedInputEncoding = $null
try {
    $savedInputEncoding = [Console]::InputEncoding
    [Console]::InputEncoding = New-Object System.Text.UTF8Encoding $false
} catch { }
$script:proc = [System.Diagnostics.Process]::Start($psi)
if ($null -ne $savedInputEncoding) {
    try { [Console]::InputEncoding = $savedInputEncoding } catch { }
}
# If a BOM still went out (no console to reconfigure), a lone newline turns it into an ignorable line.
$script:proc.StandardInput.BaseStream.Write([byte[]](10), 0, 1)
$script:proc.StandardInput.BaseStream.Flush()
$stderrTask = $script:proc.StandardError.ReadToEndAsync()

try {
    Write-Section "MCP handshake"
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    $init = Invoke-Request "initialize" @{
        protocolVersion = "2025-06-18"
        capabilities    = @{}
        clientInfo      = @{ name = "claude-wsl-mcp-smoke-test"; version = "1.0" }
    } 90
    Send-Json ([ordered]@{ jsonrpc = "2.0"; method = "notifications/initialized" })
    Write-Host "Server  : $($init.serverInfo.name) $($init.serverInfo.version), protocol $($init.protocolVersion), ready in $([int]$watch.Elapsed.TotalMilliseconds) ms"
    $tools = (Invoke-Request "tools/list" @{} 30).tools
    Write-Host "Tools   : $(($tools | ForEach-Object { $_.name }) -join ', ')"
    Test-Check ($tools.Count -ge 12) "server exposes the 12 tools"

    Write-Section "wsl_info"
    $info = Invoke-Tool "wsl_info" @{}
    Write-Host $info.Text
    Test-Check ($info.Text -match "microsoft-standard-WSL2") "wsl_info reports a WSL2 kernel"

    foreach ($command in $Commands) {
        Write-Section "run_command: $command"
        $result = Invoke-Tool "run_command" @{ command = $command }
        Write-Host $result.Text
        Test-Check ((-not $result.IsError) -and ($result.Text -match "exit_code: 0")) "'$command' exits with 0"
    }

    Write-Section "Proof: where did that run?"
    $proof = Invoke-Tool "run_command" @{ command = 'uname -sr; echo "distro=$WSL_DISTRO_NAME"; echo "shell=$(readlink /proc/$$/exe)"; echo "rootfs=$(stat -f -c %T /)"; for c in python node npm git; do echo "$c -> $(command -v $c)"; done' }
    Write-Host $proof.Text
    Test-Check ($proof.Text -match "Linux \S+microsoft-standard-WSL2") "uname says Linux / WSL2"
    Test-Check ($proof.Text -match "shell=/usr/bin/bash") "commands run in Linux bash"
    Test-Check (-not ($proof.Text -match "-> /mnt/")) "node/npm/python/git resolve to Linux binaries, not Windows ones"

    Write-Section "Boundary: Windows host files and programs"
    $boundary = Invoke-Tool "run_command" @{ command = "ls -A /mnt/c | wc -l; touch /mnt/c/claude-wsl-mcp-should-fail 2>&1; cmd.exe /c ver 2>&1; true" }
    Write-Host $boundary.Text
    Test-Check (-not (Test-Path "C:\claude-wsl-mcp-should-fail")) "no file appeared on C:\"
    Test-Check ($boundary.Text -match "Read-only file system|No such file") "writing to /mnt/c fails inside the sandbox"
    Test-Check ($boundary.Text -match "cmd.exe: command not found|not found") "cmd.exe cannot be launched"
    Test-Check ($script:nonJsonLines -eq 0) "stdout carried only MCP JSON messages"
} finally {
    try { $script:proc.StandardInput.Close() } catch { }
    if (-not $script:proc.WaitForExit(10000)) { $script:proc.Kill() }
    $serverLog = $stderrTask.Result
    Write-Section "server stderr (last 10 lines)"
    ($serverLog -split "`n" | Where-Object { $_.Trim() } | Select-Object -Last 10) | ForEach-Object { Write-Host $_ }
}

Write-Section "Summary"
if ($script:failures.Count -eq 0) {
    Write-Host "ALL CHECKS PASSED" -ForegroundColor Green
    exit 0
}
Write-Host "$($script:failures.Count) check(s) failed:" -ForegroundColor Red
$script:failures | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
exit 1
