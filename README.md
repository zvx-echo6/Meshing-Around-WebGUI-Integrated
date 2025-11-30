# Meshing-Around WebGUI Integrated

A feature-rich Meshtastic BBS bot with a full web management interface.

> **Note:** This project was developed with AI assistance using Claude (Anthropic). The codebase was "vibe coded" through collaborative sessions with multiple Claude agents. We believe in transparency about AI-assisted development.

---

## About This Fork

This is a fork of [SpudGunMan/meshing-around](https://github.com/spudgunman/meshing-around) with an integrated WebGUI for configuration and monitoring. The WebGUI requires modifications to core files, so this is distributed as a complete integrated package rather than a standalone addon.

**What's Different:**
- Full web-based management interface (WebGUI)
- Real-time packet monitoring
- Visual scheduler management
- Node name resolution on leaderboards
- TCP hostname:port bug fix

---

## Quick Start

### Docker (Recommended)

```bash
git clone https://forge.echo6.co/matt/Meshing-Around-WebGUI-Integrated.git
cd Meshing-Around-WebGUI-Integrated
cp config.template config.ini
# Edit config.ini with your settings
docker-compose up -d
```

Access the WebGUI at `http://your-server:8085`

### Manual Installation

See [INSTALL.md](INSTALL.md) for detailed instructions.

---

## WebGUI Features

The integrated web interface provides complete control over your mesh bot:

| Feature | Description |
|---------|-------------|
| **Dashboard** | Service status, statistics, and leaderboard |
| **Radio Connections** | Configure up to 9 Meshtastic interfaces (Serial/TCP/BLE) |
| **Scheduler** | Visual management of scheduled broadcasts |
| **Mesh Nodes** | View all nodes on your mesh network |
| **BBS Network** | Track BBS peer synchronization status |
| **Packet Monitor** | Real-time packet inspection |
| **System Logs** | Filterable log viewer with auto-refresh |
| **Configuration** | Edit all config.ini sections with validation |

### Screenshots

#### Dashboard
![Dashboard](webgui/docs/screenshots/Dashboard.png)

#### Radio Connections
![Radio Connections](webgui/docs/screenshots/Radio_Connections.png)

#### Mesh Nodes
![Mesh Nodes](webgui/docs/screenshots/Mesh_Nodes.png)

#### Scheduler
![Scheduler](webgui/docs/screenshots/Scheduler.png)
![Custom Schedules](webgui/docs/screenshots/Custom_Schedules.png)

#### BBS Network
![BBS Network](webgui/docs/screenshots/BBS_Network.png)

#### System Logs
![System Logs](webgui/docs/screenshots/System_Logs.png)

#### Configuration
![General Settings](webgui/docs/screenshots/General_Settings.png)
![BBS Config](webgui/docs/screenshots/BBS_Config.png)

#### Activity Log
![Activity Log](webgui/docs/screenshots/Activity_Log.png)

---

## Bot Features

This fork includes all features from the upstream meshing-around project:

### Messaging & Communication
- **BBS Mail System** - Leave messages for offline nodes, delivered when they return
- **Message Scheduler** - Automate weather updates, net reminders, announcements
- **Store and Forward** - Retrieve missed messages
- **BBS Linking** - Connect multiple bots for expanded coverage
- **Email/SMS Integration** - Bridge mesh messages to email or SMS
- **Multi-Radio Support** - Monitor up to 9 networks simultaneously

### Network Tools
- **Ping/Pong Testing** - Test message delivery with realistic packets
- **Hardware Testing** - Test radio buffer limits with incrementally sized data
- **Network Monitoring** - Alerts for noisy nodes, location tracking, relay placement suggestions
- **Site Survey** - Log GPS locations with descriptions for mapping

### Data & Alerts
- **Weather/Earthquake/Tide Data** - NOAA/USGS alerts and Open-Meteo support
- **Emergency Alerts** - FEMA iPAWS, NOAA EAS, USGS Volcano alerts
- **Proximity Alerts** - Location-based notifications for geo-fences
- **RSS/News Feeds** - Receive news directly on the mesh
- **Wikipedia/Kiwix Search** - Information lookup over mesh

### AI Integration
- **Ollama/OpenWebUI** - LLM integration with RAG support
- **Voice Commands** - "Hey Chirpy!" voice activation
- **Speech-to-Text** - Vosk integration for audio broadcasting

### Games & Fun
- DopeWars, Lemonade Stand, BlackJack, Video Poker
- FCC/ARRL QuizBot for ham radio exam practice
- Group quiz games with leaderboards
- Telemetry competitions (lowest battery, coldest temp)

### Radio Integration
- Hamlib/rigctld S-meter monitoring
- WSJT-X and JS8Call message forwarding
- Tone-out decoder for fire alerts
- Text-to-speech mesh messages

For complete feature documentation, see [modules/README.md](modules/README.md).

---

## Configuration

Copy the template and edit for your setup:

```bash
cp config.template config.ini
```

Key sections:
- `[interface]` - Radio connections (serial, TCP, BLE)
- `[general]` - Bot behavior and admin settings
- `[bbs]` - BBS configuration and linking
- `[location]` - GPS and weather settings

See [modules/README.md](modules/README.md) for all configuration options.

---

## Technical Details

### WebGUI Modifications to Upstream

This fork includes these changes to core files:

**`mesh_bot.py`** - Packet buffer for real-time monitoring (~225 lines added)
```python
# Thread-safe packet capture for WebGUI Packet Monitor
# Requires DEBUGpacket = True in config.ini
```

**`modules/system.py`** - Leaderboard enhancement
```python
# Adds node shortName/longName to leaderboard entries
# WebGUI displays names instead of hex IDs
```

**`modules/system.py`** - TCP bug fix
```python
# Fixes hostname:port parsing for TCP interfaces
```

For full technical details, see [webgui/README.md](webgui/README.md).

### Project Structure

```
├── mesh_bot.py          # Main bot application
├── pong_bot.py          # Lightweight responder
├── config.template      # Configuration template
├── compose.yaml         # Docker Compose configuration
├── modules/             # Bot modules and features
├── webgui/              # Web management interface
│   ├── main.py          # FastAPI backend
│   ├── config_schema.py # Configuration definitions
│   └── templates/       # Frontend (Tailwind CSS)
├── data/                # Runtime data storage
└── logs/                # Log files
```

---

## Development Notes

### AI-Assisted Development

This project was developed using AI pair programming with Claude. The development process involved:

- Iterative feature development through conversation
- Code generation with human review and refinement
- Documentation written collaboratively

We're transparent about this because:
1. It's the honest thing to do
2. AI-assisted code may have different characteristics than traditionally written code
3. Users should make informed decisions about the software they run

The code works, but like all software, review it before running in production.

---

## Acknowledgments

### Original Project

This fork is built on the excellent work of **SpudGunMan** and the meshing-around community:

**[SpudGunMan/meshing-around](https://github.com/spudgunman/meshing-around)**

### Upstream Contributors

The upstream project was made possible by these contributors:

**Inspiration & Code:**
- [MeshLink](https://github.com/Murturtle/MeshLink)
- [Meshtastic Python Examples](https://github.com/pdxlocations/meshtastic-Python-Examples)
- [Meshtastic Matrix Relay](https://github.com/geoffwhittington/meshtastic-matrix-relay)

**Games Ported From:**
- [Lemonade Stand](https://github.com/tigerpointe/Lemonade-Stand/)
- [Drug Wars](https://github.com/Reconfirefly/drugwars)
- [BlackJack](https://github.com/Himan10/BlackJack)
- [Video Poker](https://github.com/devtronvarma/Video-Poker-Terminal-Game)
- [Mastermind](https://github.com/pwdkramer/pythonMastermind/)
- [Golf](https://github.com/danfriedman30/pythongame)
- ARRL Question Pool from [russolsen/ham_radio_question_pool](https://github.com/russolsen/ham_radio_question_pool)

**Community Contributors:**
- **PiDiBi, Cisien, bitflip, nagu, Nestpebble, NomDeTom, Iris, Josh, GlockTuber, FJRPiolt, dj505, Woof, propstg, snydermesh, trs2982, F0X, Malice, mesb1, Hailo1999** - Testing and feature ideas
- **xdep** - HTML reporting
- **mrpatrick1991** - Original Docker configurations
- **A-c0rN** - iPAWS/EAS assistance
- **Mike O'Connell/skrrt & sheer.cold** - EAS alert parser
- **dadud** - ICAD tone decoder idea
- **WH6GXZ** - Volcano alerts
- **mikecarper** - Ham test/quiz features
- **c.merphy360** - High altitude alerts
- **G7KSE** - DX spotting idea
- **Meshtastic Discord Community**

### This Fork

**WebGUI Development:**
- AI-assisted development using Claude (Anthropic)
- Maintained by [matt/zvx-echo6](https://forge.echo6.co/matt)

---

## Links

- **This Fork:** [forge.echo6.co/matt/Meshing-Around-WebGUI-Integrated](https://forge.echo6.co/matt/Meshing-Around-WebGUI-Integrated)
- **Upstream:** [github.com/SpudGunMan/meshing-around](https://github.com/spudgunman/meshing-around)
- **Standalone WebGUI Addon:** [forge.echo6.co/matt/Meshing-Around-WebGUI](https://forge.echo6.co/matt/Meshing-Around-WebGUI)
- **Meshtastic:** [meshtastic.org](https://meshtastic.org)

---

## License

MIT License - Same as upstream meshing-around.

Meshtastic® is a registered trademark of Meshtastic LLC.

---

*Use responsibly. This software captures packets, logs communications, and may handle PII including GPS locations. Follow local regulations for radio equipment.*
