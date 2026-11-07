<#
.SYNOPSIS
  Fire Loop（火环）卸载脚本：删除安装脚本写入的 HKCU 协议、右键菜单、登录自启。

.DESCRIPTION
  默认是 dry-run：只列出将删除的注册表键和值，不删任何东西。
  加 -Apply 才删；只删本脚本约定命名的键，且会先检查内容看起来是不是火环装的
  （不是的话跳过并警告，-Force 可强制）。删完逐项验证不存在。

  只动 HKCU，不碰 HKLM、系统服务、explorer、其他用户。
  安装前的现状备份在 %LOCALAPPDATA%\FireLoop\backup\reg-<时间戳>\，卸载不会自动恢复它。

.PARAMETER Apply
  真正删除注册表项。不加就是 dry-run。

.PARAMETER KeyPrefix
  安装时用的同一个 KeyPrefix，默认 fireloop。测试前缀要用同一个值来卸载。

.PARAMETER Force
  即使键看起来不是火环创建的也强制删除。正常情况不需要。

.EXAMPLE
  .\uninstall.ps1
  生产前缀 dry-run：只列出将删除的内容。

.EXAMPLE
  .\uninstall.ps1 -Apply
  真卸载生产前缀。

.EXAMPLE
  .\uninstall.ps1 -KeyPrefix fireloop-test-a1b2 -Apply
  卸载测试前缀。

.NOTES
  兼容 Windows PowerShell 5.1 和 PowerShell 7。零第三方依赖。
  退出码：0 成功（含原本就不存在）；1 删除或验证失败；2 参数错误。
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$Apply,
    [string]$KeyPrefix = 'fireloop',
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_common.ps1')

# ---------------------------------------------------------------------------
# 参数校验（失败退出码 2）
# ---------------------------------------------------------------------------
try {
    Test-FireLoopKeyPrefix $KeyPrefix
}
catch {
    Write-Output ''
    Write-Output ('[错误] ' + $_.Exception.Message)
    Write-Output '没有删除任何注册表项。'
    exit 2
}

$handlerName = Get-FireLoopHandlerName $KeyPrefix
$schemeKey = "HKCU\Software\Classes\$KeyPrefix"
$fileVerbKey = "HKCU\Software\Classes\*\shell\$handlerName"
$dirVerbKey = "HKCU\Software\Classes\Directory\shell\$handlerName"
$runKey = 'HKCU\Software\Microsoft\Windows\CurrentVersion\Run'
$knownAncestors = @(
    'HKCU\Software\Classes',
    'HKCU\Software\Classes\*',
    'HKCU\Software\Classes\*\shell',
    'HKCU\Software\Classes\Directory',
    'HKCU\Software\Classes\Directory\shell',
    'HKCU\Software\Microsoft\Windows\CurrentVersion\Run'
)

if ($Apply) {
    $modeText = 'APPLY（真删注册表）'
}
else {
    $modeText = 'DRY-RUN（只预览，不删除）'
}

Write-Output 'Fire Loop（火环）卸载脚本'
Write-Output ('模式：' + $modeText)
Write-Output ('KeyPrefix：' + $KeyPrefix + '    右键/自启标识：' + $handlerName)
Write-Output '安全范围：只删本脚本安装的 HKCU 内容；不碰 HKLM、系统服务、explorer、其他用户，不删备份。'

# ---------------------------------------------------------------------------
# 判断各项现状与归属
# ---------------------------------------------------------------------------
function Get-FireLoopOwnership {
    param(
        [Parameter(Mandatory = $true)][string]$Kind,
        [Parameter(Mandatory = $true)][string]$Path,
        [AllowEmptyString()][string]$ValueName = '',
        [string]$CommandPath = ''
    )

    if ($Kind -eq 'value') {
        $value = Get-FireLoopRegistryValue $Path $ValueName
        if (-not $value.Exists) { return 'Absent' }
        $data = [string]$value.Data
        # 新格式：启动器 launch.py agent；旧格式（-m fireloop.agent）兼容
        if (($data -like '*launch.py* agent*') -or ($data -like '*fireloop.agent*')) { return 'Owned' }
        return 'Foreign'
    }

    $info = Get-FireLoopRegistryKeyInfo $Path
    if (-not $info.Exists) { return 'Absent' }
    if (Test-FireLoopRegistryKeyEmpty $Path) {
        # 空键：大概率是上次安装中途中断留下的，删掉安全。
        return 'Owned'
    }
    if ($Kind -eq 'scheme') {
        foreach ($value in $info.Values) {
            if ($value.Name -eq 'URL Protocol') { return 'Owned' }
        }
    }
    else {
        foreach ($value in $info.Values) {
            if ($value.Name -eq '' -and [string]$value.Data -eq '交给火环') { return 'Owned' }
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($CommandPath)) {
        $command = Get-FireLoopRegistryValue $CommandPath ''
        if ($command.Exists) {
            $data = [string]$command.Data
            if ($Kind -eq 'scheme') {
                # 新格式：启动器 launch.py cli uri；旧格式兼容
                if (($data -like '*launch.py* cli uri*') -or ($data -like '*fireloop.cli uri*')) { return 'Owned' }
            }
            else {
                if (($data -like '*launch.py* cli file*') -or ($data -like '*fireloop.cli file*')) { return 'Owned' }
            }
        }
    }
    return 'Foreign'
}

$targets = @(
    [pscustomobject]@{
        Id = 'scheme'; Category = '1/4'; CategoryName = "协议 handler：$KeyPrefix`://"
        Kind = 'key'; Label = 'fireloop:// 协议'
        Path = $schemeKey; CommandPath = "$schemeKey\shell\open\command"
        OwnerState = (Get-FireLoopOwnership -Kind 'scheme' -Path $schemeKey -CommandPath "$schemeKey\shell\open\command")
    },
    [pscustomobject]@{
        Id = 'file-verb'; Category = '2/4'; CategoryName = '文件右键菜单（显示名：交给火环）'
        Kind = 'key'; Label = '文件右键菜单'
        Path = $fileVerbKey; CommandPath = "$fileVerbKey\command"
        OwnerState = (Get-FireLoopOwnership -Kind 'verb' -Path $fileVerbKey -CommandPath "$fileVerbKey\command")
    },
    [pscustomobject]@{
        Id = 'dir-verb'; Category = '3/4'; CategoryName = '文件夹右键菜单（显示名：交给火环）'
        Kind = 'key'; Label = '文件夹右键菜单'
        Path = $dirVerbKey; CommandPath = "$dirVerbKey\command"
        OwnerState = (Get-FireLoopOwnership -Kind 'verb' -Path $dirVerbKey -CommandPath "$dirVerbKey\command")
    },
    [pscustomobject]@{
        Id = 'run'; Category = '4/4'; CategoryName = '登录自启'
        Kind = 'value'; Label = '登录自启'
        Path = $runKey; CommandPath = ''
        OwnerState = (Get-FireLoopOwnership -Kind 'value' -Path $runKey -ValueName $handlerName)
    }
)

function Write-FireLoopKeyContent {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$CommandPath = '',
        [string]$Indent = '        '
    )

    $info = Get-FireLoopRegistryKeyInfo $Path
    if (-not $info.Exists) { Write-Output ($Indent + '（键不存在）'); return }
    if ($info.Values.Count -eq 0) {
        Write-Output ($Indent + '（键存在但没有任何值）')
    }
    foreach ($value in $info.Values) {
        Write-Output ($Indent + (Format-FireLoopValueName $value.Name) + ' = ' + (Format-FireLoopData $value.Data))
    }
    if (-not [string]::IsNullOrWhiteSpace($CommandPath)) {
        $command = Get-FireLoopRegistryValue $CommandPath ''
        if ($command.Exists) {
            Write-Output ($Indent + 'command 子键默认值 = ' + (Format-FireLoopData $command.Data))
        }
    }
}

Write-Output ''
Write-Output '[计划] 将删除以下 4 类注册项'
$currentCategory = ''
foreach ($target in $targets) {
    if ($target.Category -ne $currentCategory) {
        $currentCategory = $target.Category
        Write-Output ''
        Write-Output ('[' + $target.Category + '] ' + $target.CategoryName)
    }

    if ($target.Kind -eq 'key') {
        if ($target.OwnerState -eq 'Absent') {
            Write-Output ('  键   ' + $target.Path + '    [跳过] 不存在')
            continue
        }
        if ($target.OwnerState -eq 'Foreign' -and -not $Force) {
            Write-Output ('  键   ' + $target.Path + '    [警告] 看起来不是火环创建的，跳过（要强制删除加 -Force）')
            Write-FireLoopKeyContent -Path $target.Path -CommandPath $target.CommandPath
            continue
        }
        Write-Output ('  键   ' + $target.Path + '    [将删除整个键及子键]')
        Write-FireLoopKeyContent -Path $target.Path -CommandPath $target.CommandPath
    }
    else {
        if ($target.OwnerState -eq 'Absent') {
            Write-Output ('  值   ' + $target.Path + '    [跳过] 值 ' + $handlerName + ' 不存在')
            continue
        }
        $value = Get-FireLoopRegistryValue $target.Path $handlerName
        if ($target.OwnerState -eq 'Foreign' -and -not $Force) {
            Write-Output ('  值   ' + $target.Path + '    [警告] ' + $handlerName + ' 看起来不是火环创建的，跳过（要强制删除加 -Force）')
            Write-Output ('        ' + $handlerName + ' = ' + (Format-FireLoopData $value.Data))
            continue
        }
        Write-Output ('  值   ' + $target.Path + '    [将删除]')
        Write-Output ('        ' + $handlerName + ' = ' + (Format-FireLoopData $value.Data))
    }
}

# ---------------------------------------------------------------------------
# DRY-RUN：到此为止
# ---------------------------------------------------------------------------
if (-not $Apply) {
    Write-Output ''
    Write-Output '以上为 DRY-RUN，未删除任何注册表项。'
    Write-Output ('确认无误后真删：' + $MyInvocation.MyCommand.Name + ' -Apply' + $(if ($KeyPrefix -ne 'fireloop') { ' -KeyPrefix ' + $KeyPrefix } else { '' }))
    exit 0
}

# ---------------------------------------------------------------------------
# APPLY：执行删除并验证
# ---------------------------------------------------------------------------
try {
    Write-Output ''
    Write-Output '[删除] 开始删除 HKCU 注册表项'
    $failed = 0
    $skipped = 0
    $deleted = 0

    foreach ($target in $targets) {
        if ($target.OwnerState -eq 'Absent') {
            Write-Output ('  [跳过] 不存在：' + $target.Path + $(if ($target.Kind -eq 'value') { ' :: ' + $handlerName } else { '' }))
            continue
        }
        if ($target.OwnerState -eq 'Foreign' -and -not $Force) {
            Write-Output ('  [跳过] 不是火环创建的，保留：' + $target.Path)
            $skipped++
            continue
        }

        if ($target.Kind -eq 'key') {
            $ok = Remove-FireLoopRegistryKeyTree $target.Path
            if ($ok -and -not (Test-FireLoopRegistryKey $target.Path)) {
                Write-Output ('  [已删除] ' + $target.Path)
                $deleted++
            }
            else {
                Write-Output ('  [失败] 删除后仍存在：' + $target.Path)
                $failed++
            }
        }
        else {
            $ok = Remove-FireLoopRegistryValue $target.Path $handlerName
            if ($ok -and -not (Get-FireLoopRegistryValue $target.Path $handlerName).Exists) {
                Write-Output ('  [已删除] ' + $target.Path + ' :: ' + $handlerName)
                $deleted++
            }
            else {
                Write-Output ('  [失败] 删除后仍存在：' + $target.Path + ' :: ' + $handlerName)
                $failed++
            }
        }
    }

    # 清理安装时由本脚本创建、现在已经空掉的父键（例如 HKCU\Software\Classes\Directory\shell）。
    $installResult = Find-FireLoopInstallResult -KeyPrefix $KeyPrefix -HandlerName $handlerName
    if ($null -ne $installResult) {
        $ancestorCandidates = @()
        foreach ($ancestorPath in @($installResult.ancestors_absent_before)) {
            if ([string]::IsNullOrWhiteSpace($ancestorPath)) { continue }
            if ($knownAncestors -contains $ancestorPath) { $ancestorCandidates += $ancestorPath }
        }
        $ancestorCandidates = @($ancestorCandidates | Sort-Object -Property @{ Expression = { $_.Length } } -Descending)
        foreach ($ancestorPath in $ancestorCandidates) {
            if ((Test-FireLoopRegistryKey $ancestorPath) -and (Test-FireLoopRegistryKeyEmpty $ancestorPath)) {
                $null = Remove-FireLoopRegistryKeyTree $ancestorPath
                if (-not (Test-FireLoopRegistryKey $ancestorPath)) {
                    Write-Output ('  [清理] 删除安装时创建的空父键：' + $ancestorPath)
                }
            }
        }
    }

    # 最终验证
    Write-Output ''
    Write-Output '[验证] 检查 4 类注册项是否都不存在'
    $remaining = @()
    foreach ($target in $targets) {
        if ($target.Kind -eq 'key') {
            if (Test-FireLoopRegistryKey $target.Path) { $remaining += $target.Path }
        }
        else {
            if ((Get-FireLoopRegistryValue $target.Path $handlerName).Exists) {
                $remaining += ($target.Path + ' :: ' + $handlerName)
            }
        }
    }

    if ($remaining.Count -gt 0) {
        Write-Output '[未完成] 以下项仍然存在（按保护规则跳过的非火环内容会保留；要强制删除加 -Force）：'
        foreach ($line in $remaining) { Write-Output ('  仍存在：' + $line) }
        exit 1
    }

    Write-Output ('  1. 协议：' + $schemeKey + '  -> 不存在')
    Write-Output ('  2. 文件右键：' + $fileVerbKey + '  -> 不存在')
    Write-Output ('  3. 文件夹右键：' + $dirVerbKey + '  -> 不存在')
    Write-Output ('  4. 登录自启：' + $runKey + '\' + $handlerName + '  -> 不存在')

    Write-Output ''
    if ($skipped -gt 0) {
        Write-Output ('[完成] 已删除 ' + $deleted + ' 项；有 ' + $skipped + ' 项看起来不是火环创建的，已保留（需要强制删除加 -Force）。')
    }
    elseif ($deleted -eq 0) {
        Write-Output '[完成] 没有需要删除的内容（脚本是幂等的）。'
    }
    else {
        Write-Output ('[完成] 已删除 ' + $deleted + ' 项，并验证全部不存在。')
    }
    Write-Output '  没有碰 HKLM、系统服务、explorer、其他用户；安装备份仍保留在 %LOCALAPPDATA%\FireLoop\backup\。'
    exit 0
}
catch {
    Write-Output ''
    Write-Output ('[错误] 卸载过程中失败：' + $_.Exception.Message)
    exit 1
}
