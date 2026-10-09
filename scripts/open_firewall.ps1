<#
.SYNOPSIS
  Allow the gateway port through Windows Firewall on the domain network only.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\open_firewall.ps1
.EXAMPLE
  # Restrict to the workstation subnets
  powershell -ExecutionPolicy Bypass -File scripts\open_firewall.ps1 -RemoteAddress 10.20.0.0/16,10.30.0.0/16
#>
#Requires -RunAsAdministrator
param(
    [int]$Port = 8000,
    [string[]]$RemoteAddress = @("LocalSubnet"),
    [string]$RuleName = "ComfyFarm gateway"
)
$ErrorActionPreference = "Stop"
Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Protocol TCP -LocalPort $Port `
    -Action Allow -Profile Domain -RemoteAddress $RemoteAddress | Out-Null
Write-Host "Opened TCP $Port (domain profile) for: $($RemoteAddress -join ', ')"
Write-Host "ComfyUI's own port (8188) stays closed - only the gateway talks to it."
