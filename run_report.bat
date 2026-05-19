@echo off
set PYTHONIOENCODING=utf-8
cd /d C:\Users\hyttg\daily-research
python daily_report.py >> C:\Users\hyttg\daily-research\log.txt 2>&1
