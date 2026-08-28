[CmdletBinding()]
param(
    [string]$KeystorePath = (Join-Path $HOME '.openadb-signing\acbridge-release.p12'),
    [string]$PasswordFile = (Join-Path $HOME '.openadb-signing\acbridge-release-password.dpapi'),
    [string]$KeyAlias = 'openadb-release',
    [string]$AndroidBuildToolsVersion = '37.0.0',
    [string]$AndroidPlatformVersion = '36',
    [string]$Python = 'python'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $KeystorePath -PathType Leaf)) {
    throw "ACBridge release keystore was not found: $KeystorePath"
}
if (-not (Test-Path -LiteralPath $PasswordFile -PathType Leaf)) {
    throw "The DPAPI-protected ACBridge password file was not found: $PasswordFile"
}

$securePassword = Get-Content -LiteralPath $PasswordFile -Raw | ConvertTo-SecureString
$credential = [pscredential]::new('acbridge-release', $securePassword)
$plainPassword = $credential.GetNetworkCredential().Password
$previousBuildToolsVersion = [Environment]::GetEnvironmentVariable(
    'ANDROID_BUILD_TOOLS_VERSION',
    'Process'
)
$previousPlatformVersion = [Environment]::GetEnvironmentVariable(
    'ANDROID_PLATFORM_VERSION',
    'Process'
)
try {
    $env:ACBRIDGE_RELEASE_KEYSTORE = (Resolve-Path -LiteralPath $KeystorePath).Path
    $env:ACBRIDGE_RELEASE_STORE_PASSWORD = $plainPassword
    $env:ACBRIDGE_RELEASE_KEY_PASSWORD = $plainPassword
    $env:ACBRIDGE_RELEASE_KEY_ALIAS = $KeyAlias
    $env:ANDROID_BUILD_TOOLS_VERSION = $AndroidBuildToolsVersion
    $env:ANDROID_PLATFORM_VERSION = $AndroidPlatformVersion
    & $Python tools/build_acbridge.py --signing-mode release
    if ($LASTEXITCODE -ne 0) {
        throw "ACBridge release build failed with exit code $LASTEXITCODE."
    }
}
finally {
    Remove-Item Env:ACBRIDGE_RELEASE_KEYSTORE -ErrorAction SilentlyContinue
    Remove-Item Env:ACBRIDGE_RELEASE_STORE_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:ACBRIDGE_RELEASE_KEY_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:ACBRIDGE_RELEASE_KEY_ALIAS -ErrorAction SilentlyContinue
    if ($null -eq $previousBuildToolsVersion) {
        Remove-Item Env:ANDROID_BUILD_TOOLS_VERSION -ErrorAction SilentlyContinue
    }
    else {
        $env:ANDROID_BUILD_TOOLS_VERSION = $previousBuildToolsVersion
    }
    if ($null -eq $previousPlatformVersion) {
        Remove-Item Env:ANDROID_PLATFORM_VERSION -ErrorAction SilentlyContinue
    }
    else {
        $env:ANDROID_PLATFORM_VERSION = $previousPlatformVersion
    }
    $plainPassword = $null
    $credential = $null
    $securePassword = $null
}
