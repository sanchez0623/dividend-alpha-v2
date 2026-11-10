# 最小部署包：服务器运行所需的文件（排除 handoff 文档/UI截图/node_modules/本地数据）。
# 用法: powershell -File scripts\pack_deploy.ps1   → 产出 da-deploy.zip
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$out = Join-Path $root "da-deploy.zip"
if (Test-Path $out) { Remove-Item $out -Force }

$excludeDirs = @(".git", ".venv", "venv", "node_modules", "__pycache__",
                 ".pytest_cache", ".mypy_cache", ".ruff_cache", "data", "research",
                 ".pnpm-store", ".dsh", "screenshots", "ui_screens", "docs")
$excludeFiles = @(".env", ".env.local")

# 只保留部署必需的顶层路径 + handoff/web/data.json
$keepTop = @("src", "config", "infra", "scripts", "web", "tests",
             "requirements.txt", ".env.example", ".dockerignore", "Makefile",
             "README.md", "start_local.bat")

$files = Get-ChildItem -Path $root -Recurse -File |
  Where-Object {
    $rel = $_.FullName.Substring($root.Length + 1)
    $parts = $rel -split '\\'
    if ($excludeFiles -contains $_.Name) { return $false }
    foreach ($p in $parts) { if ($excludeDirs -contains $p) { return $false } }
    # dividend-alpha-v3-handoff: 只要 web/data.json
    if ($parts[0] -eq "dividend-alpha-v3-handoff") {
      return ($rel -eq "dividend-alpha-v3-handoff\web\data.json")
    }
    return ($keepTop -contains $parts[0])
  }

# ★ 用 .NET ZipFile 以【相对路径】为 entryName，保留目录结构
# （Compress-Archive 传文件列表会把结构打平，导致服务器上 infra/xxx.sh 丢失）
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::Open($out, [System.IO.Compression.ZipArchiveMode]::Create)
try {
  foreach ($f in $files) {
    $rel = $f.FullName.Substring($root.Length + 1).Replace('\', '/')
    [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $f.FullName, $rel) | Out-Null
  }
} finally { $zip.Dispose() }
$sizeKB = [math]::Round((Get-Item $out).Length / 1KB, 0)
Write-Host "已打包: $out  ($sizeKB KB, $($files.Count) 文件)"
