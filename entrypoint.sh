#!/bin/bash
set -e
echo "🔄 Checking for SpotiFLAC updates..."
pip install --upgrade --quiet SpotiFLAC
echo "✅ SpotiFLAC $(pip show SpotiFLAC | grep Version | awk '{print $2}') ready"
echo "🚀 Starting SpotiFLAC Web UI on :7171"
exec python app.py
