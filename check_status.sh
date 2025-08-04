#!/bin/bash
# Enhanced Status Checker v1.12
echo "Lightning Detector Enhanced Status v1.12"
echo "========================================"
echo "Service Status:"
sudo systemctl status lightning-detector --no-pager -l
echo ""
echo "Recent Logs:"
sudo journalctl -u lightning-detector --no-pager -l -n 20
