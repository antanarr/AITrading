#!/bin/bash
# Helper script to update the app icon
# Usage: ./Scripts/update_app_icon.sh /path/to/your/icon.png

ICON_SOURCE="$1"
ICON_DEST="Resources/Assets.xcassets/AppIcon.appiconset/AppIcon.png"

if [ -z "$ICON_SOURCE" ]; then
    echo "Usage: $0 /path/to/icon.png"
    echo "The icon should be 1024x1024 PNG"
    exit 1
fi

if [ ! -f "$ICON_SOURCE" ]; then
    echo "Error: Source file not found: $ICON_SOURCE"
    exit 1
fi

# Check if it's a PNG (basic check)
if ! file "$ICON_SOURCE" | grep -q "PNG"; then
    echo "Warning: File doesn't appear to be a PNG"
fi

# Copy the icon
cp "$ICON_SOURCE" "$ICON_DEST"
echo "✓ App icon updated: $ICON_DEST"

# Verify dimensions if sips is available (macOS)
if command -v sips &> /dev/null; then
    DIMENSIONS=$(sips -g pixelWidth -g pixelHeight "$ICON_DEST" | grep -E "(pixelWidth|pixelHeight)" | awk '{print $2}' | tr '\n' 'x')
    echo "  Dimensions: ${DIMENSIONS}x (should be 1024x1024)"
fi
