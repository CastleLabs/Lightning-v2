#!/bin/bash
# Enhanced Log Viewer v1.12
echo "Lightning Detector Enhanced Logs v1.12"
echo "======================================"
if [ -f "lightning_detector.log" ]; then
    tail -f lightning_detector.log
else
    echo "No log file found. Start the application first."
fi
