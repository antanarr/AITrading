#!/bin/bash
set -euo pipefail
project_dir="$(cd "$(dirname "$0")" && pwd)"
app_dir="$HOME/Library/Application Support/AITrading/AITrading Credentials.app"
if [ ! -x "$app_dir/Contents/MacOS/AITradingCredentials" ]; then
  "$project_dir/tools/build-credentials.sh"
fi
exec /usr/bin/open "$app_dir"
