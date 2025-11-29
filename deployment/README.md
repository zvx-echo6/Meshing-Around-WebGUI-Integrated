# Meshing-Around-WebGUI

Web-based configuration GUI for [meshing-around](https://github.com/spudgunman/meshing-around) MeshBOT.

## Attribution

This project is a fork/extension of **[meshing-around](https://github.com/spudgunman/meshing-around)** by [spudgunman](https://github.com/spudgunman) and contributors.

- **Original Project:** https://github.com/spudgunman/meshing-around
- **License:** GPL-3.0 (same as original)

This fork adds a web-based configuration management interface. The core MeshBOT functionality comes from the original meshing-around project.

### Disclaimer

This is entirely vibe coded but tested. There is no intention of updating the core Meshing-Around features as they currently suit my purposes. Only should there be any outstanding issues or security vulnerabilities within the core application will there up updates.

## Features

- **Web GUI** for editing `config.ini` without SSH access
- **Interface Management** - Configure up to 9 Meshtastic radio interfaces
- **Custom Scheduler** - Visual schedule management for automated messages
- **Service Control** - Restart MeshBOT from the web interface
- **Backup/Restore** - Config backup and restore functionality
- **Docker Deployment** - Easy containerized deployment

## Quick Start

```bash
# Clone this repository
git clone https://forge.echo6.co/matt/Meshing-Around-WebGUI.git
cd Meshing-Around-WebGUI

# Run setup
./setup.sh

# Edit configuration
nano config.ini     # Set your mesh node connection
nano .env           # Set timezone, ports, etc.

# Start services
docker compose up -d

# Access Web GUI
open http://localhost:8421
```

## Services

| Service | Description | Port |
|---------|-------------|------|
| meshbot | Main MeshBOT application ([meshing-around](https://github.com/spudgunman/meshing-around)) | Host network |
| webgui | Web configuration UI | 8421 |
| ollama | Optional LLM (use `--profile llm`) | 11434 |

## Configuration

### Mesh Node Connection

Edit `config.ini` and set your connection type:

**TCP (recommended for remote nodes):**
```ini
[interface]
type = tcp
hostname = 192.168.1.251:4403
```

**Serial (for USB-connected nodes):**
```ini
[interface]
type = serial
port = /dev/ttyUSB0
```

Also uncomment the `devices` section in `docker-compose.yml` for serial.

### Multiple Interfaces

The Web GUI supports up to 9 interfaces. Add more via the Interfaces page.

### Scheduled Messages

1. Go to **Scheduler** in the Web GUI
2. Enable **"Use Custom Scheduler"**
3. Add your custom schedules below

## Commands

```bash
# Start services
docker compose up -d

# Start with LLM support
docker compose --profile llm up -d

# View logs
docker compose logs -f meshbot

# Restart after config changes
docker compose restart meshbot

# Stop everything
docker compose down

# Update to latest version
docker compose pull
docker compose up -d
```

## File Structure

```
Meshing-Around-WebGUI/
├── LICENSE               # GPL-3.0 (same as original)
├── README.md             # This file
├── docker-compose.yml    # Service definitions
├── .env.example          # Environment template
├── config.template       # MeshBOT config reference
├── setup.sh              # Setup script
├── data/                 # MeshBOT data (databases, etc.)
├── logs/                 # Log files
└── webgui/
    ├── Dockerfile
    ├── main.py           # FastAPI backend
    ├── config_schema.py  # Config field definitions
    ├── templates/        # HTML templates
    ├── backups/          # Config backups
    └── schedules.json    # Custom schedules
```

## Troubleshooting

### Can't connect to mesh node

1. Verify the node IP is reachable: `ping 192.168.1.251`
2. Check if TCP port is open: `nc -zv 192.168.1.251 4403`
3. Ensure the node has TCP API enabled in Meshtastic settings

### Web GUI not loading

```bash
# Check container status
docker compose ps

# View webgui logs
docker compose logs webgui
```

### Service won't restart from Web GUI

Ensure Docker socket is mounted:
```yaml
volumes:
  - /var/run/docker.sock:/var/run/docker.sock:ro
```

## License

This project is licensed under the **GNU General Public License v3.0** - see the [LICENSE](LICENSE) file for details.

This is the same license as the original [meshing-around](https://github.com/spudgunman/meshing-around) project, as required by GPL-3.0 for derivative works.

## Acknowledgments

- **[spudgunman](https://github.com/spudgunman)** - Original meshing-around MeshBOT
- **[meshing-around contributors](https://github.com/spudgunman/meshing-around/graphs/contributors)** - Core bot functionality
- **[Meshtastic](https://meshtastic.org/)** - The mesh networking platform
