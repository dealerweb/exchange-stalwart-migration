Get-Mailbox -ResultSize Unlimited | Add-MailboxPermission -User administrator@domain.com -AccessRights FullAccess -InheritanceType All -AutoMapping $false
New-ManagementRoleAssignment -Name "EWSImpersonation" -Role ApplicationImpersonation -User administrator@domain.com

New-ThrottlingPolicy -Name "MigrationPolicy" -EwsMaxConcurrency $null -EwsMaxBurst $null -EwsRechargeRate $null -EwsCutoffBalance $null
Set-Mailbox -Identity "administrator@domain.com" -ThrottlingPolicy "MigrationPolicy"

New-ThrottlingPolicy -Name "MigrationOrgPolicy" -ThrottlingPolicyScope Organization -EwsMaxConcurrency 100 -EwsMaxBurst 9999999 -EwsRechargeRate 9999999 -EwsCutoffBalance 9999999
iisreset

