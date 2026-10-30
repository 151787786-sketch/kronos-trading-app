# 一键复原到「同花顺风格改造前」的版本
#
# 用法（在 PowerShell 中执行）：
#   cd D:\a\kronos\trading-app
#   .\RESTORE.ps1
#
# 它会从最新的 _backup_* 备份目录恢复 index.html / app.py / README.md。
# 恢复后需要重启交易 APP（关闭窗口后重新双击 启动交易APP.bat）。

param(
    [string]$BackupDir = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not $BackupDir) {
    $cands = Get-ChildItem -Path $Root -Directory -Filter "_backup_*" |
             Sort-Object Name -Descending
    if (-not $cands) {
        Write-Host "[RESTORE] 没有找到任何备份目录 (_backup_*)" -ForegroundColor Red
        exit 1
    }
    $BackupDir = $cands[0].FullName
}

if (-not (Test-Path $BackupDir)) {
    Write-Host "[RESTORE] 备份目录不存在: $BackupDir" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "备份目录: $BackupDir" -ForegroundColor Cyan
if (Test-Path (Join-Path $BackupDir "VERSION.txt")) {
    Get-Content (Join-Path $BackupDir "VERSION.txt") | ForEach-Object { Write-Host "  $_" }
}
Write-Host ""

if (-not $Force) {
    $ans = Read-Host "确认用该备份覆盖当前界面? 输入 yes 继续"
    if ($ans -ne "yes") {
        Write-Host "已取消，未做任何修改。" -ForegroundColor Yellow
        exit 0
    }
}

# 先备份当前状态，防止恢复后又一版丢失
$ts = Get-Date -Format "yyyyMMdd_HHmmss"
$preRestore = Join-Path $Root "_prerestore_$ts"
New-Item -ItemType Directory -Path $preRestore -Force | Out-Null
foreach ($f in @("templates\index.html", "app.py", "README.md")) {
    if (Test-Path (Join-Path $Root $f)) { Copy-Item (Join-Path $Root $f) $preRestore -Force }
}
Write-Host "已把当前状态另存到: $preRestore" -ForegroundColor DarkGray

# 恢复
$map = @{
    "index.html" = "templates\index.html"
    "app.py"     = "app.py"
    "README.md"  = "README.md"
}
foreach ($src in $map.Keys) {
    $s = Join-Path $BackupDir $src
    $d = Join-Path $Root $map[$src]
    if (Test-Path $s) {
        Copy-Item $s $d -Force
        Write-Host ("  恢复 " + $map[$src]) -ForegroundColor Green
    }
}

Write-Host ""
Write-Host "复原完成。请重启交易 APP：" -ForegroundColor Cyan
Write-Host "  1. 关闭当前 APP 窗口"
Write-Host "  2. 双击 D:\a\kronos\启动交易APP.bat"
Write-Host "  3. 浏览器按 Ctrl+F5 强制刷新"
Write-Host ""
