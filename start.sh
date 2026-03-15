#!/bin/bash

# Install/update yt-dlp with PO token support
pip install --upgrade yt-dlp
pip install curl-cffi

# Start the Flask app
gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120 --worker-class sync
