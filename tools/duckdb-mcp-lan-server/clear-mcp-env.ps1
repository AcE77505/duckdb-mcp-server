Remove-Item Env:MCP_TRANSPORT -ErrorAction SilentlyContinue
Remove-Item Env:MCP_PATH -ErrorAction SilentlyContinue
Remove-Item Env:MCP_MESSAGE_PATH -ErrorAction SilentlyContinue
Remove-Item Env:ALLOWED_HOSTS -ErrorAction SilentlyContinue
Remove-Item Env:ENABLE_DNS_REBINDING_PROTECTION -ErrorAction SilentlyContinue
Remove-Item Env:DISABLE_DNS_REBINDING_PROTECTION -ErrorAction SilentlyContinue

Write-Host "Cleared MCP transport/security-related environment variables in current PowerShell session."
