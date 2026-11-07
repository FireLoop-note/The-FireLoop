<#
.SYNOPSIS
  Fire Loop（火环）安装脚本：注册 fireloop:// 协议、文件/文件夹右键菜单、登录自启。

.DESCRIPTION
  全部只写 HKCU（当前用户），不碰 HKLM、系统服务、explorer 和其他用户。
  默认是 dry-run：只列出将创建/修改的注册表键和值，不写注册表，也不创建备份目录。
  加 -Apply 才会先备份现状、再真写注册表，写完后逐项读回验证。

  注册内容（4 类，命令统一走 install\launch.py 启动器，不依赖 PYTHONPATH）：
    1. HKCU\Software\Classes\<KeyPrefix>                默认值 = ""、URL Protocol = ""、
       HKCU\Software\Classes\<KeyPrefix>\shell\open\command = "<PythonExe>" "<项目根>\install\launch.py" cli uri "%1"
    2. HKCU\Software\Classes\*\shell\<HandlerName>      默认值 = "交给火环"、
       ...\command = "<PythonExe>" "<项目根>\install\launch.py" cli file "%1"
    3. HKCU\Software\Classes\Directory\shell\<HandlerName>  同上，文件夹右键
    4. HKCU\Software\Microsoft\Windows\CurrentVersion\Run\<HandlerName> = "<PythonExe>" "<项目根>\install\launch.py" agent

  KeyPrefix 默认 fireloop（此时 HandlerName 按规范就是 FireLoop）。
  测试时传 -KeyPrefix fireloop-test-<随机>，HandlerName 会派生成 FireLoop-<随机>，
  保证测试只碰测试命名的键，不会创建任何生产键。

.PARAMETER Apply
  真正写入注册表。不加就是 dry-run。

.PARAMETER KeyPrefix
  协议 handler 的注册表键名，默认 fireloop。测试用 fireloop-test-<随机>。

.PARAMETER PythonExe
  Python 解释器路径。默认探测 D:\Python\pythonw.exe，不存在则报错退出。

.EXAMPLE
  .\install.ps1
  生产前缀 dry-run：只预览，不写注册表。

.EXAMPLE
  .\install.ps1 -Apply
  生产前缀真装（需要用户批准后再跑）。

.EXAMPLE
  .\install.ps1 -KeyPrefix fireloop-test-a1b2 -Apply
  测试前缀真写，只用于验证脚本，验证完用 uninstall.ps1 删掉。

.NOTES
  兼容 Windows PowerShell 5.1 和 PowerShell 7。零第三方依赖。
  退出码：0 成功；1 写入/验证失败；2 参数或环境错误（如 Python 解释器不存在）。
#>
#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$Apply,
    [string]$KeyPrefix = 'fireloop',
    [string]$PythonExe = ''
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_common.ps1')

# ---------------------------------------------------------------------------
# 参数与环境校验（失败退出码 2）
# ---------------------------------------------------------------------------
$projectRoot = Split-Path -Parent $PSScriptRoot
$launchScript = Join-Path $projectRoot 'install\launch.py'

try {
    Test-FireLoopKeyPrefix $KeyPrefix
    $resolvedPython = Resolve-FireLoopPythonExe $PythonExe
    if (-not (Test-Path -LiteralPath $launchScript -PathType Leaf)) {
        throw "找不到启动器：$launchScript（install/launch.py 必须和安装脚本放在同一个 install 目录）"
    }
}
catch {
    Write-Output ''
    Write-Output ('[错误] ' + $_.Exception.Message)
    Write-Output '没有写入任何注册表项。'
    exit 2
}

$context = New-FireLoopContext -KeyPrefix $KeyPrefix -PythonExe $resolvedPython -ProjectRoot $projectRoot
if ($Apply) {
    $modeText = 'APPLY（真写注册表）'
}
else {
    $modeText = 'DRY-RUN（只预览，不写注册表）'
}

Write-Output 'Fire Loop（火环）安装脚本'
Write-Output ('模式：' + $modeText)
Write-Output ('KeyPrefix：' + $context.KeyPrefix + '    右键/自启标识：' + $context.HandlerName)
Write-Output ('Python 解释器：' + $context.PythonExe)
Write-Output ('启动器：' + $context.LaunchScript + '（注册表命令的唯一入口，自带 src 路径，不依赖 PYTHONPATH）')
Write-Output ('备份根目录：' + (Get-FireLoopBackupRoot))
Write-Output '安全范围：只写 HKCU（当前用户）；不碰 HKLM、系统服务、explorer、其他用户，不装软件。'

# ---------------------------------------------------------------------------
# 预检：注册表命令走 install\launch.py，确认启动器从任意目录能跑起来
# ---------------------------------------------------------------------------
Write-Output ''
Write-Output '[预检] 启动器自检（注册表命令不依赖 PYTHONPATH）'
$launcherCheck = Test-FireLoopLauncher -PythonExe $context.PythonExe -ProjectRoot $projectRoot
if (-not $launcherCheck.Ok) {
    if ($launcherCheck.Attempted) {
        Write-Output ('  [错误] 用 "' + $launcherCheck.ConsoleExe + '" 从 ' + $launcherCheck.WorkingDirectory + ' 跑启动器失败：')
        Write-Output ('         ' + $launcherCheck.Detail)
    }
    else {
        Write-Output '  [错误] 没能启动 Python 做启动器自检。'
        Write-Output ('         ' + $launcherCheck.Detail)
    }
    Write-Output ('         启动器：' + $launcherCheck.LauncherPath)
    Write-Output '         注册表命令就是走这个启动器，启动器跑不起来装完也一定是坏的。'
    Write-Output '没有写入任何注册表项。先修好启动器/源码再装。'
    exit 2
}
Write-Output ('  [通过] 从 ' + $launcherCheck.WorkingDirectory + ' 运行 "' + $launcherCheck.LauncherPath + '" cli --help 成功；')
Write-Output '         说明注册表命令不依赖 PYTHONPATH、也不挑工作目录。'

# ---------------------------------------------------------------------------
# 生成计划并展示
# ---------------------------------------------------------------------------
$plan = Get-FireLoopPlan $context
$changeCount = (Get-FireLoopPlanActionCount $plan 'Create') + (Get-FireLoopPlanActionCount $plan 'Update')

Write-Output ''
Write-Output '[计划] 将创建/修改以下 4 类注册项'
Write-FireLoopInstallPlan $plan

# ---------------------------------------------------------------------------
# 现状快照（dry-run 只读）
# ---------------------------------------------------------------------------
$snapshot = Get-FireLoopBackupSnapshot $context
$absentKeys = @()
foreach ($keySnapshot in $snapshot.KeySnapshots) {
    if (-not $keySnapshot.Exists) { $absentKeys += $keySnapshot.Path }
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$plannedBackupDir = Join-Path (Get-FireLoopBackupRoot) ('reg-' + $stamp)

Write-Output ''
Write-Output '[现状] 涉及注册表键/值'
Write-Output ('  键快照：' + $snapshot.KeySnapshots.Count + ' 项，其中原本不存在 ' + $absentKeys.Count + ' 项')
if ($absentKeys.Count -gt 0) {
    foreach ($absentPath in $absentKeys) {
        Write-Output ('    原本不存在：' + $absentPath)
    }
}
if ($snapshot.RunValue.Exists) {
    Write-Output ('  Run 值 ' + $snapshot.RunValue.Name + ' 原本已存在，当前 = ' + (Format-FireLoopData $snapshot.RunValue.Data))
}
else {
    Write-Output ('  Run 值 ' + $snapshot.RunValue.Name + ' 原本不存在')
}

# ---------------------------------------------------------------------------
# DRY-RUN：到此为止，什么都不写
# ---------------------------------------------------------------------------
if (-not $Apply) {
    Write-Output ''
    Write-Output ('[备份] dry-run 不创建备份目录。加 -Apply 时会先导出到：' + $plannedBackupDir + '\')
    Write-Output ''
    Write-Output '以上为 DRY-RUN，未写入任何注册表项。'
    Write-Output ('确认无误后真装：' + $MyInvocation.MyCommand.Name + ' -Apply')
    exit 0
}

# ---------------------------------------------------------------------------
# APPLY：先备份，再写，最后读回验证
# ---------------------------------------------------------------------------
try {
    if ($changeCount -eq 0) {
        Write-Output ''
        Write-Output '[结果] 注册表已是目标状态，无需写入；本次没有新建备份目录。'
        exit 0
    }

    $backup = Write-FireLoopInstallBackup -Context $context -Snapshot $snapshot -Stamp $stamp
    Write-Output ''
    Write-Output ('[备份] ' + $backup.Directory)
    Write-Output ('  键快照：' + $snapshot.KeySnapshots.Count + ' 项（原本不存在 ' + $absentKeys.Count + ' 项）')
    $exportOk = 0
    foreach ($export in $backup.RegExports) {
        if ($export.Success) { $exportOk++ }
    }
    Write-Output ('  reg 导出：' + $backup.RegExports.Count + ' 项（成功 ' + $exportOk + ' 项；原本不存在的键没有可导出的内容）')
    if ($backup.RegExports.Count -gt 0 -and $exportOk -lt $backup.RegExports.Count) {
        foreach ($export in $backup.RegExports) {
            if (-not $export.Success) {
                Write-Output ('  [警告] reg 导出失败：' + $export.Path + '（JSON 快照仍已记录）')
            }
        }
    }
    Write-Output '  清单：before.json；安装结果：install-result.json'

    Write-Output ''
    Write-Output '[写入] 开始写 HKCU 注册表'
    foreach ($planItem in $plan) {
        foreach ($entry in $planItem.Entries) {
            if ($entry.Action -eq 'None') { continue }
            Set-FireLoopRegistryValue -Path $planItem.Item.Path -Name $entry.Target.Name -Data $entry.Target.Data
        }
    }
    Write-Output ('  已写入 ' + $changeCount + ' 个值。')

    $verifyPlan = Get-FireLoopPlan $context
    $remaining = @()
    foreach ($planItem in $verifyPlan) {
        foreach ($entry in $planItem.Entries) {
            if ($entry.Action -ne 'None') {
                $remaining += ($planItem.Item.Path + ' :: ' + (Format-FireLoopValueName $entry.Target.Name))
            }
        }
    }
    if ($remaining.Count -gt 0) {
        Write-Output ''
        Write-Output '[验证失败] 以下项写入后读回不一致：'
        foreach ($line in $remaining) { Write-Output ('  不一致：' + $line) }
        Write-Output '请先排查，必要时运行 uninstall.ps1 -Apply 回退。'
        exit 1
    }

    $installResult = Write-FireLoopInstallResult -Context $context -Backup $backup -Snapshot $snapshot

    Write-Output ''
    Write-Output '[验证通过] 4 类注册项均已写入且读回一致：'
    Write-Output ('  1. 协议：' + $context.SchemeKey + '  ->  ' + $context.UriCommand)
    Write-Output ('  2. 文件右键：' + $context.FileVerbKey + '  ->  ' + $context.FileCommand)
    Write-Output ('  3. 文件夹右键：' + $context.DirVerbKey + '  ->  ' + $context.FileCommand)
    Write-Output ('  4. 登录自启：' + $context.RunKey + '\' + $context.HandlerName + '  ->  ' + $context.AgentCommand)

    Write-Output ''
    Write-Output '[完成] 火环系统身份已安装（只写 HKCU）。'
    Write-Output ('  备份目录：' + $backup.Directory)
    Write-Output ('  回退命令：' + $MyInvocation.MyCommand.Name.Replace('install.ps1', 'uninstall.ps1') + ' -Apply' + $(if ($context.KeyPrefix -ne 'fireloop') { ' -KeyPrefix ' + $context.KeyPrefix } else { '' }))
    Write-Output '  说明：没有重启 explorer、没写 HKLM、没装服务。若右键菜单或协议没立刻生效，注销重登一次即可。'
    Write-Output ('  启动器自检通过：「' + $context.LaunchScript + '」不依赖 PYTHONPATH，注册命令可正常运行。')
    exit 0
}
catch {
    Write-Output ''
    Write-Output ('[错误] 安装过程中失败：' + $_.Exception.Message)
    Write-Output '已写入的部分可能不完整，请检查上面的输出；必要时运行 uninstall.ps1 -Apply 回退。'
    exit 1
}
