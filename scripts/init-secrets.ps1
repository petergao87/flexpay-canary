[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$projectDirectory = Split-Path -Parent $PSScriptRoot
$secretDirectory = Join-Path $projectDirectory '.secrets'
New-Item -ItemType Directory -Path $secretDirectory -Force | Out-Null
# Permit the current operator and Windows SYSTEM; omit inherited access.
$currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$directoryAcl = Get-Acl -LiteralPath $secretDirectory
$directoryAcl.SetAccessRuleProtection($true, $false)
foreach ($identity in @($currentIdentity, 'NT AUTHORITY\SYSTEM')) {
    $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
        $identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $directoryAcl.SetAccessRule($rule)
}
Set-Acl -LiteralPath $secretDirectory -AclObject $directoryAcl

function New-RandomHex {
    param([int]$Bytes = 32)
    $randomBytes = New-Object byte[] $Bytes
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($randomBytes) } finally { $generator.Dispose() }
    return ([System.BitConverter]::ToString($randomBytes)).Replace('-', '').ToLowerInvariant()
}

$secretValues = @{
    database_url = 'mock://local/' + (New-RandomHex)
    provider_api_key = 'mock-only-' + (New-RandomHex)
    grafana_admin_password = New-RandomHex
}
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
foreach ($secretName in $secretValues.Keys) {
    $secretPath = Join-Path $secretDirectory $secretName
    if (-not (Test-Path -LiteralPath $secretPath)) {
        [System.IO.File]::WriteAllText($secretPath, $secretValues[$secretName], $utf8NoBom)
        Write-Host "Created .secrets/$secretName (value hidden)"
    } else {
        Write-Host "Keeping existing .secrets/$secretName"
    }
}
Write-Host 'Mock credentials are ready. Values are hidden and the directory is restricted to your account and SYSTEM.'
