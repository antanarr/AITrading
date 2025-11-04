# Fastlane Automation (Stub)

This stub allows local builds without TestFlight credentials.

**Setup once you're ready to distribute:**

1. Install fastlane (`brew install fastlane`).

2. Edit `Scripts/fastlane/Appfile` with your Apple ID and Team ID.

3. Run:
   ```bash
   cd Scripts/fastlane
   fastlane build    # Build debug
   fastlane beta     # Upload to TestFlight
   ```

**Current lanes:**
- `build`: Builds the app for iOS simulator (Debug configuration)
- `beta`: Stub that prints a message (replace with actual TestFlight upload when ready)

**Note:** These are placeholder scripts. They compile without Fastlane installed and won't break the Xcode build process.
