$CsvPath   = "C:\exchange_groups.csv"
$BaseUrl   = "https://mail.domain.com"
$AdminUser = "admin@domain.com"
$AdminPass = "CHANGE_ME"
$DomainId  = "b"

$PSDefaultParameterValues = @{
    'Invoke-RestMethod:SkipCertificateCheck' = $true
    'Invoke-WebRequest:SkipCertificateCheck' = $true
}
try {
    Add-Type @"
using System.Net;
using System.Security.Cryptography.X509Certificates;
public class TrustAllCertsPolicy2 : ICertificatePolicy {
    public bool CheckValidationResult(
        ServicePoint srvPoint, X509Certificate certificate,
        WebRequest request, int certificateProblem) {
        return true;
    }
}
"@
} catch {}
[System.Net.ServicePointManager]::CertificatePolicy = New-Object TrustAllCertsPolicy2
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12

$auth    = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("${AdminUser}:${AdminPass}"))
$headers = @{
    "Authorization" = "Basic $auth"
    "Content-Type"  = "application/json; charset=utf-8"
}

# JMAP Session
$session   = Invoke-RestMethod -Uri "$BaseUrl/jmap/session" -Headers $headers
$accountId = ($session.accounts.PSObject.Properties | Select-Object -First 1).Name

# Vorhandene Mailing Lists laden
$queryBody = @{
    using       = @("urn:ietf:params:jmap:core", "urn:stalwart:jmap")
    methodCalls = @(
        ,@("x:MailingList/query", @{ accountId = $accountId }, "0")
        ,@("x:MailingList/get", @{
            accountId  = $accountId
            "#ids"     = @{ resultOf = "0"; name = "x:MailingList/query"; path = "/ids" }
            properties = @("emailAddress")
        }, "1")
    )
} | ConvertTo-Json -Depth 10 -Compress

$queryBytes    = [System.Text.Encoding]::UTF8.GetBytes($queryBody)
$queryResp     = Invoke-RestMethod -Uri "$BaseUrl/jmap" -Method Post -Headers $headers -Body $queryBytes
$existingLists = $queryResp.methodResponses[1][1].list | ForEach-Object { $_.emailAddress }

$groups  = Import-Csv -Path $CsvPath -Encoding UTF8
$created = 0; $skipped = 0; $errors = 0

foreach ($group in $groups) {
    $email       = $group.PrimarySmtpAddress.Trim()
    $displayName = $group.DisplayName.Trim()
    $localPart   = $email.Split("@")[0]

    if ($existingLists -contains $email) {
        Write-Host "Überspringe (bereits vorhanden): $email" -ForegroundColor Yellow
        $skipped++
        continue
    }

    Write-Host "Erstelle: $displayName ($email) ..." -NoNewline

    # Recipients als Dictionary mit true als Value
    $recipientsMap = @{}
    if ($group.Members) {
        $group.Members.Split(";") |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ -ne "" } |
            ForEach-Object {
                $recipientsMap[$_] = $true
            }
    }

    $createBody = @{
        using       = @("urn:ietf:params:jmap:core", "urn:stalwart:jmap")
        methodCalls = @(
            ,@("x:MailingList/set", @{
                accountId = $accountId
                create    = @{
                    new1 = @{
                        name        = $localPart
                        domainId    = $DomainId
                        description = $displayName
                        recipients  = $recipientsMap
                    }
                }
            }, "0")
        )
    } | ConvertTo-Json -Depth 10 -Compress

    try {
        $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($createBody)
        $resp      = Invoke-RestMethod -Uri "$BaseUrl/jmap" -Method Post -Headers $headers -Body $bodyBytes
        $result    = $resp.methodResponses[0][1]

        if ($result.created.new1) {
            Write-Host " OK (id: $($result.created.new1.id))" -ForegroundColor Green
            $created++
        } else {
            $err = $result.notCreated.new1 | ConvertTo-Json
            Write-Host " FEHLER: $err" -ForegroundColor Red
            $errors++
        }
    } catch {
        Write-Host " FEHLER: $_" -ForegroundColor Red
        $errors++
    }
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Fertig!"
Write-Host "  Erstellt:     $created" -ForegroundColor Green
Write-Host "  Übersprungen: $skipped" -ForegroundColor Yellow
Write-Host "  Fehler:       $errors" -ForegroundColor Red
Write-Host "========================================" -ForegroundColor Cyan