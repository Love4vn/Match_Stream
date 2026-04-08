name: Generate Live M3U + IPTV Checker

on:
  schedule:
    - cron: '0 */2 * * *'      # Chạy mỗi 2 giờ
  workflow_dispatch:

jobs:
  generate:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: |
          pip install requests
          sudo apt-get update && sudo apt-get install -y ffmpeg   # Cài ffprobe

      - name: Run generator with IPTV Checker
        run: python generate_live_m3u.py

      - name: Commit and push if changed
        run: |
          git config --global user.name "GitHub Actions"
          git config --global user.email "actions@github.com"
          git add Stream_live.m3u
          if git diff --staged --quiet; then
            echo "No changes"
          else
            git commit -m "Update Stream_live.m3u + IPTV Checker - $(date '+%Y-%m-%d %H:%M:%S')"
            git push
          fi
