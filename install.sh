#!/usr/bin/env bash
# Rawhide Image Viewer - Installer for Debian/Ubuntu Linux
set -euo pipefail

APP_NAME="rawhide"
INSTALL_DIR="/usr/local/bin"
APP_DIR="/usr/local/share/rawhide"
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
for pkg in python3 python3-gi python3-pip gir1.2-gtk-3.0 libgtk-3-0; do
    dpkg -s "$pkg" &>/dev/null || PKGS+=("$pkg")
done

if [[ ${#PKGS[@]} -gt 0 ]]; then
    info "Installing: ${PKGS[*]}"
    apt-get update -qq
    apt-get install -y "${PKGS[@]}"
fi

# Optional: libraw for better NEF support
if ! dpkg -s libraw-dev &>/dev/null 2>&1; then
    info "Installing libraw-dev for NEF support..."
    apt-get install -y libraw-dev || warn "libraw-dev not available, rawpy may still work via bundled C library."
fi

# ---------------------------------------------------------------
# Python packages
# ---------------------------------------------------------------
info "Installing Python packages (rawpy, Pillow, numpy)..."
pip3 install --quiet rawpy Pillow numpy

# ---------------------------------------------------------------
# Install application files
# ---------------------------------------------------------------
info "Copying application files..."
mkdir -p "$APP_DIR"
cp "$SCRIPT_DIR/rawhide.py" "$APP_DIR/rawhide.py"
chmod 644 "$APP_DIR/rawhide.py"

# Create launcher script
cat > "$INSTALL_DIR/$APP_NAME" <<'LAUNCHER'
#!/usr/bin/env bash
exec python3 /usr/local/share/rawhide/rawhide.py "$@"
LAUNCHER
chmod 755 "$INSTALL_DIR/$APP_NAME"

# ---------------------------------------------------------------
# Desktop file
# ---------------------------------------------------------------
if [[ -d "$DESKTOP_DIR" ]]; then
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
fi

# ---------------------------------------------------------------
# Done
# ---------------------------------------------------------------
echo ""
info "Installation complete!"
info "Run:  rawhide [image_file]"
info "Or launch from your application menu as 'Rawhide'"
