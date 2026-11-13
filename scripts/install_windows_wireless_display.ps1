#Requires -RunAsAdministrator
<#.SYNOPSIS
Install only Microsoft's Wireless Display receiver capability, without rebooting.
Review this script and approve Windows elevation before running it.
#>
[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$ReceiptPath)

$ErrorActionPreference = 'Stop'
$capabilityName = 'App.WirelessDisplay.Connect~~~~0.0.1.0'
try {
    $before = Get-WindowsCapability -Online -Name $capabilityName
    $restartNeeded = $false
    if ($before.State -ne 'Installed') {
        $result = Add-WindowsCapability -Online -Name $capabilityName
        $restartNeeded = [bool]$result.RestartNeeded
    }
    $after = Get-WindowsCapability -Online -Name $capabilityName
    $receipt = @{
        feature = $capabilityName
        status = if ($after.State -eq 'Installed') { 'installed' } else { 'incomplete' }
        before = [string]$before.State
        after = [string]$after.State
        restart_needed = $restartNeeded
        reboot_performed = $false
        checked_at_utc = [DateTime]::UtcNow.ToString('o')
    }
} catch {
    $receipt = @{
        feature = $capabilityName
        status = 'failed'
        error = $_.Exception.Message
        reboot_performed = $false
        checked_at_utc = [DateTime]::UtcNow.ToString('o')
    }
}
[IO.File]::WriteAllText($ReceiptPath, ($receipt | ConvertTo-Json -Depth 4), [Text.UTF8Encoding]::new($false))
