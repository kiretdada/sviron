param(
    [string]$Url = "https://pro-siv.interieur.gouv.fr/map-ppa-ui/do/home",
    [string]$ChromeExecutable = "",
    [string]$UserDataDir = "",
    [string]$DiskCacheDir = "",
    [int]$BrowserProcessId = 0,
    [int]$CertificateDelayMs = 800,
    [switch]$UseExistingBrowserWindow
)

$ErrorActionPreference = "Stop"

# =========================
# تنظیمات
# =========================

$url = $Url
$pinFile = "$env:APPDATA\SIV-token-pin.dat"
$logFile = "C:\SIV\siv-login.log"

# مدت انتظار بعد از ثبت PIN برای ظاهرشدن پنجره Certificate
# اگر گاهی زود Enter می‌زند، این مقدار را روی 1200 قرار بده.
$certificateDelayMs = $CertificateDelayMs

$bstr = [IntPtr]::Zero
$securePin = $null
$encryptedPin = $null
$pin = $null

# =========================
# توابع
# =========================

function Write-Log {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss.fff"
    Add-Content `
        -Path $logFile `
        -Value "[$timestamp] $Message" `
        -Encoding UTF8
}

function Find-ChromeExecutable {
    $candidates = @()

    if ($ChromeExecutable) {
        $candidates += $ChromeExecutable
    }

    $candidates += @(
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
    )

    $registryPaths = @(
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
        "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
    )

    foreach ($registryPath in $registryPaths) {
        try {
            $key = Get-Item -Path $registryPath -ErrorAction Stop
            $registryChrome = $key.GetValue("")

            if ($registryChrome) {
                $candidates += $registryChrome
            }
        }
        catch {
            # مسیر رجیستری وجود ندارد.
        }
    }

    try {
        $chromeCommand = Get-Command "chrome.exe" -ErrorAction Stop

        if ($chromeCommand.Source) {
            $candidates += $chromeCommand.Source
        }
    }
    catch {
        # Chrome داخل PATH نیست.
    }

    return $candidates |
        Where-Object {
            $_ -and (Test-Path $_)
        } |
        Select-Object -Unique |
        Select-Object -First 1
}

function Get-ChromeWindow {
    param(
        [int]$PreferredProcessId = 0
    )

    if ($PreferredProcessId -gt 0) {
        $preferredWindow = Get-Process -Id $PreferredProcessId -ErrorAction SilentlyContinue

        if ($preferredWindow -and $preferredWindow.MainWindowHandle -ne 0) {
            return $preferredWindow
        }
    }

    if ($UserDataDir) {
        $profileProcessIds = Get-CimInstance Win32_Process -Filter "Name = 'chrome.exe'" -ErrorAction SilentlyContinue |
            Where-Object {
                $_.CommandLine -like "*$UserDataDir*"
            } |
            Select-Object -ExpandProperty ProcessId

        foreach ($profileProcessId in $profileProcessIds) {
            $profileWindow = Get-Process -Id $profileProcessId -ErrorAction SilentlyContinue

            if ($profileWindow -and $profileWindow.MainWindowHandle -ne 0) {
                return $profileWindow
            }
        }
    }

    $chromeWindows = Get-Process "chrome" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.MainWindowHandle -ne 0
        }

    if (-not $chromeWindows) {
        return $null
    }

    # اول پنجره مربوط به SIV را پیدا می‌کند.
    $sivWindow = $chromeWindows |
        Where-Object {
            $_.MainWindowTitle -match "pro-siv" -or
            $_.MainWindowTitle -match "interieur" -or
            $_.MainWindowTitle -match "Loading" -or
            $_.MainWindowTitle -match "SIV"
        } |
        Select-Object -First 1

    if ($sivWindow) {
        return $sivWindow
    }

    # اگر عنوان قابل‌تشخیص نبود، اولین پنجره Chrome را برمی‌گرداند.
    return $chromeWindows | Select-Object -First 1
}

function Activate-WindowByTitle {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Shell,

        [Parameter(Mandatory = $true)]
        [string[]]$Titles
    )

    foreach ($title in $Titles) {
        try {
            if ($Shell.AppActivate($title)) {
                Write-Log "Window activated: $title"
                return $true
            }
        }
        catch {
            # پنجره هنوز ظاهر نشده است.
        }
    }

    return $false
}

function Convert-ToSendKeysLiteral {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Text
    )

    $result = New-Object System.Text.StringBuilder

    foreach ($character in $Text.ToCharArray()) {
        switch ($character) {
            "+" {
                [void]$result.Append("{+}")
            }

            "^" {
                [void]$result.Append("{^}")
            }

            "%" {
                [void]$result.Append("{%}")
            }

            "~" {
                [void]$result.Append("{~}")
            }

            "(" {
                [void]$result.Append("{(}")
            }

            ")" {
                [void]$result.Append("{)}")
            }

            "[" {
                [void]$result.Append("{[}")
            }

            "]" {
                [void]$result.Append("{]}")
            }

            "{" {
                [void]$result.Append("{{}")
            }

            "}" {
                [void]$result.Append("{}}")
            }

            default {
                [void]$result.Append($character)
            }
        }
    }

    return $result.ToString()
}

# =========================
# اجرای اصلی
# =========================

try {
    if (-not (Test-Path "C:\SIV")) {
        New-Item `
            -Path "C:\SIV" `
            -ItemType Directory `
            -Force |
            Out-Null
    }

    Set-Content `
        -Path $logFile `
        -Value "" `
        -Encoding UTF8

    Write-Log "SIV login automation started."

    # پیدا کردن Chrome
    $chrome = Find-ChromeExecutable

    if (-not $chrome) {
        throw "Google Chrome was not found."
    }

    Write-Log "Chrome executable found: $chrome"

    # بررسی فایل PIN
    if (-not (Test-Path $pinFile)) {
        throw "Encrypted PIN file was not found: $pinFile"
    }

    # خواندن و رمزگشایی PIN
    $encryptedPin = [System.IO.File]::ReadAllText($pinFile).Trim()

    if ([string]::IsNullOrWhiteSpace($encryptedPin)) {
        throw "The encrypted PIN file is empty."
    }

    $securePin = ConvertTo-SecureString -String $encryptedPin
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePin)
    $pin = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)

    if ([string]::IsNullOrWhiteSpace($pin)) {
        throw "The token PIN could not be decrypted."
    }

    Write-Log "Encrypted PIN loaded successfully."

    if (-not $UseExistingBrowserWindow) {
        # بازکردن سایت در پنجره جدید Chrome/Chromium
        $launchArguments = @()

        if ($UserDataDir) {
            $launchArguments += "--user-data-dir=$UserDataDir"
        }

        if ($DiskCacheDir) {
            $launchArguments += "--disk-cache-dir=$DiskCacheDir"
        }

        $launchArguments += "--new-window"
        $launchArguments += $url

        Start-Process `
            -FilePath $chrome `
            -ArgumentList $launchArguments

        Write-Log "Chrome started with the SIV URL."
    }
    else {
        Write-Log "Using existing Chrome/Chromium window. URL navigation is handled by the caller."
    }

    Write-Log "Target browser PID: $BrowserProcessId"

    $shell = New-Object -ComObject WScript.Shell

    # =========================
    # منتظر پنجره SafeNet
    # =========================

    $tokenWindowTitles = @(
        "Connexion au token",
        "Token Logon",
        "Logon to token",
        "Token Login"
    )

    $tokenWindowFound = $false
    $tokenDeadline = (Get-Date).AddSeconds(45)

    while ((Get-Date) -lt $tokenDeadline) {
        if (
            Activate-WindowByTitle `
                -Shell $shell `
                -Titles $tokenWindowTitles
        ) {
            $tokenWindowFound = $true
            break
        }

        Start-Sleep -Milliseconds 200
    }

    if (-not $tokenWindowFound) {
        throw "The SafeNet token PIN window was not found within 45 seconds."
    }

    Start-Sleep -Milliseconds 400

    # واردکردن PIN
    $pinForSendKeys = Convert-ToSendKeysLiteral -Text $pin

    $shell.SendKeys($pinForSendKeys)

    Start-Sleep -Milliseconds 200

    # تأیید PIN
    $shell.SendKeys("{ENTER}")

    Write-Log "Token PIN submitted."

    $pinForSendKeys = $null

    # =========================
    # تأیید سریع Certificate
    # =========================

    Write-Log "Waiting $certificateDelayMs milliseconds for the certificate dialog."

    Start-Sleep -Milliseconds $certificateDelayMs

    $certificateConfirmed = $false
    $chromeDeadline = (Get-Date).AddSeconds(5)

    while ((Get-Date) -lt $chromeDeadline) {
        $chromeWindow = Get-ChromeWindow -PreferredProcessId $BrowserProcessId

        if ($chromeWindow) {
            try {
                if ($shell.AppActivate($chromeWindow.Id)) {
                    Write-Log "Chrome window activated. PID: $($chromeWindow.Id)"

                    Start-Sleep -Milliseconds 150

                    # Certificate از قبل انتخاب شده است.
                    # Enter معادل کلیک روی دکمه OK است.
                    $shell.SendKeys("{ENTER}")

                    $certificateConfirmed = $true
                    Write-Log "Certificate confirmed immediately through Chrome."
                    break
                }
            }
            catch {
                Write-Log "Could not activate Chrome yet."
            }
        }

        Start-Sleep -Milliseconds 100
    }

    if (-not $certificateConfirmed) {
        throw "The Chrome certificate dialog could not be confirmed."
    }

    Write-Log "SIV login automation completed successfully."
}
catch {
    $errorMessage = $_.Exception.Message

    try {
        Write-Log "ERROR: $errorMessage"
    }
    catch {
        # درصورتی‌که نوشتن Log هم ممکن نباشد.
    }

    try {
        Add-Type -AssemblyName System.Windows.Forms

        [System.Windows.Forms.MessageBox]::Show(
            "$errorMessage`r`n`r`nLog file:`r`n$logFile",
            "SIV Login Error",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Error
        ) | Out-Null
    }
    catch {
        Write-Host ""
        Write-Host "ERROR: $errorMessage" -ForegroundColor Red
        Write-Host ""
        Write-Host "Log file: $logFile"

        Read-Host "Press Enter to close"
    }

    exit 1
}
finally {
    if ($bstr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }

    $pin = $null
    $securePin = $null
    $encryptedPin = $null
}
