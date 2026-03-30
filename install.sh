#!/usr/bin/env bash
# Rawhide Image Viewer - Installer for Debian/Ubuntu Linux
set -euo pipefail

APP_NAME="rawhide"
INSTALL_DIR="/usr/local/bin"
APP_DIR="/usr/local/share/rawhide"
VENV_DIR="$APP_DIR/venv"
DESKTOP_DIR="/usr/local/share/applications"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ---------------------------------------------------------------
# Root check
# ---------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
    warn "Not running as root — attempting sudo..."
    exec sudo bash "$0" "$@"
fi

info "Installing Rawhide Image Viewer..."

# ---------------------------------------------------------------
# System dependencies
# ---------------------------------------------------------------
info "Checking system packages..."
PKGS=()
for pkg in python3 python3-full python3-gi python3-gi-cairo \
           gir1.2-gtk-3.0 gir1.2-gdkpixbuf-2.0 libgtk-3-0; do
    dpkg -s "$pkg" &>/dev/null || PKGS+=("$pkg")
done

if [[ ${#PKGS[@]} -gt 0 ]]; then
    info "Installing system packages: ${PKGS[*]}"
    apt-get update -qq
    apt-get install -y "${PKGS[@]}"
fi

# Optional: libraw for better NEF compilation support
if ! dpkg -s libraw-dev &>/dev/null 2>&1; then
    info "Installing libraw-dev for NEF support..."
    apt-get install -y libraw-dev || warn "libraw-dev not available — rawpy will use its bundled libraw."
fi

# ---------------------------------------------------------------
# Virtual environment
# Newer Debian/Ubuntu (PEP 668) forbid pip install into the system
# Python. We create an isolated venv under $APP_DIR instead.
# The venv is configured with --system-site-packages so that the
# system-installed PyGObject (python3-gi) remains available, since
# it cannot be pip-installed on Debian.
# ---------------------------------------------------------------
info "Creating virtual environment at $VENV_DIR ..."
mkdir -p "$APP_DIR"
python3 -m venv --system-site-packages "$VENV_DIR"

info "Installing Python packages into venv (rawpy, Pillow, numpy)..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet rawpy Pillow numpy

# ---------------------------------------------------------------
# Install application file
# ---------------------------------------------------------------
info "Copying application files..."
cp "$SCRIPT_DIR/rawhide.py" "$APP_DIR/rawhide.py"
chmod 644 "$APP_DIR/rawhide.py"

# Launcher: explicitly call the venv Python so the correct packages
# are found at runtime, regardless of the system default python3.
cat > "$INSTALL_DIR/$APP_NAME" <<LAUNCHER
#!/usr/bin/env bash
exec "$VENV_DIR/bin/python3" "$APP_DIR/rawhide.py" "\$@"
LAUNCHER
chmod 755 "$INSTALL_DIR/$APP_NAME"

# ---------------------------------------------------------------
# Desktop file
# ---------------------------------------------------------------
mkdir -p "$DESKTOP_DIR"
info "Installing .desktop entry..."
cp "$SCRIPT_DIR/rawhide.desktop" "$DESKTOP_DIR/rawhide.desktop"
chmod 644 "$DESKTOP_DIR/rawhide.desktop"
update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

# Register MIME types
if command -v xdg-mime &>/dev/null; then
    xdg-mime default rawhide.desktop image/jpeg
    xdg-mime default rawhide.desktop image/png
    xdg-mime default rawhide.desktop image/x-nikon-nef
    update-mime-database /usr/local/share/mime 2>/dev/null || true
fi

# ---------------------------------------------------------------
# Done
# ---------------------------------------------------------------
echo ""
info "Installation complete!"
info "Run:  rawhide [image_file]"
info "Or launch from your application menu as 'Rawhide'"
