#!/bin/bash
# MeshBOT Deployment Setup Script
# Run this script to set up a fresh MeshBOT deployment

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================"
echo "  MeshBOT Deployment Setup"
echo "========================================"
echo ""

# Check for Docker
if ! command -v docker &> /dev/null; then
    echo "[!] Docker not found. Installing..."
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker "$USER"
    echo "[+] Docker installed. You may need to log out and back in."
fi

# Check for Docker Compose
if ! docker compose version &> /dev/null; then
    echo "[!] Docker Compose not found. Please install Docker Compose v2."
    exit 1
fi

echo "[+] Docker and Docker Compose found"

# Create directory structure
echo "[*] Creating directory structure..."
mkdir -p data logs webgui/backups

# Copy environment file if not exists
if [ ! -f .env ]; then
    echo "[*] Creating .env from template..."
    cp .env.example .env
    echo "[!] Please edit .env with your settings before starting"
fi

# Download config template if not exists
if [ ! -f config.template ]; then
    echo "[*] Downloading config template..."
    curl -fsSL "https://raw.githubusercontent.com/spudgunman/meshing-around/main/config.template" -o config.template
fi

# Create config.ini from template if not exists
if [ ! -f config.ini ]; then
    echo "[*] Creating config.ini from template..."
    cp config.template config.ini
    echo "[!] Please edit config.ini with your mesh node settings"
fi

# Create empty schedules.json if not exists
if [ ! -f webgui/schedules.json ]; then
    echo "[*] Creating schedules.json..."
    echo '{"schedules": []}' > webgui/schedules.json
fi

# Clone/copy webgui if not exists
if [ ! -d webgui ] || [ ! -f webgui/Dockerfile ]; then
    echo "[*] Setting up Web GUI..."

    # Check if we're in the meshing-around repo
    if [ -d "../webgui" ]; then
        cp -r ../webgui/* webgui/
    else
        echo "[!] Web GUI not found. Please copy the webgui directory here."
        echo "    Or clone from: https://github.com/spudgunman/meshing-around"
        exit 1
    fi
fi

echo ""
echo "========================================"
echo "  Setup Complete!"
echo "========================================"
echo ""
echo "Next steps:"
echo ""
echo "1. Edit configuration files:"
echo "   nano .env           # Environment settings"
echo "   nano config.ini     # MeshBOT settings"
echo ""
echo "2. Configure your mesh node connection in config.ini:"
echo "   [interface]"
echo "   type = tcp"
echo "   hostname = YOUR_NODE_IP:4403"
echo ""
echo "3. Start the services:"
echo "   docker compose up -d"
echo ""
echo "4. Access the Web GUI:"
echo "   http://localhost:8421"
echo ""
echo "Optional - Start with LLM support:"
echo "   docker compose --profile llm up -d"
echo ""
