$CsvPath = "C:\exchange_users.csv"
$BaseUrl = "https://mail.domain.com"
$AdminUser = "admin@domain.com"
$AdminPass = "CHANGE_ME"
$DomainId = "b"
$DefaultPass = "CHANGE_ME"

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
}
catch {}
[System.Net.ServicePointManager]::CertificatePolicy = New-Object TrustAllCertsPolicy2
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12

$auth = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("${AdminUser}:${AdminPass}"))
$headers = @{
  "Authorization" = "Basic $auth"
  "Content-Type"  = "application/json; charset=utf-8"
}

# Vorhandene User laden
$queryBody = @{
  using       = @("urn:ietf:params:jmap:core", "urn:stalwart:jmap")
  methodCalls = @(
    , @("x:Account/query", @{ accountId = $DomainId }, "0")
    , @("x:Account/get", @{
        accountId  = $DomainId
        "#ids"     = @{ resultOf = "0"; name = "x:Account/query"; path = "/ids" }
        properties = @("email")
      }, "1")
  )
} | ConvertTo-Json -Depth 10 -Compress

$queryBytes = [System.Text.Encoding]::UTF8.GetBytes($queryBody)
$queryResp = Invoke-RestMethod -Uri "$BaseUrl/jmap" -Method Post -Headers $headers -Body $queryBytes
$existingEmails = $queryResp.methodResponses[1][1].list | ForEach-Object { $_.email }

$users = Import-Csv -Path $CsvPath -Encoding UTF8
$created = 0; $skipped = 0; $errors = 0

foreach ($user in $users) {
  $email = $user.PrimarySmtpAddress.Trim()
  $displayName = $user.DisplayName.Trim()
  $localPart = $email.Split("@")[0]

  if ($existingEmails -contains $email) {
    Write-Host "Überspringe (bereits vorhanden): $email" -ForegroundColor Yellow
    $skipped++
    continue
  }

  Write-Host "Erstelle: $displayName ($email) ..." -NoNewline

  $createBody = @{
    using       = @("urn:ietf:params:jmap:core", "urn:stalwart:jmap")
    methodCalls = @(
      , @("x:Account/set", @{
          accountId = $DomainId
          create    = @{
            new1 = @{
              name        = $localPart
              domainId    = $DomainId
              displayName = $displayName
              password    = $DefaultPass
              email       = $email
            }
          }
        }, "0")
    )
  } | ConvertTo-Json -Depth 10 -Compress

  try {
    $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($createBody)
    $resp = Invoke-RestMethod -Uri "$BaseUrl/jmap" -Method Post -Headers $headers -Body $bodyBytes
    $result = $resp.methodResponses[0][1]

    if ($result.created.new1) {
      Write-Host " OK" -ForegroundColor Green
      $created++
    }
    else {
      $err = $result.notCreated.new1 | ConvertTo-Json
      Write-Host " FEHLER: $err" -ForegroundColor Red
      $errors++
    }
  }
  catch {
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