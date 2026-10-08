param(
    [Parameter(Mandatory = $true)]
    [string]$HealthUrl,

    [Parameter(Mandatory = $true)]
    [string]$AppUrl,

    [ValidateRange(1, 600)]
    [int]$TimeoutSeconds = 180,

    [switch]$NoLaunch
)

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)

do {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $HealthUrl -TimeoutSec 2
        if ($response.StatusCode -eq 200) {
            if (-not $NoLaunch) {
                Start-Process -FilePath $AppUrl
            }
            exit 0
        }
    }
    catch {
        # The server is still importing dependencies or has not bound the port yet.
    }

    Start-Sleep -Milliseconds 400
} while ((Get-Date) -lt $deadline)

exit 1
