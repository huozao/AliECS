@echo off
set PYTHONPATH=%~dp0..\src;%~dp0..
python -m tplus_datahub.jobs.job_sync_all
