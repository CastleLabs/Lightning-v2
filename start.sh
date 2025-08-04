#!/bin/bash
# Lightning Detector Enhanced Startup Script v1.12
echo "Starting Lightning Detector Enhanced v1.12..."
echo "Enhanced Alert System: Warning/Critical/All-Clear"
cd "$(dirname "$0")"
source lightning_detector_env/bin/activate
python lightning.py
