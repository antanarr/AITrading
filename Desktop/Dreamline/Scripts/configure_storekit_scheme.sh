#!/bin/bash
# Helper script to configure StoreKit in Xcode scheme
# Note: This requires manual step in Xcode if xcodebuild doesn't support it

SCHEME="Dreamline"
CONFIG_PATH="Config/StoreKit.storekit"

echo "⚠️  StoreKit Configuration"
echo ""
echo "To enable StoreKit testing in the simulator:"
echo "1. Open Dreamline.xcodeproj in Xcode"
echo "2. Product → Scheme → Edit Scheme..."
echo "3. Select 'Run' in the left sidebar"
echo "4. Go to 'Options' tab"
echo "5. Under 'StoreKit Configuration', select:"
echo "   $CONFIG_PATH"
echo "6. Click 'Close'"
echo ""
echo "Then test purchases will work in the simulator!"
