FROM mcr.microsoft.com/devcontainers/python:3.13-bookworm

# Remove the problematic Yarn repository with expired GPG key
# This fixes: "NO_PUBKEY 62D54FD4003F6525" error during apt-get update
RUN rm -f /etc/apt/sources.list.d/yarn.list 2>/dev/null || true
