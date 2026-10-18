#!/bin/bash
PROJECT_DIR="/home/anupam/Downloads/HeroviredNotes/Assignments/CAPSTONE PROJECT/Project3"
cd "$PROJECT_DIR"
source venv/bin/activate

# 1. Fetch live metrics across Azure, GCP, and MinIO
python3 collector/main_collector.py >> collector/collector.log 2>&1

# 2. Run automated alert evaluations
python3 collector/alert_engine.py >> collector/alerts.log 2>&1
