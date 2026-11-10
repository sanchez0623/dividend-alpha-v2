# 打包上传档案（排除 venv/node_modules/本地库/.env），供 Workbench 上传到 ECS。
# 用法（本机 PowerShell）: powershell -File scripts\pack_for_upload.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$stamp = Get-Date -Format "yyyyMMdd-HHmm"
$out = Join-Path $root "dividend-alpha-$stamp.zip"
if (Test-Path $out) { Remove-Item $out -Force }

$excludeDirs = @(".git", ".venv", "venv", "node_modules", "__pycache__",
                 ".pytest_cache", ".mypy_cache", ".ruff_cache", "data", "research",
                 ".pnpm-store", ".dsh", "screenshots")
$excludeFiles = @(".env", ".env.local")

$files = Get-ChildItem -Path $root -Recurse -File |
  Where-Object {
    $rel = $_.FullName.Substring($root.Length + 1)
    $parts = $rel -split '\\'
    $skip = $false
    foreach ($p in $parts) {
      if ($excludeDirs -contains $p) { $skip = $true; break }
    }
    if ($excludeFiles -contains $_.Name) { $skip = $true }
    -not $skip
  }

Compress-Archive -Path $files.FullName -DestinationPath $out -CompressionLevel Optimal
$sizeMB = [math]::Round((Get-Item $out).Length / 1MB, 1)
Write-Host "已打包: $out  ($sizeMB MB, $($files.Count) 文件)"
Write-Host "用 Workbench 上传此 zip 到 ECS，然后在服务器上: unzip dividend-alpha-*.zip"
