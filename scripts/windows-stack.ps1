param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "restart", "status", "health", "logs", "open")]
    [string]$Command = "status"
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
$ComposeArgs = @("--profile", "app")
$FrontendUrl = "http://127.0.0.1:4173"
$ApiBaseUrl = "http://127.0.0.1:8000"

function Invoke-Compose {
    param([string[]]$Arguments)
    Push-Location $RepoRoot
    try {
        & docker compose @ComposeArgs @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "docker compose failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}

function Ensure-EnvFile {
    $envPath = Join-Path $RepoRoot ".env"
    $examplePath = Join-Path $RepoRoot ".env.example"
    if (-not (Test-Path $envPath)) {
        Copy-Item $examplePath $envPath
        Write-Host "Created .env from .env.example. Review it before using this outside a local demo."
    }
}

function New-LocalSecret {
    $bytes = New-Object byte[] 48
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }
    return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

function Ensure-EnvSecret {
    param(
        [string]$Content,
        [string]$Name,
        [string[]]$UnsafeValues = @()
    )

    $match = [regex]::Match($Content, "(?m)^$([regex]::Escape($Name))=(.*)$")
    $value = if ($match.Success) { $match.Groups[1].Value.Trim() } else { "" }
    if (-not $value -or $UnsafeValues -contains $value) {
        $value = New-LocalSecret
        $line = "$Name=$value"
        if ($match.Success) {
            $Content = $Content.Remove($match.Index, $match.Length).Insert($match.Index, $line)
        }
        else {
            $Content = $Content.TrimEnd() + [Environment]::NewLine + $line + [Environment]::NewLine
        }
    }
    elseif ($value.Length -lt 32) {
        throw "$Name must contain at least 32 characters"
    }

    return @{
        Content = $Content
        Value = $value
    }
}

function Ensure-LocalSecrets {
    $envPath = Join-Path $RepoRoot ".env"
    $content = [IO.File]::ReadAllText($envPath)
    $admin = Ensure-EnvSecret `
        -Content $content `
        -Name "COMPOSE_ADMIN_BEARER_TOKEN" `
        -UnsafeValues @("compose_dev_admin_token", "admin_dev_token")
    $session = Ensure-EnvSecret `
        -Content $admin.Content `
        -Name "COMPOSE_ADMIN_SESSION_SIGNING_SECRET" `
        -UnsafeValues @("compose_dev_admin_session_signing_secret")
    $media = Ensure-EnvSecret `
        -Content $session.Content `
        -Name "COMPOSE_MEDIA_ID_SIGNING_SECRET" `
        -UnsafeValues @("compose_dev_media_id_signing_secret")
    if (
        $admin.Value -eq $session.Value -or
        $admin.Value -eq $media.Value -or
        $session.Value -eq $media.Value
    ) {
        throw "Administrator, session signing, and media signing secrets must be independent"
    }
    [IO.File]::WriteAllText(
        $envPath,
        $media.Content,
        (New-Object System.Text.UTF8Encoding($false))
    )
    Write-Host "Administrator token: $($admin.Value)"
}

function Test-HttpEndpoint {
    param(
        [string]$Name,
        [string]$Url
    )
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
        Write-Host ("{0}: ok ({1})" -f $Name, [int]$response.StatusCode)
    }
    catch {
        Write-Host ("{0}: failed - {1}" -f $Name, $_.Exception.Message)
        $script:HealthFailed = $true
    }
}

switch ($Command) {
    "start" {
        Ensure-EnvFile
        Ensure-LocalSecrets
        Invoke-Compose -Arguments @("up", "-d", "--build")
        Write-Host "Agent Console UI: $FrontendUrl"
    }
    "stop" {
        Invoke-Compose -Arguments @("stop")
    }
    "restart" {
        Ensure-EnvFile
        Ensure-LocalSecrets
        Invoke-Compose -Arguments @("up", "-d", "--build")
        Write-Host "Agent Console UI: $FrontendUrl"
    }
    "status" {
        Invoke-Compose -Arguments @("ps")
    }
    "health" {
        $script:HealthFailed = $false
        Test-HttpEndpoint "api healthz" "$ApiBaseUrl/healthz"
        Test-HttpEndpoint "api readyz" "$ApiBaseUrl/readyz"
        Test-HttpEndpoint "wxbot bridge" "$ApiBaseUrl/plugins/wxbot/bridge/status"
        Test-HttpEndpoint "frontend" "$FrontendUrl/"
        if ($script:HealthFailed) {
            exit 1
        }
    }
    "logs" {
        Invoke-Compose -Arguments @("logs", "-f", "--tail=100")
    }
    "open" {
        Start-Process $FrontendUrl
    }
}
