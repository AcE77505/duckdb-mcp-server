#!/usr/bin/env bash

# Use with: source ./clear-mcp-env.sh
# This must run in the current shell to clear current session env vars.

unset MCP_TRANSPORT
unset MCP_PATH
unset MCP_MESSAGE_PATH
unset ALLOWED_HOSTS
unset ENABLE_DNS_REBINDING_PROTECTION
unset DISABLE_DNS_REBINDING_PROTECTION

echo "Cleared MCP transport/security-related environment variables in current shell."
