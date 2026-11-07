# Fire Loop（火环）安装/卸载公共函数
# 由 install.ps1 / uninstall.ps1 点源引用。兼容 Windows PowerShell 5.1 与 PowerShell 7。
#
# 安全红线（写死在代码里）：
#   - 所有注册表操作只允许 HKCU，任何非 HKCU 路径直接抛错；
#   - 不碰 HKLM、系统服务、explorer、其他用户；
#   - 不装任何软件、不联网、不调用模型。

$script:FireLoopRegistryBase = $null

# ---------------------------------------------------------------------------
# 注册表基础操作（只走 HKCU）
# ---------------------------------------------------------------------------

function Get-FireLoopRegistryBase {
    if ($null -eq $script:FireLoopRegistryBase) {
        $script:FireLoopRegistryBase = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
            [Microsoft.Win32.RegistryHive]::CurrentUser,
            [Microsoft.Win32.RegistryView]::Default)
    }
    return $script:FireLoopRegistryBase
}

function ConvertTo-FireLoopRegRelativePath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $trimmed = $Path.Trim()
    if ($trimmed -like 'HKCU\*') {
        $relative = $trimmed.Substring(5)
        if ([string]::IsNullOrWhiteSpace($relative)) {
            throw "注册表路径为空：$Path"
        }
        return $relative
    }
    throw "安全红线：本脚本只允许操作 HKCU，拒绝注册表路径：$Path"
}

function Test-FireLoopRegistryKey {
    param([Parameter(Mandatory = $true)][string]$Path)

    $relative = ConvertTo-FireLoopRegRelativePath $Path
    $key = $null
    try {
        $key = (Get-FireLoopRegistryBase).OpenSubKey($relative, $false)
        return ($null -ne $key)
    }
    finally {
        if ($null -ne $key) { $key.Dispose() }
    }
}

function Get-FireLoopRegistryValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Name
    )

    $relative = ConvertTo-FireLoopRegRelativePath $Path
    $key = $null
    try {
        $key = (Get-FireLoopRegistryBase).OpenSubKey($relative, $false)
        if ($null -eq $key) {
            return [pscustomobject]@{ Exists = $false; Name = $Name; Data = $null; Kind = $null }
        }
        foreach ($existingName in $key.GetValueNames()) {
            if ($existingName -eq $Name) {
                return [pscustomobject]@{
                    Exists = $true
                    Name   = $Name
                    Data   = $key.GetValue($Name)
                    Kind   = [string]$key.GetValueKind($Name)
                }
            }
        }
        return [pscustomobject]@{ Exists = $false; Name = $Name; Data = $null; Kind = $null }
    }
    finally {
        if ($null -ne $key) { $key.Dispose() }
    }
}

function Get-FireLoopRegistryKeyInfo {
    param([Parameter(Mandatory = $true)][string]$Path)

    $relative = ConvertTo-FireLoopRegRelativePath $Path
    $key = $null
    $values = @()
    try {
        $key = (Get-FireLoopRegistryBase).OpenSubKey($relative, $false)
        if ($null -ne $key) {
            foreach ($name in $key.GetValueNames()) {
                $values += [pscustomobject]@{
                    Name = $name
                    Kind = [string]$key.GetValueKind($name)
                    Data = $key.GetValue($name)
                }
            }
        }
        return [pscustomobject]@{
            Path   = $Path
            Exists = ($null -ne $key)
            Values = @($values)
        }
    }
    finally {
        if ($null -ne $key) { $key.Dispose() }
    }
}

function Set-FireLoopRegistryValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Name,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Data
    )

    $relative = ConvertTo-FireLoopRegRelativePath $Path
    $key = (Get-FireLoopRegistryBase).CreateSubKey($relative)
    try {
        $key.SetValue($Name, [string]$Data, [Microsoft.Win32.RegistryValueKind]::String)
    }
    finally {
        $key.Dispose()
    }
}

function Remove-FireLoopRegistryKeyTree {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-FireLoopRegistryKey $Path)) { return $false }
    $relative = ConvertTo-FireLoopRegRelativePath $Path
    $base = Get-FireLoopRegistryBase
    $base.DeleteSubKeyTree($relative, $false)
    return $true
}

function Remove-FireLoopRegistryValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Name
    )

    $relative = ConvertTo-FireLoopRegRelativePath $Path
    $key = $null
    try {
        $key = (Get-FireLoopRegistryBase).OpenSubKey($relative, $true)
        if ($null -eq $key) { return $false }
        foreach ($existingName in $key.GetValueNames()) {
            if ($existingName -eq $Name) {
                $key.DeleteValue($Name, $false)
                return $true
            }
        }
        return $false
    }
    finally {
        if ($null -ne $key) { $key.Dispose() }
    }
}

function Test-FireLoopRegistryKeyEmpty {
    param([Parameter(Mandatory = $true)][string]$Path)

    $relative = ConvertTo-FireLoopRegRelativePath $Path
    $key = $null
    try {
        $key = (Get-FireLoopRegistryBase).OpenSubKey($relative, $false)
        if ($null -eq $key) { return $false }
        return (($key.SubKeyCount -eq 0) -and ($key.ValueCount -eq 0))
    }
    finally {
        if ($null -ne $key) { $key.Dispose() }
    }
}

# ---------------------------------------------------------------------------
# 命名规则与参数解析
# ---------------------------------------------------------------------------

# 右键菜单键名 / 自启值名：默认前缀 fireloop 时就是规范里的 FireLoop；
# 测试前缀（如 fireloop-test-a1b2）会派生 FireLoop-test-a1b2，保证测试只碰测试命名的键。
function Get-FireLoopHandlerName {
    param([Parameter(Mandatory = $true)][string]$KeyPrefix)

    if ($KeyPrefix -eq 'fireloop') { return 'FireLoop' }
    $suffix = $KeyPrefix
    if ($suffix -like 'fireloop-*') {
        $suffix = $suffix.Substring('fireloop-'.Length)
    }
    if ([string]::IsNullOrWhiteSpace($suffix)) {
        $suffix = $KeyPrefix
    }
    return "FireLoop-$suffix"
}

function Test-FireLoopKeyPrefix {
    param([string]$KeyPrefix)

    if ([string]::IsNullOrWhiteSpace($KeyPrefix)) {
        throw "KeyPrefix 不能为空。默认是 fireloop；测试请用 fireloop-test-<随机>。"
    }
    if ($KeyPrefix -notmatch '^[A-Za-z][A-Za-z0-9+.-]*$') {
        throw "KeyPrefix 不合法：'$KeyPrefix'。必须以字母开头，只能包含字母、数字、+、-、.（协议 scheme 命名规则），例如 fireloop-test-a1b2。"
    }
}

function Resolve-FireLoopPythonExe {
    param([string]$PythonExe)

    $candidate = $PythonExe
    if ([string]::IsNullOrWhiteSpace($candidate)) {
        $candidate = 'D:\Python\pythonw.exe'
    }
    else {
        $candidate = $candidate.Trim().Trim('"')
    }

    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        throw "找不到 Python 解释器：$candidate`r`n  默认应位于 D:\Python\pythonw.exe；可用 -PythonExe 指定其他路径（必须存在）。"
    }
    if ([IO.Path]::GetExtension($candidate) -notin @('.exe')) {
        throw "Python 解释器必须是 .exe 文件：$candidate"
    }
    return (Resolve-Path -LiteralPath $candidate).ProviderPath
}

# ---------------------------------------------------------------------------
# 安装内容（4 类注册项）
# ---------------------------------------------------------------------------

function New-FireLoopContext {
    param(
        [Parameter(Mandatory = $true)][string]$KeyPrefix,
        [Parameter(Mandatory = $true)][string]$PythonExe,
        [Parameter(Mandatory = $true)][string]$ProjectRoot
    )

    $handlerName = Get-FireLoopHandlerName $KeyPrefix
    # 注册表命令统一走 install\launch.py：启动器自己把 <项目根>\src 加进 sys.path，
    # 所以不依赖 PYTHONPATH、不挑启动时的工作目录。
    $launchScript = Join-Path $ProjectRoot 'install\launch.py'
    $quotedPython = '"' + $PythonExe + '"'
    $quotedLaunch = '"' + $launchScript + '"'
    $uriCommand = $quotedPython + ' ' + $quotedLaunch + ' cli uri "%1"'
    $fileCommand = $quotedPython + ' ' + $quotedLaunch + ' cli file "%1"'
    $agentCommand = $quotedPython + ' ' + $quotedLaunch + ' agent'

    $schemeKey = "HKCU\Software\Classes\$KeyPrefix"
    $fileVerbKey = "HKCU\Software\Classes\*\shell\$handlerName"
    $dirVerbKey = "HKCU\Software\Classes\Directory\shell\$handlerName"
    $runKey = 'HKCU\Software\Microsoft\Windows\CurrentVersion\Run'

    $items = @(
        [pscustomobject]@{
            Id = 'scheme'; Category = '1/4'; CategoryName = "协议 handler：$KeyPrefix`://"
            Kind = 'key'; Path = $schemeKey
            Values = @(
                [pscustomobject]@{ Name = ''; Data = ''; Kind = 'String' },
                [pscustomobject]@{ Name = 'URL Protocol'; Data = ''; Kind = 'String' }
            )
        },
        [pscustomobject]@{
            Id = 'scheme-command'; Category = '1/4'; CategoryName = ''
            Kind = 'key'; Path = "$schemeKey\shell\open\command"
            Values = @(
                [pscustomobject]@{ Name = ''; Data = $uriCommand; Kind = 'String' }
            )
        },
        [pscustomobject]@{
            Id = 'file-verb'; Category = '2/4'; CategoryName = '文件右键菜单（显示名：交给火环）'
            Kind = 'key'; Path = $fileVerbKey
            Values = @(
                [pscustomobject]@{ Name = ''; Data = '交给火环'; Kind = 'String' }
            )
        },
        [pscustomobject]@{
            Id = 'file-command'; Category = '2/4'; CategoryName = ''
            Kind = 'key'; Path = "$fileVerbKey\command"
            Values = @(
                [pscustomobject]@{ Name = ''; Data = $fileCommand; Kind = 'String' }
            )
        },
        [pscustomobject]@{
            Id = 'dir-verb'; Category = '3/4'; CategoryName = '文件夹右键菜单（显示名：交给火环）'
            Kind = 'key'; Path = $dirVerbKey
            Values = @(
                [pscustomobject]@{ Name = ''; Data = '交给火环'; Kind = 'String' }
            )
        },
        [pscustomobject]@{
            Id = 'dir-command'; Category = '3/4'; CategoryName = ''
            Kind = 'key'; Path = "$dirVerbKey\command"
            Values = @(
                [pscustomobject]@{ Name = ''; Data = $fileCommand; Kind = 'String' }
            )
        },
        [pscustomobject]@{
            Id = 'run'; Category = '4/4'; CategoryName = '登录自启'
            Kind = 'value'; Path = $runKey; Name = $handlerName; Data = $agentCommand
        }
    )

    $ancestorPaths = @(
        'HKCU\Software\Classes',
        'HKCU\Software\Classes\*',
        'HKCU\Software\Classes\*\shell',
        'HKCU\Software\Classes\Directory',
        'HKCU\Software\Classes\Directory\shell',
        'HKCU\Software\Microsoft\Windows\CurrentVersion\Run'
    )

    return [pscustomobject]@{
        KeyPrefix    = $KeyPrefix
        HandlerName  = $handlerName
        PythonExe    = $PythonExe
        ProjectRoot  = $ProjectRoot
        LaunchScript = $launchScript
        UriCommand   = $uriCommand
        FileCommand  = $fileCommand
        AgentCommand = $agentCommand
        SchemeKey    = $schemeKey
        FileVerbKey  = $fileVerbKey
        DirVerbKey   = $dirVerbKey
        RunKey       = $runKey
        Items        = $items
        AncestorPaths = $ancestorPaths
    }
}

# ---------------------------------------------------------------------------
# 展示与计划
# ---------------------------------------------------------------------------

function Format-FireLoopData {
    param([AllowNull()]$Data)

    if ($null -eq $Data) { return '<无>' }
    return '"' + [string]$Data + '"'
}

function Format-FireLoopValueName {
    param([AllowEmptyString()][string]$Name)

    if ([string]::IsNullOrEmpty($Name)) { return '默认值' }
    return $Name
}

function Get-FireLoopPlan {
    param([Parameter(Mandatory = $true)]$Context)

    $plan = @()
    foreach ($item in $Context.Items) {
        if ($item.Kind -eq 'key') {
            $keyInfo = Get-FireLoopRegistryKeyInfo $item.Path
            $entries = @()
            foreach ($target in $item.Values) {
                $current = $null
                foreach ($existing in $keyInfo.Values) {
                    if ($existing.Name -eq $target.Name) { $current = $existing; break }
                }
                $action = 'Create'
                if ($null -ne $current) {
                    if (([string]$current.Data -ceq [string]$target.Data) -and ($current.Kind -eq $target.Kind)) {
                        $action = 'None'
                    }
                    else {
                        $action = 'Update'
                    }
                }
                $entries += [pscustomobject]@{ Target = $target; Current = $current; Action = $action }
            }
            $plan += [pscustomobject]@{
                Item = $item; KeyExists = $keyInfo.Exists; Entries = @($entries)
            }
        }
        else {
            $current = Get-FireLoopRegistryValue $item.Path $item.Name
            $target = [pscustomobject]@{ Name = $item.Name; Data = $item.Data; Kind = 'String' }
            $action = 'Create'
            if ($current.Exists) {
                if (([string]$current.Data -ceq [string]$item.Data) -and ($current.Kind -eq 'String')) {
                    $action = 'None'
                }
                else {
                    $action = 'Update'
                }
            }
            $plan += [pscustomobject]@{
                Item = $item; KeyExists = $current.Exists
                Entries = @([pscustomobject]@{ Target = $target; Current = $current; Action = $action })
            }
        }
    }
    return $plan
}

function Get-FireLoopPlanActionCount {
    param([Parameter(Mandatory = $true)]$Plan, [Parameter(Mandatory = $true)][string]$Action)

    $count = 0
    foreach ($planItem in $Plan) {
        foreach ($entry in $planItem.Entries) {
            if ($entry.Action -eq $Action) { $count++ }
        }
    }
    return $count
}

function Format-FireLoopActionLabel {
    param([Parameter(Mandatory = $true)]$PlanItem, [Parameter(Mandatory = $true)]$Entry)

    switch ($Entry.Action) {
        'Create' {
            if ($PlanItem.Item.Kind -eq 'key' -and -not $PlanItem.KeyExists) { return '将创建键' }
            return '将新增值'
        }
        'Update' { return '将更新值' }
        'None' { return '无变化' }
        default { return $Entry.Action }
    }
}

function Write-FireLoopInstallPlan {
    param([Parameter(Mandatory = $true)]$Plan)

    $currentCategory = ''
    foreach ($planItem in $Plan) {
        $item = $planItem.Item
        if ($item.Category -ne $currentCategory) {
            $currentCategory = $item.Category
            Write-Output ''
            Write-Output ('[' + $item.Category + '] ' + $item.CategoryName)
        }

        if ($item.Kind -eq 'key') {
            Write-Output ('  键   ' + $item.Path)
            foreach ($entry in $planItem.Entries) {
                $line = '    ' + (Format-FireLoopValueName $entry.Target.Name) + ' = ' + (Format-FireLoopData $entry.Target.Data)
                $line += '    [' + (Format-FireLoopActionLabel $planItem $entry) + ']'
                Write-Output $line
                if ($entry.Action -eq 'Update') {
                    Write-Output ('        当前 = ' + (Format-FireLoopData $entry.Current.Data) + '（类型 ' + $entry.Current.Kind + '）')
                }
            }
        }
        else {
            Write-Output ('  值   ' + $item.Path)
            foreach ($entry in $planItem.Entries) {
                $line = '    ' + $entry.Target.Name + ' = ' + (Format-FireLoopData $entry.Target.Data)
                $line += '    [' + (Format-FireLoopActionLabel $planItem $entry) + ']'
                Write-Output $line
                if ($entry.Action -eq 'Update') {
                    Write-Output ('        当前 = ' + (Format-FireLoopData $entry.Current.Data) + '（类型 ' + $entry.Current.Kind + '）')
                }
            }
        }
    }
}

# ---------------------------------------------------------------------------
# 备份
# ---------------------------------------------------------------------------

function Get-FireLoopBackupRoot {
    $localAppData = $env:LOCALAPPDATA
    if ([string]::IsNullOrWhiteSpace($localAppData)) {
        throw 'LOCALAPPDATA 环境变量为空，无法确定备份目录。'
    }
    return (Join-Path $localAppData 'FireLoop\backup')
}

function New-FireLoopBackupDirectory {
    param([Parameter(Mandatory = $true)][string]$Stamp)

    $root = Get-FireLoopBackupRoot
    if (-not (Test-Path -LiteralPath $root)) {
        New-Item -ItemType Directory -Path $root -Force | Out-Null
    }
    $candidate = Join-Path $root ("reg-$Stamp")
    $index = 2
    while (Test-Path -LiteralPath $candidate) {
        $candidate = Join-Path $root ("reg-$Stamp-$index")
        $index++
    }
    New-Item -ItemType Directory -Path $candidate -Force | Out-Null
    return $candidate
}

function Write-FireLoopJsonFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Object
    )

    $json = $Object | ConvertTo-Json -Depth 8
    [IO.File]::WriteAllText($Path, $json + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
}

function Read-FireLoopJsonFile {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    $text = [IO.File]::ReadAllText($Path, [Text.UTF8Encoding]::new($false))
    if ([string]::IsNullOrWhiteSpace($text)) { return $null }
    return ($text | ConvertFrom-Json)
}

function Export-FireLoopRegistryKey {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$DestinationFile
    )

    $null = & reg.exe export $Path $DestinationFile /y 2>&1
    return ($LASTEXITCODE -eq 0)
}

function Get-FireLoopBackupSnapshot {
    param([Parameter(Mandatory = $true)]$Context)

    $seen = @{}
    $keySnapshots = @()
    foreach ($item in $Context.Items) {
        if ($item.Kind -ne 'key') { continue }
        $dedupeKey = $item.Path.ToLowerInvariant()
        if ($seen.ContainsKey($dedupeKey)) { continue }
        $seen[$dedupeKey] = $true
        $keySnapshots += Get-FireLoopRegistryKeyInfo $item.Path
    }

    $runValue = Get-FireLoopRegistryValue $Context.RunKey $Context.HandlerName
    $runSnapshot = [pscustomobject]@{
        Path   = $Context.RunKey
        Name   = $Context.HandlerName
        Exists = $runValue.Exists
        Data   = $runValue.Data
        Kind   = $runValue.Kind
    }

    $ancestors = @()
    foreach ($ancestorPath in $Context.AncestorPaths) {
        $ancestors += [pscustomobject]@{
            Path   = $ancestorPath
            Exists = (Test-FireLoopRegistryKey $ancestorPath)
        }
    }

    return [pscustomobject]@{
        CreatedAt     = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
        KeySnapshots  = @($keySnapshots)
        RunValue      = $runSnapshot
        Ancestors     = @($ancestors)
    }
}

function Write-FireLoopInstallBackup {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)]$Snapshot,
        [Parameter(Mandatory = $true)][string]$Stamp
    )

    $directory = New-FireLoopBackupDirectory $Stamp

    $exportCandidates = @(
        [pscustomobject]@{ Path = $Context.SchemeKey; File = 'scheme.reg' },
        [pscustomobject]@{ Path = $Context.FileVerbKey; File = 'file-verb.reg' },
        [pscustomobject]@{ Path = $Context.DirVerbKey; File = 'dir-verb.reg' }
    )
    $exports = @()
    foreach ($candidate in $exportCandidates) {
        if (-not (Test-FireLoopRegistryKey $candidate.Path)) { continue }
        $destination = Join-Path $directory $candidate.File
        $success = Export-FireLoopRegistryKey $candidate.Path $destination
        $exports += [pscustomobject]@{
            Path = $candidate.Path; File = $destination; Success = $success
        }
    }

    $manifest = [pscustomobject]@{
        schema     = 1
        kind       = 'install-backup'
        created_at = $Snapshot.CreatedAt
        key_prefix = $Context.KeyPrefix
        handler_name = $Context.HandlerName
        python_exe = $Context.PythonExe
        backup_dir = $directory
        keys       = @($Snapshot.KeySnapshots)
        run_value  = $Snapshot.RunValue
        ancestors  = @($Snapshot.Ancestors)
        reg_exports = @($exports)
    }
    Write-FireLoopJsonFile (Join-Path $directory 'before.json') $manifest

    return [pscustomobject]@{
        Directory  = $directory
        Manifest   = $manifest
        RegExports = @($exports)
    }
}

function Write-FireLoopInstallResult {
    param(
        [Parameter(Mandatory = $true)]$Context,
        [Parameter(Mandatory = $true)]$Backup,
        [Parameter(Mandatory = $true)]$Snapshot
    )

    $createdKeys = @()
    foreach ($keySnapshot in $Snapshot.KeySnapshots) {
        if (-not $keySnapshot.Exists) { $createdKeys += $keySnapshot.Path }
    }

    $ancestorsAbsent = @()
    foreach ($ancestor in $Snapshot.Ancestors) {
        if (-not $ancestor.Exists) { $ancestorsAbsent += $ancestor.Path }
    }

    $result = [pscustomobject]@{
        schema        = 1
        kind          = 'install-result'
        installed_at  = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
        key_prefix    = $Context.KeyPrefix
        handler_name  = $Context.HandlerName
        python_exe    = $Context.PythonExe
        backup_dir    = $Backup.Directory
        target_keys   = @($Context.Items | Where-Object { $_.Kind -eq 'key' } | ForEach-Object { $_.Path })
        target_run_value = [pscustomobject]@{ path = $Context.RunKey; name = $Context.HandlerName }
        created_keys  = @($createdKeys)
        ancestors_absent_before = @($ancestorsAbsent)
    }
    Write-FireLoopJsonFile (Join-Path $Backup.Directory 'install-result.json') $result
    return $result
}

function Find-FireLoopInstallResult {
    param(
        [Parameter(Mandatory = $true)][string]$KeyPrefix,
        [Parameter(Mandatory = $true)][string]$HandlerName
    )

    $root = Get-FireLoopBackupRoot
    if (-not (Test-Path -LiteralPath $root)) { return $null }

    $directories = @(Get-ChildItem -LiteralPath $root -Directory |
        Where-Object { $_.Name -like 'reg-*' } |
        Sort-Object -Property Name -Descending)

    foreach ($directory in $directories) {
        $result = Read-FireLoopJsonFile (Join-Path $directory.FullName 'install-result.json')
        if ($null -eq $result) { continue }
        if ($result.key_prefix -eq $KeyPrefix -and $result.handler_name -eq $HandlerName) {
            return $result
        }
    }
    return $null
}

# ---------------------------------------------------------------------------
# 预检：注册表里的启动器命令能不能真正跑起来（不依赖 PYTHONPATH）
# ---------------------------------------------------------------------------

function Test-FireLoopLauncher {
    param(
        [Parameter(Mandatory = $true)][string]$PythonExe,
        [Parameter(Mandatory = $true)][string]$ProjectRoot
    )

    $launcherPath = Join-Path $ProjectRoot 'install\launch.py'
    $workingDirectory = $env:SystemRoot
    if ([string]::IsNullOrWhiteSpace($workingDirectory)) { $workingDirectory = 'C:\Windows' }

    $result = [pscustomobject]@{
        Attempted        = $false
        Ok               = $false
        Detail           = ''
        ConsoleExe       = $null
        LauncherPath     = $launcherPath
        ProjectRoot      = $ProjectRoot
        SourceDir        = (Join-Path $ProjectRoot 'src')
        WorkingDirectory = $workingDirectory
    }

    if (-not (Test-Path -LiteralPath $launcherPath -PathType Leaf)) {
        $result.Detail = "找不到启动器：$launcherPath"
        return $result
    }

    # pythonw 没有控制台、失败是静默的；优先用同目录的 python.exe 做可观测检查。
    $candidates = @()
    $pythonDir = Split-Path -Parent $PythonExe
    if ([IO.Path]::GetFileName($PythonExe) -ieq 'pythonw.exe') {
        $consolePython = Join-Path $pythonDir 'python.exe'
        if (Test-Path -LiteralPath $consolePython -PathType Leaf) {
            $candidates += $consolePython
        }
    }
    $candidates += $PythonExe

    foreach ($candidate in $candidates) {
        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = $candidate
        $startInfo.Arguments = '"' + $launcherPath + '" cli --help'
        $startInfo.WorkingDirectory = $workingDirectory
        $startInfo.UseShellExecute = $false
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $startInfo.CreateNoWindow = $true

        $result.Attempted = $true
        $result.ConsoleExe = $candidate
        try {
            $process = [System.Diagnostics.Process]::Start($startInfo)
        }
        catch {
            $result.Detail = $_.Exception.Message
            continue
        }

        if (-not $process.WaitForExit(8000)) {
            try { $process.Kill() } catch { }
            $result.Detail = '启动器自检超时（8 秒）'
            continue
        }
        $stderrText = $process.StandardError.ReadToEnd().Trim()
        $exitCode = $process.ExitCode
        $process.Dispose()

        if ($exitCode -eq 0) {
            $result.Ok = $true
            $result.Detail = ''
            return $result
        }
        $lastLine = ($stderrText -split "`r?`n" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Last 1)
        if ([string]::IsNullOrWhiteSpace($lastLine)) { $lastLine = "exit code $exitCode" }
        $result.Detail = $lastLine
        return $result
    }
    return $result
}
