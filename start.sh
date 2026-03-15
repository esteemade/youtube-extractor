#!/bin/bash

# Start PO token server in background (using Docker approach)
# This is the key part - it generates tokens automatically
npx --yes bgutil-ytdlp-pot-provider@latest server --port 4416 &

# Give it a few seconds to start
sleep 3

# Start Flask app
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120
