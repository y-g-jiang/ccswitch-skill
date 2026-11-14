<#
restart-codex.ps1 — Windows：Codex 桌面版假死急救

现象：进程列表里有多个 ChatGPT.exe，但窗口标题为空，点图标不出新窗口。
原因：Electron 单实例 + 残留锁 + 起来的 codex.exe app-server 立刻退出。

用法（普通 PowerShell 即可，不需要管理员）：
    powershell -NoProfile -ExecutionPolicy Bypass -File restart-codex.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File restart-codex.ps1 -Wait 45
#>
param(
    [int]$Wait = 35,
    [switch]$KeepLogs
)

$ErrorActionPreference = 'Continue'
$codexHome = Join-Path $env:USERPROFILE '.codex'
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$logPath = Join-Path $env:TEMP ("restart-codex-" + $stamp + ".log")

function W($m) {
    $line = "$(Get-Date -Format 'HH:mm:ss') $m"
    $line | Out-File -FilePath $logPath -Append -Encoding utf8
    Write-Output $line
}

W "=== 1) 当前进程 ==="
Get-Process ChatGPT -ErrorAction SilentlyContinue | ForEach-Object {
    W ("ChatGPT pid=$($_.Id) cpu=$($_.CPU) win='$($_.MainWindowTitle)'")
}
Get-Process codex -ErrorAction SilentlyContinue | ForEach-Object { W ("codex pid=$($_.Id)") }

W "=== 2) 关闭（优雅 → 强制）==="
Get-Process ChatGPT -ErrorAction SilentlyContinue | ForEach-Object { [void]$_.CloseMainWindow() }
Start-Sleep -Seconds 4
if (Get-Process ChatGPT -ErrorAction SilentlyContinue) {
    W "仍存活，强制结束进程树"
    & taskkill /F /T /IM ChatGPT.exe 2>&1 | ForEach-Object { W "  $_" }
} else {
    W "已优雅关闭"
}
Start-Sleep -Seconds 2

W "=== 3) 清理残留 helper ==="
foreach ($n in @('codex.exe', 'codex-code-mode-host.exe', 'codex-computer-use-win.exe',
                 'node_repl.exe', 'extension-host.exe', 'codex-computer-use.exe')) {
    $name = [IO.Path]::GetFileNameWithoutExtension($n)
    $procs = Get-Process -Name $name -ErrorAction SilentlyContinue
    if ($procs) {
        W "kill $n -> $($procs.Id -join ',')"
        & taskkill /F /T /IM $n 2>&1 | ForEach-Object { W "  $_" }
    }
}
Start-Sleep -Seconds 2

W "=== 4) 清理僵尸锁 ==="
foreach ($sub in @('thread-writer-locks', 'project-metadata-locks')) {
    $dir = Join-Path $codexHome $sub
    if (Test-Path $dir) {
        Get-ChildItem $dir -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object {
            W "  rm $sub/$($_.Name) (mtime $($_.LastWriteTime.ToString('MM-dd HH:mm')))"
            Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue
        }
    }
}
Get-ChildItem $codexHome -File -Include '*.lock', '*.guard' -ErrorAction SilentlyContinue | ForEach-Object {
    W "  rm $($_.Name)"
    Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue
}

W "=== 5) 重新拉起 ==="
$pkg = Get-AppxPackage OpenAI.Codex -ErrorAction SilentlyContinue | Select-Object -First 1
if ($pkg) {
    $manifest = Join-Path $pkg.InstallLocation 'AppxManifest.xml'
    $appId = 'App'
    if (Test-Path $manifest) {
        try {
            [xml]$xml = Get-Content $manifest -Raw
            $node = $xml.Package.Applications.Application | Select-Object -First 1
            if ($node -and $node.Id) { $appId = $node.Id }
        } catch { }
    }
    $aumid = "$($pkg.PackageFamilyName)!$appId"
    W "AUMID = $aumid"
    Start-Process explorer.exe -ArgumentList ("shell:AppsFolder\" + $aumid)
} else {
    $exe = Join-Path $env:LOCALAPPDATA 'Programs\ChatGPT\ChatGPT.exe'
    if (Test-Path $exe) { W "未找到 MSIX 包，直接启动 $exe"; Start-Process $exe }
    else { W "找不到 Codex 安装，请手动启动一次"; }
}

W "=== 6) 等待并校验 ==="
Start-Sleep -Seconds $Wait
$win = Get-Process ChatGPT -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle }
if ($win) {
    W "窗口 OK: pid=$($win.Id) title='$($win.MainWindowTitle)'"
} else {
    W "窗口还没出现（正常时 30-60 秒白屏）；若超过 2 分钟仍无窗口，再跑一次本脚本"
}
$srv = Get-Process codex -ErrorAction SilentlyContinue
if ($srv) { W "app-server OK: pid=$($srv.Id -join ',')" } else { W "app-server 未运行" }

W "日志：$logPath"
if (-not $KeepLogs) { }
