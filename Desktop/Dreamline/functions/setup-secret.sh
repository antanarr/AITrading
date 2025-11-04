#!/bin/bash
# Script to set Firebase Functions secret
# Usage: ./setup-secret.sh

echo "Setting OpenAI API key as Firebase Functions secret..."
echo "⚠️  You will be prompted to paste your API key"
firebase functions:secrets:set OPENAI_API_KEY

if [ $? -eq 0 ]; then
    echo "✅ Secret set successfully!"
    echo "Next steps:"
    echo "1. Run: firebase deploy --only functions"
else
    echo "❌ Failed to set secret. Please run manually:"
    echo "   firebase functions:secrets:set OPENAI_API_KEY"
    echo "   (then paste your API key when prompted)"
fi

