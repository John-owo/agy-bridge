param(
    [string]$Package = (Join-Path $PSScriptRoot 'agy-bridge'),
    [string]$UserRoot = $env:USERPROFILE
)
$ErrorActionPreference = 'Stop'
$Utf8 = New-Object System.Text.UTF8Encoding($false)
$Canonical = Join-Path $UserRoot '.agents\skills\agy-bridge'
$Claude = Join-Path $UserRoot '.claude\skills\agy-bridge'
$Settings = Join-Path $UserRoot '.gemini\antigravity-cli\settings.json'
$RunsRoot = Join-Path $UserRoot 'Documents\Codex\agy-bridge-runs'
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'

if (-not (Test-Path -LiteralPath (Join-Path $Package 'SKILL.md'))) { throw 'Skill package missing SKILL.md.' }
if (-not (Test-Path -LiteralPath (Join-Path $Package 'scripts\agy_bridge.py'))) { throw 'Skill package missing helper.' }
$Package = (Resolve-Path -LiteralPath $Package).Path
if (Test-Path -LiteralPath $Canonical) { throw "Skill already exists at $Canonical; inspect before updating." }
if (Test-Path -LiteralPath $Claude) { throw "Claude skill already exists at $Claude; inspect before updating." }

# Validate all settings before touching the installation. Keep unrelated fields.
if (-not (Test-Path -LiteralPath $Settings)) { throw 'agy settings missing; sign in with agy first.' }
$Raw = [IO.File]::ReadAllText($Settings, [Text.Encoding]::UTF8).TrimStart([char]0xFEFF)
$Data = $Raw | ConvertFrom-Json
if ($null -eq $Data -or $Data -isnot [pscustomobject]) { throw 'agy settings must be a JSON object.' }
if ($Data.PSObject.Properties['modelProvider'] -and $Data.modelProvider -notin @($null, '', 'default')) { throw 'Custom agy modelProvider configured; subscription routing must be checked first.' }
if ($Data.PSObject.Properties['permissions'] -and $Data.permissions -isnot [pscustomobject]) { throw 'agy permissions must be an object.' }
if ($Data.permissions -and $Data.permissions.PSObject.Properties['allow'] -and $null -ne $Data.permissions.allow -and $Data.permissions.allow -isnot [array]) { throw 'agy permissions.allow must be an array.' }
if ($Data.PSObject.Properties['trustedWorkspaces'] -and $null -ne $Data.trustedWorkspaces -and $Data.trustedWorkspaces -isnot [array]) { throw 'agy trustedWorkspaces must be an array.' }

New-Item -ItemType Directory -Path (Split-Path $Canonical -Parent) -Force | Out-Null
New-Item -ItemType Directory -Path (Split-Path $Claude -Parent) -Force | Out-Null
Copy-Item -LiteralPath $Package -Destination $Canonical -Recurse
try {
    New-Item -ItemType Junction -Path $Claude -Target $Canonical | Out-Null
} catch {
    throw "Canonical skill installed at $Canonical, but CC junction failed: $($_.Exception.Message)"
}

$Backup = "$Settings.bak-agy-bridge-$Stamp"
Copy-Item -LiteralPath $Settings -Destination $Backup
$Data | Add-Member -NotePropertyName useG1Credits -NotePropertyValue $false -Force
if (-not $Data.PSObject.Properties['permissions']) { $Data | Add-Member -NotePropertyName permissions -NotePropertyValue ([pscustomobject]@{}) }
$RunWriteRule = "write_file($RunsRoot\)"
$Allow = @(@($Data.permissions.allow) | Where-Object { $null -ne $_ })
if ($Allow -notcontains $RunWriteRule) { $Allow += $RunWriteRule }
$Data.permissions | Add-Member -NotePropertyName allow -NotePropertyValue ([string[]]$Allow) -Force
$Trusted = @(@($Data.trustedWorkspaces) | Where-Object { $null -ne $_ })
if ($Trusted -notcontains $RunsRoot) { $Trusted += $RunsRoot }
$Data | Add-Member -NotePropertyName trustedWorkspaces -NotePropertyValue ([string[]]$Trusted) -Force
New-Item -ItemType Directory -Path $RunsRoot -Force | Out-Null
$Temp = "$Settings.agy-bridge-$Stamp.tmp"
[IO.File]::WriteAllText($Temp, ($Data | ConvertTo-Json -Depth 100), $Utf8)
Move-Item -LiteralPath $Temp -Destination $Settings -Force
[pscustomobject]@{
    skill = $Canonical
    claude = $Claude
    claudeTarget = (Get-Item -LiteralPath $Claude).Target
    creditsOverage = $false
    writableRunRoot = $RunsRoot
    settingsBackup = $Backup
} | ConvertTo-Json -Depth 4
